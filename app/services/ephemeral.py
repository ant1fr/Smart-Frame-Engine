"""Lifetime of ephemeral images (now playing, movie posters).

An ephemeral image is pushed for a while and must not outlive what it illustrates: once
the song or the movie is over, the previous non-ephemeral image (the *base*) comes back.

:class:`EphemeralService` handles both ways an ephemeral image ends:

* **Expiry** - a timer sleeps until the ``expires_at`` of the current ephemeral image. The
  delivery service also drops an expired image that is still queued for a sleeping frame
  and calls :meth:`EphemeralService.handle_dropped`.
* **Cancellation** - ``DELETE /api/queue/ephemeral`` (e.g. Home Assistant on pause/stop)
  drops the queued image and reverts immediately.

Reverting restores ``current.*`` from ``base.*``, re-sends the base image only when the
frame may not be showing it already (a sleeping frame that never received the ephemeral
image is left alone), and lifts the scheduler hold that protected the ephemeral image.
When there is no base image yet, a background refresh replays the active rule instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, Field

from app.services.current_image import CurrentImageMeta, CurrentImageStore
from app.services.delivery import DeliveryOutcome, DeliveryService, DropReason, PendingUpload
from app.services.display import DisplayService, RevertOutcome

logger = logging.getLogger(__name__)

EndReason = Literal["expired", "cancelled"]
Clock = Callable[[], datetime]
HoldClearer = Callable[[], Awaitable[None]]
Refresher = Callable[[], bool]

#: The timer never sleeps longer than this, so clock jumps (suspend, NTP) are caught up.
MAX_TIMER_SLEEP_S: Final = 300.0
TIMER_ERROR_PAUSE_S: Final = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EphemeralView(BaseModel):
    """The ephemeral image currently shown or queued (``GET /api/queue``, ``/api/status``)."""

    active: bool = Field(description="Whether the current image is ephemeral")
    source: str | None = None
    title: str | None = None
    template: str | None = None
    expires_at: AwareDatetime | None = Field(
        default=None, description="When the previous image comes back (null = until replaced)"
    )
    pending: bool = Field(description="Whether an ephemeral image waits for a sleeping frame")
    has_base: bool = Field(description="Whether a previous image exists to come back to")


class CancelOutcome(BaseModel):
    """Result of ``DELETE /api/queue/ephemeral``."""

    cancelled: bool = Field(description="An ephemeral image was queued or shown")
    dropped_pending: bool = Field(description="A queued ephemeral upload was discarded")
    reverted: bool = Field(description="The previous image is the current image again")
    pushed_base: bool = Field(description="The previous image was re-sent (or queued)")
    refreshing: bool = Field(
        description="No previous image existed: a background refresh was started instead"
    )
    delivery: DeliveryOutcome | None = None
    image: CurrentImageMeta | None = Field(default=None, description="The current image now")


class EphemeralService:
    """Ends ephemeral images on expiry or cancellation and restores the previous image."""

    def __init__(
        self,
        current: CurrentImageStore,
        display: DisplayService,
        delivery: DeliveryService,
        *,
        clear_hold: HoldClearer | None = None,
        refresh: Refresher | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        """Create the service (call :meth:`start`).

        Args:
            current: The image slots.
            display: Performs the revert (restore + conditional re-send).
            delivery: The upload queue (cancellation, expired-image drops).
            clear_hold: Lifts the scheduler hold that protected the ephemeral image.
            refresh: Starts a background refresh when there is no previous image; returns
                whether one was started.
            clock: Returns the current aware time.
        """
        self._current = current
        self._display = display
        self._delivery = delivery
        self._clear_hold = clear_hold
        self._refresh = refresh
        self._clock = clock
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ended: str | None = None  # SHA-256 of the last ephemeral image ended

    # --- Lifecycle ------------------------------------------------------------------------
    async def start(self) -> None:
        """Hook into the display and delivery services and start the expiry timer.

        Call it before :meth:`DeliveryService.start` so an expired image restored from
        disk is reverted as soon as the delivery worker drops it.
        """
        self._display.add_listener(self._on_display)
        self._delivery.set_drop_handler(self.handle_dropped)
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ephemeral-expiry-timer")

    async def stop(self) -> None:
        """Stop the expiry timer."""
        task, self._task = self._task, None
        self._delivery.set_drop_handler(None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def rearm(self) -> None:
        """Recompute the next expiry (call after the current image changed)."""
        self._wake.set()

    # --- Read access ----------------------------------------------------------------------
    def next_expiry(self) -> datetime | None:
        """When the current ephemeral image expires (``None`` when nothing will)."""
        meta = self._current.meta
        if meta is None or not meta.ephemeral or meta.expires_at is None:
            return None
        if meta.sha256 == self._ended:
            return None  # already ended (e.g. nothing to revert to)
        return meta.expires_at

    def view(self) -> EphemeralView:
        """The ephemeral image currently shown or queued."""
        meta = self._current.meta
        pending = self._delivery.pending
        active = meta is not None and meta.ephemeral
        return EphemeralView(
            active=active,
            source=meta.source if active and meta else None,
            title=meta.title if active and meta else None,
            template=meta.template if active and meta else None,
            expires_at=meta.expires_at if active and meta else None,
            pending=pending is not None and pending.ephemeral,
            has_base=self._current.base_meta is not None,
        )

    # --- Operations -----------------------------------------------------------------------
    async def cancel(self) -> CancelOutcome:
        """End the ephemeral image now: drop it from the queue and restore the previous one."""
        async with self._lock:
            dropped = await self._delivery.cancel_ephemeral()
            meta = self._current.meta
            if meta is None or not meta.ephemeral:
                return CancelOutcome(
                    cancelled=dropped is not None,
                    dropped_pending=dropped is not None,
                    reverted=False,
                    pushed_base=False,
                    refreshing=False,
                    image=meta,
                )
            revert, refreshing = await self._end_locked(meta, "cancelled")
        self.rearm()
        return CancelOutcome(
            cancelled=True,
            dropped_pending=dropped is not None,
            reverted=revert.reverted,
            pushed_base=revert.pushed_base,
            refreshing=refreshing,
            delivery=revert.delivery,
            image=revert.image,
        )

    async def expire_if_due(self) -> RevertOutcome | None:
        """Revert the current ephemeral image if its expiry has passed.

        Returns:
            What the revert did, or ``None`` when nothing was due.
        """
        async with self._lock:
            meta = self._current.meta
            if meta is None or meta.sha256 == self._ended or not meta.expired(self._clock()):
                return None
            revert, _ = await self._end_locked(meta, "expired")
        self.rearm()
        return revert

    async def handle_dropped(self, pending: PendingUpload, reason: DropReason) -> None:
        """Delivery drop handler: an expired image was discarded before reaching the frame."""
        async with self._lock:
            meta = self._current.meta
            if meta is None or meta.sha256 != pending.sha256 or meta.sha256 == self._ended:
                return  # a newer image is current: nothing to undo
            await self._end_locked(meta, reason)
        self.rearm()

    # --- Internals ------------------------------------------------------------------------
    async def _end_locked(
        self, meta: CurrentImageMeta, reason: EndReason
    ) -> tuple[RevertOutcome, bool]:
        """Restore the previous image (caller holds ``_lock``)."""
        revert = await self._display.revert_to_base(only_if_current=meta.sha256)
        if revert.stale:
            return revert, False
        self._ended = meta.sha256
        if self._clear_hold is not None:
            await self._clear_hold()
        refreshing = False
        if not revert.reverted and self._refresh is not None:
            refreshing = self._refresh()
        logger.info(
            "Ephemeral image %r %s: %s",
            meta.title or meta.source,
            reason,
            "previous image restored"
            if revert.reverted
            else ("refreshing" if refreshing else "no previous image to restore"),
        )
        return revert, refreshing

    def _on_display(self, meta: CurrentImageMeta) -> None:
        if meta.sha256 != self._ended:
            self._ended = None
        self.rearm()

    def _timeout(self) -> float:
        expiry = self.next_expiry()
        if expiry is None:
            return MAX_TIMER_SLEEP_S
        return min(MAX_TIMER_SLEEP_S, max(0.0, (expiry - self._clock()).total_seconds()))

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._timeout())
            self._wake.clear()
            try:
                await self.expire_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ending an expired ephemeral image failed")
                await asyncio.sleep(TIMER_ERROR_PAUSE_S)
