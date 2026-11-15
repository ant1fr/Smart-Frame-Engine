"""mDNS / Zeroconf: advertise ``_smartframe._tcp.local.`` so other devices can find the engine.

The record carries TXT properties ``version``, ``path`` (``ROOT_PATH``) and ``auth``
(``none``, ``key`` or ``password``). Advertising is best effort: any failure (no multicast
in a bridged container, no network...) is logged and ignored, the engine runs regardless.
In Docker, mDNS needs ``network_mode: host``.
"""

from __future__ import annotations

import logging
import re
import socket
from collections.abc import Callable
from typing import Any, Final

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger(__name__)

SERVICE_TYPE: Final = "_smartframe._tcp.local."
_LABEL_RE: Final = re.compile(r"[^A-Za-z0-9 _-]+")


def local_addresses() -> list[str]:
    """IPv4 addresses of this host's outgoing interface (no packet is sent)."""
    addresses: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        addresses.append(str(probe.getsockname()[0]))
    except OSError:
        pass
    finally:
        probe.close()
    return [a for a in addresses if not a.startswith("0.")] or ["127.0.0.1"]


class ZeroconfAdvertiser:
    """Registers and unregisters the service; never raises."""

    def __init__(
        self,
        *,
        port: int,
        version: str,
        root_path: str,
        auth: str,
        instance: str = "Smart Frame Engine",
        zeroconf_factory: Callable[[], Any] = lambda: AsyncZeroconf(ip_version=IPVersion.V4Only),
        addresses: Callable[[], list[str]] = local_addresses,
    ) -> None:
        label = _LABEL_RE.sub("", instance).strip()[:60] or "Smart Frame Engine"
        host = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-") or "smartframe"
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{label}.{SERVICE_TYPE}",
            port=port,
            properties={"version": version, "path": root_path or "/", "auth": auth},
            server=f"{host}.local.",
            parsed_addresses=addresses(),
        )
        self._factory = zeroconf_factory
        self._zeroconf: Any | None = None

    @property
    def active(self) -> bool:
        """Whether the service is currently advertised."""
        return self._zeroconf is not None

    async def start(self) -> bool:
        """Advertise the service; ``False`` (logged) when it could not be registered."""
        try:
            zeroconf = self._factory()
            await zeroconf.async_register_service(self._info)
        except Exception as exc:
            logger.warning("Zeroconf advertising disabled: %s: %s", type(exc).__name__, exc)
            return False
        self._zeroconf = zeroconf
        logger.info("Advertising %s on port %s (mDNS)", self._info.name, self._info.port)
        return True

    async def stop(self) -> None:
        """Withdraw the service (idempotent)."""
        zeroconf, self._zeroconf = self._zeroconf, None
        if zeroconf is None:
            return
        try:
            await zeroconf.async_unregister_service(self._info)
            await zeroconf.async_close()
        except Exception as exc:
            logger.warning("Zeroconf shutdown failed: %s: %s", type(exc).__name__, exc)
