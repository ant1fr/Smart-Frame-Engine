from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.context import RuleContext, expand_params, expand_text


def ctx(month: int, hemisphere: str = "north", tz: str = "UTC") -> RuleContext:
    return RuleContext(
        datetime(2026, month, 15, 12, 0, tzinfo=UTC),
        ZoneInfo(tz),
        hemisphere,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("month", "north", "south"),
    [
        (1, "winter", "summer"),
        (4, "spring", "autumn"),
        (7, "summer", "winter"),
        (10, "autumn", "spring"),
        (12, "winter", "summer"),
    ],
)
def test_season_by_hemisphere(month: int, north: str, south: str) -> None:
    assert ctx(month).season == north
    assert ctx(month, "south").season == south


def test_placeholders_are_expanded_recursively() -> None:
    params = {
        "clip_queries": ["{season} landscape", "{month_name} {year}"],
        "nested": {"m": "{month}"},
        "n": 3,
        "flag": True,
        "none": None,
    }
    assert expand_params(params, ctx(11)) == {
        "clip_queries": ["autumn landscape", "november 2026"],
        "nested": {"m": "11"},
        "n": 3,
        "flag": True,
        "none": None,
    }


def test_unknown_placeholders_are_left_untouched() -> None:
    assert expand_text("{weather} {season}", ctx(1)) == "{weather} winter"


def test_local_timezone_decides_the_month() -> None:
    late = RuleContext(datetime(2026, 11, 30, 23, 30, tzinfo=UTC), ZoneInfo("Europe/Paris"))
    assert late.season == "winter"  # already Dec 1 in Paris
    assert late.variables["month_name"] == "december"
