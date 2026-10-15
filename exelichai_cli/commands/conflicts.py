"""``conflicts`` (list the held conflicts) and ``conflicts resolve``.

Author (or publish) scope, through the author-readable conflict-list route.
Values are text truncated at 2KB by the server (flagged); media values are a
logical path plus sha256 -- never a URL. Held conflicts exit 2.
"""

from __future__ import annotations

from typing import Any, Mapping

from exelichai_cli.commands import Context, plural
from exelichai_cli.output import CliError, EXIT_CONFLICTS, EXIT_OK, Problem

KEEPS = ("repo", "mine")


def _side(rep, label: str, side: Mapping[str, Any]) -> None:
    if side.get("error"):
        rep.info(f"    {label}: (unavailable: {side.get('error')})")
        return
    if side.get("present") is False:
        rep.info(f"    {label}: (absent)")
    text = side.get("text")
    if isinstance(text, str) and text:
        lines = text.splitlines() or [text]
        rep.info(f"    {label}: {lines[0]}")
        for line in lines[1:]:
            rep.info(f"    {' ' * len(label)}  {line}")
        if side.get("truncated"):
            rep.info(f"    {' ' * len(label)}  ... (truncated at 2KB)")
    for media in side.get("media") or []:
        rep.info(f"    {label} media: {media.get('path')} (sha256 {media.get('sha')})")


def list_conflicts(ctx: Context) -> int:
    rep = ctx.reporter
    ctx.authenticate()
    body = ctx.client().conflicts()
    entries = body.get("conflicts") or []
    if not entries:
        rep.info("no conflicts held")
        return EXIT_OK
    rep.info(f"{plural(len(entries), 'conflict')} held:")
    for c in entries:
        field = f" [{c.get('field')}]" if c.get("field") else ""
        rep.info(f"  {c.get('conflict_id')}  {c.get('display_name')}{field}  "
                 f"({c.get('kind')} {c.get('address')})")
        rep.info(f"    from {c.get('branch') or '-'} at {(c.get('commit') or '-')[:12]}, "
                 f"detected {c.get('detected_at')}")
        _side(rep, "repo", c.get("repo") or {})
        _side(rep, "studio", c.get("browser") or {})
        keeps = c.get("keeps") or []
        if keeps:
            rep.info(f"    resolve: exelichai conflicts resolve {c.get('conflict_id')} "
                     f"--keep {'|'.join(keeps)}")
    rep.summary(f"### exelichai: {plural(len(entries), 'conflict')} held")
    return EXIT_CONFLICTS


def resolve(ctx: Context, conflict_id: str, keep: str) -> int:
    if keep not in KEEPS:
        raise CliError([Problem("conflict-resolve-invalid", f"--keep {keep!r}")])
    ctx.authenticate()
    out = ctx.client().resolve(conflict_id, keep).get("resolved") or {}
    ctx.reporter.info(f"resolved {conflict_id}: kept {keep}"
                      + (f" ({out.get('display_name')})" if out.get("display_name") else ""))
    return EXIT_OK
