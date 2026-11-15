from __future__ import annotations

import base64
import io
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from PIL import Image

from app.clients.http_images import (
    USER_AGENT,
    ImageFetcher,
    ImageFetchError,
    decode_base64_image,
    describe_host,
)
from app.core.security import InvalidImageError, PayloadTooLargeError, URLPolicy


def png_bytes(size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
async def fetcher() -> AsyncIterator[ImageFetcher]:
    instance = ImageFetcher(URLPolicy(), 1_000_000)
    yield instance
    await instance.aclose()


@respx.mock
async def test_fetch_returns_verified_image_bytes(fetcher: ImageFetcher) -> None:
    route = respx.get("https://cdn.test/poster.png").respond(200, content=png_bytes())
    assert await fetcher.fetch("https://cdn.test/poster.png") == png_bytes()
    assert route.calls.last.request.headers["user-agent"] == USER_AGENT


@respx.mock
async def test_fetch_follows_redirects(fetcher: ImageFetcher) -> None:
    respx.get("https://coverartarchive.test/release/1/front").respond(
        307, headers={"location": "https://archive.test/img/front.png"}
    )
    respx.get("https://archive.test/img/front.png").respond(200, content=png_bytes())
    assert await fetcher.fetch("https://coverartarchive.test/release/1/front") == png_bytes()


@respx.mock
async def test_fetch_resolves_relative_redirects(fetcher: ImageFetcher) -> None:
    respx.get("https://cdn.test/a/old.png").respond(302, headers={"location": "../new.png"})
    respx.get("https://cdn.test/new.png").respond(200, content=png_bytes())
    assert await fetcher.fetch("https://cdn.test/a/old.png") == png_bytes()


@respx.mock
async def test_redirect_to_a_blocked_address_is_refused() -> None:
    fetcher = ImageFetcher(URLPolicy(block_private=True), 1_000_000)
    respx.get("http://93.184.216.34/poster.png").respond(
        302, headers={"location": "http://127.0.0.1:8123/api/secret.png"}
    )
    internal = respx.get("http://127.0.0.1:8123/api/secret.png").respond(200, content=png_bytes())
    try:
        with pytest.raises(ImageFetchError, match="Refusing to follow a redirect"):
            await fetcher.fetch("http://93.184.216.34/poster.png")
    finally:
        await fetcher.aclose()
    assert not internal.called


async def test_blocked_and_malformed_urls_are_refused() -> None:
    fetcher = ImageFetcher(URLPolicy(block_private=True), 1_000_000)
    try:
        with pytest.raises(ImageFetchError, match="Refusing"):
            await fetcher.fetch("http://192.168.1.10/a.png")
        with pytest.raises(ImageFetchError, match="Refusing"):
            await fetcher.fetch("ftp://cdn.test/a.png")
    finally:
        await fetcher.aclose()


@respx.mock
async def test_too_many_redirects(fetcher: ImageFetcher) -> None:
    respx.get("https://loop.test/a").respond(302, headers={"location": "https://loop.test/a"})
    with pytest.raises(ImageFetchError, match="Too many redirects"):
        await fetcher.fetch("https://loop.test/a")


@respx.mock
async def test_errors_never_leak_the_url_query(fetcher: ImageFetcher) -> None:
    respx.get(url__startswith="http://ha.test:8123/api/media_player_proxy").respond(404)
    with pytest.raises(ImageFetchError) as caught:
        await fetcher.fetch("http://ha.test:8123/api/media_player_proxy/m?token=SECRET123")
    assert "SECRET123" not in str(caught.value)
    assert "ha.test" in str(caught.value)


@respx.mock
async def test_http_errors_timeouts_and_non_images(fetcher: ImageFetcher) -> None:
    respx.get("https://cdn.test/500").respond(500)
    respx.get("https://cdn.test/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("https://cdn.test/text").respond(200, content=b"<html>nope</html>")
    respx.get("https://cdn.test/big").respond(200, content=b"x" * 2_000_000)
    with pytest.raises(ImageFetchError, match="HTTP 500"):
        await fetcher.fetch("https://cdn.test/500")
    with pytest.raises(ImageFetchError) as timeout:
        await fetcher.fetch("https://cdn.test/slow")
    assert timeout.value.timeout
    with pytest.raises(ImageFetchError, match="usable image"):
        await fetcher.fetch("https://cdn.test/text")
    with pytest.raises(ImageFetchError, match="too large"):
        await fetcher.fetch("https://cdn.test/big")


def test_decode_base64_image() -> None:
    raw = png_bytes()
    encoded = base64.b64encode(raw).decode()
    assert decode_base64_image(encoded, 1_000_000) == raw
    assert decode_base64_image(f"data:image/png;base64,{encoded}", 1_000_000) == raw
    with pytest.raises(InvalidImageError):
        decode_base64_image("not base64!!", 1_000_000)
    with pytest.raises(InvalidImageError):
        decode_base64_image(base64.b64encode(b"plain text").decode(), 1_000_000)
    with pytest.raises(PayloadTooLargeError):
        decode_base64_image(encoded, 10)
    with pytest.raises(InvalidImageError):
        decode_base64_image("data:image/png,raw", 1_000_000)


def test_describe_host() -> None:
    assert describe_host("https://image.tmdb.org/t/p/w780/a.jpg?x=1") == "image.tmdb.org"
    assert describe_host("nonsense") == "unknown host"
