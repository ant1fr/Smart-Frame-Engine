"""End-to-end rendering: image bytes -> composed preview -> dithered indices -> ``.bin``.

Steps:

1. Decode, validate, apply the EXIF orientation and flatten transparency onto white.
2. Compose in *viewing* orientation (``frame_size`` honours portrait/landscape):
   ``smart_crop`` (fill, face-aware) or ``contain`` (fit + blur/black/white background),
   with the eInk enhancement applied to the picture only.
3. Dither in viewing orientation (Atkinson or Floyd-Steinberg, tuned metric).
4. Rotate the palette indices losslessly into the panel's native portrait buffer
   (``landscape_rotation`` degrees counter-clockwise) and pack the ``.bin``.

Rendering is CPU-bound and synchronous; call it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageOps

from app.core.config_models import EinkConfig, FitMode, PanelSpec, RuntimeConfig
from app.core.security import verify_image_bytes
from app.processor.dither import dither
from app.processor.enhance import enhance
from app.processor.fit import (
    BackgroundStyle,
    FocusRegion,
    fit_inside,
    make_background,
    paste_centered,
    smart_crop,
)
from app.processor.packing import encode_bin
from app.processor.palette import indices_to_rgb

Rotation = Literal[0, 90, 270]
PREVIEW_JPEG_QUALITY = 90


def decode_image(data: bytes) -> Image.Image:
    """Decode untrusted image bytes into an upright RGB image.

    Raises:
        InvalidImageError: If the data is not a supported, valid image.
    """
    verify_image_bytes(data)
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        upright = ImageOps.exif_transpose(opened)
    has_alpha = upright.mode in {"RGBA", "LA", "PA"} or (
        upright.mode == "P" and "transparency" in upright.info
    )
    if has_alpha:
        rgba = upright.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        return flattened
    return upright.convert("RGB")


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Everything the renderer needs, resolved from the runtime configuration."""

    panel: PanelSpec
    frame_size: tuple[int, int]
    rotation: Rotation
    fit: FitMode
    background: BackgroundStyle
    eink: EinkConfig

    def __post_init__(self) -> None:
        native = (self.panel.width, self.panel.height)
        expected = native if self.rotation == 0 else (native[1], native[0])
        if self.frame_size != expected:
            raise ValueError(
                f"frame_size {self.frame_size} does not match panel {native} "
                f"with rotation {self.rotation}"
            )

    @classmethod
    def from_config(cls, config: RuntimeConfig, *, fit: FitMode | None = None) -> RenderOptions:
        """Build options from the effective config, optionally overriding the fit mode."""
        rotation: Rotation = (
            0 if config.display.orientation == "portrait" else config.display.landscape_rotation
        )
        return cls(
            panel=config.fraimic.spec,
            frame_size=config.frame_size,
            rotation=rotation,
            fit=fit or config.display.default_fit,
            background=config.display.contain_background,
            eink=config.eink,
        )


@dataclass(frozen=True)
class RenderResult:
    """Output of :func:`render`."""

    options: RenderOptions
    composed: Image.Image = field(repr=False)
    indices: npt.NDArray[np.uint8] = field(repr=False)
    bin: bytes = field(repr=False)

    @cached_property
    def sha256(self) -> str:
        """SHA-256 of the ``.bin`` payload (hex)."""
        return hashlib.sha256(self.bin).hexdigest()

    def preview_jpeg(self, quality: int = PREVIEW_JPEG_QUALITY) -> bytes:
        """The enhanced composition (before dithering) as JPEG, in viewing orientation."""
        buffer = io.BytesIO()
        self.composed.save(buffer, format="JPEG", quality=quality, optimize=True)
        return buffer.getvalue()

    def dithered_png(self) -> bytes:
        """Exact 6-color simulation of the panel output, in viewing orientation."""
        buffer = io.BytesIO()
        Image.fromarray(indices_to_rgb(self.indices)).save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def compose(
    image: Image.Image, options: RenderOptions, regions: Sequence[FocusRegion] = ()
) -> Image.Image:
    """Fit and enhance ``image`` into the frame, in viewing orientation."""
    if options.fit == "smart_crop":
        return enhance(smart_crop(image, options.frame_size, regions), options.eink)
    picture = enhance(fit_inside(image, options.frame_size), options.eink)
    if picture.size == options.frame_size:
        return picture
    background = make_background(image, options.frame_size, options.background)
    if options.background == "blur":
        background = enhance(background, options.eink, edge_filters=False)
    return paste_centered(background, picture)


def render(
    image: Image.Image, options: RenderOptions, regions: Sequence[FocusRegion] = ()
) -> RenderResult:
    """Compose, dither and encode ``image`` for the configured panel."""
    composed = compose(image.convert("RGB"), options, regions)
    indices = dither(np.asarray(composed, dtype=np.uint8), options.eink.dither)
    portrait = np.ascontiguousarray(np.rot90(indices, k=options.rotation // 90))
    return RenderResult(
        options=options,
        composed=composed,
        indices=indices,
        bin=encode_bin(portrait, options.panel),
    )


def render_bytes(
    data: bytes, options: RenderOptions, regions: Sequence[FocusRegion] = ()
) -> RenderResult:
    """Decode untrusted bytes and :func:`render` them."""
    return render(decode_image(data), options, regions)
