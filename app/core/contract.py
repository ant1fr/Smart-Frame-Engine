"""The client-facing API contract: compatibility versions and capability flags.

This module is the single place that says what an external client (Home Assistant,
Node-RED, scripts) may rely on. ``docs/API_CONTRACT.md`` describes the policy; a test keeps
every capability tied to a route that really exists.

Rules:

* ``API_VERSION`` is an integer incremented only on a **breaking** change of the HTTP API.
* ``MIN_API_VERSION`` is the oldest contract this engine still serves. A client that
  implements contract ``N`` is compatible when ``MIN_API_VERSION <= N <= API_VERSION``.
* A capability flag is only ever **added** within a contract version, never removed or renamed.
"""

from __future__ import annotations

from typing import Final

API_VERSION: Final = 1
MIN_API_VERSION: Final = 1

#: Capability flag -> ``(METHOD, path)`` of the endpoint that backs it. Flags that are a
#: behaviour of an existing endpoint (family mode, random discovery, deduplication) point to
#: the endpoint that carries the related settings or fields.
CAPABILITIES: Final[dict[str, tuple[str, str]]] = {
    "activity_log": ("GET", "/api/activity"),
    "ambient_context": ("POST", "/api/context"),
    "display_immich": ("POST", "/api/display/immich"),
    "display_smart_search": ("POST", "/api/display/smart-search"),
    "display_tmdb": ("POST", "/api/display/tmdb"),
    "ephemeral_images": ("DELETE", "/api/queue/ephemeral"),
    "family_mode": ("PUT", "/api/config"),
    "force_refresh": ("POST", "/api/refresh"),
    "frame_push": ("POST", "/api/fraimic/push"),
    "frame_wakeup": ("POST", "/api/fraimic/wakeup"),
    "now_playing": ("POST", "/api/display/now-playing"),
    "now_playing_dedup": ("POST", "/api/display/now-playing"),
    "playground": ("POST", "/api/playground/render"),
    "random_discovery": ("PUT", "/api/config"),
    "smart_mix": ("GET", "/api/smart-mix"),
    "smart_mix_context": ("POST", "/api/smart-mix/context"),
    "title_card": ("POST", "/api/display/title-card"),
    "upload_image": ("POST", "/api/display/upload"),
}


def capability_names() -> list[str]:
    """The active capability flags, sorted (deterministic across runs and versions)."""
    return sorted(CAPABILITIES)
