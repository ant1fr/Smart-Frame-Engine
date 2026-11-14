"""Input hardening: SSRF-aware URL validation, size-limited reads, image verification."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import socket
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

ALLOWED_IMAGE_FORMATS: Final = frozenset({"JPEG", "MPO", "PNG", "WEBP", "GIF", "TIFF", "BMP"})
MAX_IMAGE_PIXELS: Final = 120_000_000
UPLOAD_CHUNK_SIZE: Final = 64 * 1024


class UnsafeURLError(ValueError):
    """The URL is malformed, uses a forbidden scheme, or targets a blocked address."""


class PayloadTooLargeError(ValueError):
    """The payload exceeds the configured size limit."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"Payload exceeds the {limit // (1024 * 1024)} MB limit")
        self.limit = limit


class InvalidImageError(ValueError):
    """The payload is not a supported, decodable image."""


@dataclass(frozen=True, slots=True)
class URLPolicy:
    """Outbound URL policy.

    Attributes:
        block_private: Reject hosts resolving to non-public addresses.
        networks: Networks exempt from ``block_private``.
        hostnames: Hostnames (lower-case) exempt from ``block_private``.
    """

    block_private: bool = False
    networks: tuple[IPNetwork, ...] = field(default_factory=tuple)
    hostnames: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_entries(cls, block_private: bool, entries: Iterable[str]) -> URLPolicy:
        """Build a policy from allowlist entries (CIDRs, IPs or hostnames)."""
        networks: list[IPNetwork] = []
        hostnames: set[str] = set()
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                hostnames.add(entry.lower().rstrip("."))
        return cls(block_private, tuple(networks), frozenset(hostnames))

    def allows_address(self, address: IPAddress) -> bool:
        """Whether ``address`` may be contacted under this policy."""
        if not self.block_private:
            return True
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if any(address in network for network in self.networks):
            return True
        return is_public_address(address)


def is_public_address(address: IPAddress) -> bool:
    """``True`` for globally routable unicast addresses only."""
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


async def _resolve(host: str, port: int) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Cannot resolve host {host!r}") from exc
    addresses: list[IPAddress] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]  # drop IPv6 zone index
        addresses.append(ipaddress.ip_address(raw))
    return addresses


async def validate_external_url(url: str, policy: URLPolicy) -> str:
    """Validate a user-supplied URL before the server fetches it.

    Checks: ``http``/``https`` scheme, a host is present, no embedded credentials and,
    when ``policy.block_private`` is enabled, every resolved address is public or
    explicitly allowlisted.

    Note:
        Resolution happens here, before the fetch; this narrows but cannot fully close
        DNS-rebinding windows. Fetchers should also disable redirects to other hosts or
        re-validate each redirect target.

    Returns:
        The stripped URL.

    Raises:
        UnsafeURLError: If the URL is rejected.
    """
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("Malformed URL") from exc
    if parts.scheme not in {"http", "https"}:
        raise UnsafeURLError("Only http:// and https:// URLs are allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    if not policy.block_private or host in policy.hostnames:
        return url

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = await _resolve(host, port or (443 if parts.scheme == "https" else 80))
    if not addresses:
        raise UnsafeURLError(f"Cannot resolve host {host!r}")
    for address in addresses:
        if not policy.allows_address(address):
            raise UnsafeURLError(f"Host {host!r} resolves to a blocked address")
    return url


async def read_limited(
    chunks: AsyncIterable[bytes], max_bytes: int, *, declared_length: int | None = None
) -> bytes:
    """Collect an async byte stream, aborting as soon as it exceeds ``max_bytes``.

    Args:
        chunks: Async byte stream (e.g. ``httpx.Response.aiter_bytes()``).
        max_bytes: Maximum accepted size.
        declared_length: Optional ``Content-Length`` for early rejection.

    Raises:
        PayloadTooLargeError: If the limit is exceeded.
    """
    if declared_length is not None and declared_length > max_bytes:
        raise PayloadTooLargeError(max_bytes)
    buffer = bytearray()
    async for chunk in chunks:
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise PayloadTooLargeError(max_bytes)
    return bytes(buffer)


class AsyncReadable(Protocol):
    """Anything with an async ``read(size)`` (e.g. ``fastapi.UploadFile``)."""

    async def read(self, size: int = -1) -> bytes: ...


async def read_upload(file: AsyncReadable, max_bytes: int) -> bytes:
    """Read an uploaded file in chunks, enforcing ``max_bytes``.

    Raises:
        PayloadTooLargeError: If the upload is larger than ``max_bytes``.
    """

    async def _iterate() -> AsyncIterable[bytes]:
        while chunk := await file.read(UPLOAD_CHUNK_SIZE):
            yield chunk

    return await read_limited(_iterate(), max_bytes)


def verify_image_bytes(data: bytes, *, max_pixels: int = MAX_IMAGE_PIXELS) -> str:
    """Check that ``data`` really is a supported image (magic bytes + structure).

    Returns:
        The detected Pillow format name (e.g. ``"JPEG"``).

    Raises:
        InvalidImageError: If the data is not a supported, structurally valid image or
            its pixel count exceeds ``max_pixels``.
    """
    if not data:
        raise InvalidImageError("Empty file")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format or ""
            width, height = image.size
            if image_format not in ALLOWED_IMAGE_FORMATS:
                raise InvalidImageError(f"Unsupported image format: {image_format or 'unknown'}")
            if width * height > max_pixels:
                raise InvalidImageError(f"Image too large: {width}x{height} pixels")
            image.verify()
    except InvalidImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise InvalidImageError("Not a valid image") from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise InvalidImageError("Corrupt or truncated image") from exc
    return image_format
