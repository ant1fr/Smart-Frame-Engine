"""Now-playing album covers (music playback from Home Assistant)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Security

from app.api.deps import NowPlayingServiceDep, SchedulerDep
from app.core.auth import api_key_scheme
from app.core.security import InvalidImageError
from app.providers.music.models import NowPlayingRequest
from app.services.display import DisplayResult

display_router = APIRouter(
    prefix="/api/display", tags=["display"], dependencies=[Security(api_key_scheme)]
)


@display_router.post(
    "/now-playing",
    response_model=DisplayResult,
    responses={502: {"description": "The cover could not be decoded as an image"}},
    summary="Display the album cover of the track that is playing",
)
async def display_now_playing(
    body: NowPlayingRequest, now_playing: NowPlayingServiceDep, scheduler: SchedulerDep
) -> DisplayResult:
    """Show the full, uncropped cover with the title, artist and album underneath.

    The cover is ``artwork_base64`` / ``artwork_url`` when usable; otherwise (with
    ``enrich_artwork``) a 1000x1000 cover is looked up on Deezer, then MusicBrainz / Cover
    Art Archive; otherwise a typographic card is shown.

    The cover is **ephemeral** by default. It expires when the track should be over
    (``duration_seconds - position_seconds`` plus ``music.ttl_grace_seconds``, or
    ``ttl_seconds``, or ``music.default_ttl_minutes``) and on
    ``DELETE /api/queue/ephemeral``; the previous image then comes back. Sending the track
    that is already displayed only moves its expiry (``deduplicated: true``), so Home
    Assistant can call this on every attribute change.
    """
    try:
        result = await now_playing.show(body)
    except InvalidImageError as exc:
        raise HTTPException(
            status_code=502, detail="The cover could not be decoded as an image"
        ) from exc
    if body.push_to_fraimic and not result.deduplicated:
        await scheduler.record_manual(
            None, ephemeral=body.ephemeral, hold_minutes=body.hold_minutes
        )
    return result
