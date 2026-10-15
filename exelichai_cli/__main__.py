"""The ``exelichai`` command line.

Exit codes: 0 succeeded or skipped; 1 validation or the job failed; 2
conflicts held; 3 timeout (the job id is printed); 4 auth error; 5 quota or a
stale run; 6 publish refused (unsent edits or conflicts); 64 usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, IO, List, Mapping, Optional

from exelichai_cli import __version__, api, tree
from exelichai_cli.commands import Context, DEFAULT_TIMEOUT, repo_relative
from exelichai_cli.output import CliError, Reporter

EXIT_USAGE = 64

DESCRIPTION = """\
Sync an Exelichai game with a GitHub repository.

credentials:
  EXELICHAI_TOKEN   a game token (exlv_ validate, exla_ author) from the GitHub panel.
  Inside GitHub Actions the run signs in with OIDC instead (no token needed);
  publishing is OIDC-only.

exit codes:
  0 succeeded or skipped   1 failed   2 conflicts held   3 timeout
  4 auth error   5 quota or stale run   6 publish refused   64 usage
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors must not read as "conflicts held" (2)
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="exelichai", description=DESCRIPTION,
                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"exelichai {__version__}")
    p.add_argument("--api-url", help=f"API origin (env EXELICHAI_API_URL; default "
                                     f"{api.DEFAULT_API_URL})")
    p.add_argument("--game-id", help="the connected game (env EXELICHAI_GAME_ID; default: "
                                     "exelichai.yaml's game_id)")
    p.add_argument("--folder", help="the repo folder holding exelichai.yaml (env "
                                    "EXELICHAI_FOLDER; default .)")
    p.add_argument("--timeout", type=float, help=f"seconds to wait for a sync job (default "
                                                  f"{DEFAULT_TIMEOUT})")
    sub = p.add_subparsers(dest="command", metavar="command", parser_class=_Parser)
    sub.required = True

    s = sub.add_parser("init", help="write the game's initial export into an empty folder "
                                    "(author token, before initialisation)")
    s.add_argument("--force", action="store_true",
                   help="replace a folder that already holds a tree (shows the diff first)")
    sub.add_parser("connect", help="look up a repository's numeric ids with gh (the owner "
                                   "connects in the GitHub panel)").add_argument("repository",
                                                                                 metavar="owner/name")
    sub.add_parser("validate", help="validate the tree on the server (validate token)")
    for name, text in (("push", "sync the tree into the game's draft (author token)"),
                       ("publish", "publish the tree (GitHub Actions OIDC only)"),
                       ("ci", "what the Action runs: sign in with OIDC and do what the run may")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--allow-mass-retire", action="store_true",
                       help="allow a push that removes more than half the synced content")
    s = sub.add_parser("pull", help="bring the Art Studio presentation into presentation/ "
                                    "(author token)")
    s.add_argument("--force", action="store_true", help="overwrite uncommitted presentation/ edits")
    s.add_argument("--draft-branch", default=None,
                   help="the binding's draft branch (env EXELICHAI_DRAFT_BRANCH; default develop)")
    s = sub.add_parser("conflicts", help="list held conflicts, or resolve one (author token)")
    csub = s.add_subparsers(dest="action", metavar="action", parser_class=_Parser)
    r = csub.add_parser("resolve", help="resolve one conflict")
    r.add_argument("conflict_id")
    r.add_argument("--keep", required=True, choices=("repo", "mine"))
    s = sub.add_parser("status", help="show a sync job (any game credential)")
    s.add_argument("job_id")
    s.add_argument("--wait", action="store_true", help="wait for the job to finish")
    return p


def main(argv: Optional[List[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         transport: api.Transport = api.urllib_transport, out: Optional[IO[str]] = None,
         err: Optional[IO[str]] = None, cwd: Optional[str] = None,
         clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep, poll_interval: Optional[float] = None) -> int:
    env = dict(os.environ if env is None else env)
    args = build_parser().parse_args(argv)
    reporter = Reporter.from_env(env, out=out, err=err)
    in_actions = env.get("GITHUB_ACTIONS") == "true"
    workspace = (env.get("GITHUB_WORKSPACE") if in_actions else None) or cwd or os.getcwd()
    try:
        folder = tree.resolve_folder(workspace, args.folder or env.get("EXELICHAI_FOLDER") or ".")
        reporter.path_prefix = repo_relative(workspace, folder)
        ctx = Context(env=env, reporter=reporter, workspace=workspace, folder=folder,
                      api_url=args.api_url or env.get("EXELICHAI_API_URL") or api.DEFAULT_API_URL,
                      game_id=args.game_id or env.get("EXELICHAI_GAME_ID") or None,
                      timeout=args.timeout or DEFAULT_TIMEOUT, transport=transport,
                      clock=clock, sleep=sleep)
        if poll_interval is not None:
            ctx.poll_interval = poll_interval
        return _dispatch(ctx, args)
    except CliError as exc:
        reporter.problems(exc.problems)
        for note in exc.notes:
            reporter.info(note)
        reporter.summary(f"### exelichai {args.command}: failed")
        reporter.summary_problems(exc.problems)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    finally:
        reporter.flush_summary()


def _dispatch(ctx: Context, args: argparse.Namespace) -> int:
    from exelichai_cli.commands import conflicts, connect, export, status, sync

    cmd = args.command
    if cmd == "init":
        return export.init(ctx, force=args.force)
    if cmd == "pull":
        return export.pull(ctx, force=args.force,
                           draft_branch=args.draft_branch or ctx.env.get("EXELICHAI_DRAFT_BRANCH")
                           or export.DEFAULT_DRAFT_BRANCH)
    if cmd in ("validate", "push", "publish", "ci"):
        return sync.run(ctx, cmd, allow_mass_retire=getattr(args, "allow_mass_retire", False))
    if cmd == "conflicts":
        if args.action == "resolve":
            return conflicts.resolve(ctx, args.conflict_id, args.keep)
        return conflicts.list_conflicts(ctx)
    if cmd == "status":
        return status.status(ctx, args.job_id, wait=args.wait)
    if cmd == "connect":
        return connect.connect(ctx, args.repository)
    raise AssertionError(cmd)  # pragma: no cover


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
