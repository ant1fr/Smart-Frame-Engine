"""API tests for the Immich display, albums and people endpoints (fake Immich + fake frame)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from app.core.security import InvalidImageError
from tests.conftest import ClientFactory
from tests.immich_fake import FakeImmich, face

FRAME = "http://frame.test"
KEY = "api-secret"


@contextmanager
def servers(*fakes: FakeImmich, frame: bool = True) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        for fake in fakes:
            fake.mount(router)
        if frame:
            router.get(f"{FRAME}/api/info").respond(
                json={"battery": {"percent": 70}, "firmware_version": "1"}
            )
            router.post(f"{FRAME}/api/image").respond(200)
        yield router


def family() -> FakeImmich:
    fake = FakeImmich()
    fake.add_person("p1", "Alice")
    fake.add_person("p2", "Bob")
    fake.add_asset("a1", people=[("p1", "Alice")], faces=[face("p1", (300, 100, 400, 200))])
    fake.add_asset("a2", people=[("p2", "Bob")])
    return fake


def immich_client(make_client: ClientFactory, **overrides: Any) -> TestClient:
    overrides.setdefault("immich_url", "http://immich.test")
    overrides.setdefault("immich_api_key", "good-key")
    overrides.setdefault("fraimic_host", "frame.test")
    return make_client(**overrides)


def show(client: TestClient, **body: Any) -> httpx.Response:
    return client.post("/api/display/immich", json=body)


def people_calls(fake: FakeImmich) -> int:
    return len([1 for _, path, _ in fake.calls if path == "/api/people"])


# --- display ------------------------------------------------------------------
def test_display_renders_delivers_and_records_history(make_client: ClientFactory) -> None:
    fake = family()
    client = immich_client(make_client)
    with servers(fake):
        response = show(client, persons=["Alice"])
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["image"]["source"] == "immich"
        assert body["image"]["title"] == "Alice · 14 Jul 2024 · Lyon, France"
        assert body["image"]["details"]["account"] == "default"
        assert body["image"]["bin_size"] == 960_000
        assert body["delivery"]["status"] == "delivered"

    history = client.app.state.history_store  # type: ignore[attr-defined]
    assert history.recent_ids("immich", 30) == {"sha1:sum-a1"}
    assert client.get("/api/status").json()["current_image"]["source"] == "immich"

    with servers(fake):
        # The only matching photo is now on cooldown...
        again = show(client, persons=["Alice"])
        assert again.status_code == 404 and "ignore_history" in again.json()["detail"]
        # ...unless the request says otherwise.
        assert show(client, persons=["Alice"], ignore_history=True).status_code == 200


def test_push_to_fraimic_false_does_not_contact_the_frame(make_client: ClientFactory) -> None:
    fake = family()
    client = immich_client(make_client)
    with servers(fake) as router:
        response = show(client, push_to_fraimic=False)
        assert response.status_code == 200
        assert response.json()["delivery"] is None
        assert not [c for c in router.calls if c.request.url.host == "frame.test"]


def test_display_uses_face_regions_for_the_crop(make_client: ClientFactory) -> None:
    # Wide photo, portrait frame: a centered crop would miss the red face at the far right.
    image = Image.new("RGB", (1600, 900), (255, 255, 255))
    image.paste((255, 0, 0), (1450, 300, 1550, 400))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    fake = FakeImmich()
    fake.add_person("p1", "Alice")
    fake.add_asset(
        "wide",
        people=[("p1", "Alice")],
        faces=[face("p1", (1450, 300, 1550, 400), frame=(1600, 900))],
        original=buffer.getvalue(),
        width=1600,
        height=900,
    )
    client = immich_client(make_client)
    with servers(fake, frame=False):
        assert show(client, persons=["Alice"], push_to_fraimic=False).status_code == 200
    current = np.asarray(Image.open(io.BytesIO(client.get("/api/preview.jpg").content)))
    red = (current[..., 0] > 200) & (current[..., 1] < 80) & (current[..., 2] < 80)
    assert red.sum() > 1000  # the face is inside the 1200x1600 crop


def test_fit_override_is_forwarded(make_client: ClientFactory) -> None:
    fake = family()
    client = immich_client(make_client)
    with servers(fake, frame=False):
        response = show(client, fit="contain", push_to_fraimic=False)
    assert response.json()["image"]["fit"] == "contain"


# --- errors -------------------------------------------------------------------
def test_error_statuses(make_client: ClientFactory) -> None:
    fake = family()
    client = immich_client(make_client)
    with servers(fake, frame=False):
        unknown = show(client, persons=["Zed"], push_to_fraimic=False)
        assert unknown.status_code == 422 and "Unknown person" in unknown.json()["detail"]
        assert show(client, mode="album", push_to_fraimic=False).status_code == 422
        assert show(client, accounts=["nope"], push_to_fraimic=False).status_code == 422
        assert show(client, mode="search").status_code == 422  # body validation
        assert show(client, bogus=1).status_code == 422

        fake.search_error = 500
        assert show(client, push_to_fraimic=False).status_code == 502

    with servers(fake, frame=False) as router:
        router.route(host="immich.test").mock(side_effect=httpx.ReadTimeout("slow"))
        assert show(client, push_to_fraimic=False).status_code == 504

    fake.key = "rotated"
    fake.search_error = None
    with servers(fake, frame=False):
        rejected = show(client, push_to_fraimic=False)
        assert rejected.status_code == 502 and "rejected the API key" in rejected.json()["detail"]
        assert "good-key" not in rejected.text


def test_not_configured_is_a_conflict(make_client: ClientFactory) -> None:
    client = make_client()
    assert show(client).status_code == 409
    assert client.get("/api/immich/albums").status_code == 409
    assert client.get("/api/immich/people").status_code == 409


def test_undecodable_image_is_a_bad_gateway(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeImmich()
    fake.add_asset("x1")
    client = immich_client(make_client)

    def broken(*args: object, **kwargs: object) -> object:
        raise InvalidImageError("boom")

    monkeypatch.setattr("app.services.display.render_bytes", broken)
    with servers(fake, frame=False):
        response = show(client, push_to_fraimic=False)
    assert response.status_code == 502 and "cannot be decoded" in response.json()["detail"]
    history = client.app.state.history_store  # type: ignore[attr-defined]
    assert history.recent_ids("immich", 30) == set()  # nothing recorded for a failed render


# --- albums & people ----------------------------------------------------------
def test_albums_endpoint(make_client: ClientFactory) -> None:
    fake = family()
    fake.add_album("al1", "Summer", ["a1", "a2"])
    client = immich_client(make_client)
    with servers(fake, frame=False):
        body = client.get("/api/immich/albums").json()
    assert body["albums"] == [
        {
            "account": "default",
            "id": "al1",
            "name": "Summer",
            "asset_count": 2,
            "shared": False,
            "thumbnail_asset_id": body["albums"][0]["thumbnail_asset_id"],
        }
    ]
    assert body["errors"] == {}


def test_people_endpoint_cache_refresh_ambiguity_and_aliases(make_client: ClientFactory) -> None:
    fake = family()
    fake.add_person("p9", "Alice")  # ambiguous name
    client = immich_client(make_client)
    aliases = {"Ali": {"default": ["p9"]}, "Ghost": {"default": []}}
    saved = client.put("/api/config", json={"immich": {"person_aliases": aliases}})
    assert saved.status_code == 200
    with servers(fake, frame=False):
        first = client.get("/api/immich/people").json()["accounts"][0]
        assert first["account"] == "default" and first["error"] is None
        assert [p["name"] for p in first["people"]] == ["Alice", "Alice", "Bob"]
        assert first["ambiguous"] == ["Alice"]
        assert first["aliases"] == {"Ali": ["p9"], "Ghost": []}
        assert first["cached_at"]

        calls = people_calls(fake)
        client.get("/api/immich/people")
        assert people_calls(fake) == calls  # served from the cache

        fake.add_person("p5", "Carol")
        refreshed = client.post("/api/immich/people/refresh").json()["accounts"][0]
    assert "Carol" in [p["name"] for p in refreshed["people"]]
    assert people_calls(fake) == calls + 1


def test_people_and_albums_report_a_failing_account(make_client: ClientFactory) -> None:
    fake = family()
    client = immich_client(make_client)
    fake.key = "rotated"
    with servers(fake, frame=False):
        people = client.get("/api/immich/people").json()["accounts"][0]
        albums = client.get("/api/immich/albums").json()
    assert people["people"] == [] and "rejected the API key" in people["error"]
    assert albums["albums"] == [] and "default" in albums["errors"]


def test_unknown_account_filter_is_rejected(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    assert client.get("/api/immich/people", params={"accounts": "nope"}).status_code == 422


# --- auth ---------------------------------------------------------------------
def test_immich_routes_require_the_api_key(make_client: ClientFactory) -> None:
    fake = family()
    client = immich_client(make_client, app_api_key=KEY)
    headers = {"X-API-Key": KEY}
    with servers(fake, frame=False):
        assert show(client).status_code == 401
        assert client.get("/api/immich/albums").status_code == 401  # lists personal data
        assert client.get("/api/immich/people").status_code == 401
        assert client.post("/api/immich/people/refresh").status_code == 401
        assert client.get("/api/immich/albums", headers=headers).status_code == 200
        shown = client.post("/api/display/immich", json={"push_to_fraimic": False}, headers=headers)
        assert shown.status_code == 200


def test_multi_account_configuration_over_the_api(make_client: ClientFactory) -> None:
    home = FakeImmich("http://home.test", "k-home")
    partner = FakeImmich("http://partner.test", "k-partner")
    home.add_asset("h1", checksum="dup")
    partner.add_asset("p1", checksum="dup")
    partner.add_asset("p2", checksum="uniq-partner")
    client = make_client(fraimic_host="frame.test")
    saved = client.put(
        "/api/config",
        json={
            "immich": {
                "accounts": {
                    "home": {"url": home.base, "api_key": "k-home"},
                    "partner": {"url": partner.base, "api_key": "k-partner"},
                }
            }
        },
    )
    assert saved.status_code == 200 and "k-home" not in saved.text
    with servers(home, partner, frame=False):
        picked = {
            show(client, push_to_fraimic=False, ignore_history=True).json()["image"]["details"][
                "asset_id"
            ]
            for _ in range(4)
        }
        only = show(client, accounts=["partner"], push_to_fraimic=False, ignore_history=True)
    assert picked <= {"h1", "p1", "p2"}
    assert only.json()["image"]["details"]["account"] == "partner"


# --- quality gate and burst cooldown ------------------------------------------------------------
def test_display_records_taken_at_and_blocks_the_same_moment(make_client: ClientFactory) -> None:
    from datetime import UTC, datetime

    fake = family()
    # Same capture time as a1 (shown below): a rapid-fire shot of the same moment.
    fake.add_asset("a1b", people=[("p1", "Alice")], checksum="sum-a1b")
    client = immich_client(make_client)
    with servers(fake, frame=False):
        assert show(client, persons=["Alice"], push_to_fraimic=False).status_code == 200
        history = client.app.state.history_store  # type: ignore[attr-defined]
        assert history.entries()[0].taken_at == datetime(2024, 7, 14, 10, 22, 11, tzinfo=UTC)

        blocked = show(client, persons=["Alice"], push_to_fraimic=False)
        assert blocked.status_code == 404 and "taken within 2 h" in blocked.json()["detail"]
        again = show(client, persons=["Alice"], ignore_history=True, push_to_fraimic=False)
        assert again.status_code == 200


def test_quality_gate_over_the_api(make_client: ClientFactory) -> None:
    from tests.test_immich_quality import add_good, ui_screen

    fake = FakeImmich()
    fake.add_asset("shot", thumbnail=ui_screen(), width=1080, height=2400)
    add_good(fake, "good")
    client = immich_client(make_client)
    with servers(fake, frame=False):
        body = show(client, push_to_fraimic=False, ignore_history=True).json()
        suitability = body["image"]["details"]["suitability"]
        assert suitability["level"] == 0 and suitability["checked"] in {1, 2}
        if suitability["checked"] == 2:
            assert body["image"]["details"]["asset_id"] == "good"

        skipped = show(client, skip_quality_gate=True, push_to_fraimic=False, ignore_history=True)
        assert "suitability" not in skipped.json()["image"]["details"]


def test_quality_settings_over_the_api(make_client: ClientFactory) -> None:
    client = make_client()
    quality = client.get("/api/config").json()["config"]["quality"]
    assert quality["enabled"] is True and quality["min_dimension"] == 600
    assert client.put("/api/config", json={"quality": {"min_dimension": 800}}).status_code == 200
    assert client.get("/api/config").json()["config"]["quality"]["min_dimension"] == 800
    assert client.put("/api/config", json={"quality": {"min_dimension": -1}}).status_code == 422
    assert client.put("/api/config", json={"quality": {"nope": 1}}).status_code == 400
    assert (
        client.put("/api/config", json={"immich": {"burst_cooldown_hours": 0}}).status_code == 200
    )
