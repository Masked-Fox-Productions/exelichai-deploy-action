"""``init`` (the initial full export) and ``pull`` (the presentation).

Both download the server's export archive and verify it against the manifest
the server signed off (archive sha256 and size, the exact member list, each
member's sha256 and size), then run the vendored tree limits and confine every
path BEFORE the first write. Nothing is written unless all of that holds.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any, Dict, List, Mapping

from exelichai_cli import tree
from exelichai_cli.commands import Context, git, list_paths, plural
from exelichai_cli.output import CliError, EXIT_FAILED, EXIT_OK, Problem

DEFAULT_DRAFT_BRANCH = "develop"


def _bad(detail: str) -> CliError:
    return CliError([Problem("archive-invalid", detail)], EXIT_FAILED)


def fetch_export(ctx: Context, scope: str) -> Dict[str, bytes]:
    """The verified ``{path: bytes}`` of a fresh export of ``scope``."""
    client = ctx.client()
    body = client.export(scope)
    if body.get("scope") != scope:
        raise _bad(f"asked for a {scope} export, got {body.get('scope')!r}")
    archive = body.get("archive") or {}
    listed = body.get("files")
    if not isinstance(listed, list) or not isinstance(archive, Mapping):
        raise _bad("the export answer has no file list")
    expected: Dict[str, Mapping[str, Any]] = {}
    for entry in listed:
        path = entry.get("path") if isinstance(entry, Mapping) else None
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
    problems = tree.check_tree(files)
    if problems:
        raise CliError(problems, EXIT_FAILED)
    return files


def _summary(ctx: Context, changes: Mapping[str, List[str]]) -> None:
    rep = ctx.reporter
    list_paths(rep, "add", changes["added"])
    list_paths(rep, "change", changes["changed"])
    list_paths(rep, "delete", changes["removed"])
    if changes.get("unchanged"):
        rep.info(f"  unchanged: {len(changes['unchanged'])}")


def _apply(ctx: Context, writes: Mapping[str, bytes], deletes: List[str]) -> None:
    tree.preflight_writes(ctx.folder, [*writes, *deletes])
    for path in sorted(writes):
        tree.write_file(ctx.folder, path, writes[path])
    for path in deletes:
        tree.delete_file(ctx.folder, path)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def init(ctx: Context, *, force: bool = False) -> int:
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
    try:
        files = fetch_export(ctx, "full")
    except CliError as exc:
        if any(p.code == "archive-invalid" for p in exc.problems):
            exc.notes.append("The server may already have recorded this game's initial export; "
                             "if `init` now answers already-initialised, the owner can download "
                             "it again with Re-initialise in the GitHub panel.")
        raise
    changes = tree.diff(before, files, scope=lambda p: True)
    if before:
        rep.info("init --force will change the folder:")
        _summary(ctx, changes)
    _apply(ctx, files, changes["removed"])
    rep.info(f"initialised {ctx.game_id}: wrote {plural(len(files), 'file')}"
             + (f", deleted {len(changes['removed'])}" if changes["removed"] else ""))
    rep.info("next: commit this folder and push it to the draft branch")
    rep.summary(f"### exelichai: initialised `{ctx.game_id}` ({plural(len(files), 'file')})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _dirty_presentation(ctx: Context) -> List[str]:
    """Uncommitted paths under ``presentation/``; raises when git cannot say."""
    top = git(ctx.folder, "rev-parse", "--show-toplevel")
    if top is None:
        raise CliError([Problem("presentation-dirty",
                                "the folder is not a git checkout, so uncommitted edits cannot be "
                                "ruled out; pass --force to overwrite presentation/ anyway")])
    status = git(ctx.folder, "status", "--porcelain", "--untracked-files=all", "--",
                 tree.PRESENTATION_DIR)
    return [line[3:] for line in (status or "").splitlines() if line.strip()]


def pull(ctx: Context, *, force: bool = False, draft_branch: str = DEFAULT_DRAFT_BRANCH) -> int:
    rep = ctx.reporter
    ctx.require_game_id()
    if not force:
        dirty = _dirty_presentation(ctx)
        if dirty:
            list_paths(rep, "uncommitted", dirty, limit=20)
            raise CliError([Problem("presentation-dirty", f"{plural(len(dirty), 'path')} "
                                    "uncommitted under presentation/")])
    branch = git(ctx.folder, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != draft_branch:
        rep.warn(f"you are on {branch!r}, not the draft branch {draft_branch!r}: pulled edits "
                 f"belong on {draft_branch!r}")
    ctx.authenticate()
    files = fetch_export(ctx, "presentation")
    stray = sorted(p for p in files
                   if not p.startswith((tree.PRESENTATION_DIR, tree.MEDIA_DIR)))
    if stray:
        raise _bad(f"a presentation export carried {stray[:5]}")
    before = tree.existing_tree(ctx.folder)
    # presentation/ is replaced wholesale; media/ only gains or overwrites.
    changes = tree.diff({p: b for p, b in before.items()
                         if p.startswith(tree.PRESENTATION_DIR) or p in files},
                        files, scope=lambda p: p.startswith(tree.PRESENTATION_DIR))
    writes = {p: files[p] for p in [*changes["added"], *changes["changed"]]}
    _apply(ctx, writes, changes["removed"])
    touched = len(changes["added"]) + len(changes["changed"]) + len(changes["removed"])
    if not touched:
        rep.info("presentation/ is already up to date")
        return EXIT_OK
    rep.info(f"pulled the Art Studio presentation: {plural(touched, 'file')} changed")
    _summary(ctx, {**changes, "unchanged": []})
    rep.info("")
    rep.info("These Art Studio edits stay UNSENT -- and publishing stays blocked -- until a "
             "pushed tree carries them. Commit and push:")
    rep.info(f"  git add presentation media && git commit -m 'Pull Art Studio edits' "
             f"&& git push origin {draft_branch}")
    return EXIT_OK
