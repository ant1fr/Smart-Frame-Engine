"""SchedulerService: timer planning, quiet hours, backoff, lock and persistence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from app.core.config_models import SourceRule
from app.core.config_store import ConfigStore
from app.providers.base import ProviderUnavailableError
from app.providers.immich import ImmichDisplayRequest
from app.services.display import DisplayResult
from app.services.scheduler import (
    JOB_ID,
    InvalidRuleError,
    NoRuleError,
    RefreshBusyError,
    SchedulerService,
    UnsupportedRuleError,
    immich_rule,
    immich_runner,
)

PARIS = ZoneInfo("Europe/Paris")
# Far in the future so a timer armed from the fake clock never fires during a test.
T0 = datetime(2099, 1, 14, 12, 0, tzinfo=UTC)
ALICE = SourceRule(kind="immich", params={"persons": ["Alice"]})
BOB = SourceRule(kind="immich", params={"persons": ["Bob"]})
DISPLAY = cast(DisplayResult, object())


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[SourceRule] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, rule: SourceRule) -> DisplayResult:
        self.calls.append(rule)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return DISPLAY


class Harness:
    def __init__(self, tmp_path: Path, clock: FakeClock, **scheduler: Any) -> None:
        self.tmp_path = tmp_path
        self.clock = clock
        self.runner = FakeRunner()
        self.pending = False
        scheduler.setdefault("enabled", True)
        self.store = ConfigStore(tmp_path / "config.json", {"scheduler": scheduler})
        self.store.load()
        self.services: list[SchedulerService] = []

    @property
    def state_path(self) -> Path:
        return self.tmp_path / "scheduler_state.json"

    def service(self, *, clock: Callable[[], datetime] | None = None, **kwargs: Any) -> Any:
        service = SchedulerService(
            self.state_path,
            self.store,
            {"immich": self.runner},
            has_pending=lambda: self.pending,
            clock=clock or self.clock,
            **kwargs,
        )
        service.load()
        self.services.append(service)
        return service

    async def configure(self, **scheduler: Any) -> None:
        await self.store.update({"scheduler": scheduler})


HarnessFactory = Callable[..., Harness]


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[HarnessFactory]:
    made: list[Harness] = []

    def _make(clock: FakeClock | None = None, **scheduler: Any) -> Harness:
        h = Harness(tmp_path, clock or FakeClock(), **scheduler)
        made.append(h)
        return h

    yield _make
    for h in made:
        for service in h.services:
            await service.stop()


async def eventually(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


# --- Timer planning -------------------------------------------------------------------
async def test_success_rearms_at_last_display_plus_interval(harness: HarnessFactory) -> None:
    h = harness(interval_hours=6)
    service = h.service()
    await service.record_manual(ALICE)
    assert service.next_run_at == T0 + timedelta(hours=6)

    h.clock.advance(hours=6)
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "ok" and last_run.reason == "scheduled"
    assert h.runner.calls == [ALICE]
    assert service.state.last_display_at == T0 + timedelta(hours=6)
    assert service.next_run_at == T0 + timedelta(hours=12)


async def test_any_display_resets_the_timer_and_uploads_keep_the_rule(
    harness: HarnessFactory,
) -> None:
    h = harness(interval_hours=6)
    service = h.service()
    await service.record_manual(ALICE)
    h.clock.advance(hours=5)
    await service.record_manual(None)  # e.g. an uploaded file
    assert service.next_run_at == T0 + timedelta(hours=11)
    active = service.active_rule()
    assert active is not None and (active.rule, active.source) == (ALICE, "last")


async def test_first_run_and_overdue_runs_wait_the_minimum_delay(
    harness: HarnessFactory,
) -> None:
    h = harness(interval_hours=1, source_rule=ALICE.model_dump())
    service = h.service(min_delay_s=30)
    assert service.next_run_at == T0 + timedelta(seconds=30)  # never displayed anything
    await service.record_manual(None)
    h.clock.advance(hours=5)  # e.g. the app was down
    assert service.compute_next_run() == h.clock.now + timedelta(seconds=30)


async def test_disabled_has_no_next_run(harness: HarnessFactory) -> None:
    h = harness(enabled=False)
    service = h.service()
    await service.record_manual(ALICE)
    assert service.next_run_at is None
    assert await service.tick() is None
    assert h.runner.calls == []


# --- Quiet hours -------------------------------------------------------------------
async def test_due_time_inside_quiet_hours_moves_to_the_window_end(
    harness: HarnessFactory,
) -> None:
    # 18:00 in Paris (CET, UTC+1) + 6 h = midnight, inside 22:00 -> 07:00.
    clock = FakeClock(datetime(2099, 1, 14, 18, 0, tzinfo=PARIS))
    h = harness(clock, interval_hours=6, timezone="Europe/Paris")
    await h.configure(quiet_hours_start="22:00", quiet_hours_end="07:00")
    service = h.service()
    await service.record_manual(ALICE)
    assert service.next_run_at == datetime(2099, 1, 15, 7, 0, tzinfo=PARIS)
    # The same window in UTC would end at 08:00 Paris time.
    await h.configure(timezone="UTC")
    service._arm()
    assert service.next_run_at == datetime(2099, 1, 15, 7, 0, tzinfo=UTC)


async def test_tick_inside_quiet_hours_skips_and_waits_for_the_window_end(
    harness: HarnessFactory,
) -> None:
    clock = FakeClock(datetime(2099, 1, 14, 12, 0, tzinfo=PARIS))
    h = harness(clock, interval_hours=6, timezone="Europe/Paris")
    service = h.service()
    await service.record_manual(ALICE)
    # The window is added after the timer was armed; the tick then lands inside it.
    await h.configure(quiet_hours_start="17:00", quiet_hours_end="19:30")
    clock.now = datetime(2099, 1, 14, 18, 0, tzinfo=PARIS)
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "skipped"
    assert last_run.detail == "Quiet hours"
    assert h.runner.calls == []
    assert service.state.timer_reset_at == datetime(2099, 1, 14, 12, 0, tzinfo=PARIS)
    assert service.next_run_at == datetime(2099, 1, 14, 19, 30, tzinfo=PARIS)
    assert service.in_quiet_hours() is True


async def test_manual_run_ignores_quiet_hours_and_pending(harness: HarnessFactory) -> None:
    clock = FakeClock(datetime(2099, 1, 14, 23, 0, tzinfo=PARIS))
    h = harness(clock, interval_hours=6, timezone="Europe/Paris")
    await h.configure(quiet_hours_start="22:00", quiet_hours_end="07:00")
    service = h.service()
    await service.record_manual(ALICE)
    h.pending = True
    outcome = await service.run()
    assert outcome.reason == "manual" and outcome.active.source == "last"
    assert outcome.display is DISPLAY
    assert h.runner.calls == [ALICE]
    assert service.state.last_run is not None and service.state.last_run.reason == "manual"
    # 23:00 + 6 h = 05:00, inside the window.
    assert service.next_run_at == datetime(2099, 1, 15, 7, 0, tzinfo=PARIS)


# --- Skips and failures ------------------------------------------------------------
async def test_skip_if_pending(harness: HarnessFactory) -> None:
    h = harness(interval_hours=2)
    service = h.service()
    await service.record_manual(ALICE)
    h.pending = True
    h.clock.advance(hours=2)
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "skipped"
    assert last_run.detail is not None and "waiting for the frame" in last_run.detail
    assert h.runner.calls == []
    assert service.next_run_at == h.clock.now + timedelta(hours=2)  # the slot is consumed

    await h.configure(skip_if_pending=False)
    h.clock.advance(hours=2)
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "ok"
    assert h.runner.calls == [ALICE]


async def test_failure_backoff_is_capped_and_reset_on_success(harness: HarnessFactory) -> None:
    h = harness(interval_hours=1, retry_minutes=15)
    service = h.service()
    await service.record_manual(ALICE)
    h.runner.error = ProviderUnavailableError("Immich is down")

    delays = []
    for _ in range(4):
        h.clock.now = service.next_run_at
        last_run = await service.tick()
        assert last_run is not None and last_run.status == "failed"
        assert last_run.detail == "Immich is down"
        delays.append(service.next_run_at - h.clock.now)
    assert delays == [timedelta(minutes=m) for m in (15, 30, 60, 60)]
    assert service.state.consecutive_failures == 4

    h.runner.error = None
    h.clock.now = service.next_run_at
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "ok"
    assert service.state.consecutive_failures == 0
    assert service.state.retry_at is None
    assert service.next_run_at == h.clock.now + timedelta(hours=1)


async def test_unexpected_errors_are_recorded_without_details(harness: HarnessFactory) -> None:
    h = harness()
    service = h.service()
    await service.record_manual(ALICE)
    h.runner.error = RuntimeError("secret internals")
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "failed"
    assert last_run.detail is not None and "secret" not in last_run.detail


async def test_manual_failure_does_not_count(harness: HarnessFactory) -> None:
    h = harness(interval_hours=6)
    service = h.service()
    await service.record_manual(ALICE)
    h.runner.error = ProviderUnavailableError("down")
    with pytest.raises(ProviderUnavailableError):
        await service.run()
    assert service.state.consecutive_failures == 0
    assert service.state.last_run is not None
    assert (service.state.last_run.reason, service.state.last_run.status) == ("manual", "failed")
    assert service.next_run_at == T0 + timedelta(hours=6)  # timer untouched


async def test_pinned_rule_beats_the_last_rule(harness: HarnessFactory) -> None:
    h = harness()
    service = h.service()
    await service.record_manual(ALICE)
    await h.configure(source_rule=BOB.model_dump())
    active = service.active_rule()
    assert active is not None and (active.rule, active.source) == (BOB, "pinned")
    await service.tick()
    assert h.runner.calls == [BOB]
    assert service.state.last_rule == ALICE  # the manual rule is kept for later


async def test_no_rule(harness: HarnessFactory) -> None:
    h = harness()
    service = h.service()
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "skipped"
    assert last_run.detail is not None and "No rule" in last_run.detail
    with pytest.raises(NoRuleError):
        await service.run()
    assert service.state.last_run == last_run  # a refused manual run records nothing


async def test_unsupported_rule_kind(harness: HarnessFactory) -> None:
    h = harness(source_rule={"kind": "media", "params": {"title": "Alien"}})
    service = h.service()
    last_run = await service.tick()
    assert last_run is not None and last_run.status == "skipped"
    assert last_run.rule_kind == "media"
    with pytest.raises(UnsupportedRuleError, match="media"):
        await service.run()


async def test_concurrent_refresh_is_refused(harness: HarnessFactory) -> None:
    h = harness()
    service = h.service()
    await service.record_manual(ALICE)
    h.runner.gate = asyncio.Event()
    first = asyncio.create_task(service.run())
    await eventually(lambda: bool(h.runner.calls))
    assert service.running
    with pytest.raises(RefreshBusyError):
        await service.run()
    assert await service.tick() is None
    h.runner.gate.set()
    await first
    assert len(h.runner.calls) == 1
    assert not service.running


# --- Persistence --------------------------------------------------------------------
async def test_state_survives_a_restart(harness: HarnessFactory) -> None:
    h = harness(interval_hours=4)
    service = h.service()
    await service.record_manual(ALICE)
    h.clock.advance(hours=4)
    h.runner.error = ProviderUnavailableError("down")
    await service.tick()

    restarted = h.service()
    assert restarted.state == service.state
    assert restarted.state.last_rule == ALICE
    assert restarted.state.consecutive_failures == 1
    assert restarted.next_run_at == service.next_run_at == h.clock.now + timedelta(minutes=15)
    saved = json.loads(h.state_path.read_text(encoding="utf-8"))
    assert saved["last_rule"] == {"kind": "immich", "params": {"persons": ["Alice"]}}


@pytest.mark.parametrize(
    "content", ['{"consecutive_failures": -3}', '{"last_rule": {"kind": "ftp"}}', "[1, 2]"]
)
async def test_malformed_state_is_ignored(
    harness: HarnessFactory, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness()
    h.state_path.write_text(content, encoding="utf-8")
    service = h.service()
    assert service.state.last_rule is None
    assert "malformed scheduler state" in caplog.text


async def test_corrupt_state_file_is_moved_aside(harness: HarnessFactory) -> None:
    h = harness()
    h.state_path.write_text("{oops", encoding="utf-8")
    service = h.service()
    assert service.state.last_rule is None
    assert list(h.tmp_path.glob("scheduler_state.json.corrupt-*"))


# --- Timer job ----------------------------------------------------------------------
async def test_config_changes_rearm_and_disabling_removes_the_job(
    harness: HarnessFactory,
) -> None:
    h = harness(interval_hours=6)
    service = h.service()
    await service.start()
    await service.record_manual(ALICE)
    job = service._scheduler.get_job(JOB_ID)
    assert job is not None and job.next_run_time == T0 + timedelta(hours=6)

    await h.configure(interval_hours=2)
    assert service.next_run_at == T0 + timedelta(hours=2)
    assert service._scheduler.get_job(JOB_ID).next_run_time == T0 + timedelta(hours=2)

    await h.configure(enabled=False)
    assert service.next_run_at is None
    assert service._scheduler.get_job(JOB_ID) is None

    await h.configure(enabled=True)
    assert service._scheduler.get_job(JOB_ID) is not None


async def test_real_timer_fires_once(harness: HarnessFactory) -> None:
    h = harness(interval_hours=6, source_rule=ALICE.model_dump())
    service = h.service(clock=lambda: datetime.now(UTC), min_delay_s=0.05)
    started = datetime.now(UTC)
    await service.start()
    await eventually(lambda: service.state.last_run is not None and not service.running, 5)
    assert h.runner.calls == [ALICE]
    assert service.state.last_run is not None and service.state.last_run.status == "ok"
    assert service.next_run_at is not None
    assert service.next_run_at - started > timedelta(hours=5, minutes=59)
    await asyncio.sleep(0.2)
    assert len(h.runner.calls) == 1
    await service.stop()


# --- Immich rules -------------------------------------------------------------------
def test_immich_rule_keeps_only_replayable_fields() -> None:
    request = ImmichDisplayRequest(
        persons=["Alice"], album_name="Summer", push_to_fraimic=False, ignore_history=True
    )
    rule = immich_rule(request)
    assert rule.kind == "immich"
    assert rule.params == {"persons": ["Alice"], "album_name": "Summer", "clip_queries": []}


async def test_immich_runner_rejects_invalid_params() -> None:
    runner = immich_runner(cast(Any, None), cast(Any, None))
    rule = SourceRule(kind="immich", params={"mode": "bogus-mode", "extra": "x"})
    with pytest.raises(InvalidRuleError) as excinfo:
        await runner(rule)
    message = str(excinfo.value)
    assert "mode" in message and "extra" in message
    assert "bogus-mode" not in message
