"""Web UI login sessions: signed cookie tokens and a login throttle.

A session token is ``v1.<expiry>.<nonce>.<signature>``: the expiry (Unix seconds), a random
nonce and an HMAC-SHA256 signature of both. The signing key is derived from the session
secret *and* the admin password, so changing the password (or the session secret) signs
everyone out. Nothing is stored server-side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

VERSION: Final = "v1"
KDF_ITERATIONS: Final = 100_000
COOKIE_NAME: Final = "sfe_session"
#: Failed logins from one client before throttling starts, and the longest wait (seconds).
FREE_ATTEMPTS: Final = 5
MAX_LOCKOUT_S: Final = 300.0

Clock = Callable[[], float]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@lru_cache(maxsize=8)
def signing_key(session_secret: str, password: str) -> bytes:
    """Key of the session signatures; changes with the secret or the password.

    Derived with PBKDF2 (salted with the random session secret) and cached, so the cost is
    paid once per process and not on every request.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        b"sfe-session:" + session_secret.encode("utf-8"),
        KDF_ITERATIONS,
    )


def _signature(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode("ascii"), "sha256").digest())


def issue_token(key: bytes, ttl_s: float, *, now: float | None = None) -> tuple[str, int]:
    """A new session token and its expiry (Unix seconds)."""
    expires = int((time.time() if now is None else now) + ttl_s)
    payload = f"{VERSION}.{expires}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{_signature(key, payload)}", expires


def token_expiry(token: str | None, key: bytes, *, now: float | None = None) -> int | None:
    """The expiry of a valid, unexpired ``token``, else ``None``."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != VERSION or not parts[1].isdigit():
        return None
    payload = ".".join(parts[:3])
    if not hmac.compare_digest(_signature(key, payload), parts[3]):
        return None
    expires = int(parts[1])
    return expires if expires > (time.time() if now is None else now) else None


@dataclass(slots=True)
class _Failures:
    count: int = 0
    blocked_until: float = 0.0


class LoginThrottle:
    """Per-client exponential backoff after repeated failed logins."""

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._failures: dict[str, _Failures] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> float:
        """Seconds before ``client`` may try again (0 = now)."""
        with self._lock:
            entry = self._failures.get(client)
            if entry is None:
                return 0.0
            return max(0.0, entry.blocked_until - self._clock())

    def failure(self, client: str) -> None:
        """Record a failed attempt."""
        with self._lock:
            entry = self._failures.setdefault(client, _Failures())
            entry.count += 1
            if entry.count >= FREE_ATTEMPTS:
                delay = min(2.0 ** (entry.count - FREE_ATTEMPTS), MAX_LOCKOUT_S)
                entry.blocked_until = self._clock() + delay
            if len(self._failures) > 10_000:  # bound memory under a distributed attack
                self._failures.clear()

    def success(self, client: str) -> None:
        """Forget the failures of ``client``."""
        with self._lock:
            self._failures.pop(client, None)
