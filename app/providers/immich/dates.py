"""Date windows for "on this day" memories."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo

from app.providers.immich.models import OnThisDay

DateWindow = tuple[datetime | None, datetime | None]


def _same_day(year: int, month: int, day: int) -> date:
    """``month/day`` in ``year``; Feb 29 falls back to Feb 28 in non-leap years."""
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


def on_this_day_windows(now: datetime, tz: tzinfo, spec: OnThisDay) -> list[DateWindow]:
    """One inclusive UTC window per past year, centred on today's date (in ``tz``).

    Newest year first. Window bounds are local midnight to local end-of-day so the days at the
    edge are fully covered whatever the time zone.
    """
    today = now.astimezone(tz).date()
    windows: list[DateWindow] = []
    for years_ago in range(spec.min_years_ago, spec.years_back + 1):
        year = today.year - years_ago
        if year < 1:
            break
        centre = _same_day(year, today.month, today.day)
        start = datetime.combine(centre - timedelta(days=spec.window_days), time.min, tzinfo=tz)
        end = datetime.combine(centre + timedelta(days=spec.window_days), time.max, tzinfo=tz)
        windows.append((start.astimezone(UTC), end.astimezone(UTC)))
    return windows
