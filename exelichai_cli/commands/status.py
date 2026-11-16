"""``status <job id>``: the minimal job view any game credential may read.

``sync status``: each deploy branch's newest sync, who started it
(``initiated_by``: ``creator`` or ``platform``), and each lane's LAST platform
re-sync attempt -- what that attempt recorded when it ended, not a fresh
check. ``sync rerun``: re-run a deploy branch's last synced commit through the
Exelichai GitHub App, as Re-run on its GitHub check does. ``sync update``:
what a waiting base update brings (read-only). All three take ``--json`` for
an agent.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Mapping, Optional

from exelichai_cli.commands import Context
from exelichai_cli.commands.sync import TERMINAL_STATES, follow, report_job
from exelichai_cli.output import EXIT_OK, CliError, public


def status(ctx: Context, job_id: str, *, wait: bool = False) -> int:
    ctx.authenticate()
    job = ctx.client().job(job_id).get("job") or {}
    if wait:
        return follow(ctx, job)
    if job.get("state") not in TERMINAL_STATES:
        ctx.reporter.report_job(job)
        ctx.reporter.info(f"job {job_id}: {job.get('state')} ({job.get('lane')} on "
                          f"{job.get('branch') or '-'})")
        return EXIT_OK
    return report_job(ctx, job, lane=job.get("lane"))


def _print_json(ctx: Context, body: Mapping[str, Any]) -> None:
    ctx.reporter.info(json.dumps(public(body), indent=2, sort_keys=True))


def _json_problems(ctx: Context, exc: CliError) -> None:
    _print_json(ctx, {"ok": False, "exit_code": exc.exit_code,
                      "problems": [{"code": p.code, "detail": p.detail,
                                    **({"message": p.message} if p.message else {})}
                                   for p in exc.problems]})


def _short(sha: Optional[str]) -> str:
    return str(sha)[:12] if sha else "-"


# What a held game's agent can do about a waiting base update: only the owner
# takes it in the Studio; a token (author or CI) re-checks or waits.
TAKE_IT = ("the owner presses Take the update in the Studio, or re-run the publish check "
           "or workflow")
NOTHING_WAITING = "No base update is waiting"

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _plain(value: Any) -> str:
    """A note is plain text: no escape sequences or control characters reach
    the terminal (newlines and tabs stay)."""
    return _CONTROL.sub("", _ANSI.sub("", str(value)))


def _base_label(base: Mapping[str, Any]) -> str:
    """The server's label already names the base ("Base game 1.1", or
    "Base game, updated <date>"); the bare name only when it sent none."""
    return _plain(base.get("label") or base.get("name") or base.get("game_id") or "")


def _update_line(update: Optional[Mapping[str, Any]]) -> str:
    """The waiting-update line for ``sync status`` ('' when none waits)."""
    if not update or update.get("state") not in ("waiting", "unknown"):
        return ""
    labels = ", ".join(_base_label(b) for b in update.get("bases") or []
                       if isinstance(b, Mapping)) or "a game this one builds on"
    return f"    Base update waiting: {labels} \u2014 {TAKE_IT}"


def _resync_line(view: Optional[Mapping[str, Any]]) -> str:
    if not view:
        return "  last platform re-sync attempt: none"
    state = str(view.get("kind"))
    if view.get("outcome"):
        state += f" ({view['outcome']})"
    if view.get("pending"):
        state += ", waiting on you"
    line = f"  last platform re-sync attempt: {state}"
    if view.get("job_id"):
        line += f", job {view['job_id']}"
    if view.get("recorded_at"):
        line += f", at {view['recorded_at']}"
    line += f"\n    {view.get('message') or ''}".rstrip()
    update = _update_line(view.get("update"))
    return line + (f"\n{update}" if update else "")


def sync_status(ctx: Context, *, as_json: bool = False) -> int:
    ctx.authenticate()
    try:
        body = ctx.client().sync_status()
    except CliError as exc:
        if as_json:
            _json_problems(ctx, exc)
        raise
    ctx.reporter.report_data("sync_status", body)
    if as_json:
        _print_json(ctx, body)
        return EXIT_OK
    rep = ctx.reporter
    if not body.get("connected"):
        rep.info(f"{ctx.require_game_id()} is not connected to GitHub")
        return EXIT_OK
    conn = body.get("connection") or {}
    last = body.get("last_sync") or {}
    resync = body.get("resync") or {}
    rep.info(f"{ctx.require_game_id()}: "
             f"{'GitHub App' if conn.get('app_bound') else 'workflow only'}"
             f"{', publishing from the App on' if conn.get('app_publishing') else ''}")
    for lane, key in (("draft", "draft_branch"), ("publish", "publish_branch")):
        branch = conn.get(key)
        entry = last.get(branch) or {}
        if entry:
            codes = [r.get("code") for r in entry.get("reasons") or [] if isinstance(r, Mapping)]
            rep.info(f"{branch} ({lane}): {entry.get('state')} {_short(entry.get('sha'))}, "
                     f"started by {entry.get('initiated_by') or 'creator'}, "
                     f"job {entry.get('job_id')}"
                     + (f" [{', '.join(str(c) for c in codes)}]" if codes else "")
                     + (f" (skipped: {entry['skipped']})" if entry.get("skipped") else ""))
        else:
            rep.info(f"{branch} ({lane}): no sync yet")
        rep.info(_resync_line(resync.get(lane)))
    return EXIT_OK


def sync_rerun(ctx: Context, *, branch: Optional[str] = None, lane: Optional[str] = None,
               as_json: bool = False) -> int:
    ctx.authenticate()
    try:
        body = ctx.client().rerun(lane=lane, branch=branch)
    except CliError as exc:
        if as_json:
            _json_problems(ctx, exc)
        raise
    ctx.reporter.report_data("rerun", body)
    if as_json:
        _print_json(ctx, {"ok": True, **body})
        return EXIT_OK
    job = body.get("job") or {}
    verb = "already syncing that commit" if body.get("deduped") else "re-run queued"
    ctx.reporter.info(f"{verb}: job {job.get('job_id')} ({job.get('lane')} on "
                      f"{job.get('branch')}, {_short(job.get('sha'))})")
    ctx.reporter.info(f"follow it with: exelichai status {job.get('job_id')} --wait")
    return EXIT_OK


def _flags(base: Mapping[str, Any]) -> List[str]:
    out = []
    if base.get("critical"):
        out.append("CRITICAL")
    if base.get("major"):
        out.append("MAJOR: May need changes to your game.")
    return out


def _print_base(rep: Any, base: Mapping[str, Any]) -> None:
    release = f" (from {_plain(base['was_release'])})" if base.get("was_release") else ""
    flags = _flags(base)
    rep.info(f"{_base_label(base)}{release}" + (f"  [{'; '.join(flags)}]" if flags else ""))
    for note in base.get("notes") or []:
        if not isinstance(note, Mapping):
            continue
        tags = " ".join(t for t in (str(note.get("level") or "").upper(),
                                    "CRITICAL" if note.get("critical") else "") if t)
        text = _plain(note.get("changelog") or "")
        rep.info(f"  {_plain(note.get('release'))}" + (f" [{tags}]" if tags else "")
                 + (f": {text}" if text else ""))
    changes = base.get("changes")
    if not isinstance(changes, Mapping):
        rep.info("  Changes can't be shown for this update.")
        return
    rep.info("  " + ", ".join(f"{changes.get(k) or 0} {k}" for k in
                              ("added", "removed", "changed", "touched", "orphaned")))
    for row in changes.get("overrides") or []:
        if isinstance(row, Mapping):
            rep.info(f"  {str(row.get('status') or '').upper()}: {_plain(row.get('item'))}"
                     f".{_plain(row.get('field'))}")


def sync_update(ctx: Context, *, as_json: bool = False) -> int:
    """What the waiting base update brings; read-only."""
    ctx.authenticate()
    try:
        body = ctx.client().base_update()
    except CliError as exc:
        if len(exc.problems) == 1 and exc.problems[0].code == "api-unreachable" \
                and exc.problems[0].detail.startswith("HTTP 404"):
            if as_json:
                _print_json(ctx, {"ok": True, "waiting": False})
            else:
                ctx.reporter.info(NOTHING_WAITING)
            return EXIT_OK
        if as_json:
            _json_problems(ctx, exc)
        raise
    ctx.reporter.report_data("base_update", body)
    if as_json:
        _print_json(ctx, body)
        return EXIT_OK
    if body.get("state") == "taken":
        ctx.reporter.info(NOTHING_WAITING)
        return EXIT_OK
    rep = ctx.reporter
    rep.info(f"Base update waiting on {_plain(body.get('branch') or ctx.require_game_id())} "
             f"({body.get('lane')})" + (f", recorded {body['recorded_at']}"
                                        if body.get("recorded_at") else ""))
    for base in body.get("bases") or []:
        if isinstance(base, Mapping):
            _print_base(rep, base)
    rep.info(f"To take it: {TAKE_IT}.")
    return EXIT_OK
