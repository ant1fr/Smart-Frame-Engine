"""Display orchestration: render an image, store it, deliver it to the frame.

Every image source goes through :class:`DisplayService`, which serializes rendering so
that only one image is processed at a time (bounded memory, consistent slot files).

* A **pushed** render becomes the current image and is handed to the delivery service.
  Unless it is ephemeral, it also becomes the *base* image restored after an ephemeral
  image (now playing, movie poster) expires or is cancelled.
* A render with ``push=False`` only updates the *preview* slot: what Home Assistant and
  the frame see (``current.*``) is left untouched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field, JsonValue

from app.core.config_models import FitMode
from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.processor.fit import FocusRegion
from app.processor.pipeline import (
    RenderOptions,
    RenderResult,
    render_bytes,
    render_layout_bytes,
)
from app.processor.templates import Layout
from app.providers.base import ImageAsset
from app.services.current_image import CurrentImageMeta, CurrentImageStore
from app.services.delivery import DeliveryOutcome, DeliveryService

logger = logging.getLogger(__name__)

DisplayListener = Callable[[CurrentImageMeta], None]


class NoCurrentImageError(LookupError):
    """There is no usable current image to push."""


class DisplayResult(BaseModel):
    """Result of displaying an image."""

    image: CurrentImageMeta
    delivery: DeliveryOutcome | None = Field(
        default=None, description="Frame delivery outcome (null when push was disabled)"
    )
    deduplicated: bool = Field(
        default=False,
        description="The same content was already displayed: only its expiry was extended",
    )


@dataclass(frozen=True, slots=True)
class DisplayMeta:
    """Descriptive metadata and delivery intent of one display."""

    source: str
    title: str | None = None
    details: dict[str, JsonValue] | None = None
    push: bool = True
    ephemeral: bool = False
    expires_at: datetime | None = None
    content_key: str | None = None


class RevertOutcome(BaseModel):
    """What restoring the previous (base) image did."""

    reverted: bool = Field(description="current.* now shows the previous image again")
    has_base: bool = Field(description="Whether a previous non-ephemeral image exists")
    pushed_base: bool = Field(
        default=False, description="The previous image was re-sent (or queued) for the frame"
    )
    delivery: DeliveryOutcome | None = None
    image: CurrentImageMeta | None = None


class DisplayService:
    """Renders images and hands them to the delivery service."""

    def __init__(
        self,
        config_store: ConfigStore,
        current_store: CurrentImageStore,
        delivery: DeliveryService,
        history: HistoryStore,
    ) -> None:
        self._config_store = config_store
        self._current = current_store
        self._delivery = delivery
        self._history = history
        self._render_lock = asyncio.Lock()
        self._listeners: list[DisplayListener] = []

    def add_listener(self, listener: DisplayListener) -> None:
        """Call ``listener`` after every change of the current image (display or revert)."""
        self._listeners.append(listener)

    # --- Rendering ------------------------------------------------------------------------
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
        ephemeral: bool = False,
        expires_at: datetime | None = None,
        content_key: str | None = None,
    ) -> DisplayResult:
        """Render ``data`` with a fit mode, store it and optionally push it.

        Raises:
            InvalidImageError: If ``data`` is not a supported image.
        """
        options = RenderOptions.from_config(self._config_store.config, fit=fit)
        focus = tuple(regions)
        return await self._display(
            lambda: render_bytes(data, options, focus),
            DisplayMeta(source, title, details, push, ephemeral, expires_at, content_key),
            template=None,
        )

    async def display_layout(
        self, data: bytes | None, layout: Layout, meta: DisplayMeta
    ) -> DisplayResult:
        """Render ``data`` (or nothing, for a title card) with a composition template.

        Templates never crop and always use the ``official`` eInk profile (posters and
        album art are graphic designs, not photos).

        Raises:
            InvalidImageError: If ``data`` is not a supported image.
        """
        options = RenderOptions.from_config(
            self._config_store.config, fit="contain", eink_profile="official"
        )
        return await self._display(
            lambda: render_layout_bytes(data, layout, options), meta, template=layout.name
        )

    async def display_asset(
        self,
        asset: ImageAsset,
        *,
        fit: FitMode | None = None,
        push: bool = True,
        ephemeral: bool = False,
        expires_at: datetime | None = None,
        content_key: str | None = None,
    ) -> DisplayResult:
        """Display a provider asset and record it in the anti-repeat history.

        Assets with a :attr:`ImageAsset.layout` use that template; others use the explicit
        ``fit``, else the asset's recommended one, else the configured default. The history
        entry is written only after rendering succeeded (and only for tracked assets).

        Raises:
            InvalidImageError: If the asset's bytes are not a supported image.
        """
        meta = DisplayMeta(
            asset.source, asset.title, asset.details, push, ephemeral, expires_at, content_key
        )
        if asset.layout is not None:
            result = await self.display_layout(asset.data or None, asset.layout, meta)
        else:
            result = await self.display_bytes(
                asset.data,
                source=asset.source,
                title=asset.title,
                details=asset.details,
                fit=fit or asset.recommended_fit,
                regions=asset.regions,
                push=push,
                ephemeral=ephemeral,
                expires_at=expires_at,
                content_key=content_key,
            )
        if asset.track_history:
            await self._history.record(asset.history_key, asset.source, taken_at=asset.taken_at)
        return result

    async def _display(
        self,
        renderer: Callable[[], RenderResult],
        meta: DisplayMeta,
        *,
        template: str | None,
    ) -> DisplayResult:
        async with self._render_lock:
            result = await asyncio.to_thread(renderer)
            stored = await asyncio.to_thread(
                self._current.save,
                result,
                source=meta.source,
                title=meta.title,
                details=meta.details,
                push=meta.push,
                ephemeral=meta.ephemeral,
                expires_at=meta.expires_at if meta.ephemeral else None,
                content_key=meta.content_key,
                template=template,
            )
        delivery: DeliveryOutcome | None = None
        if meta.push:
            delivery = await self._delivery.submit(
                result.bin,
                source=meta.source,
                title=meta.title,
                ephemeral=meta.ephemeral,
                expires_at=meta.expires_at if meta.ephemeral else None,
                content_key=meta.content_key,
            )
            self._notify(stored)
        return DisplayResult(image=stored, delivery=delivery)

    # --- Ephemeral support ----------------------------------------------------------------
    async def extend_expiry(
        self, content_key: str, expires_at: datetime | None
    ) -> CurrentImageMeta | None:
        """Keep the current ephemeral image up longer if it shows ``content_key``.

        The queued upload (if any) is updated too; nothing is rendered or re-sent.

        Returns:
            The updated metadata, or ``None`` when the current image is something else.
        """
        async with self._render_lock:
            updated = await asyncio.to_thread(self._current.extend_expiry, content_key, expires_at)
        if updated is None:
            return None
        await self._delivery.extend_pending(content_key, expires_at)
        self._notify(updated)
        return updated

    async def revert_to_base(self, *, only_if_current: str | None = None) -> RevertOutcome:
        """Show the previous non-ephemeral image again.

        ``current.*`` is restored from ``base.*``; the base ``.bin`` is (re)submitted only
        when the frame may not be showing it already (see
        :meth:`DeliveryService.frame_content_differs`), so a deep-sleeping frame is not
        woken up for nothing.

        Args:
            only_if_current: Revert only while the current image still has this SHA-256
                (an expiry must not undo a newer display).
        """
        async with self._render_lock:
            current = self._current.meta
            base = self._current.base_meta
            if only_if_current is not None and (
                current is None or current.sha256 != only_if_current
            ):
                return RevertOutcome(reverted=False, has_base=base is not None, image=current)
            if base is None:
                return RevertOutcome(reverted=False, has_base=False, image=current)
            restored = await asyncio.to_thread(self._current.restore_base)
            loaded = await asyncio.to_thread(self._current.read_bin, "base")
        if restored is None or loaded is None:
            return RevertOutcome(reverted=False, has_base=False, image=self._current.meta)
        data, meta = loaded
        delivery: DeliveryOutcome | None = None
        panel_ok = meta.panel == self._config_store.config.fraimic.panel
        if panel_ok and self._delivery.frame_content_differs(meta.sha256):
            delivery = await self._delivery.submit(data, source=meta.source, title=meta.title)
        elif not panel_ok:
            logger.warning("The previous image was rendered for panel %s; not re-sent", meta.panel)
        self._notify(restored)
        return RevertOutcome(
            reverted=True,
            has_base=True,
            pushed_base=delivery is not None,
            delivery=delivery,
            image=restored,
        )

    async def push_current(self, *, from_preview: bool = False) -> DeliveryOutcome:
        """Send the current image to the frame now, whatever the power mode.

        Args:
            from_preview: First make the latest preview (e.g. a ``push_to_fraimic=false``
                render) the current image, then send it.

        Raises:
            NoCurrentImageError: If nothing was rendered yet, or the image was rendered for
                a different panel than the one configured now.
        """
        if from_preview:
            preview = self._current.preview_meta
            if preview is None:
                raise NoCurrentImageError("No preview has been rendered yet")
            if preview.panel != self._config_store.config.fraimic.panel:
                raise NoCurrentImageError(
                    f"The preview was rendered for panel {preview.panel}; render it again "
                    f"for panel {self._config_store.config.fraimic.panel} first"
                )
            async with self._render_lock:
                promoted = await asyncio.to_thread(self._current.promote_preview)
            if promoted is None:
                raise NoCurrentImageError("The preview files are missing or damaged")
            self._notify(promoted)
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
            data,
            source=meta.source,
            title=meta.title,
            immediate=True,
            ephemeral=meta.ephemeral,
            expires_at=meta.expires_at,
            content_key=meta.content_key,
        )

    def _notify(self, meta: CurrentImageMeta) -> None:
        for listener in self._listeners:
            try:
                listener(meta)
            except Exception:
                logger.exception("Display listener failed")
