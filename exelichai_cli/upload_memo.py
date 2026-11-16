"""The uploads a playtest may play again: a small per-user record of each
upload of a folder, so a playtest of an unchanged folder names the earlier
upload instead of uploading again.

Every upload spends calls from the game's hourly upload budget (the
manifest route's 600 calls an hour, shared with ``validate`` and ``push``),
and a playtest of an unchanged folder would upload the same bytes again.
The server lets a playtest name any upload session of its token's lane --
validate's, push's or an earlier playtest's, open or finalised -- until the
session expires (two hours). :func:`remember` records each upload (not a
``dev`` push's, which goes to the dev shadow); :func:`recall` finds one for
the same server, game, token and manifest no older than :data:`REUSE_SECONDS`.
A server that can no longer play it answers ``playtest-upload-stale`` (or
``blob-expired``); the playtest then forgets it and uploads afresh.

The record is a JSON file in the user's cache directory
(``EXELICHAI_CACHE_DIR``, else ``$XDG_CACHE_HOME/exelichai``, else
``~/.cache/exelichai``; ``%LOCALAPPDATA%\\exelichai`` on Windows), written
owner-only. It holds session ids and digests, never a token: the key is a
digest of the server, the game, a digest of the token and the manifest.
Nothing is recorded inside GitHub Actions, or when no cache directory can be
named. Any failure to read or write it only means an upload.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Mapping, Optional

from exelichai_cli import tree

FILE = "playtest-uploads.json"
#: The server's ``upload_session.SESSION_TTL`` is two hours from the first
#: page; reuse stops well short of it.
REUSE_SECONDS = 90 * 60
#: The most uploads remembered (the oldest go first).
MAX_ENTRIES = 32


def cache_dir(env: Mapping[str, str]) -> Optional[str]:
    if env.get("GITHUB_ACTIONS") == "true":
        return None
    if env.get("EXELICHAI_CACHE_DIR"):
        return env["EXELICHAI_CACHE_DIR"]
    if env.get("XDG_CACHE_HOME"):
        return os.path.join(env["XDG_CACHE_HOME"], "exelichai")
    if env.get("LOCALAPPDATA"):
        return os.path.join(env["LOCALAPPDATA"], "exelichai")
    if env.get("HOME"):
        return os.path.join(env["HOME"], ".cache", "exelichai")
    return None


def key(ctx, scanned: tree.Scan) -> Optional[str]:
    """The record's key for this upload: the server, the game, the token
    (by digest) and the manifest. None without a static token."""
    client = ctx.client()
    cred = client.credential
    if cred is None or getattr(cred, "kind", None) != "token":
        return None  # a CI session never plays a folder
    try:
        bearer = cred.bearer()
    except Exception:  # noqa: BLE001 - no token, no reuse
        return None
    doc = [ctx.api_url, ctx.require_game_id(), hashlib.sha256(bearer.encode()).hexdigest(),
           scanned.manifest()]
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _path(ctx) -> Optional[str]:
    d = cache_dir(ctx.env)
    return os.path.join(d, FILE) if d else None


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save(path: str, doc: Mapping[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _fresh(doc: Mapping[str, Any], now: float) -> Dict[str, Any]:
    live = {k: v for k, v in doc.items()
            if isinstance(v, dict) and now - float(v.get("at") or 0) < REUSE_SECONDS}
    newest = sorted(live, key=lambda k: float(live[k]["at"]), reverse=True)[:MAX_ENTRIES]
    return {k: live[k] for k in newest}


def recall(ctx, scanned: tree.Scan) -> Optional[Dict[str, Any]]:
    """The recorded upload of this folder, ``{session, manifest_sha,
    ignored, at}``, or None."""
    path, k = _path(ctx), key(ctx, scanned)
    if not path or not k:
        return None
    entry = _fresh(_load(path), ctx.clock()).get(k)
    if not entry or not isinstance(entry.get("session"), str) \
            or not isinstance(entry.get("manifest_sha"), str):
        return None
    return entry


def remember(ctx, scanned: tree.Scan, sent: Mapping[str, Any]) -> None:
    """Record the upload ``sent`` (``sync.upload_tree``'s answer)."""
    path, k = _path(ctx), key(ctx, scanned)
    if not path or not k or not sent.get("session") or not sent.get("manifest_sha"):
        return
    now = ctx.clock()
    doc = _fresh(_load(path), now)
    doc[k] = {"session": sent["session"], "manifest_sha": sent["manifest_sha"],
              "ignored": dict(sent.get("ignored") or {}), "at": now}
    _save(path, _fresh(doc, now))


def forget(ctx, scanned: tree.Scan) -> None:
    path, k = _path(ctx), key(ctx, scanned)
    if not path or not k:
        return
    doc = _load(path)
    if doc.pop(k, None) is not None:
        _save(path, doc)
