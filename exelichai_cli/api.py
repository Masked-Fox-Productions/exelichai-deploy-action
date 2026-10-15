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
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

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


# ---------------------------------------------------------------------------
# Transport (replaceable in tests)
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # surface the 3xx as an HTTPError instead of following it


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]


def urllib_transport(method: str, url: str, headers: Mapping[str, str],
                     body: Optional[bytes], timeout: float) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read() or b"", dict(exc.headers or {}))


Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], Response]


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
                 transport: Transport = urllib_transport, in_actions: bool = False,
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

    # -- plumbing ------------------------------------------------------------
    def raw(self, method: str, url: str, headers: Mapping[str, str],
            body: Optional[bytes] = None) -> Response:
        """One request with retries on connection errors and 502/503/504."""
        last: Optional[str] = None
        for attempt in range(RETRIES):
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

    def manifest(self, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call("POST", self._game() + "/github/sync/manifest", {"files": files})

    def finalise(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        return self.call("POST", self._game() + "/github/sync/finalise", body)

    def job(self, job_id: str) -> Dict[str, Any]:
        return self.call("GET", self._game() + "/github/sync/jobs/"
                         + urllib.parse.quote(job_id, safe=""))

    def export(self, scope: str, *, reinitialise: bool = False) -> Dict[str, Any]:
        body: Dict[str, Any] = {"scope": scope}
        if reinitialise:
            body["reinitialise"] = True
        return self.call("POST", self._game() + "/github/export", body)

    def conflicts(self) -> Dict[str, Any]:
        return self.call("GET", self._game() + "/github/conflicts")

    def resolve(self, conflict_id: str, keep: str) -> Dict[str, Any]:
        return self.call("POST", self._game() + "/github/conflicts/"
                         + urllib.parse.quote(conflict_id, safe="") + "/resolve", {"keep": keep})

    # -- presigned transfers (no credential) ---------------------------------
    def put_blob(self, upload: Mapping[str, Any], data: bytes) -> None:
        url = check_presigned(upload.get("url"), allow_loopback=not self.in_actions)
        headers = {str(k): str(v) for k, v in (upload.get("headers") or {}).items()
                   if str(k).lower() != "authorization"}
        resp = self.raw(str(upload.get("method") or "PUT"), url, headers, data)
        if not 200 <= resp.status < 300:
            raise CliError([Problem("api-unreachable",
                                    f"upload of {upload.get('sha256')} failed: HTTP {resp.status}")])

    def download(self, url: Any, *, expected_size: int) -> bytes:
        url = check_presigned(url, allow_loopback=not self.in_actions)
        resp = self.raw("GET", url, {})
        if resp.status != 200:
            raise CliError([Problem("archive-invalid", f"download failed: HTTP {resp.status}")])
        if len(resp.body) != expected_size:
            raise CliError([Problem("archive-invalid",
                                    f"{len(resp.body)} bytes, the manifest says {expected_size}")])
        return resp.body


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
