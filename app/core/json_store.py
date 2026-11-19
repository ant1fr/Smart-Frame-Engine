"""Crash-safe JSON file helpers used by the configuration and history stores."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.atomic import atomic_write_bytes

logger = logging.getLogger(__name__)


def read_json(path: Path, default_factory: Callable[[], Any]) -> Any:
    """Read a JSON document, tolerating a missing or corrupt file.

    A missing file returns ``default_factory()``. An unreadable or invalid file is
    renamed to ``<name>.corrupt-<UTC timestamp>`` (so nothing is lost) and
    ``default_factory()`` is returned.

    Args:
        path: File to read.
        default_factory: Builds the value returned when the file cannot be used.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_factory()

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            os.replace(path, backup)
            logger.warning("Corrupt JSON in %s (%s); moved it to %s", path, exc, backup.name)
        except OSError:
            logger.exception("Corrupt JSON in %s and it could not be moved aside", path)
        return default_factory()


def atomic_write_json(path: Path, data: Any, *, file_mode: int | None = None) -> None:
    """Write ``data`` as pretty UTF-8 JSON atomically (see :func:`atomic_write_bytes`).

    Args:
        path: Destination file. Parent directories are created when missing.
        data: JSON-serializable document.
        file_mode: Optional POSIX permission bits (e.g. ``0o600``).
    """
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"), file_mode=file_mode)
