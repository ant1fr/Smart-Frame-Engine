"""Upload queue inspection and ephemeral image cancellation (Home Assistant friendly).

* ``GET /api/queue`` - what waits for the frame (an expired image is never listed), the
  last delivery and the ephemeral image currently shown or queued.
* ``DELETE /api/queue/ephemeral`` - playback stopped: drop the queued ephemeral image and
  bring the previous image back right away (idempotent; always ``200``).
"""

from __future__ import annotations

from fastapi import APIRouter, Security
from pydantic import BaseModel, Field

from app.api.deps import DeliveryServiceDep, EphemeralServiceDep
from app.core.auth import api_key_scheme
from app.core.config_models import PowerMode
from app.services.delivery import LastDelivery, PendingUpload
from app.services.ephemeral import CancelOutcome, EphemeralView

router = APIRouter(prefix="/api/queue", tags=["queue"])


class QueueView(BaseModel):
    """The upload queue and the ephemeral image state."""

    power_mode: PowerMode
    pending: PendingUpload | None = Field(
        description="The image waiting for the frame (expired images are dropped, not listed)"
    )
    last_delivery: LastDelivery | None
    ephemeral: EphemeralView


@router.get("", response_model=QueueView, summary="Inspect the upload queue")
async def get_queue(delivery: DeliveryServiceDep, ephemeral: EphemeralServiceDep) -> QueueView:
    """The pending upload, the last delivery and the current ephemeral image."""
    snapshot = delivery.snapshot()
    return QueueView(
        power_mode=snapshot.power_mode,
        pending=snapshot.pending,
        last_delivery=snapshot.last_delivery,
        ephemeral=ephemeral.view(),
    )


@router.delete(
    "/ephemeral",
    response_model=CancelOutcome,
    dependencies=[Security(api_key_scheme)],
    summary="Cancel the ephemeral image (playback stopped)",
)
async def cancel_ephemeral(ephemeral: EphemeralServiceDep) -> CancelOutcome:
    """Drop a queued movie poster or album cover and restore the previous image.

    Call it when playback pauses or stops. The previous non-ephemeral image becomes the
    current image again; it is re-sent only if the frame may not be showing it (a sleeping
    frame that never received the cover is not woken up). Without a previous image, a
    background refresh replays the active rule. Calling it when nothing is ephemeral is a
    harmless no-op (``cancelled: false``).
    """
    return await ephemeral.cancel()
