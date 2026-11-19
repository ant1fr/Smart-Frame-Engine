from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.providers.immich.dates import on_this_day_windows
from app.providers.immich.models import OnThisDay


def test_one_window_per_past_year_newest_first() -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    windows = on_this_day_windows(now, UTC, OnThisDay(window_days=2, years_back=3))

    assert [w[0].year for w in windows] == [2025, 2024, 2023]
    assert windows[0] == (
        datetime(2025, 11, 13, 0, 0, tzinfo=UTC),
        datetime(2025, 11, 17, 23, 59, 59, 999999, tzinfo=UTC),
    )


def test_min_years_ago_skips_recent_years() -> None:
    now = datetime(2026, 11, 15, tzinfo=UTC)
    windows = on_this_day_windows(now, UTC, OnThisDay(years_back=4, min_years_ago=3))
    assert [w[0].year for w in windows] == [2023, 2022]


def test_leap_day_falls_back_to_feb_28_in_non_leap_years() -> None:
    now = datetime(2028, 2, 29, 12, 0, tzinfo=UTC)
    windows = on_this_day_windows(now, UTC, OnThisDay(window_days=0, years_back=4))
    assert [(w[0].year, w[0].month, w[0].day) for w in windows] == [
        (2027, 2, 28),
        (2026, 2, 28),
        (2025, 2, 28),
        (2024, 2, 29),
    ]


def test_local_day_is_used_for_the_window_bounds() -> None:
    paris = ZoneInfo("Europe/Paris")
    now = datetime(2026, 11, 14, 23, 30, tzinfo=UTC)  # already Nov 15 in Paris
    start, end = on_this_day_windows(now, paris, OnThisDay(window_days=0, years_back=1))[0]
    assert start == datetime(2025, 11, 14, 23, 0, tzinfo=UTC)
    assert end.date().isoformat() == "2025-11-15"
