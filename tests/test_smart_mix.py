"""Smart Mix: weighted resolver and its integration in the scheduler."""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.config_models import SmartMixConfig, SmartMixEntry, SourceRule
from app.core.config_store import ConfigStore
from app.providers.base import NoCandidateError
from app.services.scheduler import NoRuleError, SchedulerService
from app.services.smart_mix import MixContext, SmartMixResolver
from tests.test_scheduler_service import ALICE, BOB, T0, FakeClock, FakeRunner


def seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


class Scripted(random.Random):
    """``random()`` returns scripted values (a small value sorts that candidate first)."""

    def __init__(self, *values: float) -> None:
        super().__init__(0)
        self.values = list(values)

    def random(self) -> float:
        return self.values.pop(0)


def entry(kind: str = "immich", weight: float = 1.0, **extra: Any) -> SmartMixEntry:
    return SmartMixEntry(kind=kind, weight=weight, **extra)


def mix(*entries: SmartMixEntry, **config: Any) -> SmartMixConfig:
    return SmartMixConfig(enabled=True, entries=list(entries), **config)


# --- Resolver -------------------------------------------------------------------------
def test_weights_drive_the_first_pick() -> None:
    resolver = SmartMixResolver(seeded(7))
    config = mix(entry(weight=3, label="a"), entry(weight=1, label="b"), avoid_consecutive=False)
    firsts = [resolver.order(config)[0].label for _ in range(4000)]
    assert 0.72 < firsts.count("a") / len(firsts) < 0.78


def test_order_contains_every_enabled_entry_once() -> None:
    config = mix(entry(label="a"), entry(label="b", enabled=False), entry(label="c"))
    order = SmartMixResolver(seeded(1)).order(config)
    assert sorted(c.label or "" for c in order) == ["a", "c"]
    assert sorted(c.index for c in order if c.index is not None) == [0, 2]


def test_avoid_consecutive_moves_the_last_entry_to_the_end() -> None:
    config = mix(entry(label="a"), entry(label="b"), entry(label="c"))
    resolver = SmartMixResolver(Scripted(0.01, 0.5, 0.9))  # natural order: a, b, c
    assert [c.label for c in resolver.order(config, last_index=0)] == ["b", "c", "a"]


def test_avoid_consecutive_can_be_disabled_and_ignores_single_entries() -> None:
    config = mix(entry(label="a"), entry(label="b"), avoid_consecutive=False)
    resolver = SmartMixResolver(Scripted(0.01, 0.9))
    assert [c.label for c in resolver.order(config, last_index=0)] == ["a", "b"]
    single = mix(entry(label="a"))
    assert [c.label for c in SmartMixResolver().order(single, last_index=0)] == ["a"]


def test_context_joins_the_pool_or_replaces_it() -> None:
    config = mix(entry(label="a"))
    context = MixContext(rule=BOB, expires_at=T0 + timedelta(hours=1), label="rainy")
    resolver = SmartMixResolver(seeded(1))
    assert sorted(c.label or "" for c in resolver.order(config, context)) == ["a", "rainy"]
    exclusive = context.model_copy(update={"exclusive": True})
    assert [c.label for c in resolver.order(config, exclusive)] == ["rainy"]


def test_context_liveness() -> None:
    context = MixContext(rule=BOB, expires_at=T0 + timedelta(minutes=5))
    assert context.live(T0) and not context.live(T0 + timedelta(minutes=5))


# --- Scheduler integration --------------------------------------------------------------
class MixHarness:
    def __init__(self, tmp_path: Path, **smart_mix: Any) -> None:
        self.clock = FakeClock()
        self.immich = FakeRunner()
        self.other = FakeRunner()
        self.store = ConfigStore(
            tmp_path / "config.json",
            {
                "scheduler": {"enabled": True},
                "smart_mix": {"enabled": True, "discovery_share": 0, **smart_mix},
            },
        )
        self.store.load()
        self.state_path = tmp_path / "scheduler_state.json"

    def service(self, *order: float) -> SchedulerService:
        service = SchedulerService(
            self.state_path,
            self.store,
            {"immich": self.immich, "other": self.other},
            clock=self.clock,
            resolver=SmartMixResolver(Scripted(*order) if order else seeded(3)),
        )
        service.load()
        return service


def entries(*specs: tuple[str, str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"kind": kind, "label": label, "params": params, "weight": 1}
        for kind, label, params in specs
    ]


@pytest.fixture
def h(tmp_path: Path) -> MixHarness:
    return MixHarness(
        tmp_path,
        entries=entries(("immich", "alice", {"persons": ["Alice"]}), ("other", "film", {})),
    )


async def test_precedence_pinned_then_mix_then_last(h: MixHarness) -> None:
    service = h.service()
    await service.record_manual(ALICE)
    active = service.active_rule()
    assert active is not None and (active.source, active.kind) == ("smart_mix", "smart_mix")

    await h.store.update({"scheduler": {"source_rule": BOB.model_dump()}})
    active = service.active_rule()
    assert active is not None and (active.source, active.rule) == ("pinned", BOB)

    await h.store.update({"scheduler": {"source_rule": None}, "smart_mix": {"enabled": False}})
    active = service.active_rule()
    assert active is not None and (active.source, active.rule) == ("last", ALICE)


async def test_mix_without_enabled_entries_falls_back_to_last_rule(tmp_path: Path) -> None:
    h = MixHarness(tmp_path, entries=[{"kind": "immich", "enabled": False}])
    service = h.service()
    assert service.active_rule() is None
    await service.record_manual(ALICE)
    active = service.active_rule()
    assert active is not None and active.source == "last"


async def test_refresh_picks_a_weighted_entry_and_keeps_last_rule(h: MixHarness) -> None:
    service = h.service(0.01, 0.9)  # alice first
    await service.record_manual(BOB)
    h.clock.advance(hours=1)

    outcome = await service.run("manual")

    assert outcome.active.source == "smart_mix"
    assert outcome.entry_label == "alice" and outcome.rule.kind == "immich"
    assert h.immich.calls == [SourceRule(kind="immich", params={"persons": ["Alice"]})]
    assert service.state.last_rule == BOB  # mix runs never rewrite the last manual rule
    assert service.state.last_mix_index == 0
    assert service.state.last_run is not None
    assert (service.state.last_run.entry_label, service.state.last_run.entry_index) == ("alice", 0)
    assert service.state.timer_reset_at == h.clock.now


async def test_no_candidate_rerolls_to_the_next_entry(h: MixHarness) -> None:
    h.immich.error = NoCandidateError("nothing")
    service = h.service(0.01, 0.9)  # alice (fails), then film

    outcome = await service.run("manual")

    assert outcome.entry_label == "film"
    assert len(h.immich.calls) == 1 and len(h.other.calls) == 1
    assert service.state.last_mix_index == 1


async def test_all_entries_failing_raises_the_last_error_and_backs_off(h: MixHarness) -> None:
    h.immich.error = NoCandidateError("nothing")
    h.other.error = NoCandidateError("still nothing")
    service = h.service(0.01, 0.9, 0.01, 0.9)

    with pytest.raises(NoCandidateError, match="still nothing"):
        await service.run("manual")
    assert service.state.last_run is not None and service.state.last_run.status == "failed"

    last_run = await service.tick()
    assert last_run is not None and last_run.status == "failed"
    assert service.state.consecutive_failures == 1 and service.state.retry_at is not None


async def test_max_attempts_limits_the_rerolls(tmp_path: Path) -> None:
    h = MixHarness(
        tmp_path,
        max_attempts=1,
        entries=entries(("immich", "a", {}), ("other", "b", {})),
    )
    h.immich.error = NoCandidateError("nothing")
    service = h.service(0.01, 0.9)
    with pytest.raises(NoCandidateError):
        await service.run("manual")
    assert h.other.calls == []


async def test_unsupported_kind_is_rerolled(tmp_path: Path) -> None:
    h = MixHarness(tmp_path, entries=entries(("tmdb", "poster", {}), ("immich", "a", {})))
    service = h.service(0.01, 0.9)  # tmdb first: no runner registered
    outcome = await service.run("manual")
    assert outcome.entry_label == "a"
    assert service.supported_kinds == ["immich", "other"]


async def test_placeholders_are_expanded_before_running(tmp_path: Path) -> None:
    h = MixHarness(
        tmp_path,
        entries=entries(("immich", "season", {"clip_queries": ["{season} {month_name}"]})),
    )
    await h.service().run("manual")
    assert h.immich.calls[0].params == {"clip_queries": ["winter january"]}  # T0 = January


async def test_context_boost_and_expiry(h: MixHarness) -> None:
    service = h.service()
    context = MixContext(
        rule=SourceRule(kind="other", params={"q": "beach"}),
        expires_at=h.clock.now + timedelta(hours=1),
        exclusive=True,
        label="beach",
    )
    await service.set_context(context)

    outcome = await service.run("manual")
    assert outcome.entry_label == "beach" and h.other.calls[-1].params == {"q": "beach"}

    h.clock.advance(hours=2)
    assert service.live_context() is None
    outcome = await service.run("manual")
    assert outcome.entry_label in {"alice", "film"}

    await service.set_context(context)
    await service.set_context(None)
    assert service.state.context is None


async def test_context_makes_an_otherwise_empty_mix_active(tmp_path: Path) -> None:
    h = MixHarness(tmp_path, entries=[])
    service = h.service()
    with pytest.raises(NoRuleError):
        await service.run("manual")
    await service.set_context(
        MixContext(rule=ALICE, expires_at=h.clock.now + timedelta(minutes=10))
    )
    assert (await service.run("manual")).rule == ALICE


async def test_snapshot_reports_the_mix(h: MixHarness) -> None:
    snapshot = h.service().snapshot()
    assert snapshot.active_rule is not None
    assert (snapshot.active_rule.kind, snapshot.active_rule.source) == ("smart_mix", "smart_mix")
