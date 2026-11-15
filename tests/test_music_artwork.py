from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from PIL import Image

from app.clients.http_images import USER_AGENT, ImageFetcher
from app.core.config_store import ConfigStore
from app.core.security import URLPolicy
from app.providers.music import ArtworkResolver, MinIntervalLimiter
from app.providers.music.artwork import lucene_quote

DEEZER = "https://api.deezer.com"
MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"


def png_bytes(color: tuple[int, int, int] = (10, 20, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (50, 50), color).save(buffer, format="PNG")
    return buffer.getvalue()


class NoWait(MinIntervalLimiter):
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        clock = iter(float(i) * 0.1 for i in range(1000))

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        super().__init__(1.0, clock=lambda: next(clock), sleep=fake_sleep)


class Setup:
    def __init__(self, tmp_path: Path, **music: Any) -> None:
        self.store = ConfigStore(tmp_path / "config.json", {"music": music})
        self.store.load()
        self.fetcher = ImageFetcher(URLPolicy(), 5_000_000)
        self.limiter = NoWait()
        self.resolver = ArtworkResolver(self.store, self.fetcher, limiter=self.limiter)

    async def close(self) -> None:
        await self.resolver.aclose()
        await self.fetcher.aclose()


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[Setup]:
    instance = Setup(tmp_path)
    yield instance
    await instance.close()


def deezer_album(title: str, artist: str, cover: str) -> dict[str, Any]:
    return {"title": title, "artist": {"name": artist}, "cover_xl": cover}


@respx.mock
async def test_deezer_album_search_finds_the_xl_cover(setup: Setup) -> None:
    search = respx.get(f"{DEEZER}/search/album").respond(
        json={
            "data": [
                deezer_album("Kind of Blue (Legacy)", "Miles Davis", "https://cdn.test/legacy.jpg"),
                deezer_album("Kind of Blue", "Miles Davis", "https://cdn.test/kob.jpg"),
                deezer_album("Kind of Blue", "Tribute Band", "https://cdn.test/fake.jpg"),
            ]
        }
    )
    respx.get("https://cdn.test/kob.jpg").respond(content=png_bytes())

    artwork = await setup.resolver.find(title="So What", artist="Miles Davis", album="Kind of Blue")

    assert artwork is not None
    assert artwork.data == png_bytes()
    assert artwork.match.source == "deezer"
    assert artwork.match.album == "Kind of Blue"
    request = search.calls.last.request
    assert request.url.params["q"] == 'artist:"Miles Davis" album:"Kind of Blue"'
    assert request.headers["user-agent"] == USER_AGENT


@respx.mock
async def test_deezer_track_search_when_the_album_is_unknown(setup: Setup) -> None:
    album_search = respx.get(f"{DEEZER}/search/album")
    respx.get(f"{DEEZER}/search").respond(
        json={
            "data": [
                {
                    "title": "Bohemian Rhapsody",
                    "artist": {"name": "Queen"},
                    "album": {
                        "title": "A Night at the Opera",
                        "cover_xl": "https://cdn.test/q.jpg",
                    },
                }
            ]
        }
    )
    respx.get("https://cdn.test/q.jpg").respond(content=png_bytes())
    artwork = await setup.resolver.find(title="Bohemian Rhapsody", artist="Queen", album=None)
    assert artwork is not None and artwork.match.album == "A Night at the Opera"
    assert not album_search.called


@respx.mock
async def test_musicbrainz_and_cover_art_archive_fallback(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(json={"data": []})
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    mb = respx.get(f"{MB}/release-group/").respond(
        json={
            "release-groups": [
                {
                    "id": "low-score",
                    "score": 40,
                    "title": "Obscure",
                    "artist-credit": [{"name": "Indie Band"}],
                },
                {
                    "id": "rg-1",
                    "score": 100,
                    "title": "Obscure",
                    "artist-credit": [{"name": "Indie Band"}],
                },
            ]
        }
    )
    respx.get(f"{CAA}/release-group/rg-1/front-1200").respond(
        307, headers={"location": "https://archive.test/rg-1/front.jpg"}
    )
    respx.get("https://archive.test/rg-1/front.jpg").respond(content=png_bytes((1, 1, 1)))

    artwork = await setup.resolver.find(title="Track", artist="Indie Band", album="Obscure")

    assert artwork is not None
    assert artwork.match.source == "musicbrainz"
    assert artwork.data == png_bytes((1, 1, 1))
    params = mb.calls.last.request.url.params
    assert params["query"] == 'releasegroup:"Obscure" AND artist:"Indie Band"'
    assert params["fmt"] == "json"


@respx.mock
async def test_musicbrainz_recording_search_without_an_album(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    respx.get(f"{MB}/recording/").respond(
        json={
            "recordings": [
                {
                    "score": 95,
                    "artist-credit": [{"name": "Indie Band"}],
                    "releases": [{"title": "Single", "release-group": {"id": "rg-9"}}],
                }
            ]
        }
    )
    match = await setup.resolver.lookup(title="Track", artist="Indie Band", album=None)
    assert match is not None
    assert match.url == f"{CAA}/release-group/rg-9/front-1200"
    assert match.album == "Single"


@respx.mock
async def test_recording_search_prefers_the_studio_album_over_videos_and_compilations(
    setup: Setup,
) -> None:
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    respx.get(f"{MB}/recording/").respond(
        json={
            "recordings": [
                {
                    "score": 100,
                    "artist-credit": [{"name": "Daft Punk"}],
                    "releases": [
                        {
                            "release-group": {
                                "id": "video",
                                "title": "D.A.F.T.",
                                "primary-type": "Other",
                            }
                        },
                        {
                            "release-group": {
                                "id": "best-of",
                                "title": "Musique",
                                "primary-type": "Album",
                                "secondary-types": ["Compilation"],
                            }
                        },
                        {
                            "release-group": {
                                "id": "single",
                                "title": "Around the World",
                                "primary-type": "Single",
                            }
                        },
                        {
                            "release-group": {
                                "id": "homework",
                                "title": "Homework",
                                "primary-type": "Album",
                            }
                        },
                    ],
                }
            ]
        }
    )
    match = await setup.resolver.lookup(title="Around the World", artist="Daft Punk", album=None)
    assert match is not None
    assert match.album == "Homework"
    assert match.url.endswith("/release-group/homework/front-1200")


@respx.mock
async def test_nothing_found_returns_none_and_is_cached(setup: Setup) -> None:
    deezer = respx.get(f"{DEEZER}/search/album").respond(json={"data": []})
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    mb = respx.get(f"{MB}/release-group/").respond(json={"release-groups": []})
    assert await setup.resolver.find(title="x", artist="Nobody", album="Nothing") is None
    assert await setup.resolver.find(title="x", artist="nobody", album="NOTHING") is None
    assert deezer.call_count == 1 and mb.call_count == 1


@respx.mock
async def test_matches_are_cached(setup: Setup) -> None:
    search = respx.get(f"{DEEZER}/search/album").respond(
        json={"data": [deezer_album("Album", "Artist", "https://cdn.test/a.jpg")]}
    )
    download = respx.get("https://cdn.test/a.jpg").respond(content=png_bytes())
    for _ in range(3):
        assert await setup.resolver.find(title="Song", artist="Artist", album="Album") is not None
    assert search.call_count == 1
    assert download.call_count == 3  # the URL is cached, not the image


@respx.mock
async def test_transient_errors_are_not_cached(setup: Setup) -> None:
    route = respx.get(f"{DEEZER}/search/album").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{MB}/release-group/").respond(503)
    assert await setup.resolver.lookup(title="s", artist="A", album="B") is None

    route.mock(
        return_value=httpx.Response(
            200, json={"data": [deezer_album("B", "A", "https://cdn.test/b.jpg")]}
        )
    )
    match = await setup.resolver.lookup(title="s", artist="A", album="B")
    assert match is not None and match.url == "https://cdn.test/b.jpg"


@respx.mock
async def test_deezer_quota_errors_fall_through_to_musicbrainz(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(
        json={"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}
    )
    respx.get(f"{MB}/release-group/").respond(
        json={"release-groups": [{"id": "g", "score": 100, "artist-credit": [{"name": "A"}]}]}
    )
    match = await setup.resolver.lookup(title="s", artist="A", album="B")
    assert match is not None and match.source == "musicbrainz"


@respx.mock
async def test_sources_can_be_disabled(tmp_path: Path) -> None:
    setup = Setup(tmp_path, deezer_enabled=False, musicbrainz_enabled=False)
    deezer = respx.get(f"{DEEZER}/search/album")
    try:
        assert await setup.resolver.lookup(title="s", artist="A", album="B") is None
    finally:
        await setup.close()
    assert not deezer.called


@respx.mock
async def test_a_failed_cover_download_returns_none(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(
        json={"data": [deezer_album("B", "A", "https://cdn.test/missing.jpg")]}
    )
    respx.get("https://cdn.test/missing.jpg").respond(404)
    assert await setup.resolver.find(title="s", artist="A", album="B") is None


async def test_rate_limiter_spaces_out_calls() -> None:
    limiter = NoWait()
    await limiter.wait()  # t=0.0: first call is free
    await limiter.wait()  # t=0.1: waits 0.9 s (the clock advances 0.1 per reading)
    assert limiter.sleeps and limiter.sleeps[0] == pytest.approx(0.9)


def test_lucene_quote_escapes_special_characters() -> None:
    assert lucene_quote('AC/DC "Live"') == '"AC\\/DC \\"Live\\""'
    assert lucene_quote("Sigur Rós") == '"Sigur Rós"'
