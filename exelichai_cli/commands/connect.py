"""``connect``: the connection itself is made by the game's owner in the
GitHub panel (it needs the owner's sign-in). This only looks up the numeric
repository and owner ids with ``gh`` -- the ids a private repository's
connection needs pasted. It never talks to the Exelichai API.
"""

from __future__ import annotations

import json
import re
import subprocess

from exelichai_cli.commands import Context
from exelichai_cli.output import CliError, EXIT_OK, Problem

_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


def connect(ctx: Context, repository: str) -> int:
    if not _NAME_RE.match(repository or ""):
        raise CliError([Problem("repo-name-invalid", f"{repository!r} is not owner/name")])
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
