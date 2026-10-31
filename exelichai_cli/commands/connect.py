"""``connect``: the connection itself is made by the game's owner in the
GitHub panel (it needs the owner's sign-in). This only looks up the numeric
repository and owner ids with ``gh`` -- the ids a private repository's
connection needs pasted. It never talks to the Exelichai API.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse

from exelichai_cli.commands import Context
from exelichai_cli.output import CliError, EXIT_OK, Problem

# A mirror of the server's ``connection.normalize_repository`` (plan
# 2026-10-26-001 Unit 1, KTD-7), so a pasted link or SSH remote looks up the
# repository it names. Feedback only: the server re-parses what the panel sends.
# ``shared/repo_sync/repository_spellings.json`` is the table both are tested on.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_URL_SCHEMES = frozenset({"https", "http", "ssh"})
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://(.*)", re.S)
_SCP_RE = re.compile(r"([^@/:]+)@([^@/:]+):(.*)", re.S)
_HOSTLIKE_RE = re.compile(r"[@:]|[A-Za-z0-9]\.[A-Za-z0-9]")
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_SPACE = ("\t\n\x0b\x0c\r \u00a0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200B))
          + "\u2028\u2029\u202f\u205f\u3000\ufeff")


class _Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _github_host(host: str) -> None:
    if not host.isascii() or host.lower() not in _GITHUB_HOSTS:
        raise _Refused("repo-host-unsupported")


def _owner_and_name(owner: str, name: str, *, decode: bool) -> str:
    if decode:
        owner, name = urllib.parse.unquote(owner), urllib.parse.unquote(name)
    if name.endswith(".git"):
        name = name[:-4]
    if (not _OWNER_RE.fullmatch(owner) or not _REPO_NAME_RE.fullmatch(name)
            or name in (".", "..")):
        raise _Refused("repo-name-invalid")
    return f"{owner}/{name}"


def _normalize(value: str) -> str:
    raw = value.strip(_SPACE)
    if not raw or len(raw) > 1000 or any(
            ch in _SPACE or ord(ch) < 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in raw):
        raise _Refused("repo-name-invalid")
    scheme_match = _SCHEME_RE.fullmatch(raw)
    if scheme_match:
        scheme, rest = scheme_match.group(1).lower(), scheme_match.group(2)
    else:
        scp = _SCP_RE.fullmatch(raw)
        if scp:
            user, host, path = scp.groups()
            _github_host(host)
            parts = path.split("/")
            if user != "git" or len(parts) != 2:
                raise _Refused("repo-name-invalid")
            return _owner_and_name(parts[0], parts[1], decode=True)
        if not _HOSTLIKE_RE.search(raw.split("/", 1)[0]):
            parts = raw.split("/")          # bare owner/name
            if len(parts) != 2:
                raise _Refused("repo-name-invalid")
            return _owner_and_name(parts[0], parts[1], decode=False)
        scheme, rest = "https", raw
    end = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
    authority, path = rest[:end], rest[end:]
    userinfo, _, hostport = authority.rpartition("@")
    host, colon, _ = hostport.partition(":")
    _github_host(host)
    if (scheme not in _URL_SCHEMES or colon
            or ("@" in authority and not (scheme == "ssh" and userinfo == "git"))):
        raise _Refused("repo-name-invalid")
    for mark in "?#":
        path = path.split(mark, 1)[0]
    parts = path.split("/")[1:]
    if len(parts) < 2:
        raise _Refused("repo-name-invalid")
    return _owner_and_name(parts[0], parts[1], decode=True)


def normalize_repository(value: str) -> str:
    """Any common spelling of a GitHub repository -> ``owner/name``; a
    :class:`CliError` (``repo-host-unsupported`` / ``repo-name-invalid``)
    otherwise."""
    try:
        return _normalize(value or "")
    except _Refused as exc:
        detail = ("is not a github.com repository" if exc.code == "repo-host-unsupported"
                  else "is not owner/name or a GitHub link")
        raise CliError([Problem(exc.code, f"{value!r} {detail}")])


def connect(ctx: Context, repository: str) -> int:
    repository = normalize_repository(repository)
    try:
        done = subprocess.run(["gh", "api", f"repos/{repository}"], capture_output=True,
                              text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError([Problem("gh-unavailable", str(exc))])
    if done.returncode != 0:
        raise CliError([Problem("gh-unavailable", (done.stderr or "").strip()[:300])])
    try:
        repo = json.loads(done.stdout)
        repo_id, owner_id = int(repo["id"]), int(repo["owner"]["id"])
    except (ValueError, KeyError, TypeError):
        raise CliError([Problem("gh-unavailable", "gh returned an unexpected answer")])
    rep = ctx.reporter
    rep.info(f"repository: {repo.get('full_name') or repository}"
             f"{' (private)' if repo.get('private') else ''}")
    rep.info(f"  repository id: {repo_id}")
    rep.info(f"  owner id:      {owner_id}")
    rep.info("Connect the game in its GitHub panel (Art Studio -> the game -> GitHub); "
             "paste these ids there for a private repository.")
    return EXIT_OK
