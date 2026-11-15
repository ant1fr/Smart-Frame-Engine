"""Direct pushes: ephemeral rules and hold windows never fight the scheduler."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.config_store import ConfigStore
from app.services.scheduler import SchedulerService, immich_rule
from tests.conftest import ClientFactory
from tests.immich_fake import FakeImmich
from tests.test_api_immich import immich_client, servers, show
from tests.test_api_refresh import alice_and_bob, pin, refresh, upload
from tests.test_scheduler_service import ALICE, BOB, T0, FakeClock, FakeRunner


def make_service(tmp_path: Path, clock: FakeClock, runner: FakeRunner, **config: Any) -> Any:
    store = ConfigStore(
        tmp_path / "config.json",
        {"scheduler": {"enabled": True, "interval_hours": 2, **config}},
    )
    store.load()
    service = SchedulerService(
        tmp_path / "scheduler_state.json", store, {"immich": runner}, clock=clock
    )
    service.load()
    return service


async def test_ephemeral_push_resets_the_timer_but_keeps_last_rule(tmp_path: Path) -> None:
    clock, runner = FakeClock(), FakeRunner()
    service = make_service(tmp_path, clock, runner)
    await service.record_manual(ALICE)
    clock.advance(hours=1)

    await service.record_manual(BOB, ephemeral=True)

    assert service.state.last_rule == ALICE
    assert service.state.timer_reset_at == clock.now
    assert service.next_run_at == clock.now + timedelta(hours=2)


async def test_hold_postpones_the_next_refresh(tmp_path: Path) -> None:
    clock, runner = FakeClock(), FakeRunner()
    service = make_service(tmp_path, clock, runner)

    await service.record_manual(None, ephemeral=True, hold_minutes=300)

    assert service.next_run_at == T0 + timedelta(minutes=300)  # not 2 h
    assert service.snapshot().hold_until == T0 + timedelta(minutes=300)


async def test_tick_during_a_hold_is_skipped_without_restarting_the_timer(tmp_path: Path) -> None:
    clock, runner = FakeClock(), FakeRunner()
    service = make_service(tmp_path, clock, runner)
    await service.record_manual(ALICE, hold_minutes=300)
    clock.advance(hours=3)  # the plain interval elapsed; the hold has not

    last_run = await service.tick()

    assert last_run is not None and last_run.status == "skipped"
    assert "held" in (last_run.detail or "")
    assert runner.calls == []
    assert service.state.timer_reset_at == T0
    assert service.next_run_at == T0 + timedelta(minutes=300)


async def test_hold_expires_and_a_refresh_clears_it(tmp_path: Path) -> None:
    clock, runner = FakeClock(), FakeRunner()
    service = make_service(tmp_path, clock, runner)
    await service.record_manual(ALICE, hold_minutes=60)
    clock.advance(hours=2)

    assert service.snapshot().hold_until is None
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "ok"
    assert service.state.hold_until is None


async def test_a_plain_push_clears_a_previous_hold(tmp_path: Path) -> None:
    clock, runner = FakeClock(), FakeRunner()
    service = make_service(tmp_path, clock, runner)
    await service.record_manual(None, hold_minutes=600)
    await service.record_manual(ALICE)
    assert service.state.hold_until is None
    assert service.next_run_at == T0 + timedelta(hours=2)


async def test_manual_refresh_ignores_the_hold(tmp_path: Path) -> None:
    clock, runner = FakeClock(), FakeRunner()
    service = make_service(tmp_path, clock, runner)
    await service.record_manual(ALICE, hold_minutes=600)

    await service.run("manual")

    assert runner.calls == [ALICE]
    assert service.state.hold_until is None


async def test_push_during_a_running_refresh_does_not_block_or_corrupt_state(
    tmp_path: Path,
) -> None:
    clock, runner = FakeClock(), FakeRunner()
    runner.gate = asyncio.Event()
    service = make_service(tmp_path, clock, runner)
    await service.record_manual(ALICE)

    refreshing = asyncio.create_task(service.run("manual"))
    await asyncio.sleep(0)
    assert service.running

    clock.advance(minutes=1)
    await service.record_manual(BOB, ephemeral=True, hold_minutes=90)  # not blocked by the lock
    assert service.state.hold_until == clock.now + timedelta(minutes=90)

    runner.gate.set()
    await refreshing
    # The refresh finished last, so the timer state is the refresh's; the rule is untouched.
    assert service.state.last_rule == ALICE
    assert service.state.hold_until is None
    await service.record_manual(None, hold_minutes=30)  # a later push wins again
    assert service.state.hold_until == clock.now + timedelta(minutes=30)


def test_non_replayed_fields_stay_out_of_the_rule() -> None:
    from app.providers.immich import ImmichDisplayRequest

    request = ImmichDisplayRequest(persons=["Alice"], ephemeral=True, hold_minutes=30)
    params = immich_rule(request).params
    assert params["persons"] == ["Alice"]
    assert "ephemeral" not in params and "hold_minutes" not in params


# --- API --------------------------------------------------------------------------------
def test_ephemeral_immich_push_keeps_the_previous_rule(make_client: ClientFactory) -> None:
    fake: FakeImmich = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake):
        assert show(client, persons=["Alice"]).status_code == 200
        assert show(client, persons=["Bob"], ephemeral=True, hold_minutes=120).status_code == 200
        response = refresh(client)

    assert response.status_code == 200, response.text
    assert response.json()["display"]["image"]["title"].startswith("Alice")  # Bob was ephemeral


def test_hold_is_reported_in_the_status(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake):
        assert show(client, persons=["Alice"], hold_minutes=45).status_code == 200
    scheduler = client.get("/api/status").json()["scheduler"]
    assert scheduler["hold_until"] is not None


def test_upload_accepts_hold_and_ephemeral(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    with servers(fake):
        assert upload(client).status_code == 200
    assert client.get("/api/status").json()["scheduler"]["hold_until"] is None


def test_direct_push_never_changes_the_configuration(make_client: ClientFactory) -> None:
    fake = alice_and_bob()
    client = immich_client(make_client)
    pin(client, {"kind": "immich", "params": {"persons": ["Bob"]}})
    before = client.get("/api/config").json()
    with servers(fake):
        assert show(client, persons=["Alice"], ephemeral=True, hold_minutes=10).status_code == 200
    assert client.get("/api/config").json() == before


@pytest.mark.parametrize("minutes", [0, -5, 10081])
def test_hold_minutes_are_validated(make_client: ClientFactory, minutes: int) -> None:
    client = immich_client(make_client)
    assert show(client, hold_minutes=minutes).status_code == 422
