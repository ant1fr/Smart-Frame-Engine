"""Health and status endpoints."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app import __version__
from app.api.deps import (
    AuthStateDep,
    ConfigStoreDep,
    CurrentImageStoreDep,
    DeliveryServiceDep,
    EphemeralServiceDep,
    IdentityDep,
    SchedulerDep,
    SettingsDep,
)
from app.core.contract import API_VERSION, MIN_API_VERSION, capability_names
from app.services.current_image import CurrentImageMeta
from app.services.delivery import FrameTelemetry, LastDelivery, PendingUpload
from app.services.ephemeral import EphemeralView
from app.services.scheduler import SchedulerSnapshot
from app.version import release_label

router = APIRouter(prefix="/api", tags=["system"])


class HealthResponse(BaseModel):
    """Liveness probe and compatibility handshake (public: no credentials needed)."""

    status: Literal["ok"] = "ok"
    version: str = Field(description="Engine version (PEP 440, e.g. 1.0.0rc1)")
    api_version: int = Field(
        description="Contract version this engine speaks (breaking changes only)"
    )
    min_api_version: int = Field(
        description="Oldest contract version still served; a client implementing contract N "
        "is compatible when min_api_version <= N <= api_version"
    )
    instance_id: str = Field(
        description="Stable random UUID of this engine, generated once and kept in the data "
        "volume; use it as the client-side unique key"
    )
    instance_name: str = Field(description="Human-readable name (also the mDNS service name)")
    auth: Literal["open", "key", "password"] = Field(
        description="Credentials the other endpoints need (same values as the Zeroconf record)"
    )
    capabilities: list[str] = Field(
        description="Sorted feature flags of this engine; only ever added within a contract version"
    )


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

    version: str = Field(description="Engine version (PEP 440, same as /api/health)")
    release: str = Field(
        description="Version as spelled by the git tag and the Docker image tag (SemVer, e.g. "
        "1.0.0-rc.2), with +<build> appended for images built outside a release tag; "
        "informational, for display only"
    )
    uptime_s: float
    auth_enabled: bool
    auth_mode: str = Field(description="open, key (API key only) or password (admin login)")
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
async def health(auth: AuthStateDep, identity: IdentityDep) -> HealthResponse:
    """Liveness plus the compatibility handshake, readable before any credential is set up."""
    return HealthResponse(
        version=__version__,
        api_version=API_VERSION,
        min_api_version=MIN_API_VERSION,
        instance_id=identity.instance_id,
        instance_name=identity.instance_name,
        auth=auth.mode,
        capabilities=capability_names(),
    )


@router.get("/status", response_model=StatusResponse, summary="System status")
async def status(
    request: Request,
    settings: SettingsDep,
    auth: AuthStateDep,
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
        release=release_label(),
        uptime_s=round(time.monotonic() - started_at, 1),
        auth_enabled=auth.mode != "open",
        auth_mode=auth.mode,
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
