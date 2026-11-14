"""Async client for the Fraimic frame's local REST API.

Endpoints (official Fraimic REST guide, no authentication, LAN only):

* ``GET /api/info`` - status JSON: ``battery.percent``, ``battery.charging``,
  ``wifi.rssi``, ``wifi.ip``, ``firmware_version``. Used as the availability ping.
* ``POST /api/image`` - raw ``.bin`` frame buffer, ``Content-Type:
  application/octet-stream``. The frame displays it.

Both paths are configurable (``fraimic.status_path`` / ``fraimic.upload_path``). The
frame is unreachable while in deep sleep; callers decide whether and when to retry.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx
from pydantic import BaseModel, Field, JsonValue

from app.core.config_models import FraimicConfig

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S: Final = 5.0
INFO_TIMEOUT_S: Final = 8.0
_ERROR_BODY_PREVIEW: Final = 200
_MDNS_HINT: Final = (
    " (.local names rely on mDNS, which usually does not resolve inside Docker "
    "bridge networks; configure the frame's IP address instead)"
)


class FraimicError(Exception):
    """Base class for frame communication errors (messages are safe to show users)."""


class FraimicNotConfiguredError(FraimicError):
    """No frame host is configured."""


class FraimicUnreachableError(FraimicError):
    """The frame could not be reached (asleep, offline, DNS failure, timeout)."""


class FraimicResponseError(FraimicError):
    """The frame answered with an error status or an unexpected payload."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FraimicPayloadError(FraimicError):
    """The ``.bin`` payload does not have the size the panel expects."""


class FrameInfo(BaseModel):
    """Frame telemetry parsed leniently from ``GET /api/info``."""

    battery_percent: float | None = Field(default=None, description="Battery level (%)")
    charging: bool | None = Field(default=None, description="Whether the battery is charging")
    rssi: int | None = Field(default=None, description="Wi-Fi signal strength (dBm)")
    ip: str | None = Field(default=None, description="Frame IP address")
    firmware_version: str | None = Field(default=None, description="Firmware version")
    raw: dict[str, JsonValue] = Field(default_factory=dict, description="Full JSON response")

    @classmethod
    def from_payload(cls, payload: Any) -> FrameInfo:
        """Extract known fields, tolerating missing keys and unexpected types."""
        if not isinstance(payload, dict):
            raise FraimicResponseError("Frame info is not a JSON object")
        battery = payload.get("battery")
        wifi = payload.get("wifi")
        battery = battery if isinstance(battery, dict) else {}
        wifi = wifi if isinstance(wifi, dict) else {}
        return cls(
            battery_percent=_as_float(battery.get("percent")),
            charging=battery.get("charging") if isinstance(battery.get("charging"), bool) else None,
            rssi=_as_int(wifi.get("rssi")),
            ip=wifi.get("ip") if isinstance(wifi.get("ip"), str) else None,
            firmware_version=_as_str(payload.get("firmware_version")),
            raw=payload,
        )


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Outcome of a successful ``.bin`` upload."""

    status_code: int
    bytes_sent: int
    duration_s: float


class FraimicClient:
    """Thin async wrapper around the frame's HTTP API."""

    def __init__(
        self,
        *,
        base_url: str,
        upload_path: str = "/api/image",
        status_path: str = "/api/info",
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create a client.

        Args:
            base_url: Frame base URL (e.g. ``http://192.168.1.50``); empty = unconfigured.
            upload_path: Endpoint receiving the raw ``.bin``.
            status_path: Endpoint returning the frame status JSON.
            timeout_s: Read/write timeout for uploads.
            transport: Optional custom transport (tests).
        """
        self.base_url = base_url.rstrip("/")
        self.upload_path = upload_path
        self.status_path = status_path
        self.timeout_s = timeout_s
        # No keep-alive: the frame drops Wi-Fi in deep sleep, so pooled sockets go stale.
        self._http = httpx.AsyncClient(
            transport=transport,
            limits=httpx.Limits(max_keepalive_connections=0),
            follow_redirects=False,
            headers={"User-Agent": "smart-frame-engine"},
        )

    @classmethod
    def from_config(
        cls, config: FraimicConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> FraimicClient:
        """Build a client from the ``fraimic`` configuration section."""
        return cls(
            base_url=config.base_url,
            upload_path=config.upload_path,
            status_path=config.status_path,
            timeout_s=config.request_timeout_s,
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        """Whether a frame host is configured."""
        return bool(self.base_url)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def get_info(self) -> FrameInfo:
        """Fetch frame telemetry; doubles as the availability ping.

        Raises:
            FraimicNotConfiguredError, FraimicUnreachableError, FraimicResponseError
        """
        response = await self._request(
            "GET",
            self.status_path,
            timeout=httpx.Timeout(INFO_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FraimicResponseError(
                "Frame info is not valid JSON", response.status_code
            ) from exc
        return FrameInfo.from_payload(payload)

    async def upload_bin(self, data: bytes, *, expected_size: int) -> UploadResult:
        """Send a ``.bin`` frame buffer to the frame.

        Args:
            data: Encoded frame buffer.
            expected_size: Exact size required by the configured panel.

        Raises:
            FraimicPayloadError: If ``data`` has the wrong size (nothing is sent).
            FraimicNotConfiguredError, FraimicUnreachableError, FraimicResponseError
        """
        if len(data) != expected_size:
            raise FraimicPayloadError(
                f"Refusing to upload {len(data)} bytes; the panel expects {expected_size}"
            )
        started = time.monotonic()
        response = await self._request(
            "POST",
            self.upload_path,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx.Timeout(self.timeout_s, connect=CONNECT_TIMEOUT_S),
        )
        duration = time.monotonic() - started
        logger.info(
            "Uploaded %d bytes to the frame in %.1fs (HTTP %d)",
            len(data),
            duration,
            response.status_code,
        )
        return UploadResult(response.status_code, len(data), duration)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.base_url:
            raise FraimicNotConfiguredError("No Fraimic frame host is configured")
        url = f"{self.base_url}{path}"
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise FraimicUnreachableError(
                f"Frame did not respond in time ({type(exc).__name__})"
            ) from exc
        except httpx.ConnectError as exc:
            raise FraimicUnreachableError(self._connect_message(exc)) from exc
        except httpx.HTTPError as exc:
            raise FraimicUnreachableError(
                f"Frame connection failed ({type(exc).__name__})"
            ) from exc
        if not response.is_success:
            snippet = response.text[:_ERROR_BODY_PREVIEW].strip()
            raise FraimicResponseError(
                f"Frame returned HTTP {response.status_code}" + (f": {snippet}" if snippet else ""),
                response.status_code,
            )
        return response

    def _connect_message(self, exc: httpx.ConnectError) -> str:
        host = httpx.URL(self.base_url).host
        message = f"Cannot connect to the frame at {host}"
        if host.endswith(".local"):
            message += _MDNS_HINT
        detail = str(exc).strip()
        return f"{message}: {detail}" if detail else message
