"""API tests: smart search, Smart Mix context boost and playlist inspection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.core.context import RuleContext
from tests.conftest import ClientFactory
from tests.immich_fake import FakeImmich
from tests.test_api_immich import KEY, immich_client, servers, show
from tests.test_api_refresh import alice_and_bob, pin, refresh


def smart_search(client: TestClient, **body: Any) -> Any:
    return client.post("/api/display/smart-search", json=body)


def enable_mix(client: TestClient, *entries: dict[str, Any], **extra: Any) -> None:
    response = client.put(
        "/api/config",
        json={
            "smart_mix": {"enabled": True, "discovery_share": 0, "entries": list(entries), **extra}
        },
    )
    assert response.status_code == 200, response.text


def bob_entry(**extra: Any) -> dict[str, Any]:
    return {"kind": "immich", "label": "bob", "params": {"persons": ["Bob"]}, **extra}


def smart_bodies(fake: FakeImmich) -> list[dict[str, Any]]:
    return fake.bodies("/api/search/smart")


# --- POST /api/display/smart-search ------------------------------------------------------
def test_smart_search_displays_a_semantic_match(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    fake.smart_results["beach vacation"] = ["a2"]
    client = immich_client(make_client)
    with servers(fake):
        response = smart_search(client, query="beach vacation", persons=["Alice"])

    assert response.status_code == 200, response.text
    assert response.json()["image"]["details"]["asset_id"] == "a2"
    body = smart_bodies(fake)[0]
    assert body["query"] == "beach vacation" and body["personIds"] == ["p1"]


def test_smart_search_is_ephemeral_by_default(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    fake.add_asset("b2", people=[("p2", "Bob")], localDateTime="2022-01-01T10:00:00.000Z")
    client = immich_client(make_client)
    with servers(fake):
        assert show(client, persons=["Bob"]).status_code == 200
        assert smart_search(client, query="sunset", persons=["Alice"]).status_code == 200
        replayed = refresh(client)
        assert (
            smart_search(client, query="sunset", persons=["Alice"], ephemeral=False).status_code
            == 200
        )
        stored = client.get("/api/status").json()["scheduler"]["active_rule"]

    assert replayed.json()["display"]["image"]["title"].startswith("Bob")
    assert stored == {"kind": "immich", "source": "last"}


def test_smart_search_needs_something_to_search(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    response = smart_search(client)
    assert response.status_code == 422 and "query" in response.json()["detail"]


def test_smart_search_expands_placeholders(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake):
        assert smart_search(client, query="{season} walk").status_code == 200

    season = RuleContext(datetime.now(UTC), ZoneInfo("UTC")).season
    assert smart_bodies(fake)[0]["query"] == f"{season} walk"


def test_smart_search_on_this_day_searches_past_years(make_client: ClientFactory) -> None:
    today = datetime.now(UTC).date()
    memory = f"{today.year - 2}-{today.month:02d}-{min(today.day, 28):02d}T10:00:00.000Z"
    fake = FakeImmich()
    fake.add_asset("memory", localDateTime=memory)
    fake.add_asset(
        "other", localDateTime=f"{today.year - 2}-{(today.month % 12) + 1:02d}-15T10:00:00.000Z"
    )
    client = immich_client(make_client)
    with servers(fake):
        response = smart_search(client, on_this_day=True)

    assert response.status_code == 200, response.text
    assert response.json()["image"]["details"]["asset_id"] == "memory"
    assert all("takenAfter" in body for body in fake.bodies("/api/search/random"))


def test_smart_search_hold_is_reported(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake):
        assert smart_search(client, query="x", hold_minutes=30).status_code == 200
    assert client.get("/api/status").json()["scheduler"]["hold_until"] is not None


# --- /api/smart-mix/context -----------------------------------------------------------------
def test_context_requires_an_enabled_mix(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    response = client.post("/api/smart-mix/context", json={"queries": ["beach"]})
    assert response.status_code == 409


def test_context_boost_steers_the_next_refresh_then_clears(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    fake.smart_results["beach"] = ["a1", "a2", "a3"]
    client = immich_client(make_client)
    enable_mix(client, bob_entry())

    with servers(fake):
        response = client.post(
            "/api/smart-mix/context",
            json={"queries": ["beach"], "exclusive": True, "label": "beach", "ttl_minutes": 30},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["label"], body["kind"], body["exclusive"], body["effective"]) == (
            "beach",
            "immich",
            True,
            True,
        )
        boosted = refresh(client).json()
        status = client.get("/api/status").json()["scheduler"]["smart_mix"]
        cleared = client.delete("/api/smart-mix/context").json()
        normal = refresh(client).json()

    assert (boosted["rule_source"], boosted["entry_label"]) == ("smart_mix", "beach")
    assert smart_bodies(fake)[0]["query"] == "beach"
    assert status["context"]["label"] == "beach" and status["entries"] == 1
    assert cleared == {"cleared": True}
    assert normal["entry_label"] == "bob"


def test_expired_context_is_ignored(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    enable_mix(client, bob_entry())
    assert (
        client.post(
            "/api/smart-mix/context", json={"queries": ["beach"], "ttl_minutes": 1}
        ).status_code
        == 200
    )
    scheduler = client.app.state.scheduler_service  # type: ignore[attr-defined]
    scheduler._clock = lambda: datetime.now(UTC) + timedelta(minutes=5)
    assert client.get("/api/smart-mix").json()["context"] is None
    assert client.delete("/api/smart-mix/context").json() == {"cleared": False}


def test_context_validation(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    enable_mix(client, bob_entry())
    post = client.post
    assert post("/api/smart-mix/context", json={}).status_code == 422
    assert (
        post("/api/smart-mix/context", json={"kind": "tmdb", "params": {"a": 1}}).status_code == 422
    )
    assert (
        post("/api/smart-mix/context", json={"kind": "Bad Kind", "params": {"a": 1}}).status_code
        == 422
    )
    assert post("/api/smart-mix/context", json={"params": {"mode": "teleport"}}).status_code == 422
    assert (
        post("/api/smart-mix/context", json={"ttl_minutes": 0, "queries": ["x"]}).status_code == 422
    )
    assert post("/api/smart-mix/context", json={"weight": 0, "queries": ["x"]}).status_code == 422


def test_pinned_rule_wins_over_the_mix(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    enable_mix(client, bob_entry())
    pin(client, {"kind": "immich", "params": {"persons": ["Alice"]}})
    with servers(fake):
        response = refresh(client)
        context = client.post("/api/smart-mix/context", json={"queries": ["x"]}).json()

    assert response.json()["rule_source"] == "pinned"
    assert context["effective"] is False


# --- GET /api/smart-mix -----------------------------------------------------------------------
def test_playlist_view(make_client: ClientFactory) -> None:
    client = immich_client(make_client)
    enable_mix(
        client,
        bob_entry(weight=3),
        {"kind": "immich", "params": {"clip_queries": ["{season} light"], "mode": "search"}},
        {"kind": "immich", "weight": 5, "enabled": False},
    )
    view = client.get("/api/smart-mix").json()

    assert view["enabled"] and view["effective"] and view["supported_kinds"] == ["immich", "tmdb"]
    assert [e["probability"] for e in view["entries"]] == [0.75, 0.25, 0.0]
    season = RuleContext(datetime.now(UTC), ZoneInfo("UTC")).season
    assert view["entries"][1]["resolved_params"]["clip_queries"] == [f"{season} light"]
    assert view["entries"][1]["params"]["clip_queries"] == ["{season} light"]
    assert view["last_entry_index"] is None and view["context"] is None


def test_playlist_view_when_disabled(make_client: ClientFactory) -> None:
    view = immich_client(make_client).get("/api/smart-mix").json()
    assert view["enabled"] is False and view["effective"] is False and view["entries"] == []


# --- Authentication ---------------------------------------------------------------------------
def test_endpoints_require_the_api_key(make_client: ClientFactory) -> None:
    client = immich_client(make_client, app_api_key=KEY)
    headers = {"X-API-Key": KEY}

    assert client.get("/api/smart-mix").status_code == 401  # GET is protected too
    assert client.post("/api/smart-mix/context", json={"queries": ["x"]}).status_code == 401
    assert client.delete("/api/smart-mix/context").status_code == 401
    assert client.post("/api/display/smart-search", json={"query": "x"}).status_code == 401

    assert client.get("/api/smart-mix", headers=headers).status_code == 200
    assert client.delete("/api/smart-mix/context", headers=headers).status_code == 200
    assert client.get("/api/status").status_code == 200  # status stays public
