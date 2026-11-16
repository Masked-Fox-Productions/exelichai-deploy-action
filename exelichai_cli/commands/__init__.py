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
    transport: api.Transport = api.pooled_transport
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    poll_interval: float = POLL_INTERVAL
    #: The command ignores static tokens and signs in with OIDC only (``ci``,
    #: ``publish``): a token's game must never choose its game.
    prefer_oidc: bool = False
    _game_resolved: bool = field(default=False, repr=False)
    _client: Optional[api.ApiClient] = field(default=None, repr=False)

    @property
    def in_actions(self) -> bool:
        return self.env.get("GITHUB_ACTIONS") == "true"

    def require_game_id(self) -> str:
        """The game, resolved once per command. It follows the credential:
        ``--game-id`` / ``EXELICHAI_GAME_ID``, else the static token's own game
        (a token can only ever reach that one), else the manifest's
        ``game_id``. A command that ignores static tokens (:attr:`prefer_oidc`:
        ``ci``, ``publish``) never consults one: flag, else manifest. A
        manifest naming another game is warned about here, once, and its id is
        never sent."""
        if self._game_resolved:
            return self.game_id  # type: ignore[return-value]
        _rules, manifest_game = tree.manifest_rules(self.folder)
        token_game = None if self.prefer_oidc else api.static_token_game(self.env)
        chosen = self.game_id or token_game or manifest_game
        if not chosen:
            raise CliError([Problem("manifest-missing",
                                    "no game id: pass --game-id (or set EXELICHAI_GAME_ID)")])
        if manifest_game and manifest_game != chosen:
            self.reporter.warn(f"{tree.MANIFEST} names {manifest_game!r}; using {chosen!r} "
                               "(the connected game wins)")
        self.game_id, self._game_resolved = chosen, True
        return chosen

    def client(self) -> api.ApiClient:
        if self._client is None:
            origin = api.check_api_origin(self.api_url, in_actions=self.in_actions)
            self._client = api.ApiClient(origin, reporter=self.reporter,
                                         game_id=self.require_game_id(),
                                         transport=self.transport, in_actions=self.in_actions,
                                         clock=self.clock, sleep=self.sleep)
        return self._client

    def authenticate(self, *, prefer_oidc: Optional[bool] = None) -> api.Credential:
        """The command's credential. ``prefer_oidc`` (default: the context's)
        must be settled before the game is: the OIDC exchange is for the
        client's game."""
        mode = self.prefer_oidc if prefer_oidc is None else prefer_oidc
        if mode != self.prefer_oidc:
            if self._game_resolved:
                raise RuntimeError("the game was resolved before the command chose how it "
                                   "signs in; set Context.prefer_oidc first")
            self.prefer_oidc = mode
        client = self.client()
        if client.credential is None:
            client.credential = api.credential_from_env(client, self.env, prefer_oidc=mode)
        return client.credential


def git(folder: str, *args: str, strip: bool = True) -> Optional[str]:
    """``git -C folder args`` stdout (stripped unless ``strip=False``, for
    ``-z`` output), or ``None`` when git is missing, the folder is not a
    checkout, or the command exits non-zero."""
    try:
        done = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() if strip else done.stdout


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
