from __future__ import annotations

import base64
import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from PIL import Image

from app.clients.http_images import ImageFetcher
from app.core.config_store import ConfigStore
from app.core.security import URLPolicy
from app.processor.templates import PosterLayout, TitleCardLayout
from app.providers.base import (
    NoCandidateError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.providers.tmdb import PosterResolver, TmdbProvider, TmdbRequest

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"
V3_KEY = "0123456789abcdef0123456789abcdef"
V4_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.c2lnbmF0dXJl"


def png_bytes(color: tuple[int, int, int] = (200, 30, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buffer, format="PNG")
    return buffer.getvalue()


def config_store(tmp_path: Path, **tmdb: Any) -> ConfigStore:
    seed = {"tmdb": {"api_key": V3_KEY, "language": "fr-FR", **tmdb}}
    store = ConfigStore(tmp_path / "config.json", seed)
    store.load()
    return store


class Setup:
    def __init__(self, tmp_path: Path, **tmdb: Any) -> None:
        self.store = config_store(tmp_path, **tmdb)
        self.fetcher = ImageFetcher(URLPolicy(), 5_000_000)
        self.provider = TmdbProvider(self.store, self.fetcher)
        self.assets = tmp_path / "assets"
        self.assets.mkdir()
        self.resolver = PosterResolver(self.provider, self.fetcher, self.store, self.assets)


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[Setup]:
    instance = Setup(tmp_path)
    yield instance
    await instance.fetcher.aclose()


AMELIE_SEARCH = {
    "results": [
        {
            "id": 194,
            "title": "Le Fabuleux Destin d'Amélie Poulain",
            "original_title": "Le Fabuleux Destin d'Amélie Poulain",
            "release_date": "2001-04-25",
            "poster_path": "/default.jpg",
            "popularity": 50.0,
        }
    ]
}
AMELIE_POSTERS = {
    "posters": [
        {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 9.0, "width": 2000},
        {
            "file_path": "/fr_ca.jpg",
            "iso_639_1": "fr",
            "iso_3166_1": "CA",
            "vote_average": 6.0,
            "width": 2000,
        },
        {
            "file_path": "/fr_fr.jpg",
            "iso_639_1": "fr",
            "iso_3166_1": "FR",
            "vote_average": 5.0,
            "width": 2000,
        },
        {"file_path": "/textless.jpg", "iso_639_1": None, "vote_average": 8.0, "width": 2000},
    ]
}


@respx.mock
async def test_fetch_downloads_the_best_regional_poster(setup: Setup) -> None:
    search = respx.get(f"{API}/search/movie").respond(json=AMELIE_SEARCH)
    images = respx.get(f"{API}/movie/194/images").respond(json=AMELIE_POSTERS)
    download = respx.get(f"{IMG}/w1280/fr_fr.jpg").respond(content=png_bytes())

    asset = await setup.provider.fetch(TmdbRequest(query="amelie", media_type="movie", year=2001))

    assert asset.data == png_bytes()
    assert download.called
    assert asset.layout == PosterLayout()
    assert asset.recommended_fit == "contain"
    assert not asset.track_history
    assert asset.title == "Le Fabuleux Destin d'Amélie Poulain (2001)"
    assert asset.details["poster_region"] == "FR"
    assert asset.details["tmdb_id"] == 194
    params = search.calls.last.request.url.params
    assert params["language"] == "fr-FR"
    assert params["year"] == "2001"
    assert params["api_key"] == V3_KEY
    assert images.calls.last.request.url.params["include_image_language"] == "fr,en,null"


@respx.mock
async def test_v4_tokens_use_a_bearer_header(tmp_path: Path) -> None:
    setup = Setup(tmp_path, api_key=V4_TOKEN)
    search = respx.get(f"{API}/search/multi").respond(json={"results": []})
    try:
        with pytest.raises(NoCandidateError):
            await setup.provider.fetch(TmdbRequest(query="Nothing"))
    finally:
        await setup.fetcher.aclose()
    request = search.calls.last.request
    assert request.headers["authorization"] == f"Bearer {V4_TOKEN}"
    assert "api_key" not in request.url.params


@respx.mock
async def test_multi_search_skips_people_and_prefers_an_exact_title(setup: Setup) -> None:
    respx.get(f"{API}/search/multi").respond(
        json={
            "results": [
                {"id": 1, "media_type": "person", "name": "Breaking Bad"},
                {"id": 2, "media_type": "movie", "title": "Breaking Bad Movie"},
                {"id": 1396, "media_type": "tv", "name": "Breaking Bad", "first_air_date": "2008"},
            ]
        }
    )
    respx.get(f"{API}/tv/1396/images").respond(
        json={"posters": [{"file_path": "/bb.jpg", "iso_639_1": "en"}]}
    )
    respx.get(f"{IMG}/original/bb.jpg").respond(content=png_bytes())
    asset = await setup.provider.fetch(TmdbRequest(query="breaking bad", language="en-US"))
    assert asset.details["media_type"] == "tv"
    assert asset.details["tmdb_id"] == 1396


@respx.mock
async def test_season_posters_with_fallback_to_the_series(setup: Setup) -> None:
    respx.get(f"{API}/search/tv").respond(
        json={"results": [{"id": 1399, "name": "Game of Thrones", "poster_path": "/series.jpg"}]}
    )
    season = respx.get(f"{API}/tv/1399/season/3/images").respond(
        json={"posters": [{"file_path": "/s3.jpg", "iso_639_1": "fr"}]}
    )
    respx.get(f"{IMG}/original/s3.jpg").respond(content=png_bytes())
    asset = await setup.provider.fetch(
        TmdbRequest(query="Game of Thrones", media_type="tv", season=3)
    )
    assert season.called
    assert asset.details["season"] == 3
    assert asset.title == "Game of Thrones · Season 3"

    respx.get(f"{API}/tv/1399/season/9/images").respond(404)
    respx.get(f"{API}/tv/1399/images").respond(json={"posters": []})
    respx.get(f"{IMG}/original/series.jpg").respond(content=png_bytes())
    fallback = await setup.provider.fetch(
        TmdbRequest(query="Game of Thrones", media_type="tv", season=9)
    )
    assert "season" not in fallback.details  # the series' default poster was used


@respx.mock
async def test_a_failed_download_tries_the_next_ranked_poster(setup: Setup) -> None:
    respx.get(f"{API}/search/movie").respond(json=AMELIE_SEARCH)
    respx.get(f"{API}/movie/194/images").respond(json=AMELIE_POSTERS)
    respx.get(f"{IMG}/w1280/fr_fr.jpg").respond(404)
    respx.get(f"{IMG}/w1280/fr_ca.jpg").respond(content=png_bytes())
    asset = await setup.provider.fetch(TmdbRequest(query="Amélie", media_type="movie"))
    assert asset.details["poster_region"] == "CA"


@respx.mock
async def test_configured_image_size_is_used(tmp_path: Path) -> None:
    setup = Setup(tmp_path, image_size="original")
    respx.get(f"{API}/search/movie").respond(json=AMELIE_SEARCH)
    respx.get(f"{API}/movie/194/images").respond(json=AMELIE_POSTERS)
    route = respx.get(f"{IMG}/original/fr_fr.jpg").respond(content=png_bytes())
    try:
        await setup.provider.fetch(TmdbRequest(query="Amélie", media_type="movie"))
    finally:
        await setup.fetcher.aclose()
    assert route.called


async def test_missing_key_is_reported(tmp_path: Path) -> None:
    setup = Setup(tmp_path, api_key="")
    try:
        assert not setup.provider.configured
        with pytest.raises(ProviderNotConfiguredError):
            await setup.provider.fetch(TmdbRequest(query="x"))
    finally:
        await setup.fetcher.aclose()


@respx.mock
async def test_upstream_errors_are_reported(setup: Setup) -> None:
    route = respx.get(f"{API}/search/multi").respond(401)
    with pytest.raises(ProviderUnavailableError, match="rejected the API key"):
        await setup.provider.fetch(TmdbRequest(query="x"))
    route.mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderUnavailableError) as timeout:
        await setup.provider.fetch(TmdbRequest(query="x"))
    assert timeout.value.timeout


# --- fallbacks ------------------------------------------------------------------------------
@respx.mock
async def test_resolver_returns_the_tmdb_poster_when_found(setup: Setup) -> None:
    respx.get(f"{API}/search/movie").respond(json=AMELIE_SEARCH)
    respx.get(f"{API}/movie/194/images").respond(json=AMELIE_POSTERS)
    respx.get(f"{IMG}/w1280/fr_fr.jpg").respond(content=png_bytes())
    asset = await setup.resolver.resolve(
        TmdbRequest(query="Amélie", media_type="movie"), fallback_url="https://ha.test/pic.png"
    )
    assert "fallback" not in asset.details


@respx.mock
async def test_resolver_uses_the_home_assistant_picture_first(setup: Setup) -> None:
    respx.get(f"{API}/search/multi").respond(json={"results": []})
    ha = respx.get("https://ha.test/api/media_player_proxy/tv").respond(
        content=png_bytes((1, 2, 3))
    )
    asset = await setup.resolver.resolve(
        TmdbRequest(query="Home video"),
        fallback_url="https://ha.test/api/media_player_proxy/tv?token=abc",
    )
    assert ha.called
    assert asset.data == png_bytes((1, 2, 3))
    assert asset.details["fallback"] == "request"
    assert asset.layout == PosterLayout()


@respx.mock
async def test_resolver_prefers_base64_over_the_url(setup: Setup) -> None:
    respx.get(f"{API}/search/multi").respond(500)
    url = respx.get("https://ha.test/pic.png").respond(content=png_bytes())
    encoded = base64.b64encode(png_bytes((9, 9, 9))).decode()
    asset = await setup.resolver.resolve(
        TmdbRequest(query="x"), fallback_url="https://ha.test/pic.png", fallback_base64=encoded
    )
    assert asset.data == png_bytes((9, 9, 9))
    assert not url.called
    assert "TMDB answered HTTP 500" in str(asset.details["fallback_reason"])


@respx.mock
async def test_resolver_falls_back_to_the_global_poster_then_a_title_card(setup: Setup) -> None:
    respx.get(f"{API}/search/multi").respond(json={"results": []})
    respx.get("https://ha.test/broken.png").respond(404)
    request = TmdbRequest(query="Unknown Film", year=1999)

    card = await setup.resolver.resolve(request, fallback_url="https://ha.test/broken.png")
    assert card.details["fallback"] == "title_card"
    assert isinstance(card.layout, TitleCardLayout)
    assert card.layout.title == "Unknown Film"
    assert card.layout.subtitle == "1999"
    assert card.data == b""

    setup.resolver.global_fallback_path.write_bytes(png_bytes((5, 5, 5)))
    glob = await setup.resolver.resolve(request, fallback_url="https://ha.test/broken.png")
    assert glob.details["fallback"] == "global"
    assert glob.data == png_bytes((5, 5, 5))

    setup.resolver.global_fallback_path.write_bytes(b"not an image")
    assert (await setup.resolver.resolve(request)).details["fallback"] == "title_card"


@respx.mock
async def test_resolver_without_title_card_raises_the_tmdb_error(tmp_path: Path) -> None:
    setup = Setup(tmp_path, fallback_title_card=False)
    respx.get(f"{API}/search/multi").respond(json={"results": []})
    try:
        with pytest.raises(NoCandidateError):
            await setup.resolver.resolve(TmdbRequest(query="x"))
    finally:
        await setup.fetcher.aclose()


async def test_resolver_works_without_a_tmdb_key(tmp_path: Path) -> None:
    setup = Setup(tmp_path, api_key="")
    try:
        asset = await setup.resolver.resolve(TmdbRequest(query="Offline", season=2))
    finally:
        await setup.fetcher.aclose()
    assert isinstance(asset.layout, TitleCardLayout)
    assert asset.layout.subtitle == "Season 2"
    assert "not configured" in str(asset.details["fallback_reason"])


@respx.mock
async def test_a_low_res_regional_poster_yields_to_a_sharp_one(setup: Setup) -> None:
    """Real case: the best rated fr-FR poster of "Intouchables" is only 600x900."""
    respx.get(f"{API}/search/movie").respond(json=AMELIE_SEARCH)
    respx.get(f"{API}/movie/194/images").respond(
        json={
            "posters": [
                {
                    "file_path": "/small.jpg",
                    "iso_639_1": "fr",
                    "iso_3166_1": "FR",
                    "vote_average": 9.0,
                    "width": 600,
                },
                {
                    "file_path": "/sharp.jpg",
                    "iso_639_1": "fr",
                    "iso_3166_1": "FR",
                    "vote_average": 5.0,
                    "width": 2000,
                },
            ]
        }
    )
    route = respx.get(f"{IMG}/w1280/sharp.jpg").respond(content=png_bytes())
    asset = await setup.provider.fetch(TmdbRequest(query="Amélie", media_type="movie"))
    assert route.called
    assert asset.details["poster_size"] == "w1280"
    assert asset.details["poster_original_width"] == 2000


@respx.mock
async def test_the_31_inch_panel_downloads_the_original(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    await setup.store.update({"fraimic": {"panel": "315"}})
    assert setup.provider.target_width() == 1296
    respx.get(f"{API}/search/movie").respond(json=AMELIE_SEARCH)
    respx.get(f"{API}/movie/194/images").respond(json=AMELIE_POSTERS)
    route = respx.get(f"{IMG}/original/fr_fr.jpg").respond(content=png_bytes())
    try:
        await setup.provider.fetch(TmdbRequest(query="Amélie", media_type="movie"))
    finally:
        await setup.fetcher.aclose()
    assert route.called
