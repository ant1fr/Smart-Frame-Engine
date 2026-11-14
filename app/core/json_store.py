"""Crash-safe JSON file helpers used by the configuration and history stores."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    """Write ``data`` as pretty JSON atomically (temp file + fsync + ``os.replace``).

    Readers never observe a partially written file, even if the process crashes or the
    container is stopped mid-write.

    Args:
        path: Destination file. Parent directories are created when missing.
        data: JSON-serializable document.
        file_mode: Optional POSIX permission bits (e.g. ``0o600``). Ignored where the
            platform does not support ``chmod`` semantics.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if file_mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, file_mode)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
