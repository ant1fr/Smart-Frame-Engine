"""Persistence of the most recently rendered image in ``/data/cache``.

Files:

* ``current.bin`` - the panel frame buffer pushed to the Fraimic frame.
* ``current.jpg`` - the enhanced composition before dithering (Home Assistant, UI).
* ``current_dithered.png`` - exact 6-color simulation of what the panel shows.
* ``current.json`` - metadata, written **last** as the commit marker. It records the
  SHA-256 of ``current.bin``, so a set interrupted mid-write is detected and ignored.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.core.atomic import atomic_write_bytes
from app.core.config_models import DitherAlgorithm, FitMode, PanelModel
from app.core.json_store import atomic_write_json, read_json
from app.processor.pipeline import RenderResult

logger = logging.getLogger(__name__)

BIN_NAME: Final = "current.bin"
JPEG_NAME: Final = "current.jpg"
DITHERED_NAME: Final = "current_dithered.png"
META_NAME: Final = "current.json"


class CurrentImageMeta(BaseModel):
    """Metadata of the current image (served by ``GET /api/status``)."""

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
    sha256: str = Field(description="SHA-256 of current.bin")
    bin_size: int


class CurrentImageStore:
    """Reads and atomically writes the current image set."""

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._lock = threading.Lock()
        self._meta: CurrentImageMeta | None = None

    @property
    def bin_path(self) -> Path:
        """Location of ``current.bin``."""
        return self._dir / BIN_NAME

    @property
    def jpeg_path(self) -> Path:
        """Location of ``current.jpg``."""
        return self._dir / JPEG_NAME

    @property
    def dithered_path(self) -> Path:
        """Location of ``current_dithered.png``."""
        return self._dir / DITHERED_NAME

    @property
    def meta_path(self) -> Path:
        """Location of ``current.json``."""
        return self._dir / META_NAME

    @property
    def meta(self) -> CurrentImageMeta | None:
        """Metadata of the current image, or ``None`` when nothing was rendered yet."""
        return self._meta

    def load(self) -> CurrentImageMeta | None:
        """Load and verify the stored set (call once at startup)."""
        with self._lock:
            self._meta = self._load_verified()
            return self._meta

    def save(
        self,
        result: RenderResult,
        *,
        source: str,
        title: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> CurrentImageMeta:
        """Persist a render result as the current image (blocking; use a thread)."""
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
        )
        jpeg = result.preview_jpeg()
        dithered = result.dithered_png()
        with self._lock:
            atomic_write_bytes(self.bin_path, result.bin)
            atomic_write_bytes(self.jpeg_path, jpeg)
            atomic_write_bytes(self.dithered_path, dithered)
            atomic_write_json(self.meta_path, meta.model_dump(mode="json"))
            self._meta = meta
        logger.info(
            "Saved current image from %s (%s, %d bytes, sha256 %s)",
            source,
            title or "untitled",
            meta.bin_size,
            meta.sha256[:12],
        )
        return meta

    def read_bin(self) -> tuple[bytes, CurrentImageMeta] | None:
        """Return ``current.bin`` and its metadata if both are present and consistent."""
        with self._lock:
            meta = self._meta
            if meta is None:
                return None
            try:
                data = self.bin_path.read_bytes()
            except FileNotFoundError:
                return None
            if hashlib.sha256(data).hexdigest() != meta.sha256:
                logger.warning("current.bin does not match current.json; ignoring it")
                return None
            return data, meta

    def _load_verified(self) -> CurrentImageMeta | None:
        raw = read_json(self.meta_path, lambda: None)
        if raw is None:
            return None
        try:
            meta = CurrentImageMeta.model_validate(raw)
        except ValidationError:
            logger.warning("Ignoring invalid %s", self.meta_path)
            return None
        try:
            data = self.bin_path.read_bytes()
        except FileNotFoundError:
            logger.warning("%s is missing; no current image", self.bin_path)
            return None
        if len(data) != meta.bin_size or hashlib.sha256(data).hexdigest() != meta.sha256:
            logger.warning("current.bin does not match current.json; no current image")
            return None
        return meta
