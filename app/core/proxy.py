"""Reverse proxy sub-path support (``ROOT_PATH``).

``RootPathMiddleware`` sets ``scope["root_path"]`` for every request, which is what
FastAPI's (now deprecated) ``FastAPI(root_path=...)`` argument did. FastAPI then serves
``/docs`` with the prefixed OpenAPI URL and advertises the prefix in ``servers``.

Unlike Uvicorn's ``--root-path`` (which prepends the prefix to every incoming path and
therefore only works with proxies that strip it), this works with both proxy styles:

* the proxy strips ``/fraimic`` and forwards ``/api/status``;
* the proxy keeps the prefix and forwards ``/fraimic/api/status``.

Starlette's router removes the prefix when present before matching routes.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class RootPathMiddleware:
    """Inject a fixed ``root_path`` into HTTP and WebSocket scopes."""

    def __init__(self, app: ASGIApp, *, root_path: str) -> None:
        """Wrap ``app``.

        Args:
            app: Downstream ASGI application.
            root_path: Normalized prefix such as ``/fraimic``; empty disables the middleware.
        """
        self.app = app
        self.root_path = root_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.root_path and scope["type"] in {"http", "websocket"}:
            scope = {**scope, "root_path": self.root_path}
        await self.app(scope, receive, send)
