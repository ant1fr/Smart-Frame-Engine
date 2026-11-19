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
from app.providers.music.artwork import (
    ArtworkMatch,
    artist_match_level,
    lucene_quote,
    sized_cover_url,
    title_match_level,
)

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


@respx.mock
async def test_track_search_finds_the_real_album_hidden_by_lookalikes(setup: Setup) -> None:
    """Real case: the album search returns only covers and a remix EP of "Back to Black"."""
    respx.get(f"{DEEZER}/search/album").respond(
        json={
            "data": [
                deezer_album("Back To Black", "Demi Chang", "https://cdn.test/cover.jpg"),
                {
                    **deezer_album(
                        "Back To Black - The Singles Remixes",
                        "Amy Winehouse",
                        "https://cdn.test/remixes.jpg",
                    ),
                    "record_type": "ep",
                },
            ]
        }
    )
    respx.get(f"{DEEZER}/search").respond(
        json={
            "data": [
                {
                    "title": "Rehab",
                    "artist": {"name": "Amy Winehouse"},
                    "album": {"title": "Back To Black", "cover_xl": "https://cdn.test/btb.jpg"},
                }
            ]
        }
    )
    mb = respx.get(f"{MB}/release-group/")
    match = await setup.resolver.lookup(
        title="Rehab", artist="Amy Winehouse", album="Back to Black"
    )
    assert match is not None and match.url == "https://cdn.test/btb.jpg"
    assert not mb.called


@respx.mock
async def test_musicbrainz_exact_match_beats_an_approximate_deezer_result(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(
        json={"data": [deezer_album("Homework (Remixes)", "Daft Punk", "https://cdn.test/rmx.jpg")]}
    )
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    respx.get(f"{MB}/release-group/").respond(
        json={
            "release-groups": [
                {
                    "id": "hw",
                    "score": 100,
                    "title": "Homework",
                    "artist-credit": [{"name": "Daft Punk"}],
                }
            ]
        }
    )
    match = await setup.resolver.lookup(title="Da Funk", artist="Daft Punk", album="Homework")
    assert match is not None and match.source == "musicbrainz"


@respx.mock
async def test_approximate_deezer_result_is_the_last_resort(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(
        json={"data": [deezer_album("Homework (Remixes)", "Daft Punk", "https://cdn.test/rmx.jpg")]}
    )
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    respx.get(f"{MB}/release-group/").respond(json={"release-groups": []})
    match = await setup.resolver.lookup(title="Da Funk", artist="Daft Punk", album="Homework")
    assert match is not None and match.url == "https://cdn.test/rmx.jpg"


@respx.mock
async def test_throttled_musicbrainz_requests_are_retried(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(json={"data": []})
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    mb = respx.get(f"{MB}/release-group/").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(
                200,
                json={
                    "release-groups": [
                        {"id": "hw", "score": 100, "artist-credit": [{"name": "Daft Punk"}]}
                    ]
                },
            ),
        ]
    )
    match = await setup.resolver.lookup(title="Da Funk", artist="Daft Punk", album="Homework")
    assert match is not None and match.source == "musicbrainz"
    assert mb.call_count == 2
    assert 2.0 in setup.limiter.sleeps  # backed off before retrying


@respx.mock
async def test_musicbrainz_gives_up_after_the_retries(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(json={"data": []})
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    mb = respx.get(f"{MB}/release-group/").respond(503)
    assert await setup.resolver.lookup(title="x", artist="A", album="B") is None
    assert mb.call_count == 3


@pytest.mark.parametrize(
    ("candidate", "level"),
    [
        ("Abbey Road", 0),
        ("ABBEY ROAD", 0),
        ("Abbey Road (Remastered 2009)", 1),
        ("Abbey Road (2019 Mix)", 1),
        ("Abbey Road (Super Deluxe Edition)", 1),
        ("Abbey Road [Anniversary Edition] (Remastered)", 1),
        ("Abbey Road (Live)", 2),
        ("Abbey Road (Remixes)", 2),
        ("Abbey Road - The Singles Remixes", 2),
        ("Abbey Road (Tribute Edition)", 2),
        ("Let It Be", 2),
    ],
)
def test_title_match_level(candidate: str, level: int) -> None:
    assert title_match_level("Abbey Road", candidate) == level


def test_artist_match_level() -> None:
    assert artist_match_level("The Beatles", "the beatles") == 0
    assert artist_match_level("The Beatles", "The Beatles Complete On Ukulele") == 1
    assert artist_match_level("Queen", "Queen + David Bowie") == 1
    assert artist_match_level("Queen", "Madonna") is None
    assert artist_match_level("Queen", None) is None


@respx.mock
async def test_the_real_artist_beats_a_tribute_with_the_exact_title(setup: Setup) -> None:
    """Real case: "Abbey Road" by a ukulele tribute vs "Abbey Road (2019 Mix)" by The Beatles."""
    respx.get(f"{DEEZER}/search/album").respond(
        json={
            "data": [
                {
                    **deezer_album(
                        "Abbey Road (2019 Mix)", "The Beatles", "https://cdn.test/mix.jpg"
                    ),
                    "record_type": "album",
                },
                deezer_album(
                    "Abbey Road", "The Beatles Complete On Ukulele", "https://cdn.test/uke.jpg"
                ),
            ]
        }
    )
    mb = respx.get(f"{MB}/release-group/")
    match = await setup.resolver.lookup(
        title="Come Together", artist="The Beatles", album="Abbey Road"
    )
    assert match is not None and match.url == "https://cdn.test/mix.jpg"
    assert not mb.called  # an edition of the right album by the right artist is a sure match


DZ_XL = "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000-000000-80-0-0.jpg"
DZ_Q100 = "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000-000000-100-0-0.jpg"
DZ_1400 = "https://cdn-images.dzcdn.net/images/cover/abc/1400x1400-000000-100-0-0.jpg"


@pytest.mark.parametrize(
    ("url", "source", "side", "expected"),
    [
        (DZ_XL, "deezer", 984, DZ_Q100),  # same size, without JPEG artifacts
        (DZ_XL, "deezer", 1181, DZ_1400),  # larger slot (31.5" panel)
        ("https://cdn.test/a.jpg", "deezer", 984, "https://cdn.test/a.jpg"),
        (
            f"{CAA}/release-group/g/front-1200",
            "musicbrainz",
            984,
            f"{CAA}/release-group/g/front-1200",
        ),
        (f"{CAA}/release-group/g/front-1200", "musicbrainz", 1268, f"{CAA}/release-group/g/front"),
    ],
)
def test_sized_cover_url(url: str, source: str, side: int, expected: str) -> None:
    assert sized_cover_url(ArtworkMatch(url, source), side) == expected  # type: ignore[arg-type]


@respx.mock
async def test_find_downloads_the_high_quality_deezer_cover(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(json={"data": [deezer_album("B", "A", DZ_XL)]})
    q100 = respx.get(DZ_Q100).respond(content=png_bytes((1, 2, 3)))
    xl = respx.get(DZ_XL).respond(content=png_bytes())
    artwork = await setup.resolver.find(title="s", artist="A", album="B", min_side=984)
    assert artwork is not None and artwork.data == png_bytes((1, 2, 3))
    assert q100.called and not xl.called


@respx.mock
async def test_find_falls_back_to_the_standard_cover_url(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(json={"data": [deezer_album("B", "A", DZ_XL)]})
    respx.get(DZ_1400).respond(403)
    respx.get(DZ_XL).respond(content=png_bytes())
    artwork = await setup.resolver.find(title="s", artist="A", album="B", min_side=1181)
    assert artwork is not None and artwork.data == png_bytes()


@respx.mock
async def test_musicbrainz_prefers_the_album_over_a_single_with_the_same_title(
    setup: Setup,
) -> None:
    """Real case: "Thriller" by Michael Jackson is a Single (listed first) and the Album."""
    respx.get(f"{DEEZER}/search/album").respond(json={"data": []})
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    credit = [{"name": "Michael Jackson"}]
    respx.get(f"{MB}/release-group/").respond(
        json={
            "release-groups": [
                {
                    "id": "single",
                    "score": 100,
                    "title": "Thriller",
                    "primary-type": "Single",
                    "artist-credit": credit,
                },
                {
                    "id": "album",
                    "score": 100,
                    "title": "Thriller",
                    "primary-type": "Album",
                    "artist-credit": credit,
                },
                {
                    "id": "demos",
                    "score": 87,
                    "title": "The Thriller Demos",
                    "primary-type": "Album",
                    "secondary-types": ["Demo"],
                    "artist-credit": credit,
                },
            ]
        }
    )
    match = await setup.resolver.lookup(
        title="Thriller", artist="Michael Jackson", album="Thriller"
    )
    assert match is not None and match.url.endswith("/release-group/album/front-1200")


@respx.mock
async def test_musicbrainz_title_beats_type_and_order_breaks_ties(setup: Setup) -> None:
    respx.get(f"{DEEZER}/search/album").respond(json={"data": []})
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    credit = [{"name": "Band"}]
    respx.get(f"{MB}/release-group/").respond(
        json={
            "release-groups": [
                {
                    "id": "greatest",
                    "score": 100,
                    "title": "Greatest Hits",
                    "primary-type": "Album",
                    "artist-credit": credit,
                },
                {
                    "id": "first",
                    "score": 95,
                    "title": "Wanted",
                    "primary-type": "Single",
                    "artist-credit": credit,
                },
                {
                    "id": "second",
                    "score": 95,
                    "title": "Wanted",
                    "primary-type": "Single",
                    "artist-credit": credit,
                },
            ]
        }
    )
    match = await setup.resolver.lookup(title="x", artist="Band", album="Wanted")
    assert match is not None and "/first/" in match.url  # exact title wins, then MB order
