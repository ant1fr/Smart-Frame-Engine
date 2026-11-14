"""Exact, NumPy-vectorized error-diffusion dithering onto the Spectra 6 palette.

Error diffusion is sequential by nature: every pixel depends on the quantization
error of the pixels processed before it. Both supported kernels, however, only push
error to the right neighbour ``(y, x+1)`` and to the three pixels below
``(y+1, x-1..x+1)``. A pixel therefore only depends on pixels with a strictly smaller
wavefront key ``t = x + 2y``, so all pixels sharing the same ``t`` (an anti-diagonal)
can be quantized together in one vectorized step. This turns ``W * H`` Python
iterations into ``W + 2H`` NumPy steps (about 0.7 s for 1200x1600 instead of ~40 s).

Within a step the down-left contribution is applied before the right contribution,
which reproduces the floating-point summation order of the sequential row-by-row
loop. The result is bit-identical to the official Fraimic converter's Atkinson
implementation (``quantize_atkinson_indexed``), as verified by the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image

from app.core.config_models import DitherAlgorithm
from app.processor.palette import PALETTE_ARRAY, match_palette


@dataclass(frozen=True, slots=True)
class DitherTap:
    """One error-diffusion target relative to the current pixel."""

    dy: int
    dx: int
    weight: float


@dataclass(frozen=True, slots=True)
class DitherKernel:
    """An error-diffusion kernel compatible with the ``t = x + 2y`` wavefront.

    ``taps`` are listed in the order they must be applied within a wavefront step.
    """

    name: str
    taps: tuple[DitherTap, ...]

    def __post_init__(self) -> None:
        for tap in self.taps:
            forward_in_row = tap.dy == 0 and tap.dx >= 1
            next_row = tap.dy == 1 and tap.dx >= -1
            if not (forward_in_row or next_row):
                raise ValueError(
                    f"Tap ({tap.dy}, {tap.dx}) is incompatible with the x + 2y wavefront"
                )


#: The official Fraimic/PhotoPainter Atkinson variant: 4 neighbours, 5/8 of the error.
ATKINSON: Final = DitherKernel(
    "atkinson",
    (
        DitherTap(1, -1, 1 / 8),  # down-left (must precede "right": same target step)
        DitherTap(0, 1, 1 / 8),  # right
        DitherTap(1, 0, 1 / 4),  # down
        DitherTap(1, 1, 1 / 8),  # down-right
    ),
)

#: Classic Floyd-Steinberg (7/16, 3/16, 5/16, 1/16) with the tuned palette metric.
FLOYD_STEINBERG: Final = DitherKernel(
    "floyd_steinberg",
    (
        DitherTap(1, -1, 3 / 16),
        DitherTap(0, 1, 7 / 16),
        DitherTap(1, 0, 5 / 16),
        DitherTap(1, 1, 1 / 16),
    ),
)

KERNELS: Final[dict[DitherAlgorithm, DitherKernel]] = {
    "atkinson": ATKINSON,
    "floyd_steinberg": FLOYD_STEINBERG,
}


def dither(
    rgb: npt.NDArray[np.uint8], algorithm: DitherAlgorithm = "atkinson"
) -> npt.NDArray[np.uint8]:
    """Quantize an RGB image to Spectra 6 palette indices with error diffusion.

    Args:
        rgb: ``(H, W, 3)`` ``uint8`` image.
        algorithm: ``"atkinson"`` (official default) or ``"floyd_steinberg"``.

    Returns:
        ``(H, W)`` ``uint8`` array of palette indices in ``0..5``.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.shape[0] < 1 or rgb.shape[1] < 1:
        raise ValueError(f"Expected an (H, W, 3) RGB array, got shape {rgb.shape}")
    kernel = KERNELS[algorithm]
    height, width = int(rgb.shape[0]), int(rgb.shape[1])

    # Float32 working buffer, exactly like the official converter.
    work = rgb.reshape(-1, 3).astype(np.float32)
    out = np.empty(height * width, dtype=np.uint8)
    taps = [(tap.dy, tap.dx, tap.dy * width + tap.dx, tap.weight) for tap in kernel.taps]

    for step in range(width + 2 * (height - 1)):
        # Pixels with x + 2y == step: y in [y_min, y_max], x = step - 2y in [0, width).
        y_min = max(0, (step - width + 2) // 2)
        y_max = min(height - 1, step // 2)
        if y_min > y_max:
            continue
        ys = np.arange(y_min, y_max + 1)
        xs = step - 2 * ys
        flat = ys * width + xs

        old = work[flat]
        chosen = match_palette(np.clip(old, 0, 255).astype(np.int64))
        new = PALETTE_ARRAY[chosen]
        out[flat] = chosen
        work[flat] = new
        error = old - new

        for dy, dx, offset, weight in taps:
            valid = (ys + dy < height) & (xs + dx >= 0) & (xs + dx < width)
            if valid.all():
                work[flat + offset] += error * weight
            elif valid.any():
                work[flat[valid] + offset] += error[valid] * weight

    return out.reshape(height, width)


def dither_image(
    image: Image.Image, algorithm: DitherAlgorithm = "atkinson"
) -> npt.NDArray[np.uint8]:
    """Convenience wrapper around :func:`dither` for Pillow images."""
    return dither(np.asarray(image.convert("RGB"), dtype=np.uint8), algorithm)
