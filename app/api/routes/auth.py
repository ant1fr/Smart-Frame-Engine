"""Web UI login, session status and API key management."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, Field

from app.api.deps import AuthStateDep
from app.core.auth import ADMIN_USER, AuthMode, AuthVia, api_key_scheme
from app.core.secrets_store import ApiKeyManagedByEnvError, ApiKeySource
from app.core.sessions import COOKIE_NAME

router = APIRouter(tags=["auth"])


@lru_cache(maxsize=4)
def page(name: str) -> str:
    """A bundled HTML page (``app/templates/<name>``)."""
    return resources.files("app").joinpath("templates", name).read_text(encoding="utf-8")


def _cookie_path(request: Request) -> str:
    root_path: str = request.scope.get("root_path", "")
    return root_path or "/"


class LoginRequest(BaseModel):
    """Credentials of the web UI login."""

    username: str = Field(default=ADMIN_USER, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class SessionView(BaseModel):
    """Who the caller is, and how the instance is protected."""

    mode: AuthMode = Field(
        description="open = no authentication; key = API key only; password = admin login"
    )
    authenticated: bool
    via: AuthVia | None = Field(default=None, description="api_key or session")
    user: str | None = None
    expires_at: AwareDatetime | None = Field(default=None, description="Session expiry")
    api_key_source: ApiKeySource = Field(description="env (APP_API_KEY), generated or none")


class ApiKeyView(BaseModel):
    """The API key for Home Assistant and other clients."""

    api_key: str
    source: ApiKeySource
    regenerable: bool = Field(description="False when APP_API_KEY manages the key")


@router.get("/login", include_in_schema=False)
async def login_page() -> HTMLResponse:
    """The login page (relative URLs, works under any reverse-proxy sub-path)."""
    return HTMLResponse(page("login.html"), headers={"Cache-Control": "no-store"})


@router.get("/api/auth/session", response_model=SessionView, summary="Current session")
async def session(request: Request, auth: AuthStateDep) -> SessionView:
    """Authentication mode and whether the caller is signed in (always reachable)."""
    via = auth.authenticate(request.scope) if auth.mode != "open" else None
    expires = None
    if via == "session":
        expiry = auth.session_expiry(request.cookies.get(COOKIE_NAME))
        expires = datetime.fromtimestamp(expiry, UTC) if expiry else None
    return SessionView(
        mode=auth.mode,
        authenticated=auth.mode == "open" or via is not None,
        via=via,
        user=ADMIN_USER if via == "session" else None,
        expires_at=expires,
        api_key_source=auth.secrets.api_key_source,
    )


@router.post(
    "/api/auth/login",
    responses={
        401: {"description": "Wrong user name or password"},
        409: {"description": "No login is configured (set ADMIN_PASSWORD)"},
        429: {"description": "Too many failed attempts; see Retry-After"},
    },
    summary="Sign in to the web UI",
)
async def login(body: LoginRequest, request: Request, auth: AuthStateDep) -> JSONResponse:
    """Check ``admin`` + ``ADMIN_PASSWORD`` and set an HttpOnly session cookie.

    Without ``ADMIN_PASSWORD`` but with an API key, the API key is accepted as the password.
    """
    if auth.mode == "open":
        raise HTTPException(status_code=409, detail="No login is configured: set ADMIN_PASSWORD")
    client = request.client.host if request.client else "unknown"
    wait = auth.throttle.retry_after(client)
    if wait > 0:
        return JSONResponse(
            {"detail": "Too many failed attempts, try again later"},
            status_code=429,
            headers={"Retry-After": str(int(wait) + 1)},
        )
    if not auth.check_login(body.username, body.password):
        auth.throttle.failure(client)
        raise HTTPException(status_code=401, detail="Wrong user name or password")
    auth.throttle.success(client)
    token, expires = auth.issue_session()
    response = JSONResponse(
        {
            "authenticated": True,
            "user": ADMIN_USER,
            "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
        }
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(auth.session_ttl_s),
        path=_cookie_path(request),
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@router.post(
    "/api/auth/logout",
    dependencies=[Security(api_key_scheme)],
    summary="Sign out of the web UI",
)
async def logout(request: Request) -> JSONResponse:
    """Remove the session cookie."""
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        COOKIE_NAME,
        path=_cookie_path(request),
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@router.get(
    "/api/auth/api-key",
    response_model=ApiKeyView,
    dependencies=[Security(api_key_scheme)],
    summary="Show the API key",
)
async def api_key(auth: AuthStateDep) -> ApiKeyView:
    """The key Home Assistant and the frame use (``X-API-Key`` / ``Authorization: Bearer``)."""
    source = auth.secrets.api_key_source
    return ApiKeyView(
        api_key=auth.api_key,
        source=source,
        regenerable=source != "env" and auth.mode == "password",
    )


@router.post(
    "/api/auth/api-key/regenerate",
    response_model=ApiKeyView,
    dependencies=[Security(api_key_scheme)],
    responses={
        403: {"description": "Only a signed-in admin session can regenerate the key"},
        409: {"description": "APP_API_KEY manages the key, or no ADMIN_PASSWORD is set"},
    },
    summary="Generate a new API key",
)
async def regenerate_api_key(request: Request, auth: AuthStateDep) -> ApiKeyView:
    """Replace the stored API key; clients using the old one must be updated."""
    via: Literal["api_key", "session"] | None = getattr(request.state, "auth_via", None)
    if auth.mode != "password":
        raise HTTPException(
            status_code=409, detail="Set ADMIN_PASSWORD to manage the API key from the UI"
        )
    if via != "session":
        raise HTTPException(
            status_code=403, detail="Sign in to the web UI to regenerate the API key"
        )
    try:
        key = auth.secrets.regenerate_api_key()
    except ApiKeyManagedByEnvError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    refresh = getattr(request.app.state, "refresh_masked_secrets", None)
    if callable(refresh):
        refresh()
    return ApiKeyView(api_key=key, source="generated", regenerable=True)
