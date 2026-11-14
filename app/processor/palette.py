"""Spectra 6 palette, device codes and the official tuned color-distance metric.

The palette order, 4-bit device codes and the RGB + luma distance metric are taken
from the official Fraimic converter (``convert_to_bin_spectra6.py``,
https://github.com/Fraimic/fraimic_bin_converter, MIT License, Copyright (c) 2026
Fraimic), which itself builds on Toon-nooT's PhotoPainter Spectra 6 converter.

:func:`match_palette` is a vectorized, numerically identical version of the official
``closest_palette_color``: it performs the same float64 operations in the same order,
so it selects the same palette entry for every input (verified by the test suite).
"""

from __future__ import annotations

from typing import Final

import numpy as np
import numpy.typing as npt

#: Palette colors in index order (index ``i`` is encoded as ``DEVICE_CODES[i]``).
PALETTE_RGB: Final[tuple[tuple[int, int, int], ...]] = (
    (0, 0, 0),  # Black
    (255, 255, 255),  # White
    (255, 255, 0),  # Yellow
    (255, 0, 0),  # Red
    (0, 0, 255),  # Blue
    (0, 255, 0),  # Green
)
PALETTE_NAMES: Final[tuple[str, ...]] = ("black", "white", "yellow", "red", "blue", "green")

#: 4-bit panel codes per palette index. ``0x4`` is intentionally skipped by the vendor.
DEVICE_CODES: Final[npt.NDArray[np.uint8]] = np.array([0x0, 0x1, 0x2, 0x3, 0x5, 0x6], np.uint8)
DEVICE_CODES.setflags(write=False)
ALLOWED_CODES: Final[frozenset[int]] = frozenset(int(code) for code in DEVICE_CODES)

#: White device code, used for the EL315 off-panel padding nibbles (``0x11`` bytes).
WHITE_CODE: Final = 0x1

#: Palette as float32, the dtype the official converter diffuses errors with.
PALETTE_ARRAY: Final[npt.NDArray[np.float32]] = np.array(PALETTE_RGB, dtype=np.float32)
PALETTE_ARRAY.setflags(write=False)

#: Perceptual luma per palette entry (weights 0.25 / 0.35 / 0.40, as officially tuned).
PALETTE_LUMA: Final[npt.NDArray[np.float32]] = np.array(
    [r * 250 + g * 350 + b * 400 for (r, g, b) in PALETTE_RGB], dtype=np.float32
) / (255.0 * 1000)
PALETTE_LUMA.setflags(write=False)

_RED: Final = PALETTE_ARRAY[:, 0]
_GREEN: Final = PALETTE_ARRAY[:, 1]
_BLUE: Final = PALETTE_ARRAY[:, 2]


def match_palette(rgb: npt.NDArray[np.int64]) -> npt.NDArray[np.intp]:
    """Return the closest palette index for each RGB triple.

    Args:
        rgb: ``(N, 3)`` integer array with channel values in ``0..255`` (the official
            converter clips and truncates the error-diffused value before matching).

    Returns:
        ``(N,)`` array of palette indices in ``0..5``.
    """
    red = rgb[:, 0:1]
    green = rgb[:, 1:2]
    blue = rgb[:, 2:3]
    luma = (red * 250 + green * 350 + blue * 400) / (255.0 * 1000)

    diff_r = red - _RED
    diff_g = green - _GREEN
    diff_b = blue - _BLUE
    # Blue boosted, green and red reduced: compensates eye sensitivity and panel response.
    rgb_dist = (
        (diff_r * diff_r * 0.250 + diff_g * diff_g * 0.350 + diff_b * diff_b * 0.400)
        * 0.75
        / (255.0 * 255.0)
    )
    luma_diff = luma - PALETTE_LUMA
    total = 1.5 * rgb_dist + 0.60 * (luma_diff * luma_diff)
    return np.argmin(total, axis=1)


def indices_to_rgb(indices: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Render palette indices as an RGB image array (used for dithered previews)."""
    lut = np.array(PALETTE_RGB, dtype=np.uint8)
    return lut[indices]
