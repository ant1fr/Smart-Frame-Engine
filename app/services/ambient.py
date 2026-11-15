"""Ambient context: short-lived signals (presence, weather, season...) that shape the mix.

Home Assistant (or anything else) posts facts such as "Alice is home", "it is rainy" or
"guests are over" to ``POST /api/context``. Every field expires on its own (``ttl_minutes``)
and the live fields are stored in ``scheduler_state.json``, so they survive a restart.

Effects:

* ``family_mode`` overrides ``family.enabled`` while live;
* ``presence`` and ``boost_people`` add an Immich candidate showing any of those people;
* ``boost_tags`` and ``weather`` (through ``ambient.weather_queries``) add CLIP search
  candidates;
* ``season`` replaces the calendar season in ``{season}`` placeholders.

The extra candidates join the Smart Mix pool with ``ambient.boost_weight``; they only matter
while the Smart Mix is the active rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.core.config_models import AmbientConfig, SourceRule
from app.services.smart_mix import MixCandidate

Season = Literal["spring", "summer", "autumn", "winter"]
MAX_ITEMS: Final = 20
MAX_MINUTES: Final = 10080  # one week


def _clean(values: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        stripped = value.strip()
        if stripped:
            seen.setdefault(stripped.casefold(), stripped)
    return list(seen.values())


class TimedList(BaseModel):
    """A list of names or phrases with an expiry."""

    model_config = ConfigDict(extra="forbid")

    values: list[str] = Field(max_length=MAX_ITEMS)
    expires_at: AwareDatetime

    _normalize = field_validator("values")(_clean)


class TimedText(BaseModel):
    """One word or phrase with an expiry."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=80)
    expires_at: AwareDatetime


class TimedFlag(BaseModel):
    """A boolean with an expiry."""

    model_config = ConfigDict(extra="forbid")

    value: bool
    expires_at: AwareDatetime


class AmbientState(BaseModel):
    """The live ambient fields (``None`` = not set)."""

    model_config = ConfigDict(extra="ignore")

    presence: TimedList | None = None
    family_mode: TimedFlag | None = None
    weather: TimedText | None = None
    season: TimedText | None = None
    boost_tags: TimedList | None = None
    boost_people: TimedList | None = None

    def live(self, now: datetime) -> AmbientState:
        """A copy without the expired fields."""
        update = {
            name: None
            for name in AMBIENT_FIELDS
            if (entry := getattr(self, name)) is not None and entry.expires_at <= now
        }
        return self.model_copy(update=update)

    @property
    def empty(self) -> bool:
        """Whether no field is set."""
        return all(getattr(self, name) is None for name in AMBIENT_FIELDS)

    def season_override(self) -> Season | None:
        """The overriding season, if one is set and valid."""
        if self.season is not None and self.season.value in SEASONS:
            return self.season.value  # type: ignore[return-value]
        return None


AMBIENT_FIELDS: Final = tuple(AmbientState.model_fields)
SEASONS: Final = ("spring", "summer", "autumn", "winter")


class ListInput(BaseModel):
    """``{"values": [...], "ttl_minutes": 30}``: a list with its own lifetime."""

    model_config = ConfigDict(extra="forbid")

    values: list[str] = Field(max_length=MAX_ITEMS)
    ttl_minutes: int | None = Field(default=None, ge=1, le=MAX_MINUTES)


class TextInput(BaseModel):
    """``{"value": "rainy", "ttl_minutes": 30}``."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(max_length=80)
    ttl_minutes: int | None = Field(default=None, ge=1, le=MAX_MINUTES)


class FlagInput(BaseModel):
    """``{"value": true, "ttl_minutes": 30}``."""

    model_config = ConfigDict(extra="forbid")

    value: bool
    ttl_minutes: int | None = Field(default=None, ge=1, le=MAX_MINUTES)


def _fresh_list(given: ListInput | list[str], default_ttl: int, now: datetime) -> TimedList | None:
    values, ttl = (
        (given.values, given.ttl_minutes) if isinstance(given, ListInput) else (given, None)
    )
    cleaned = _clean(values)
    if not cleaned:
        return None  # an empty list clears the field
    return TimedList(values=cleaned, expires_at=now + timedelta(minutes=ttl or default_ttl))


def _fresh_text(given: TextInput | str, default_ttl: int, now: datetime) -> TimedText | None:
    value, ttl = (given.value, given.ttl_minutes) if isinstance(given, TextInput) else (given, None)
    value = value.strip()
    if not value:
        return None
    return TimedText(value=value, expires_at=now + timedelta(minutes=ttl or default_ttl))


class AmbientRequest(BaseModel):
    """Body of ``POST /api/context``; unset fields keep their value, empty ones clear it.

    Each field is either the bare value or an object with its own ``ttl_minutes``;
    ``ttl_minutes`` at the top level is the default for fields without one.
    """

    model_config = ConfigDict(extra="forbid")

    presence: ListInput | list[str] | None = Field(
        default=None, description="People at home (Immich names): their photos are boosted"
    )
    family_mode: FlagInput | bool | None = Field(
        default=None, description="Temporarily forces Family Mode on or off"
    )
    weather: TextInput | str | None = Field(
        default=None, description="Keyword of ambient.weather_queries, e.g. rainy"
    )
    season: TextInput | Season | None = Field(
        default=None, description="Replaces the calendar season in {season} placeholders"
    )
    boost_tags: ListInput | list[str] | None = Field(
        default=None, description="CLIP search phrases to favour (e.g. beach, birthday)"
    )
    boost_people: ListInput | list[str] | None = Field(
        default=None, description="People (Immich names) to favour"
    )
    ttl_minutes: int | None = Field(
        default=None, ge=1, le=MAX_MINUTES, description="Default lifetime of the fields below"
    )

    def apply(self, state: AmbientState, config: AmbientConfig, now: datetime) -> AmbientState:
        """``state`` updated with the fields of this request.

        Raises:
            ValueError: Unknown weather keyword or season.
        """
        ttl = self.ttl_minutes or config.default_ttl_minutes
        update: dict[str, TimedList | TimedText | TimedFlag | None] = {}
        for name in ("presence", "boost_tags", "boost_people"):
            given = getattr(self, name)
            if given is not None:
                update[name] = _fresh_list(given, ttl, now)
        if self.family_mode is not None:
            flag = self.family_mode
            value, own = (
                (flag.value, flag.ttl_minutes) if isinstance(flag, FlagInput) else (flag, None)
            )
            update["family_mode"] = TimedFlag(
                value=value, expires_at=now + timedelta(minutes=own or ttl)
            )
        if self.weather is not None:
            weather = _fresh_text(self.weather, ttl, now)
            if weather is not None:
                weather = weather.model_copy(update={"value": weather.value.casefold()})
                if weather.value not in config.weather_queries:
                    known = ", ".join(sorted(config.weather_queries))
                    raise ValueError(f"Unknown weather {weather.value!r}; configured: {known}")
            update["weather"] = weather
        if self.season is not None:
            season = _fresh_text(self.season, ttl, now)
            if season is not None:
                season = season.model_copy(update={"value": season.value.casefold()})
                if season.value not in SEASONS:
                    raise ValueError(f"Unknown season {season.value!r}; use {', '.join(SEASONS)}")
            update["season"] = season
        return state.model_copy(update=update)


def ambient_candidates(state: AmbientState, config: AmbientConfig) -> list[MixCandidate]:
    """Smart Mix candidates implied by the live ambient fields."""
    weight = config.boost_weight
    candidates: list[MixCandidate] = []
    people = _clean(
        [
            *(state.presence.values if state.presence else []),
            *(state.boost_people.values if state.boost_people else []),
        ]
    )
    if people:
        rule = SourceRule(
            kind="immich",
            params={
                "mode": "random",
                "persons": list(people),
                "always_include": [],
                "match_mode": "any",
            },
        )
        candidates.append(MixCandidate(rule, weight, None, "ambient:people"))
    if state.boost_tags is not None and state.boost_tags.values:
        rule = SourceRule(
            kind="immich",
            params={"mode": "search", "clip_queries": list(state.boost_tags.values)},
        )
        candidates.append(MixCandidate(rule, weight, None, "ambient:tags"))
    if state.weather is not None:
        phrase = config.weather_queries.get(state.weather.value)
        if phrase:
            rule = SourceRule(kind="immich", params={"mode": "search", "clip_queries": [phrase]})
            candidates.append(
                MixCandidate(rule, weight, None, f"ambient:weather:{state.weather.value}")
            )
    return candidates
