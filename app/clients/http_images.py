"""SSRF-aware download of images from external URLs (posters, album covers, HA pictures).

Every URL - including each redirect target - is checked with
:func:`app.core.security.validate_external_url` before it is contacted, redirects are
followed manually (at most :data:`MAX_REDIRECTS` hops), the body is read with a hard size
limit and the bytes must be a real, supported image. Error messages never contain the URL
(Home Assistant picture URLs embed access tokens), only its host.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from app import __version__
from app.core.security import (
    InvalidImageError,
    PayloadTooLargeError,
    UnsafeURLError,
    URLPolicy,
    read_limited,
    validate_external_url,
    verify_image_bytes,
)
from app.providers.base import ProviderUnavailableError

logger = logging.getLogger(__name__)

MAX_REDIRECTS: Final = 5
CONNECT_TIMEOUT_S: Final = 5.0
READ_TIMEOUT_S: Final = 20.0
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
#: Generic client identification (no personal data); MusicBrainz requires a meaningful one.
USER_AGENT: Final = (
    f"SmartFrameEngine/{__version__} (+https://github.com/ant1fr/Smart-Frame-Engine)"
)


class ImageFetchError(ProviderUnavailableError):
    """The image could not be downloaded (unsafe URL, HTTP error, not an image...)."""


def describe_host(url: str) -> str:
    """The host of ``url`` for log and error messages (never the path or query)."""
    try:
        return urlsplit(url).hostname or "unknown host"
    except ValueError:
        return "invalid URL"


class ImageFetcher:
    """Downloads images from untrusted URLs under an outbound URL policy."""

    def __init__(
        self,
        policy: URLPolicy,
        max_bytes: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the fetcher.

        Args:
            policy: Outbound URL policy (``BLOCK_PRIVATE_URLS`` / ``URL_ALLOWLIST``).
            max_bytes: Maximum accepted body size.
            transport: Optional transport (tests).
        """
        self._policy = policy
        self._max_bytes = max_bytes
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
            transport=transport,
        )

    @property
    def max_bytes(self) -> int:
        """The size limit applied to downloads."""
        return self._max_bytes

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def fetch(self, url: str) -> bytes:
        """Download ``url`` and return verified image bytes.

        Raises:
            ImageFetchError: The URL is unsafe, the server failed, the body is too large or
                it is not a supported image.
        """
        host = describe_host(url)
        try:
            current = await validate_external_url(url, self._policy)
        except UnsafeURLError as exc:
            raise ImageFetchError(f"Refusing to fetch an image from {host}: {exc}") from None
        for _ in range(MAX_REDIRECTS + 1):
            host = describe_host(current)
            try:
                async with self._http.stream("GET", current) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise ImageFetchError(f"{host} answered a redirect without a target")
                        current = await validate_external_url(
                            urljoin(current, location), self._policy
                        )
                        continue
                    if response.status_code != 200:
                        raise ImageFetchError(
                            f"{host} answered HTTP {response.status_code} for the image"
                        )
                    declared = response.headers.get("content-length")
                    data = await read_limited(
                        response.aiter_bytes(),
                        self._max_bytes,
                        declared_length=int(declared) if declared and declared.isdigit() else None,
                    )
            except UnsafeURLError as exc:
                raise ImageFetchError(f"Refusing to follow a redirect from {host}: {exc}") from None
            except PayloadTooLargeError as exc:
                raise ImageFetchError(f"The image from {host} is too large ({exc})") from None
            except httpx.TimeoutException:
                raise ImageFetchError(f"{host} did not respond in time", timeout=True) from None
            except httpx.HTTPError as exc:
                raise ImageFetchError(
                    f"Cannot download the image from {host} ({type(exc).__name__})"
                ) from None
            try:
                verify_image_bytes(data)
            except InvalidImageError as exc:
                raise ImageFetchError(f"{host} did not return a usable image ({exc})") from None
            return data
        raise ImageFetchError(f"Too many redirects while fetching the image from {host}")


def decode_base64_image(value: str, max_bytes: int) -> bytes:
    """Decode a base64 image (optionally a ``data:image/...;base64,`` URI) and verify it.

    Raises:
        PayloadTooLargeError: The decoded image would exceed ``max_bytes``.
        InvalidImageError: The value is not valid base64 or not a supported image.
    """
    payload = value.strip()
    if payload.startswith("data:"):
        header, _, payload = payload.partition(",")
        if ";base64" not in header:
            raise InvalidImageError("Only base64 data URIs are supported")
    payload = "".join(payload.split())
    if len(payload) // 4 * 3 > max_bytes + 3:
        raise PayloadTooLargeError(max_bytes)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidImageError("Invalid base64 image data") from exc
    if len(data) > max_bytes:
        raise PayloadTooLargeError(max_bytes)
    verify_image_bytes(data)
    return data
