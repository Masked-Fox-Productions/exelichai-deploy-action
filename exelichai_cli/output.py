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
EXIT_USAGE = 64             # usage error
EXIT_INTERRUPTED = 130      # interrupted

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

#: What each exit code means, as the guide's exit-code table says it: the
#: ``outcome`` of a structured result, so an agent need not know the numbers.
OUTCOMES = {
    EXIT_OK: "succeeded",
    EXIT_FAILED: "failed",
    EXIT_CONFLICTS: "conflicts held",
    EXIT_TIMEOUT: "timed out",
    EXIT_AUTH: "auth error",
    EXIT_QUOTA: "quota or stale run",
    EXIT_PUBLISH_REFUSED: "publish refused",
    EXIT_USAGE: "usage error",
    EXIT_INTERRUPTED: "interrupted",
}


def public(body: Mapping[str, Any]) -> Dict[str, Any]:
    """A server answer without the client's own ``_``-prefixed keys."""
    return {k: v for k, v in body.items() if not str(k).startswith("_")}


def outcome_for(exit_code: int) -> str:
    return OUTCOMES.get(exit_code, "failed")

GUIDE_URL_ENV = "EXELICHAI_GUIDE_URL"
#: Where the creator guide is served: the site's in-app ``/guides/github-sync``
#: page (one anchor per reason code). ``EXELICHAI_GUIDE_URL`` overrides it.
DEFAULT_GUIDE_URL = "https://exelichai.com/guides/github-sync"
#: Each stage's guide, by the API origin the command talks to: a run against
#: the dev API links the dev site's guide, never production's (Phase 1
#: finding 6). Any other origin (a local stack) gets the default.
GUIDE_URL_BY_API_ORIGIN = {
    "https://api.exelichai.com": DEFAULT_GUIDE_URL,
    "https://apidev.exelichai.com": "https://dev.exelichai.com/guides/github-sync",
}


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


def guide_base_for(api_url: Optional[str], env: Optional[Mapping[str, str]] = None) -> str:
    """The guide to link for a command talking to ``api_url``:
    ``EXELICHAI_GUIDE_URL`` if set, else that stage's site, else the default."""
    env = os.environ if env is None else env
    if env.get(GUIDE_URL_ENV):
        return env[GUIDE_URL_ENV]
    origin = (api_url or "").strip().rstrip("/").lower()
    return GUIDE_URL_BY_API_ORIGIN.get(origin, DEFAULT_GUIDE_URL)


def guide_url(code: str, base: Optional[str] = None) -> str:
    base = (base or os.environ.get(GUIDE_URL_ENV) or DEFAULT_GUIDE_URL).rstrip("/")
    anchor = catalogue().get(code, {}).get("anchor") or code
    return f"{base}#{anchor}"


#: What the CLI adds after a reason's catalogue sentence where the CLI itself
#: is the way through (the catalogue's text is lane-neutral: plan
#: 2026-10-21-002 Unit 1).
HINTS = {
    "mass-retire": "if the removal is intended, push again with --allow-mass-retire",
    "first-import-held": "to import it over the Studio's content, push again with "
                         "--allow-first-import (or choose Keep the Studio in the GitHub panel)",
}


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


def annotation(problem: Problem, *, path_prefix: str = "",
               guide_base: Optional[str] = None) -> str:
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
    if problem.code in HINTS:
        text = f"{text} Hint: {HINTS[problem.code]}."
    text = f"{text} {guide_url(problem.code, guide_base)}"
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
    #: The guide every printed link points into (:func:`guide_base_for`);
    #: ``None`` is the default guide.
    guide_base: Optional[str] = None
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
        # Flushed: `exelichai dev` runs for hours, and piped or logged its
        # progress lines would otherwise sit in the buffer (stderr already
        # flushes per line, so only the errors would show).
        print(text, file=self.out, flush=True)

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
        if problem.code in HINTS:
            print(f"  hint: {HINTS[problem.code]}", file=self.err)
        print(f"  guide: {guide_url(problem.code, self.guide_base)}", file=self.err)
        if self.actions:
            print(annotation(problem, path_prefix=self.path_prefix, guide_base=self.guide_base),
                  file=self.out)

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
                         f"([guide]({guide_url(p.code, self.guide_base)}))")

    # -- structured data (only a recording reporter keeps it) ----------------
    def report_job(self, job: Mapping[str, Any]) -> None:
        """The final job mapping (or the local report) a command ends on."""

    def report_data(self, kind: str, data: Mapping[str, Any]) -> None:
        """A server response a command printed, handed over raw as ``kind``."""

    def flush_summary(self) -> None:
        if not self.summary_lines:
            return
        if self.summary_path:
            with open(self.summary_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(self.summary_lines) + "\n")
        self.summary_lines = []


@dataclass
class RecordingReporter(Reporter):
    """The printing reporter's interface, collecting instead of printing:
    what ``--json`` prints and what an MCP tool returns. The job summary is
    still written in GitHub Actions, as the printing reporter writes it."""

    recorded_problems: List[Problem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)
    job: Optional[Dict[str, Any]] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def info(self, text: str = "") -> None:
        if text:
            self.lines.append(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def mask(self, value: Optional[str]) -> None:
        pass

    def problem(self, problem: Problem) -> None:
        self.recorded_problems.append(problem)

    def report_job(self, job: Mapping[str, Any]) -> None:
        self.job = public(job)

    def report_data(self, kind: str, data: Mapping[str, Any]) -> None:
        self.data[kind] = public(data)

    def problem_dict(self, p: Problem) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": p.code, "message": message_for(p), "detail": p.detail,
                               "file": p.file, "line": p.line,
                               "guide": guide_url(p.code, self.guide_base)}
        if p.code in HINTS:
            out["hint"] = HINTS[p.code]
        return out

    def result(self, exit_code: int) -> Dict[str, Any]:
        """``{exit_code, outcome, problems, warnings, info, job, data}``."""
        return {"exit_code": exit_code, "outcome": outcome_for(exit_code),
                "problems": [self.problem_dict(p) for p in self.recorded_problems],
                "warnings": list(self.warnings), "info": list(self.lines),
                "job": self.job, "data": dict(self.data)}


def _md(text: str) -> str:
    """Plain text safe inside a Markdown list item (no HTML, no links)."""
    return (text.replace("\\", "\\\\").replace("<", "&lt;").replace(">", "&gt;")
            .replace("[", "\\[").replace("]", "\\]").replace("\n", " "))
