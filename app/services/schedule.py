"""Pure scheduling helpers: quiet-hours windows and the next refresh time.

Everything here is side-effect free and works on timezone-aware datetimes, so it can be
tested without a clock or an event loop. Quiet hours are expressed in local wall-clock
time of a configured IANA timezone (Docker containers usually run in UTC), and local
times are built with ``datetime.combine(..., tzinfo=ZoneInfo)`` so daylight saving
transitions are resolved by the timezone database:

* a window boundary that does not exist on a spring-forward day (e.g. 02:30 in Paris)
  is normalized to the equivalent instant just after the jump (03:30);
* on a fall-back day the repeated hour is read literally from the wall clock, so a
  window ending at 02:30 ends at the first 02:30 that follows the moment considered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo


def _parse_hhmm(value: str) -> time:
    hours, _, minutes = value.strip().partition(":")
    return time(int(hours), int(minutes))


@dataclass(frozen=True, slots=True)
class QuietWindow:
    """A daily no-refresh window in local wall-clock time.

    ``start`` is inclusive and ``end`` exclusive. A window whose end is earlier than its
    start crosses midnight (e.g. 22:00 -> 07:00).
    """

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("quiet window times must be naive wall-clock times")
        if self.start == self.end:
            raise ValueError("quiet window start and end must differ")

    @classmethod
    def parse(cls, start: str | None, end: str | None) -> QuietWindow | None:
        """Build a window from ``HH:MM`` strings; ``None`` when either is unset.

        Raises:
            ValueError: If a value is not ``HH:MM`` or both values are equal.
        """
        if not start or not end:
            return None
        return cls(_parse_hhmm(start), _parse_hhmm(end))

    @property
    def crosses_midnight(self) -> bool:
        """Whether the window spans two calendar days."""
        return self.end < self.start

    def contains(self, local: time) -> bool:
        """Whether the wall-clock time ``local`` falls inside the window."""
        local = local.replace(tzinfo=None)
        if self.crosses_midnight:
            return local >= self.start or local < self.end
        return self.start <= local < self.end


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("expected a timezone-aware datetime")


def _local(moment: datetime, tz: tzinfo) -> datetime:
    """``moment`` as a normalized local time of ``tz``.

    Going through UTC matters: ``astimezone`` returns its input untouched when it already
    uses ``tz``, which would keep a non-existent local time or a stale ``fold``.
    """
    return moment.astimezone(UTC).astimezone(tz)


def in_quiet(moment: datetime, window: QuietWindow | None, tz: tzinfo) -> bool:
    """Whether ``moment`` falls inside ``window`` in the local time of ``tz``.

    Args:
        moment: Timezone-aware instant.
        window: Quiet window, or ``None`` (never quiet).
        tz: Timezone the window is expressed in.
    """
    _require_aware(moment)
    if window is None:
        return False
    return window.contains(_local(moment, tz).time())


def quiet_end_after(moment: datetime, window: QuietWindow, tz: tzinfo) -> datetime:
    """The first end of ``window`` strictly after ``moment`` (aware, in ``tz``).

    When ``moment`` is inside the window this is the instant the window closes.
    """
    _require_aware(moment)
    after = moment.astimezone(UTC)  # compare in UTC: same-zone comparisons ignore fold
    local_date = _local(moment, tz).date()
    candidates: list[datetime] = []
    for offset in range(3):  # today, tomorrow, and one spare day for DST oddities
        day = local_date + timedelta(days=offset)
        for fold in (0, 1):
            # fold=1 selects the second occurrence of a repeated (fall-back) local time;
            # converting to UTC maps a non-existent local time to a real instant.
            candidate = datetime.combine(day, window.end, tzinfo=tz).replace(fold=fold)
            instant = candidate.astimezone(UTC)
            # A non-existent end read with fold=1 maps to an instant *before* the jump,
            # which is still inside the window: it is not a real end.
            if instant > after and not window.contains(instant.astimezone(tz).time()):
                candidates.append(instant)
    if not candidates:
        raise AssertionError("unreachable: a daily window always ends within two days")
    return min(candidates).astimezone(tz)


def next_run_at(base: datetime, window: QuietWindow | None, tz: tzinfo) -> datetime:
    """When a refresh due at ``base`` may actually run (aware, in ``tz``).

    A due time inside the quiet window is postponed to the moment the window ends;
    otherwise ``base`` is returned unchanged (converted to ``tz``).
    """
    _require_aware(base)
    if window is not None and in_quiet(base, window, tz):
        return quiet_end_after(base, window, tz)
    return _local(base, tz)
