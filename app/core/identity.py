"""Instance identity: a stable random ``instance_id`` and a human-readable ``instance_name``.

The id is generated once, stored in ``/data/instance.json`` and never changes while the data
volume lives, so clients (Home Assistant, scripts) can use it as a stable key even when the
engine's address changes. It is random: not derived from the MAC address, the hostname or
anything else about the machine, so exposing it publicly leaks nothing.

The name defaults to ``Smart Frame Engine <first 6 characters of the id>`` so two engines on one
LAN advertise different mDNS names without any configuration; ``INSTANCE_NAME`` overrides it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.core.json_store import atomic_write_json, read_json

logger = logging.getLogger(__name__)

DEFAULT_NAME: Final = "Smart Frame Engine"
MAX_NAME_LENGTH: Final = 60


@dataclass(frozen=True)
class InstanceIdentity:
    """Who this engine is."""

    instance_id: str
    instance_name: str


def _valid_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def default_name(instance_id: str) -> str:
    """The unique default name built from the start of the id."""
    return f"{DEFAULT_NAME} {instance_id[:6]}"


def load_identity(path: Path, name_override: str = "") -> InstanceIdentity:
    """Read the stored id (generating and persisting it on first run) and resolve the name."""
    raw = read_json(path, dict)
    instance_id = _valid_id(raw.get("instance_id") if isinstance(raw, dict) else None)
    if instance_id is None:
        instance_id = str(uuid.uuid4())
        try:
            atomic_write_json(path, {"instance_id": instance_id})
        except OSError as exc:  # read-only volume: keep running with a per-process id
            logger.warning("Cannot persist the instance id (%s); it will change on restart", exc)
    name = " ".join(name_override.split())[:MAX_NAME_LENGTH] or default_name(instance_id)
    return InstanceIdentity(instance_id=instance_id, instance_name=name)
