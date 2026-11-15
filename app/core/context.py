"""Runtime context for source rules: ``{season}``, ``{month}``... placeholders.

Stored rules stay templated (e.g. ``"clip_queries": ["{season} landscape"]``) and are expanded
right before they run, so one rule follows the calendar without hard-coded album IDs.
Unknown placeholders are left untouched rather than failing the refresh.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pydantic import JsonValue

Hemisphere = Literal["north", "south"]
Season = Literal["spring", "summer", "autumn", "winter"]

_NORTH: Final[dict[int, Season]] = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}  # fmt: skip
_OPPOSITE: Final[dict[Season, Season]] = {
    "winter": "summer",
    "summer": "winter",
    "spring": "autumn",
    "autumn": "spring",
}


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Calendar facts for one evaluation moment (meteorological seasons)."""

    now: datetime
    tz: ZoneInfo
    hemisphere: Hemisphere = "north"

    @property
    def local(self) -> datetime:
        return self.now.astimezone(self.tz)

    @property
    def season(self) -> Season:
        season = _NORTH[self.local.month]
        return season if self.hemisphere == "north" else _OPPOSITE[season]

    @property
    def variables(self) -> dict[str, str]:
        local = self.local
        return {
            "season": self.season,
            "month": f"{local.month:02d}",
            "month_name": calendar.month_name[local.month].lower(),
            "year": str(local.year),
        }


def expand_text(text: str, context: RuleContext) -> str:
    """Replace the known ``{placeholders}`` in one string."""
    for key, value in context.variables.items():
        text = text.replace("{" + key + "}", value)
    return text


def expand_params(value: JsonValue, context: RuleContext) -> JsonValue:
    """Recursively expand placeholders in every string of a rule's ``params``."""
    if isinstance(value, str):
        return expand_text(value, context)
    if isinstance(value, list):
        return [expand_params(item, context) for item in value]
    if isinstance(value, dict):
        return {key: expand_params(item, context) for key, item in value.items()}
    return value
