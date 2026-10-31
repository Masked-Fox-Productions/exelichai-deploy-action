"""``exelichai dev``: save a file, see it in the browser in seconds, no commit
(plan 2026-10-18-002 Unit 6; Phase 2 R34, R35).

Watches the folder, and on every settled change pushes the tree into the
game's owner-only DEV SHADOW (an author token; ``"target": "dev"`` on the
manifest and finalise routes -- the server derives the shadow from the token's
game). The play tab the first push opens follows each save itself.

* **Settled**: a change is pushed once the tree area has stopped changing for
  :data:`DEBOUNCE` seconds, so an editor's save-as-rename or a burst of saves
  is one push.
* **Coalesced**: a push waits for its job; saves made meanwhile are seen on the
  next scan, and only the latest tree is pushed. There is never more than one
  dev job of ours in flight.
* **Unchanged** trees push nothing (the manifest digest is compared locally,
  and the server skips a manifest its dev record already holds).
* **Uploads** go through an upload session (plan 2026-10-26-002 Unit 3):
  manifest pages, then the candidate stage, each blob streamed from disk.
* **Known blobs** (Unit 9): a save whose every blob the server confirmed
  earlier this session (an undo, a revert) goes straight to finalise, with no
  manifest call (the legacy single-stage finalise, naming every file). A blob the server no longer holds -- ``blobs-missing`` on that
  finalise, or a job failing ``blob-expired`` -- costs ONE fall-back push
  through manifest -> upload -> finalise, never a loop.
* A failed save prints ``file:line: message`` and the watcher keeps running;
  the tab keeps the last good draft. A quota refusal prints and keeps
  watching.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Set

from exelichai_cli import api, tree
from exelichai_cli.commands import Context, plural
from exelichai_cli.commands import sync
from exelichai_cli.output import CliError, EXIT_OK, EXIT_TIMEOUT, Problem

#: Seconds the tree must hold still before a push.
DEBOUNCE = 0.3
#: Seconds between scans of the folder.
SCAN_INTERVAL = 0.25
#: How often a dev push's job is polled (``sync`` polls every 3 s).
DEV_POLL_INTERVAL = 0.5
#: The site a stage's API serves, for the play link.
SITE_BY_API_ORIGIN = {
    "https://api.exelichai.com": "https://exelichai.com",
    "https://apidev.exelichai.com": "https://dev.exelichai.com",
}
#: Refusals the watcher survives (it prints them and keeps watching).
SURVIVABLE = frozenset({"sync-quota", "upload-quota", "sync-concurrency", "blobs-missing"})


def play_link(api_url: str, shadow_id: str, game_id: str) -> str:
    origin = (api_url or "").strip().rstrip("/").lower()
    site = SITE_BY_API_ORIGIN.get(origin, "https://exelichai.com")
    return f"{site}/play?game_id={shadow_id}&dev={game_id}"


def _digest(manifest: List[Mapping[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def _codes(reasons: Any) -> Set[str]:
    return {str(r.get("code")) for r in (reasons or []) if isinstance(r, Mapping)}


def push_once(ctx: Context, scanned: tree.Scan, *, verbose: bool = False,
              confirmed: Optional[Set[str]] = None) -> Dict[str, Any]:
    """One dev push of the scanned tree: an upload session (pages, the
    candidate stage) -> finalise -> follow. ``confirmed`` is the set of shas
    the server confirmed this session (updated in place): when it covers the
    whole manifest, the push starts at finalise (the legacy single-stage
    finalise, which names every file). Returns ``{shadow_id, skipped, exit,
    elapsed, legs}``."""
    rep = ctx.reporter
    client = ctx.client()
    start = ctx.clock()
    legs: Dict[str, float] = {}
    known: Set[str] = confirmed if confirmed is not None else set()

    def leg(name: str, since: float) -> float:
        now = ctx.clock()
        legs[name] = round(now - since, 3)
        return now

    manifest = scanned.manifest()
    shas = {str(f["sha256"]) for f in manifest}
    direct = bool(shas) and shas <= known
    fell_back = False
    uploaded = 0
    mark = start
    out: Dict[str, Any] = {"legs": legs}
    while True:
        sent: Mapping[str, Any] = {}
        try:
            if direct:
                done = client.finalise({"files": manifest, "target": "dev"})
            else:
                sent = sync.upload_tree(ctx, scanned, target="dev")
                uploaded += int(sent["uploaded"])
                mark = leg("upload", mark)
                done = client.finalise({"protocol": api.UPLOAD_PROTOCOL, "target": "dev",
                                        "session": sent["session"],
                                        "manifest_sha": sent["manifest_sha"]})
        except CliError as exc:
            if direct and not fell_back and "blobs-missing" in {p.code for p in exc.problems}:
                # The server no longer holds a blob it confirmed: the one
                # fall-back push, through the manifest.
                known.clear()
                direct, fell_back = False, True
                continue
            raise
        mark = leg("finalise", mark)
        known |= shas if direct else set(sent.get("confirmed") or ())
        out["shadow_id"] = str((done.get("dev") or sent.get("dev") or {}).get("shadow_id")
                               or out.get("shadow_id") or "")
        out["skipped"] = bool(done.get("skipped"))
        if done.get("skipped"):
            out["exit"] = EXIT_OK
            break
        saved = ctx.poll_interval
        ctx.poll_interval = DEV_POLL_INTERVAL
        try:
            final = sync.await_job(ctx, done.get("job") or {})
        finally:
            ctx.poll_interval = saved
        mark = leg("job", mark)
        if final is None:
            out["exit"] = EXIT_TIMEOUT
            break
        if (not fell_back and final.get("state") == "failed"
                and "blob-expired" in _codes(final.get("reasons"))):
            rep.info("a stored blob has expired; uploading the tree again")
            known.clear()
            direct, fell_back = False, True
            continue
        out["exit"] = sync.report_job(ctx, final, lane="dev")
        break
    out["uploaded"] = uploaded
    out["elapsed"] = round(ctx.clock() - start, 2)
    if verbose:
        rep.info("  " + " ".join(f"{k}={v}s" for k, v in legs.items()))
    return out


def run(ctx: Context, *, once: bool = False, verbose: bool = False, open_browser: bool = True,
        max_pushes: Optional[int] = None, opener=None) -> int:
    """Watch and push until interrupted (``once``: push the tree once and
    return; ``max_pushes`` bounds the loop, for tests)."""
    rep = ctx.reporter
    if tree.MANIFEST not in tree.scan(ctx.folder).entries:
        raise CliError([Problem("manifest-missing", f"no {tree.MANIFEST} in the folder; "
                                "run `exelichai init` first")])
    game_id = ctx.require_game_id()
    sync._credential_for(ctx, "push")  # an author token: the dev lane writes a draft
    rep.info(f"{game_id}: watching {ctx.folder} -- save a file to see it in your browser "
             "(Ctrl-C to stop)")
    last_pushed: Optional[str] = None
    confirmed: Set[str] = set()  # the shas the server confirmed this session
    linked = False
    pushes = 0
    seen_sig = None
    exit_code = EXIT_OK
    while True:
        sig = tree.tree_signature(ctx.folder)
        if seen_sig is not None and sig == seen_sig:
            ctx.sleep(SCAN_INTERVAL)
            continue
        if seen_sig is not None:
            # Changed: wait until it holds still for DEBOUNCE.
            settled = sig
            while True:
                ctx.sleep(DEBOUNCE)
                again = tree.tree_signature(ctx.folder)
                if again == settled:
                    break
                settled = again
            sig = settled
        seen_sig = sig
        try:
            scanned = tree.scan(ctx.folder)
        except CliError as exc:
            rep.problems(exc.problems)
            if once:
                return exc.exit_code
            continue
        digest = _digest(scanned.manifest())
        if digest == last_pushed:
            continue
        try:
            out = push_once(ctx, scanned, verbose=verbose, confirmed=confirmed)
        except CliError as exc:
            rep.problems(exc.problems)
            if once or not {p.code for p in exc.problems} & SURVIVABLE:
                return exc.exit_code
            continue  # a quota or a lost race: the next save retries
        last_pushed = digest
        pushes += 1
        if out["shadow_id"] and not linked:
            link = play_link(ctx.api_url, out["shadow_id"], game_id)
            rep.info(f"play your dev draft: {link}")
            linked = True
            if open_browser and not once:
                try:
                    (opener or _open)(link)
                except Exception:  # noqa: BLE001 -- a missing browser is not an error
                    pass
        if out["skipped"]:
            rep.info("nothing changed")
        elif out["exit"] == EXIT_OK:
            rep.info(f"✓ {out['elapsed']}s ({plural(out['uploaded'], 'file')} uploaded)")
        exit_code = out["exit"]
        if once or (max_pushes is not None and pushes >= max_pushes):
            return exit_code


def _open(link: str) -> None:
    import webbrowser

    webbrowser.open(link, new=2)
