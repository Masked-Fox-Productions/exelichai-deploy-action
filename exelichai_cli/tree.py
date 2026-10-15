"""The repo tree on the creator's disk: what belongs to it, the limits it is
held to before anything is uploaded, and confined writes for ``init`` / ``pull``.

The limits are VENDORED from the server (``services/repo_sync/tree_limits.py``,
``services/core/game_bundle.py``, ``services/repo_sync/safe_yaml.py``) so a
creator hears about a bad path before an upload, not after. The server checks
everything again; the CLI is never the only gate. ``tests/test_tree.py`` pins
these values against the server's source (the parity test) -- change both or
neither.

What the CLI does NOT vendor: media magic bytes and image pixel caps (the
server reports those with the file's path), and YAML parsing beyond reading the
manifest's ``rules:`` list.

Two rules hold for every filesystem touch here:

* **symlinks are never followed** -- a link inside the tree is refused, a link
  on the way to a write target is refused, and files are opened with
  ``O_NOFOLLOW`` where the platform has it;
* **paths are confined before any write** -- every server-supplied path passes
  :func:`path_error` and resolves inside the folder, or nothing is written.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from exelichai_cli.output import CliError, EXIT_FAILED, Problem

# --- Vendored limits (parity-tested against the server's source) ----------

MAX_FILES = 2000
MAX_MEDIA_BYTES = 10 * 1024 * 1024        # tree_limits.MAX_MEDIA_BYTES
MAX_TREE_BYTES = 200 * 1024 * 1024        # tree_limits.MAX_TREE_BYTES
MAX_YAML_BYTES = 1024 * 1024              # safe_yaml.MAX_FILE_BYTES

#: ``game_bundle.ALLOWED_BUNDLE_EXTENSIONS``.
ALLOWED_EXTENSIONS = frozenset({
    ".json", ".yaml", ".yml",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    ".ogg", ".wav", ".mp3", ".opus", ".flac", ".m4a", ".aac",
    ".mp4", ".webm", ".mov", ".m4v",
    ".ttf", ".otf", ".woff", ".woff2",
    ".md", ".txt",
})
#: ``game_bundle._ARCHIVE_EXTENSIONS``.
ARCHIVE_EXTENSIONS = (".zip", ".tar", ".gz", ".bz2", ".7z", ".rar")

# --- The repo format's fixed names (format.py) ----------------------------

MANIFEST = "exelichai.yaml"
PRESENTATION_DIR = "presentation/"
MEDIA_DIR = "media/"
YAML_EXTENSIONS = (".yaml", ".yml")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def path_error(path: object) -> Optional[Problem]:
    """Why ``path`` is not a safe tree path, or ``None`` (the server's
    ``tree_limits.path_error``, same order, same codes)."""
    shown = repr(path)
    if not isinstance(path, str) or not path:
        return Problem("path-unsafe", f"{shown} is not a path")
    where = path.replace("\x00", "\\0")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return Problem("path-unsafe", f"{shown} contains a NUL or control character", file=where)
    if not path.strip():
        return Problem("path-unsafe", f"{shown} is not a path", file=where)
    if "\\" in path:
        return Problem("path-unsafe", f"backslash separators are not allowed ({shown})", file=where)
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return Problem("path-unsafe", f"must be folder-relative, got absolute {shown}", file=where)
    parts = path.split("/")
    if any(part in ("..", "") for part in parts) or posixpath.normpath(path).startswith(".."):
        return Problem("path-unsafe", f"unsafe path component in {shown}", file=where)
    if any(part == "." for part in parts):
        return Problem("path-unsafe", f"{shown} has a '.' component", file=where)
    if path.lower().endswith(ARCHIVE_EXTENSIONS):
        return Problem("path-nested-archive", f"{shown} is an archive", file=where)
    ext = posixpath.splitext(path.lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        return Problem("path-extension", f"{shown} has extension {ext or '(none)'}", file=where)
    return None


def case_collisions(paths: Iterable[str]) -> List[Problem]:
    """Paths -- or directories on them -- equal under case folding."""
    seen: Dict[str, str] = {}
    reported = set()
    out: List[Problem] = []
    for path in sorted(paths):
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            folded = prefix.casefold()
            other = seen.setdefault(folded, prefix)
            if other != prefix and folded not in reported:
                reported.add(folded)
                out.append(Problem("path-case-collision",
                                   f"{prefix!r} and {other!r} differ only by case", file=prefix))
    return out


def check_tree(files: Mapping[str, bytes], *, symlinks: Iterable[str] = ()) -> List[Problem]:
    """Every vendored-limit violation in ``files`` (path -> bytes)."""
    problems: List[Problem] = [Problem("path-symlink", f"{link!r} is a symbolic link", file=link)
                               for link in sorted(symlinks)]
    if len(files) > MAX_FILES:
        problems.append(Problem("tree-too-many-files",
                                f"{len(files)} files is over the {MAX_FILES} file limit"))
    total = sum(len(b) for b in files.values())
    if total > MAX_TREE_BYTES:
        problems.append(Problem("tree-too-large",
                                f"{total} bytes is over the {MAX_TREE_BYTES} byte limit"))
    safe: List[str] = []
    for path in sorted(files):
        err = path_error(path)
        if err:
            problems.append(err)
            continue
        safe.append(path)
        size = len(files[path])
        if path.startswith(MEDIA_DIR) and size > MAX_MEDIA_BYTES:
            problems.append(Problem("media-too-large",
                                    f"{size} bytes is over the {MAX_MEDIA_BYTES} byte limit",
                                    file=path))
        elif path.lower().endswith(YAML_EXTENSIONS) and size > MAX_YAML_BYTES:
            problems.append(Problem("yaml-file-too-large",
                                    f"{size} bytes is over the {MAX_YAML_BYTES} byte limit",
                                    file=path))
    problems.extend(case_collisions(safe))
    return problems


# ---------------------------------------------------------------------------
# Hashing and the upload manifest
# ---------------------------------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_manifest(files: Mapping[str, bytes]) -> List[Dict[str, object]]:
    """``[{path, sha256, size}]`` sorted by path -- the manifest/finalise body."""
    return [{"path": p, "sha256": sha256(files[p]), "size": len(files[p])} for p in sorted(files)]


# ---------------------------------------------------------------------------
# The folder
# ---------------------------------------------------------------------------

def resolve_folder(workspace: str, folder: str) -> str:
    """The absolute folder ``folder`` names inside ``workspace``. Refuses a
    folder that escapes the workspace or reaches it through a symlink."""
    workspace = os.path.abspath(workspace)
    folder = (folder or ".").strip() or "."
    if os.path.isabs(folder):
        target = os.path.abspath(folder)
    else:
        if "\\" in folder or any(p == ".." for p in folder.replace(os.sep, "/").split("/")):
            raise CliError([Problem("path-unsafe", f"the folder {folder!r} escapes the workspace")],
                           EXIT_FAILED)
        target = os.path.abspath(os.path.join(workspace, folder))
    rel = os.path.relpath(target, workspace)
    if rel == ".." or rel.startswith(".." + os.sep):
        raise CliError([Problem("path-unsafe", f"the folder {folder!r} is outside {workspace}")],
                       EXIT_FAILED)
    # No component between the workspace and the folder may be a link.
    cur = workspace
    for part in ([] if rel == "." else rel.split(os.sep)):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise CliError([Problem("path-symlink", f"{os.path.relpath(cur, workspace)!r} is a "
                                    "symbolic link; the folder must be a real directory")],
                           EXIT_FAILED)
    return target


def manifest_rules(root: str) -> Tuple[List[str], Optional[str]]:
    """``(rules files, game_id)`` the folder's ``exelichai.yaml`` lists. A
    missing or unreadable manifest gives ``([], None)``: the server's decode
    reports it with its line."""
    path = os.path.join(root, MANIFEST)
    if not os.path.isfile(path) or os.path.islink(path):
        return [], None
    try:
        import yaml  # PyYAML: the one third-party dependency

        with open(path, "rb") as fh:
            data = fh.read(MAX_YAML_BYTES + 1)
        if len(data) > MAX_YAML_BYTES:
            return [], None
        doc = yaml.safe_load(data)
    except Exception:  # noqa: BLE001 -- the server reports the real fault
        return [], None
    if not isinstance(doc, dict):
        return [], None
    rules = doc.get("rules")
    game_id = doc.get("game_id")
    return ([r for r in rules if isinstance(r, str)] if isinstance(rules, list) else [],
            game_id if isinstance(game_id, str) else None)


def in_tree_area(path: str, rules: Sequence[str] = ()) -> bool:
    """True for a path the repo format owns: the manifest, a listed rules
    file, ``presentation/**`` or ``media/**``. Everything else in the folder
    (README, LICENSE, workflows) is the creator's and never touched."""
    return (path == MANIFEST or path in set(rules)
            or path.startswith((PRESENTATION_DIR, MEDIA_DIR)))


def walk(root: str, include: Callable[[str], bool]) -> Tuple[Dict[str, bytes], List[str]]:
    """``(files, symlinks)`` under ``root`` that ``include`` accepts. Never
    follows a link; hidden entries (``.git``, ``.github``) are skipped. A
    directory is offered to ``include`` as ``"<dir>/"``."""
    files: Dict[str, bytes] = {}
    links: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        keep = []
        for d in sorted(dirnames):
            if d.startswith("."):
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                if include(rel_dir + d + "/"):
                    links.append(rel_dir + d)
                continue
            if include(rel_dir + d + "/"):
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = rel_dir + name
            if name.startswith(".") or not include(rel):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                links.append(rel)
                continue
            files[rel] = _read_nofollow(full)
    return files, links


def _include_for(rules: Sequence[str]) -> Callable[[str], bool]:
    rule_set = set(rules)

    def include(path: str) -> bool:
        if path.endswith("/"):
            return (path.startswith((PRESENTATION_DIR, MEDIA_DIR))
                    or any(r.startswith(path) for r in rules))
        return path == MANIFEST or path in rule_set or path.startswith((PRESENTATION_DIR, MEDIA_DIR))

    return include


def read_tree(root: str) -> Dict[str, bytes]:
    """The tree in ``root`` (the server's ``format.read_repo_folder``), held to
    the vendored limits. Raises :class:`CliError` listing every problem."""
    rules, _ = manifest_rules(root)
    files, links = walk(root, _include_for(rules))
    problems = check_tree(files, symlinks=links)
    if problems:
        raise CliError(problems, EXIT_FAILED)
    return files


def existing_tree(root: str) -> Dict[str, bytes]:
    """The tree-area files already in ``root``, for diff summaries. Lenient:
    links are left out (a write over one is refused later)."""
    rules, _ = manifest_rules(root)
    files, _links = walk(root, _include_for(rules))
    return files


# ---------------------------------------------------------------------------
# Confined writes
# ---------------------------------------------------------------------------

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _read_nofollow(full: str) -> bytes:
    fd = os.open(full, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def confined_target(root: str, rel: str) -> str:
    """The absolute path for tree path ``rel`` under ``root``: ``rel`` must be
    a safe tree path and no existing component on the way may be a link."""
    err = path_error(rel)
    if err:
        raise CliError([err], EXIT_FAILED)
    root = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([root, target]) != root or target == root:
        raise CliError([Problem("path-unsafe", f"{rel!r} escapes the folder", file=rel)],
                       EXIT_FAILED)
    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise CliError([Problem("path-symlink", f"{rel!r} goes through a symbolic link",
                                    file=rel)], EXIT_FAILED)
    return target


def preflight_writes(root: str, paths: Iterable[str]) -> None:
    """Confine EVERY path before the first byte is written: one bad path
    refuses the whole write."""
    problems: List[Problem] = []
    for rel in paths:
        try:
            confined_target(root, rel)
        except CliError as exc:
            problems.extend(exc.problems)
    if problems:
        raise CliError(problems, EXIT_FAILED)


def write_file(root: str, rel: str, data: bytes) -> None:
    target = confined_target(root, rel)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    # Re-check after makedirs: nothing on the way may have become a link.
    confined_target(root, rel)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def delete_file(root: str, rel: str) -> None:
    """Remove one regular file (never a link's target); prune empty parents
    up to, not including, the tree-area roots."""
    target = confined_target(root, rel)
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise CliError([Problem("path-unsafe", f"{rel!r} is not a regular file", file=rel)],
                       EXIT_FAILED)
    os.unlink(target)
    root = os.path.abspath(root)
    parent = os.path.dirname(target)
    while parent != root and os.path.dirname(parent) != root:
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)


def diff(before: Mapping[str, bytes], after: Mapping[str, bytes], *,
         scope: Optional[Callable[[str], bool]] = None) -> Dict[str, List[str]]:
    """``{added, changed, removed, unchanged}`` path lists (``removed`` only
    for paths ``scope`` accepts)."""
    in_scope = scope or (lambda _p: True)
    added = sorted(p for p in after if p not in before)
    changed = sorted(p for p in after if p in before and before[p] != after[p])
    unchanged = sorted(p for p in after if p in before and before[p] == after[p])
    removed = sorted(p for p in before if p not in after and in_scope(p))
    return {"added": added, "changed": changed, "removed": removed, "unchanged": unchanged}
