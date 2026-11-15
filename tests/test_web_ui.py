"""Web UI pages and the endpoints added for them (activity, playground, title card, helpers)."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from app.api.routes.auth import page
from tests.conftest import ClientFactory
from tests.immich_fake import FakeImmich

TMDB = "https://api.themoviedb.org/3"
TMDB_KEY = "0123456789abcdef0123456789abcdef"


def png_bytes(
    size: tuple[int, int] = (320, 240), color: tuple[int, int, int] = (40, 120, 200)
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def decode_data_url(url: str) -> Image.Image:
    header, payload = url.split(",", 1)
    assert header.startswith("data:image/") and header.endswith(";base64")
    return Image.open(io.BytesIO(base64.b64decode(payload)))


# --- Pages ------------------------------------------------------------------------------
ABSOLUTE_URL = re.compile(r"""(?:href|src|action)=["']/(?!/)|fetch\(\s*["'`]/|["'`]/api/""")


@pytest.mark.parametrize("name", ["index.html", "login.html"])
def test_pages_only_use_relative_urls(name: str) -> None:
    html = page(name)
    assert not ABSOLUTE_URL.search(html), ABSOLUTE_URL.search(html)
    # Third-party scripts are pinned to exact versions.
    for url in re.findall(r'src="(https://[^"]+)"', html):
        assert re.search(r"\d+\.\d+\.\d+", url), url


def test_index_is_served_at_root(make_client: ClientFactory) -> None:
    client = make_client()
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert 'x-data="app()"' in response.text
    assert client.get("/docs").status_code == 200


def test_index_under_root_path_and_password(make_client: ClientFactory) -> None:
    client = make_client(root_path="/fraimic", admin_password="correct horse")
    anonymous = client.get("/fraimic/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/fraimic/login"

    login = client.post(
        "/fraimic/api/auth/login",
        json={"username": "admin", "password": "correct horse"},
        headers={"X-Requested-With": "SmartFrame"},
    )
    assert login.status_code == 200
    assert client.get("/fraimic/").status_code == 200
    assert client.get("/fraimic/api/activity").status_code == 200


@pytest.mark.parametrize("path", ["/api/activity", "/api/tmdb/search?q=x"])
def test_ui_helpers_need_the_key_in_key_mode(make_client: ClientFactory, path: str) -> None:
    client = make_client(app_api_key="secret-key-1234")
    assert client.get(path).status_code == 401


# --- Activity ---------------------------------------------------------------------------
def test_activity_records_renders_and_masked_logs(make_client: ClientFactory) -> None:
    client = make_client(app_api_key="secret-key-1234")
    headers = {"X-API-Key": "secret-key-1234"}
    upload = client.post(
        "/api/display/upload",
        files={"file": ("beach.png", png_bytes(), "image/png")},
        data={"push_to_fraimic": "true"},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    logging.getLogger("app.test").warning("leaked secret-key-1234 here")

    body = client.get("/api/activity", headers=headers).json()
    assert body["renders"][0]["source"] == "upload"
    assert body["renders"][0]["title"] == "beach.png"
    assert body["renders"][0]["render"]["profile"]
    messages = [line["message"] for line in body["logs"]]
    assert any("leaked *** here" in message for message in messages)
    assert not any("secret-key-1234" in message for message in messages)

    limited = client.get("/api/activity?logs=1", headers=headers).json()
    assert len(limited["logs"]) == 1


def test_preview_renders_are_not_activity(make_client: ClientFactory) -> None:
    client = make_client()
    response = client.post(
        "/api/display/upload",
        files={"file": ("x.png", png_bytes(), "image/png")},
        data={"push_to_fraimic": "false"},
    )
    assert response.status_code == 200
    assert client.get("/api/activity").json()["renders"] == []


# --- Title card -------------------------------------------------------------------------
def test_title_card_preview_and_push(make_client: ClientFactory) -> None:
    client = make_client()
    preview = client.post(
        "/api/display/title-card",
        json={"title": "Welcome home", "subtitle": "Grandma", "push_to_fraimic": False},
    )
    assert preview.status_code == 200, preview.text
    image = preview.json()["image"]
    assert image["template"] == "title_card"
    assert image["pushed"] is False
    assert image["render"]["context"] == "title_card"
    assert client.get("/api/status").json()["current_image"] is None

    pushed = client.post(
        "/api/display/title-card", json={"title": "Dinner", "accent": "blue", "ttl_minutes": 5}
    )
    assert pushed.status_code == 200
    current = client.get("/api/status").json()
    assert current["current_image"]["title"] == "Dinner"
    assert current["ephemeral"]["active"] is True
    assert current["ephemeral"]["expires_at"] is not None


@pytest.mark.parametrize(
    "body",
    [{"title": "   "}, {"title": "x", "accent": "purple"}, {"title": "x", "extra": 1}],
)
def test_title_card_validation(make_client: ClientFactory, body: dict[str, Any]) -> None:
    assert make_client().post("/api/display/title-card", json=body).status_code == 422


# --- Playground -------------------------------------------------------------------------
def test_playground_renders_variants_without_displaying(make_client: ClientFactory) -> None:
    client = make_client()
    variants = [{"profile": "auto", "dither": "auto"}, {"profile": "vivid", "dither": "stucki"}]
    response = client.post(
        "/api/playground/render",
        files={"file": ("p.png", png_bytes(), "image/png")},
        data={"variants": json.dumps(variants)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template"] == "none"
    width, height = body["frame_size"]
    assert len(body["variants"]) == 2
    auto, manual = body["variants"]
    assert auto["decision"]["automatic"] is True
    assert manual["decision"] == {
        "context": "photo",
        "profile": "vivid",
        "dither": "stucki",
        "automatic": False,
        "reasons": manual["decision"]["reasons"],
    }
    for variant in body["variants"]:
        assert decode_data_url(variant["composed"]).size == (width, height)
        dithered = decode_data_url(variant["dithered"])
        assert dithered.size == (width, height) and dithered.mode == "P"
        assert variant["elapsed_ms"] > 0

    status = client.get("/api/status").json()
    assert status["current_image"] is None
    assert client.get("/api/preview.jpg").status_code == 404


def test_playground_templates(make_client: ClientFactory) -> None:
    client = make_client()
    card = client.post("/api/playground/render", data={"template": "title_card", "title": "Hello"})
    assert card.status_code == 200, card.text
    assert card.json()["variants"][0]["decision"]["context"] == "title_card"

    album = client.post(
        "/api/playground/render",
        files={"file": ("c.png", png_bytes((300, 300)), "image/png")},
        data={"template": "album", "title": "Billie Jean", "subtitle": "Michael Jackson"},
    )
    assert album.status_code == 200
    assert album.json()["variants"][0]["decision"]["context"] == "album"


@pytest.mark.parametrize(
    ("data", "files", "status"),
    [
        ({}, None, 422),  # no image
        ({"variants": "not json"}, "png", 422),
        ({"variants": json.dumps([{"profile": "nope"}])}, "png", 422),
        ({"variants": json.dumps([{}] * 5)}, "png", 422),
        ({"variants": "[]"}, "png", 422),
        ({}, "text", 415),
    ],
)
def test_playground_rejects_bad_input(
    make_client: ClientFactory, data: dict[str, str], files: str | None, status: int
) -> None:
    upload = None
    if files == "png":
        upload = {"file": ("p.png", png_bytes(), "image/png")}
    elif files == "text":
        upload = {"file": ("p.txt", b"not an image at all", "text/plain")}
    response = make_client().post("/api/playground/render", data=data, files=upload)
    assert response.status_code == status, response.text


# --- TMDB helpers -----------------------------------------------------------------------
@respx.mock
def test_tmdb_search_returns_thumbnails(make_client: ClientFactory) -> None:
    route = respx.get(f"{TMDB}/search/multi").respond(
        json={
            "results": [
                {
                    "id": 77338,
                    "media_type": "movie",
                    "title": "Intouchables",
                    "release_date": "2011-11-02",
                    "poster_path": "/poster.jpg",
                },
                {"id": 1, "media_type": "person", "name": "Someone"},
                {"id": 2, "media_type": "tv", "name": "No Poster", "first_air_date": ""},
            ]
        }
    )
    client = make_client(tmdb_api_key=TMDB_KEY)
    response = client.get("/api/tmdb/search", params={"q": " Intouchables ", "language": "en-US"})
    assert response.status_code == 200, response.text
    hits = response.json()
    assert hits[0] == {
        "id": 77338,
        "media_type": "movie",
        "title": "Intouchables",
        "original_title": None,
        "year": 2011,
        "poster_thumbnail": "https://image.tmdb.org/t/p/w185/poster.jpg",
    }
    assert hits[1]["poster_thumbnail"] is None and len(hits) == 2
    sent = route.calls.last.request.url.params
    assert sent["query"] == "Intouchables" and sent["language"] == "en-US"


def test_tmdb_search_errors(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.get("/api/tmdb/search", params={"q": "x"}).status_code in {409, 503}
    keyed = make_client(tmdb_api_key=TMDB_KEY)
    bad_language = keyed.get("/api/tmdb/search", params={"q": "x", "language": "french"})
    assert bad_language.status_code == 422


@respx.mock
def test_tmdb_key_test(make_client: ClientFactory) -> None:
    route = respx.get(f"{TMDB}/configuration")
    route.side_effect = lambda request: httpx.Response(
        200 if request.url.params.get("api_key") == TMDB_KEY else 401, json={}
    )
    client = make_client(tmdb_api_key=TMDB_KEY)
    masked = client.get("/api/config").json()["config"]["tmdb"]["api_key"]
    assert client.post("/api/tmdb/test", json={"api_key": masked}).json() == {
        "ok": True,
        "error": None,
    }
    assert client.post("/api/tmdb/test", json={}).json()["ok"] is True
    rejected = client.post("/api/tmdb/test", json={"api_key": "wrong"}).json()
    assert rejected == {"ok": False, "error": "TMDB rejected the API key"}


def test_tmdb_key_test_without_any_key(make_client: ClientFactory) -> None:
    result = make_client().post("/api/tmdb/test", json={}).json()
    assert result["ok"] is False and "not configured" in result["error"]


# --- Immich helpers ---------------------------------------------------------------------
@contextmanager
def immich(fake: FakeImmich) -> Iterator[None]:
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        yield


def library() -> FakeImmich:
    fake = FakeImmich()
    fake.add_person("p1", "Alice")
    fake.add_asset("a1", people=[("p1", "Alice")])
    fake.add_album("al1", "Holidays", ["a1"])
    fake.person_thumbnails["p1"] = png_bytes((64, 64))
    fake.files[("a1", "thumbnail")] = png_bytes((250, 250))
    return fake


def immich_client(make_client: ClientFactory) -> TestClient:
    return make_client(immich_url="http://immich.test", immich_api_key="good-key")


def test_album_thumbnails_and_person_faces(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    with immich(library()):
        albums = client.get("/api/immich/albums").json()["albums"]
        assert albums[0]["thumbnail_asset_id"] == "a1"
        cover = client.get("/api/immich/thumbnail/default/asset/a1")
        assert cover.status_code == 200
        assert cover.headers["cache-control"] == "private, max-age=86400"
        face = client.get("/api/immich/thumbnail/default/person/p1")
        assert face.status_code == 200 and face.headers["content-type"] == "image/png"
        assert client.get("/api/immich/thumbnail/default/person/nobody").status_code == 404
        assert client.get("/api/immich/thumbnail/default/person/bad%20id").status_code == 422
        assert client.get("/api/immich/thumbnail/default/album/a1").status_code == 422
        assert client.get("/api/immich/thumbnail/ghost/asset/a1").status_code == 422


def test_immich_connection_test(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    with immich(library()):
        stored = client.post("/api/immich/test", json={"account": "default"}).json()
        assert stored["ok"] is True and stored["people"] == 1 and stored["albums"] == 1
        assert stored["version"]

        masked = client.get("/api/config").json()["config"]["immich"]["accounts"]["default"]
        same = client.post(
            "/api/immich/test", json={"account": "default", "api_key": masked["api_key"]}
        ).json()
        assert same["ok"] is True

        wrong = client.post(
            "/api/immich/test", json={"url": "http://immich.test", "api_key": "bad-key"}
        ).json()
        assert wrong["ok"] is False and wrong["error"]

    missing = client.post("/api/immich/test", json={"url": "http://immich.test"}).json()
    assert missing == {
        "ok": False,
        "version": None,
        "people": None,
        "albums": None,
        "error": "Give a URL and an API key",
    }
    invalid = client.post("/api/immich/test", json={"url": "ftp://x", "api_key": "k"}).json()
    assert invalid["ok"] is False
