"""``playtest``: play the local folder on the server, step by step, before
anything is pushed -- and save what it proves as a game test (plan
2026-11-11-002 Unit 7; KTD-7, KTD-8).

``exelichai playtest [steps.yaml] [--step STEP]...`` uploads the folder
exactly as ``exelichai validate`` uploads it (:func:`sync.upload_tree`: the
manifest pages, then the candidate stage), never finalises it, and sends the
steps to ``POST .../github/playtest`` naming that upload. A folder unchanged
since its last upload (by ``playtest``, ``validate`` or ``push``, with the same
token, within :data:`upload_memo.REUSE_SECONDS`) is not uploaded again: the
playtest names that upload, and spends nothing of the game's upload budget.
If the server can no longer play it, the folder is uploaded and the playtest
asked once more. ``--version``
plays a stored release instead, and uploads nothing. Steps are the game-test
grammar (``run``, ``expect``, ``refused``, each ``run``/``refused`` with an
optional ``with``) plus ``look``. A steps file is a YAML list of steps, or a
mapping of ``steps`` with ``seed``, ``player``, ``pin`` and ``explain``.

After each step the answer carries its outcome and the pinned cells
(``--pin ASSET[@OWNER]``); after each ``look`` and the final step, what the
player is offered, what is disabled or not offered, where they stand and
their stats. ``--explain TXN`` asks why a transaction is not offered.

**Saving** (``--save tests/<name>.yaml --pin ...``): the server translates the
played steps into a test, runs it against the same uploaded folder, and
returns its YAML only when it passed. This command checks the path before any
request -- a relative ``tests/<name>.yaml`` inside the game folder, no
``..``, nothing absolute, no symbolic link on the way, an existing file only
with ``--overwrite`` -- and writes the server's text, unchanged, only when
``save.verified`` is true. It never translates on its own, and writes nothing
on any failure.

Exit codes: 0 when every step played (a refused or unavailable step is a
finding, not a failure); 1 when the game did not start, a step's ``expect``
did not hold, a ``refused`` step was available, a step errored or a snapshot
could not be taken, or a save was refused or failed; 64 for a request this
command refuses before sending it.

Text from the server (messages, display names, the saved YAML) is printed in
labelled fields as data, never interpreted.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from exelichai_cli import api, tree, upload_memo
from exelichai_cli.commands import Context, gametest, plural, sync
from exelichai_cli.output import EXIT_FAILED, EXIT_OK, EXIT_USAGE, CliError, Problem

INVALID = "playtest-invalid"
NOT_SAVABLE = "playtest-not-savable"
SAVE_FAILED = "playtest-save-failed"
#: The keys a steps file's mapping may hold (the server's playtest document).
DOC_KEYS = ("steps", "seed", "player", "pin", "explain")
#: A saved test's path: the server's ``game_test_files.PATH_RE``.
SAVE_PATH_RE = re.compile(r"^tests/[^/.][^/]*\.ya?ml$")
#: Step outcomes that make the command exit 1.
#: The refusals on which a reused upload is forgotten and the folder uploaded
#: again (``upload_memo``): the server cannot play that upload any more.
STALE_UPLOAD = frozenset({"playtest-upload-stale", "blob-expired"})
FAILING_OUTCOMES = frozenset({"expectation-failed", "unexpectedly-available", "error",
                              "snapshot-error"})


def _usage(detail: str, **kw: Any) -> CliError:
    return CliError([Problem(INVALID, detail, **kw)], EXIT_USAGE)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

def _load_steps_file(ctx: Context, path: str) -> Dict[str, Any]:
    full = path if os.path.isabs(path) else os.path.join(ctx.workspace, path)
    try:
        with open(full, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except OSError as exc:
        raise _usage(f"cannot read the steps file: {exc.strerror or exc}", file=path) from None
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise _usage(f"the steps file is not YAML: {exc}", file=path) from None
    if isinstance(doc, list):
        return {"steps": doc}
    if isinstance(doc, Mapping) and "steps" in doc:
        unknown = sorted(str(k) for k in doc if k not in DOC_KEYS)
        if unknown:
            raise _usage(f"unknown keys: {', '.join(unknown)} (a steps file holds "
                         f"{', '.join(DOC_KEYS)})", file=path)
        return dict(doc)
    raise _usage("a steps file is a list of steps, or a mapping with `steps:`", file=path)


def _parse_step(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _usage(f"--step {text!r} is not YAML: {exc}") from None


def parse_pin(text: str) -> Dict[str, str]:
    """``ASSET[@OWNER]`` as the test grammar's cell ``{asset_id, owner?}``
    (the server defaults the owner to ``buyer``)."""
    asset, sep, owner = text.partition("@")
    if not asset or (sep and not owner):
        raise _usage(f"--pin {text!r}: a cell is ASSET or ASSET@OWNER")
    return {"asset_id": asset, "owner": owner} if sep else {"asset_id": asset}


def build_request(ctx: Context, *, steps_file: Optional[str], steps: Sequence[str],
                  seed: Optional[int], player: Optional[str], pins: Sequence[str],
                  explain: Sequence[str]) -> Dict[str, Any]:
    """``{steps, seed?, player?, pin?, explain?}`` from the steps file and
    the flags: ``--step`` adds steps after the file's, ``--pin`` and
    ``--explain`` add to its lists, ``--seed`` and ``--player`` win."""
    doc: Dict[str, Any] = _load_steps_file(ctx, steps_file) if steps_file else {}
    out: Dict[str, Any] = {}
    file_steps = doc.get("steps") if steps_file else []
    if not isinstance(file_steps, list):
        raise _usage("`steps` is a list of steps", file=steps_file)
    out["steps"] = list(file_steps) + [_parse_step(s) for s in steps]
    if not out["steps"]:
        raise _usage("no steps: pass a steps file or --step (run, expect, refused or look)")
    for key, flag in (("seed", seed), ("player", player)):
        value = flag if flag is not None else doc.get(key)
        if value is not None:
            out[key] = value
    pin = list(doc.get("pin") or []) + [parse_pin(p) for p in pins]
    if pin:
        out["pin"] = pin
    wanted = list(doc.get("explain") or []) + list(explain)
    if wanted:
        out["explain"] = wanted
    return out


def check_save_path(folder: str, rel: str, *, overwrite: bool) -> str:
    """``rel`` when the CLI may write a saved test there, else a refusal:
    a relative ``tests/<name>.yaml`` (or ``.yml``) inside ``folder``, no
    ``..`` or absolute path, no symbolic link on the way (``tests/`` or the
    file itself), and an existing file only with ``overwrite``."""
    err = tree.write_path_error(rel)
    if err is not None:
        raise CliError([err], EXIT_USAGE)
    if not SAVE_PATH_RE.fullmatch(rel):
        raise _usage(f"--save {rel!r}: a saved playtest is a test file, "
                     "`tests/<name>.yaml` in the game folder", file=rel)
    try:
        target = tree.confined_target(folder, rel)
    except CliError as exc:
        raise CliError(exc.problems, EXIT_USAGE) from None
    if os.path.lexists(target):
        if not os.path.isfile(target):
            raise _usage(f"{rel} exists and is not a file", file=rel)
        if not overwrite:
            raise _usage(f"{rel} exists: pass --overwrite to replace it", file=rel)
    return rel


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(", ", ": "), default=str)


def _rows(rows: Any) -> str:
    out: List[str] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        tid = str(row.get("transaction_id") or "?")
        name = row.get("display_name")
        out.append(f"{tid} ({name})" if name and name != tid else tid)
    return ", ".join(out) if out else "(none)"


def _cells(values: Mapping[str, Any]) -> str:
    return ", ".join(f"{k}={_compact(v)}" for k, v in values.items()) or "(none)"


def _step_head(step: Mapping[str, Any]) -> str:
    head = f"step {step.get('step')} {step.get('kind')}"
    if step.get("transaction_id"):
        head += f" {step['transaction_id']}"
    if step.get("with"):
        head += f" with {_compact(step['with'])}"
    return f"{head}: {step.get('outcome')}"


def print_snapshot(rep, snap: Mapping[str, Any]) -> None:
    rep.info(f"  offered: {_rows(snap.get('offered'))}")
    rep.info(f"  disabled: {_rows(snap.get('disabled'))}")
    rep.info(f"  not offered: {_rows(snap.get('not_offered'))}")
    if snap.get("location") is not None:
        rep.info(f"  location: {_compact(snap['location'])}")
    if snap.get("status_bar") is not None:
        rep.info(f"  status bar: {_compact(snap['status_bar'])}")
    rep.info(f"  stats: {_cells(snap.get('stats') or {})}")
    for tid, why in (snap.get("explained") or {}).items():
        why = why if isinstance(why, Mapping) else {}
        if why.get("available"):
            rep.info(f"  explained {tid}: offered")
        elif why.get("withheld"):
            rep.info(f"  explained {tid}: withheld (sealed)")
        else:
            rep.info(f"  explained {tid}:")
            for clause in why.get("clauses") or []:
                rep.info(f"    unmet: {_compact(clause)}")


def print_steps(rep, answer: Mapping[str, Any]) -> None:
    """One block per step: the outcome, its labelled detail, the pinned
    cells and any snapshot."""
    for step in answer.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        rep.info(_step_head(step))
        if step.get("message"):
            rep.info(f"  message: {step['message']}")
        if step.get("code"):
            rep.info(f"  code: {step['code']}")
        if step.get("refused_by"):
            rep.info(f"  refused by: {step['refused_by']}")
        if step.get("withheld"):
            rep.info("  withheld: the refusing transaction is sealed")
        if step.get("condition"):
            rep.info(f"  condition: {_compact(step['condition'])}")
        if step.get("observed") is not None:
            rep.info(f"  observed: {_compact(step['observed'])}")
        for clause in step.get("clauses") or []:
            rep.info(f"  unmet: {_compact(clause)}")
        if step.get("pinned"):
            rep.info(f"  pinned: {_cells(step['pinned'])}")
        if step.get("snapshot_error"):
            rep.info(f"  snapshot error: {step['snapshot_error']}")
        if isinstance(step.get("snapshot"), Mapping):
            print_snapshot(rep, step["snapshot"])


def exit_code(answer: Mapping[str, Any]) -> int:
    if not answer.get("started", True) or answer.get("failure"):
        return EXIT_FAILED
    outcomes = {str(s.get("outcome")) for s in answer.get("steps") or []
                if isinstance(s, Mapping)}
    return EXIT_FAILED if outcomes & FAILING_OUTCOMES else EXIT_OK


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def _post(ctx: Context, body: Mapping[str, Any]) -> Dict[str, Any]:
    """The playtest call; a server that predates the route is named as such."""
    try:
        return ctx.client().playtest(body)
    except CliError as exc:
        codes = [p.code for p in exc.problems]
        if codes == ["NOT_FOUND"] or (codes == ["api-unreachable"]
                                      and exc.problems[0].detail.startswith("HTTP 404")):
            raise CliError([Problem("cli-upgrade-required",
                                    "this server does not serve playtests yet (it needs a "
                                    "newer server, not a newer exelichai)")]) from None
        raise


def _upload_fields(sent: Mapping[str, Any]) -> Dict[str, Any]:
    return {"protocol": api.UPLOAD_PROTOCOL, "session": sent["session"],
            "manifest_sha": sent["manifest_sha"]}


def _play_folder(ctx: Context, body: Mapping[str, Any]) -> Dict[str, Any]:
    """Play the folder: the upload of it :mod:`upload_memo` recalls, while
    the server can still play it, else a fresh upload."""
    rep = ctx.reporter
    scanned, game_id = sync.scan_game_folder(ctx)
    ctx.authenticate()
    rep.info(f"{game_id}: playtest of the folder ({plural(len(body['steps']), 'step')}), "
             "uploaded as validate uploads it; nothing is pushed")
    reused = upload_memo.recall(ctx, scanned)
    if reused is not None:
        try:
            answer = _post(ctx, {**body, **_upload_fields(reused)})
        except CliError as exc:
            if not {p.code for p in exc.problems} & STALE_UPLOAD:
                raise
            upload_memo.forget(ctx, scanned)
            rep.info("the folder's last upload can no longer be played: uploading it again")
        else:
            age = max(0, int(ctx.clock() - float(reused["at"]))) // 60
            rep.info(f"played the folder's upload from {age} min ago: the folder is "
                     "unchanged, so nothing was uploaded")
            sync.report_ignored(rep, scanned, reused.get("ignored") or {})
            return answer
    sent = sync.upload_tree(ctx, scanned)
    rep.info(f"uploaded {plural(sent['uploaded'], 'blob')} ({sent['present']} already stored)")
    sync.report_ignored(rep, scanned, sent["ignored"])
    return _post(ctx, {**body, **_upload_fields(sent)})


def run(ctx: Context, *, steps_file: Optional[str] = None, steps: Sequence[str] = (),
        seed: Optional[int] = None, player: Optional[str] = None,
        pins: Sequence[str] = (), explain: Sequence[str] = (),
        version: Optional[str] = None, save: Optional[str] = None,
        overwrite: bool = False, name: Optional[str] = None) -> int:
    rep = ctx.reporter
    body = build_request(ctx, steps_file=steps_file, steps=steps, seed=seed, player=player,
                         pins=pins, explain=explain)
    # Everything a save needs is checked before the first request (KTD-8).
    if save is not None:
        if not body.get("pin"):
            raise _usage("--save needs at least one --pin: a test with no `expect:` proves "
                         "nothing about the game's state")
        check_save_path(ctx.folder, save, overwrite=overwrite)
        body.update(save=True, save_as=save)
        if name:
            body["name"] = name
    elif name or overwrite:
        raise _usage("--name and --overwrite go with --save")

    if version:
        game_id = ctx.require_game_id()
        ctx.authenticate()
        rep.info(f"{game_id}: playtest of release {version} "
                 f"({plural(len(body['steps']), 'step')})")
        body["version"] = version
        answer = _post(ctx, body)
    else:
        answer = _play_folder(ctx, body)
    rep.report_data("playtest", answer)
    for w in answer.get("warnings") or []:
        if isinstance(w, Mapping) and w.get("message"):
            rep.warn(f"warning: {w['message']}")
    rep.info(f"seed {answer.get('seed')}, player {answer.get('player')}")
    failure = answer.get("failure")
    if isinstance(failure, Mapping):
        rep.info(f"the game did not start: {failure.get('message')}")
    print_steps(rep, answer)
    code = exit_code(answer)
    rep.summary(f"### exelichai playtest: {plural(len(answer.get('steps') or []), 'step')} "
                f"played{'' if code == EXIT_OK else ', with a failure'}")
    if save is None:
        return code
    return _write_saved(ctx, save, answer.get("save"), overwrite=overwrite, code=code)


def _write_saved(ctx: Context, rel: str, saved: Any, *, overwrite: bool, code: int) -> int:
    """Write the server's verified YAML to ``rel`` -- only that, and only
    when the server verified it; otherwise print why and write nothing."""
    rep = ctx.reporter
    if not isinstance(saved, Mapping):
        raise CliError([Problem(SAVE_FAILED, "the server sent no save result; nothing was "
                                             "written", file=rel)])
    text = saved.get("yaml")
    if saved.get("verified") is True and isinstance(text, str):
        check_save_path(ctx.folder, rel, overwrite=overwrite)  # still true now
        tree.write_file(ctx.folder, rel, text.encode("utf-8"))
        rep.report_data("saved", {"path": rel, "written": True})
        rep.info(f"wrote {rel}: the test passed against the folder")
        rep.summary(f"Wrote `{rel}`, verified against the folder.")
        return code
    rep.report_data("saved", {"path": rel, "written": False})
    report = saved.get("report")
    if isinstance(report, Mapping):
        gametest.print_report(ctx, report)
    step = saved.get("step")
    where = f"step {step}: " if step else ""
    detail = str(saved.get("message") or "the server did not verify the test")
    raise CliError([Problem(str(saved.get("code") or SAVE_FAILED), where + detail, file=rel)],
                   notes=[f"nothing was written to {rel}"])
