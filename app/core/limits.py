"""Request body size limit (pure ASGI middleware).

FastAPI parses (and spools to disk) multipart bodies *before* the endpoint runs, so a
per-route size check alone cannot stop a client from streaming gigabytes. This
middleware rejects requests whose ``Content-Length`` exceeds the limit and aborts
chunked bodies as soon as they grow past it.
"""

from __future__ import annotations

from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Allowance for multipart boundaries and form fields on top of the file itself.
MULTIPART_OVERHEAD_BYTES: Final = 1024 * 1024


class BodyTooLargeError(Exception):
    """Raised inside ``receive`` when a streamed body exceeds the limit."""


class BodySizeLimitMiddleware:
    """Reject HTTP request bodies larger than ``max_body_bytes`` with ``413``."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                break

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLargeError:
            if not response_started:
                await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit_mb = self.max_body_bytes // (1024 * 1024)
        response = JSONResponse(
            {"detail": f"Request body exceeds the {limit_mb} MB limit"},
            status_code=413,
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)
