"""FastAPI dependencies giving routes access to application-scoped services."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.settings import EnvSettings
from app.services.current_image import CurrentImageStore
from app.services.delivery import DeliveryService
from app.services.display import DisplayService


def get_settings(request: Request) -> EnvSettings:
    """Environment settings the application was created with."""
    return cast(EnvSettings, request.app.state.settings)


def get_config_store(request: Request) -> ConfigStore:
    """The runtime configuration store (initialized during app startup)."""
    return cast(ConfigStore, request.app.state.config_store)


def get_history_store(request: Request) -> HistoryStore:
    """The display history store (initialized during app startup)."""
    return cast(HistoryStore, request.app.state.history_store)


def get_current_image_store(request: Request) -> CurrentImageStore:
    """Store of the current rendered image."""
    return cast(CurrentImageStore, request.app.state.current_image_store)


def get_delivery_service(request: Request) -> DeliveryService:
    """Frame delivery service (queue, worker, telemetry)."""
    return cast(DeliveryService, request.app.state.delivery_service)


def get_display_service(request: Request) -> DisplayService:
    """Render-and-display orchestration service."""
    return cast(DisplayService, request.app.state.display_service)


SettingsDep = Annotated[EnvSettings, Depends(get_settings)]
ConfigStoreDep = Annotated[ConfigStore, Depends(get_config_store)]
HistoryStoreDep = Annotated[HistoryStore, Depends(get_history_store)]
CurrentImageStoreDep = Annotated[CurrentImageStore, Depends(get_current_image_store)]
DeliveryServiceDep = Annotated[DeliveryService, Depends(get_delivery_service)]
DisplayServiceDep = Annotated[DisplayService, Depends(get_display_service)]
