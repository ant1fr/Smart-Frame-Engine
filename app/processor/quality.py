"""Photo suitability gate for the 6-color eInk panel.

Instead of degrading a poor photo with heavy processing, the gate decides cheaply (on a
~250 px thumbnail and a little metadata) whether a photo is worth showing at all. It never
modifies pixels. The rules and defaults come from a calibration run on ~400 real photos:

* ``blank``      - near-black or flat frames.
* ``tiny``       - images whose short side is below ``min_dimension`` (avatars, icons).
* ``screenshot`` - no camera EXIF and either one bright flat color covering a large share
  of the image (UI, graphics) or a tall phone-screen shape.
* ``document``   - no camera EXIF, washed out and mostly white (scans, pages).

Deliberately *not* rejected: black-and-white portraits (they dither beautifully), night
shots, scanned family photos and low-contrast snow scenes. "No camera EXIF" alone is never
enough because scans and shared pictures lack it.

Progressive relaxation (:data:`LEVELS`) lets callers loosen the rules step by step when a
whole batch of candidates fails, so a request never dead-ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
from PIL import Image

from app.core.config_models import QualityConfig

#: Rule sets, strictest first. Level ``LEVELS`` (and above) means "accept anything".
LEVELS: Final = 4
THUMBNAIL_LIMIT: Final = 256
_BRIGHT_PIXEL: Final = 250


@dataclass(frozen=True, slots=True)
class ImageStats:
    """Cheap pixel statistics of a (thumbnail-sized) image."""

    luma_mean: float
    luma_std: float
    flat_share: float  # % of pixels sharing the most common (5-bit quantized) color
    flat_luma: float  # luma of that most common color
    colorfulness: float  # Hasler-Susstrunk colorfulness
    white_share: float  # % of near-white pixels


@dataclass(frozen=True, slots=True)
class AssetFacts:
    """What the source knows about the picture without looking at pixels."""

    has_camera: bool
    width: int | None = None
    height: int | None = None

    @property
    def short_side(self) -> int | None:
        if not self.width or not self.height:
            return None
        return min(self.width, self.height)

    @property
    def aspect(self) -> float | None:
        if not self.width or not self.height:
            return None
        return max(self.width, self.height) / min(self.width, self.height)


def measure(image: Image.Image) -> ImageStats:
    """Compute :class:`ImageStats` (works on a copy downscaled to at most 256 px)."""
    small = image.convert("RGB")
    small.thumbnail((THUMBNAIL_LIMIT, THUMBNAIL_LIMIT))
    rgb = np.asarray(small, dtype=np.float32)
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    rg = rgb[..., 0] - rgb[..., 1]
    yb = 0.5 * (rgb[..., 0] + rgb[..., 1]) - rgb[..., 2]
    colorfulness = float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))

    quantized = rgb.astype(np.uint8) >> 3
    codes = (
        quantized[..., 0].astype(np.int32) * 1024
        + quantized[..., 1].astype(np.int32) * 32
        + quantized[..., 2].astype(np.int32)
    ).ravel()
    counts = np.bincount(codes)
    top = int(np.argmax(counts))
    flat_rgb = np.array([(top >> 10) & 31, (top >> 5) & 31, top & 31], dtype=np.float32) * 8 + 4
    flat_luma = float(flat_rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32))

    return ImageStats(
        luma_mean=float(luma.mean()),
        luma_std=float(luma.std()),
        flat_share=float(counts[top] / codes.size * 100),
        flat_luma=flat_luma,
        colorfulness=colorfulness,
        white_share=float((rgb >= _BRIGHT_PIXEL).all(axis=2).mean() * 100),
    )


def violations(
    stats: ImageStats, facts: AssetFacts, config: QualityConfig, level: int = 0
) -> list[str]:
    """Rule names the image breaks at relaxation ``level`` (empty list = suitable).

    Levels: ``0`` all rules; ``1`` halves ``min_dimension`` and drops the phone-shape and
    document rules; ``2`` keeps only ``blank`` and the flat-color screenshot rule; ``3``
    keeps only ``blank``; ``4`` and above accept anything.
    """
    if level >= LEVELS:
        return []
    broken: list[str] = []
    if stats.luma_mean < config.blank_luma_mean or stats.luma_std < config.blank_luma_std:
        broken.append("blank")

    short = facts.short_side
    min_dimension = config.min_dimension if level == 0 else config.min_dimension // 2
    if level <= 1 and short is not None and short < min_dimension:
        broken.append("tiny")

    if level <= 2 and not facts.has_camera:
        flat_graphic = (
            stats.flat_share >= config.screenshot_flat_share
            and stats.flat_luma >= config.screenshot_flat_luma
        )
        aspect = facts.aspect
        phone_shape = (
            level == 0
            and aspect is not None
            and short is not None
            and aspect >= config.screenshot_aspect
            and short <= config.screenshot_max_dimension
        )
        if flat_graphic or phone_shape:
            broken.append("screenshot")

    if (
        level == 0
        and not facts.has_camera
        and stats.colorfulness < config.document_colorfulness
        and stats.white_share >= config.document_white_share
    ):
        broken.append("document")
    return broken
