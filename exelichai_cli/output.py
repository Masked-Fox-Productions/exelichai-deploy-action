"""The output contract: exit codes, problems, and how they are shown.

* A human summary on stdout/stderr; colour only on a TTY (and never with
  ``NO_COLOR``), plain text otherwise.
* Inside GitHub Actions (``GITHUB_ACTIONS=true``): every problem is also an
  ``::error file=,line=::`` annotation (paths made repository-relative), minted
  secrets are masked with ``::add-mask::`` before anything could print them,
  and a Markdown job summary is appended to ``$GITHUB_STEP_SUMMARY``.
* Every problem's text comes from the shared reason catalogue (vendored as
  ``reason_codes.json``), with a link into the creator guide.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, IO, Iterable, List, Mapping, Optional

# --- Exit codes (plan 2026-10-05-001 Unit 15) ------------------------------

EXIT_OK = 0                 # succeeded or skipped
EXIT_FAILED = 1             # validation failed or the job failed
EXIT_CONFLICTS = 2          # conflicts held
EXIT_TIMEOUT = 3            # timeout (the job id is printed)
EXIT_AUTH = 4               # auth error
EXIT_QUOTA = 5              # quota or stale run
EXIT_PUBLISH_REFUSED = 6    # publish refused (unsent edits or conflicts)

#: Codes that are an authentication / authorisation problem (exit 4).
AUTH_CODES = frozenset({
    "credential-missing", "credential-invalid", "credential-expired", "credential-scope",
    "credential-wrong-game", "credential-required", "credentials-unavailable",
    "oidc-not-granted", "oidc-invalid", "oidc-expired", "oidc-key-unknown",
    "oidc-unavailable", "oidc-workflow-mismatch", "exchange-invalid",
    "environment-required", "publish-oidc-only", "api-origin-refused",
    "binding-displaced", "binding-epoch-stale", "binding-owner-changed",
    "binding-repo-mismatch", "binding-unverified", "not-connected", "not-owner",
    "sync-disabled", "lane-mismatch", "branch-mismatch", "sha-mismatch",
})
#: Quota, rate and run-order refusals (exit 5).
QUOTA_CODES = frozenset({
    "sync-quota", "upload-quota", "sync-concurrency", "credential-rate-limited", "run-stale",
    "run-superseded",
})
#: What refuses a publish because the Studio holds edits (exit 6).
PUBLISH_REFUSED_CODES = frozenset({"unsent-edits", "conflicts-held"})

GUIDE_URL_ENV = "EXELICHAI_GUIDE_URL"
#: Where the creator guide is served: the site's in-app ``/guides/github-sync``
#: page (one anchor per reason code). ``EXELICHAI_GUIDE_URL`` overrides it.
DEFAULT_GUIDE_URL = "https://exelichai.com/guides/github-sync"


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------

@dataclass
class Problem:
    """One reason, from the server (a refusal or a job reason) or local."""

    code: str
    detail: str = ""
    file: Optional[str] = None
    line: Optional[int] = None
    message: Optional[str] = None   # the server's catalogue sentence, if it sent one

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Problem":
        line = raw.get("line")
        return cls(code=str(raw.get("code") or "api-unreachable"),
                   detail=str(raw.get("detail") or ""),
                   file=raw.get("file") if isinstance(raw.get("file"), str) else None,
                   line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                   message=raw.get("message") if isinstance(raw.get("message"), str) else None)


class CliError(Exception):
    """Stop with ``problems`` and ``exit_code``."""

    def __init__(self, problems: Iterable[Problem], exit_code: int = EXIT_FAILED,
                 *, notes: Iterable[str] = ()):
        self.problems: List[Problem] = list(problems)
        self.exit_code = exit_code
        self.notes = list(notes)
        super().__init__("; ".join(p.code for p in self.problems))


def exit_code_for(codes: Iterable[str], *, default: int = EXIT_FAILED) -> int:
    """The exit code a set of refusal codes means (auth > quota > default)."""
    codes = set(codes)
    if codes & AUTH_CODES:
        return EXIT_AUTH
    if codes & QUOTA_CODES:
        return EXIT_QUOTA
    return default


# ---------------------------------------------------------------------------
# The reason catalogue
# ---------------------------------------------------------------------------

_CATALOGUE: Optional[Dict[str, Dict[str, str]]] = None


def catalogue() -> Dict[str, Dict[str, str]]:
    """``{code: {message, anchor}}`` from the vendored export."""
    global _CATALOGUE
    if _CATALOGUE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reason_codes.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        _CATALOGUE = {c["code"]: c for c in doc["codes"]}
    return _CATALOGUE


def guide_url(code: str) -> str:
    base = (os.environ.get(GUIDE_URL_ENV) or DEFAULT_GUIDE_URL).rstrip("/")
    anchor = catalogue().get(code, {}).get("anchor") or code
    return f"{base}#{anchor}"


def message_for(problem: Problem) -> str:
    entry = catalogue().get(problem.code)
    if entry:
        return entry["message"]
    return problem.message or f"Unrecognised reason `{problem.code}` (a newer server?)."


# ---------------------------------------------------------------------------
# GitHub workflow commands
# ---------------------------------------------------------------------------

def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def annotation(problem: Problem, *, path_prefix: str = "") -> str:
    """``::error file=<repo path>,line=<n>,title=<code>::<message>``."""
    props = []
    if problem.file:
        props.append(f"file={_escape_property(path_prefix + problem.file)}")
        if problem.line:
            props.append(f"line={problem.line}")
    props.append(f"title={_escape_property(problem.code)}")
    text = message_for(problem)
    if problem.detail:
        text = f"{text} ({problem.detail})"
    text = f"{text} {guide_url(problem.code)}"
    return f"::error {','.join(props)}::{_escape_data(text)}"


# ---------------------------------------------------------------------------
# The reporter
# ---------------------------------------------------------------------------

@dataclass
class Reporter:
    """Where everything a command says goes."""

    out: IO[str] = field(default_factory=lambda: sys.stdout)
    err: IO[str] = field(default_factory=lambda: sys.stderr)
    actions: bool = False
    summary_path: Optional[str] = None
    color: bool = False
    #: Prepended to tree paths in annotations (the folder, repo-relative).
    path_prefix: str = ""
    summary_lines: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *,
                 out: Optional[IO[str]] = None, err: Optional[IO[str]] = None) -> "Reporter":
        env = os.environ if env is None else env
        out = out or sys.stdout
        err = err or sys.stderr
        actions = env.get("GITHUB_ACTIONS") == "true"
        tty = bool(getattr(out, "isatty", lambda: False)())
        return cls(out=out, err=err, actions=actions,
                   summary_path=env.get("GITHUB_STEP_SUMMARY") if actions else None,
                   color=tty and not env.get("NO_COLOR") and not actions)

    # -- plain lines ---------------------------------------------------------
    def _paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def info(self, text: str = "") -> None:
        print(text, file=self.out)

    def warn(self, text: str) -> None:
        if self.actions:
            print(f"::warning::{_escape_data(text)}", file=self.out)
        else:
            print(f"{self._paint('warning', '33')}: {text}", file=self.err)

    def mask(self, value: Optional[str]) -> None:
        """Register a minted secret with the runner's log masker. Called
        BEFORE the value is used anywhere it could be echoed."""
        if self.actions and value:
            print(f"::add-mask::{value}", file=self.out)

    # -- problems ------------------------------------------------------------
    def problem(self, problem: Problem) -> None:
        where = ""
        if problem.file:
            where = f" {self.path_prefix}{problem.file}"
            if problem.line:
                where += f":{problem.line}"
        print(f"{self._paint('error', '1;31')}[{problem.code}]{where}: {message_for(problem)}",
              file=self.err)
        if problem.detail:
            print(f"  {problem.detail}", file=self.err)
        print(f"  guide: {guide_url(problem.code)}", file=self.err)
        if self.actions:
            print(annotation(problem, path_prefix=self.path_prefix), file=self.out)

    def problems(self, problems: Iterable[Problem]) -> None:
        for p in problems:
            self.problem(p)

    # -- the job summary -----------------------------------------------------
    def summary(self, line: str = "") -> None:
        self.summary_lines.append(line)

    def summary_problems(self, problems: Iterable[Problem]) -> None:
        for p in problems:
            where = ""
            if p.file:
                where = f" `{self.path_prefix}{p.file}{':' + str(p.line) if p.line else ''}`"
            detail = f" — {_md(p.detail)}" if p.detail else ""
            self.summary(f"- **{p.code}**{where}: {_md(message_for(p))}{detail} "
                         f"([guide]({guide_url(p.code)}))")

    def flush_summary(self) -> None:
        if not self.summary_lines:
            return
        if self.summary_path:
            with open(self.summary_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(self.summary_lines) + "\n")
        self.summary_lines = []


def _md(text: str) -> str:
    """Plain text safe inside a Markdown list item (no HTML, no links)."""
    return (text.replace("\\", "\\\\").replace("<", "&lt;").replace(">", "&gt;")
            .replace("[", "\\[").replace("]", "\\]").replace("\n", " "))
