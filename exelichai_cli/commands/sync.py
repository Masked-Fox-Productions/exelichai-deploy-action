"""``validate``, ``push``, ``publish`` and ``ci``: upload the tree, finalise,
and wait for the sync job.

The SERVER decides the lane from the credential (validate token -> validate,
author token -> the draft import, an OIDC session -> whatever the run may do).
So each command first checks the credential runs the lane the creator asked
for, and refuses locally rather than run a different one:

* ``validate`` needs a validate-scope credential (an author token would run
  the draft import);
* ``push`` needs author scope;
* ``publish`` needs an OIDC session minted for the publish branch -- a static
  token, or anywhere outside GitHub Actions, is refused (``publish-oidc-only``);
* ``ci`` (what the Action runs) signs in with OIDC and runs whatever lane the
  exchange grants.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from exelichai_cli import tree
from exelichai_cli.commands import Context, git, plural
from exelichai_cli.output import (CliError, EXIT_AUTH, EXIT_CONFLICTS, EXIT_FAILED, EXIT_OK,
                                  EXIT_PUBLISH_REFUSED, EXIT_QUOTA, EXIT_TIMEOUT,
                                  PUBLISH_REFUSED_CODES, Problem, exit_code_for)

TERMINAL_STATES = ("succeeded", "failed", "superseded", "stale-refused", "cancelled")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

LANE_WORDS = {"validate": "validation", "draft": "draft sync", "publish": "publish"}


def _credential_for(ctx: Context, command: str):
    if command == "publish":
        if not ctx.in_actions:
            token = (ctx.env.get("EXELICHAI_TOKEN") or "").strip()
            raise CliError([Problem("publish-oidc-only",
                                    "a game token cannot publish" if token
                                    else "not running in GitHub Actions")], EXIT_AUTH)
        cred = ctx.authenticate(prefer_oidc=True)  # a token in env is ignored, never used
    else:
        cred = ctx.authenticate(prefer_oidc=command == "ci")
    if cred.kind == "oidc":
        cred.bearer()  # exchange now: the lane is only known afterwards
    want = {"validate": "validate", "push": "draft", "publish": "publish"}.get(command)
    if want and cred.lane != want:
        hint = {
            "validate": "validating needs a validate token (exlv_); an author token runs the "
                        "draft import -- use `exelichai push` for that",
            "push": "pushing the draft needs an author token (exla_)",
            "publish": "only a push to the publish branch signs in to publish",
        }[command]
        raise CliError([Problem("credential-scope",
                                f"this credential runs the {cred.lane} lane; {hint}")], EXIT_AUTH)
    return cred


def _report_text(folder: str) -> Dict[str, Any]:
    """CI-reported commit facts (labelled 'reported by CI' server-side)."""
    out: Dict[str, Any] = {}
    message = git(folder, "log", "-1", "--format=%B")
    author = git(folder, "log", "-1", "--format=%an <%ae>")
    if message:
        out["message"] = message[:4000]
    if author:
        out["author"] = author[:400]
    return out


def run(ctx: Context, command: str, *, allow_mass_retire: bool = False) -> int:
    rep = ctx.reporter
    files = tree.read_tree(ctx.folder)
    if tree.MANIFEST not in files:
        raise CliError([Problem("manifest-missing", f"no {tree.MANIFEST} in the folder; "
                                "run `exelichai init` first")])
    game_id = ctx.require_game_id()
    _rules, manifest_game = tree.manifest_rules(ctx.folder)
    if manifest_game and manifest_game != game_id:
        rep.warn(f"{tree.MANIFEST} names {manifest_game!r}; syncing to {game_id!r} "
                 "(the connected game wins)")
    cred = _credential_for(ctx, command)
    client = ctx.client()
    lane = cred.lane or "validate"

    manifest = tree.file_manifest(files)
    total = sum(f["size"] for f in manifest)  # type: ignore[misc]
    rep.info(f"{game_id}: {LANE_WORDS.get(lane, lane)} of {plural(len(manifest), 'file')} "
             f"({total} bytes) as {cred.kind} ({cred.scope})")
    plan = client.manifest(manifest)
    uploads: List[Mapping[str, Any]] = plan.get("uploads") or []
    by_sha = {f["sha256"]: files[f["path"]] for f in manifest}  # type: ignore[index]
    for up in uploads:
        data = by_sha.get(str(up.get("sha256")))
        if data is None:
            raise CliError([Problem("api-unreachable",
                                    f"the server asked for {up.get('sha256')!r}, not in the tree")])
        client.put_blob(up, data)
    rep.info(f"uploaded {plural(len(uploads), 'blob')} "
             f"({plan.get('present', 0)} already stored)")

    body: Dict[str, Any] = {"files": manifest, **_report_text(ctx.folder)}
    if allow_mass_retire:
        body["allow_mass_retire"] = True
    if cred.kind == "token":
        head = git(ctx.folder, "rev-parse", "HEAD")
        if head and _SHA_RE.match(head):
            body["sha"] = head
        if lane == "validate":
            branch = (ctx.env.get("GITHUB_HEAD_REF") or ctx.env.get("GITHUB_REF_NAME")
                      or git(ctx.folder, "rev-parse", "--abbrev-ref", "HEAD"))
            if branch and branch != "HEAD":
                body["branch"] = branch
    done = client.finalise(body)
    if done.get("skipped"):
        rep.info(f"skipped: {done.get('reason')} -- nothing changed since the last sync")
        rep.summary(f"### exelichai: {LANE_WORDS.get(lane, lane)} skipped")
        rep.summary(f"Nothing changed since the last sync (`{done.get('reason')}`).")
        return EXIT_OK
    job = done.get("job") or {}
    if done.get("repeat"):
        rep.info("this workflow run was already finalised; following its job")
    return follow(ctx, job, lane=lane)


def follow(ctx: Context, job: Mapping[str, Any], *, lane: Optional[str] = None) -> int:
    """Poll ``job`` to a terminal state (or the timeout) and report it."""
    rep = ctx.reporter
    client = ctx.client()
    job_id = str(job.get("job_id") or "")
    rep.info(f"job {job_id}: {job.get('state')}")
    deadline = ctx.clock() + ctx.timeout
    last_state = job.get("state")
    while job.get("state") not in TERMINAL_STATES:
        if ctx.clock() >= deadline:
            p = Problem("job-timeout", f"job {job_id} is still {job.get('state')}")
            rep.problem(p)
            rep.info(f"job id: {job_id}")
            rep.summary(f"### exelichai: timed out waiting for job `{job_id}`")
            rep.summary_problems([p])
            return EXIT_TIMEOUT
        ctx.sleep(ctx.poll_interval)
        job = client.job(job_id).get("job") or {}
        if job.get("state") != last_state:
            last_state = job.get("state")
            rep.info(f"job {job_id}: {last_state}")
    return report_job(ctx, job, lane=lane or job.get("lane"))


def _report_warnings(rep, job: Mapping[str, Any]) -> None:
    """A committed publish's advisory warnings (a release missing its Close
    button, say): printed and summarised, never an exit code -- the server
    published, and says so in the message itself."""
    for w in job.get("warnings") or []:
        message = w.get("message") if isinstance(w, Mapping) else None
        if message:
            rep.warn(str(message))
            rep.summary(f"> {message}")


def report_job(ctx: Context, job: Mapping[str, Any], *, lane: Optional[str]) -> int:
    rep = ctx.reporter
    state = job.get("state")
    job_id = job.get("job_id")
    problems = [Problem.from_dict(r) for r in (job.get("reasons") or []) if isinstance(r, dict)]
    counts = job.get("counts") or {}
    conflicts = int(counts.get("conflicts") or 0)
    unsent = int(counts.get("unsent") or 0)
    what = LANE_WORDS.get(lane or "", lane or "sync")
    head = f"### exelichai: {what} {state}"
    rep.summary(head)
    rep.summary(f"Job `{job_id}` on `{job.get('branch') or '-'}` "
                f"at `{(job.get('sha') or '-')[:12]}`.")

    if state == "succeeded":
        if conflicts:
            rep.info(f"{what} succeeded, but {plural(conflicts, 'conflict')} "
                     f"{'is' if conflicts == 1 else 'are'} held: run `exelichai conflicts`")
            rep.summary(f"**{plural(conflicts, 'conflict')} held.** Resolve them in the GitHub "
                        "panel or with `exelichai conflicts`.")
            rep.problems([Problem("conflicts-held", f"{plural(conflicts, 'conflict')} held")])
            return EXIT_CONFLICTS
        rep.info(f"{what} succeeded")
        _report_warnings(rep, job)
        if unsent:
            rep.info(f"{plural(unsent, 'Art Studio edit')} not in the repo yet: run "
                     "`exelichai pull`, commit and push")
            rep.summary(f"{plural(unsent, 'Art Studio edit')} not in the repo yet "
                        "(`exelichai pull`).")
        return EXIT_OK

    if not problems:
        problems = [Problem("worker-error", f"job {job_id} ended {state} with no reason")]
    rep.problems(problems)
    rep.summary_problems(problems)
    if job.get("errors_truncated"):
        rep.info(f"(showing {len(problems)} of {job.get('error_count')} errors)")
    codes = {p.code for p in problems}
    if state in ("superseded", "stale-refused"):
        return EXIT_QUOTA
    if lane == "publish" and codes & PUBLISH_REFUSED_CODES:
        rep.info("publishing was refused: the Art Studio holds edits the pushed tree does not "
                 "carry. Run `exelichai pull` on the draft branch, commit, push, and merge again.")
        return EXIT_PUBLISH_REFUSED
    if "conflicts-held" in codes:
        return EXIT_CONFLICTS
    return exit_code_for(codes, default=EXIT_FAILED)
