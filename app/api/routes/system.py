"""Health and status endpoints."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app import __version__
from app.api.deps import ConfigStoreDep, SettingsDep

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


class SchedulerStatus(BaseModel):
    """Configured autonomous refresh."""

    enabled: bool
    interval_hours: float
    timezone: str


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
    scheduler: SchedulerStatus
    services: ServiceConfigured


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Return ``ok`` while the process is serving requests."""
    return HealthResponse(version=__version__)


@router.get("/status", response_model=StatusResponse, summary="System status")
async def status(request: Request, settings: SettingsDep, store: ConfigStoreDep) -> StatusResponse:
    """Overall system status and configuration summary (no secrets)."""
    config = store.config
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
        ),
        scheduler=SchedulerStatus(
            enabled=config.scheduler.enabled,
            interval_hours=config.scheduler.interval_hours,
            timezone=config.scheduler.timezone,
        ),
        services=ServiceConfigured(
            immich=bool(config.immich.url and config.immich.api_key.get_secret_value()),
            tmdb=bool(config.tmdb.api_key.get_secret_value()),
        ),
    )
