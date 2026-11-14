from __future__ import annotations

import io
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
from PIL import Image

from app.core.security import (
    InvalidImageError,
    PayloadTooLargeError,
    UnsafeURLError,
    URLPolicy,
    read_limited,
    read_upload,
    validate_external_url,
    verify_image_bytes,
)


def fake_resolver(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    async def _getaddrinfo(
        self: Any, host: str, port: int, **_: Any
    ) -> list[tuple[Any, Any, Any, str, tuple[str, int]]]:
        if host not in mapping:
            raise socket.gaierror("not found")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in mapping[host]]

    import asyncio

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", _getaddrinfo)


OPEN = URLPolicy()
STRICT = URLPolicy.from_entries(True, [])


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a.jpg",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://",
        "http://user:pass@example.com/a.jpg",
        "http://example.com:99999/",
    ],
)
async def test_rejects_malformed_or_forbidden_urls(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_external_url(url, OPEN)


async def test_private_addresses_allowed_by_default() -> None:
    url = "http://192.168.1.10:8123/local/poster.jpg"
    assert await validate_external_url(f"  {url} ", OPEN) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:192.168.0.1]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
    ],
)
async def test_strict_policy_blocks_non_public_ips(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_external_url(url, STRICT)


async def test_strict_policy_checks_resolved_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_resolver(
        monkeypatch,
        {"public.example": ["93.184.216.34"], "sneaky.example": ["93.184.216.34", "10.1.2.3"]},
    )
    assert await validate_external_url("https://public.example/a.jpg", STRICT)
    with pytest.raises(UnsafeURLError, match="blocked"):
        await validate_external_url("https://sneaky.example/a.jpg", STRICT)
    with pytest.raises(UnsafeURLError, match="resolve"):
        await validate_external_url("https://missing.example/a.jpg", STRICT)


async def test_allowlist_networks_and_hostnames(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_resolver(monkeypatch, {"ha.lan": ["192.168.1.20"], "nas.lan": ["192.168.2.5"]})
    policy = URLPolicy.from_entries(True, ["192.168.1.0/24", "Immich.LAN", "", "  "])
    assert policy.hostnames == frozenset({"immich.lan"})
    assert await validate_external_url("http://ha.lan:8123/x.jpg", policy)
    assert await validate_external_url("http://immich.lan/x.jpg", policy)
    with pytest.raises(UnsafeURLError):
        await validate_external_url("http://nas.lan/x.jpg", policy)


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_read_limited() -> None:
    assert await read_limited(_stream(b"ab", b"cd"), 4) == b"abcd"
    with pytest.raises(PayloadTooLargeError):
        await read_limited(_stream(b"ab", b"cde"), 4)
    with pytest.raises(PayloadTooLargeError):
        await read_limited(_stream(), 4, declared_length=5)


class FakeUpload:
    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


async def test_read_upload_enforces_limit() -> None:
    payload = b"x" * (200 * 1024)
    assert await read_upload(FakeUpload(payload), len(payload)) == payload
    with pytest.raises(PayloadTooLargeError):
        await read_upload(FakeUpload(payload), len(payload) - 1)


def _image_bytes(fmt: str, size: tuple[int, int] = (8, 6)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF", "BMP"])
def test_verify_image_accepts_supported_formats(fmt: str) -> None:
    assert verify_image_bytes(_image_bytes(fmt)) == fmt


@pytest.mark.parametrize(
    "data",
    [b"", b"GIF89a-not-really", b"<html>hello</html>", b"%PDF-1.7\n", b"\x89PNG\r\n\x1a\n"],
)
def test_verify_image_rejects_non_images(data: bytes) -> None:
    with pytest.raises(InvalidImageError):
        verify_image_bytes(data)


def test_verify_image_rejects_truncated_png() -> None:
    data = _image_bytes("PNG", (64, 64))
    with pytest.raises(InvalidImageError):
        verify_image_bytes(data[: len(data) // 2])


def test_verify_image_rejects_unsupported_format() -> None:
    with pytest.raises(InvalidImageError, match="Unsupported"):
        verify_image_bytes(_image_bytes("ICO", (16, 16)))


def test_verify_image_enforces_pixel_limit() -> None:
    with pytest.raises(InvalidImageError, match="too large"):
        verify_image_bytes(_image_bytes("PNG", (100, 100)), max_pixels=9_999)
