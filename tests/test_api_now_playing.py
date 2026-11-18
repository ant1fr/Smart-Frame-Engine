from __future__ import annotations

import base64
import io
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from app.core.config_models import MusicConfig
from app.providers.music.models import NowPlayingRequest
from app.services.now_playing import content_key, cover_ttl
from tests.conftest import ClientFactory

DEEZER = "https://api.deezer.com"
MB = "https://musicbrainz.org/ws/2"


def png_bytes(color: tuple[int, int, int] = (20, 40, 160), size: int = 300) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


def frame_asleep() -> None:
    respx.get("http://frame.test/api/info").mock(side_effect=httpx.ConnectTimeout("asleep"))


def music_client(make_client: ClientFactory) -> TestClient:
    return make_client(fraimic_host="frame.test", fraimic_power_mode="deepsleep")


def play(client: TestClient, **body: Any) -> Any:
    body.setdefault("title", "So What")
    body.setdefault("artist", "Miles Davis")
    response = client.post("/api/display/now-playing", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@respx.mock
def test_provided_artwork_is_rendered_with_the_album_template(make_client: ClientFactory) -> None:
    frame_asleep()
    cover = respx.get("http://ha.test:8123/api/media_player_proxy/m").respond(content=png_bytes())
    deezer = respx.get(url__startswith=DEEZER)
    client = music_client(make_client)
    before = datetime.now(UTC)

    body = play(
        client,
        album="Kind of Blue",
        artwork_url="http://ha.test:8123/api/media_player_proxy/m?token=abc",
        duration_seconds=545,
        position_seconds=45,
    )

    assert cover.called and not deezer.called
    image = body["image"]
    assert image["template"] == "album"
    assert image["source"] == "music"
    assert image["title"] == "Kind of Blue — Miles Davis"
    assert "title" not in image["details"]
    assert image["details"]["artwork"] == "provided"
    assert image["ephemeral"] is True
    expires = datetime.fromisoformat(image["expires_at"])
    # 500 s left in the track + 30 s grace
    assert before + timedelta(seconds=525) < expires < datetime.now(UTC) + timedelta(seconds=535)
    assert body["delivery"]["status"] == "queued"
    assert client.get("/api/queue").json()["pending"]["ephemeral"] is True


@respx.mock
def test_broken_artwork_url_is_enriched_from_deezer(make_client: ClientFactory) -> None:
    frame_asleep()
    respx.get("https://ha.test/broken.jpg").respond(404)
    respx.get(f"{DEEZER}/search/album").respond(
        json={
            "data": [
                {
                    "title": "Kind of Blue",
                    "artist": {"name": "Miles Davis"},
                    "cover_xl": "https://cdn.test/kob.jpg",
                }
            ]
        }
    )
    respx.get("https://cdn.test/kob.jpg").respond(content=png_bytes((200, 30, 30), 1000))
    body = play(
        music_client(make_client),
        album="Kind of Blue",
        artwork_url="https://ha.test/broken.jpg",
        style="vinyl",
    )
    assert body["image"]["details"]["artwork"] == "deezer"
    assert body["image"]["details"]["style"] == "vinyl"


@respx.mock
def test_base64_artwork_is_used_first(make_client: ClientFactory) -> None:
    frame_asleep()
    url = respx.get("https://ha.test/cover.jpg").respond(content=png_bytes())
    encoded = base64.b64encode(png_bytes((5, 5, 5))).decode()
    body = play(
        music_client(make_client),
        artwork_base64=encoded,
        artwork_url="https://ha.test/cover.jpg",
    )
    assert body["image"]["details"]["artwork"] == "provided"
    assert not url.called


@respx.mock
def test_title_card_when_no_cover_can_be_found(make_client: ClientFactory) -> None:
    frame_asleep()
    respx.get(f"{DEEZER}/search").respond(json={"data": []})
    respx.get(f"{MB}/recording/").respond(json={"recordings": []})
    body = play(music_client(make_client), title="Unreleased Demo", artist="Garage Band")
    assert body["image"]["template"] == "title_card"
    assert body["image"]["details"]["artwork"] == "none"


@respx.mock
def test_enrichment_can_be_disabled(make_client: ClientFactory) -> None:
    frame_asleep()
    deezer = respx.get(url__startswith=DEEZER)
    body = play(music_client(make_client), enrich_artwork=False)
    assert body["image"]["template"] == "title_card"
    assert not deezer.called


@respx.mock
def test_same_track_is_deduplicated_and_only_extends_the_expiry(
    make_client: ClientFactory,
) -> None:
    frame_asleep()
    respx.get("https://ha.test/c.jpg").respond(content=png_bytes())
    client = music_client(make_client)
    first = play(client, album="Kind of Blue", artwork_url="https://ha.test/c.jpg", ttl_seconds=60)
    pending_sha = client.get("/api/queue").json()["pending"]["sha256"]

    again = play(client, album="kind of blue", artwork_url="https://ha.test/c.jpg", ttl_seconds=600)

    assert again["deduplicated"] is True
    assert again["delivery"] is None
    assert again["image"]["sha256"] == first["image"]["sha256"]
    first_expiry = datetime.fromisoformat(first["image"]["expires_at"])
    new_expiry = datetime.fromisoformat(again["image"]["expires_at"])
    assert new_expiry - first_expiry > timedelta(seconds=500)
    queue = client.get("/api/queue").json()
    assert queue["pending"]["sha256"] == pending_sha
    assert datetime.fromisoformat(queue["pending"]["expires_at"]) == new_expiry

    # Another track of the same album: same picture, nothing is sent, only the expiry moves.
    next_track = play(client, title="Freddie Freeloader", album="Kind of Blue", ttl_seconds=300)
    assert next_track["deduplicated"] is True
    assert next_track["delivery"] is None
    assert client.get("/api/queue").json()["pending"]["sha256"] == pending_sha

    other_album = play(
        client, title="Blue in Green", album="Sketches of Spain", enrich_artwork=False
    )
    assert other_album["deduplicated"] is False


@respx.mock
def test_printed_title_makes_every_track_a_new_picture(make_client: ClientFactory) -> None:
    frame_asleep()
    respx.get("https://ha.test/c.jpg").respond(content=png_bytes())
    client = music_client(make_client)
    fields = {"album": "Kind of Blue", "artwork_url": "https://ha.test/c.jpg", "show_title": True}
    first = play(client, **fields)
    assert first["image"]["title"] == "So What — Miles Davis"
    assert first["image"]["details"]["title"] == "So What"

    assert play(client, **fields)["deduplicated"] is True
    assert play(client, title="Blue in Green", **fields)["deduplicated"] is False


@respx.mock
def test_title_is_kept_without_an_album(make_client: ClientFactory) -> None:
    frame_asleep()
    respx.get("https://ha.test/c.jpg").respond(content=png_bytes())
    client = music_client(make_client)
    first = play(client, artwork_url="https://ha.test/c.jpg", show_title=False)
    assert first["image"]["title"] == "So What — Miles Davis"
    assert (
        play(client, title="Blue in Green", artwork_url="https://ha.test/c.jpg")["deduplicated"]
        is False
    )


@respx.mock
def test_title_is_optional_with_an_album(make_client: ClientFactory) -> None:
    frame_asleep()
    respx.get("https://ha.test/c.jpg").respond(content=png_bytes())
    client = music_client(make_client)
    body = client.post(
        "/api/display/now-playing",
        json={
            "artist": "Miles Davis",
            "album": "Kind of Blue",
            "artwork_url": "https://ha.test/c.jpg",
        },
    )
    assert body.status_code == 200, body.text
    assert body.json()["image"]["title"] == "Kind of Blue — Miles Davis"


@respx.mock
def test_push_false_renders_a_preview(make_client: ClientFactory) -> None:
    respx.get("https://ha.test/c.jpg").respond(content=png_bytes())
    client = music_client(make_client)
    body = play(client, artwork_url="https://ha.test/c.jpg", push_to_fraimic=False)
    assert body["delivery"] is None and body["deduplicated"] is False
    assert client.get("/api/current.jpg").status_code == 404
    assert client.get("/api/preview.jpg").status_code == 200


@respx.mock
def test_cancel_reverts_to_the_previous_image(make_client: ClientFactory) -> None:
    frame_asleep()
    respx.get("https://ha.test/c.jpg").respond(content=png_bytes())
    client = music_client(make_client)
    client.post(
        "/api/display/upload",
        files={"file": ("photo.png", png_bytes((90, 160, 60)), "image/png")},
    )
    base = client.get("/api/status").json()["current_image"]["sha256"]
    play(client, artwork_url="https://ha.test/c.jpg")
    outcome = client.delete("/api/queue/ephemeral").json()
    assert outcome["reverted"] is True
    assert client.get("/api/status").json()["current_image"]["sha256"] == base


def test_request_validation(make_client: ClientFactory) -> None:
    client = music_client(make_client)
    post = client.post
    assert post("/api/display/now-playing", json={"title": "x"}).status_code == 422
    assert post("/api/display/now-playing", json={"artist": "a"}).status_code == 422
    assert post("/api/display/now-playing", json={"title": "   ", "artist": "a"}).status_code == 422
    assert (
        post(
            "/api/display/now-playing", json={"title": "x", "artist": "a", "style": "cassette"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"ttl_seconds": 90, "duration_seconds": 300}, timedelta(seconds=90)),
        ({"duration_seconds": 200, "position_seconds": 50}, timedelta(seconds=180)),
        ({"duration_seconds": 200, "position_seconds": 250}, timedelta(minutes=10)),
        ({}, timedelta(minutes=10)),
    ],
)
def test_cover_ttl(fields: dict[str, Any], expected: timedelta) -> None:
    request = NowPlayingRequest(title="t", artist="a", **fields)
    assert cover_ttl(request, MusicConfig()) == expected


def test_content_key_ignores_case_accents_but_not_style() -> None:
    a = NowPlayingRequest(title="Café", artist="Sigur Rós", album="Ágætis byrjun")
    b = NowPlayingRequest(title="cafe", artist="SIGUR ROS", album="agaetis byrjun")
    assert content_key(a, "sleeve", show_title=True) == content_key(b, "sleeve", show_title=True)
    assert content_key(a, "sleeve", show_title=True) != content_key(a, "vinyl", show_title=True)


def test_content_key_ignores_the_title_unless_it_is_printed() -> None:
    a = NowPlayingRequest(
        title="Speak to Me", artist="Pink Floyd", album="The Dark Side of the Moon"
    )
    b = a.model_copy(update={"title": "Breathe"})
    assert content_key(a, "sleeve", show_title=False) == content_key(b, "sleeve", show_title=False)
    assert content_key(a, "sleeve", show_title=True) != content_key(b, "sleeve", show_title=True)


def test_shows_title_rules() -> None:
    with_album = NowPlayingRequest(title="t", artist="a", album="x")
    assert with_album.shows_title(False) is False
    assert with_album.shows_title(True) is True
    assert with_album.model_copy(update={"show_title": True}).shows_title(False) is True
    assert with_album.model_copy(update={"show_title": False}).shows_title(True) is False
    single = NowPlayingRequest(title="t", artist="a")
    assert single.shows_title(False) is True
    assert NowPlayingRequest(artist="a", album="x").shows_title(True) is False
