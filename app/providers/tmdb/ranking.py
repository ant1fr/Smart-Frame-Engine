"""Regional poster ranking and search-result selection.

TMDB tags each poster with a language (``iso_639_1``) and, for regional variants, a
country (``iso_3166_1``): a French poster for France (``fr``/``FR``) differs from the
Québec one (``fr``/``CA``). For ``language="fr-FR"`` posters are ranked:

1. exact language **and** region (``fr`` + ``FR``);
2. same language, any or no region (``fr`` + ``CA``, ``fr`` + none);
3. textless artwork (no language);
4. English;
5. anything else.

Within a tier, posters large enough for the frame come first (``min_width``: a poster
smaller than its on-panel size has to be enlarged and looks soft), then the best rated:
``(vote_average, vote_count)`` descending. The language tiers always take precedence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from app.core.text import fold
from app.providers.tmdb.models import SearchResult, TmdbImage

FALLBACK_LANGUAGE: Final = "en"
#: Search results considered when looking for an exact title match.
EXACT_MATCH_WINDOW: Final = 5


def parse_language(language: str) -> tuple[str, str | None]:
    """Split ``"fr-FR"`` into ``("fr", "FR")`` (``"fr"`` gives ``("fr", None)``)."""
    lang, _, region = language.partition("-")
    return lang.lower(), (region.upper() or None)


def poster_tier(image: TmdbImage, lang: str, region: str | None) -> int:
    """The priority tier of ``image`` (0 = best)."""
    image_lang = image.iso_639_1.lower() if image.iso_639_1 else None
    image_region = image.iso_3166_1.upper() if image.iso_3166_1 else None
    if image_lang == lang:
        return 0 if region is not None and image_region == region else 1
    if image_lang is None:
        return 2
    if image_lang == FALLBACK_LANGUAGE:
        return 3
    return 4


def rank_posters(
    images: Sequence[TmdbImage], lang: str, region: str | None, *, min_width: int = 0
) -> list[TmdbImage]:
    """Posters ordered by regional preference, then sharpness, then rating (stable).

    Args:
        images: Candidate posters.
        lang: Wanted language (``fr``).
        region: Wanted region (``FR``) or ``None``.
        min_width: On-panel width; narrower originals rank last within their tier.
    """
    return sorted(
        images,
        key=lambda image: (
            poster_tier(image, lang, region),
            image.width < min_width,
            -image.vote_average,
            -image.vote_count,
        ),
    )


#: TMDB poster sizes below "original", by width.
SCALED_SIZES: Final = ((780, "w780"), (1280, "w1280"))


def choose_image_size(configured: str, original_width: int, needed_width: int) -> str:
    """The TMDB size to download a poster at.

    ``auto`` picks the smallest scaled size that covers ``needed_width`` **and** is a real
    downscale of the original (TMDB upscales silently when asked for more pixels than the
    original has), else ``original``. An unknown original width (0) gives ``original``.
    """
    if configured != "auto":
        return configured
    for width, name in SCALED_SIZES:
        if needed_width <= width <= original_width:
            return name
    return "original"


def pick_result(
    results: Sequence[SearchResult], query: str, year: int | None
) -> SearchResult | None:
    """The search result that best matches ``query``.

    Among the first :data:`EXACT_MATCH_WINDOW` results (TMDB orders by relevance), an
    exact folded title match (translated or original title) wins, preferring the requested
    year; otherwise the first result is used.
    """
    if not results:
        return None
    wanted = fold(query)
    window = results[:EXACT_MATCH_WINDOW]
    exact = [
        result
        for result in window
        if wanted in {fold(result.title), fold(result.original_title or "")}
    ]
    if year is not None:
        dated = [result for result in exact if result.year == year]
        if dated:
            return dated[0]
    if exact:
        return exact[0]
    return results[0]
