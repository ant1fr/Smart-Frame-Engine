"""Runtime configuration store with sparse JSON persistence.

Resolution order (later wins)::

    built-in defaults  <-  environment seeds (.env)  <-  /data/config.json overrides

``config.json`` only contains values the user explicitly changed, addressed as
``{"section": {"field": value}}``. Fields that were never saved keep following the
environment (or the built-in defaults), and ``reset`` removes overrides so the value
falls back again.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from app.core.config_models import (
    SECTION_NAMES,
    RuntimeConfig,
    dump_config,
    is_masked,
    nested_secret_values,
    secret_field_paths,
    section_fields,
)
from app.core.json_store import atomic_write_json, read_json

logger = logging.getLogger(__name__)

Overrides = dict[str, dict[str, Any]]
ValueSource = Literal["default", "env", "config"]
ConfigListener = Callable[[RuntimeConfig], Awaitable[None]]

_MAX_PRUNE_PASSES = 32


class ConfigError(ValueError):
    """Base class for configuration errors that are safe to report to API clients."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors: list[dict[str, Any]] = errors or []


class ConfigValidationError(ConfigError):
    """The resulting configuration failed validation. ``errors`` never echoes input values."""

    @classmethod
    def from_pydantic(cls, exc: ValidationError) -> ConfigValidationError:
        errors = [
            {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        return cls("Invalid configuration", errors)


class UnknownConfigPathError(ConfigError):
    """A patch or reset referenced a section or field that does not exist."""


def _merge(*layers: Mapping[str, Mapping[str, Any]]) -> Overrides:
    """Merge ``section -> field -> value`` layers; later layers win per field."""
    merged: Overrides = {}
    for layer in layers:
        for section, fields in layer.items():
            target = merged.setdefault(section, {})
            for name, value in fields.items():
                target[name] = copy.deepcopy(value)
    return merged


def _parse_path(path: str) -> tuple[str, str | None]:
    """Split ``"section"`` or ``"section.field"`` and check it exists."""
    section, _, name = path.strip().partition(".")
    if section not in SECTION_NAMES:
        raise UnknownConfigPathError(f"Unknown configuration section: {section!r}")
    if name and name not in section_fields(section):
        raise UnknownConfigPathError(f"Unknown configuration field: {path!r}")
    return section, name or None


class ConfigStore:
    """Holds the effective :class:`RuntimeConfig` and persists user overrides."""

    def __init__(self, path: Path, env_seed: Mapping[str, Mapping[str, Any]]) -> None:
        """Create the store and validate built-in defaults merged with ``env_seed``.

        Args:
            path: Location of ``config.json``.
            env_seed: Environment fallbacks (see :meth:`EnvSettings.runtime_seed`).

        Raises:
            ConfigValidationError: If an environment variable holds an invalid value.
        """
        self._path = path
        self._defaults: Overrides = dump_config(RuntimeConfig(), reveal_secrets=True)
        self._env_seed: Overrides = _merge(env_seed)
        self._overrides: Overrides = {}
        self._lock = asyncio.Lock()
        self._listeners: list[ConfigListener] = []
        try:
            self._config = self._build({})
        except ConfigValidationError as exc:
            details = "; ".join(f"{'.'.join(e['loc'])}: {e['msg']}" for e in exc.errors)
            raise ConfigValidationError(
                f"Invalid configuration from environment variables: {details}", exc.errors
            ) from exc

    # --- Read access -----------------------------------------------------------------
    @property
    def path(self) -> Path:
        """Location of the overrides file."""
        return self._path

    @property
    def config(self) -> RuntimeConfig:
        """The current effective configuration."""
        return self._config

    @property
    def overrides(self) -> Overrides:
        """A copy of the values persisted in ``config.json``."""
        return copy.deepcopy(self._overrides)

    def sources(self) -> dict[str, ValueSource]:
        """Where each ``section.field`` value currently comes from."""
        result: dict[str, ValueSource] = {}
        for section in SECTION_NAMES:
            for name in section_fields(section):
                if name in self._overrides.get(section, {}):
                    result[f"{section}.{name}"] = "config"
                elif name in self._env_seed.get(section, {}):
                    result[f"{section}.{name}"] = "env"
                else:
                    result[f"{section}.{name}"] = "default"
        return result

    def public_view(self) -> dict[str, Any]:
        """Configuration safe to send to clients (secrets masked) plus value sources."""
        return {
            "config": dump_config(self._config, reveal_secrets=False),
            "sources": self.sources(),
        }

    def secret_values(self) -> list[str]:
        """Every non-empty secret currently in the effective configuration."""
        values: list[str] = []
        for section, name in secret_field_paths():
            raw = getattr(getattr(self._config, section), name).get_secret_value()
            if raw:
                values.append(raw)
        values.extend(nested_secret_values(self._config))
        return values

    def add_listener(self, listener: ConfigListener) -> None:
        """Register a coroutine called with the new config after every change."""
        self._listeners.append(listener)

    # --- Lifecycle ---------------------------------------------------------------------
    def load(self) -> RuntimeConfig:
        """Load ``config.json`` from disk, dropping unknown or invalid entries.

        Invalid entries (e.g. left over from an older version) are logged and ignored in
        memory; the file is rewritten without them on the next successful save.
        """
        raw = self._migrate_legacy(read_json(self._path, dict))
        overrides = self._sanitize_shape(raw)
        overrides = self._prune_invalid(overrides)
        self._overrides = overrides
        self._config = self._build(overrides)
        logger.info(
            "Loaded runtime configuration (%d saved value(s)) from %s",
            sum(len(fields) for fields in overrides.values()),
            self._path,
        )
        return self._config

    # --- Mutations ---------------------------------------------------------------------
    async def update(self, patch: Mapping[str, Any]) -> RuntimeConfig:
        """Validate and persist a partial update ``{"section": {"field": value}}``.

        * Masked secret placeholders (as returned by :meth:`public_view`) mean "unchanged".
        * A value equal to the current effective value is only stored if the field is
          already saved, so submitting a full settings form does not freeze fields that
          currently follow the environment.
        * Nothing is written when validation fails.

        Raises:
            UnknownConfigPathError: If the patch references unknown sections or fields.
            ConfigValidationError: If the resulting configuration is invalid.
        """
        normalized = self._normalize_patch(patch)
        async with self._lock:
            self._restore_masked_account_keys(normalized)
            candidate = _merge(self._overrides, normalized)
            config = self._build(candidate)
            canonical = dump_config(config, reveal_secrets=True)
            current = dump_config(self._config, reveal_secrets=True)

            new_overrides = copy.deepcopy(self._overrides)
            for section, fields in normalized.items():
                for name in fields:
                    already_saved = name in self._overrides.get(section, {})
                    if not already_saved and canonical[section][name] == current[section][name]:
                        continue
                    new_overrides.setdefault(section, {})[name] = canonical[section][name]
            await asyncio.to_thread(self._persist, new_overrides)
            self._overrides = new_overrides
            self._config = config
        await self._notify(config)
        return config

    async def reset(self, paths: Iterable[str] | None = None) -> RuntimeConfig:
        """Remove saved overrides so values fall back to the environment or defaults.

        Args:
            paths: ``"section"`` or ``"section.field"`` entries; ``None`` resets everything.

        Raises:
            UnknownConfigPathError: If a path does not exist.
            ConfigValidationError: If the fallback values form an invalid configuration
                (e.g. resetting only one half of a quiet-hours window).
        """
        targets = None if paths is None else [_parse_path(path) for path in paths]
        async with self._lock:
            new_overrides: Overrides = {}
            if targets is not None:
                new_overrides = copy.deepcopy(self._overrides)
                for section, name in targets:
                    if name is None:
                        new_overrides.pop(section, None)
                    else:
                        new_overrides.get(section, {}).pop(name, None)
                new_overrides = {s: f for s, f in new_overrides.items() if f}
            config = self._build(new_overrides)
            await asyncio.to_thread(self._persist, new_overrides)
            self._overrides = new_overrides
            self._config = config
        await self._notify(config)
        return config

    # --- Internals -----------------------------------------------------------------------
    def _build(self, overrides: Mapping[str, Mapping[str, Any]]) -> RuntimeConfig:
        merged = _merge(self._defaults, self._env_seed, overrides)
        try:
            return RuntimeConfig.model_validate(merged)
        except ValidationError as exc:
            raise ConfigValidationError.from_pydantic(exc) from None

    def _persist(self, overrides: Overrides) -> None:
        atomic_write_json(self._path, overrides, file_mode=0o600)

    async def _notify(self, config: RuntimeConfig) -> None:
        for listener in list(self._listeners):
            try:
                await listener(config)
            except Exception:
                logger.exception("Configuration listener %r failed", listener)

    def _restore_masked_account_keys(self, normalized: Overrides) -> None:
        """Let a masked or omitted account ``api_key`` keep the stored key of that account.

        ``immich.accounts`` is replaced as a whole, so a client that sends the masked view
        back (or omits the key) must not wipe the secrets. A new account without a real
        key is left without one and fails validation.
        """
        accounts = normalized.get("immich", {}).get("accounts")
        if not isinstance(accounts, dict):
            return
        for name, account in accounts.items():
            if not isinstance(account, dict):
                continue
            key = account.get("api_key")
            if key is not None and not is_masked(key):
                continue
            account.pop("api_key", None)
            existing = self._config.immich.accounts.get(name)
            if existing is not None:
                account["api_key"] = existing.api_key.get_secret_value()

    @staticmethod
    def _migrate_legacy(raw: Any) -> Any:
        """Move the pre-accounts ``immich.url``/``api_key`` pair into ``accounts.default``."""
        if not isinstance(raw, dict) or not isinstance(raw.get("immich"), dict):
            return raw
        section = dict(raw["immich"])
        url, key = section.pop("url", None), section.pop("api_key", None)
        if url is None and key is None:
            return raw
        if url and key and "accounts" not in section:
            section["accounts"] = {"default": {"url": url, "api_key": key}}
            logger.info("Migrated saved immich.url/api_key to immich.accounts.default")
        else:
            logger.warning("Ignoring incomplete legacy immich.url/api_key settings")
        return {**raw, "immich": section}

    @staticmethod
    def _normalize_patch(patch: Mapping[str, Any]) -> Overrides:
        if not isinstance(patch, Mapping):
            raise UnknownConfigPathError("Patch must be an object of sections")
        normalized: Overrides = {}
        for section, fields in patch.items():
            _parse_path(section)
            if not isinstance(fields, Mapping):
                raise UnknownConfigPathError(f"Section {section!r} must be an object of fields")
            for name, value in fields.items():
                _parse_path(f"{section}.{name}")
                if is_masked(value):
                    continue
                normalized.setdefault(section, {})[name] = copy.deepcopy(value)
        return normalized

    def _sanitize_shape(self, raw: Any) -> Overrides:
        if not isinstance(raw, dict):
            logger.warning("Ignoring %s: expected a JSON object", self._path)
            return {}
        overrides: Overrides = {}
        for section, fields in raw.items():
            if section not in SECTION_NAMES or not isinstance(fields, dict):
                logger.warning("Ignoring unknown configuration section %r", section)
                continue
            known = section_fields(section)
            for name, value in fields.items():
                if name not in known:
                    logger.warning("Ignoring unknown configuration field %s.%s", section, name)
                    continue
                overrides.setdefault(section, {})[name] = value
        return overrides

    def _prune_invalid(self, overrides: Overrides) -> Overrides:
        pruned = copy.deepcopy(overrides)
        for _ in range(_MAX_PRUNE_PASSES):
            try:
                self._build(pruned)
                return pruned
            except ConfigValidationError as exc:
                removed = False
                for error in exc.errors:
                    loc = error["loc"]
                    section = loc[0] if loc else None
                    if section not in pruned:
                        continue
                    if len(loc) >= 2 and loc[1] in pruned[section]:
                        del pruned[section][loc[1]]
                        logger.warning("Ignoring invalid saved value %s.%s", section, loc[1])
                    else:
                        del pruned[section]
                        logger.warning("Ignoring invalid saved section %r", section)
                    removed = True
                pruned = {s: f for s, f in pruned.items() if f}
                if not removed:
                    raise
        return {}
