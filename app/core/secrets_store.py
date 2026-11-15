"""Generated secrets persisted in ``/data/secrets.json``: the API key and the session key.

* **API key.** ``APP_API_KEY`` from the environment always wins and can never be changed
  through the API it protects. Without it, a key is generated and stored here as soon as an
  admin password is configured (secure by default: Home Assistant and the frame need a key
  once the UI is locked), and an authenticated admin can regenerate it.
* **Session key.** ``SESSION_SECRET`` from the environment, else a random key generated once
  and stored here, so web UI sessions survive restarts.

The file is written atomically and readable by its owner only (where the OS supports it).
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Final, Literal

from app.core.json_store import atomic_write_json, read_json

logger = logging.getLogger(__name__)

KEY_BYTES: Final = 32
ApiKeySource = Literal["env", "generated", "none"]


class ApiKeyManagedByEnvError(RuntimeError):
    """The API key comes from ``APP_API_KEY`` and cannot be changed from the API."""


def new_secret() -> str:
    """A fresh URL-safe random secret (256 bits)."""
    return secrets.token_urlsafe(KEY_BYTES)


class SecretsStore:
    """Reads, generates and persists the generated secrets."""

    def __init__(self, path: Path, *, env_api_key: str = "", env_session_secret: str = "") -> None:
        self._path = path
        self._env_api_key = env_api_key.strip()
        self._env_session_secret = env_session_secret.strip()
        self._lock = threading.Lock()
        self._stored: dict[str, str] = {}

    # --- Lifecycle ------------------------------------------------------------------------
    def load(self) -> None:
        """Read the stored secrets (a missing or malformed file means "none")."""
        raw = read_json(self._path, dict)
        with self._lock:
            self._stored = {
                key: value
                for key, value in (raw if isinstance(raw, dict) else {}).items()
                if isinstance(key, str) and isinstance(value, str) and value
            }

    # --- API key --------------------------------------------------------------------------
    @property
    def api_key(self) -> str:
        """The effective API key (empty when none is configured)."""
        return self._env_api_key or self._stored.get("api_key", "")

    @property
    def api_key_source(self) -> ApiKeySource:
        """Where the effective API key comes from."""
        if self._env_api_key:
            return "env"
        return "generated" if self._stored.get("api_key") else "none"

    def ensure_api_key(self) -> str:
        """Generate and store an API key unless one exists (environment or stored)."""
        if self.api_key:
            return self.api_key
        return self._store("api_key", new_secret())

    def regenerate_api_key(self) -> str:
        """Replace the stored API key.

        Raises:
            ApiKeyManagedByEnvError: The key comes from ``APP_API_KEY``.
        """
        if self._env_api_key:
            raise ApiKeyManagedByEnvError(
                "The API key is set by APP_API_KEY; change it in .env and restart"
            )
        key = self._store("api_key", new_secret())
        logger.info("Generated a new API key")
        return key

    # --- Session key ----------------------------------------------------------------------
    @property
    def session_secret(self) -> str:
        """The key signing session cookies (generated and stored on first use)."""
        if self._env_session_secret:
            return self._env_session_secret
        stored = self._stored.get("session_secret")
        return stored if stored else self._store("session_secret", new_secret())

    def values(self) -> list[str]:
        """Every secret held here (for log masking)."""
        return [value for value in (self.api_key, self._stored.get("session_secret", "")) if value]

    # --- Internals ------------------------------------------------------------------------
    def _store(self, name: str, value: str) -> str:
        with self._lock:
            updated = {**self._stored, name: value}
            atomic_write_json(self._path, updated)
            try:
                os.chmod(self._path, 0o600)
            except OSError:  # pragma: no cover - e.g. some network filesystems
                logger.debug("Could not restrict the permissions of %s", self._path)
            self._stored = updated
        return value
