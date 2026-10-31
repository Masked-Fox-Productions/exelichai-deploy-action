"""``init`` (the initial full export) and ``pull`` (the Studio's unsent
edits: art, rules and packs, as a three-way against the baseline commit).

Both download the server's export archive and verify it against the manifest
the server signed off (archive sha256 and size, the exact member list, each
member's sha256 and size), then run the vendored tree limits and confine every
path BEFORE the first write. ``pull`` also holds every returned path to the
manifest-derived allowlist with the write-side guard (no ``.git``/``.github``
or other dot-leading segment). Nothing is written unless all of that holds.

**Media by manifest** (plan 2026-10-26-002 KTD-8). The archive holds the
rules side and ``media-manifest.json``: ``{path, sha256, size, url}`` per media
file. Before anything is downloaded, every entry is held to the hardening
below; then each file streams into a staging folder while it is hashed
(memory holds one chunk), and only once EVERY download matched its sha256 and
size are the files written and the media renamed into place. A failure
anywhere writes nothing.

* the URL is ``https`` on this API origin's media host EXACTLY (never a suffix
  match), and no redirect is followed;
* the entry count and declared total are within the per-game limits, and
  each size within its kind's cap;
* the path is a safe ``media/`` path (no ``..``, nothing absolute, nothing
  outside the tree area), no Windows-reserved name, no case collision with
  another path, and its destination passes through no symbolic link and stays
  inside the folder.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set, Union

from exelichai_cli import api, tree
from exelichai_cli.commands import Context, git, list_paths, plural
from exelichai_cli.output import (CliError, EXIT_AUTH, EXIT_CONFLICTS, EXIT_FAILED, EXIT_OK,
                                  Problem)

DEFAULT_DRAFT_BRANCH = "develop"
#: The archive member listing the media (the server's ``export.MEDIA_MANIFEST``).
MEDIA_MANIFEST = "media-manifest.json"
_SHA = re.compile(r"[0-9a-f]{64}")


def _bad(detail: str) -> CliError:
    return CliError([Problem("archive-invalid", detail)], EXIT_FAILED)


@dataclass(frozen=True)
class MediaEntry:
    """One media file of a manifest-mode answer, checked
    (:func:`media_entries`), not yet downloaded."""
    path: str
    sha256: str
    size: int
    url: str


#: A file an answer delivers: its bytes (from the zip), or a media entry.
Delivered = Union[bytes, MediaEntry]


def _identity(value: Delivered) -> str:
    return value.sha256 if isinstance(value, MediaEntry) else tree.sha256(value)


def verified_archive(client, body: Mapping[str, Any], scope: str) -> Dict[str, Delivered]:
    """The ``{path: bytes or MediaEntry}`` an export answer delivers, verified
    against the manifest the server signed off before anything is returned:
    the zip's members by bytes; a manifest-mode answer's media as checked
    :class:`MediaEntry` values (downloaded by :func:`apply_writes`)."""
    if body.get("scope") != scope:
        raise _bad(f"asked for a {scope} export, got {body.get('scope')!r}")
    archive = body.get("archive") or {}
    listed = body.get("files")
    if not isinstance(listed, list) or not isinstance(archive, Mapping):
        raise _bad("the export answer has no file list")
    manifest_mode = body.get("media") == api.MEDIA_MODE
    expected: Dict[str, Mapping[str, Any]] = {}
    for entry in listed:
        path = entry.get("path") if isinstance(entry, Mapping) else None
        if not (manifest_mode and path == MEDIA_MANIFEST):
            err = tree.path_error(path)
            if err:
                raise CliError([err], EXIT_FAILED)
        if path in expected:
            raise _bad(f"{path!r} is listed twice")
        expected[path] = entry
    size = archive.get("size")
    if not isinstance(size, int) or size < 0 or size > tree.MAX_TREE_BYTES * 2:
        raise _bad(f"archive size {size!r}")
    data = client.download(archive.get("url"), expected_size=size)
    if tree.sha256(data) != archive.get("sha256"):
        raise _bad("the archive's sha256 does not match")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise _bad(f"not a zip archive ({exc})")
    files: Dict[str, bytes] = {}
    with zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        if sorted(names) != sorted(expected) or len(set(names)) != len(names):
            extra = sorted(set(names) - set(expected))
            missing = sorted(set(expected) - set(names))
            raise _bad(f"members differ from the manifest (extra {extra[:5]}, missing {missing[:5]})")
        for info in infos:
            want = expected[info.filename]
            if info.is_dir() or info.file_size != want.get("size"):
                raise _bad(f"{info.filename}: size {info.file_size}, manifest {want.get('size')}")
            with zf.open(info) as fh:
                blob = fh.read(info.file_size + 1)
            if len(blob) != want.get("size") or tree.sha256(blob) != want.get("sha256"):
                raise _bad(f"{info.filename}: bytes do not match the manifest")
            files[info.filename] = blob
    raw_media = files.pop(MEDIA_MANIFEST, None) if manifest_mode else None
    if manifest_mode and raw_media is None:
        raise _bad(f"the answer says media by manifest but carries no {MEDIA_MANIFEST}")
    problems = tree.check_tree(files)
    if problems:
        raise CliError(problems, EXIT_FAILED)
    out: Dict[str, Delivered] = dict(files)
    if raw_media is not None:
        out.update({e.path: e for e in media_entries(client, raw_media, files)})
    return out


def media_entries(client, raw: bytes, files: Mapping[str, bytes]) -> List[MediaEntry]:
    """The media manifest's entries, every one checked (module docstring)
    before any download; one bad entry refuses them all."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _bad(f"{MEDIA_MANIFEST} is not JSON")
    items = doc.get("media") if isinstance(doc, Mapping) else None
    if not isinstance(items, list):
        raise _bad(f"{MEDIA_MANIFEST} has no media list")
    if len(items) > tree.GAME_MAX_FILES:
        raise _bad(f"{MEDIA_MANIFEST} lists {len(items)} files, over the "
                   f"{tree.GAME_MAX_FILES}-file limit")
    problems: List[Problem] = []
    out: List[MediaEntry] = []
    total = 0
    seen: Set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise _bad(f"{MEDIA_MANIFEST} entry {i} is not an object")
        path, sha, size, url = (item.get("path"), item.get("sha256"), item.get("size"),
                                item.get("url"))
        err = tree.write_path_error(path) or (None if str(path).startswith(tree.MEDIA_DIR) else
                                              Problem("path-unsafe", f"{path!r} is not under "
                                                      f"{tree.MEDIA_DIR}", file=str(path)))
        err = err or tree.windows_reserved_error(path)
        if err is None and (path in seen or path in files):
            err = Problem("archive-invalid", f"{path!r} is delivered twice", file=path)
        if err is None and (not isinstance(sha, str) or not _SHA.fullmatch(sha)):
            err = Problem("archive-invalid", f"{path}: sha256 {str(sha)[:80]!r}", file=path)
        if err is None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            err = Problem("archive-invalid", f"{path}: size {size!r}", file=path)
        if err is None and size > tree.media_max_bytes(path):
            err = Problem("media-too-large", f"{size} bytes is over the "
                                             f"{tree.media_max_bytes(path)} byte limit", file=path)
        if err is None:
            try:
                api.check_media_url(url, api_origin=client.origin)
            except CliError as exc:
                err = Problem("archive-invalid", f"{path}: {exc.problems[0].detail}", file=path)
        if err is not None:
            problems.append(err)
            continue
        seen.add(path)
        total += size
        out.append(MediaEntry(path, sha, size, url))
    if total > tree.GAME_MAX_BYTES:
        problems.append(Problem("archive-invalid", f"{MEDIA_MANIFEST} declares {total} bytes, "
                                                   f"over the {tree.GAME_MAX_BYTES}-byte limit"))
    problems += [Problem("path-case-collision", p.detail, file=p.file)
                 for p in tree.case_collisions([*files, *seen])]
    if problems:
        raise CliError(problems, EXIT_FAILED)
    return out


def _confined_media(root: str, rel: str) -> str:
    """:func:`tree.confined_target`, and the destination's real path must stay
    inside the folder's (no parent resolves elsewhere)."""
    target = tree.confined_target(root, rel)
    real_root = os.path.realpath(root)
    parent = os.path.dirname(target)
    probe = parent
    while not os.path.exists(probe) and probe != os.path.dirname(probe):
        probe = os.path.dirname(probe)
    real = os.path.realpath(probe)
    if os.path.commonpath([real_root, real]) != real_root:
        raise CliError([Problem("path-symlink", f"{rel!r} resolves outside the folder",
                                file=rel)], EXIT_FAILED)
    return target


def _summary(ctx: Context, changes: Mapping[str, List[str]]) -> None:
    rep = ctx.reporter
    list_paths(rep, "add", changes["added"])
    list_paths(rep, "change", changes["changed"])
    list_paths(rep, "delete", changes["removed"])
    if changes.get("unchanged"):
        rep.info(f"  unchanged: {len(changes['unchanged'])}")


def _apply(ctx: Context, writes: Mapping[str, Delivered], deletes: List[str]) -> None:
    """Write ``writes`` and delete ``deletes``. Every path is confined first;
    every media entry is downloaded (``api.TRANSFER_WORKERS`` at once) into a
    staging folder inside ``ctx.folder`` and verified BEFORE the first write,
    then renamed into place -- so a failed download writes nothing, and an
    interrupted one leaves no partial file at a destination."""
    tree.preflight_writes(ctx.folder, [*writes, *deletes])
    media = {p: v for p, v in writes.items() if isinstance(v, MediaEntry)}
    for path in media:
        _confined_media(ctx.folder, path)
    stage = tempfile.mkdtemp(prefix=".exelichai-download-", dir=ctx.folder) if media else None
    try:
        staged: Dict[str, str] = ({path: os.path.join(stage, str(n))
                                   for n, path in enumerate(sorted(media))} if stage else {})
        if media:
            client = ctx.client()

            def fetch(path: str) -> None:
                entry = media[path]
                client.download_to(entry.url, staged[path], expected_size=entry.size,
                                   expected_sha256=entry.sha256, path=path)

            api.concurrently(sorted(media), fetch)
        for path in sorted(writes):
            if path in staged:
                target = _confined_media(ctx.folder, path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                _confined_media(ctx.folder, path)   # nothing became a link meanwhile
                if os.path.islink(target):
                    raise CliError([Problem("path-symlink", f"{path!r} is a symbolic link",
                                            file=path)], EXIT_FAILED)
                os.replace(staged[path], target)
            else:
                tree.write_file(ctx.folder, path, writes[path])
        for path in deletes:
            tree.delete_file(ctx.folder, path)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def _deletable(path: str) -> bool:
    """``init --force`` deletes only what the sync carries itself -- the
    rules area and ingested media types (plan 2026-10-26-002 R0j.8). An
    ignored file (a ``.psd``, the presentation README's neighbours) and a
    text or SVG candidate the server may simply not reference are the
    creator's, and never touched."""
    return tree.structural_class(path) in (tree.RULES, tree.BINARY)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

#: Why ``init`` refuses a non-empty folder on an initialised game, whatever
#: ``--force`` says (plan 2026-10-26-001 C6).
PULL_INSTEAD_NOTE = ("This game is already initialised, so `init` only downloads its files, "
                     "and only into an empty folder -- even with --force. To bring the "
                     "Studio's changes into this folder, use `exelichai pull`.")


def init(ctx: Context, *, force: bool = False) -> int:
    """Write the game's full tree into the folder. On a game not yet
    initialised this is the initial export (the server writes the sync
    baselines; the platform normally does that itself on connect). On an
    initialised game it is a download (``download: true``; the server writes
    nothing) -- into an EMPTY folder only, ``--force`` or not."""
    rep = ctx.reporter
    ctx.require_game_id()
    before = tree.existing_tree(ctx.folder)
    if before and not force:
        # Refuse BEFORE calling the server: an author token's first full
        # export initialises the game, so nothing may be asked for here.
        rep.info(f"{ctx.folder} already holds {plural(len(before), 'repo-tree file')}:")
        list_paths(rep, "present", sorted(before), limit=20)
        raise CliError([Problem("folder-not-empty", f"{plural(len(before), 'file')} present")],
                       EXIT_FAILED)
    ctx.authenticate()
    client = ctx.client()
    body = client.export("full", download=True)
    downloaded = body.get("already_initialised") is True
    if downloaded and before:
        rep.info(f"{ctx.folder} already holds {plural(len(before), 'repo-tree file')}:")
        list_paths(rep, "present", sorted(before), limit=20)
        raise CliError([Problem("folder-not-empty", f"{plural(len(before), 'file')} present; "
                                                    f"{ctx.game_id} is already initialised")],
                       EXIT_FAILED, notes=[PULL_INSTEAD_NOTE])
    try:
        files = verified_archive(client, body, "full")
    except CliError as exc:
        if any(p.code == "archive-invalid" for p in exc.problems) and not downloaded:
            exc.notes.append("The server may already have recorded this game's initial export; "
                             "run `exelichai init` again into an empty folder to download it.")
        raise
    changes = tree.diff(before, {p: _identity(v) for p, v in files.items()},
                        scope=_deletable)
    if before:
        rep.info("init --force will change the folder:")
        _summary(ctx, changes)
    _apply(ctx, {p: v for p, v in files.items()
                 if p not in before or before[p] != _identity(v)}, changes["removed"])
    if downloaded:
        rep.info(f"downloaded {ctx.game_id} (already initialised): wrote "
                 f"{plural(len(files), 'file')}")
        rep.info("next: commit this folder; push it to the draft branch only if it should "
                 "replace what the branch holds")
        rep.summary(f"### exelichai: downloaded `{ctx.game_id}` "
                    f"({plural(len(files), 'file')})")
        return EXIT_OK
    rep.info(f"initialised {ctx.game_id}: wrote {plural(len(files), 'file')}"
             + (f", deleted {len(changes['removed'])}" if changes["removed"] else ""))
    rep.info("next: commit this folder and push it to the draft branch")
    rep.summary(f"### exelichai: initialised `{ctx.game_id}` ({plural(len(files), 'file')})")
    return EXIT_OK




# ---------------------------------------------------------------------------
# pull (plan 2026-10-19-001 Unit 10)
# ---------------------------------------------------------------------------
#
# A three-way against the baseline commit (KTD "``pull`` is a three-way
# against the baseline commit"):
#
# 1. the CLI places its checkout against the commit the server last synced:
#    HEAD equal to it, or strictly ahead of it, proceeds; behind, diverged or
#    missing refuses ``pull-behind`` before anything is sent;
# 2. it sends the working copy of the manifest and every rules file the
#    manifest lists; the server decides each rules and pack key against the
#    baseline (splice live / keep local / ``pull-conflict``) and builds the
#    presentation on the retained tree at the baseline commit;
# 3. the CLI writes a returned presentation or media file (or deletes one the
#    server lists) only when its checkout did not change that path since the
#    baseline. A changed path carrying an unsent address with other bytes, or
#    a deletion of a changed path, is a ``pull-conflict``; a changed path
#    carrying none is kept.

#: A git object name: SHA-1 or SHA-256 hex.
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

PULL_CONFLICT_NOTE = ("Each pull-conflict was left exactly as your checkout has it. Push "
                      "your change to the draft branch (commit it first if it is not "
                      "committed): the sync then holds a real conflict, which you resolve in "
                      "the Studio's GitHub panel or with `exelichai conflicts`.")


def _behind(detail: str) -> CliError:
    return CliError([Problem("pull-behind", detail)], EXIT_FAILED)


def _require_checkout(ctx: Context) -> None:
    if git(ctx.folder, "rev-parse", "--show-toplevel") is None:
        raise _behind("the folder is not a git checkout, so it cannot be placed against the "
                      "commit Exelichai last synced")


def _uncommitted(ctx: Context, pathspecs: List[str]) -> List[str]:
    status = git(ctx.folder, "status", "--porcelain", "--untracked-files=all", "--", *pathspecs,
                 strip=False)
    if status is None:
        raise _behind("git could not report the checkout's status")
    return [line[3:] for line in status.splitlines() if line.strip()]


def _sent_files(ctx: Context, rules: List[str]) -> Dict[str, str]:
    """The working copy of the manifest and every rules file it lists, as
    text. Refuses (nothing sent) a missing, linked, hidden or non-UTF-8 one."""
    problems: List[Problem] = []
    out: Dict[str, str] = {}
    if not os.path.isfile(os.path.join(ctx.folder, tree.MANIFEST)):
        raise CliError([Problem("manifest-missing", f"no {tree.MANIFEST} in {ctx.folder}")])
    if not rules:
        raise CliError([Problem("manifest-invalid", "`rules:` lists no rules files",
                                file=tree.MANIFEST)])
    for path in [tree.MANIFEST, *dict.fromkeys(rules)]:
        missing = "manifest-missing" if path == tree.MANIFEST else "rules-file-missing"
        err = tree.path_error(path)
        if err is not None:
            problems.append(err)
            continue
        if tree.write_path_error(path) is not None:
            # The CLI's tree walk skips hidden folders, so push never reads
            # such a file either (rules-file-missing on this lane).
            problems.append(Problem(missing, f"{path} is under a hidden folder, which the "
                                             "exelichai command never reads or writes",
                                    file=path))
            continue
        try:
            full = tree.confined_target(ctx.folder, path)
        except CliError as exc:
            problems.extend(exc.problems)
            continue
        if not os.path.isfile(full):
            problems.append(Problem(missing, f"{path} is not in the folder", file=path))
            continue
        data = tree._read_nofollow(full)
        if len(data) > tree.MAX_YAML_BYTES:
            problems.append(Problem("yaml-file-too-large", f"{len(data)} bytes", file=path))
            continue
        try:
            out[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(Problem("manifest-invalid" if path == tree.MANIFEST
                                    else "rules-invalid", "not UTF-8 text", file=path))
    if problems:
        raise CliError(problems, EXIT_FAILED)
    return out


def _place_head(ctx: Context, commit: str) -> str:
    """``"equal"`` or ``"ahead"``: where HEAD stands against ``commit``;
    raises ``pull-behind`` for behind, diverged, or a commit not in the
    local history."""
    folder = ctx.folder
    head = git(folder, "rev-parse", "--verify", "--quiet", "HEAD")
    short = commit[:12]
    if not head:
        raise _behind("the checkout has no commit yet")
    if git(folder, "cat-file", "-e", f"{commit}^{{commit}}") is None:
        raise _behind(f"the commit Exelichai last synced ({short}) is not in this checkout: "
                      "fetch, then update your branch from the draft branch")
    if head == commit:
        return "equal"
    if git(folder, "merge-base", "--is-ancestor", commit, "HEAD") is not None:
        return "ahead"
    if git(folder, "merge-base", "--is-ancestor", "HEAD", commit) is not None:
        raise _behind(f"HEAD {head[:12]} is behind the last synced commit {short}")
    raise _behind(f"HEAD {head[:12]} has diverged from the last synced commit {short}")


def _changed_since(ctx: Context, commit: str) -> Set[str]:
    """Folder-relative paths the checkout changed since ``commit``: committed
    since (``commit..HEAD``), uncommitted, or untracked (ignored included)."""
    diff = git(ctx.folder, "diff", "--name-only", "--no-renames", "--no-ext-diff", "-z",
               "--relative", commit, "--", strip=False)
    others = git(ctx.folder, "ls-files", "-z", "--others", "--", ".", strip=False)
    if diff is None or others is None:
        raise _behind(f"git could not list what the checkout changed since {commit[:12]}")
    return {p for p in (diff + "\0" + others).split("\0") if p}


def _local_bytes(ctx: Context, path: str) -> Optional[bytes]:
    full = tree.confined_target(ctx.folder, path)
    try:
        mode = os.lstat(full).st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise CliError([Problem("path-unsafe", f"{path!r} is not a regular file", file=path)])
    return tree._read_nofollow(full)


def _local_sha(ctx: Context, path: str) -> Optional[str]:
    """The local file's sha256, hashed in chunks (a media file is never held
    whole), or None when there is none."""
    full = tree.confined_target(ctx.folder, path)
    try:
        mode = os.lstat(full).st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise CliError([Problem("path-unsafe", f"{path!r} is not a regular file", file=path)])
    return tree.hash_file(full)[0]


def _string_list(body: Mapping[str, Any], key: str) -> List[str]:
    value = body.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _bad(f"`{key}` is not a list of paths")
    return value


def _check_returned(paths: List[str], rules: List[str]) -> None:
    """Every returned or deleted path passes the write-side guard and lies in
    the manifest-derived allowlist, or nothing is written."""
    problems: List[Problem] = []
    for path in sorted(set(paths)):
        err = tree.write_path_error(path)
        if err is None and not tree.in_tree_area(path, rules):
            err = Problem("path-unsafe", f"{path!r} is outside the manifest, its rules files, "
                                         "presentation/ and media/", file=path)
        if err is not None:
            problems.append(err)
    if problems:
        raise CliError(problems, EXIT_FAILED)


def _pathspec_roots(writes: Mapping[str, Delivered], deletes: List[str]) -> List[str]:
    """What to ``git add``: ``presentation`` / ``media`` as folders, any
    other written path (the manifest, a rules file) by name."""
    roots = set()
    for path in [*writes, *deletes]:
        top = path.split("/", 1)[0] + "/"
        roots.add(top.rstrip("/") if top in (tree.PRESENTATION_DIR, tree.MEDIA_DIR) else path)
    return sorted(roots)


def _pair(item: Any) -> str:
    if isinstance(item, Mapping):
        return f"{item.get('kind')} {item.get('address')}"
    return str(item)


def pull(ctx: Context, *, force: bool = False, draft_branch: str = DEFAULT_DRAFT_BRANCH) -> int:
    rep = ctx.reporter
    ctx.require_game_id()
    rules, _game = tree.manifest_rules(ctx.folder)
    sent = _sent_files(ctx, rules)
    rules = [p for p in sent if p != tree.MANIFEST]
    _require_checkout(ctx)
    if not force:
        dirty = _uncommitted(ctx, list(sent))
        if dirty:
            list_paths(rep, "uncommitted", dirty, limit=20)
            raise CliError([Problem("rules-dirty", f"{plural(len(dirty), 'path')} uncommitted "
                                    "in the manifest or its rules files")])
        dirty = _uncommitted(ctx, [tree.PRESENTATION_DIR])
        if dirty:
            list_paths(rep, "uncommitted", dirty, limit=20)
            raise CliError([Problem("presentation-dirty", f"{plural(len(dirty), 'path')} "
                                    "uncommitted under presentation/")])

    credential = ctx.authenticate()
    if credential.scope == "validate":
        raise CliError([Problem("credential-scope", "pull needs an author token (exla_), not a "
                                                    "validate token")], EXIT_AUTH)
    client = ctx.client()
    placed = client.pull_baseline()
    commit = placed.get("baseline_commit")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise _bad(f"the baseline commit {str(commit)[:80]!r} is not a commit id")
    branch_name = placed.get("draft_branch") if isinstance(placed.get("draft_branch"), str) \
        else draft_branch
    branch = git(ctx.folder, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != branch_name:
        rep.warn(f"you are on {branch!r}, not the draft branch {branch_name!r}: pulled edits "
                 f"belong on {branch_name!r}")
    where = _place_head(ctx, commit)
    changed_local = _changed_since(ctx, commit)

    body = client.pull_rules(sent, commit)
    if body.get("baseline_commit") != commit:
        raise CliError([Problem("pull-baseline-moved",
                                f"placed against {commit[:12]}, the server answered for "
                                f"{str(body.get('baseline_commit'))[:12]}")])
    files = verified_archive(client, body, "rules")
    deleted = _string_list(body, "deleted")
    unsent_paths = set(_string_list(body, "unsent_paths"))
    _check_returned([*files, *deleted], rules)
    if set(deleted) & set(sent):
        raise _bad(f"the answer deletes {sorted(set(deleted) & set(sent))}, which the manifest "
                   "still lists")

    # Sort every returned and deleted path (KTD: the per-file three-way).
    writes: Dict[str, Delivered] = {}
    deletes: List[str] = []
    kept: List[str] = []
    conflicts: List[Problem] = []
    for path in sorted(files):
        data = files[path]
        if path in sent:
            # The server spliced the text this run sent, key by key against
            # the baseline: a key the checkout changed is already kept.
            if isinstance(data, MediaEntry) or data != sent[path].encode("utf-8"):
                writes[path] = data
            continue
        if _local_sha(ctx, path) == _identity(data):
            continue
        if path not in changed_local:
            writes[path] = data
        elif path in unsent_paths:
            conflicts.append(Problem("pull-conflict", f"{path}: your checkout changed this file "
                                     "since the last sync, and the Studio changed it too",
                                     file=path))
        else:
            kept.append(path)
    for path in sorted(set(deleted)):
        if _local_bytes(ctx, path) is None:
            continue
        if path not in changed_local:
            deletes.append(path)
        else:
            conflicts.append(Problem("pull-conflict", f"{path}: the Studio removed this, and "
                                     "your checkout changed it since the last sync", file=path))
    server_conflicts = [Problem.from_dict(r) for r in body.get("pull_conflicts") or []
                        if isinstance(r, Mapping)]
    refused = [Problem.from_dict(r) for r in body.get("refused") or [] if isinstance(r, Mapping)]

    # Nothing is written unless every path is confined, and the rules files
    # still hold the text this run sent.
    for path in writes:
        if path in sent and _local_bytes(ctx, path) != sent[path].encode("utf-8"):
            raise CliError([Problem("rules-dirty", f"{path} changed while pull ran; nothing "
                                                   "was written", file=path)])
    _apply(ctx, writes, deletes)

    rep.info(f"pulled from the Studio against {commit[:12]} "
             f"({'your checkout is ahead' if where == 'ahead' else 'up to date'})")
    list_paths(rep, "write", sorted(writes))
    list_paths(rep, "delete", deletes)
    list_paths(rep, "kept, your checkout changed it", kept)
    list_paths(rep, "kept, your checkout changed it", [_pair(a) for a in body.get("kept_local")
                                                        or []])
    waiting = [_pair(a) for a in body.get("waiting") or []]
    if waiting:
        list_paths(rep, "waiting on a held conflict (run `exelichai conflicts`)", waiting)
    verification = body.get("verification") or {}
    if isinstance(verification, Mapping) and verification.get("ok") is False:
        rep.warn("the server could not prove this pull faithful (it was still answered): "
                 + "; ".join(str(e.get("detail") or e.get("code")) for e in
                             verification.get("errors") or [] if isinstance(e, Mapping))[:500])
    all_conflicts = [*server_conflicts, *conflicts]
    rep.problems([*all_conflicts, *refused])
    if all_conflicts:
        rep.info(PULL_CONFLICT_NOTE)
    touched = len(writes) + len(deletes)
    if touched:
        rep.info("")
        rep.info("These Studio edits stay UNSENT -- and publishing stays blocked -- until a "
                 "pushed tree carries them. Commit and push:")
        rep.info(f"  git add -A {' '.join(_pathspec_roots(writes, deletes))} && git commit -m "
                 f"'Pull Studio edits' && git push origin {branch_name}")
    elif not all_conflicts and not refused:
        rep.info("the folder is already up to date with the Studio")
    rep.summary(f"### exelichai pull: {plural(touched, 'file')} changed"
                + (f", {plural(len(all_conflicts), 'pull-conflict')}" if all_conflicts else ""))
    rep.summary_problems([*all_conflicts, *refused])
    if all_conflicts:
        return EXIT_CONFLICTS
    if refused:
        return EXIT_FAILED
    return EXIT_OK
