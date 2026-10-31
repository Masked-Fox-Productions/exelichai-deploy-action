"""``conflicts`` (list the held conflicts) and ``conflicts resolve``.

Author (or publish) scope, through the author-readable conflict-list route.
Values are text truncated at 2KB by the server (flagged); media values are a
logical path plus sha256 -- never a URL. Held conflicts exit 2.

``--keep studio`` keeps the Studio's value -- for art, and (plan
2026-10-19-001 Unit 9) for rules, meta, transaction-delta and pack keys too.
Phase 1 called it ``mine``, which read backwards from here (the person at the
CLI is on the repository's side); ``mine`` is still accepted and sent as
``studio``. ``--in-github`` is refused here: Resolve in GitHub writes with the
platform App, a Studio act no game token may ask for.

``conflicts show <id>`` prints one conflict's full values (and a rules
conflict's file diff). ``discards`` lists what a Keep or a Discard this change
set aside, and restores or dismisses one (Unit 8's routes).
"""

from __future__ import annotations

import datetime
from typing import Any, Mapping, Optional

from exelichai_cli.commands import Context, plural
from exelichai_cli.output import CliError, EXIT_AUTH, EXIT_CONFLICTS, EXIT_OK, Problem

KEEPS = ("repo", "studio")
#: Older spellings, accepted and sent as their current name.
KEEP_ALIASES = {"mine": "studio"}


def iso_time(value: Any) -> str:
    """A listing time as ISO 8601 UTC (the server sends Unix seconds); text
    passes through, and a missing time is ``-``."""
    if isinstance(value, bool) or value is None or value == "":
        return "-"
    if isinstance(value, (int, float)):
        try:
            stamp = datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return str(value)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


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
                 f"detected {iso_time(c.get('detected_at'))}")
        _side(rep, "repo", c.get("repo") or {})
        _side(rep, "studio", c.get("browser") or {})
        keeps = c.get("keeps") or []
        if keeps:
            rep.info(f"    resolve: exelichai conflicts resolve {c.get('conflict_id')} "
                     f"--keep {'|'.join(keeps)}")
    rep.summary(f"### exelichai: {plural(len(entries), 'conflict')} held")
    return EXIT_CONFLICTS


def resolve(ctx: Context, conflict_id: str, keep: str) -> int:
    keep = KEEP_ALIASES.get(keep, keep)
    if keep not in KEEPS:
        raise CliError([Problem("conflict-resolve-invalid", f"--keep {keep!r}")])
    ctx.authenticate()
    out = ctx.client().resolve(conflict_id, keep).get("resolved") or {}
    ctx.reporter.info(f"resolved {conflict_id}: kept {keep}"
                      + (f" ({out.get('display_name')})" if out.get("display_name") else ""))
    return EXIT_OK


def resolve_in_github(ctx: Context, conflict_id: str) -> int:
    """Refused before any request: Resolve in GitHub has the platform App
    write to the repository, which only the owner's Studio session may ask
    for."""
    raise CliError([Problem("credential-scope",
                            f"Resolve in GitHub ({conflict_id}) writes with the platform "
                            "App, so only the Studio's Conflicts panel offers it")],
                   EXIT_AUTH,
                   notes=["Use Resolve in GitHub in the Studio's GitHub panel, or keep the "
                          f"Studio's value from here: `exelichai conflicts resolve {conflict_id} "
                          "--keep studio`, then `exelichai pull`, commit and push."])


def show(ctx: Context, conflict_id: str) -> int:
    rep = ctx.reporter
    ctx.authenticate()
    c = ctx.client().conflict(conflict_id).get("conflict") or {}
    field = f" [{c.get('field')}]" if c.get("field") else ""
    rep.info(f"{c.get('conflict_id')}  {c.get('display_name')}{field}  "
             f"({c.get('kind')} {c.get('address')})")
    if c.get("summary"):
        rep.info(f"  {c.get('summary')}")
    rep.info(f"  from {c.get('branch') or '-'} at {(c.get('commit') or '-')[:12]}, "
             f"detected {iso_time(c.get('detected_at'))}")
    _side(rep, "repo", c.get("repo_full") or c.get("repo") or {})
    _side(rep, "studio", c.get("browser_full") or c.get("browser") or {})
    diff = c.get("rules_diff")
    if isinstance(diff, list) and diff:
        for f in diff:
            if not isinstance(f, Mapping):
                continue
            rep.info(f"  {f.get('path')} (repo -> studio):")
            for line in f.get("lines") or []:
                op, text = line.get("op"), line.get("text", "")
                rep.info(f"    {text}" if op == "@" else f"    {op or ' '}{text}")
    elif c.get("rules_diff_unavailable"):
        rep.info(f"  rules diff unavailable: {c.get('rules_diff_unavailable')}")
    keeps = c.get("keeps") or []
    if keeps:
        rep.info(f"  resolve: exelichai conflicts resolve {c.get('conflict_id')} "
                 f"--keep {'|'.join(keeps)}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# unsent
# ---------------------------------------------------------------------------

def list_unsent(ctx: Context) -> int:
    rep = ctx.reporter
    ctx.authenticate()
    body = ctx.client().unsent()
    changes = body.get("changes") or []
    if not changes:
        rep.info("no unsent Studio changes")
    else:
        rep.info(f"{plural(len(changes), 'unsent Studio change')}:")
        for c in changes:
            detail = f" -- {c.get('detail')}" if c.get("detail") else ""
            rep.info(f"  {c.get('label') or c.get('address')}{detail}")
            rep.info(f"    key: {c.get('address')}")
        rep.info("discard one: exelichai unsent discard '<key>'   bring them into the "
                 "folder: exelichai pull")
    if body.get("unsent_rules_unavailable"):
        rep.info("(the unsent rules and packs could not be read just now; try again)")
    conflicts = int(body.get("conflicts") or 0)
    if conflicts:
        rep.info(f"{plural(conflicts, 'conflict')} held as well: run `exelichai conflicts`")
    return EXIT_OK


def discard_change(ctx: Context, address: str) -> int:
    ctx.authenticate()
    out = ctx.client().discard_change(address).get("discarded") or {}
    saved = out.get("discard") or {}
    ctx.reporter.info(f"discarded {out.get('kind') or ''} {out.get('address') or address}: "
                      "the Studio is back to the repository's value"
                      + (f"; its own is saved to Discards as {saved.get('discard_id')} "
                         f"(`exelichai discards restore {saved.get('discard_id')}` undoes it)"
                         if saved.get("discard_id") else ""))
    return EXIT_OK


# ---------------------------------------------------------------------------
# discards
# ---------------------------------------------------------------------------

def list_discards(ctx: Context) -> int:
    rep = ctx.reporter
    ctx.authenticate()
    client = ctx.client()
    entries, cursor, seen = [], None, set()
    while True:
        page = client.discards(cursor)
        entries.extend(page.get("discards") or [])
        cursor = page.get("next_cursor")
        if not cursor or not isinstance(cursor, str) or cursor in seen:
            break
        seen.add(cursor)
    if not entries:
        rep.info("no discards")
        return EXIT_OK
    rep.info(f"{plural(len(entries), 'discard')}, newest first:")
    for d in entries:
        field = f" [{d.get('field')}]" if d.get("field") else ""
        rep.info(f"  {d.get('discard_id')}  {d.get('display_name')}{field}  "
                 f"({d.get('kind')} {d.get('address')})")
        rep.info(f"    {d.get('cause') or '-'}, {iso_time(d.get('created_at'))}"
                 + ("" if d.get("present", True) else ", the item was absent"))
        if d.get("preview"):
            rep.info(f"    {str(d.get('preview')).splitlines()[0][:200]}")
    rep.info("restore: exelichai discards restore <id>   dismiss: exelichai discards "
             "dismiss <id> (or --all)")
    return EXIT_OK


def restore_discard(ctx: Context, discard_id: str, *, confirm: bool = False) -> int:
    ctx.authenticate()
    try:
        out = ctx.client().restore_discard(discard_id, confirm=confirm).get("restored") or {}
    except CliError as exc:
        if any(p.code == "discard-restore-confirm" for p in exc.problems):
            exc.notes.append(f"To replace that edit: exelichai discards restore {discard_id} "
                             "--confirm")
        raise
    ctx.reporter.info(f"restored {discard_id}: {out.get('kind')} {out.get('address')} "
                      f"({out.get('outcome')}); it is an unsent Studio edit again -- "
                      "`exelichai pull` brings it into the folder")
    return EXIT_OK


def dismiss_discard(ctx: Context, discard_id: Optional[str]) -> int:
    ctx.authenticate()
    client = ctx.client()
    if discard_id is None:
        out = client.dismiss_all_discards()
        ctx.reporter.info(f"dismissed {plural(int(out.get('dismissed') or 0), 'discard')}")
    else:
        client.dismiss_discard(discard_id)
        ctx.reporter.info(f"dismissed {discard_id}")
    return EXIT_OK
