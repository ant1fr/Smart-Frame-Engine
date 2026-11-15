"""TMDB poster provider and its two-tier fallback chain.

:class:`TmdbProvider` finds the movie or series, ranks its posters by regional preference
(:mod:`app.providers.tmdb.ranking`) and downloads the best one. Posters are rendered with
the ``poster`` template, so they are never cropped.

:class:`PosterResolver` adds the fallbacks used when TMDB has no match or fails:

1. the per-request fallback from Home Assistant (``fallback_base64``, then
   ``fallback_url`` - typically the ``media_player`` entity picture);
2. the global fallback ``/data/assets/tmdb_fallback.png`` when present, else a clean
   typographic title card.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from app.clients.http_images import ImageFetcher, ImageFetchError, decode_base64_image
from app.core.config_store import ConfigStore
from app.core.security import InvalidImageError, PayloadTooLargeError, verify_image_bytes
from app.core.text import fold
from app.processor.templates import Canvas, PosterLayout, TitleCardLayout, poster_target_width
from app.providers.base import (
    BaseImageProvider,
    ImageAsset,
    NoCandidateError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.providers.tmdb.client import TmdbClient, image_url
from app.providers.tmdb.models import SearchResult, TmdbImage, TmdbRequest
from app.providers.tmdb.ranking import (
    choose_image_size,
    parse_language,
    pick_result,
    rank_posters,
)

logger = logging.getLogger(__name__)

SOURCE: Final = "tmdb"
GLOBAL_FALLBACK_NAME: Final = "tmdb_fallback.png"
#: Ranked posters tried before giving up on downloads.
MAX_DOWNLOAD_ATTEMPTS: Final = 3
TITLE_CARD_ACCENT: Final = (255, 0, 0)

FallbackTier = Literal["request", "global", "title_card"]
ClientFactory = Callable[[str], TmdbClient]


def _display_title(result: SearchResult, season: int | None) -> str:
    title = result.title
    if season is not None:
        return f"{title} · Season {season}"
    return f"{title} ({result.year})" if result.year else title


class TmdbProvider(BaseImageProvider[TmdbRequest]):
    """Movie and TV posters from The Movie Database."""

    name = SOURCE

    def __init__(
        self,
        config_store: ConfigStore,
        fetcher: ImageFetcher,
        *,
        client_factory: ClientFactory = TmdbClient,
    ) -> None:
        self._config_store = config_store
        self._fetcher = fetcher
        self._client_factory = client_factory

    @property
    def configured(self) -> bool:
        """Whether a TMDB key is set."""
        return bool(self._config_store.config.tmdb.api_key.get_secret_value().strip())

    async def fetch(self, request: TmdbRequest) -> ImageAsset:
        """Find the title and download its best poster.

        Raises:
            ProviderNotConfiguredError: No TMDB key.
            NoCandidateError: No match, or the match has no poster.
            ProviderUnavailableError: TMDB or its image CDN failed.
        """
        config = self._config_store.config.tmdb
        api_key = config.api_key.get_secret_value().strip()
        if not api_key:
            raise ProviderNotConfiguredError(
                "TMDB is not configured: set TMDB_API_KEY or tmdb.api_key"
            )
        language = request.language or config.language
        lang, region = parse_language(language)
        include = ",".join(dict.fromkeys([lang, "en", "null"]))
        async with self._client_factory(api_key) as client:
            results = await client.search(
                request.query, request.media_type, year=request.year, language=language
            )
            chosen = pick_result(results, request.query, request.year)
            if chosen is None:
                raise NoCandidateError(f"TMDB has no movie or series matching {request.query!r}")
            season = request.season if chosen.media_type == "tv" else None
            posters: list[TmdbImage] = []
            if season is not None:
                posters = await client.posters(
                    "tv", chosen.id, include_languages=include, season=season
                )
                if not posters:
                    season = None  # no season artwork: use the series posters
            if not posters:
                posters = await client.posters(
                    chosen.media_type, chosen.id, include_languages=include
                )
        needed = self.target_width()
        ranked = rank_posters(posters, lang, region, min_width=needed)
        candidates = [
            (poster.file_path, choose_image_size(config.image_size, poster.width, needed))
            for poster in ranked[:MAX_DOWNLOAD_ATTEMPTS]
        ]
        if chosen.poster_path and all(chosen.poster_path != path for path, _ in candidates):
            # The default poster as a last resort (its original size is unknown).
            candidates.append((chosen.poster_path, choose_image_size(config.image_size, 0, needed)))
        if not candidates:
            raise NoCandidateError(f"TMDB has no poster for {chosen.title!r}")

        data, path, size = await self._download(candidates)
        poster = next((p for p in ranked if p.file_path == path), None)
        details: dict[str, JsonValue] = {
            "title": chosen.title,
            "original_title": chosen.original_title,
            "year": chosen.year,
            "media_type": chosen.media_type,
            "tmdb_id": chosen.id,
            "season": season,
            "language": language,
            "poster_language": poster.iso_639_1 if poster else None,
            "poster_region": poster.iso_3166_1 if poster else None,
            "poster_size": size,
            "poster_original_width": poster.width if poster else None,
        }
        return ImageAsset(
            data=data,
            source=SOURCE,
            history_key=f"tmdb:{chosen.media_type}:{chosen.id}",
            title=_display_title(chosen, season),
            details={key: value for key, value in details.items() if value is not None},
            recommended_fit="contain",
            layout=PosterLayout(),
            track_history=False,
        )

    def target_width(self) -> int:
        """Width (px) of the poster on the configured panel and orientation."""
        config = self._config_store.config
        canvas = Canvas(config.frame_size, config.eink, config.display.safe_margin_px)
        return poster_target_width(canvas)

    async def _download(self, candidates: list[tuple[str, str]]) -> tuple[bytes, str, str]:
        failure: ImageFetchError | None = None
        for path, size in candidates:
            try:
                return await self._fetcher.fetch(image_url(path, size)), path, size
            except ImageFetchError as exc:
                logger.info("TMDB poster download failed, trying the next one: %s", exc)
                failure = exc
        raise ProviderUnavailableError(
            f"Could not download a poster from TMDB ({failure})",
            timeout=bool(failure and failure.timeout),
        )


class PosterResolver:
    """TMDB poster with the per-request and global fallbacks."""

    def __init__(
        self,
        provider: TmdbProvider,
        fetcher: ImageFetcher,
        config_store: ConfigStore,
        assets_dir: Path,
    ) -> None:
        self._provider = provider
        self._fetcher = fetcher
        self._config_store = config_store
        self._assets_dir = assets_dir

    @property
    def global_fallback_path(self) -> Path:
        """Location of the optional global fallback poster."""
        return self._assets_dir / GLOBAL_FALLBACK_NAME

    async def resolve(
        self,
        request: TmdbRequest,
        *,
        fallback_url: str | None = None,
        fallback_base64: str | None = None,
    ) -> ImageAsset:
        """The TMDB poster, else a fallback (see the module docstring).

        Raises:
            ProviderError: TMDB failed, no fallback worked and ``tmdb.fallback_title_card``
                is disabled.
        """
        try:
            return await self._provider.fetch(request)
        except ProviderError as exc:
            failure = exc
            logger.info("TMDB poster unavailable for %r: %s", request.query, exc)

        reason = str(failure)
        asset = await self._request_fallback(request, fallback_url, fallback_base64, reason)
        if asset is None:
            asset = await self._global_fallback(request, reason)
        if asset is None and self._config_store.config.tmdb.fallback_title_card:
            asset = self._title_card(request, reason)
        if asset is None:
            raise failure
        return asset

    async def _request_fallback(
        self,
        request: TmdbRequest,
        url: str | None,
        encoded: str | None,
        reason: str,
    ) -> ImageAsset | None:
        if encoded:
            try:
                data = decode_base64_image(encoded, self._fetcher.max_bytes)
                return self._asset(data, request, "request", reason)
            except (InvalidImageError, PayloadTooLargeError) as exc:
                logger.info("Ignoring the fallback_base64 poster: %s", exc)
        if url:
            try:
                data = await self._fetcher.fetch(url)
                return self._asset(data, request, "request", reason)
            except ImageFetchError as exc:
                logger.info("Ignoring the fallback_url poster: %s", exc)
        return None

    async def _global_fallback(self, request: TmdbRequest, reason: str) -> ImageAsset | None:
        path = self.global_fallback_path
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Cannot read %s: %s", path, exc)
            return None
        try:
            if len(data) > self._fetcher.max_bytes:
                raise PayloadTooLargeError(self._fetcher.max_bytes)
            verify_image_bytes(data)
        except (InvalidImageError, PayloadTooLargeError) as exc:
            logger.warning("Ignoring %s: %s", path, exc)
            return None
        return self._asset(data, request, "global", reason)

    def _title_card(self, request: TmdbRequest, reason: str) -> ImageAsset:
        parts = []
        if request.year:
            parts.append(str(request.year))
        if request.season is not None:
            parts.append(f"Season {request.season}")
        layout = TitleCardLayout(
            title=request.query,
            subtitle=" · ".join(parts) or None,
            accent=TITLE_CARD_ACCENT,
        )
        asset = self._asset(b"", request, "title_card", reason)
        return replace(asset, layout=layout)

    @staticmethod
    def _asset(data: bytes, request: TmdbRequest, tier: FallbackTier, reason: str) -> ImageAsset:
        return ImageAsset(
            data=data,
            source=SOURCE,
            history_key=f"tmdb-fallback:{fold(request.query)}",
            title=request.query,
            details={"title": request.query, "fallback": tier, "fallback_reason": reason},
            recommended_fit="contain",
            layout=PosterLayout(),
            track_history=False,
        )
