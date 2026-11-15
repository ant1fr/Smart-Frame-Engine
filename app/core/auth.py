"""Adaptive authentication (pure ASGI middleware): API key, Bearer token and login session.

Three modes, chosen from the environment and ``/data/secrets.json``:

* **open** - no ``ADMIN_PASSWORD`` and no API key: pure LAN usage, everything is open.
* **key** - an API key (``APP_API_KEY``) but no password. As before:

  * ``POST``/``PUT``/``PATCH``/``DELETE`` on any route require credentials;
  * ``/api/config``, ``/api/immich`` (family member names, albums) and ``/api/smart-mix``
    require them for every method;
  * the image endpoints (``/api/current.*``, ``/api/preview.*``) require them only when
    ``PUBLIC_IMAGE_ENDPOINTS=false``;
  * everything else stays public.

* **password** - ``ADMIN_PASSWORD`` is set (an API key is generated if none exists). Every
  route requires credentials except ``/api/health``, the login (``/login``,
  ``POST /api/auth/login``, ``GET /api/auth/session``) and the image endpoints while
  ``PUBLIC_IMAGE_ENDPOINTS=true``. Browsers asking for a page are redirected to the login.

Credentials: the API key in the ``X-API-Key`` header, an ``Authorization: Bearer`` header or
the ``api_key`` query parameter (constant-time comparison), or the signed ``sfe_session``
cookie of a web UI login. A cookie-authenticated ``POST``/``PUT``/``PATCH``/``DELETE`` must
also carry ``X-Requested-With: SmartFrame``: browsers cannot add that header cross-site
without a CORS preflight, which blocks cross-site request forgery.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Final, Literal
from urllib.parse import parse_qsl

from fastapi.security import APIKeyHeader
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.secrets_store import SecretsStore
from app.core.sessions import COOKIE_NAME, LoginThrottle, issue_token, signing_key, token_expiry

API_KEY_HEADER: Final = "X-API-Key"
API_KEY_QUERY_PARAM: Final = "api_key"
CSRF_HEADER: Final = "X-Requested-With"
CSRF_VALUE: Final = "SmartFrame"
ADMIN_USER: Final = "admin"

MUTATING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PROTECTED_PREFIXES: Final = (
    "/api/config",
    "/api/immich",
    "/api/smart-mix",
    "/api/auth/api-key",
    "/api/tmdb",
    "/api/activity",
    "/api/playground",
)
IMAGE_ENDPOINTS: Final = frozenset(
    {
        "/api/current.jpg",
        "/api/current.bin",
        "/api/current_dithered.png",
        "/api/preview.jpg",
        "/api/preview_dithered.png",
    }
)
#: Reachable without credentials in every mode (the login itself and the liveness probe).
ALWAYS_PUBLIC: Final = frozenset(
    {"/api/health", "/login", "/api/auth/login", "/api/auth/session", "/favicon.ico"}
)
PUBLIC_PREFIXES: Final = ("/static/",)

AuthMode = Literal["open", "key", "password"]
AuthVia = Literal["api_key", "session"]

#: OpenAPI-only security scheme so ``/docs`` offers an "Authorize" button. Enforcement
#: is done by :class:`AuthMiddleware`, never by this dependency.
api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="API key (or a web UI login session). Also accepted as "
    "'Authorization: Bearer <key>' or '?api_key='.",
)


def route_path(scope: Scope) -> str:
    """Request path relative to the application's ``root_path``.

    Works whether or not the reverse proxy strips the sub-path prefix.
    """
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return "/"
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def requires_api_key(method: str, path: str, *, public_image_endpoints: bool) -> bool:
    """The ``key`` mode rules: whether a request must carry credentials."""
    method = method.upper()
    if method == "OPTIONS":
        return False
    if method in MUTATING_METHODS:
        return True
    normalized = path.rstrip("/") or "/"
    if any(normalized == p or normalized.startswith(f"{p}/") for p in PROTECTED_PREFIXES):
        return True
    return normalized in IMAGE_ENDPOINTS and not public_image_endpoints


def needs_credentials(
    method: str, path: str, mode: AuthMode, *, public_image_endpoints: bool
) -> bool:
    """Whether a request must carry credentials in ``mode``."""
    if mode == "open" or method.upper() == "OPTIONS":
        return False
    normalized = path.rstrip("/") or "/"
    if normalized in ALWAYS_PUBLIC or path.startswith(PUBLIC_PREFIXES):
        return False
    if mode == "key":
        return requires_api_key(method, path, public_image_endpoints=public_image_endpoints)
    if normalized in IMAGE_ENDPOINTS:
        return not public_image_endpoints
    return True


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return bytes(value).decode("latin-1")
    return None


def extract_api_key(scope: Scope) -> str | None:
    """The API key from ``X-API-Key``, ``Authorization: Bearer`` or ``?api_key=``."""
    header = _header(scope, API_KEY_HEADER)
    if header is not None:
        return header.strip()
    authorization = _header(scope, "authorization")
    if authorization is not None:
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    query: bytes = scope.get("query_string", b"")
    if query:
        for name, value in parse_qsl(query.decode("latin-1"), keep_blank_values=True):
            if name == API_KEY_QUERY_PARAM:
                return value.strip()
    return None


def extract_session_cookie(scope: Scope) -> str | None:
    """The raw ``sfe_session`` cookie value, if any."""
    header = _header(scope, "cookie")
    if not header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:  # malformed Cookie header: treat as absent
        return None
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel is not None else None


def keys_match(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of a provided key against the expected key."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


class AuthState:
    """Effective credentials: the admin password, the API key and session signing."""

    def __init__(
        self,
        secrets: SecretsStore,
        *,
        password: str = "",
        session_ttl_s: float = 7 * 24 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.secrets = secrets
        self.password = password
        self.session_ttl_s = session_ttl_s
        self.throttle = LoginThrottle()
        self._clock = clock

    def load(self) -> None:
        """Read the stored secrets; a password without any key generates one."""
        self.secrets.load()
        if self.password:
            self.secrets.ensure_api_key()

    @property
    def mode(self) -> AuthMode:
        """``password`` when a login is configured, else ``key`` or ``open``."""
        if self.password:
            return "password"
        return "key" if self.api_key else "open"

    @property
    def api_key(self) -> str:
        """The effective API key (empty when none exists)."""
        return self.secrets.api_key

    def _signing_key(self) -> bytes:
        return signing_key(self.secrets.session_secret, self.password)

    def issue_session(self) -> tuple[str, int]:
        """A new session token and its expiry (Unix seconds)."""
        return issue_token(self._signing_key(), self.session_ttl_s, now=self._clock())

    def session_expiry(self, token: str | None) -> int | None:
        """The expiry of a valid session token (``None`` when invalid or expired)."""
        if not token or self.mode == "open":
            return None
        return token_expiry(token, self._signing_key(), now=self._clock())

    def check_login(self, username: str, password: str) -> bool:
        """Validate a login: ``admin`` + the password, or the API key when no password is set."""
        if self.mode == "password":
            user_ok = hmac.compare_digest(username.strip().encode(), ADMIN_USER.encode())
            return keys_match(password, self.password) and user_ok
        if self.mode == "key":
            return keys_match(password, self.api_key)
        return False

    def authenticate(self, scope: Scope) -> AuthVia | None:
        """How the request is authenticated (``None`` = not at all)."""
        if keys_match(extract_api_key(scope), self.api_key):
            return "api_key"
        if self.session_expiry(extract_session_cookie(scope)) is not None:
            return "session"
        return None


def _wants_html(scope: Scope, path: str) -> bool:
    accept = _header(scope, "accept") or ""
    return not path.startswith("/api/") and "text/html" in accept


class AuthMiddleware:
    """Enforce :func:`needs_credentials` and record how each request authenticated."""

    def __init__(self, app: ASGIApp, *, state: AuthState, public_image_endpoints: bool) -> None:
        """Wrap ``app``.

        Args:
            app: Downstream ASGI application.
            state: Effective credentials (read on every request: keys can be regenerated).
            public_image_endpoints: Keep the image endpoints readable without credentials.
        """
        self.app = app
        self.state = state
        self.public_image_endpoints = public_image_endpoints

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method: str = scope.get("method", "GET").upper()
        path = route_path(scope)
        via = self.state.authenticate(scope) if self.state.mode != "open" else None
        scope.setdefault("state", {})["auth_via"] = via

        if needs_credentials(
            method, path, self.state.mode, public_image_endpoints=self.public_image_endpoints
        ):
            response = self._refusal(scope, method, path, via)
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _refusal(
        self, scope: Scope, method: str, path: str, via: AuthVia | None
    ) -> Response | None:
        if via == "api_key":
            return None
        if via == "session":
            if method in MUTATING_METHODS and _header(scope, CSRF_HEADER) != CSRF_VALUE:
                return JSONResponse(
                    {"detail": f"Session requests must send '{CSRF_HEADER}: {CSRF_VALUE}'"},
                    status_code=403,
                )
            return None
        if method == "GET" and self.state.mode == "password" and _wants_html(scope, path):
            root_path: str = scope.get("root_path", "")
            return RedirectResponse(f"{root_path}/login", status_code=303)
        return JSONResponse(
            {"detail": "Missing or invalid API key or session"},
            status_code=401,
            headers={"WWW-Authenticate": f'APIKey header="{API_KEY_HEADER}"'},
        )
