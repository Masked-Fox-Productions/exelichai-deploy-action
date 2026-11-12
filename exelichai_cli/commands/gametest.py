"""``test``: run the game's own tests on the server, and the ``tests/`` reader
the sync commands share (plan 2026-10-26-002 Unit 9b).

A game's tests are ``tests/*.yaml`` (or ``.yml``) directly under the folder
that holds ``exelichai.yaml``. They are not part of the tree -- never
uploaded, imported or published -- so they travel inline: in the finalise
body (the publish lane runs them before it publishes) and in ``exelichai
test``'s request (which runs them now and writes nothing).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Mapping, Optional

from exelichai_cli.commands import Context, plural
from exelichai_cli.output import CliError, EXIT_FAILED, EXIT_OK, Problem, _md

TESTS_DIR = "tests"
#: The server's ``game_test_files`` caps.
MAX_FILES = 100
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 512 * 1024
_NAME_RE = re.compile(r"^[^/.][^/]*\.ya?ml$")
INVALID = "game-tests-invalid"


def read_tests(folder: str) -> Dict[str, str]:
    """``{"tests/<name>.yaml": text}`` for every test file directly under the
    folder's ``tests/`` (hidden files and subfolders skipped); ``{}`` when
    there is none. Over a cap, or not UTF-8, raises ``game-tests-invalid``."""
    root = os.path.join(folder, TESTS_DIR)
    if not os.path.isdir(root) or os.path.islink(root):
        return {}
    out: Dict[str, str] = {}
    total = 0
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not _NAME_RE.fullmatch(name) or os.path.islink(full) or not os.path.isfile(full):
            continue
        rel = f"{TESTS_DIR}/{name}"
        size = os.path.getsize(full)
        if size > MAX_FILE_BYTES:
            raise CliError([Problem(INVALID, f"{size} bytes is over the {MAX_FILE_BYTES} byte "
                                             "limit for one test file", file=rel)])
        total += size
        if total > MAX_TOTAL_BYTES:
            raise CliError([Problem(INVALID, f"the test files hold over {MAX_TOTAL_BYTES} bytes",
                                    file=TESTS_DIR + "/")])
        with open(full, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        try:
            out[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise CliError([Problem(INVALID, "a test file is not UTF-8 text", file=rel)])
        if len(out) > MAX_FILES:
            raise CliError([Problem(INVALID, f"more than {MAX_FILES} test files",
                                    file=TESTS_DIR + "/")])
    return out


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(", ", ": "), default=str)


def print_report(ctx: Context, report: Mapping[str, Any]) -> None:
    """One line per test, and a failure's step, kind, message, condition and
    the value the condition found."""
    rep = ctx.reporter
    for r in report.get("results") or []:
        if r.get("passed"):
            rep.info(f"PASS {r.get('source')} {r.get('name')}")
            continue
        f = r.get("failure") or {}
        rep.info(f"FAIL {r.get('source')} {r.get('name')}")
        rep.info(f"  step {f.get('step')} ({f.get('kind')}): {f.get('message')}")
        if f.get("condition"):
            rep.info(f"  condition: {_compact(f['condition'])}")
        if "observed" in f and f.get("observed") is not None:
            rep.info(f"  observed: {_compact(f['observed'])}")
        for clause in f.get("clauses") or []:
            rep.info(f"  unmet: {_compact(clause)}")


def run(ctx: Context, *, version: Optional[str] = None) -> int:
    rep = ctx.reporter
    tests = read_tests(ctx.folder)
    if not tests:
        raise CliError([Problem(INVALID, "no tests: add `tests/<name>.yaml` beside "
                                         "exelichai.yaml", file=TESTS_DIR + "/")])
    game_id = ctx.require_game_id()
    ctx.authenticate()
    against = f"release {version}" if version else "the draft"
    rep.info(f"{game_id}: running {plural(len(tests), 'test')} against {against}")
    report = ctx.client().game_tests(tests, version=version)
    rep.report_data("tests", report)
    print_report(ctx, report)
    passed, failed = int(report.get("passed") or 0), int(report.get("failed") or 0)
    rep.info(f"{passed} passed, {failed} failed")
    rep.summary(f"### exelichai test: {passed} passed, {failed} failed")
    for r in report.get("results") or []:
        if not r.get("passed"):
            f = r.get("failure") or {}
            rep.summary(f"- FAIL `{r.get('source')}` step {f.get('step')} "
                        f"({f.get('kind')}): {_md(str(f.get('message') or ''))}")
    return EXIT_FAILED if failed else EXIT_OK
