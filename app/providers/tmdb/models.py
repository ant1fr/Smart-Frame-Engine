"""TMDB API payloads and the poster request."""

from __future__ import annotations

import re
from datetime import date
from typing import Final, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

TmdbMediaType = Literal["movie", "tv", "multi"]
ResultMediaType = Literal["movie", "tv"]

_LANGUAGE_RE: Final = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def validate_language(value: str | None) -> str | None:
    """Accept ``fr``, ``fr-FR``... (``None`` = use the configured language)."""
    if value is None:
        return None
    value = value.strip()
    if not _LANGUAGE_RE.match(value):
        raise ValueError("must look like 'fr-FR', 'en-US' or 'fr'")
    return value


class TmdbRequest(BaseModel):
    """What to look up on TMDB."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    query: str = Field(min_length=1, max_length=200, description="Movie or series title")
    media_type: TmdbMediaType = Field(
        default="multi", description="movie, tv, or multi (both; 'auto' is accepted)"
    )
    year: int | None = Field(default=None, ge=1870, le=2200, description="Release year")
    season: int | None = Field(
        default=None, ge=0, le=100, description="TV only: prefer this season's poster"
    )
    language: str | None = Field(
        default=None, description="Overrides tmdb.language for this request, e.g. fr-CA"
    )

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("media_type", mode="before")
    @classmethod
    def _auto_is_multi(cls, value: object) -> object:
        return "multi" if value == "auto" else value

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str | None) -> str | None:
        return validate_language(value)

    @model_validator(mode="after")
    def _season_needs_tv(self) -> TmdbRequest:
        if self.season is not None and self.media_type == "movie":
            raise ValueError("season only applies to media_type 'tv' (or 'multi')")
        return self


class SearchResult(BaseModel):
    """One movie or series from a TMDB search."""

    model_config = ConfigDict(extra="ignore")

    id: int
    media_type: ResultMediaType
    title: str
    original_title: str | None = None
    release_date: str | None = None
    poster_path: str | None = None
    popularity: float = 0.0

    @property
    def year(self) -> int | None:
        """Release (or first air) year, if known."""
        if not self.release_date:
            return None
        try:
            return date.fromisoformat(self.release_date[:10]).year
        except ValueError:
            return None


class TmdbImage(BaseModel):
    """One poster of a movie, series or season.

    ``iso_3166_1`` (region) is optional: older payloads only carry the language.
    """

    model_config = ConfigDict(extra="ignore")

    file_path: str
    iso_639_1: str | None = None
    iso_3166_1: str | None = None
    vote_average: float = 0.0
    vote_count: int = 0
    width: int = 0
    height: int = 0

    @field_validator("iso_639_1", "iso_3166_1", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


MAX_FALLBACK_URL_LENGTH: Final = 4096
#: Upper bound of a base64 fallback picture (the decoded size is checked separately).
MAX_FALLBACK_BASE64_LENGTH: Final = 40_000_000


class TmdbDisplayRequest(TmdbRequest):
    """Body of ``POST /api/display/tmdb`` (and of replayable ``tmdb`` rules)."""

    query: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("query", "title"),
        description="Movie or series title ('title' is accepted as an alias)",
    )
    fallback_url: str | None = Field(
        default=None,
        max_length=MAX_FALLBACK_URL_LENGTH,
        description="Picture used when TMDB has no match, e.g. the absolute URL of the Home "
        "Assistant media_player entity_picture",
    )
    fallback_base64: str | None = Field(
        default=None,
        max_length=MAX_FALLBACK_BASE64_LENGTH,
        description="Same as fallback_url, as base64 (or a data: URI); tried first",
    )
    push_to_fraimic: bool = Field(
        default=True, description="Send to the frame; false only renders a preview"
    )
    ephemeral: bool = Field(
        default=True,
        description="Temporary poster: the previous image comes back after ttl_minutes or "
        "DELETE /api/queue/ephemeral, and the request is not replayed by POST /api/refresh",
    )
    ttl_minutes: int | None = Field(
        default=None,
        ge=1,
        le=1440,
        description="Ephemeral lifetime (default tmdb.ephemeral_ttl_minutes)",
    )
    hold_minutes: int | None = Field(
        default=None,
        ge=1,
        le=10080,
        description="Protect the poster from scheduled refreshes (default tmdb.hold_minutes)",
    )

    def search(self) -> TmdbRequest:
        """The TMDB lookup part of the request."""
        return TmdbRequest(
            query=self.query,
            media_type=self.media_type,
            year=self.year,
            season=self.season,
            language=self.language,
        )
