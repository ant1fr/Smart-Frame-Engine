from __future__ import annotations

from typing import Any

from starlette.types import Message, Receive, Scope, Send

from app.core.limits import BodySizeLimitMiddleware


async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Reads the whole body, then answers 200 with its length."""
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(size).encode()})


async def call(
    app: Any, chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None
) -> list[Message]:
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers or []}
    pending = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def test_small_bodies_pass_through() -> None:
    app = BodySizeLimitMiddleware(echo_app, max_body_bytes=10)
    sent = await call(app, [b"12345", b"678"], [(b"content-length", b"8")])
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"8"


async def test_declared_length_over_limit_is_rejected_before_reading() -> None:
    app = BodySizeLimitMiddleware(echo_app, max_body_bytes=10)
    sent = await call(app, [b"x"], [(b"content-length", b"11")])
    assert sent[0]["status"] == 413


async def test_invalid_content_length_is_rejected() -> None:
    app = BodySizeLimitMiddleware(echo_app, max_body_bytes=10)
    sent = await call(app, [b"x"], [(b"content-length", b"nope")])
    assert sent[0]["status"] == 413


async def test_chunked_body_over_limit_is_aborted() -> None:
    app = BodySizeLimitMiddleware(echo_app, max_body_bytes=10)
    sent = await call(app, [b"123456", b"789012", b"345"])  # no Content-Length
    assert sent[0]["status"] == 413
    assert len([m for m in sent if m["type"] == "http.response.start"]) == 1


async def test_non_http_scopes_are_ignored() -> None:
    seen: list[str] = []

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    app = BodySizeLimitMiddleware(lifespan_app, max_body_bytes=1)
    await app({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]
