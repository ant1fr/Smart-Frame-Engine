"""FastAPI dependencies giving routes access to application-scoped services."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.auth import AuthState
from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.settings import EnvSettings
from app.providers.immich import ImmichProvider, PeopleDirectory
from app.providers.tmdb import PosterResolver
from app.services.current_image import CurrentImageStore
from app.services.delivery import DeliveryService
from app.services.display import DisplayService
from app.services.ephemeral import EphemeralService
from app.services.now_playing import NowPlayingService
from app.services.scheduler import SchedulerService


def get_settings(request: Request) -> EnvSettings:
    """Environment settings the application was created with."""
    return cast(EnvSettings, request.app.state.settings)


def get_auth_state(request: Request) -> AuthState:
    """Effective credentials (admin password, API key, session signing)."""
    return cast(AuthState, request.app.state.auth_state)


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


def get_immich_provider(request: Request) -> ImmichProvider:
    """The Immich image provider."""
    return cast(ImmichProvider, request.app.state.immich_provider)


def get_poster_resolver(request: Request) -> PosterResolver:
    """TMDB posters with the Home Assistant and global fallbacks."""
    return cast(PosterResolver, request.app.state.poster_resolver)


def get_scheduler_service(request: Request) -> SchedulerService:
    """The autonomous refresh scheduler."""
    return cast(SchedulerService, request.app.state.scheduler_service)


def get_ephemeral_service(request: Request) -> EphemeralService:
    """Lifetime manager of ephemeral images (expiry, cancellation, revert)."""
    return cast(EphemeralService, request.app.state.ephemeral_service)


def get_now_playing_service(request: Request) -> NowPlayingService:
    """Now-playing album covers."""
    return cast(NowPlayingService, request.app.state.now_playing_service)


def get_people_directory(request: Request) -> PeopleDirectory:
    """Cache of the people known to each Immich account."""
    return cast(PeopleDirectory, request.app.state.people_directory)


SettingsDep = Annotated[EnvSettings, Depends(get_settings)]
AuthStateDep = Annotated[AuthState, Depends(get_auth_state)]
ConfigStoreDep = Annotated[ConfigStore, Depends(get_config_store)]
HistoryStoreDep = Annotated[HistoryStore, Depends(get_history_store)]
CurrentImageStoreDep = Annotated[CurrentImageStore, Depends(get_current_image_store)]
DeliveryServiceDep = Annotated[DeliveryService, Depends(get_delivery_service)]
DisplayServiceDep = Annotated[DisplayService, Depends(get_display_service)]
ImmichProviderDep = Annotated[ImmichProvider, Depends(get_immich_provider)]
PeopleDirectoryDep = Annotated[PeopleDirectory, Depends(get_people_directory)]
SchedulerDep = Annotated[SchedulerService, Depends(get_scheduler_service)]
EphemeralServiceDep = Annotated[EphemeralService, Depends(get_ephemeral_service)]
PosterResolverDep = Annotated[PosterResolver, Depends(get_poster_resolver)]
NowPlayingServiceDep = Annotated[NowPlayingService, Depends(get_now_playing_service)]
