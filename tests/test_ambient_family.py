"""Locked Folder rule, Family Mode, Random Discovery, ambient context and Zeroconf."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from app.core.config_models import AmbientConfig, SourceRule
from app.core.config_store import ConfigStore
from app.providers.base import NoCandidateError
from app.services.ambient import AmbientRequest, AmbientState, ambient_candidates
from app.services.discovery import SERVICE_TYPE, ZeroconfAdvertiser
from app.services.display import DisplayResult
from app.services.scheduler import DISCOVERY_LABEL, SchedulerService
from app.services.smart_mix import SmartMixResolver
from tests.conftest import ClientFactory
from tests.test_api_immich import immich_client, servers
from tests.test_api_refresh import alice_and_bob
from tests.test_immich_client import mocked
from tests.test_immich_provider import Env, family

NOW = datetime(2099, 1, 14, 12, 0, tzinfo=UTC)


async def pick_all(env: Env, tries: int = 12) -> set[str]:
    """Asset IDs seen over several picks (the test rng cycles through the candidates)."""
    seen: set[str] = set()
    for _ in range(tries):
        try:
            asset = await env.fetch(ignore_history=True)
        except NoCandidateError:
            break
        seen.add(str(asset.details["asset_id"]))
    return seen


# --- Locked Folder (always on) -----------------------------------------------------------
async def test_locked_assets_are_never_selected(tmp_path: Path) -> None:
    fake = family()
    fake.add_asset("locked1", visibility="locked")
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        seen = await pick_all(env)
    assert seen and "locked1" not in seen


# --- Family Mode ---------------------------------------------------------------------------
async def test_members_keep_only_photos_with_a_member(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    await env.store.update({"family": {"enabled": True, "members": ["Alice"]}})
    with mocked(fake):
        seen = await pick_all(env)
    assert seen == {"a1", "a3"}  # a6 is archived, a2/a4/a5 have no Alice


async def test_family_mode_off_leaves_the_library_alone(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    await env.store.update({"family": {"enabled": False, "members": ["Alice"]}})
    with mocked(fake):
        assert len(await pick_all(env)) > 2


async def test_private_people_are_excluded(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    await env.store.update({"family": {"enabled": True, "private_people": ["Kid"]}})
    with mocked(fake):
        seen = await pick_all(env)
    assert seen and "a4" not in seen


async def test_private_albums_are_excluded(tmp_path: Path) -> None:
    fake = family()
    fake.add_album("alb1", "Private", ["a1", "a5"])
    env = Env(tmp_path, {"home": fake})
    await env.store.update({"family": {"enabled": True, "private_albums": ["private"]}})
    with mocked(fake):
        seen = await pick_all(env)
    assert seen and not seen & {"a1", "a5"}


async def test_require_member_false_keeps_only_exclusions(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    await env.store.update(
        {"family": {"enabled": True, "members": ["Alice"], "require_member": False}}
    )
    with mocked(fake):
        assert "a2" in await pick_all(env)


async def test_ambient_override_forces_family_mode(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    await env.store.update({"family": {"enabled": False, "members": ["Alice"]}})
    flag: list[bool | None] = [True]
    env.provider._family_override = lambda: flag[0]
    with mocked(fake):
        assert await pick_all(env) == {"a1", "a3"}
        flag[0] = None
        assert len(await pick_all(env)) > 2
    assert env.provider.family_active() is False
    flag[0] = False
    await env.store.update({"family": {"enabled": True}})
    assert env.provider.family_active() is False


async def test_whole_library_ignores_default_filters(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake}, persons=["Alice"])
    with mocked(fake):
        narrow = await pick_all(env)
        wide = set()
        for _ in range(12):
            asset = await env.fetch(whole_library=True, ignore_history=True)
            wide.add(str(asset.details["asset_id"]))
    assert narrow <= {"a1", "a3"} and len(wide) > 2


# --- Random Discovery ------------------------------------------------------------------------
class Scripted(random.Random):
    """``random()`` returns the scripted values in turn, then 0.99."""

    def __init__(self, *values: float) -> None:
        super().__init__(0)
        self.values = list(values)

    def random(self) -> float:
        return self.values.pop(0) if self.values else 0.99


class Runner:
    def __init__(self) -> None:
        self.calls: list[SourceRule] = []
        self.fail_discovery = False

    async def __call__(self, rule: SourceRule) -> DisplayResult:
        self.calls.append(rule)
        if self.fail_discovery and rule.params.get("whole_library"):
            raise NoCandidateError("nothing in the library")
        return cast(DisplayResult, object())


def scheduler(tmp_path: Path, runner: Runner, rng: random.Random, **mix: Any) -> SchedulerService:
    store = ConfigStore(
        tmp_path / "config.json",
        {
            "scheduler": {"enabled": True},
            "smart_mix": {
                "enabled": True,
                "entries": [{"kind": "immich", "label": "alice", "params": {"persons": ["Alice"]}}],
                **mix,
            },
        },
    )
    store.load()
    service = SchedulerService(
        tmp_path / "state.json",
        store,
        {"immich": runner},
        clock=lambda: NOW,
        resolver=SmartMixResolver(rng),
    )
    service.load()
    return service


async def test_discovery_hit_shows_a_whole_library_photo(tmp_path: Path) -> None:
    runner = Runner()
    service = scheduler(tmp_path, runner, Scripted(0.01), discovery_share=0.5)
    await service.run("scheduled")
    assert runner.calls[0].params["whole_library"] is True
    assert runner.calls[0].params["skip_quality_gate"] is False
    last = service.state.last_run
    assert last is not None and last.entry_label == DISCOVERY_LABEL


async def test_discovery_miss_uses_the_weighted_entries(tmp_path: Path) -> None:
    runner = Runner()
    service = scheduler(tmp_path, runner, Scripted(0.9), discovery_share=0.5)
    await service.run("scheduled")
    assert runner.calls[0].params == {"persons": ["Alice"]}


async def test_discovery_failure_falls_back_to_the_entries(tmp_path: Path) -> None:
    runner = Runner()
    runner.fail_discovery = True
    service = scheduler(tmp_path, runner, Scripted(0.01), discovery_share=0.5)
    await service.run("scheduled")
    assert [c.params.get("whole_library") for c in runner.calls] == [True, None]


async def test_discovery_off_never_rolls_a_hit(tmp_path: Path) -> None:
    runner = Runner()
    service = scheduler(tmp_path, runner, Scripted(0.0), discovery_share=0.0)
    await service.run("scheduled")
    assert runner.calls[0].params == {"persons": ["Alice"]}


def test_discovery_share_is_capped_at_half() -> None:
    from pydantic import ValidationError

    from app.core.config_models import SmartMixConfig

    assert SmartMixConfig().discovery_share == 0.15
    with pytest.raises(ValidationError):
        SmartMixConfig(discovery_share=0.6)


# --- Ambient context -------------------------------------------------------------------------
def test_fields_expire_on_their_own() -> None:
    config = AmbientConfig()
    request = AmbientRequest.model_validate(
        {
            "presence": {"values": ["Alice"], "ttl_minutes": 10},
            "boost_tags": ["beach"],
            "ttl_minutes": 60,
        }
    )
    state = request.apply(AmbientState(), config, NOW)
    assert state.live(NOW + timedelta(minutes=5)).presence is not None
    later = state.live(NOW + timedelta(minutes=30))
    assert later.presence is None and later.boost_tags is not None
    assert state.live(NOW + timedelta(minutes=61)).empty


def test_merge_keeps_unset_fields_and_empty_clears() -> None:
    config = AmbientConfig()
    first = AmbientRequest.model_validate({"presence": ["Alice"], "weather": "rainy"})
    state = first.apply(AmbientState(), config, NOW)
    second = AmbientRequest.model_validate({"presence": [], "season": "winter"})
    merged = second.apply(state, config, NOW)
    assert merged.presence is None
    assert merged.weather is not None and merged.season is not None


def test_candidates_map_people_tags_and_weather() -> None:
    config = AmbientConfig()
    request = AmbientRequest.model_validate(
        {
            "presence": ["Alice"],
            "boost_people": ["Bob", "alice"],
            "boost_tags": ["beach"],
            "weather": "RAINY",
        }
    )
    state = request.apply(AmbientState(), config, NOW)
    by_label = {c.label: c for c in ambient_candidates(state, config)}
    assert by_label["ambient:people"].rule.params["persons"] == ["Alice", "Bob"]
    assert by_label["ambient:tags"].rule.params["clip_queries"] == ["beach"]
    assert by_label["ambient:weather:rainy"].rule.params["clip_queries"] == [
        config.weather_queries["rainy"]
    ]
    assert all(c.weight == config.boost_weight for c in by_label.values())


def test_unknown_weather_and_season_are_rejected() -> None:
    config = AmbientConfig()
    with pytest.raises(ValueError, match="Unknown weather"):
        AmbientRequest(weather="plasma").apply(AmbientState(), config, NOW)
    with pytest.raises(ValueError, match="Unknown season"):
        AmbientRequest(season={"value": "monsoon"}).apply(AmbientState(), config, NOW)  # type: ignore[arg-type]


def test_context_api_roundtrip(make_client: ClientFactory) -> None:
    client = make_client()
    posted = client.post(
        "/api/context",
        json={"presence": ["Alice"], "family_mode": {"value": True, "ttl_minutes": 30}},
    )
    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert body["context"]["presence"]["values"] == ["Alice"]
    assert body["family_active"] is True  # overrides family.enabled=false
    assert body["smart_mix_enabled"] is False
    assert body["extra_candidates"] == ["ambient:people"]
    assert client.get("/api/context").json()["context"]["family_mode"]["value"] is True
    status = client.get("/api/status").json()["scheduler"]["ambient"]
    assert status["presence"]["values"] == ["Alice"]

    cleared = client.delete("/api/context", params={"field": "presence"}).json()
    assert cleared["context"]["presence"] is None and cleared["family_active"] is True
    assert client.delete("/api/context").json()["family_active"] is False


def test_context_api_validates_input(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.post("/api/context", json={"weather": "plasma"}).status_code == 422
    assert client.post("/api/context", json={"nope": 1}).status_code == 422
    assert client.post("/api/context", json={"ttl_minutes": 0}).status_code == 422


def test_context_api_requires_the_key(make_client: ClientFactory) -> None:
    client = make_client(app_api_key="secret-key")
    assert client.get("/api/context").status_code == 401
    assert client.get("/api/context", headers={"X-API-Key": "secret-key"}).status_code == 200


def test_context_survives_a_restart(tmp_path: Path) -> None:
    runner = Runner()
    first = scheduler(tmp_path, runner, Scripted())
    state = AmbientRequest(presence=["Alice"]).apply(AmbientState(), AmbientConfig(), NOW)
    asyncio.run(first.set_ambient(state))
    second = scheduler(tmp_path, runner, Scripted())
    assert second.live_ambient().presence is not None
    assert [c.label for c in second.ambient_extras()] == ["ambient:people"]


def test_family_mode_override_reaches_the_provider(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    client.put("/api/config", json={"family": {"members": ["Alice"]}})
    client.post("/api/context", json={"family_mode": True})
    with servers(fake):
        for _ in range(6):
            response = client.post("/api/display/immich", json={"ignore_history": True})
            assert response.status_code == 200, response.text
            assert "Alice" in response.json()["image"]["details"]["people"]


# --- Zeroconf ----------------------------------------------------------------------------------
class FakeZeroconf:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.registered: list[Any] = []
        self.closed = False

    async def async_register_service(self, info: Any, **kwargs: Any) -> None:
        if self.fail:
            raise OSError("multicast unavailable")
        self.registered.append(info)

    async def async_unregister_service(self, info: Any) -> None:
        self.registered.remove(info)

    async def async_close(self) -> None:
        self.closed = True


async def test_zeroconf_advertises_the_service() -> None:
    fake = FakeZeroconf()
    advertiser = ZeroconfAdvertiser(
        port=8080,
        version="1.2.3",
        root_path="/fraimic",
        auth="password",
        zeroconf_factory=lambda: fake,
        addresses=lambda: ["192.0.2.10"],
    )
    assert await advertiser.start() is True and advertiser.active
    info = fake.registered[0]
    assert info.type == SERVICE_TYPE and info.port == 8080
    assert info.decoded_properties == {
        "version": "1.2.3",
        "api_version": "1",
        "min_api_version": "1",
        "path": "/fraimic",
        "auth": "password",
    }
    assert info.parsed_addresses() == ["192.0.2.10"]
    await advertiser.stop()
    assert fake.closed and not fake.registered and not advertiser.active
    await advertiser.stop()  # idempotent


async def test_zeroconf_errors_are_logged_and_ignored(caplog: pytest.LogCaptureFixture) -> None:
    advertiser = ZeroconfAdvertiser(
        port=8080,
        version="1",
        root_path="",
        auth="open",
        zeroconf_factory=lambda: FakeZeroconf(fail=True),
        addresses=lambda: ["192.0.2.10"],
    )
    assert await advertiser.start() is False and not advertiser.active
    assert "Zeroconf advertising disabled" in caplog.text
    await advertiser.stop()


def test_zeroconf_is_off_during_tests_and_on_by_default() -> None:
    from app.core.settings import EnvSettings

    assert EnvSettings.model_fields["zeroconf_enabled"].default is True
    assert EnvSettings(_env_file=None).zeroconf_enabled is False  # conftest disables it
