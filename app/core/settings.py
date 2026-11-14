"""Environment settings (``.env`` / container environment).

Two kinds of values live here:

* **Infrastructure settings** (data directory, auth key, reverse proxy, CORS, limits).
  They are read once at startup, are never editable through the API, and require a
  restart to change. ``APP_API_KEY`` in particular can never be changed by the API it
  protects.
* **Runtime seeds** (``IMMICH_URL``, ``TMDB_API_KEY``, ``FRAIMIC_HOST``...). They provide
  fallback values for :class:`app.core.config_models.RuntimeConfig`; anything saved to
  ``/data/config.json`` from the UI or API takes precedence over them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class EnvSettings(BaseSettings):
    """Process-level settings loaded from environment variables and an optional ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Infrastructure -------------------------------------------------------
    data_dir: Path = Field(default=Path("/data"), description="Persistent data directory")
    host: str = Field(default="0.0.0.0", description="Bind address")  # noqa: S104
    port: int = Field(default=8000, ge=1, le=65535, description="Bind port")
    log_level: LogLevel = Field(default="INFO", description="Python log level")

    app_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="When set, mutating routes and /api/config require X-API-Key",
    )
    public_image_endpoints: bool = Field(
        default=True,
        description="Serve /api/current.jpg and /api/current.bin without an API key",
    )

    root_path: str = Field(default="", description="Sub-path when behind a reverse proxy")
    cors_origins: str = Field(default="*", description="Comma-separated allowed CORS origins")
    forwarded_allow_ips: str = Field(
        default="*", description="Proxy IPs trusted for X-Forwarded-* headers"
    )

    max_upload_mb: int = Field(default=20, ge=1, le=200, description="Max image size in MB")
    block_private_urls: bool = Field(
        default=False,
        description="Reject external URLs resolving to private/loopback/link-local addresses",
    )
    url_allowlist: str = Field(
        default="",
        description="Comma-separated hosts or CIDRs exempt from BLOCK_PRIVATE_URLS",
    )

    # --- Runtime seeds (fallbacks for config.json) ----------------------------
    immich_url: str | None = None
    immich_api_key: SecretStr | None = None
    tmdb_api_key: SecretStr | None = None
    tmdb_language: str | None = None
    fraimic_host: str | None = None
    fraimic_power_mode: str | None = None
    fraimic_panel: str | None = None
    orientation: str | None = None
    scheduler_enabled: bool | None = None
    refresh_interval_hours: float | None = None
    tz: str | None = None

    @field_validator("root_path")
    @classmethod
    def _normalize_root_path(cls, value: str) -> str:
        value = value.strip().strip("/")
        return f"/{value}" if value else ""

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator(
        "immich_url",
        "tmdb_language",
        "fraimic_host",
        "fraimic_power_mode",
        "fraimic_panel",
        "orientation",
        "tz",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("immich_api_key", "tmdb_api_key", mode="before")
    @classmethod
    def _blank_secret_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("scheduler_enabled", "refresh_interval_hours", mode="before")
    @classmethod
    def _blank_scalar_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- Derived values ----------------------------------------------------------
    @property
    def api_key(self) -> str:
        """The configured API key (empty string when authentication is disabled)."""
        return self.app_api_key.get_secret_value().strip()

    @property
    def auth_enabled(self) -> bool:
        """Whether API key authentication is enforced."""
        return bool(self.api_key)

    @property
    def cors_origin_list(self) -> list[str]:
        """Parsed ``CORS_ORIGINS`` (``["*"]`` when empty)."""
        return _split_csv(self.cors_origins) or ["*"]

    @property
    def url_allowlist_entries(self) -> list[str]:
        """Parsed ``URL_ALLOWLIST`` entries."""
        return _split_csv(self.url_allowlist)

    @property
    def max_upload_bytes(self) -> int:
        """Upload/download size limit in bytes."""
        return self.max_upload_mb * 1024 * 1024

    @property
    def config_path(self) -> Path:
        """Location of the runtime configuration overrides file."""
        return self.data_dir / "config.json"

    @property
    def history_path(self) -> Path:
        """Location of the display history file."""
        return self.data_dir / "history.json"

    @property
    def cache_dir(self) -> Path:
        """Directory holding the current rendered image (``current.jpg``, ``current.bin``)."""
        return self.data_dir / "cache"

    @property
    def queue_dir(self) -> Path:
        """Directory holding the upload waiting for the frame (``pending.bin``)."""
        return self.data_dir / "queue"

    def secret_values(self) -> list[str]:
        """All secret values provided through the environment (for log masking)."""
        values = [self.api_key]
        for secret in (self.immich_api_key, self.tmdb_api_key):
            if secret is not None:
                values.append(secret.get_secret_value())
        return [value for value in values if value]

    def runtime_seed(self) -> dict[str, dict[str, Any]]:
        """Environment-provided fallbacks shaped like ``RuntimeConfig`` overrides.

        Only variables that are actually set are included, so unset variables fall back
        to the built-in defaults.
        """
        candidates: dict[tuple[str, str], Any] = {
            ("tmdb", "api_key"): (
                self.tmdb_api_key.get_secret_value() if self.tmdb_api_key else None
            ),
            ("tmdb", "language"): self.tmdb_language,
            ("fraimic", "host"): self.fraimic_host,
            ("fraimic", "power_mode"): self.fraimic_power_mode,
            ("fraimic", "panel"): self.fraimic_panel,
            ("display", "orientation"): self.orientation,
            ("scheduler", "enabled"): self.scheduler_enabled,
            ("scheduler", "interval_hours"): self.refresh_interval_hours,
            ("scheduler", "timezone"): self.tz,
        }
        seed: dict[str, dict[str, Any]] = {}
        for (section, name), value in candidates.items():
            if value is not None:
                seed.setdefault(section, {})[name] = value
        if self.immich_url and self.immich_api_key:
            seed["immich"] = {
                "accounts": {
                    "default": {
                        "url": self.immich_url,
                        "api_key": self.immich_api_key.get_secret_value(),
                    }
                }
            }
        return seed


@lru_cache(maxsize=1)
def get_env_settings() -> EnvSettings:
    """Return the process-wide :class:`EnvSettings` (cached after the first call)."""
    return EnvSettings()
