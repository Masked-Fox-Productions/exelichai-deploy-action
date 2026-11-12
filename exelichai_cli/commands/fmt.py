"""``fmt`` (plan 2026-11-07-004 Unit 4): bring each ``presentation/`` file to
what ``exelichai pull`` would write once a push has stored it, offline.

Content the Art Studio stores differently is changed in place; the file's key
order, layout and comments are the author's and stay, as ``pull`` keeps them.
The bytes come from the platform's own code (``splice_presentation``, the rule
the outgoing lane writes by), shipped in the ``exelichai-rules`` package (the
``local`` extra), never from a second implementation here. Only files that
already exist are changed; nothing is added, moved or deleted. ``--check``
writes nothing: it lists the files that would change and exits 1, for CI. A file a push would refuse is reported with its
reason and left alone.
"""

from __future__ import annotations

from typing import Dict, List

from exelichai_cli import tree
from exelichai_cli.commands import Context, list_paths, plural
from exelichai_cli.commands.sync import local_rules
from exelichai_cli.output import EXIT_FAILED, EXIT_OK, Problem


def _presentation_files(ctx: Context) -> Dict[str, str]:
    """``{tree path: absolute path}`` of every ``presentation/<type>/<file>.yaml``."""
    def include(path: str) -> bool:
        if path.endswith("/"):
            return path == tree.PRESENTATION_DIR or (
                path.startswith(tree.PRESENTATION_DIR) and path.count("/") == 2)
        return (path.startswith(tree.PRESENTATION_DIR) and path.count("/") == 2
                and path.endswith((".yaml", ".yml")))

    files, _links = tree.walk_paths(ctx.folder, include)
    return files


def run(ctx: Context, *, check: bool = False) -> int:
    presentation = local_rules("presentation")
    rep = ctx.reporter
    files = _presentation_files(ctx)
    changed: List[str] = []
    refused: List[Problem] = []
    for rel, full in sorted(files.items()):
        data = tree._read_nofollow(full)
        try:
            canonical = presentation.canonical_presentation_file(rel, data)
        except presentation.TreeRefused as exc:
            refused.extend(Problem.from_dict(e.to_dict()) for e in exc.errors)
            continue
        if canonical != data:
            changed.append(rel)
            if not check:
                tree.write_file(ctx.folder, rel, canonical)
    rep.report_data("fmt", {"checked": len(files), "changed": changed, "check": check})
    verb = "would change" if check else "changed"
    rep.info(f"{plural(len(files), 'presentation file')} checked; {len(changed)} {verb}")
    list_paths(rep, verb, changed)
    rep.summary(f"### exelichai fmt: {len(changed)} {verb}")
    problems = list(refused)
    if check:
        problems += [Problem("presentation-not-canonical", "run `exelichai fmt`", file=p)
                     for p in changed]
    rep.problems(problems)
    rep.summary_problems(problems)
    return EXIT_FAILED if problems else EXIT_OK
