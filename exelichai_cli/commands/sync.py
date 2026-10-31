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

``validate --local`` (:func:`run_local`) needs no credential and no network: it
runs the server's structural checks from the optional ``exelichai-rules``
package (``pip install 'exelichai[local]'``) and names the ones it cannot run.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

from exelichai_cli import api, tree
from exelichai_cli.commands import Context, git, plural
from exelichai_cli.output import (CliError, EXIT_AUTH, EXIT_CONFLICTS, EXIT_FAILED, EXIT_OK,
                                  EXIT_PUBLISH_REFUSED, EXIT_QUOTA, EXIT_TIMEOUT,
                                  PUBLISH_REFUSED_CODES, Problem, catalogue, exit_code_for,
                                  guide_url)

TERMINAL_STATES = ("succeeded", "failed", "superseded", "stale-refused", "cancelled")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

LANE_WORDS = {"validate": "validation", "draft": "draft sync", "publish": "publish"}
#: Manifest entries per page (the server's ``upload_session.CLIENT_PAGE_FILES``).
PAGE_FILES = 500
#: Seconds before a presigned URL's expiry at which the page is presigned
#: again rather than risk a PUT that outlives it.
URL_MARGIN = 60
#: Ignored paths printed by name.
MAX_IGNORED_NAMED = 20


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


def env_flag(env: Mapping[str, str], name: str) -> bool:
    """A boolean environment switch: ``1``, ``true`` or ``yes`` (any case)."""
    return str(env.get(name) or "").strip().lower() in ("1", "true", "yes")


def _rules_first(entry: Mapping[str, Any]) -> Tuple[int, str]:
    """Page order: the rules area first (stage 2 reads it), then the other
    non-media files, then media -- the server's own presign order."""
    path = str(entry["path"])
    if path.startswith(tree.MEDIA_DIR):
        return (2, path)
    return (0 if tree.structural_class(path) == tree.RULES else 1, path)


def upload_tree(ctx: Context, scanned: tree.Scan, *,
                target: Optional[str] = None) -> Dict[str, Any]:
    """Upload ``scanned`` through an upload session (plan 2026-10-26-002
    Unit 3): the manifest in pages of ``PAGE_FILES``, each page's presigned
    blobs streamed from disk (``api.TRANSFER_WORKERS`` at once), then the
    candidate stage and its blobs. An upload whose URL has expired is
    presigned again (the same page, or the same candidate cursor) and retried
    once. Returns ``{session, manifest_sha, uploaded, present, ignored,
    confirmed, dev}`` -- ``confirmed`` the shas the server now holds for this
    manifest."""
    client = ctx.client()
    by_sha = scanned.by_sha()
    manifest = sorted(scanned.manifest(), key=_rules_first)
    out: Dict[str, Any] = {"uploaded": 0, "present": 0, "dev": None}

    def send(plan: Mapping[str, Any], again) -> None:
        # The page's blobs go up concurrently (``api.TRANSFER_WORKERS``), and
        # all of them land before the next page or stage is asked for: stage
        # 2 reads the rules area, and a later page's presign must see what
        # this one stored. ``latest`` is the page's current presign; an
        # expiry presigns it again ONCE however many uploads notice it.
        for up in plan.get("uploads") or []:
            sha_ = str(up.get("sha256"))
            if sha_ not in by_sha:
                raise CliError([Problem("api-unreachable",
                                        f"the server asked for {sha_!r}, not in the tree")])
        lock = threading.Lock()
        latest = {"plan": plan, "issued": ctx.clock(), "round": 0}

        def presign_again(seen: int) -> Tuple[Mapping[str, Any], int]:
            with lock:
                if latest["round"] == seen:
                    latest.update(plan=again(), issued=ctx.clock(), round=seen + 1)
                return latest["plan"], latest["round"]

        def put(up: Mapping[str, Any]) -> None:
            sha_ = str(up.get("sha256"))
            entry = by_sha[sha_]
            with lock:
                current, issued, seen = latest["plan"], latest["issued"], latest["round"]
            if ctx.clock() - issued > float(current.get("expires_in") or 900) - URL_MARGIN:
                current, seen = presign_again(seen)
            if seen:
                up = _same_blob(current, sha_)
                if up is None:
                    return  # stored meanwhile
            try:
                client.put_file(up, entry.full, path=entry.path)
            except api.UploadExpired:
                fresh = _same_blob(presign_again(seen)[0], sha_)
                if fresh is not None:
                    client.put_file(fresh, entry.full, path=entry.path)
            with lock:
                out["uploaded"] += 1

        api.concurrently(plan.get("uploads") or [], put)
        out["present"] += int(plan.get("present") or 0)

    session: Optional[str] = None
    pages = [manifest[i:i + PAGE_FILES] for i in range(0, len(manifest), PAGE_FILES)] or [[]]
    for index, chunk in enumerate(pages):
        def page_call(index=index, chunk=chunk):
            return client.manifest_page(chunk, page=index, session=session, target=target)

        plan = page_call()
        session = str(plan.get("session") or "")
        out["dev"] = plan.get("dev") or out["dev"]
        send(plan, page_call)
    cursor: Optional[int] = 0
    ignored: Mapping[str, Any] = {}
    digest = None
    while cursor is not None:
        def stage_call(cursor=cursor):
            return client.candidates(session, cursor=cursor, target=target)

        plan = stage_call()
        digest = plan.get("manifest_sha")
        ignored = plan.get("ignored") or ignored
        send(plan, stage_call)
        cursor = plan.get("next_cursor")
    skipped = set(ignored.get("paths") or [])
    candidates = {e.sha256 for e in scanned.entries.values() if e.cls == tree.CANDIDATE}
    confirmed = {e.sha256 for p, e in scanned.entries.items() if p not in skipped}
    if ignored.get("truncated"):
        confirmed -= candidates
    out.update(session=session, manifest_sha=digest, ignored=dict(ignored),
               confirmed=confirmed)
    return out


def _same_blob(plan: Mapping[str, Any], sha_: str) -> Optional[Mapping[str, Any]]:
    return next((u for u in plan.get("uploads") or [] if u.get("sha256") == sha_), None)


def report_ignored(rep, scanned: tree.Scan, server: Optional[Mapping[str, Any]] = None) -> None:
    """``N files ignored``: the ignored types found locally, plus the
    candidates the server found nothing references."""
    # The CLI never sends an ignored type, so the server's list is the
    # unreferenced candidates alone: the two never overlap.
    local = list(scanned.ignored)
    named = list((server or {}).get("paths") or [])
    count = len(local) + int((server or {}).get("count") or 0)
    if not count:
        return
    rep.info(f"{plural(count, 'file')} ignored (never uploaded or published):")
    for path in (local + named)[:MAX_IGNORED_NAMED]:
        rep.info(f"  {path}")
    if count > MAX_IGNORED_NAMED:
        rep.info(f"  ... and {count - MAX_IGNORED_NAMED} more")


def run(ctx: Context, command: str, *, allow_mass_retire: bool = False,
        allow_first_import: bool = False) -> int:
    rep = ctx.reporter
    scanned = tree.scan(ctx.folder)
    if tree.MANIFEST not in scanned.entries:
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

    manifest = scanned.manifest()
    total = sum(f["size"] for f in manifest)  # type: ignore[misc]
    rep.info(f"{game_id}: {LANE_WORDS.get(lane, lane)} of {plural(len(manifest), 'file')} "
             f"({total} bytes) as {cred.kind} ({cred.scope})")
    sent = upload_tree(ctx, scanned)
    rep.info(f"uploaded {plural(sent['uploaded'], 'blob')} "
             f"({sent['present']} already stored)")
    report_ignored(rep, scanned, sent["ignored"])

    body: Dict[str, Any] = {"protocol": api.UPLOAD_PROTOCOL, "session": sent["session"],
                            "manifest_sha": sent["manifest_sha"], **_report_text(ctx.folder)}
    if allow_mass_retire:
        body["allow_mass_retire"] = True
    if allow_first_import:
        # The first import after connecting, into a game that already has
        # Studio content (plan 2026-10-26-001 KTD-10): held unless asked.
        body["allow_first_import"] = True
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
    final = await_job(ctx, job)
    if final is None:
        return EXIT_TIMEOUT
    return report_job(ctx, final, lane=lane or final.get("lane"))


def await_job(ctx: Context, job: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Poll ``job`` to a terminal state and return it, unreported; ``None``
    (the timeout reported) when it does not get there in time."""
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
            return None
        ctx.sleep(ctx.poll_interval)
        job = client.job(job_id).get("job") or {}
        if job.get("state") != last_state:
            last_state = job.get("state")
            rep.info(f"job {job_id}: {last_state}")
    return job


def _report_warnings(rep, job: Mapping[str, Any]) -> None:
    """A succeeded job's advisory warnings (a release missing its Close
    button, an entity set aside because its base is gone): printed and
    summarised, never an exit code -- the server did the work, and says so in
    the message itself. A catalogued code links its guide entry."""
    for w in job.get("warnings") or []:
        message = w.get("message") if isinstance(w, Mapping) else None
        if message:
            code = str(w.get("code") or "")
            link = guide_url(code, rep.guide_base) if code in catalogue() else None
            rep.warn(f"{message} {link}" if link else str(message))
            rep.summary(f"> {message}" + (f" ([guide]({link}))" if link else ""))


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
            rep.info(f"{plural(unsent, 'Studio change')} not in the repo yet: run "
                     "`exelichai pull`, commit and push")
            rep.summary(f"{plural(unsent, 'Studio change')} not in the repo yet "
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


# ---------------------------------------------------------------------------
# validate --local (plan 2026-10-18-003 Unit 13)
# ---------------------------------------------------------------------------

def _local_rules():
    """The ``exelichai_rules`` package (the ``local`` extra), or a refusal
    naming how to install it."""
    try:
        import exelichai_rules
    except ModuleNotFoundError as exc:
        if exc.name != "exelichai_rules":
            raise
        raise CliError([Problem("local-checks-unavailable",
                                "the exelichai-rules package is not installed")]) from None
    return exelichai_rules


def run_local(ctx: Context) -> int:
    """``validate --local``: the server's structural checks on the folder,
    offline. Reported like a validate job (its reasons, warnings and exit
    code), plus the checks that need the server, by name."""
    rules = _local_rules()
    report = rules.validate_folder(ctx.folder)
    return report_local(ctx, report, version=getattr(rules, "__version__", "?"))


def report_local(ctx: Context, report: Mapping[str, Any], *, version: str) -> int:
    rep = ctx.reporter
    state = report.get("state")
    counts = report.get("validation") or {}
    game = f"{report['game_id']}: " if report.get("game_id") else ""
    files = f" of {plural(int(counts['files']), 'file')}" if counts.get("files") else ""
    rep.info(f"{game}local validation{files} (exelichai-rules {version}, no server)")
    rep.summary(f"### exelichai: local validation {state}")
    rep.summary(f"Offline checks from `exelichai-rules` {version}: "
                f"{', '.join(report.get('checked') or [])}.")
    problems = [Problem.from_dict(r) for r in (report.get("reasons") or [])
                if isinstance(r, Mapping)]
    if problems:
        rep.problems(problems)
        rep.summary_problems(problems)
        if report.get("errors_truncated"):
            rep.info(f"(showing {len(problems)} of {report.get('error_count')} errors)")
    for w in report.get("warnings") or []:
        if not isinstance(w, Mapping) or not w.get("message"):
            continue
        where = f"{w['file']}:{w['line']}: " if w.get("file") and w.get("line") else (
            f"{w['file']}: " if w.get("file") else "")
        rep.warn(f"{where}{w['message']}")
        rep.summary(f"> {where}{w['message']}")
    skipped = [s for s in report.get("skipped") or [] if isinstance(s, Mapping)]
    if skipped:
        names = ", ".join(str(s.get("check")) for s in skipped)
        rep.info(f"not checked offline: {names}. `exelichai validate` runs every check.")
        for s in skipped:
            rep.info(f"  {s.get('check')}: {s.get('detail')}")
        rep.summary(f"Not checked offline: {names}. `exelichai validate` runs every check.")
    if state == "succeeded" and not problems:
        rep.info("local validation succeeded")
        return EXIT_OK
    if not problems:
        problems = [Problem("worker-error", f"local validation ended {state} with no reason")]
        rep.problems(problems)
    rep.info("local validation failed")
    return exit_code_for({p.code for p in problems}, default=EXIT_FAILED)
