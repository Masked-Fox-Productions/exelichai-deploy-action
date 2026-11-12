"""Talking to the Exelichai API: the origin allowlist, credentials (a static
game token or a GitHub Actions OIDC session), and the sync routes.

Hardening, in one place:

* **Origin allowlist.** Credentials are only ever sent to an allowlisted API
  origin (:data:`ALLOWED_API_ORIGINS`); a loopback origin (a local stack) is
  accepted only OUTSIDE GitHub Actions. The URL must be a bare origin.
* **No redirects** are followed for API calls: a redirect could carry the
  ``Authorization`` header somewhere else.
* **Presigned URLs** (uploads, the export archive) get NO credential, and must
  be HTTPS on an S3 host (or loopback when a local stack is allowed).
* **Minted values are masked** (``::add-mask::``) the moment they exist: the
  GitHub OIDC token and the session credential it is exchanged for.
* **OIDC sessions are re-exchanged** when they expire mid-poll (proactively a
  minute early, and once on a ``credential-expired`` answer).
* **Static tokens never ride argv:** they come from ``EXELICHAI_TOKEN``.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, Iterable, List, Mapping, Optional, TypeVar, Union

from exelichai_cli import __version__
from exelichai_cli.output import (CliError, EXIT_AUTH, EXIT_FAILED, Problem, Reporter,
                                  exit_code_for)

#: The stages' API origins (``serverless.yml`` ``custom.domain``). The OIDC
#: audience is the origin itself (the server's ``API_BASE_URL``).
ALLOWED_API_ORIGINS = ("https://api.exelichai.com", "https://apidev.exelichai.com")
DEFAULT_API_URL = "https://api.exelichai.com"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

TOKEN_ENV = "EXELICHAI_TOKEN"
STATIC_PREFIX_SCOPE = {"exlv_": "validate", "exla_": "author"}
SESSION_PREFIX = "exls_"
LANE_BY_SCOPE = {"validate": "validate", "author": "draft", "publish": "publish"}

OIDC_ROUTE = "/auth/github-oidc"
REFRESH_MARGIN = 60          # re-exchange a session this many seconds before it expires
REQUEST_TIMEOUT = 60
RETRY_STATUSES = frozenset({502, 503, 504})
RETRIES = 3
MAX_JSON_BYTES = 8 * 1024 * 1024
#: The upload protocol this CLI speaks (the server's
#: ``upload_session.PROTOCOL``): a paged, two-stage upload session.
UPLOAD_PROTOCOL = 2


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------

def _is_loopback(host: Optional[str]) -> bool:
    return (host or "").lower().strip("[]") in LOOPBACK_HOSTS


def check_api_origin(url: str, *, in_actions: bool) -> str:
    """The normalised origin of ``url``, or :class:`CliError`
    ``api-origin-refused``."""
    refused = lambda why: CliError([Problem("api-origin-refused", why)], EXIT_AUTH)  # noqa: E731
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        raise refused(f"{url!r} is not a URL")
    if parts.username or parts.password or parts.query or parts.fragment \
            or parts.path not in ("", "/"):
        raise refused(f"{url!r} must be a bare origin (scheme://host[:port])")
    host = (parts.hostname or "").lower()
    scheme = parts.scheme.lower()
    if _is_loopback(host):
        if in_actions:
            raise refused("a loopback API is a local stack; it is refused inside GitHub Actions")
        if scheme not in ("http", "https"):
            raise refused(f"{url!r} is not http(s)")
        netloc = f"[{host}]" if ":" in host else host
        return f"{scheme}://{netloc}{f':{port}' if port else ''}"
    origin = f"{scheme}://{host}{f':{port}' if port and port != 443 else ''}"
    if origin not in ALLOWED_API_ORIGINS:
        raise refused(f"{origin} is not one of {', '.join(ALLOWED_API_ORIGINS)}")
    return origin


def check_presigned(url: Any, *, allow_loopback: bool) -> str:
    """A presigned URL the API handed out: HTTPS on S3, or loopback locally."""
    bad = CliError([Problem("api-unreachable", f"refusing presigned URL {str(url)[:80]!r}")],
                   EXIT_FAILED)
    if not isinstance(url, str):
        raise bad
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise bad
    host = (parts.hostname or "").lower()
    if parts.username or parts.password:
        raise bad
    if _is_loopback(host):
        if not allow_loopback:
            raise bad
        return url
    if parts.scheme != "https" or not (host == "amazonaws.com" or host.endswith(".amazonaws.com")):
        raise bad
    return url


#: Each stage's media host, keyed by the API origin (plan 2026-10-26-002 KTD-8,
#: KTD-9; the backend's ``custom.mediaHost``). A manifest's media URL must be
#: ``https`` on EXACTLY this host, never a suffix match: dev and prod share the
#: registrable domain ``exelichai-media.com``. The old assets hosts
#: (``assets(dev).exelichai.com``) are deliberately absent: the server composes
#: every manifest URL on the media host, and the old host only redirects, which
#: the CLI never follows.
MEDIA_HOST_BY_API_ORIGIN = {
    "https://api.exelichai.com": "media.exelichai-media.com",
    "https://apidev.exelichai.com": "media.dev.exelichai-media.com",
}


def check_media_url(url: Any, *, api_origin: str) -> str:
    """A manifest media URL: ``https`` on the API origin's media host exactly
    (default port, no userinfo), or :class:`CliError` ``archive-invalid``."""
    bad = CliError([Problem("archive-invalid", f"refusing media URL {str(url)[:80]!r}")],
                   EXIT_FAILED)
    expected = MEDIA_HOST_BY_API_ORIGIN.get(api_origin)
    if not expected or not isinstance(url, str):
        raise bad
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise bad
    if parts.scheme != "https" or parts.username or parts.password \
            or port not in (None, 443) or parts.hostname != expected:
        raise bad
    return url


# ---------------------------------------------------------------------------
# Transport (replaceable in tests)
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # surface the 3xx as an HTTPError instead of following it


_OPENER = urllib.request.build_opener(_NoRedirect)


#: A request body: bytes, or a binary file object read as it is sent.
Body = Union[bytes, BinaryIO]


@dataclass
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]


def urllib_transport(method: str, url: str, headers: Mapping[str, str],
                     body: Optional[Body], timeout: float) -> Response:
    """``body`` is bytes, or a binary file object streamed as it is read (an
    upload of a media file; its ``Content-Length`` header is set)."""
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read() or b"", dict(exc.headers or {}))


class _Connections(threading.local):
    """Each thread's open connections, by ``(scheme, host, port)``."""

    def __init__(self) -> None:
        self.open: Dict[tuple, http.client.HTTPConnection] = {}


_CONNECTIONS = _Connections()
_TLS: Optional[ssl.SSLContext] = None


def _tls() -> ssl.SSLContext:
    global _TLS
    if _TLS is None:
        _TLS = ssl.create_default_context()   # what urllib verifies with
    return _TLS


def pooled_transport(method: str, url: str, headers: Mapping[str, str],
                     body: Optional[Body], timeout: float) -> Response:
    """:func:`urllib_transport` over a kept-alive connection per thread and
    host, so a run of presigned PUTs pays one TLS handshake, not one each.
    ``http.client`` follows no redirect (a 3xx is answered as itself) and
    verifies TLS as urllib does. A proxied host (``https_proxy`` and the
    like) goes through urllib, which honours the proxy. A kept-alive
    connection the far end has meanwhile closed is reopened once; any other
    connection error is the caller's to retry (:meth:`ApiClient.raw`)."""
    parts = urllib.parse.urlsplit(url)
    scheme, host = parts.scheme, parts.hostname or ""
    if scheme not in ("http", "https") or (urllib.request.getproxies().get(scheme)
                                           and not urllib.request.proxy_bypass(host)):
        return urllib_transport(method, url, headers, body, timeout)
    key = (scheme, host, parts.port)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    for attempt in (0, 1):
        conn = _CONNECTIONS.open.get(key)
        reused = conn is not None
        if conn is None:
            conn = (http.client.HTTPSConnection(host, parts.port, timeout=timeout,
                                                context=_tls()) if scheme == "https"
                    else http.client.HTTPConnection(host, parts.port, timeout=timeout))
            _CONNECTIONS.open[key] = conn
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        try:
            if attempt and body is not None and hasattr(body, "seek"):
                body.seek(0)  # type: ignore[union-attr]
            conn.request(method, target, body=body, headers=dict(headers))
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            _CONNECTIONS.open.pop(key, None)
            if reused and attempt == 0 and isinstance(
                    exc, (ConnectionError, http.client.RemoteDisconnected,
                          http.client.BadStatusLine)):
                continue   # an idle connection closed by the far end
            if isinstance(exc, OSError):
                raise
            raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if resp.will_close:
            conn.close()
            _CONNECTIONS.open.pop(key, None)
        return Response(resp.status, data, dict(resp.headers))
    raise AssertionError("unreachable")  # pragma: no cover


Transport = Callable[[str, str, Mapping[str, str], Optional[Body], float], Response]


@dataclass
class Stream:
    """A GET answer whose body is read as it arrives (``body.read(n)``)."""
    status: int
    headers: Mapping[str, str]
    body: Any


def urllib_stream(url: str, timeout: float) -> Stream:
    """A streaming GET with no credential; a redirect is never followed (it
    surfaces as its 3xx status)."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": f"exelichai-cli/{__version__}"})
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return Stream(exc.code, dict(exc.headers or {}), exc)
    return Stream(resp.status, dict(resp.headers), resp)


StreamTransport = Callable[[str, float], Stream]

#: Bytes read per chunk while downloading a media file (one chunk per
#: :data:`TRANSFER_WORKERS` download in flight).
DOWNLOAD_CHUNK = 256 * 1024
#: Presigned uploads and media downloads in flight at once. A small file's
#: transfer is nearly all round trip, so a 20,000-file tree is bound by this.
TRANSFER_WORKERS = 16

_T = TypeVar("_T")


def concurrently(items: Iterable[_T], work: Callable[[_T], None], *,
                 workers: int = TRANSFER_WORKERS) -> None:
    """``work(item)`` for each item on a bounded pool, returning once every
    started item is done. After the first failure no further item starts, and
    the failure raised is the one of the EARLIEST item that failed -- the
    error a one-at-a-time loop would have stopped on."""
    items = list(items)
    if len(items) <= 1 or workers <= 1:
        for item in items:
            work(item)
        return
    stop = threading.Event()

    def one(item: _T) -> None:
        if stop.is_set():
            return
        try:
            work(item)
        except BaseException:
            stop.set()
            raise

    pool = ThreadPoolExecutor(max_workers=min(workers, len(items)),
                              thread_name_prefix="exelichai-transfer")
    try:
        futures = [pool.submit(one, item) for item in items]
        for future in futures:
            future.exception()   # wait for each, in order
    except BaseException:
        stop.set()   # an interrupt: start nothing more
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    for future in futures:
        exc = future.exception()
        if exc is not None:
            raise exc


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

class Credential:
    kind = "token"
    scope: Optional[str] = None
    lane: Optional[str] = None
    game_id: Optional[str] = None

    def bearer(self) -> str:
        raise NotImplementedError

    def refreshable(self) -> bool:
        return False

    def refresh(self) -> None:  # pragma: no cover - static tokens never refresh
        raise NotImplementedError


class StaticToken(Credential):
    """``exlv_`` (validate) or ``exla_`` (author). Publishing is OIDC-only:
    no static token can publish."""

    kind = "token"

    def __init__(self, token: str):
        token = token.strip()
        scope = STATIC_PREFIX_SCOPE.get(token[:5])
        if scope is None:
            hint = (" (an exls_ session credential is minted inside Actions, never set by hand)"
                    if token.startswith(SESSION_PREFIX) else "")
            raise CliError([Problem("credential-invalid",
                                    f"{TOKEN_ENV} is not a game token{hint}")], EXIT_AUTH)
        self.token = token
        self.scope = scope
        self.lane = LANE_BY_SCOPE[scope]
        self.game_id = _token_game(token)

    def bearer(self) -> str:
        return self.token


def _token_game(token: str) -> Optional[str]:
    parts = token[5:].split(".")
    if len(parts) != 4:
        return None
    raw = parts[1]
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


class OidcSession(Credential):
    """A GitHub Actions OIDC token exchanged for a 15-minute session
    credential (``POST /auth/github-oidc``). The audience is the API origin."""

    kind = "oidc"

    def __init__(self, client: "ApiClient", game_id: str, env: Mapping[str, str]):
        self.client = client
        self.game_id = game_id
        self.env = env
        self.session: Optional[str] = None
        self.expires_at: float = 0
        self.ref: Optional[str] = None
        self.sha: Optional[str] = None
        self.exchanges = 0

    @staticmethod
    def available(env: Mapping[str, str]) -> bool:
        return bool(env.get("ACTIONS_ID_TOKEN_REQUEST_URL")
                    and env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"))

    def _github_token(self) -> str:
        url = self.env.get("ACTIONS_ID_TOKEN_REQUEST_URL") or ""
        request_token = self.env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN") or ""
        if not url or not request_token:
            raise CliError([Problem("oidc-not-granted",
                                    "ACTIONS_ID_TOKEN_REQUEST_URL is not set for this job")],
                           EXIT_AUTH)
        if urllib.parse.urlsplit(url).scheme != "https":
            raise CliError([Problem("oidc-not-granted", "the OIDC request URL is not HTTPS")],
                           EXIT_AUTH)
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}audience={urllib.parse.quote(self.client.origin, safe='')}"
        resp = self.client.raw(
            "GET", full, {"Authorization": f"bearer {request_token}", "Accept": "application/json"})
        value = None
        if resp.status == 200:
            try:
                value = json.loads(resp.body.decode("utf-8")).get("value")
            except (ValueError, AttributeError, UnicodeDecodeError):
                value = None
        if not isinstance(value, str) or not value:
            raise CliError([Problem("oidc-unavailable",
                                    f"GitHub's OIDC endpoint answered HTTP {resp.status}")],
                           EXIT_AUTH)
        self.client.reporter.mask(value)
        return value

    def refreshable(self) -> bool:
        return True

    def refresh(self) -> None:
        id_token = self._github_token()
        body = self.client.call("POST", OIDC_ROUTE, {"game_id": self.game_id, "id_token": id_token},
                                auth=False)
        session = body.get("session_token")
        if not isinstance(session, str) or not session.startswith(SESSION_PREFIX):
            raise CliError([Problem("api-unreachable", "the OIDC exchange returned no session")],
                           EXIT_AUTH)
        self.client.reporter.mask(session)
        self.session = session
        self.scope = body.get("scope")
        self.lane = body.get("lane") or LANE_BY_SCOPE.get(self.scope or "")
        self.ref, self.sha = body.get("ref"), body.get("sha")
        try:
            self.expires_at = float(body.get("expires_at") or 0)
        except (TypeError, ValueError):
            self.expires_at = 0
        self.exchanges += 1

    def bearer(self) -> str:
        if self.session is None or self.expires_at - self.client.clock() < REFRESH_MARGIN:
            self.refresh()
        return self.session  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

def problems_from_body(status: int, raw: bytes) -> List[Problem]:
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        body = {}
    if isinstance(body, dict) and isinstance(body.get("errors"), list) and body["errors"]:
        return [Problem.from_dict(e) for e in body["errors"] if isinstance(e, dict)]
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        return [Problem.from_dict(body)]
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("message") or body.get("error") or "")
    return [Problem("api-unreachable", f"HTTP {status}{': ' + detail if detail else ''}")]


class ApiClient:
    def __init__(self, origin: str, *, reporter: Reporter, game_id: str,
                 transport: Transport = pooled_transport, in_actions: bool = False,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.origin = origin
        self.reporter = reporter
        self.game_id = game_id
        self.transport = transport
        self.in_actions = in_actions
        self.clock = clock
        self.sleep = sleep
        self.credential: Optional[Credential] = None
        #: Media downloads (``download_to``): the transport's own streaming GET
        #: when it has one (a test's fake), else urllib's.
        self.stream: StreamTransport = getattr(transport, "stream", None) or urllib_stream

    # -- plumbing ------------------------------------------------------------
    def raw(self, method: str, url: str, headers: Mapping[str, str],
            body: Optional[Body] = None) -> Response:
        """One request with retries on connection errors and 502/503/504. A
        file body is rewound before each attempt."""
        last: Optional[str] = None
        for attempt in range(RETRIES):
            if body is not None and hasattr(body, "seek"):
                body.seek(0)  # type: ignore[union-attr]
            try:
                resp = self.transport(method, url, headers, body, REQUEST_TIMEOUT)
            except (OSError, urllib.error.URLError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status not in RETRY_STATUSES:
                    return resp
                last = f"HTTP {resp.status}"
            if attempt + 1 < RETRIES:
                self.sleep(2 ** attempt)
        raise CliError([Problem("api-unreachable", f"{method} {_redact(url)}: {last}")],
                       EXIT_FAILED)

    def call(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None, *,
             auth: bool = True) -> Dict[str, Any]:
        """A JSON API call. Non-2xx raises :class:`CliError` with the
        server's reasons; a ``credential-expired`` session re-exchanges once."""
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (0, 1):
            headers = {"Accept": "application/json",
                       "User-Agent": f"exelichai-cli/{__version__}"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            if auth:
                if self.credential is None:
                    raise CliError([Problem("credential-missing")], EXIT_AUTH)
                headers["Authorization"] = f"Bearer {self.credential.bearer()}"
            resp = self.raw(method, self.origin + path, headers, payload)
            if 200 <= resp.status < 300:
                if len(resp.body) > MAX_JSON_BYTES:
                    raise CliError([Problem("api-unreachable", "response too large")])
                try:
                    out = json.loads(resp.body.decode("utf-8")) if resp.body else {}
                except (ValueError, UnicodeDecodeError):
                    raise CliError([Problem("api-unreachable",
                                            f"{method} {path}: the answer is not JSON")])
                out = out if isinstance(out, dict) else {}
                out["_status"] = resp.status
                return out
            problems = problems_from_body(resp.status, resp.body)
            codes = {p.code for p in problems}
            if (attempt == 0 and auth and "credential-expired" in codes
                    and self.credential is not None and self.credential.refreshable()):
                self.credential.refresh()
                continue
            raise CliError(problems, exit_code_for(codes))
        raise AssertionError("unreachable")  # pragma: no cover

    # -- routes --------------------------------------------------------------
    def _game(self) -> str:
        return "/studio/games/" + urllib.parse.quote(self.game_id, safe="")

    def manifest(self, files: List[Dict[str, Any]], *,
                 target: Optional[str] = None) -> Dict[str, Any]:
        """The legacy single stage (no ``protocol``). ``target="dev"``: the
        caller's own dev shadow (``exelichai dev``)."""
        body: Dict[str, Any] = {"files": files}
        if target:
            body["target"] = target
        return self.call("POST", self._game() + "/github/sync/manifest", body)

    def manifest_page(self, files: List[Dict[str, Any]], *, page: int,
                      session: Optional[str] = None,
                      target: Optional[str] = None) -> Dict[str, Any]:
        """A stage-1 page of an upload session (plan 2026-10-26-002 Unit 3):
        the first page (``session`` None) mints the session."""
        body: Dict[str, Any] = {"protocol": UPLOAD_PROTOCOL, "stage": "files", "page": page,
                                "files": files}
        if session:
            body["session"] = session
        if target:
            body["target"] = target
        return self.call("POST", self._game() + "/github/sync/manifest", body)

    def candidates(self, session: str, *, cursor: int = 0,
                   target: Optional[str] = None) -> Dict[str, Any]:
        """Stage 2: the referenced candidates' URLs, ``cursor`` onward, and
        the session's manifest digest."""
        body: Dict[str, Any] = {"protocol": UPLOAD_PROTOCOL, "stage": "candidates",
                                "session": session, "cursor": cursor}
        if target:
            body["target"] = target
        return self.call("POST", self._game() + "/github/sync/manifest", body)

    def finalise(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        return self.call("POST", self._game() + "/github/sync/finalise", body)

    def game_tests(self, tests: Mapping[str, str], *,
                   version: Optional[str] = None) -> Dict[str, Any]:
        """Run the game's own tests (plan 2026-10-26-002 Unit 9b) against the
        draft, or the release ``version``: the report; writes nothing."""
        body: Dict[str, Any] = {"tests": dict(tests)}
        if version:
            body["version"] = version
        return self.call("POST", self._game() + "/github/tests", body)

    def sync_status(self) -> Dict[str, Any]:
        """The deploy branches' newest jobs and last platform re-sync attempts."""
        return self.call("GET", self._game() + "/github/sync/status")

    def rerun(self, *, lane: Optional[str] = None, branch: Optional[str] = None) -> Dict[str, Any]:
        """Re-run a deploy branch's last commit through the App."""
        body = {k: v for k, v in (("lane", lane), ("branch", branch)) if v}
        return self.call("POST", self._game() + "/github/sync/rerun", body)

    def _definition_path(self, entity_type: str, entity_id: str) -> str:
        return (self._game() + "/github/definitions/" + urllib.parse.quote(entity_type, safe="")
                + "/" + urllib.parse.quote(entity_id, safe=""))

    @staticmethod
    def _version_query(version: Optional[str], **extra: Optional[str]) -> str:
        params = {k: v for k, v in (("version", version), *extra.items()) if v}
        return "?" + urllib.parse.urlencode(params) if params else ""

    def definitions(self, entity_type: Optional[str] = None, *,
                    version: Optional[str] = None) -> Dict[str, Any]:
        """Every definition of the draft (or ``version``), with its row lineage."""
        return self.call("GET", self._game() + "/github/definitions"
                         + self._version_query(version, type=entity_type))

    def definition(self, entity_type: str, entity_id: str, *,
                   version: Optional[str] = None) -> Dict[str, Any]:
        """One definition, resolved, with its provenance."""
        return self.call("GET", self._definition_path(entity_type, entity_id)
                         + self._version_query(version))

    def definition_references(self, entity_type: str, entity_id: str, *,
                              version: Optional[str] = None) -> Dict[str, Any]:
        """Who references one definition, across the game's own chain."""
        return self.call("GET", self._definition_path(entity_type, entity_id) + "/references"
                         + self._version_query(version))

    def job(self, job_id: str) -> Dict[str, Any]:
        return self.call("GET", self._game() + "/github/sync/jobs/"
                         + urllib.parse.quote(job_id, safe=""))

    def export(self, scope: str, *, reinitialise: bool = False,
               download: bool = False) -> Dict[str, Any]:
        """``download`` (plan 2026-10-26-001 KTD-6): on an initialised game a
        full export answers the tree with ``already_initialised: true`` and
        writes nothing, instead of refusing ``already-initialised``. Media
        comes by manifest (plan 2026-10-26-002 KTD-8): the zip holds the rules
        side and ``media-manifest.json``; each media file is downloaded from
        its URL (:meth:`download_to`)."""
        body: Dict[str, Any] = {"scope": scope, "media": MEDIA_MODE}
        if reinitialise:
            body["reinitialise"] = True
        if download:
            body["download"] = True
        return self.call("POST", self._game() + "/github/export", body)

    def pull_baseline(self) -> Dict[str, Any]:
        """The commit ``pull`` places its checkout against, and the draft
        branch (``scope: rules`` with ``baseline_only``; writes nothing)."""
        return self.call("POST", self._game() + "/github/export",
                         {"scope": "rules", "baseline_only": True})

    def pull_rules(self, files: Mapping[str, str], baseline_commit: str) -> Dict[str, Any]:
        """``scope: rules``: the working copy of the manifest and every rules
        file it lists, and the commit the checkout was placed against."""
        return self.call("POST", self._game() + "/github/export",
                         {"scope": "rules", "files": dict(files),
                          "baseline_commit": baseline_commit, "media": MEDIA_MODE})

    def conflicts(self) -> Dict[str, Any]:
        return self.call("GET", self._game() + "/github/conflicts")

    def conflict(self, conflict_id: str) -> Dict[str, Any]:
        return self.call("GET", self._game() + "/github/conflicts/"
                         + urllib.parse.quote(conflict_id, safe=""))

    def resolve(self, conflict_id: str, keep: str) -> Dict[str, Any]:
        return self.call("POST", self._game() + "/github/conflicts/"
                         + urllib.parse.quote(conflict_id, safe="") + "/resolve", {"keep": keep})

    def unsent(self) -> Dict[str, Any]:
        return self.call("GET", self._game() + "/github/unsent")

    def discard_change(self, address: str) -> Dict[str, Any]:
        return self.call("POST", self._game() + "/github/unsent/"
                         + urllib.parse.quote(address, safe="") + "/discard", {})

    def discards(self, cursor: Optional[str] = None) -> Dict[str, Any]:
        query = "?" + urllib.parse.urlencode({"cursor": cursor}) if cursor else ""
        return self.call("GET", self._game() + "/github/discards" + query)

    def restore_discard(self, discard_id: str, *, confirm: bool = False) -> Dict[str, Any]:
        return self.call("POST", self._game() + "/github/discards/"
                         + urllib.parse.quote(discard_id, safe="") + "/restore",
                         {"confirm": confirm})

    def dismiss_discard(self, discard_id: str) -> Dict[str, Any]:
        return self.call("DELETE", self._game() + "/github/discards/"
                         + urllib.parse.quote(discard_id, safe=""))

    def dismiss_all_discards(self) -> Dict[str, Any]:
        return self.call("DELETE", self._game() + "/github/discards")

    # -- presigned transfers (no credential) ---------------------------------
    def put_blob(self, upload: Mapping[str, Any], data: Body, *,
                 path: Optional[str] = None) -> None:
        """PUT ``data`` (bytes, or a file object streamed as it is read) to a
        presigned URL with its signed headers. S3 refuses bytes whose
        checksum is not the signed one (:class:`UploadRefused`
        ``blob-hash-mismatch``, naming ``path``) and a URL past its expiry
        (:class:`UploadExpired`)."""
        url = check_presigned(upload.get("url"), allow_loopback=not self.in_actions)
        headers = {str(k): str(v) for k, v in (upload.get("headers") or {}).items()
                   if str(k).lower() != "authorization"}
        resp = self.raw(str(upload.get("method") or "PUT"), url, headers, data)
        if 200 <= resp.status < 300:
            return
        what = path or str(upload.get("sha256"))
        text = resp.body[:2048].decode("utf-8", "replace") if resp.body else ""
        if resp.status == 403 and "expired" in text.lower():
            raise UploadExpired([Problem("api-unreachable",
                                         f"the upload URL for {what} has expired")])
        if resp.status == 400 and any(word in text for word in _CHECKSUM_REFUSALS):
            raise UploadRefused([Problem(
                "blob-hash-mismatch",
                f"{what} changed after it was scanned: its bytes no longer match the "
                f"sha256 the upload was signed for. Run the command again.", file=path)])
        raise CliError([Problem("api-unreachable",
                                f"upload of {what} failed: HTTP {resp.status}")])

    def put_file(self, upload: Mapping[str, Any], full: str, *,
                 path: Optional[str] = None) -> None:
        """:meth:`put_blob` streaming the file at ``full`` (never through a
        link): memory stays flat whatever its size."""
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as fh:
            self.put_blob(upload, fh, path=path)

    def download(self, url: Any, *, expected_size: int) -> bytes:
        url = check_presigned(url, allow_loopback=not self.in_actions)
        resp = self.raw("GET", url, {})
        if resp.status != 200:
            raise CliError([Problem("archive-invalid", f"download failed: HTTP {resp.status}")])
        if len(resp.body) != expected_size:
            raise CliError([Problem("archive-invalid",
                                    f"{len(resp.body)} bytes, the manifest says {expected_size}")])
        return resp.body


    def download_to(self, url: Any, dest: str, *, expected_size: int, expected_sha256: str,
                    path: Optional[str] = None) -> None:
        """Stream one manifest media file into ``dest`` (a new file the caller
        renames into place), hashing as it arrives. The URL must be ``https``
        on this API origin's media host EXACTLY (:func:`check_media_url`); no
        redirect is followed; more than ``expected_size`` bytes stops the
        read; the sha256 and size must match. Any failure removes ``dest`` and
        raises ``archive-invalid`` naming ``path``. Memory holds one chunk.
        Retries connection errors and 5xx like :meth:`raw`."""
        import hashlib

        url = check_media_url(url, api_origin=self.origin)
        what = path or _redact(url)
        bad = lambda why: CliError([Problem("archive-invalid", f"{what}: {why}",  # noqa: E731
                                            file=path)], EXIT_FAILED)
        last = ""
        for attempt in range(RETRIES):
            try:
                resp = self.stream(url, REQUEST_TIMEOUT)
            except (OSError, urllib.error.URLError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status in RETRY_STATUSES:
                    _close(resp.body)
                    last = f"HTTP {resp.status}"
                else:
                    try:
                        if resp.status != 200:
                            raise bad(f"download failed: HTTP {resp.status}")
                        digest, size = hashlib.sha256(), 0
                        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
                        fd = os.open(dest, flags, 0o644)
                        try:
                            with os.fdopen(fd, "wb") as fh:
                                while True:
                                    chunk = resp.body.read(DOWNLOAD_CHUNK)
                                    if not chunk:
                                        break
                                    size += len(chunk)
                                    if size > expected_size:
                                        raise bad(f"more than the manifest's {expected_size} "
                                                  "bytes")
                                    digest.update(chunk)
                                    fh.write(chunk)
                            if size != expected_size or digest.hexdigest() != expected_sha256:
                                raise bad("the downloaded bytes do not match the manifest's "
                                          "sha256 and size")
                        except BaseException:
                            _remove(dest)
                            raise
                        return
                    finally:
                        _close(resp.body)
            if attempt + 1 < RETRIES:
                self.sleep(2 ** attempt)
        raise CliError([Problem("api-unreachable", f"GET {_redact(url)}: {last}")], EXIT_FAILED)


#: The export request's media mode (the server's ``export.MEDIA_MANIFEST_MODE``).
MEDIA_MODE = "manifest"


def _close(body: Any) -> None:
    close = getattr(body, "close", None)
    if close:
        try:
            close()
        except OSError:
            pass


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class UploadExpired(CliError):
    """A presigned URL was used after its expiry: presign again."""


class UploadRefused(CliError):
    """S3 refused the bytes (their checksum is not the signed one)."""


#: S3's words for a body whose checksum is not the signed one.
_CHECKSUM_REFUSALS = ("BadDigest", "XAmzContentChecksumMismatch", "XAmzContentSHA256Mismatch",
                      "InvalidDigest", "checksum")


def _redact(url: str) -> str:
    """A URL without its query (presigned signatures, OIDC request params)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def credential_from_env(client: ApiClient, env: Mapping[str, str], *,
                        prefer_oidc: bool = False) -> Credential:
    """``EXELICHAI_TOKEN`` if set (unless ``prefer_oidc``), else the Actions
    OIDC session when the job may mint one, else ``credential-missing`` /
    ``oidc-not-granted``."""
    token = (env.get(TOKEN_ENV) or "").strip()
    if token and not prefer_oidc:
        cred: Credential = StaticToken(token)
        if cred.game_id and cred.game_id != client.game_id:
            raise CliError([Problem("credential-wrong-game",
                                    f"the token is for {cred.game_id!r}, not {client.game_id!r}")],
                           EXIT_AUTH)
        client.reporter.mask(token)
        return cred
    if env.get("GITHUB_ACTIONS") == "true":
        if not OidcSession.available(env):
            fork = env.get("GITHUB_EVENT_NAME") in ("pull_request", "pull_request_review")
            why = ("a pull request from a fork gets no OIDC token (or the job lacks "
                   "`id-token: write`)" if fork
                   else "the job has no `id-token: write` permission")
            raise CliError([Problem("oidc-not-granted", why)], EXIT_AUTH)
        return OidcSession(client, client.game_id, env)
    raise CliError([Problem("credential-missing")], EXIT_AUTH)
