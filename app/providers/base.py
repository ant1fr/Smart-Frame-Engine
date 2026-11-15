"""Provider abstractions shared by every image source.

A provider turns a *request* (what the user asked for) into an :class:`ImageAsset`
(raw bytes plus everything the renderer and the UI need). Providers never render,
store or deliver anything: :class:`app.services.display.DisplayService` does that.

Adding a source = subclass :class:`BaseImageProvider`, return :class:`ImageAsset`
objects and raise the :class:`ProviderError` subclasses below so the API layer can map
them to HTTP status codes without knowing the provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import JsonValue

from app.core.config_models import FitMode
from app.processor.fit import FocusRegion
from app.processor.templates import Layout


class ProviderError(Exception):
    """Base class of every error a provider raises on purpose (messages are client-safe)."""


class ProviderNotConfiguredError(ProviderError):
    """The provider has no usable credentials or endpoint yet (HTTP 409)."""


class InvalidSelectionError(ProviderError):
    """The request names something that does not exist or is ambiguous (HTTP 422)."""


class NoCandidateError(ProviderError):
    """Nothing eligible matched the request (HTTP 404)."""


class ProviderUnavailableError(ProviderError):
    """The upstream service failed or could not be reached (HTTP 502, or 504 on timeout)."""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass(frozen=True, slots=True)
class ImageAsset:
    """A fetched image and its metadata, ready for rendering.

    Attributes:
        data: Raw image bytes (any format Pillow can decode).
        source: Provider name (``"immich"``, ``"media"``...); also the history namespace.
        history_key: Stable identifier used by the anti-repeat history. Providers that can
            see the same picture through several routes (e.g. two Immich accounts) should
            derive it from the content so the cooldown applies across routes.
        title: Short human-readable title (people, place, movie name...).
        details: Provider metadata shown in the UI; never put secrets or URLs with keys here.
        recommended_fit: Fit mode this source prefers (e.g. posters: ``contain``); ``None``
            means "use the configured default".
        regions: Regions of interest (faces) in normalized coordinates, most relevant first.
        taken_at: When the picture was taken, if known (burst cooldown in the history).
        layout: Composition template (poster, album cover, title card) used instead of a
            fit mode; ``data`` may then be empty for templates that need no artwork.
        track_history: Record the display in the anti-repeat history (photos); posters
            and album covers are shown on demand and are not tracked.
    """

    data: bytes = field(repr=False)
    source: str
    history_key: str
    title: str | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)
    recommended_fit: FitMode | None = None
    regions: tuple[FocusRegion, ...] = ()
    taken_at: datetime | None = None
    layout: Layout | None = None
    track_history: bool = True


RequestT = TypeVar("RequestT", contravariant=True)


class BaseImageProvider(ABC, Generic[RequestT]):
    """An image source."""

    #: Short, stable provider name (also the :attr:`ImageAsset.source` value).
    name: str

    @abstractmethod
    async def fetch(self, request: RequestT) -> ImageAsset:
        """Select an image for ``request`` and download it.

        Raises:
            ProviderNotConfiguredError: The provider is not set up.
            InvalidSelectionError: The request references unknown or ambiguous items.
            NoCandidateError: Nothing eligible matched.
            ProviderUnavailableError: The upstream service failed.
        """
