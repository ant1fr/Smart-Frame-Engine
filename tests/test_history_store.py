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
