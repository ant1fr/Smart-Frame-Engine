"""Now-playing album covers (Home Assistant music playback).

A request describes the track that is playing. The cover comes from, in order:

1. ``artwork_base64`` / ``artwork_url`` sent by Home Assistant;
2. when missing or unusable (and enrichment is enabled), a high-resolution cover looked up
   on Deezer, then MusicBrainz / Cover Art Archive;
3. otherwise a clean typographic card (title, artist, album).

The cover is rendered with the ``album`` template and is ephemeral by default: it expires
when the track should be over (``duration - position`` plus a grace period), so a frame
waking up later shows the previous image again instead of a stale song.

Home Assistant fires automations on every attribute change (position updates, volume...).
A request for the track that is already displayed is **deduplicated**: only its expiry is
moved, nothing is rendered or sent again (no needless wake-up of a battery frame).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from pydantic import JsonValue

from app.clients.http_images import ImageFetcher, ImageFetchError, decode_base64_image
from app.core.config_models import MusicConfig
from app.core.config_store import ConfigStore
from app.core.security import InvalidImageError, PayloadTooLargeError
from app.core.text import fold
from app.processor.templates import (
    AlbumLayout,
    AlbumStyle,
    Canvas,
    Layout,
    TitleCardLayout,
    album_cover_side,
)
from app.providers.music.artwork import ArtworkResolver
from app.providers.music.models import NowPlayingRequest
from app.services.current_image import CurrentImageStore
from app.services.display import DisplayMeta, DisplayResult, DisplayService

logger = logging.getLogger(__name__)

SOURCE: Final = "music"
TITLE_CARD_ACCENT: Final = (0, 0, 255)

ArtworkOrigin = Literal["provided", "deezer", "musicbrainz", "none"]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def content_key(request: NowPlayingRequest, style: str) -> str:
    """Identity of the displayed cover (same track, album and style = same picture)."""
    parts = [fold(request.title), fold(request.artist), fold(request.album or ""), style]
    return "music:" + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def cover_ttl(request: NowPlayingRequest, config: MusicConfig) -> timedelta:
    """How long the cover stays: explicit ttl, else remaining track time, else the default."""
    if request.ttl_seconds is not None:
        return timedelta(seconds=request.ttl_seconds)
    if request.duration_seconds:
        remaining = request.duration_seconds - (request.position_seconds or 0.0)
        if remaining > 0:
            return timedelta(seconds=remaining + config.ttl_grace_seconds)
    return timedelta(minutes=config.default_ttl_minutes)


class NowPlayingService:
    """Renders and pushes now-playing covers."""

    def __init__(
        self,
        config_store: ConfigStore,
        display: DisplayService,
        current: CurrentImageStore,
        fetcher: ImageFetcher,
        artwork: ArtworkResolver,
        *,
        clock: Clock = _utcnow,
    ) -> None:
        self._config_store = config_store
        self._display = display
        self._current = current
        self._fetcher = fetcher
        self._artwork = artwork
        self._clock = clock

    async def show(self, request: NowPlayingRequest) -> DisplayResult:
        """Display (or refresh the expiry of) the cover of the track that is playing.

        Raises:
            InvalidImageError: The cover could not be rendered.
        """
        config = self._config_store.config.music
        style = request.style or config.default_style
        key = content_key(request, style)
        now = self._clock()
        expires_at = now + cover_ttl(request, config) if request.ephemeral else None

        if request.push_to_fraimic and request.ephemeral:
            current = self._current.meta
            if (
                current is not None
                and current.ephemeral
                and current.content_key == key
                and not current.expired(now)
            ):
                updated = await self._display.extend_expiry(key, expires_at)
                if updated is not None:
                    logger.debug("Now playing %r is already displayed; expiry moved", request.title)
                    return DisplayResult(image=updated, delivery=None, deduplicated=True)

        data, origin = await self._cover(request, config)
        layout: Layout
        if data is not None:
            layout = AlbumLayout(
                title=request.title, artist=request.artist, album=request.album, style=style
            )
        else:
            layout = TitleCardLayout(
                title=request.title,
                subtitle=request.artist,
                caption=request.album,
                accent=TITLE_CARD_ACCENT,
            )
        details: dict[str, JsonValue] = {
            "title": request.title,
            "artist": request.artist,
            "album": request.album,
            "style": style,
            "artwork": origin,
            "duration_seconds": request.duration_seconds,
        }
        meta = DisplayMeta(
            source=SOURCE,
            title=f"{request.title} — {request.artist}",
            details={k: v for k, v in details.items() if v is not None},
            push=request.push_to_fraimic,
            ephemeral=request.ephemeral,
            expires_at=expires_at,
            content_key=key,
        )
        return await self._display.display_layout(data, layout, meta)

    def _cover_side(self, style: AlbumStyle) -> int:
        """Side (px) of the album cover on the configured panel and orientation."""
        config = self._config_store.config
        canvas = Canvas(config.frame_size, config.eink, config.display.safe_margin_px)
        return album_cover_side(canvas, style)

    async def _cover(
        self, request: NowPlayingRequest, config: MusicConfig
    ) -> tuple[bytes | None, ArtworkOrigin]:
        if request.artwork_base64:
            try:
                return decode_base64_image(request.artwork_base64, self._fetcher.max_bytes), (
                    "provided"
                )
            except (InvalidImageError, PayloadTooLargeError) as exc:
                logger.info("Ignoring artwork_base64: %s", exc)
        if request.artwork_url:
            try:
                return await self._fetcher.fetch(request.artwork_url), "provided"
            except ImageFetchError as exc:
                logger.info("Ignoring artwork_url: %s", exc)
        if request.enrich_artwork and config.enrich_artwork:
            artwork = await self._artwork.find(
                title=request.title,
                artist=request.artist,
                album=request.album,
                min_side=self._cover_side(request.style or config.default_style),
            )
            if artwork is not None:
                return artwork.data, artwork.match.source
        return None, "none"
