"""``status <job id>``: the minimal job view any game credential may read."""

from __future__ import annotations

from exelichai_cli.commands import Context
from exelichai_cli.commands.sync import TERMINAL_STATES, follow, report_job
from exelichai_cli.output import EXIT_OK


def status(ctx: Context, job_id: str, *, wait: bool = False) -> int:
    ctx.authenticate()
    job = ctx.client().job(job_id).get("job") or {}
    if wait:
        return follow(ctx, job)
    if job.get("state") not in TERMINAL_STATES:
        ctx.reporter.info(f"job {job_id}: {job.get('state')} ({job.get('lane')} on "
                          f"{job.get('branch') or '-'})")
        return EXIT_OK
    return report_job(ctx, job, lane=job.get("lane"))
