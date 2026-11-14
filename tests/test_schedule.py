"""Quiet-hours planning (pure functions, no clock)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.schedule import QuietWindow, in_quiet, next_run_at, quiet_end_after

PARIS = ZoneInfo("Europe/Paris")
NIGHT = QuietWindow(time(22, 0), time(7, 0))  # crosses midnight
NAP = QuietWindow(time(13, 0), time(15, 30))  # same day


def paris(*args: int, fold: int = 0) -> datetime:
    return datetime(*args, tzinfo=PARIS, fold=fold)  # type: ignore[misc]


def utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


# --- QuietWindow --------------------------------------------------------------
def test_parse() -> None:
    assert QuietWindow.parse("22:00", "07:00") == NIGHT
    assert QuietWindow.parse(None, "07:00") is None
    assert QuietWindow.parse("", "") is None
    with pytest.raises(ValueError, match="differ"):
        QuietWindow.parse("07:00", "07:00")
    with pytest.raises(ValueError):
        QuietWindow.parse("7h", "08:00")


def test_window_times_must_be_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        QuietWindow(time(1, 0, tzinfo=UTC), time(2, 0))


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (time(21, 59), False),
        (time(22, 0), True),  # start inclusive
        (time(23, 59), True),
        (time(0, 0), True),
        (time(6, 59), True),
        (time(7, 0), False),  # end exclusive
        (time(12, 0), False),
    ],
)
def test_contains_across_midnight(local: time, expected: bool) -> None:
    assert NIGHT.crosses_midnight
    assert NIGHT.contains(local) is expected


@pytest.mark.parametrize(
    ("local", "expected"),
    [(time(12, 59), False), (time(13, 0), True), (time(15, 29), True), (time(15, 30), False)],
)
def test_contains_same_day(local: time, expected: bool) -> None:
    assert not NAP.crosses_midnight
    assert NAP.contains(local) is expected


# --- in_quiet -----------------------------------------------------------------
def test_same_instant_differs_between_timezones() -> None:
    instant = datetime(2026, 11, 14, 22, 30, tzinfo=UTC)  # 23:30 in Paris (CET)
    assert in_quiet(instant, NIGHT, PARIS) is True
    assert in_quiet(instant, QuietWindow(time(23, 0), time(23, 15)), UTC) is False
    early = datetime(2026, 11, 14, 21, 30, tzinfo=UTC)  # 22:30 Paris, 21:30 UTC
    assert in_quiet(early, NIGHT, PARIS) is True
    assert in_quiet(early, NIGHT, UTC) is False


def test_no_window_is_never_quiet() -> None:
    assert in_quiet(datetime(2026, 1, 1, 3, 0, tzinfo=UTC), None, PARIS) is False


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        in_quiet(datetime(2026, 1, 1, 3, 0), NIGHT, PARIS)
    with pytest.raises(ValueError, match="aware"):
        next_run_at(datetime(2026, 1, 1, 3, 0), NIGHT, PARIS)


# --- quiet_end_after / next_run_at ----------------------------------------------
def test_end_after_before_and_after_midnight() -> None:
    assert quiet_end_after(paris(2026, 11, 14, 23, 0), NIGHT, PARIS) == paris(2026, 11, 15, 7, 0)
    assert quiet_end_after(paris(2026, 11, 15, 3, 0), NIGHT, PARIS) == paris(2026, 11, 15, 7, 0)
    # Exactly at the end: the next end is tomorrow's.
    assert quiet_end_after(paris(2026, 11, 15, 7, 0), NIGHT, PARIS) == paris(2026, 11, 16, 7, 0)


def test_next_run_at_pushes_into_the_window_end() -> None:
    due = datetime(2026, 11, 14, 23, 0, tzinfo=UTC)  # 00:00 Paris, inside the window
    result = next_run_at(due, NIGHT, PARIS)
    assert result == paris(2026, 11, 15, 7, 0)
    assert result.tzinfo is PARIS
    assert result.utcoffset() is not None


def test_next_run_at_keeps_times_outside_the_window() -> None:
    due = paris(2026, 11, 14, 12, 0)
    assert next_run_at(due, NIGHT, PARIS) == due
    assert next_run_at(due, None, PARIS) == due
    # Converted into the scheduler timezone.
    assert next_run_at(datetime(2026, 11, 14, 11, 0, tzinfo=UTC), None, PARIS).hour == 12


def test_dst_spring_forward() -> None:
    # 29 Mar 2026: Paris jumps from 02:00 CET to 03:00 CEST; 02:30 does not exist.
    window = QuietWindow(time(1, 0), time(2, 30))
    end = quiet_end_after(paris(2026, 3, 29, 1, 15), window, PARIS)
    assert end == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # 03:30 CEST, right after the jump
    assert (end.hour, end.minute) == (3, 30)
    # A normal window keeps its wall-clock end across the jump (7 h of wall time, 6 h real).
    start = paris(2026, 3, 29, 0, 30)
    night_end = quiet_end_after(start, NIGHT, PARIS)
    assert night_end == paris(2026, 3, 29, 7, 0)
    assert night_end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=5, minutes=30)


def test_dst_fall_back() -> None:
    # 25 Oct 2026: Paris goes from 03:00 CEST back to 02:00 CET; 02:15 happens twice.
    window = QuietWindow(time(22, 0), time(2, 30))
    first = paris(2026, 10, 25, 2, 15)  # CEST occurrence
    second = paris(2026, 10, 25, 2, 15, fold=1)  # CET occurrence, one hour later
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)
    assert in_quiet(first, window, PARIS) and in_quiet(second, window, PARIS)
    # Ambiguous local times never compare equal across zones (PEP 495): compare in UTC.
    assert utc(quiet_end_after(first, window, PARIS)) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert utc(quiet_end_after(second, window, PARIS)) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    # Autumn evening: the 22:00 -> 07:00 window spans 10 real hours.
    night_end = quiet_end_after(paris(2026, 10, 24, 22, 0), NIGHT, PARIS)
    assert night_end == datetime(2026, 10, 25, 6, 0, tzinfo=UTC)
