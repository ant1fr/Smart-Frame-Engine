"""Crash-safe file writes (temporary file + fsync + atomic rename)."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, file_mode: int | None = None) -> None:
    """Write ``data`` to ``path`` atomically.

    The content is written to a temporary file in the same directory, flushed to disk
    and then moved over ``path`` with :func:`os.replace`. Readers therefore observe
    either the previous file or the complete new one, never a partial write, even if
    the process crashes or the container stops mid-write.

    Args:
        path: Destination file. Parent directories are created when missing.
        data: Bytes to write.
        file_mode: Optional POSIX permission bits (e.g. ``0o600``). Ignored where the
            platform does not support ``chmod`` semantics.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
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


def remove_quietly(*paths: Path) -> None:
    """Delete files, ignoring the ones that do not exist."""
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
