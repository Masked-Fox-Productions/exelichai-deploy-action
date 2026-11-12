"""The repo tree on the creator's disk: what belongs to it, how each file is
classified, the limits it is held to before anything is uploaded, and
confined writes for ``init`` / ``pull``.

The limits and the classifier are VENDORED from the server
(``services/repo_sync/tree_limits.py``, ``media_class.py``,
``services/core/content_media.py``, ``game_bundle.py``, ``safe_yaml.py``) so
a creator hears about a bad path before an upload, not after. The server
checks everything again and is authoritative: the CLI is never the only gate,
and its local limits are the most a stage ever accepts (the per-kind caps and
per-game totals of plan 2026-10-26-002 Unit 1), so it never refuses what the
server would take. ``tests/test_tree.py`` pins these values against the
server's source (the parity test) -- change both or neither.

**The scan** (:func:`scan`, plan 2026-10-26-002 Unit 3, KTD-8) replaces
reading the tree into memory: per file it records the path, size, sha256
(hashed in chunks) and structural class. Media bytes are never held; a rules
file is read on demand. A file under ``media/`` of a type the sync ignores (a
``.psd``, an ``.html``) is IGNORED: never hashed, never sent, never refused,
only counted.

What the CLI does NOT vendor: media magic bytes and image pixel caps (the
server reports those with the file's path), whether a candidate (text or SVG
under ``media/``) is referenced (the server derives it from the rules it
holds), and YAML parsing beyond reading the manifest's ``rules:`` list.

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
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from exelichai_cli.output import CliError, EXIT_FAILED, Problem

# --- Vendored limits (parity-tested against the server's source) ----------

#: Today's single-request totals and per-file cap (``tree_limits``): what a
#: stage enforces until its media domain is verified. Kept for the export
#: archive's own bound; the scan holds a tree to the raised limits below.
MAX_FILES = 2000
MAX_MEDIA_BYTES = 10 * 1024 * 1024        # tree_limits.MAX_MEDIA_BYTES
MAX_TREE_BYTES = 200 * 1024 * 1024        # tree_limits.MAX_TREE_BYTES
MAX_YAML_BYTES = 1024 * 1024              # safe_yaml.MAX_FILE_BYTES

#: The per-game totals (``media_class.GAME_MAX_FILES`` / ``GAME_MAX_BYTES``):
#: the most any stage accepts, over the files a sync carries.
GAME_MAX_FILES = 20_000
GAME_MAX_BYTES = 10 * 1000 ** 3
#: The rules area's own totals (``tree_limits.RULES_MAX_FILES`` /
#: ``RULES_MAX_BYTES``).
RULES_MAX_FILES = 1000
RULES_MAX_BYTES = 16 * 1024 * 1024

#: ``content_media.MAX_BYTES_BY_KIND`` and the extensions of each cap kind
#: (``content_media.cap_kind``): image 50 MB, audio 100 MB, font 10 MB,
#: video 10 MB, text and SVG 1 MB.
MAX_BYTES_BY_KIND = {
    "image": 50 * 1024 * 1024,
    "audio": 100 * 1024 * 1024,
    "font": 10 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
    "svg": 1024 * 1024,
    "text": 1024 * 1024,
}
CAP_KIND_EXTENSIONS = {
    "image": frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"}),
    "audio": frozenset({".ogg", ".opus", ".wav", ".mp3", ".flac", ".aac", ".m4a"}),
    "video": frozenset({".mp4", ".m4v", ".mov", ".webm"}),
    "font": frozenset({".ttf", ".otf", ".woff", ".woff2"}),
    "svg": frozenset({".svg"}),
    "text": frozenset({".json", ".yaml", ".yml", ".md", ".txt"}),  # content_media.TEXT_EXTENSIONS
}

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

# --- The structural classes (media_class.structural_class) ----------------

RULES = "rules"
BINARY = "binary"
CANDIDATE = "candidate"
IGNORED = "ignored"
#: Text-like files and SVG under ``media/``: published only when the rules
#: reference them (the server decides). ``media_class.CANDIDATE_EXTENSIONS``.
CANDIDATE_EXTENSIONS = frozenset(
    (CAP_KIND_EXTENSIONS["text"] | {".svg"}) & ALLOWED_EXTENSIONS)
#: ``media_class.BINARY_EXTENSIONS``: the admitted extensions less the
#: candidates.
BINARY_EXTENSIONS = frozenset(ALLOWED_EXTENSIONS - CANDIDATE_EXTENSIONS)


def _ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def structural_class(path: str) -> str:
    """:data:`RULES`, :data:`BINARY`, :data:`CANDIDATE` or :data:`IGNORED`
    for a tree-area path (``media_class.structural_class`` with the tree area
    already selected): under ``media/`` by extension; under
    ``presentation/`` a YAML file is rules and anything else is ignored;
    everything else in the tree area is rules."""
    ext = _ext(path)
    if path.startswith(MEDIA_DIR):
        if ext in BINARY_EXTENSIONS:
            return BINARY
        if ext in CANDIDATE_EXTENSIONS:
            return CANDIDATE
        return IGNORED
    if path.startswith(PRESENTATION_DIR):
        return RULES if ext in YAML_EXTENSIONS else IGNORED
    return RULES


def upload_ignored(path: str) -> bool:
    """A file the upload never carries (``uploads.upload_ignored``): an
    ignored type under ``media/``. A ``presentation/`` file the format does
    not parse (its README) is still carried, as the server expects."""
    return path.startswith(MEDIA_DIR) and structural_class(path) == IGNORED


def kind_max_bytes(path: str) -> Optional[int]:
    """The per-kind cap for ``path``'s extension, or ``None``."""
    ext = _ext(path)
    for kind, exts in CAP_KIND_EXTENSIONS.items():
        if ext in exts:
            return MAX_BYTES_BY_KIND[kind]
    return None


def media_max_bytes(path: str) -> int:
    """The largest a stage accepts for the media file ``path``: its per-kind
    cap (a stage whose media domain is not verified yet holds media to
    :data:`MAX_MEDIA_BYTES`, and refuses the rest itself)."""
    cap = kind_max_bytes(path)
    return cap if cap is not None else MAX_MEDIA_BYTES


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def path_error(path: object) -> Optional[Problem]:
    """Why ``path`` is not a path Exelichai writes or uploads as-is, or
    ``None`` (the server's ``tree_limits.strict_path_error``, same order,
    same codes): the extension allowlist applies everywhere."""
    return _path_error(path, strict=True)


def read_path_error(path: object) -> Optional[Problem]:
    """Why ``path`` is not a safe tree path to READ, or ``None`` (the
    server's ``tree_limits.path_error``): under ``media/`` and
    ``presentation/`` any extension passes -- the classifier ignores a type
    the sync does not read rather than refuse it."""
    return _path_error(path, strict=False)


def _path_error(path: object, *, strict: bool) -> Optional[Problem]:
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
    if not strict and path.startswith((MEDIA_DIR, PRESENTATION_DIR)):
        return None  # the classifier decides: an unread type is ignored
    if path.lower().endswith(ARCHIVE_EXTENSIONS):
        return Problem("path-nested-archive", f"{shown} is an archive", file=where)
    ext = posixpath.splitext(path.lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        return Problem("path-extension", f"{shown} has extension {ext or '(none)'}", file=where)
    return None


def write_path_error(path: object) -> Optional[Problem]:
    """Why the CLI may not WRITE (or delete) ``path`` in the folder, or
    ``None`` (the server's ``tree_limits.write_path_error``): everything
    :func:`path_error` refuses, plus any dot-leading segment (``.git``,
    ``.github``, ...), even for a manifest-listed rules file."""
    err = path_error(path)
    if err is not None:
        return err
    for part in str(path).split("/"):
        if part.startswith("."):
            return Problem("path-unsafe", f"{path!r} is under a protected folder ({part!r}): "
                                          "Exelichai never writes there", file=str(path))
    return None


#: Names Windows cannot create, whatever the extension (``con.png``).
WINDOWS_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)] + [f"lpt{i}" for i in range(1, 10)])


def windows_reserved_error(path: str) -> Optional[Problem]:
    """``path-unsafe`` when a component of ``path`` is a name Windows
    reserves (a download must land the same on every creator's machine)."""
    for part in path.split("/"):
        stem = part.split(".", 1)[0].rstrip(" ").lower()
        if stem in WINDOWS_RESERVED or part.endswith((" ", ".")):
            return Problem("path-unsafe", f"{path!r} uses a name Windows reserves ({part!r})",
                           file=path)
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


def check_sizes(sizes: Mapping[str, int], *, symlinks: Iterable[str] = ()) -> List[Problem]:
    """Every vendored-limit violation of a tree given as ``{path: size}``:
    symlinks, path safety (:func:`read_path_error`), the per-file caps, the
    totals over what a sync carries, and case collisions. An ignored
    ``media/`` file is never judged or counted; a candidate's cap waits for
    the server, which judges it only when the rules reference it."""
    problems: List[Problem] = [Problem("path-symlink", f"{link!r} is a symbolic link", file=link)
                               for link in sorted(symlinks)]
    carried: Dict[str, int] = {}
    for path in sorted(sizes):
        err = read_path_error(path)
        if err:
            problems.append(err)
            continue
        if upload_ignored(path):
            continue
        carried[path] = int(sizes[path])
        size = carried[path]
        cls = structural_class(path)
        if path.startswith(MEDIA_DIR):
            if cls != CANDIDATE and size > media_max_bytes(path):
                problems.append(Problem("media-too-large", f"{size} bytes is over the "
                                        f"{media_max_bytes(path)} byte limit", file=path))
        elif path.lower().endswith(YAML_EXTENSIONS) and size > MAX_YAML_BYTES:
            problems.append(Problem("yaml-file-too-large",
                                    f"{size} bytes is over the {MAX_YAML_BYTES} byte limit",
                                    file=path))
    if len(carried) > GAME_MAX_FILES:
        problems.append(Problem("tree-too-many-files",
                                f"{len(carried)} files is over the {GAME_MAX_FILES} file limit"))
    total = sum(carried.values())
    if total > GAME_MAX_BYTES:
        problems.append(Problem("tree-too-large",
                                f"{total} bytes is over the {GAME_MAX_BYTES} byte limit"))
    rules = {p: n for p, n in carried.items() if structural_class(p) == RULES}
    if len(rules) > RULES_MAX_FILES:
        problems.append(Problem("tree-too-many-files",
                                f"{len(rules)} rules and presentation files is over the "
                                f"{RULES_MAX_FILES} file limit"))
    if sum(rules.values()) > RULES_MAX_BYTES:
        problems.append(Problem("tree-too-large",
                                f"{sum(rules.values())} bytes of rules and presentation is over "
                                f"the {RULES_MAX_BYTES} byte limit"))
    problems.extend(case_collisions(carried))
    return problems


def check_tree(files: Mapping[str, bytes], *, symlinks: Iterable[str] = ()) -> List[Problem]:
    """:func:`check_sizes` for a tree held in memory (``path -> bytes``)."""
    return check_sizes({p: len(b) for p, b in files.items()}, symlinks=symlinks)


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
    (README, LICENSE, workflows) is the creator's and never touched. It is
    also the write allowlist (the server's ``format.write_allowed``);
    :func:`write_path_error` still wins over it."""
    return (path == MANIFEST or path in set(rules)
            or path.startswith((PRESENTATION_DIR, MEDIA_DIR)))


def folder_empty(root: str) -> bool:
    """``root`` is absent or holds no game file (hidden entries -- ``.git``,
    ``.gitkeep`` -- do not count, as every read skips them, and neither does a
    top-level README / LICENSE, as a new GitHub repository starts with): the
    state of a game folder before ``exelichai init`` fills it. A folder holding
    any other file but no manifest is not empty (that is ``manifest-missing``).
    The server's rule is the same (``app_tree.FolderEmpty``)."""
    if not os.path.isdir(root):
        return True
    files, links = walk_paths(root, lambda _path: True)
    return not links and all("/" not in p and p.upper().startswith(("README", "LICENSE"))
                             for p in files)


def walk_paths(root: str, include: Callable[[str], bool]
               ) -> Tuple[Dict[str, str], List[str]]:
    """``({tree path: absolute path}, symlinks)`` under ``root`` that
    ``include`` accepts -- no file is opened. Never follows a link; hidden
    entries (``.git``, ``.github``) are skipped. A directory is offered to
    ``include`` as ``"<dir>/"``."""
    files: Dict[str, str] = {}
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
            files[rel] = full
    return files, links


def _include_for(rules: Sequence[str]) -> Callable[[str], bool]:
    rule_set = set(rules)

    def include(path: str) -> bool:
        if path.endswith("/"):
            return (path.startswith((PRESENTATION_DIR, MEDIA_DIR))
                    or any(r.startswith(path) for r in rules))
        return path == MANIFEST or path in rule_set or path.startswith((PRESENTATION_DIR, MEDIA_DIR))

    return include


#: Bytes read per chunk while hashing: a file's bytes are never held whole.
HASH_CHUNK = 1024 * 1024


def hash_file(full: str) -> Tuple[str, int]:
    """``(sha256, size)`` of the file at ``full``, read in
    :data:`HASH_CHUNK` pieces, never through a link."""
    digest = hashlib.sha256()
    size = 0
    fd = os.open(full, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as fh:
        while True:
            chunk = fh.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass
class ScanEntry:
    """One file the sync carries: its tree path, size, sha256, structural
    class and where it is on disk."""

    path: str
    size: int
    sha256: str
    cls: str
    full: str


@dataclass
class Scan:
    """A scanned tree (:func:`scan`): the carried files, the ignored ones
    (paths only -- never read), and any symlinks found."""

    root: str
    entries: Dict[str, ScanEntry] = field(default_factory=dict)
    ignored: List[str] = field(default_factory=list)
    symlinks: List[str] = field(default_factory=list)

    def manifest(self) -> List[Dict[str, object]]:
        """``[{path, sha256, size}]`` sorted by path -- the upload manifest.
        Ignored files are not in it."""
        return [{"path": e.path, "sha256": e.sha256, "size": e.size}
                for e in (self.entries[p] for p in sorted(self.entries))]

    def read(self, path: str) -> bytes:
        """One carried file's bytes, read now (a rules file; media is
        streamed by the upload, never read whole)."""
        return _read_nofollow(self.entries[path].full)

    def by_sha(self) -> Dict[str, ScanEntry]:
        """The first entry carrying each sha256: where an upload reads it."""
        out: Dict[str, ScanEntry] = {}
        for path in sorted(self.entries):
            out.setdefault(self.entries[path].sha256, self.entries[path])
        return out


def scan(root: str, *, strict: bool = True) -> Scan:
    """Scan the tree in ``root`` (the server's ``format.read_repo_folder``
    area): every carried file hashed in chunks, ignored ``media/`` types
    listed and never opened. ``strict`` raises :class:`CliError` listing every
    vendored-limit problem (:func:`check_sizes`); otherwise the scan is
    returned as found (``existing_tree``)."""
    rules, _ = manifest_rules(root)
    paths, links = walk_paths(root, _include_for(rules))
    out = Scan(root=root, symlinks=links)
    sizes: Dict[str, int] = {}
    for rel in sorted(paths):
        if upload_ignored(rel) and read_path_error(rel) is None:
            out.ignored.append(rel)
            sizes[rel] = 0
            continue
        sha, size = hash_file(paths[rel])
        out.entries[rel] = ScanEntry(rel, size, sha, structural_class(rel), paths[rel])
        sizes[rel] = size
    if strict:
        problems = check_sizes(sizes, symlinks=links)
        if problems:
            raise CliError(problems, EXIT_FAILED)
    return out


def tree_signature(root: str) -> Tuple[Tuple[str, int, int], ...]:
    """A cheap fingerprint of the tree area -- ``(path, size, mtime_ns)`` per
    file, no bytes read -- for ``exelichai dev``'s watcher: it reads the tree
    only when this changes. The same include rules as :func:`scan`; an
    unreadable manifest fingerprints the manifest alone (the next read
    reports it)."""
    try:
        rules, _ = manifest_rules(root)
    except CliError:
        rules = []
    include = _include_for(rules)
    out: List[Tuple[str, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                             and include(rel_dir + d + "/"))
        for name in sorted(filenames):
            rel = rel_dir + name
            if name.startswith(".") or not include(rel):
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            out.append((rel, int(st.st_size), int(st.st_mtime_ns)))
    return tuple(out)


def existing_tree(root: str) -> Dict[str, str]:
    """``{tree path: sha256}`` of every tree-area file already in ``root``,
    ignored ones included, for diff summaries -- hashed in chunks, never held.
    Lenient: links are left out (a write over one is refused later)."""
    rules, _ = manifest_rules(root)
    paths, _links = walk_paths(root, _include_for(rules))
    return {rel: hash_file(paths[rel])[0] for rel in sorted(paths)}


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


def _identity(value: Union[bytes, str]) -> str:
    """A file's identity for :func:`diff`: a sha256 as given, bytes hashed."""
    return value if isinstance(value, str) else sha256(value)


def diff(before: Mapping[str, Union[bytes, str]], after: Mapping[str, Union[bytes, str]], *,
         scope: Optional[Callable[[str], bool]] = None) -> Dict[str, List[str]]:
    """``{added, changed, removed, unchanged}`` path lists (``removed`` only
    for paths ``scope`` accepts). Each side maps a path to its bytes or its
    sha256 (:func:`existing_tree`)."""
    in_scope = scope or (lambda _p: True)
    same = {p: _identity(before[p]) == _identity(after[p]) for p in after if p in before}
    added = sorted(p for p in after if p not in before)
    changed = sorted(p for p, eq in same.items() if not eq)
    unchanged = sorted(p for p, eq in same.items() if eq)
    removed = sorted(p for p in before if p not in after and in_scope(p))
    return {"added": added, "changed": changed, "removed": removed, "unchanged": unchanged}
