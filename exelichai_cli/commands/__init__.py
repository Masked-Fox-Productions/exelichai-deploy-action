"""The ``exelichai`` commands. Each takes a :class:`Context` and returns an
exit code (or raises :class:`~exelichai_cli.output.CliError`)."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Mapping, Optional

from exelichai_cli import api, tree
from exelichai_cli.output import CliError, Problem, Reporter

DEFAULT_TIMEOUT = 1200      # seconds a command waits for its sync job
POLL_INTERVAL = 3.0


@dataclass
class Context:
    env: Mapping[str, str]
    reporter: Reporter
    workspace: str
    folder: str                       # absolute, confined to the workspace
    api_url: str
    game_id: Optional[str]
    timeout: float = DEFAULT_TIMEOUT
    transport: api.Transport = api.urllib_transport
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    poll_interval: float = POLL_INTERVAL
    _client: Optional[api.ApiClient] = field(default=None, repr=False)

    @property
    def in_actions(self) -> bool:
        return self.env.get("GITHUB_ACTIONS") == "true"

    def require_game_id(self) -> str:
        """The game: ``--game-id`` / ``EXELICHAI_GAME_ID``, else the manifest's
        ``game_id``, else the static token's own game."""
        if self.game_id:
            return self.game_id
        _rules, manifest_game = tree.manifest_rules(self.folder)
        token = (self.env.get(api.TOKEN_ENV) or "").strip()
        token_game = api._token_game(token) if token[:5] in api.STATIC_PREFIX_SCOPE else None
        self.game_id = manifest_game or token_game
        if not self.game_id:
            raise CliError([Problem("manifest-missing",
                                    "no game id: pass --game-id (or set EXELICHAI_GAME_ID)")])
        return self.game_id

    def client(self) -> api.ApiClient:
        if self._client is None:
            origin = api.check_api_origin(self.api_url, in_actions=self.in_actions)
            self._client = api.ApiClient(origin, reporter=self.reporter,
                                         game_id=self.require_game_id(),
                                         transport=self.transport, in_actions=self.in_actions,
                                         clock=self.clock, sleep=self.sleep)
        return self._client

    def authenticate(self, *, prefer_oidc: bool = False) -> api.Credential:
        client = self.client()
        if client.credential is None:
            client.credential = api.credential_from_env(client, self.env, prefer_oidc=prefer_oidc)
        return client.credential


def git(folder: str, *args: str) -> Optional[str]:
    """``git -C folder args`` stdout (stripped), or ``None`` when git is
    missing or the folder is not a checkout."""
    try:
        done = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def list_paths(reporter: Reporter, label: str, paths: List[str], *, limit: int = 50) -> None:
    if not paths:
        return
    reporter.info(f"  {label} ({len(paths)}):")
    for p in paths[:limit]:
        reporter.info(f"    {p}")
    if len(paths) > limit:
        reporter.info(f"    ... and {len(paths) - limit} more")


def repo_relative(workspace: str, folder: str) -> str:
    """The folder as a repository-relative prefix for annotations (``""`` at
    the root)."""
    rel = os.path.relpath(folder, workspace).replace(os.sep, "/")
    return "" if rel == "." else rel.rstrip("/") + "/"
