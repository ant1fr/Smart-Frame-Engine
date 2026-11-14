from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.history_store import HistoryStore


@pytest.fixture
def history_path(tmp_path: Path) -> Path:
    return tmp_path / "history.json"


async def test_record_and_recent_ids(history_path: Path) -> None:
    store = HistoryStore(history_path)
    store.load()
    now = datetime.now(UTC)
    await store.record("a1", "immich", now - timedelta(days=40))
    await store.record("a2", "immich", now - timedelta(days=2))
    await store.record("p1", "media", now)

    assert store.recent_ids("immich", 30) == {"a2"}
    assert store.recent_ids("immich", 60) == {"a1", "a2"}
    assert store.recent_ids("immich", 0) == set()
    assert [entry.asset_id for entry in store.entries()] == ["p1", "a2", "a1"]
    assert [entry.asset_id for entry in store.entries(limit=1)] == ["p1"]


async def test_persistence_round_trip(history_path: Path) -> None:
    store = HistoryStore(history_path)
    store.load()
    await store.record("a1", "immich")

    payload = json.loads(history_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["entries"][0]["asset_id"] == "a1"

    reloaded = HistoryStore(history_path)
    reloaded.load()
    assert reloaded.recent_ids("immich", 1) == {"a1"}


async def test_retention_by_age_and_count(history_path: Path) -> None:
    store = HistoryStore(history_path, max_age_days=10, max_entries=3)
    store.load()
    now = datetime.now(UTC)
    await store.record("old", "immich", now - timedelta(days=11))
    for index in range(4):
        await store.record(f"n{index}", "immich", now - timedelta(minutes=10 - index))
    assert [entry.asset_id for entry in store.entries()] == ["n3", "n2", "n1"]


def test_malformed_entries_are_skipped(history_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    history_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"asset_id": "ok", "source": "immich", "shown_at": now},
                    {"asset_id": "", "source": "immich", "shown_at": now},
                    {"asset_id": "naive", "source": "immich", "shown_at": "2026-01-01T00:00:00"},
                    "garbage",
                ],
            }
        ),
        encoding="utf-8",
    )
    store = HistoryStore(history_path)
    store.load()
    assert [entry.asset_id for entry in store.entries()] == ["ok"]


def test_invalid_limits_rejected(history_path: Path) -> None:
    with pytest.raises(ValueError):
        HistoryStore(history_path, max_entries=0)


# --- taken_at and burst detection ---------------------------------------------------------------
async def test_taken_at_is_persisted_and_naive_values_are_utc(history_path: Path) -> None:
    store = HistoryStore(history_path)
    store.load()
    await store.record("a1", "immich", taken_at=datetime(2024, 7, 14, 10, 0, tzinfo=UTC))
    await store.record("a2", "immich", taken_at=datetime(2024, 7, 14, 12, 30))  # naive
    await store.record("a3", "immich")

    reloaded = HistoryStore(history_path)
    reloaded.load()
    by_id = {entry.asset_id: entry for entry in reloaded.entries()}
    assert by_id["a1"].taken_at == datetime(2024, 7, 14, 10, 0, tzinfo=UTC)
    assert by_id["a2"].taken_at == datetime(2024, 7, 14, 12, 30, tzinfo=UTC)
    assert by_id["a3"].taken_at is None


def test_entries_written_before_taken_at_existed_still_load(history_path: Path) -> None:
    old = {
        "version": 1,
        "entries": [
            {"asset_id": "a1", "source": "immich", "shown_at": datetime.now(UTC).isoformat()}
        ],
    }
    history_path.write_text(json.dumps(old), encoding="utf-8")
    store = HistoryStore(history_path)
    store.load()
    assert store.entries()[0].taken_at is None
    assert store.recent_ids("immich", 30) == {"a1"}


async def test_near_recent_detects_bursts_in_both_directions(history_path: Path) -> None:
    store = HistoryStore(history_path)
    store.load()
    shot = datetime(2024, 7, 14, 10, 0, tzinfo=UTC)
    await store.record("a1", "immich", taken_at=shot)

    def near(offset_hours: float, **kwargs: float) -> bool:
        taken = shot + timedelta(hours=offset_hours)
        return store.near_recent(
            "immich", taken, within_hours=kwargs.get("hours", 2), shown_within_days=30
        )

    assert near(0.5) and near(-0.5) and near(2) and near(-2)  # the window is inclusive
    assert not near(2.01) and not near(-3) and not near(48)
    assert near(5, hours=6) and not near(7, hours=6)


async def test_near_recent_ignores_unknown_times_old_entries_and_other_sources(
    history_path: Path,
) -> None:
    store = HistoryStore(history_path)
    store.load()
    shot = datetime(2024, 7, 14, 10, 0, tzinfo=UTC)
    now = datetime.now(UTC)
    await store.record("old", "immich", now - timedelta(days=40), taken_at=shot)
    await store.record("poster", "media", now, taken_at=shot)
    await store.record("no-time", "immich", now)

    def near(taken: datetime | None, *, days: int = 30, hours: float = 2) -> bool:
        return store.near_recent("immich", taken, within_hours=hours, shown_within_days=days)

    assert not near(shot)  # the match was shown too long ago / by another source
    assert near(shot, days=60)  # ... but counts when the shown-window is wide enough
    assert not near(None, days=60)  # unknown capture time is never a burst
    assert not near(shot, days=60, hours=0)  # disabled
    assert not near(shot, days=0)
