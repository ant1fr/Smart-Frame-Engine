"""Adaptive API key authentication (pure ASGI middleware).

Rules when ``APP_API_KEY`` is set (no-op when it is empty):

* ``POST``/``PUT``/``PATCH``/``DELETE`` on any route require the key.
* ``/api/config`` and everything below it require the key for every method.
* ``/api/current.jpg`` and ``/api/current.bin`` require the key only when
  ``PUBLIC_IMAGE_ENDPOINTS=false``.
* Everything else (UI, ``/docs``, ``/openapi.json``, ``/api/health``, ``GET /api/status``)
  stays public so the UI can load and prompt for the key.

The key is accepted from the ``X-API-Key`` header or the ``api_key`` query parameter and
compared in constant time.
"""

from __future__ import annotations

import hmac
from typing import Final
from urllib.parse import parse_qsl

from fastapi.security import APIKeyHeader
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_HEADER: Final = "X-API-Key"
API_KEY_QUERY_PARAM: Final = "api_key"

MUTATING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PROTECTED_PREFIXES: Final = ("/api/config",)
IMAGE_ENDPOINTS: Final = frozenset({"/api/current.jpg", "/api/current.bin"})

#: OpenAPI-only security scheme so ``/docs`` offers an "Authorize" button. Enforcement
#: is done by :class:`APIKeyMiddleware`, never by this dependency.
api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Required for mutating routes and /api/config when APP_API_KEY is set.",
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
    """Decide whether a request must carry a valid API key."""
    method = method.upper()
    if method == "OPTIONS":
        return False
    if method in MUTATING_METHODS:
        return True
    normalized = path.rstrip("/") or "/"
    if any(normalized == p or normalized.startswith(f"{p}/") for p in PROTECTED_PREFIXES):
        return True
    return normalized in IMAGE_ENDPOINTS and not public_image_endpoints


def extract_api_key(scope: Scope) -> str | None:
    """Read the API key from the ``X-API-Key`` header or the ``api_key`` query param."""
    header_name = API_KEY_HEADER.lower().encode("latin-1")
    for name, value in scope.get("headers", []):
        if name.lower() == header_name:
            return bytes(value).decode("latin-1").strip()
    query: bytes = scope.get("query_string", b"")
    if query:
        for name, value in parse_qsl(query.decode("latin-1"), keep_blank_values=True):
            if name == API_KEY_QUERY_PARAM:
                return value.strip()
    return None


def keys_match(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of a provided key against the expected key."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


class APIKeyMiddleware:
    """Enforce :func:`requires_api_key` rules for HTTP requests."""

    def __init__(self, app: ASGIApp, *, api_key: str, public_image_endpoints: bool) -> None:
        """Wrap ``app``.

        Args:
            app: Downstream ASGI application.
            api_key: Expected key; an empty string disables authentication.
            public_image_endpoints: Keep current.jpg/current.bin readable without a key.
        """
        self.app = app
        self.api_key = api_key
        self.public_image_endpoints = public_image_endpoints

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.api_key:
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "GET")
        if requires_api_key(
            method, route_path(scope), public_image_endpoints=self.public_image_endpoints
        ) and not keys_match(extract_api_key(scope), self.api_key):
            response = JSONResponse(
                {"detail": "Missing or invalid API key"},
                status_code=401,
                headers={"WWW-Authenticate": f'APIKey header="{API_KEY_HEADER}"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
