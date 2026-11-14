"""Delivery of ``.bin`` frame buffers to the Fraimic frame, for both power modes.

* **continuous** (mains powered): a new image is pushed immediately. If the push fails,
  the image stays queued and is retried with exponential backoff (30 s .. 15 min).
* **deepsleep** (battery): the frame is unreachable most of the time. A new image is
  queued (``/data/queue/pending.bin`` + ``pending.json``, written atomically) and the
  background worker probes ``GET /api/info`` every ``poll_interval_s`` seconds. As soon
  as the frame answers, the image is uploaded. ``POST /api/fraimic/wakeup`` (e.g. from
  a Home Assistant automation) triggers an immediate attempt.

Only the newest image is kept: submitting a new image replaces any pending one. The
queue survives restarts. Uploads are serialized, so a wakeup racing with the worker
never pushes the same image twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from app.clients.fraimic import (
    FraimicClient,
    FraimicError,
    FraimicResponseError,
    FraimicUnreachableError,
    FrameInfo,
)
from app.core.atomic import atomic_write_bytes, remove_quietly
from app.core.config_models import PANEL_SPECS, FraimicConfig, PanelModel, PowerMode, RuntimeConfig
from app.core.config_store import ConfigStore
from app.core.json_store import atomic_write_json, read_json

logger = logging.getLogger(__name__)

PENDING_BIN: Final = "pending.bin"
PENDING_META: Final = "pending.json"
RETRY_BASE_S: Final = 30.0
RETRY_MAX_S: Final = 900.0
TELEMETRY_INTERVAL_S: Final = 300.0
WORKER_ERROR_PAUSE_S: Final = 5.0

DeliveryReason = Literal["submit", "force", "wakeup", "worker"]
Clock = Callable[[], datetime]
ClientFactory = Callable[[FraimicConfig], FraimicClient]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _online_after(exc: FraimicError) -> bool | None:
    """Frame reachability implied by an error (``None`` when it says nothing)."""
    if isinstance(exc, FraimicUnreachableError):
        return False
    if isinstance(exc, FraimicResponseError):
        return True  # it answered, just not successfully
    return None


class PendingUpload(BaseModel):
    """Metadata of the image waiting for the frame (persisted as ``pending.json``)."""

    sha256: str
    size: int
    panel: PanelModel
    created_at: AwareDatetime
    source: str | None = None
    title: str | None = None
    attempts: int = Field(default=0, description="Failed delivery attempts so far")
    last_attempt_at: AwareDatetime | None = None
    last_error: str | None = None
    next_attempt_at: AwareDatetime | None = None


class LastDelivery(BaseModel):
    """The most recent successful upload."""

    sha256: str
    delivered_at: AwareDatetime
    duration_s: float
    bytes: int
    reason: DeliveryReason


class FrameTelemetry(BaseModel):
    """Last known frame state (updated whenever the frame is contacted)."""

    online: bool | None = Field(default=None, description="None = never contacted")
    last_seen: AwareDatetime | None = None
    last_checked: AwareDatetime | None = None
    last_error: str | None = None
    info: FrameInfo | None = None


class DeliveryOutcome(BaseModel):
    """Result of a submit, push or flush."""

    status: Literal["delivered", "queued", "nothing_pending"]
    reason: DeliveryReason
    sha256: str | None = None
    error: str | None = None
    duration_s: float | None = None


class DeliverySnapshot(BaseModel):
    """Delivery state exposed through ``GET /api/status``."""

    power_mode: PowerMode
    pending: PendingUpload | None
    last_delivery: LastDelivery | None
    telemetry: FrameTelemetry


class DeliveryService:
    """Owns the pending upload, the frame client and the background worker."""

    def __init__(
        self,
        queue_dir: Path,
        config_store: ConfigStore,
        *,
        client_factory: ClientFactory = FraimicClient.from_config,
        clock: Clock = _utcnow,
    ) -> None:
        self._bin_path = queue_dir / PENDING_BIN
        self._meta_path = queue_dir / PENDING_META
        self._config_store = config_store
        self._client_factory = client_factory
        self._clock = clock

        self._state_lock = asyncio.Lock()  # pending files + in-memory pending state
        self._upload_lock = asyncio.Lock()  # one upload at a time
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[DeliveryOutcome]] = set()

        self._client: FraimicClient | None = None
        self._client_config: FraimicConfig | None = None
        self._pending: PendingUpload | None = None
        self._last_delivery: LastDelivery | None = None
        self._telemetry = FrameTelemetry()

    # --- Lifecycle ----------------------------------------------------------------------
    def load(self) -> PendingUpload | None:
        """Restore a pending upload from disk, discarding inconsistent leftovers."""
        raw = read_json(self._meta_path, lambda: None)
        pending: PendingUpload | None = None
        if raw is not None:
            try:
                pending = PendingUpload.model_validate(raw)
            except ValidationError:
                logger.warning("Discarding invalid %s", self._meta_path)
        if pending is not None and not self._pending_file_valid(pending):
            pending = None
        if pending is None:
            remove_quietly(self._bin_path, self._meta_path)
        else:
            logger.info("Restored pending upload %s from %s", pending.sha256[:12], pending.source)
        self._pending = pending
        return pending

    async def start(self) -> None:
        """Start the background worker and follow configuration changes."""
        self._config_store.add_listener(self._on_config_change)
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fraimic-delivery-worker")

    async def stop(self) -> None:
        """Stop the worker and close the frame client."""
        tasks = [t for t in (self._task, *self._background) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._background.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- Read access -------------------------------------------------------------------
    @property
    def pending(self) -> PendingUpload | None:
        """The image waiting for the frame, if any."""
        return self._pending

    @property
    def telemetry(self) -> FrameTelemetry:
        """Last known frame telemetry."""
        return self._telemetry

    def snapshot(self) -> DeliverySnapshot:
        """Current delivery state (no network access)."""
        return DeliverySnapshot(
            power_mode=self._config.fraimic.power_mode,
            pending=self._pending,
            last_delivery=self._last_delivery,
            telemetry=self._telemetry,
        )

    # --- Operations ------------------------------------------------------------------------
    async def submit(
        self,
        data: bytes,
        *,
        source: str | None = None,
        title: str | None = None,
        immediate: bool | None = None,
    ) -> DeliveryOutcome:
        """Queue ``data`` for the frame, replacing any pending image.

        Args:
            data: Encoded ``.bin`` for the configured panel.
            source: Short description of where the image came from.
            title: Optional human-readable title.
            immediate: Attempt the upload now. ``None`` follows the power mode
                (``continuous`` = now, ``deepsleep`` = queue for the worker).

        Raises:
            ValueError: If ``data`` does not match the configured panel's size.
        """
        config = self._config
        spec = config.fraimic.spec
        if len(data) != spec.bin_size:
            raise ValueError(f"{spec.label} needs a {spec.bin_size}-byte .bin, got {len(data)}")
        pending = PendingUpload(
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            panel=spec.key,
            created_at=self._clock(),
            source=source,
            title=title,
        )
        async with self._state_lock:
            await asyncio.to_thread(self._write_pending, data, pending)
            self._pending = pending
        logger.info("Queued image %s for the frame (%s)", pending.sha256[:12], source or "unknown")

        attempt_now = config.fraimic.power_mode == "continuous" if immediate is None else immediate
        if attempt_now:
            return await self.flush("force" if immediate else "submit")
        self._wake.set()
        return DeliveryOutcome(status="queued", reason="submit", sha256=pending.sha256)

    async def flush(self, reason: DeliveryReason = "wakeup") -> DeliveryOutcome:
        """Try to deliver the pending image now (no-op when nothing is pending)."""
        async with self._upload_lock:
            async with self._state_lock:
                pending = self._pending
                data = await asyncio.to_thread(self._read_pending, pending) if pending else None
                if pending is not None and data is None:
                    await asyncio.to_thread(remove_quietly, self._bin_path, self._meta_path)
                    self._pending = None
            if pending is None or data is None:
                return DeliveryOutcome(status="nothing_pending", reason=reason)

            client = await self._get_client()
            try:
                await self._probe(client)
                result = await client.upload_bin(data, expected_size=pending.size)
            except FraimicError as exc:
                await self._record_failure(pending, exc, reason)
                return DeliveryOutcome(
                    status="queued", reason=reason, sha256=pending.sha256, error=str(exc)
                )

            now = self._clock()
            self._telemetry = self._telemetry.model_copy(
                update={"online": True, "last_seen": now, "last_checked": now, "last_error": None}
            )
            self._last_delivery = LastDelivery(
                sha256=pending.sha256,
                delivered_at=now,
                duration_s=round(result.duration_s, 3),
                bytes=result.bytes_sent,
                reason=reason,
            )
            async with self._state_lock:
                if self._pending is not None and self._pending.sha256 == pending.sha256:
                    await asyncio.to_thread(remove_quietly, self._bin_path, self._meta_path)
                    self._pending = None
                else:
                    self._wake.set()  # a newer image arrived during the upload
            logger.info("Delivered image %s to the frame (%s)", pending.sha256[:12], reason)
            return DeliveryOutcome(
                status="delivered",
                reason=reason,
                sha256=pending.sha256,
                duration_s=round(result.duration_s, 3),
            )

    async def wakeup(self, *, wait: bool) -> DeliveryOutcome | None:
        """Handle a "frame is awake" signal.

        Args:
            wait: Await the delivery and return its outcome; otherwise start it in the
                background and return ``None``.
        """
        if self._pending is None:
            self._spawn(self._refresh_quietly())
            return DeliveryOutcome(status="nothing_pending", reason="wakeup") if wait else None
        if wait:
            return await self.flush("wakeup")
        self._spawn(self.flush("wakeup"))
        return None

    async def refresh_info(self) -> FrameInfo:
        """Query the frame's telemetry now.

        Raises:
            FraimicError: If the frame cannot be reached or answers with an error.
        """
        client = await self._get_client()
        now = self._clock()
        try:
            info = await client.get_info()
        except FraimicError as exc:
            self._telemetry = self._telemetry.model_copy(
                update={"online": _online_after(exc), "last_checked": now, "last_error": str(exc)}
            )
            raise
        self._telemetry = FrameTelemetry(
            online=True, last_seen=now, last_checked=now, last_error=None, info=info
        )
        return info

    def wake(self) -> None:
        """Wake the worker so it re-evaluates immediately."""
        self._wake.set()

    # --- Worker ---------------------------------------------------------------------------
    def next_wait(self) -> float | None:
        """Seconds the worker sleeps before its next check (``None`` = until woken)."""
        config = self._config.fraimic
        pending = self._pending
        if pending is not None:
            if config.power_mode == "deepsleep":
                return float(config.poll_interval_s)
            if pending.next_attempt_at is None:
                return 0.0
            return max(0.0, (pending.next_attempt_at - self._clock()).total_seconds())
        if config.power_mode == "continuous" and config.base_url:
            return TELEMETRY_INTERVAL_S
        return None

    async def _run(self) -> None:
        while True:
            woken = False
            try:
                timeout = self.next_wait()
                if timeout is None:
                    await self._wake.wait()
                    woken = True
                else:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                    woken = True
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._tick(woken)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Delivery worker iteration failed")
                await asyncio.sleep(WORKER_ERROR_PAUSE_S)

    async def _tick(self, woken: bool) -> None:
        config = self._config.fraimic
        pending = self._pending
        if pending is not None:
            due = pending.next_attempt_at is None or pending.next_attempt_at <= self._clock()
            if woken or config.power_mode == "deepsleep" or due:
                await self.flush("worker")
        elif config.power_mode == "continuous" and config.base_url:
            await self._refresh_quietly()

    # --- Internals --------------------------------------------------------------------------
    @property
    def _config(self) -> RuntimeConfig:
        return self._config_store.config

    async def _get_client(self) -> FraimicClient:
        config = self._config.fraimic
        if self._client is None or self._client_config != config:
            if self._client is not None:
                await self._client.aclose()
            self._client = self._client_factory(config)
            self._client_config = config
        return self._client

    async def _probe(self, client: FraimicClient) -> None:
        """Ping the frame before uploading; only an unreachable frame aborts the upload."""
        now = self._clock()
        try:
            info = await client.get_info()
        except FraimicResponseError as exc:
            # The frame is up but its status endpoint misbehaves: still try the upload.
            logger.debug("Frame status endpoint error ignored: %s", exc)
            self._telemetry = self._telemetry.model_copy(
                update={"online": True, "last_seen": now, "last_checked": now}
            )
            return
        except FraimicUnreachableError as exc:
            self._telemetry = self._telemetry.model_copy(
                update={"online": _online_after(exc), "last_checked": now, "last_error": str(exc)}
            )
            raise
        self._telemetry = FrameTelemetry(
            online=True, last_seen=now, last_checked=now, last_error=None, info=info
        )

    async def _record_failure(
        self, pending: PendingUpload, exc: FraimicError, reason: DeliveryReason
    ) -> None:
        now = self._clock()
        attempts = pending.attempts + 1
        if self._config.fraimic.power_mode == "deepsleep":
            delay = float(self._config.fraimic.poll_interval_s)
        else:
            delay = min(RETRY_BASE_S * 2 ** (attempts - 1), RETRY_MAX_S)
        updated = pending.model_copy(
            update={
                "attempts": attempts,
                "last_attempt_at": now,
                "last_error": str(exc),
                "next_attempt_at": now + timedelta(seconds=delay),
            }
        )
        async with self._state_lock:
            if self._pending is not None and self._pending.sha256 == pending.sha256:
                self._pending = updated
                await asyncio.to_thread(
                    atomic_write_json, self._meta_path, updated.model_dump(mode="json")
                )
        if not isinstance(exc, FraimicUnreachableError):
            level = logging.WARNING
        elif reason == "worker" and attempts > 1:
            level = logging.DEBUG  # a sleeping frame is expected; avoid log spam
        else:
            level = logging.INFO
        logger.log(level, "Frame delivery failed (%s, attempt %d): %s", reason, attempts, exc)

    async def _refresh_quietly(self) -> DeliveryOutcome:
        with contextlib.suppress(FraimicError):
            await self.refresh_info()
        return DeliveryOutcome(status="nothing_pending", reason="wakeup")

    def _spawn(self, coro: Coroutine[Any, Any, DeliveryOutcome]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _on_config_change(self, config: RuntimeConfig) -> None:
        pending = self._pending
        if pending is not None and pending.panel != config.fraimic.panel:
            async with self._state_lock:
                logger.warning(
                    "Panel changed to %s; discarding pending image built for %s",
                    config.fraimic.panel,
                    pending.panel,
                )
                await asyncio.to_thread(remove_quietly, self._bin_path, self._meta_path)
                self._pending = None
        self._wake.set()

    def _write_pending(self, data: bytes, pending: PendingUpload) -> None:
        atomic_write_bytes(self._bin_path, data)
        atomic_write_json(self._meta_path, pending.model_dump(mode="json"))

    def _read_pending(self, pending: PendingUpload) -> bytes | None:
        """Read ``pending.bin``; ``None`` when it is missing or does not match ``pending``."""
        try:
            data = self._bin_path.read_bytes()
        except FileNotFoundError:
            logger.warning("pending.bin vanished; dropping the queued upload")
            return None
        if hashlib.sha256(data).hexdigest() != pending.sha256:
            logger.warning("pending.bin does not match pending.json; dropping it")
            return None
        return data

    def _pending_file_valid(self, pending: PendingUpload) -> bool:
        try:
            data = self._bin_path.read_bytes()
        except FileNotFoundError:
            logger.warning("pending.json without pending.bin; discarding the queued upload")
            return False
        if len(data) != PANEL_SPECS[pending.panel].bin_size or len(data) != pending.size:
            logger.warning("pending.bin has the wrong size; discarding it")
            return False
        if hashlib.sha256(data).hexdigest() != pending.sha256:
            logger.warning("pending.bin does not match pending.json; discarding it")
            return False
        if pending.panel != self._config.fraimic.panel:
            logger.warning("pending.bin was built for panel %s; discarding it", pending.panel)
            return False
        return True
