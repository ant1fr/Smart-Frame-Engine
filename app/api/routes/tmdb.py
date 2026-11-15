"""TMDB poster display (movie / TV playback from Home Assistant)."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel, Field

from app.api.deps import (
    ConfigStoreDep,
    DisplayServiceDep,
    PosterResolverDep,
    SchedulerDep,
    TmdbProviderDep,
)
from app.api.errors import PROVIDER_ERROR_RESPONSES, provider_http_error
from app.core.auth import api_key_scheme
from app.core.config_models import is_masked
from app.core.config_store import ConfigStore
from app.core.security import InvalidImageError
from app.providers.base import ProviderError
from app.providers.tmdb import PosterResolver
from app.providers.tmdb.client import image_url
from app.providers.tmdb.models import TmdbDisplayRequest, TmdbMediaType, validate_language
from app.services.display import DisplayResult, DisplayService
from app.services.scheduler import SchedulerService, tmdb_rule

display_router = APIRouter(
    prefix="/api/display", tags=["display"], dependencies=[Security(api_key_scheme)]
)
router = APIRouter(prefix="/api/tmdb", tags=["tmdb"], dependencies=[Security(api_key_scheme)])

#: Poster thumbnails in search results (loaded by the browser from the public TMDB CDN).
THUMBNAIL_SIZE: Final = "w185"
MAX_SEARCH_RESULTS: Final = 12

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


# --- UI helpers: search and key test ------------------------------------------------------
class TmdbSearchHit(BaseModel):
    """One movie or series in ``GET /api/tmdb/search``."""

    id: int
    media_type: str
    title: str
    original_title: str | None = None
    year: int | None = None
    poster_thumbnail: str | None = Field(
        default=None, description="Small poster URL on the public TMDB image CDN"
    )


@router.get(
    "/search",
    response_model=list[TmdbSearchHit],
    responses=PROVIDER_ERROR_RESPONSES,
    summary="Search movies and series (for the web UI picker)",
)
async def search(
    tmdb: TmdbProviderDep,
    q: Annotated[str, Query(min_length=1, max_length=200, description="Title to search")],
    media_type: Annotated[TmdbMediaType, Query(description="movie, tv or multi")] = "multi",
    year: Annotated[int | None, Query(ge=1870, le=2200)] = None,
    language: Annotated[str | None, Query(description="Overrides tmdb.language")] = None,
) -> list[TmdbSearchHit]:
    """The most relevant matches with a poster thumbnail; nothing is displayed."""
    try:
        language = validate_language(language)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"language {exc}") from exc
    try:
        results = await tmdb.search(q.strip(), media_type, year=year, language=language)
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    return [
        TmdbSearchHit(
            id=result.id,
            media_type=result.media_type,
            title=result.title,
            original_title=result.original_title,
            year=result.year,
            poster_thumbnail=image_url(result.poster_path, THUMBNAIL_SIZE)
            if result.poster_path
            else None,
        )
        for result in results[:MAX_SEARCH_RESULTS]
    ]


class TmdbTestRequest(BaseModel):
    """Test the stored key, or an unsaved one typed in the UI."""

    api_key: str | None = Field(
        default=None, description="Unsaved key (empty or masked = the stored one)"
    )


class TmdbTestResult(BaseModel):
    """Outcome of a TMDB key test."""

    ok: bool
    error: str | None = None


@router.post("/test", response_model=TmdbTestResult, summary="Test a TMDB API key")
async def test_key(body: TmdbTestRequest, tmdb: TmdbProviderDep) -> TmdbTestResult:
    """Check that TMDB is reachable and accepts the key (v3 key or v4 read token)."""
    key = None if body.api_key is None or is_masked(body.api_key) else body.api_key
    try:
        await tmdb.check_key(key)
    except ProviderError as exc:
        return TmdbTestResult(ok=False, error=str(exc))
    return TmdbTestResult(ok=True)
