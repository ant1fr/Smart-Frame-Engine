"""Recent activity for the web UI: the last displayed images and the last log lines.

Both live in bounded in-memory ring buffers (nothing is persisted): the dashboard only
needs "what happened lately", and the full history stays in the container logs.

* Renders are recorded by a :class:`~app.services.display.DisplayService` listener, so every
  change of the current image (push, ephemeral expiry, revert) appears.
* Log lines are captured by a :class:`logging.Handler` that redacts secrets with the
  process-wide :data:`~app.core.logging_config.masking_filter` before storing anything.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, Field

from app.core.logging_config import masking_filter
from app.processor.auto import RenderDecision
from app.services.current_image import CurrentImageMeta

MAX_RENDERS: Final = 30
MAX_LOG_LINES: Final = 200
MAX_MESSAGE_LENGTH: Final = 1000

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class RenderEvent(BaseModel):
    """One change of the current image."""

    at: AwareDatetime
    source: str
    title: str | None = None
    template: str | None = None
    render: RenderDecision | None = None
    ephemeral: bool = False
    expires_at: AwareDatetime | None = None
    sha256: str


class LogLine(BaseModel):
    """One captured log record (secrets already redacted)."""

    at: AwareDatetime
    level: LogLevel
    logger: str
    message: str


class ActivitySnapshot(BaseModel):
    """Body of ``GET /api/activity`` (newest first)."""

    renders: list[RenderEvent] = Field(description="Last displayed images, newest first")
    logs: list[LogLine] = Field(description="Last log lines, newest first")


def _level(record: logging.LogRecord) -> LogLevel:
    if record.levelno >= logging.CRITICAL:
        return "CRITICAL"
    if record.levelno >= logging.ERROR:
        return "ERROR"
    if record.levelno >= logging.WARNING:
        return "WARNING"
    return "INFO" if record.levelno >= logging.INFO else "DEBUG"


class _RingHandler(logging.Handler):
    """Stores formatted, masked records in the activity log."""

    def __init__(self, sink: ActivityLog, level: int) -> None:
        super().__init__(level)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = masking_filter.mask(record.getMessage())
            if record.exc_info and record.exc_info[0] is not None:
                message = f"{message} ({record.exc_info[0].__name__})"
            self._sink.add_log(
                LogLine(
                    at=datetime.fromtimestamp(record.created, tz=UTC),
                    level=_level(record),
                    logger=record.name,
                    message=message[:MAX_MESSAGE_LENGTH],
                )
            )
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


class ActivityLog:
    """Bounded buffers of recent renders and log lines (thread-safe)."""

    def __init__(self, *, max_renders: int = MAX_RENDERS, max_logs: int = MAX_LOG_LINES) -> None:
        self._lock = threading.Lock()
        self._renders: deque[RenderEvent] = deque(maxlen=max_renders)
        self._logs: deque[LogLine] = deque(maxlen=max_logs)
        self._handler: _RingHandler | None = None

    # --- Renders --------------------------------------------------------------------------
    def record_render(self, meta: CurrentImageMeta) -> None:
        """Display listener: remember a new current image."""
        event = RenderEvent(
            at=meta.processed_at,
            source=meta.source,
            title=meta.title,
            template=meta.template,
            render=meta.render,
            ephemeral=meta.ephemeral,
            expires_at=meta.expires_at,
            sha256=meta.sha256,
        )
        with self._lock:
            self._renders.append(event)

    # --- Logs -----------------------------------------------------------------------------
    def add_log(self, line: LogLine) -> None:
        """Store one log line."""
        with self._lock:
            self._logs.append(line)

    def attach(self, logger: logging.Logger | None = None, level: int = logging.INFO) -> None:
        """Start capturing records of ``logger`` (default: the root logger)."""
        if self._handler is not None:
            return
        self._handler = _RingHandler(self, level)
        (logger or logging.getLogger()).addHandler(self._handler)

    def detach(self, logger: logging.Logger | None = None) -> None:
        """Stop capturing log records."""
        if self._handler is None:
            return
        (logger or logging.getLogger()).removeHandler(self._handler)
        self._handler = None

    # --- Reading --------------------------------------------------------------------------
    def snapshot(self, *, logs: int = MAX_LOG_LINES) -> ActivitySnapshot:
        """Newest-first copy of both buffers (``logs`` caps the number of log lines)."""
        with self._lock:
            renders = list(reversed(self._renders))
            lines = list(reversed(self._logs))[: max(0, logs)]
        return ActivitySnapshot(renders=renders, logs=lines)
