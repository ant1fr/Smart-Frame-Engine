"""TMDB poster display (movie / TV playback from Home Assistant)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Security

from app.api.deps import ConfigStoreDep, DisplayServiceDep, PosterResolverDep, SchedulerDep
from app.api.errors import PROVIDER_ERROR_RESPONSES, provider_http_error
from app.core.auth import api_key_scheme
from app.core.config_store import ConfigStore
from app.core.security import InvalidImageError
from app.providers.base import ProviderError
from app.providers.tmdb import PosterResolver
from app.providers.tmdb.models import TmdbDisplayRequest
from app.services.display import DisplayResult, DisplayService
from app.services.scheduler import SchedulerService, tmdb_rule

display_router = APIRouter(
    prefix="/api/display", tags=["display"], dependencies=[Security(api_key_scheme)]
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    **PROVIDER_ERROR_RESPONSES,
    404: {"description": "No poster found and tmdb.fallback_title_card is disabled"},
    409: {"description": "TMDB is not configured and tmdb.fallback_title_card is disabled"},
}


@display_router.post(
    "/tmdb",
    response_model=DisplayResult,
    responses=_ERRORS,
    summary="Display a movie or TV series poster",
)
async def display_tmdb(
    body: TmdbDisplayRequest,
    resolver: PosterResolverDep,
    display: DisplayServiceDep,
    scheduler: SchedulerDep,
    config: ConfigStoreDep,
) -> DisplayResult:
    """Find the title on TMDB and show its best poster, uncropped, over a blurred backdrop.

    Posters are ranked for ``language`` (default ``tmdb.language``): exact language and
    region first (``fr``/``FR``), then the same language, textless artwork, English, and
    anything else. When TMDB has no match or fails, ``fallback_base64`` / ``fallback_url``
    (e.g. the Home Assistant ``media_player`` picture) is used, then
    ``/data/assets/tmdb_fallback.png``, then a typographic title card.

    By default the poster is **ephemeral**: after ``ttl_minutes`` (default
    ``tmdb.ephemeral_ttl_minutes``) or ``DELETE /api/queue/ephemeral`` the previous image comes
    back, and a deep-sleeping frame that wakes up later never receives an expired poster.
    """
    return await show_tmdb(body, resolver, display, scheduler, config)


@display_router.post(
    "/media",
    response_model=DisplayResult,
    responses=_ERRORS,
    summary="Display a poster (alias of /api/display/tmdb)",
    deprecated=True,
)
async def display_media(
    body: TmdbDisplayRequest,
    resolver: PosterResolverDep,
    display: DisplayServiceDep,
    scheduler: SchedulerDep,
    config: ConfigStoreDep,
) -> DisplayResult:
    """Same as ``POST /api/display/tmdb`` (``title`` and ``media_type: auto`` accepted)."""
    return await show_tmdb(body, resolver, display, scheduler, config)


async def show_tmdb(
    body: TmdbDisplayRequest,
    resolver: PosterResolver,
    display: DisplayService,
    scheduler: SchedulerService,
    config: ConfigStore,
) -> DisplayResult:
    """Resolve, render and (when pushed) record one TMDB display request."""
    settings = config.config.tmdb
    try:
        asset = await resolver.resolve(
            body.search(), fallback_url=body.fallback_url, fallback_base64=body.fallback_base64
        )
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    ttl = body.ttl_minutes or settings.ephemeral_ttl_minutes
    expires_at = scheduler.now() + timedelta(minutes=ttl) if body.ephemeral else None
    try:
        result = await display.display_asset(
            asset, push=body.push_to_fraimic, ephemeral=body.ephemeral, expires_at=expires_at
        )
    except InvalidImageError as exc:
        raise HTTPException(
            status_code=502, detail="The poster could not be decoded as an image"
        ) from exc
    if body.push_to_fraimic:
        hold = body.hold_minutes if body.hold_minutes is not None else settings.hold_minutes
        await scheduler.record_manual(tmdb_rule(body), ephemeral=body.ephemeral, hold_minutes=hold)
    return result
