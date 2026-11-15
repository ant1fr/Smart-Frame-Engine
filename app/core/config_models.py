"""Runtime configuration models (editable from the UI and ``/api/config``).

The effective configuration is resolved by :class:`app.core.config_store.ConfigStore`
with the precedence ``built-in defaults <- environment (.env) <- /data/config.json``.

The schema is intentionally two levels deep (``section.field``) so that overrides,
value sources and resets can be addressed with simple dotted paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

PanelModel = Literal["133", "315"]
Orientation = Literal["portrait", "landscape"]
FitMode = Literal["smart_crop", "contain"]
DitherAlgorithm = Literal["atkinson", "floyd_steinberg"]
EinkProfile = Literal["official", "photo"]
PowerMode = Literal["continuous", "deepsleep"]


@dataclass(frozen=True, slots=True)
class PanelSpec:
    """Physical characteristics of a Fraimic Spectra 6 panel (native portrait)."""

    key: PanelModel
    label: str
    width: int
    height: int
    bin_size: int


PANEL_SPECS: Final[dict[PanelModel, PanelSpec]] = {
    "133": PanelSpec("133", 'Spectra 6 13.3" (EL133UF1)', 1200, 1600, 960_000),
    "315": PanelSpec("315", 'Spectra 6 31.5" (EL315)', 1440, 2560, 2_304_000),
}

_HHMM_RE: Final = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_LANGUAGE_RE: Final = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def _normalize_http_base_url(value: str) -> str:
    """Validate an optional ``http(s)://host[:port][/path]`` base URL.

    An empty string means "not configured". Trailing slashes are removed so that
    API paths can be appended safely.
    """
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("must be an absolute http:// or https:// URL")
    if parts.username or parts.password:
        raise ValueError("must not embed credentials; use the API key field instead")
    if parts.query or parts.fragment:
        raise ValueError("must not contain a query string or fragment")
    return value.rstrip("/")


class _Section(BaseModel):
    """Base class for configuration sections: strict keys, validated assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


ACCOUNT_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _clean_names(value: list[str]) -> list[str]:
    cleaned: list[str] = []
    for name in value:
        stripped = name.strip()
        if stripped and stripped not in cleaned:
            cleaned.append(stripped)
    return cleaned


class ImmichAccountConfig(_Section):
    """One Immich server/account (a family member or a shared library)."""

    url: str = Field(description="Immich base URL, e.g. http://immich.local:2283")
    api_key: SecretStr = Field(description="Immich API key of this account")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        normalized = _normalize_http_base_url(value)
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("api_key")
    @classmethod
    def _validate_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not raw:
            raise ValueError("must not be empty")
        return SecretStr(raw)


class ImmichConfig(_Section):
    """Immich accounts and the default selection filters of the Immich provider."""

    accounts: dict[str, ImmichAccountConfig] = Field(
        default_factory=dict,
        description="Immich accounts by name (IMMICH_URL/IMMICH_API_KEY seed one named 'default')",
    )
    persons: list[str] = Field(default_factory=list, description="Default person names filter")
    always_include: list[str] = Field(
        default_factory=list,
        description="Person names merged into every selection (e.g. children without a "
        "Home Assistant presence), unless a request overrides them",
    )
    match_mode: Literal["any", "all"] = Field(
        default="any", description="Match photos containing any or all of the persons"
    )
    person_aliases: dict[str, dict[str, list[str]]] = Field(
        default_factory=dict,
        description="Canonical person name -> account name -> person IDs or names in that "
        "account (disambiguates duplicate or differently named people)",
    )
    album: str = Field(default="", description="Default album name filter (empty = all)")
    favorites_only: bool = Field(default=False, description="Only pick favorite assets")
    exclude_archived: bool = Field(default=True, description="Skip archived assets")
    anti_repeat_days: int = Field(
        default=30, ge=0, le=3650, description="Do not show the same photo again within N days"
    )
    burst_cooldown_hours: float = Field(
        default=2.0,
        ge=0,
        le=168,
        description="After a photo was shown, skip photos taken within this many hours of it "
        "(rapid-fire shots of one moment); 0 = off",
    )
    burst_cooldown_days: int = Field(
        default=3,
        ge=0,
        le=365,
        description="How long a shown photo blocks others from its burst (days); 0 = off. "
        "Shorter than anti_repeat_days so other photos of the same trip can appear later",
    )
    people_cache_ttl_s: int = Field(
        default=3600, ge=0, le=604_800, description="How long the people list is cached (0 = off)"
    )
    search_candidates: int = Field(
        default=40, ge=5, le=200, description="Candidates fetched per search before picking one"
    )

    @field_validator("accounts")
    @classmethod
    def _validate_account_names(
        cls, value: dict[str, ImmichAccountConfig]
    ) -> dict[str, ImmichAccountConfig]:
        for name in value:
            if not ACCOUNT_NAME_RE.match(name):
                raise ValueError(f"invalid account name {name!r}: use 1-32 letters, digits, - or _")
        return value

    @field_validator("persons", "always_include")
    @classmethod
    def _clean_persons(cls, value: list[str]) -> list[str]:
        return _clean_names(value)

    @field_validator("person_aliases")
    @classmethod
    def _clean_aliases(
        cls, value: dict[str, dict[str, list[str]]]
    ) -> dict[str, dict[str, list[str]]]:
        cleaned: dict[str, dict[str, list[str]]] = {}
        for canonical, per_account in value.items():
            name = canonical.strip()
            if not name:
                raise ValueError("alias names must not be empty")
            targets: dict[str, list[str]] = {}
            for account, entries in per_account.items():
                if not ACCOUNT_NAME_RE.match(account):
                    raise ValueError(f"invalid account name {account!r} in alias {name!r}")
                targets[account] = _clean_names(entries)
            cleaned[name] = targets
        return cleaned


class TmdbConfig(_Section):
    """TMDB (The Movie Database) settings for HD movie and TV posters."""

    api_key: SecretStr = Field(
        default=SecretStr(""), description="TMDB API key (v3) or read access token (v4)"
    )
    language: str = Field(
        default="fr-FR",
        description="Preferred poster/metadata language and region, e.g. fr-FR or en-US "
        "(regional posters first, then same language, textless, English)",
    )
    image_size: Literal["w780", "w1280", "original"] = Field(
        default="w780",
        description='Poster resolution to download (original is sharper on the 31.5" panel)',
    )
    ephemeral_ttl_minutes: int = Field(
        default=180,
        ge=1,
        le=1440,
        description="How long an ephemeral poster stays before the previous image returns",
    )
    hold_minutes: int | None = Field(
        default=None,
        ge=1,
        le=10080,
        description="Protect a pushed poster from scheduled refreshes for this long",
    )
    fallback_title_card: bool = Field(
        default=True,
        description="Render a title card when no poster can be found (else answer 404)",
    )

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        value = value.strip()
        if not _LANGUAGE_RE.match(value):
            raise ValueError("must look like 'en-US' or 'fr-FR'")
        return value


class MusicConfig(_Section):
    """Now-playing album covers (POST /api/display/now-playing)."""

    enrich_artwork: bool = Field(
        default=True,
        description="Look up a high-resolution cover (Deezer, then MusicBrainz / Cover Art "
        "Archive) when none is provided or it cannot be downloaded",
    )
    default_style: Literal["sleeve", "vinyl"] = Field(
        default="sleeve", description="Album template style: sleeve, or vinyl (a record peeks out)"
    )
    default_ttl_minutes: int = Field(
        default=10,
        ge=1,
        le=1440,
        description="Cover lifetime when the request gives no duration or ttl",
    )
    ttl_grace_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="Added to the remaining track time so the cover outlives short gaps",
    )
    deezer_enabled: bool = Field(default=True, description="Search covers on Deezer (no key)")
    musicbrainz_enabled: bool = Field(
        default=True, description="Fall back to MusicBrainz / Cover Art Archive"
    )
    lookup_cache_size: int = Field(
        default=256, ge=0, le=10_000, description="Cover lookups remembered in memory (0 = off)"
    )


class FraimicConfig(_Section):
    """Fraimic frame connection, panel model and power management."""

    host: str = Field(
        default="fraimic.local",
        description="Frame hostname, IP or base URL (http:// is assumed when omitted)",
    )
    power_mode: PowerMode = Field(
        default="continuous",
        description="continuous = always reachable; deepsleep = queue until the frame wakes up",
    )
    panel: PanelModel = Field(default="133", description='Panel model: 133 (13.3") or 315 (31.5")')
    upload_path: str = Field(default="/api/image", description="Frame endpoint receiving .bin")
    status_path: str = Field(default="/api/info", description="Frame endpoint used as ping")
    poll_interval_s: int = Field(
        default=30, ge=5, le=3600, description="Deepsleep: seconds between availability checks"
    )
    request_timeout_s: float = Field(
        default=60.0, gt=0, le=600, description="HTTP timeout for frame requests"
    )

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        candidate = value if "://" in value else f"http://{value}"
        parts = urlsplit(candidate)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("must be a hostname, an IP address or an http(s) base URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("must not contain credentials, a query string or a fragment")
        return value

    @field_validator("upload_path", "status_path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/") or "://" in value or any(c in value for c in "?#"):
            raise ValueError("must be an absolute path such as /api/info")
        return value

    @property
    def base_url(self) -> str:
        """Frame base URL including scheme, or an empty string when unset."""
        if not self.host:
            return ""
        return self.host if "://" in self.host else f"http://{self.host}"

    @property
    def spec(self) -> PanelSpec:
        """Physical specification of the configured panel."""
        return PANEL_SPECS[self.panel]


class DisplayConfig(_Section):
    """How the frame is hung and how images are fitted into it."""

    orientation: Orientation = Field(default="portrait", description="How the frame is hung")
    landscape_rotation: Literal[90, 270] = Field(
        default=90,
        description="Landscape only: degrees (counter-clockwise) to rotate the composed image "
        "into the panel's native portrait buffer; use 270 if the image appears upside down",
    )
    default_fit: FitMode = Field(
        default="smart_crop", description="Fit mode for photos (posters always use contain)"
    )
    contain_background: Literal["blur", "black", "white"] = Field(
        default="blur", description="Padding style used by the contain fit mode"
    )
    safe_margin_px: int = Field(
        default=0,
        ge=0,
        le=250,
        description="Inset (pixels) kept clear on every side so subjects stay out of the "
        "frame's mat/bezel; the margin is filled with the contain background style",
    )


#: Values of the ``photo`` profile, tuned on a real Spectra 6 panel with indoor photos: the
#: official edge-enhance/sharpen chain and +20% saturation/contrast make photos look
#: outlined, over-saturated and grainy. Dithering stays as configured.
PHOTO_PROFILE: Final[dict[str, float | bool]] = {
    "brightness": 1.0,
    "contrast": 1.0,
    "saturation": 0.85,
    "sharpness": 0.8,
    "edge_filters": False,
}


class EinkConfig(_Section):
    """Color eInk optimization. Defaults mirror the official Fraimic converter."""

    profile: EinkProfile = Field(
        default="official",
        description="official = the values below (defaults = official converter); photo = softer "
        "preset for photographs that overrides brightness, contrast, saturation, sharpness "
        "and edge_filters",
    )
    brightness: float = Field(default=1.1, ge=0.1, le=3.0, description="1.0 = unchanged")
    contrast: float = Field(default=1.2, ge=0.1, le=3.0, description="1.0 = unchanged")
    saturation: float = Field(default=1.2, ge=0.0, le=3.0, description="1.0 = unchanged")
    sharpness: float = Field(default=1.0, ge=0.0, le=3.0, description="1.0 = unchanged")
    edge_filters: bool = Field(
        default=True, description="Apply the official edge-enhance / smooth / sharpen chain"
    )
    dither: DitherAlgorithm = Field(
        default="atkinson",
        description="atkinson = tuned color metric (official default); floyd_steinberg = classic",
    )

    def effective(self, profile: EinkProfile | None = None) -> EinkConfig:
        """The values actually used for rendering after applying the profile.

        Args:
            profile: Overrides :attr:`profile` (e.g. a poster source forcing ``official``).
        """
        if (profile or self.profile) == "official":
            return self
        return self.model_copy(update=PHOTO_PROFILE)


class QualityConfig(_Section):
    """Photo suitability gate: skips images a 6-color eInk panel renders badly.

    Thresholds were calibrated on ~400 real photos (thumbnails): they catch blank frames,
    tiny images, screenshots/graphics and document scans while keeping black-and-white
    portraits and night shots. If a whole batch of candidates fails, the gate relaxes
    step by step instead of finding nothing (see ``app.processor.quality``).
    """

    enabled: bool = Field(default=True, description="Check photos before downloading them")
    candidates_checked: int = Field(
        default=8, ge=1, le=50, description="Max candidates inspected per request"
    )
    min_dimension: int = Field(
        default=600, ge=0, le=4000, description="Reject images whose short side is below this (px)"
    )
    blank_luma_mean: float = Field(
        default=15.0, ge=0, le=255, description="Reject near-black frames (mean luma below)"
    )
    blank_luma_std: float = Field(
        default=8.0, ge=0, le=128, description="Reject flat frames (luma std-dev below)"
    )
    screenshot_flat_share: float = Field(
        default=35.0,
        ge=1,
        le=100,
        description="No camera EXIF and this % of pixels one bright color",
    )
    screenshot_flat_luma: float = Field(
        default=128.0, ge=0, le=255, description="Luma above which the flat color counts as bright"
    )
    screenshot_aspect: float = Field(
        default=1.9,
        ge=1,
        le=10,
        description="No camera EXIF and aspect at least this (phone screen)",
    )
    screenshot_max_dimension: int = Field(
        default=1300, ge=0, le=10000, description="... and short side at most this (px)"
    )
    document_colorfulness: float = Field(
        default=15.0, ge=0, le=255, description="No camera EXIF, colorfulness below this ..."
    )
    document_white_share: float = Field(
        default=15.0, ge=0, le=100, description="... and at least this % of near-white pixels"
    )


SourceKind = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
"""Open identifier of a source (``immich``, ``tmdb``...).

Which kinds exist is decided at runtime by the scheduler's runner registry, not here, so
adding a provider never changes the configuration models.
"""


class SourceRule(BaseModel):
    """A replayable source request (``/api/refresh`` and the scheduler).

    ``params`` holds the request body of the matching display endpoint, e.g. for
    ``immich`` the fields of ``POST /api/display/immich`` (``persons``, ``album_name``...).
    """

    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    params: dict[str, JsonValue] = Field(default_factory=dict)


class SmartMixEntry(BaseModel):
    """One weighted entry of the Smart Mix playlist: a :class:`SourceRule` plus a weight.

    ``params`` may contain context placeholders (``{season}``, ``{month_name}``...) that are
    expanded each time the entry runs.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    params: dict[str, JsonValue] = Field(default_factory=dict)
    weight: float = Field(default=1.0, gt=0, le=1000, description="Relative chance of being picked")
    label: str | None = Field(default=None, max_length=80, description="Name shown in the UI")
    enabled: bool = Field(default=True, description="Disabled entries are never picked")

    def to_rule(self) -> SourceRule:
        """The replayable rule this entry wraps."""
        return SourceRule(kind=self.kind, params=dict(self.params))


class SmartMixConfig(_Section):
    """Weighted, source-agnostic playlist used by scheduled refreshes."""

    enabled: bool = Field(
        default=False,
        description="Pick the next scheduled photo from the entries (a pinned "
        "scheduler.source_rule still wins)",
    )
    entries: list[SmartMixEntry] = Field(
        default_factory=list, max_length=50, description="Weighted playlist entries"
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Entries tried per refresh when one has no candidate or fails",
    )
    avoid_consecutive: bool = Field(
        default=True, description="Do not pick the same entry twice in a row when others exist"
    )
    hemisphere: Literal["north", "south"] = Field(
        default="north", description="Used to map months to {season}"
    )


class SchedulerConfig(_Section):
    """Autonomous periodic refresh with an optional quiet-hours window."""

    enabled: bool = Field(default=False, description="Enable autonomous periodic refresh")
    interval_hours: float = Field(
        default=6.0, ge=0.25, le=168, description="Hours between automatic refreshes"
    )
    quiet_hours_start: str | None = Field(
        default=None, description="HH:MM start of the no-refresh window (local time)"
    )
    quiet_hours_end: str | None = Field(
        default=None, description="HH:MM end of the no-refresh window (local time)"
    )
    timezone: str = Field(
        default="UTC", description="IANA timezone of the quiet hours, e.g. Europe/Paris"
    )
    source_rule: SourceRule | None = Field(
        default=None,
        description="Pinned rule replayed on refresh (optional). When empty, the last manual "
        "display request is replayed",
    )
    skip_if_pending: bool = Field(
        default=True,
        description="Skip a scheduled refresh while the previous image still waits for the "
        "frame (deepsleep), so an image the frame never showed is not replaced",
    )
    retry_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="First retry delay after a failed refresh; doubles on each failure, "
        "capped by interval_hours",
    )

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _validate_hhmm(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _HHMM_RE.match(value):
            raise ValueError("must use 24h HH:MM format")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _quiet_hours_pair(self) -> SchedulerConfig:
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValueError("quiet_hours_start and quiet_hours_end must be set together")
        if self.quiet_hours_start is not None and self.quiet_hours_start == self.quiet_hours_end:
            raise ValueError("quiet_hours_start and quiet_hours_end must differ")
        return self


class RuntimeConfig(BaseModel):
    """Complete runtime configuration persisted (sparsely) to ``/data/config.json``."""

    model_config = ConfigDict(extra="forbid")

    immich: ImmichConfig = Field(default_factory=ImmichConfig)
    tmdb: TmdbConfig = Field(default_factory=TmdbConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    fraimic: FraimicConfig = Field(default_factory=FraimicConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    eink: EinkConfig = Field(default_factory=EinkConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    smart_mix: SmartMixConfig = Field(default_factory=SmartMixConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    @property
    def frame_size(self) -> tuple[int, int]:
        """Composition size ``(width, height)`` honoring the frame orientation."""
        spec = self.fraimic.spec
        if self.display.orientation == "landscape":
            return spec.height, spec.width
        return spec.width, spec.height


SECTION_NAMES: Final[tuple[str, ...]] = tuple(RuntimeConfig.model_fields)


def section_model(section: str) -> type[BaseModel]:
    """Return the Pydantic model class of a configuration section.

    Raises:
        KeyError: If ``section`` is not a known section.
    """
    annotation = RuntimeConfig.model_fields[section].annotation
    if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        raise TypeError(f"{section!r} is not a configuration section")
    return annotation


def section_fields(section: str) -> tuple[str, ...]:
    """Return the field names of a configuration section."""
    return tuple(section_model(section).model_fields)


def secret_field_paths() -> tuple[tuple[str, str], ...]:
    """Return ``(section, field)`` pairs whose values are secrets."""
    return tuple(
        (section, name)
        for section in SECTION_NAMES
        for name, field in section_model(section).model_fields.items()
        if field.annotation is SecretStr
    )


def dump_config(config: RuntimeConfig, *, reveal_secrets: bool) -> dict[str, dict[str, Any]]:
    """Serialize a config to plain JSON-compatible data.

    Args:
        config: Configuration to serialize.
        reveal_secrets: When ``False``, secret values are replaced by :func:`mask_secret`.
    """
    data: dict[str, dict[str, Any]] = config.model_dump(mode="json")
    for section, name in secret_field_paths():
        secret: SecretStr = getattr(getattr(config, section), name)
        raw = secret.get_secret_value()
        data[section][name] = raw if reveal_secrets else mask_secret(raw)
    for account_name, account in config.immich.accounts.items():
        raw = account.api_key.get_secret_value()
        data["immich"]["accounts"][account_name]["api_key"] = (
            raw if reveal_secrets else mask_secret(raw)
        )
    return data


def nested_secret_values(config: RuntimeConfig) -> list[str]:
    """Secrets stored inside nested structures (the Immich account API keys)."""
    return [
        raw
        for account in config.immich.accounts.values()
        if (raw := account.api_key.get_secret_value())
    ]


MASK_PREFIX: Final = "•" * 8


def mask_secret(value: str) -> str:
    """Mask a secret for display. Empty stays empty; long keys keep their last 4 chars."""
    if not value:
        return ""
    return MASK_PREFIX + (value[-4:] if len(value) >= 16 else "")


def is_masked(value: object) -> bool:
    """Return ``True`` if ``value`` is a masked placeholder produced by :func:`mask_secret`."""
    return isinstance(value, str) and value.startswith(MASK_PREFIX)
