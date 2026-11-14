"""Display history persisted to ``/data/history.json`` (used for anti-repeat rules)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from app.core.json_store import atomic_write_json, read_json

logger = logging.getLogger(__name__)

HISTORY_FORMAT_VERSION = 1


class HistoryEntry(BaseModel):
    """One image that was displayed on the frame."""

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(min_length=1, description="Provider-specific asset identifier")
    source: str = Field(min_length=1, description="Provider name, e.g. 'immich'")
    shown_at: AwareDatetime = Field(description="When the image was displayed (UTC)")


class HistoryStore:
    """Append-only display log with time- and size-based retention."""

    def __init__(
        self,
        path: Path,
        *,
        max_age_days: int = 365,
        max_entries: int = 5000,
    ) -> None:
        """Create the store.

        Args:
            path: Location of ``history.json``.
            max_age_days: Entries older than this are discarded.
            max_entries: Hard cap on stored entries (oldest are discarded first).
        """
        if max_age_days < 1 or max_entries < 1:
            raise ValueError("max_age_days and max_entries must be positive")
        self._path = path
        self._max_age = timedelta(days=max_age_days)
        self._max_entries = max_entries
        self._entries: list[HistoryEntry] = []
        self._lock = asyncio.Lock()

    def load(self) -> None:
        """Load the history from disk, skipping malformed entries."""
        raw = read_json(self._path, dict)
        items: Any = raw.get("entries", []) if isinstance(raw, dict) else []
        entries: list[HistoryEntry] = []
        skipped = 0
        for item in items if isinstance(items, list) else []:
            try:
                entries.append(HistoryEntry.model_validate(item))
            except ValidationError:
                skipped += 1
        if skipped:
            logger.warning("Skipped %d malformed history entries in %s", skipped, self._path)
        entries.sort(key=lambda entry: entry.shown_at)
        self._entries = self._prune(entries, datetime.now(UTC))

    async def record(
        self, asset_id: str, source: str, shown_at: datetime | None = None
    ) -> HistoryEntry:
        """Append an entry and persist the history.

        Args:
            asset_id: Provider-specific identifier of the displayed asset.
            source: Provider name.
            shown_at: Display time (timezone-aware); defaults to now.
        """
        now = datetime.now(UTC)
        entry = HistoryEntry(
            asset_id=asset_id, source=source, shown_at=(shown_at or now).astimezone(UTC)
        )
        async with self._lock:
            entries = sorted([*self._entries, entry], key=lambda item: item.shown_at)
            entries = self._prune(entries, now)
            await asyncio.to_thread(self._persist, entries)
            self._entries = entries
        return entry

    def recent_ids(self, source: str, within_days: int) -> set[str]:
        """Asset IDs from ``source`` displayed within the last ``within_days`` days."""
        if within_days <= 0:
            return set()
        cutoff = datetime.now(UTC) - timedelta(days=within_days)
        return {
            entry.asset_id
            for entry in self._entries
            if entry.source == source and entry.shown_at >= cutoff
        }

    def entries(self, limit: int | None = None) -> list[HistoryEntry]:
        """Entries, newest first, optionally limited."""
        newest_first = list(reversed(self._entries))
        return newest_first if limit is None else newest_first[: max(limit, 0)]

    def _prune(self, entries: list[HistoryEntry], now: datetime) -> list[HistoryEntry]:
        cutoff = now - self._max_age
        kept = [entry for entry in entries if entry.shown_at >= cutoff]
        return kept[-self._max_entries :]

    def _persist(self, entries: list[HistoryEntry]) -> None:
        atomic_write_json(
            self._path,
            {
                "version": HISTORY_FORMAT_VERSION,
                "entries": [entry.model_dump(mode="json") for entry in entries],
            },
        )
