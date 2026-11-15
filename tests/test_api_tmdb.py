from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import ClientFactory

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"
KEY = "0123456789abcdef0123456789abcdef"


def png_bytes(color: tuple[int, int, int] = (180, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (200, 300), color).save(buffer, format="PNG")
    return buffer.getvalue()


def mock_amelie() -> None:
    # The deep-sleeping frame never answers (the worker probes it after a push).
    respx.get("http://frame.test/api/info").mock(side_effect=httpx.ConnectTimeout("asleep"))
    respx.get(f"{API}/search/movie").respond(
        json={"results": [{"id": 194, "title": "Amélie", "release_date": "2001-04-25"}]}
    )
    respx.get(f"{API}/movie/194/images").respond(
        json={"posters": [{"file_path": "/fr.jpg", "iso_639_1": "fr", "iso_3166_1": "FR"}]}
    )
    respx.get(f"{IMG}/w780/fr.jpg").respond(content=png_bytes())


def tmdb_client(make_client: ClientFactory, **overrides: Any) -> TestClient:
    overrides.setdefault("tmdb_api_key", KEY)
    return make_client(fraimic_host="frame.test", fraimic_power_mode="deepsleep", **overrides)


def show(client: TestClient, **body: Any) -> Any:
    body.setdefault("query", "Amélie")
    body.setdefault("media_type", "movie")
    response = client.post("/api/display/tmdb", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@respx.mock
def test_ephemeral_poster_push(make_client: ClientFactory) -> None:
    mock_amelie()
    client = tmdb_client(make_client)
    before = datetime.now(UTC)
    body = show(client)

    image = body["image"]
    assert image["template"] == "poster"
    assert image["source"] == "tmdb"
    assert image["ephemeral"] is True
    expires = datetime.fromisoformat(image["expires_at"])
    assert before + timedelta(minutes=179) < expires < datetime.now(UTC) + timedelta(minutes=181)
    assert body["delivery"]["status"] == "queued"

    queue = client.get("/api/queue").json()
    assert queue["pending"]["ephemeral"] is True
    assert queue["ephemeral"]["active"] and queue["ephemeral"]["template"] == "poster"
    status = client.get("/api/status").json()
    assert status["scheduler"]["active_rule"] is None  # an ephemeral push is not replayed


@respx.mock
def test_ttl_and_hold_overrides(make_client: ClientFactory) -> None:
    mock_amelie()
    client = tmdb_client(make_client)
    assert client.put("/api/config", json={"tmdb": {"hold_minutes": 30}}).status_code == 200
    body = show(client, ttl_minutes=5)
    expires = datetime.fromisoformat(body["image"]["expires_at"])
    assert expires < datetime.now(UTC) + timedelta(minutes=6)
    hold = client.get("/api/status").json()["scheduler"]["hold_until"]
    assert hold is not None


@respx.mock
def test_non_ephemeral_poster_becomes_the_replayed_rule(make_client: ClientFactory) -> None:
    mock_amelie()
    client = tmdb_client(make_client)
    body = show(client, ephemeral=False, fallback_base64="aGVsbG8=")
    assert body["image"]["ephemeral"] is False
    assert body["image"]["expires_at"] is None
    status = client.get("/api/status").json()
    assert status["scheduler"]["active_rule"] == {"kind": "tmdb", "source": "last"}

    refreshed = client.post("/api/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["rule_kind"] == "tmdb"
    assert refreshed.json()["rule_source"] == "last"
    state = client.app.state.scheduler_service.state  # type: ignore[attr-defined]
    assert state.last_rule.params == {"query": "Amélie", "media_type": "movie"}


@respx.mock
def test_push_false_renders_a_preview_only(make_client: ClientFactory) -> None:
    mock_amelie()
    client = tmdb_client(make_client)
    body = show(client, push_to_fraimic=False)
    assert body["delivery"] is None
    assert client.get("/api/current.jpg").status_code == 404
    preview = client.get("/api/preview.jpg")
    assert preview.status_code == 200
    assert Image.open(io.BytesIO(preview.content)).size == (1200, 1600)
    assert client.get("/api/queue").json()["pending"] is None


def test_title_card_when_tmdb_is_not_configured(make_client: ClientFactory) -> None:
    client = tmdb_client(make_client, tmdb_api_key="")
    body = show(client, query="Some Home Movie", year=1999)
    assert body["image"]["template"] == "title_card"
    assert body["image"]["details"]["fallback"] == "title_card"


def test_errors_without_any_fallback(make_client: ClientFactory) -> None:
    client = tmdb_client(make_client, tmdb_api_key="")
    assert (
        client.put("/api/config", json={"tmdb": {"fallback_title_card": False}}).status_code == 200
    )
    response = client.post("/api/display/tmdb", json={"query": "x"})
    assert response.status_code == 409
    invalid = {"query": "x", "season": 1, "media_type": "movie"}
    assert client.post("/api/display/tmdb", json=invalid).status_code == 422


@respx.mock
def test_media_alias_accepts_title_and_auto(make_client: ClientFactory) -> None:
    respx.get(f"{API}/search/multi").respond(
        json={"results": [{"id": 194, "media_type": "movie", "title": "Amélie"}]}
    )
    respx.get(f"{API}/movie/194/images").respond(json={"posters": [{"file_path": "/fr.jpg"}]})
    respx.get(f"{IMG}/w780/fr.jpg").respond(content=png_bytes())
    client = tmdb_client(make_client)
    response = client.post(
        "/api/display/media",
        json={"title": "Amélie", "media_type": "auto", "push_to_fraimic": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["image"]["details"]["tmdb_id"] == 194


@respx.mock
def test_smart_mix_entries_can_replay_posters(make_client: ClientFactory) -> None:
    mock_amelie()
    client = tmdb_client(make_client)
    entry = {"kind": "tmdb", "params": {"query": "Amélie", "media_type": "movie"}, "label": "Film"}
    response = client.put("/api/config", json={"smart_mix": {"enabled": True, "entries": [entry]}})
    assert response.status_code == 200
    refreshed = client.post("/api/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["rule_kind"] == "tmdb"
    assert body["rule_source"] == "smart_mix"
    assert body["entry_label"] == "Film"
    current = client.get("/api/status").json()["current_image"]
    assert current["template"] == "poster" and current["ephemeral"] is False


def test_smart_mix_context_validates_tmdb_params(make_client: ClientFactory) -> None:
    client = tmdb_client(make_client)
    assert client.put("/api/config", json={"smart_mix": {"enabled": True}}).status_code == 200
    bad = client.post("/api/smart-mix/context", json={"kind": "tmdb", "params": {"a": 1}})
    assert bad.status_code == 422
    good = client.post(
        "/api/smart-mix/context", json={"kind": "tmdb", "params": {"query": "Amélie"}}
    )
    assert good.status_code == 200, good.text
    assert good.json()["kind"] == "tmdb"
