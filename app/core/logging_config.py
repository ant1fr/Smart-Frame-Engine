"""Logging setup with secret masking.

Every handler gets a :class:`SecretMaskingFilter` that redacts known secret values
(API keys from the environment and from ``config.json``) and any ``api_key=...`` query
parameter, which would otherwise leak through Uvicorn's access log.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Iterable
from typing import Any, Final

REDACTED: Final = "***"
_QUERY_KEY_RE: Final = re.compile(r"(?i)((?:api_key|apikey|api-key|token)=)[^&\s\"']+")
_MIN_SECRET_LENGTH: Final = 4
_UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")
LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class SecretMaskingFilter(logging.Filter):
    """Redact secrets from log records (message *and* formatting arguments)."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._secrets: tuple[str, ...] = ()

    def set_secrets(self, values: Iterable[str]) -> None:
        """Replace the set of literal values to redact."""
        cleaned = {v for v in values if v and len(v) >= _MIN_SECRET_LENGTH}
        with self._lock:
            # Longest first so a secret containing another one is fully redacted.
            self._secrets = tuple(sorted(cleaned, key=len, reverse=True))

    def mask(self, text: str) -> str:
        """Return ``text`` with every known secret and ``api_key=`` value redacted."""
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return _QUERY_KEY_RE.sub(rf"\1{REDACTED}", text)

    def _mask_value(self, value: Any) -> Any:
        return self.mask(value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.mask(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._mask_value(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._mask_value(val) for key, val in record.args.items()}
        return True


#: Process-wide filter instance shared by all handlers.
masking_filter = SecretMaskingFilter()


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once and attach the masking filter everywhere.

    Safe to call multiple times. When Uvicorn installed its own handlers (e.g. when run
    as ``uvicorn --factory app.main:create_app``), the filter is attached to them too.
    """
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(stream_handler)

    # APScheduler logs every job execution at INFO; the scheduler service logs its own.
    logging.getLogger("apscheduler").setLevel(
        logging.DEBUG if root.level <= logging.DEBUG else logging.WARNING
    )

    handlers: list[logging.Handler] = list(root.handlers)
    for name in _UVICORN_LOGGERS:
        handlers.extend(logging.getLogger(name).handlers)
    for handler in handlers:
        if masking_filter not in handler.filters:
            handler.addFilter(masking_filter)
