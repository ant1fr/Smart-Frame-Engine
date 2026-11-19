"""Persistence of rendered images in ``/data/cache``, in three slots.

* ``current`` - what the frame shows (or is about to show once a sleeping frame wakes up):
  ``current.bin`` (pushed to the frame), ``current.jpg`` (Home Assistant, UI),
  ``current_dithered.png`` (exact 6-color simulation) and ``current.json`` (metadata).
* ``base`` - the most recent **pushed, non-ephemeral** image (``base.*``). When an
  ephemeral image (now playing, movie poster) expires or is cancelled, ``base`` is copied
  back to ``current`` and re-sent to the frame if needed.
* ``preview`` - the most recent render of any kind (``preview.*``), including renders made
  with ``push_to_fraimic=false``, which never touch ``current``.

In each slot the ``.json`` metadata is written **last** as the commit marker. It records
the SHA-256 of the slot's ``.bin``, so a set interrupted mid-write is detected and ignored.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.core.atomic import atomic_write_bytes
from app.core.config_models import DitherAlgorithm, FitMode, PanelModel
from app.core.json_store import atomic_write_json, read_json
from app.processor.auto import RenderDecision
from app.processor.pipeline import RenderResult

logger = logging.getLogger(__name__)

SlotName = Literal["current", "base", "preview"]
SLOTS: Final[tuple[SlotName, ...]] = ("current", "base", "preview")

BIN_NAME: Final = "current.bin"
JPEG_NAME: Final = "current.jpg"
DITHERED_NAME: Final = "current_dithered.png"
META_NAME: Final = "current.json"


class CurrentImageMeta(BaseModel):
    """Metadata of a rendered image (served by ``GET /api/status``)."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(description="Where the image came from, e.g. 'upload' or 'immich'")
    title: str | None = Field(default=None, description="Human-readable title or file name")
    details: dict[str, JsonValue] = Field(
        default_factory=dict, description="Provider metadata (date, location, people...)"
    )
    panel: PanelModel
    orientation: str
    rotation: int
    fit: FitMode
    dither: DitherAlgorithm
    frame_size: tuple[int, int] = Field(description="Composition size (width, height)")
    processed_at: AwareDatetime
    sha256: str = Field(description="SHA-256 of the .bin")
    bin_size: int
    template: str | None = Field(
        default=None, description="Composition template (poster, album, title_card), if any"
    )
    render: RenderDecision | None = Field(
        default=None,
        description="The eInk profile and dithering used, and why (automatic or from settings)",
    )
    pushed: bool = Field(default=True, description="Whether the image was sent to the frame")
    ephemeral: bool = Field(
        default=False, description="Temporary image: the previous one comes back afterwards"
    )
    expires_at: AwareDatetime | None = Field(
        default=None, description="When an ephemeral image is replaced by the previous one"
    )
    content_key: str | None = Field(
        default=None, description="Identity of the displayed content (deduplication)"
    )

    def expired(self, now: datetime) -> bool:
        """Whether this ephemeral image is past its expiry."""
        return self.ephemeral and self.expires_at is not None and self.expires_at <= now


@dataclass(frozen=True, slots=True)
class SlotPaths:
    """Files of one slot."""

    bin: Path
    jpeg: Path
    dithered: Path
    meta: Path

    def all(self) -> tuple[Path, Path, Path, Path]:
        """Every file of the slot, metadata last."""
        return self.bin, self.jpeg, self.dithered, self.meta


def slots_for(*, push: bool, ephemeral: bool) -> tuple[SlotName, ...]:
    """Slots a render is written to.

    * pushed, non-ephemeral: ``current`` + ``base`` + ``preview``;
    * pushed, ephemeral: ``current`` + ``preview``;
    * not pushed: ``preview`` only.
    """
    if not push:
        return ("preview",)
    if ephemeral:
        return ("current", "preview")
    return ("current", "base", "preview")


class CurrentImageStore:
    """Reads and atomically writes the image slots."""

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._lock = threading.Lock()
        self._metas: dict[SlotName, CurrentImageMeta | None] = dict.fromkeys(SLOTS)

    # --- Paths ----------------------------------------------------------------------------
    def paths(self, slot: SlotName) -> SlotPaths:
        """Files of ``slot``."""
        return SlotPaths(
            bin=self._dir / f"{slot}.bin",
            jpeg=self._dir / f"{slot}.jpg",
            dithered=self._dir / f"{slot}_dithered.png",
            meta=self._dir / f"{slot}.json",
        )

    @property
    def bin_path(self) -> Path:
        """Location of ``current.bin``."""
        return self.paths("current").bin

    @property
    def jpeg_path(self) -> Path:
        """Location of ``current.jpg``."""
        return self.paths("current").jpeg

    @property
    def dithered_path(self) -> Path:
        """Location of ``current_dithered.png``."""
        return self.paths("current").dithered

    @property
    def meta_path(self) -> Path:
        """Location of ``current.json``."""
        return self.paths("current").meta

    # --- Metadata -------------------------------------------------------------------------
    @property
    def meta(self) -> CurrentImageMeta | None:
        """Metadata of the current image, or ``None`` when nothing was rendered yet."""
        return self._metas["current"]

    @property
    def base_meta(self) -> CurrentImageMeta | None:
        """Metadata of the last pushed non-ephemeral image, if any."""
        return self._metas["base"]

    @property
    def preview_meta(self) -> CurrentImageMeta | None:
        """Metadata of the most recent render (pushed or not), if any."""
        return self._metas["preview"]

    def slot_meta(self, slot: SlotName) -> CurrentImageMeta | None:
        """Metadata of ``slot``."""
        return self._metas[slot]

    def load(self) -> CurrentImageMeta | None:
        """Load and verify every slot (call once at startup); returns the current one."""
        with self._lock:
            for slot in SLOTS:
                self._metas[slot] = self._load_verified(slot)
            current = self._metas["current"]
            if self._metas["base"] is None and current is not None and not current.ephemeral:
                # Data written before the base slot existed: the current image is the base.
                self._copy_slot("current", "base", current)
            return current

    # --- Writes ---------------------------------------------------------------------------
    def save(
        self,
        result: RenderResult,
        *,
        source: str,
        title: str | None = None,
        details: dict[str, JsonValue] | None = None,
        push: bool = True,
        ephemeral: bool = False,
        expires_at: datetime | None = None,
        content_key: str | None = None,
        template: str | None = None,
    ) -> CurrentImageMeta:
        """Persist a render result into the slots chosen by :func:`slots_for`.

        Blocking; call it through a thread.
        """
        options = result.options
        meta = CurrentImageMeta(
            source=source,
            title=title,
            details=details or {},
            panel=options.panel.key,
            orientation="portrait" if options.rotation == 0 else "landscape",
            rotation=options.rotation,
            fit=options.fit,
            dither=options.eink.dither,
            frame_size=options.frame_size,
            processed_at=datetime.now(UTC),
            sha256=result.sha256,
            bin_size=len(result.bin),
            template=template,
            render=result.decision,
            pushed=push,
            ephemeral=ephemeral,
            expires_at=expires_at,
            content_key=content_key,
        )
        jpeg = result.preview_jpeg()
        dithered = result.dithered_png()
        slots = slots_for(push=push, ephemeral=ephemeral)
        with self._lock:
            for slot in slots:
                self._write_slot(slot, result.bin, jpeg, dithered, meta)
        logger.info(
            "Saved image from %s (%s, %d bytes, sha256 %s) to %s",
            source,
            title or "untitled",
            meta.bin_size,
            meta.sha256[:12],
            "+".join(slots),
        )
        return meta

    def restore_base(self) -> CurrentImageMeta | None:
        """Copy the ``base`` slot over ``current`` (blocking; call it through a thread).

        Returns:
            The restored metadata, or ``None`` when there is no valid base image.
        """
        with self._lock:
            base = self._metas["base"]
            if base is None:
                return None
            if not self._copy_slot("base", "current", base):
                self._metas["base"] = None
                return None
        logger.info("Restored the previous image (%s, %s)", base.source, base.title or "untitled")
        return base

    def promote_preview(self) -> CurrentImageMeta | None:
        """Make the preview the current image (and the base, unless it is ephemeral).

        Blocking; call it through a thread.

        Returns:
            The promoted metadata, or ``None`` when there is no valid preview.
        """
        with self._lock:
            preview = self._metas["preview"]
            if preview is None:
                return None
            promoted = preview.model_copy(update={"pushed": True})
            targets: tuple[SlotName, ...] = (
                ("current",) if promoted.ephemeral else ("current", "base")
            )
            for target in targets:
                if not self._copy_slot("preview", target, promoted):
                    return None
            atomic_write_json(self.paths("preview").meta, promoted.model_dump(mode="json"))
            self._metas["preview"] = promoted
        logger.info("Promoted the preview (%s) to the current image", promoted.title or "untitled")
        return promoted

    def extend_expiry(
        self, content_key: str, expires_at: datetime | None
    ) -> CurrentImageMeta | None:
        """Move the expiry of the current image if it shows ``content_key``.

        Returns:
            The updated metadata, or ``None`` when the current image is something else.
        """
        with self._lock:
            meta = self._metas["current"]
            if meta is None or not meta.ephemeral or meta.content_key != content_key:
                return None
            updated = meta.model_copy(update={"expires_at": expires_at})
            atomic_write_json(self.paths("current").meta, updated.model_dump(mode="json"))
            self._metas["current"] = updated
            return updated

    # --- Reads ----------------------------------------------------------------------------
    def read_bin(self, slot: SlotName = "current") -> tuple[bytes, CurrentImageMeta] | None:
        """Return the ``.bin`` of ``slot`` and its metadata if both are present and consistent."""
        with self._lock:
            meta = self._metas[slot]
            if meta is None:
                return None
            try:
                data = self.paths(slot).bin.read_bytes()
            except FileNotFoundError:
                return None
            if hashlib.sha256(data).hexdigest() != meta.sha256:
                logger.warning("%s.bin does not match %s.json; ignoring it", slot, slot)
                return None
            return data, meta

    # --- Internals ------------------------------------------------------------------------
    def _copy_slot(self, source: SlotName, target: SlotName, meta: CurrentImageMeta) -> bool:
        """Copy the files of ``source`` to ``target`` (caller holds the lock)."""
        paths = self.paths(source)
        try:
            data = paths.bin.read_bytes()
            jpeg = paths.jpeg.read_bytes()
            dithered = paths.dithered.read_bytes()
        except FileNotFoundError:
            logger.warning("The %s image files are missing; cannot copy them", source)
            return False
        if hashlib.sha256(data).hexdigest() != meta.sha256:
            logger.warning("%s.bin does not match %s.json; cannot copy it", source, source)
            return False
        self._write_slot(target, data, jpeg, dithered, meta)
        return True

    def _write_slot(
        self, slot: SlotName, data: bytes, jpeg: bytes, dithered: bytes, meta: CurrentImageMeta
    ) -> None:
        paths = self.paths(slot)
        atomic_write_bytes(paths.bin, data)
        atomic_write_bytes(paths.jpeg, jpeg)
        atomic_write_bytes(paths.dithered, dithered)
        atomic_write_json(paths.meta, meta.model_dump(mode="json"))
        self._metas[slot] = meta

    def _load_verified(self, slot: SlotName) -> CurrentImageMeta | None:
        paths = self.paths(slot)
        raw = read_json(paths.meta, lambda: None)
        if raw is None:
            return None
        try:
            meta = CurrentImageMeta.model_validate(raw)
        except ValidationError:
            logger.warning("Ignoring invalid %s", paths.meta)
            return None
        try:
            data = paths.bin.read_bytes()
        except FileNotFoundError:
            logger.warning("%s is missing; no %s image", paths.bin, slot)
            return None
        if len(data) != meta.bin_size or hashlib.sha256(data).hexdigest() != meta.sha256:
            logger.warning("%s.bin does not match %s.json; no %s image", slot, slot, slot)
            return None
        return meta
