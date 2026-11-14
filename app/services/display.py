"""Display orchestration: render an image, store it as current, deliver it to the frame.

Every image source (uploads now; Immich, TMDB and URLs in later milestones) goes
through :meth:`DisplayService.display_bytes`, which serializes rendering so that only
one image is processed at a time (bounded memory, consistent ``current.*`` files).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, Field, JsonValue

from app.core.config_models import FitMode
from app.core.config_store import ConfigStore
from app.processor.fit import FocusRegion
from app.processor.pipeline import RenderOptions, render_bytes
from app.services.current_image import CurrentImageMeta, CurrentImageStore
from app.services.delivery import DeliveryOutcome, DeliveryService


class NoCurrentImageError(LookupError):
    """There is no usable current image to push."""


class DisplayResult(BaseModel):
    """Result of displaying an image."""

    image: CurrentImageMeta
    delivery: DeliveryOutcome | None = Field(
        default=None, description="Frame delivery outcome (null when push was disabled)"
    )


class DisplayService:
    """Renders images and hands them to the delivery service."""

    def __init__(
        self,
        config_store: ConfigStore,
        current_store: CurrentImageStore,
        delivery: DeliveryService,
    ) -> None:
        self._config_store = config_store
        self._current = current_store
        self._delivery = delivery
        self._render_lock = asyncio.Lock()

    async def display_bytes(
        self,
        data: bytes,
        *,
        source: str,
        title: str | None = None,
        details: dict[str, JsonValue] | None = None,
        fit: FitMode | None = None,
        regions: Sequence[FocusRegion] = (),
        push: bool = True,
    ) -> DisplayResult:
        """Render ``data``, make it the current image and optionally push it.

        Raises:
            InvalidImageError: If ``data`` is not a supported image.
        """
        async with self._render_lock:
            options = RenderOptions.from_config(self._config_store.config, fit=fit)
            result = await asyncio.to_thread(render_bytes, data, options, tuple(regions))
            meta = await asyncio.to_thread(
                self._current.save, result, source=source, title=title, details=details
            )
        delivery = (
            await self._delivery.submit(result.bin, source=source, title=title) if push else None
        )
        return DisplayResult(image=meta, delivery=delivery)

    async def push_current(self) -> DeliveryOutcome:
        """Send the current image to the frame now, whatever the power mode.

        Raises:
            NoCurrentImageError: If nothing was rendered yet, or the current image was
                rendered for a different panel than the one configured now.
        """
        loaded = await asyncio.to_thread(self._current.read_bin)
        if loaded is None:
            raise NoCurrentImageError("No image has been rendered yet")
        data, meta = loaded
        if meta.panel != self._config_store.config.fraimic.panel:
            raise NoCurrentImageError(
                f"The current image was rendered for panel {meta.panel}; display a new image "
                f"for panel {self._config_store.config.fraimic.panel} first"
            )
        return await self._delivery.submit(
            data, source=meta.source, title=meta.title, immediate=True
        )
