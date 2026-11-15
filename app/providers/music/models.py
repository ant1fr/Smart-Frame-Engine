"""Now-playing request (Home Assistant ``media_player`` attributes)."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AlbumStyleName = Literal["sleeve", "vinyl"]

MAX_ARTWORK_URL_LENGTH: Final = 4096
#: Upper bound of a base64 cover (the decoded size is checked separately).
MAX_ARTWORK_BASE64_LENGTH: Final = 40_000_000


class NowPlayingRequest(BaseModel):
    """Body of ``POST /api/display/now-playing``."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300, description="Track title (media_title)")
    artist: str = Field(min_length=1, max_length=300, description="Artist (media_artist)")
    album: str | None = Field(
        default=None, max_length=300, description="Album name (media_album_name)"
    )
    artwork_url: str | None = Field(
        default=None,
        max_length=MAX_ARTWORK_URL_LENGTH,
        description="Cover URL, e.g. the absolute URL of the media_player entity_picture",
    )
    artwork_base64: str | None = Field(
        default=None,
        max_length=MAX_ARTWORK_BASE64_LENGTH,
        description="Cover as base64 (or a data: URI); tried before artwork_url",
    )
    enrich_artwork: bool = Field(
        default=True,
        description="When no usable cover was provided, look one up on Deezer, then "
        "MusicBrainz / Cover Art Archive",
    )
    style: AlbumStyleName | None = Field(
        default=None, description="sleeve or vinyl (default music.default_style)"
    )
    duration_seconds: float | None = Field(
        default=None, ge=0, le=86_400, description="Track length (media_duration)"
    )
    position_seconds: float | None = Field(
        default=None, ge=0, le=86_400, description="Playback position (media_position)"
    )
    ttl_seconds: int | None = Field(
        default=None,
        ge=10,
        le=86_400,
        description="Explicit cover lifetime (overrides the duration-based one)",
    )
    hold_minutes: int | None = Field(
        default=None,
        ge=1,
        le=10_080,
        description="Protect the cover from scheduled refreshes for this long",
    )
    push_to_fraimic: bool = Field(
        default=True, description="Send to the frame; false only renders a preview"
    )
    ephemeral: bool = Field(
        default=True,
        description="Temporary cover: the previous image comes back when it expires or on "
        "DELETE /api/queue/ephemeral",
    )

    @field_validator("title", "artist", "album")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @field_validator("title", "artist")
    @classmethod
    def _required(cls, value: str | None) -> str:
        if not value:
            raise ValueError("must not be blank")
        return value
