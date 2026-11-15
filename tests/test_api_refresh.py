"""API tests for POST /api/refresh and the scheduler block of /api/status."""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import ClientFactory
from tests.immich_fake import FakeImmich
from tests.test_api_immich import KEY, immich_client, servers, show


def alice_and_bob() -> FakeImmich:
    """Three Alice photos taken on different days (no burst cooldown) and one of Bob."""
    fake = FakeImmich()
    fake.add_person("p1", "Alice")
    fake.add_person("p2", "Bob")
    for index, day in enumerate(("2024-07-01", "2024-08-01", "2024-09-01"), start=1):
        fake.add_asset(f"a{index}", people=[("p1", "Alice")], localDateTime=f"{day}T10:00:00.000Z")
    fake.add_asset("b1", people=[("p2", "Bob")], localDateTime="2023-01-01T10:00:00.000Z")
    return fake


def refresh(client: TestClient, **headers: str) -> httpx.Response:
    return client.post("/api/refresh", headers=headers)


def searched_person_ids(fake: FakeImmich) -> list[list[str]]:
    return [body.get("personIds", []) for body in fake.bodies("/api/search/random")]


def upload(client: TestClient) -> httpx.Response:
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), (200, 80, 40)).save(buffer, format="JPEG")
    return client.post(
        "/api/display/upload",
        files={"file": ("holiday.jpg", buffer.getvalue(), "image/jpeg")},
        data={"push_to_fraimic": "true"},
    )


def pin(client: TestClient, rule: dict[str, Any] | None) -> None:
    response = client.put("/api/config", json={"scheduler": {"source_rule": rule}})
    assert response.status_code == 200, response.text


# --- Replay -----------------------------------------------------------------------------
def test_refresh_replays_the_last_display(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake) as router:
        first = show(client, persons=["Alice"], fit="contain")
        assert first.status_code == 200, first.text
        response = refresh(client)
        assert response.status_code == 200, response.text
        uploads = [c for c in router.calls if c.request.url.path == "/api/image"]

    body = response.json()
    assert (body["reason"], body["rule_kind"], body["rule_source"]) == ("manual", "immich", "last")
    assert body["display"]["image"]["source"] == "immich"
    assert body["display"]["image"]["fit"] == "contain"  # the fit is replayed too
    assert body["display"]["image"]["title"].startswith("Alice")
    assert (
        body["display"]["image"]["details"]["asset_id"]
        != first.json()["image"]["details"]["asset_id"]
    )  # anti-repeat history still applies
    assert body["display"]["delivery"]["status"] == "delivered"
    assert len(uploads) == 2
    assert searched_person_ids(fake)[-1] == searched_person_ids(fake)[0] == ["p1"]


def test_refresh_without_a_rule_is_a_conflict(make_client: ClientFactory) -> None:
    client = make_client()
    response = refresh(client)
    assert response.status_code == 409
    assert "Nothing to refresh" in response.json()["detail"]


def test_previews_do_not_become_the_rule(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake, frame=False):
        assert show(client, persons=["Alice"], push_to_fraimic=False).status_code == 200
    assert refresh(client).status_code == 409


def test_uploads_keep_the_replayable_rule(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    fake.add_asset("b2", people=[("p2", "Bob")], localDateTime="2022-01-01T10:00:00.000Z")
    client = immich_client(make_client)
    with servers(fake):
        assert show(client, persons=["Bob"]).status_code == 200
        assert upload(client).status_code == 200
        assert client.get("/api/status").json()["current_image"]["source"] == "upload"
        response = refresh(client)
    assert response.status_code == 200, response.text
    assert response.json()["display"]["image"]["title"].startswith("Bob")


# --- Pinned rule ---------------------------------------------------------------------
def test_pinned_rule_wins(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake):
        assert show(client, persons=["Alice"]).status_code == 200
        pin(client, {"kind": "immich", "params": {"persons": ["Bob"]}})
        response = refresh(client)
    assert response.status_code == 200, response.text
    assert response.json()["rule_source"] == "pinned"
    assert response.json()["display"]["image"]["title"].startswith("Bob")


def test_pinned_rule_errors(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    pin(client, {"kind": "media", "params": {"title": "Alien"}})
    unsupported = refresh(client)
    assert unsupported.status_code == 409 and "media" in unsupported.json()["detail"]

    pin(client, {"kind": "immich", "params": {"mode": "teleport"}})
    invalid = refresh(client)
    assert invalid.status_code == 422 and "mode" in invalid.json()["detail"]
    assert "teleport" not in invalid.text


# --- Provider errors ---------------------------------------------------------------
def test_provider_errors_are_mapped(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake):
        assert show(client, persons=["Bob"]).status_code == 200
        exhausted = refresh(client)  # Bob's only photo is on the anti-repeat cooldown
        assert exhausted.status_code == 404
        fake.search_error = 500
        assert refresh(client).status_code == 502
    status = client.get("/api/status").json()["scheduler"]
    assert status["last_run"]["status"] == "failed"
    assert status["last_run"]["reason"] == "manual"
    assert status["last_run"]["detail"]
    assert status["consecutive_failures"] == 0  # manual failures do not back off


def test_refresh_requires_the_api_key(make_client: ClientFactory) -> None:
    client = make_client(app_api_key=KEY)
    assert refresh(client).status_code == 401
    assert refresh(client, **{"X-API-Key": KEY}).status_code == 409


# --- Status ---------------------------------------------------------------------------
def test_status_when_disabled(make_client: ClientFactory) -> None:
    status = make_client().get("/api/status").json()["scheduler"]
    assert status == {
        "enabled": False,
        "interval_hours": 6.0,
        "timezone": "UTC",
        "quiet_hours": None,
        "in_quiet_hours": False,
        "next_run_at": None,
        "running": False,
        "last_run": None,
        "last_display_at": None,
        "consecutive_failures": 0,
        "hold_until": None,
        "smart_mix": {"enabled": False, "entries": 0, "context": None},
        "ambient": {
            "presence": None,
            "family_mode": None,
            "weather": None,
            "season": None,
            "boost_tags": None,
            "boost_people": None,
        },
        "active_rule": None,
    }


def test_status_after_a_display(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(
        make_client, scheduler_enabled=True, refresh_interval_hours=2, tz="Europe/Paris"
    )
    with servers(fake):
        assert show(client, persons=["Alice"]).status_code == 200
    status = client.get("/api/status").json()["scheduler"]
    assert status["enabled"] is True and status["timezone"] == "Europe/Paris"
    assert status["active_rule"] == {"kind": "immich", "source": "last"}
    shown = datetime.fromisoformat(status["last_display_at"])
    next_run = datetime.fromisoformat(status["next_run_at"])
    assert next_run - shown == timedelta(hours=2)
    assert next_run.utcoffset() == next_run.astimezone(ZoneInfo("Europe/Paris")).utcoffset()


def test_status_inside_quiet_hours(make_client: ClientFactory) -> None:
    paris = ZoneInfo("Europe/Paris")
    now = datetime.now(paris)
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end_at = now + timedelta(hours=1)
    client = make_client(
        scheduler_enabled=True,
        refresh_interval_hours=0.25,
        tz="Europe/Paris",
        quiet_hours_start=start,
        quiet_hours_end=end_at.strftime("%H:%M"),
    )
    status = client.get("/api/status").json()["scheduler"]
    assert status["quiet_hours"] == {"start": start, "end": end_at.strftime("%H:%M")}
    assert status["in_quiet_hours"] is True
    next_run = datetime.fromisoformat(status["next_run_at"]).astimezone(paris)
    assert next_run.strftime("%Y-%m-%d %H:%M") == end_at.strftime("%Y-%m-%d %H:%M")
