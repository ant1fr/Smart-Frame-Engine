"""Health and status endpoints."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app import __version__
from app.api.deps import (
    ConfigStoreDep,
    CurrentImageStoreDep,
    DeliveryServiceDep,
    EphemeralServiceDep,
    SchedulerDep,
    SettingsDep,
)
from app.services.current_image import CurrentImageMeta
from app.services.delivery import FrameTelemetry, LastDelivery, PendingUpload
from app.services.ephemeral import EphemeralView
from app.services.scheduler import SchedulerSnapshot

router = APIRouter(prefix="/api", tags=["system"])


class HealthResponse(BaseModel):
    """Liveness probe payload."""

    status: Literal["ok"] = "ok"
    version: str


class FrameStatus(BaseModel):
    """Configured frame characteristics."""

    panel: str = Field(description="Panel model key (133 or 315)")
    panel_label: str
    power_mode: str
    orientation: str
    frame_size: tuple[int, int] = Field(description="Composition size (width, height)")
    configured: bool = Field(description="Whether a frame host is configured")
    telemetry: FrameTelemetry = Field(description="Last known frame state (no live query)")


class DeliveryStatus(BaseModel):
    """Upload queue state."""

    pending: bool = Field(description="Whether an image is waiting for the frame")
    pending_upload: PendingUpload | None = None
    last_delivery: LastDelivery | None = None


class ServiceConfigured(BaseModel):
    """Whether each external service has the settings it needs."""

    immich: bool
    tmdb: bool


class StatusResponse(BaseModel):
    """Overall system status."""

    version: str
    uptime_s: float
    auth_enabled: bool
    public_image_endpoints: bool
    frame: FrameStatus
    delivery: DeliveryStatus
    current_image: CurrentImageMeta | None = Field(
        default=None, description="Metadata of the current image (null before the first one)"
    )
    scheduler: SchedulerSnapshot = Field(description="Autonomous refresh: timer and last run")
    ephemeral: EphemeralView = Field(
        description="Temporary image (now playing, poster) shown or queued, and its expiry"
    )
    services: ServiceConfigured


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Return ``ok`` while the process is serving requests."""
    return HealthResponse(version=__version__)


@router.get("/status", response_model=StatusResponse, summary="System status")
async def status(
    request: Request,
    settings: SettingsDep,
    store: ConfigStoreDep,
    current: CurrentImageStoreDep,
    delivery: DeliveryServiceDep,
    scheduler: SchedulerDep,
    ephemeral: EphemeralServiceDep,
) -> StatusResponse:
    """Overall system status, queue state and current image (no secrets, no network)."""
    config = store.config
    snapshot = delivery.snapshot()
    started_at: float = request.app.state.started_at
    return StatusResponse(
        version=__version__,
        uptime_s=round(time.monotonic() - started_at, 1),
        auth_enabled=settings.auth_enabled,
        public_image_endpoints=settings.public_image_endpoints,
        frame=FrameStatus(
            panel=config.fraimic.panel,
            panel_label=config.fraimic.spec.label,
            power_mode=config.fraimic.power_mode,
            orientation=config.display.orientation,
            frame_size=config.frame_size,
            configured=bool(config.fraimic.base_url),
            telemetry=snapshot.telemetry,
        ),
        delivery=DeliveryStatus(
            pending=snapshot.pending is not None,
            pending_upload=snapshot.pending,
            last_delivery=snapshot.last_delivery,
        ),
        current_image=current.meta,
        scheduler=scheduler.snapshot(),
        ephemeral=ephemeral.view(),
        services=ServiceConfigured(
            immich=bool(config.immich.accounts),
            tmdb=bool(config.tmdb.api_key.get_secret_value()),
        ),
    )
