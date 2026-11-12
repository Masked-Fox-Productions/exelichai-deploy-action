"""``status <job id>``: the minimal job view any game credential may read.

``sync status``: each deploy branch's newest sync, who started it
(``initiated_by``: ``creator`` or ``platform``), and each lane's LAST platform
re-sync attempt -- what that attempt recorded when it ended, not a fresh
check. ``sync rerun``: re-run a deploy branch's last synced commit through the
Exelichai GitHub App, as Re-run on its GitHub check does. Both take
``--json`` for an agent.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

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
    return line + f"\n    {view.get('message') or ''}".rstrip()


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
