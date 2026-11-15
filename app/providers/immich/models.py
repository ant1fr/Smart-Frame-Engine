"""Immich API data (lenient DTOs) and the request model of ``POST /api/display/immich``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.config_models import FitMode

ImmichMode = Literal["random", "album", "favorites", "search"]
MatchMode = Literal["any", "all"]


class _Dto(BaseModel):
    """Immich payloads change between versions: ignore unknown fields, default the rest."""

    model_config = ConfigDict(extra="ignore")


class PersonDto(_Dto):
    id: str
    name: str = ""
    isHidden: bool = False


class AlbumDto(_Dto):
    id: str
    albumName: str = ""
    assetCount: int = 0
    shared: bool = False


class ExifDto(_Dto):
    make: str | None = None
    model: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    orientation: str | None = None
    dateTimeOriginal: str | None = None
    fileSizeInByte: int | None = None


class AssetDto(_Dto):
    id: str
    type: str = "IMAGE"
    originalFileName: str = ""
    originalMimeType: str = ""
    checksum: str | None = None
    isFavorite: bool = False
    isArchived: bool = False
    isTrashed: bool = False
    isEdited: bool = False
    localDateTime: str | None = None
    fileCreatedAt: str | None = None
    width: int | None = None
    height: int | None = None
    exifInfo: ExifDto | None = None
    people: list[PersonDto] = Field(default_factory=list)


class FaceDto(_Dto):
    """A detected face; the box is in pixels of an ``imageWidth`` x ``imageHeight`` frame."""

    imageWidth: int
    imageHeight: int
    boundingBoxX1: float
    boundingBoxY1: float
    boundingBoxX2: float
    boundingBoxY2: float
    person: PersonDto | None = None


def _utc_iso(moment: datetime) -> str:
    """ISO 8601 UTC timestamp (``...Z``) as Immich expects for date filters."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Filters shared by the random and smart search endpoints."""

    person_ids: tuple[str, ...] = ()
    album_ids: tuple[str, ...] = ()
    favorites_only: bool = False
    exclude_archived: bool = True
    taken_after: datetime | None = None
    taken_before: datetime | None = None

    def body(self, size: int) -> dict[str, object]:
        """JSON body for ``/search/random`` and ``/search/smart``."""
        body: dict[str, object] = {"type": "IMAGE", "size": size, "withExif": True}
        if self.person_ids:
            body["personIds"] = list(self.person_ids)
        if self.album_ids:
            body["albumIds"] = list(self.album_ids)
        if self.favorites_only:
            body["isFavorite"] = True
        if self.exclude_archived:
            body["visibility"] = "timeline"
        if self.taken_after is not None:
            body["takenAfter"] = _utc_iso(self.taken_after)
        if self.taken_before is not None:
            body["takenBefore"] = _utc_iso(self.taken_before)
        return body


class AlbumInfo(BaseModel):
    """An album of one account (``GET /api/immich/albums``)."""

    account: str
    id: str
    name: str
    asset_count: int
    shared: bool


class OnThisDay(BaseModel):
    """ "On this day" memories: photos taken around today's date in previous years."""

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(
        default=3, ge=0, le=31, description="Days before and after today's date to include"
    )
    years_back: int = Field(default=10, ge=1, le=20, description="How many past years to search")
    min_years_ago: int = Field(
        default=1, ge=1, le=20, description="Skip the most recent years (1 = exclude this year)"
    )

    @model_validator(mode="after")
    def _check_years(self) -> OnThisDay:
        if self.min_years_ago > self.years_back:
            raise ValueError("min_years_ago must not exceed years_back")
        return self


class ImmichDisplayRequest(BaseModel):
    """Which Immich photo to show (``POST /api/display/immich``).

    Every field except ``mode`` is optional: unset filters fall back to the ``immich``
    section of the runtime configuration. People are plain names; the provider resolves
    them to Immich person IDs per account (see ``immich.person_aliases``).
    """

    model_config = ConfigDict(extra="forbid")

    mode: ImmichMode = Field(
        default="random",
        description="random = any matching photo; album = from an album; "
        "favorites = favorites only; search = CLIP text search",
    )
    persons: list[str] | None = Field(
        default=None, description="Person names (default: immich.persons)"
    )
    always_include: list[str] | None = Field(
        default=None,
        description="Names merged into persons (default: immich.always_include; [] disables)",
    )
    match_mode: MatchMode | None = Field(
        default=None, description="any or all of the persons (default: immich.match_mode)"
    )
    album_name: str | None = Field(default=None, description="Album name (case-insensitive)")
    album_id: str | None = Field(default=None, description="Album ID (alternative to the name)")
    clip_queries: list[str] = Field(
        default_factory=list, description="CLIP search phrases (mode=search)", max_length=10
    )
    accounts: list[str] | None = Field(
        default=None, description="Limit the selection to these account names"
    )
    fit: FitMode | None = Field(default=None, description="Override the default fit mode")
    push_to_fraimic: bool = Field(
        default=True, description="Send the result to the frame (or queue it in deepsleep mode)"
    )
    ignore_history: bool = Field(
        default=False,
        description="Allow photos shown within anti_repeat_days or taken close to one",
    )
    taken_after: AwareDatetime | None = Field(
        default=None, description="Only photos taken at or after this moment (ISO 8601)"
    )
    taken_before: AwareDatetime | None = Field(
        default=None, description="Only photos taken at or before this moment (ISO 8601)"
    )
    on_this_day: OnThisDay | None = Field(
        default=None,
        description="Memories: photos taken around today's date in previous years "
        "(evaluated at display time; exclusive with taken_after/taken_before)",
    )
    ephemeral: bool = Field(
        default=False,
        description="Temporary push: do not make this request the rule replayed by "
        "POST /api/refresh when nothing is pinned",
    )
    hold_minutes: int | None = Field(
        default=None,
        ge=1,
        le=10080,
        description="Keep this image up for at least this long before a scheduled refresh",
    )
    skip_quality_gate: bool | None = Field(
        default=None,
        description="Skip the photo suitability check. Default: skipped in album mode "
        "(you picked the album yourself), checked otherwise",
    )

    @model_validator(mode="after")
    def _check_mode(self) -> ImmichDisplayRequest:
        self.clip_queries = [query.strip() for query in self.clip_queries if query.strip()]
        if self.mode == "search" and not self.clip_queries:
            raise ValueError("mode 'search' needs at least one clip_queries entry")
        if self.on_this_day is not None and (self.taken_after or self.taken_before):
            raise ValueError("on_this_day cannot be combined with taken_after/taken_before")
        if self.taken_after and self.taken_before and self.taken_after >= self.taken_before:
            raise ValueError("taken_after must be earlier than taken_before")
        return self
