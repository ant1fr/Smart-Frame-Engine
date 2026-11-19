"""Spectra 6 dithering: exact NumPy wavefront diffusion, compiled kernels and blue noise.

Algorithms (all quantize onto the six panel colors with the official tuned color metric):

* ``atkinson`` - the official Fraimic converter's 4-neighbour variant (5/8 of the error).
  **Bit-identical** to the official converter, as verified by the test suite.
* ``atkinson-classic`` - the original 6-neighbour Atkinson (3/4 of the error): crisp
  edges and clean flats, ideal for graphics, covers and title cards.
* ``fs`` - classic Floyd-Steinberg. ``fs-serpentine`` alternates the scan direction on every
  row, which removes the directional "worm" patterns of plain FS.
* ``stucki`` - a wide 12-neighbour kernel: smoother tonal transitions in skies and gradients.
* ``blue-noise`` - ordered dithering against a tileable blue-noise threshold map: no error
  diffusion, so it runs in milliseconds on any hardware, with a film-grain look.

Speed. Error diffusion is sequential by nature, but a *causal* kernel (error only goes
forward: right, and below) only depends on pixels with a strictly smaller wavefront key
``t = x + a*y`` (``a`` = :attr:`DitherKernel.skew`; 2 for FS/Atkinson, 3 for Stucki), so all
pixels of one anti-diagonal are quantized together in one vectorized NumPy step. That turns
``W * H`` Python iterations into ``W + a*H`` steps (about 0.7 s for 1200x1600 instead of
~40 s). Serpentine scanning breaks the wavefront (each row depends on the end of the
previous one), so ``fs-serpentine`` - and ``stucki``, where it is 20x faster - run as
:mod:`numba`-compiled loops (tens of milliseconds on x86). If numba cannot be imported,
``stucki`` falls back to the wavefront and ``fs-serpentine`` to ``fs``.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Final

import numpy as np
import numpy.typing as npt
from PIL import Image

from app.core.config_models import ConcreteDither, DitherAlgorithm
from app.processor.palette import PALETTE_ARRAY, PALETTE_LUMA, match_palette

logger = logging.getLogger(__name__)

# --- Optional numba --------------------------------------------------------------------------
os.environ.setdefault(
    "NUMBA_CACHE_DIR", os.path.join(os.environ.get("DATA_DIR", "/data"), ".numba_cache")
)


def _import_numba() -> Any:
    try:
        import numba as numba_module
    except Exception:  # ImportError, or llvmlite failing to load on an exotic platform
        return None
    return numba_module


numba: Any = _import_numba()
NUMBA_AVAILABLE: bool = numba is not None


def _jit(function: Callable[..., Any]) -> Callable[..., Any]:
    """Compile ``function`` with numba, caching to disk when a cache directory is writable."""
    if numba is None:
        return function
    try:
        compiled: Callable[..., Any] = numba.njit(cache=True, nogil=True)(function)
    except Exception:  # no writable cache locator: compile in memory
        compiled = numba.njit(nogil=True)(function)
    return compiled


# --- Kernels ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DitherTap:
    """One error-diffusion target relative to the current pixel."""

    dy: int
    dx: int
    weight: float


@dataclass(frozen=True, slots=True)
class DitherKernel:
    """A causal error-diffusion kernel.

    ``taps`` are listed in the order they are applied within a wavefront step.
    """

    name: str
    taps: tuple[DitherTap, ...]

    def __post_init__(self) -> None:
        for tap in self.taps:
            forward_in_row = tap.dy == 0 and tap.dx >= 1
            below = tap.dy >= 1
            if not (forward_in_row or below):
                raise ValueError(
                    f"Tap ({tap.dy}, {tap.dx}) is not causal: the wavefront needs error to "
                    "go right or to later rows"
                )

    @property
    def skew(self) -> int:
        """Smallest ``a`` such that every tap moves forward on ``t = x + a*y``."""
        skew = 1
        for tap in self.taps:
            if tap.dy >= 1:
                skew = max(skew, (-tap.dx) // tap.dy + 1)
        return skew


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

#: The original Atkinson: 6 neighbours of 1/8 each (3/4 of the error is diffused).
ATKINSON_CLASSIC: Final = DitherKernel(
    "atkinson-classic",
    (
        DitherTap(1, -1, 1 / 8),
        DitherTap(0, 1, 1 / 8),
        DitherTap(1, 0, 1 / 8),
        DitherTap(1, 1, 1 / 8),
        DitherTap(0, 2, 1 / 8),
        DitherTap(2, 0, 1 / 8),
    ),
)

#: Classic Floyd-Steinberg (7/16, 3/16, 5/16, 1/16) with the tuned palette metric.
FLOYD_STEINBERG: Final = DitherKernel(
    "fs",
    (
        DitherTap(1, -1, 3 / 16),
        DitherTap(0, 1, 7 / 16),
        DitherTap(1, 0, 5 / 16),
        DitherTap(1, 1, 1 / 16),
    ),
)

#: Stucki: 12 neighbours over two rows.
STUCKI: Final = DitherKernel(
    "stucki",
    (
        DitherTap(1, -2, 2 / 42),
        DitherTap(1, -1, 4 / 42),
        DitherTap(0, 1, 8 / 42),
        DitherTap(0, 2, 4 / 42),
        DitherTap(1, 0, 8 / 42),
        DitherTap(1, 1, 4 / 42),
        DitherTap(1, 2, 2 / 42),
        DitherTap(2, -2, 1 / 42),
        DitherTap(2, -1, 2 / 42),
        DitherTap(2, 0, 4 / 42),
        DitherTap(2, 1, 2 / 42),
        DitherTap(2, 2, 1 / 42),
    ),
)

KERNELS: Final[dict[str, DitherKernel]] = {
    "atkinson": ATKINSON,
    "atkinson-classic": ATKINSON_CLASSIC,
    "fs": FLOYD_STEINBERG,
    "floyd_steinberg": FLOYD_STEINBERG,
    "stucki": STUCKI,
}
#: Algorithms that need the compiled serpentine loop.
SERPENTINE: Final = frozenset({"fs-serpentine"})


def _check(rgb: npt.NDArray[np.uint8]) -> tuple[int, int]:
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.shape[0] < 1 or rgb.shape[1] < 1:
        raise ValueError(f"Expected an (H, W, 3) RGB array, got shape {rgb.shape}")
    return int(rgb.shape[0]), int(rgb.shape[1])


# --- NumPy wavefront (exact, no extra dependency) --------------------------------------------
def _wavefront(rgb: npt.NDArray[np.uint8], kernel: DitherKernel) -> npt.NDArray[np.uint8]:
    height, width = _check(rgb)
    skew = kernel.skew

    # Float32 working buffer, exactly like the official converter.
    work = rgb.reshape(-1, 3).astype(np.float32)
    out = np.empty(height * width, dtype=np.uint8)
    taps = [(tap.dy, tap.dx, tap.dy * width + tap.dx, tap.weight) for tap in kernel.taps]

    for step in range(width + skew * (height - 1)):
        # Pixels with x + skew*y == step: x = step - skew*y must lie in [0, width).
        y_min = max(0, (step - width + skew) // skew)
        y_max = min(height - 1, step // skew)
        if y_min > y_max:
            continue
        ys = np.arange(y_min, y_max + 1)
        xs = step - skew * ys
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


# --- Compiled sequential loop (serpentine, Stucki) -------------------------------------------
_PAL_R: Final = PALETTE_ARRAY[:, 0].astype(np.float64)
_PAL_G: Final = PALETTE_ARRAY[:, 1].astype(np.float64)
_PAL_B: Final = PALETTE_ARRAY[:, 2].astype(np.float64)
_PAL_LUMA: Final = PALETTE_LUMA.astype(np.float64)


def _diffuse_loop(
    work: Any,
    pal_r: Any,
    pal_g: Any,
    pal_b: Any,
    pal_luma: Any,
    pal_rgb: Any,
    dys: Any,
    dxs: Any,
    weights: Any,
    serpentine: Any,
    out: Any,
) -> None:
    """Sequential error diffusion with the official metric (compiled by numba)."""
    height = work.shape[0]
    width = work.shape[1]
    taps = dys.shape[0]
    for y in range(height):
        flip = serpentine and (y % 2 == 1)
        for i in range(width):
            x = width - 1 - i if flip else i
            old_r = work[y, x, 0]
            old_g = work[y, x, 1]
            old_b = work[y, x, 2]
            red = float(int(min(max(old_r, 0.0), 255.0)))
            green = float(int(min(max(old_g, 0.0), 255.0)))
            blue = float(int(min(max(old_b, 0.0), 255.0)))
            luma = (red * 250 + green * 350 + blue * 400) / (255.0 * 1000)
            best = 0
            best_total = 1e30
            for index in range(6):
                dr = red - pal_r[index]
                dg = green - pal_g[index]
                db = blue - pal_b[index]
                rgb_dist = (
                    (dr * dr * 0.250 + dg * dg * 0.350 + db * db * 0.400) * 0.75 / (255.0 * 255.0)
                )
                luma_diff = luma - pal_luma[index]
                total = 1.5 * rgb_dist + 0.60 * (luma_diff * luma_diff)
                if total < best_total:
                    best_total = total
                    best = index
            out[y, x] = best
            err_r = old_r - pal_rgb[best, 0]
            err_g = old_g - pal_rgb[best, 1]
            err_b = old_b - pal_rgb[best, 2]
            for tap in range(taps):
                ny = y + dys[tap]
                dx = -dxs[tap] if flip else dxs[tap]
                nx = x + dx
                if ny < height and nx >= 0 and nx < width:
                    work[ny, nx, 0] += err_r * weights[tap]
                    work[ny, nx, 1] += err_g * weights[tap]
                    work[ny, nx, 2] += err_b * weights[tap]


_DIFFUSE: Final = _jit(_diffuse_loop) if NUMBA_AVAILABLE else None


def _compiled(
    rgb: npt.NDArray[np.uint8], kernel: DitherKernel, *, serpentine: bool
) -> npt.NDArray[np.uint8]:
    height, width = _check(rgb)
    work = np.ascontiguousarray(rgb, dtype=np.float32)
    out = np.empty((height, width), dtype=np.uint8)
    if _DIFFUSE is None:
        raise RuntimeError("The compiled dithering kernel needs numba")
    _DIFFUSE(
        work,
        _PAL_R,
        _PAL_G,
        _PAL_B,
        _PAL_LUMA,
        PALETTE_ARRAY,
        np.array([tap.dy for tap in kernel.taps], dtype=np.int64),
        np.array([tap.dx for tap in kernel.taps], dtype=np.int64),
        np.array([tap.weight for tap in kernel.taps], dtype=np.float32),
        serpentine,
        out,
    )
    return out


# --- Blue-noise ordered dithering ------------------------------------------------------------
BLUE_NOISE_FILE: Final = "blue_noise_64.npy"


@lru_cache(maxsize=1)
def blue_noise_thresholds() -> npt.NDArray[np.float32]:
    """The bundled 64x64 tileable blue-noise tile as thresholds in ``(0, 255)``."""
    resource = resources.files("app").joinpath("assets", BLUE_NOISE_FILE)
    ranks = np.load(io.BytesIO(resource.read_bytes()))
    return np.asarray((ranks.astype(np.float32) + 0.5) / ranks.size * 255.0, dtype=np.float32)


@lru_cache(maxsize=1)
def _corner_lut() -> npt.NDArray[np.uint8]:
    """Palette index of each RGB-cube corner (``r*4 + g*2 + b``); cyan and magenta map to
    their nearest panel color."""
    corners = np.array(
        [[(code >> 2) & 1, (code >> 1) & 1, code & 1] for code in range(8)], dtype=np.int64
    )
    return np.asarray(match_palette(corners * 255), dtype=np.uint8)


def _blue_noise(rgb: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    height, width = _check(rgb)
    tile = blue_noise_thresholds()
    rows = np.arange(height) % tile.shape[0]
    cols = np.arange(width) % tile.shape[1]
    threshold = tile[rows[:, None], cols[None, :]]
    bits = rgb > threshold[:, :, None]  # one shared mask: greys become black/white stipple
    code = bits[..., 0] * 4 + bits[..., 1] * 2 + bits[..., 2]
    return np.asarray(_corner_lut()[code], dtype=np.uint8)


# --- Public API ------------------------------------------------------------------------------
def effective_algorithm(algorithm: ConcreteDither) -> ConcreteDither:
    """The algorithm that will really run (``fs-serpentine`` needs numba, else ``fs``)."""
    if algorithm in SERPENTINE and not NUMBA_AVAILABLE:
        return "fs"
    return algorithm


def dither(
    rgb: npt.NDArray[np.uint8], algorithm: DitherAlgorithm = "atkinson"
) -> npt.NDArray[np.uint8]:
    """Quantize an RGB image to Spectra 6 palette indices.

    Args:
        rgb: ``(H, W, 3)`` ``uint8`` image.
        algorithm: A concrete algorithm (see the module docstring); ``"auto"`` must be
            resolved by the caller (:mod:`app.processor.auto`).

    Returns:
        ``(H, W)`` ``uint8`` array of palette indices in ``0..5``.
    """
    _check(rgb)
    if algorithm == "auto":
        raise ValueError('"auto" must be resolved to a concrete dither algorithm first')
    name = effective_algorithm(algorithm)
    if name != algorithm:
        logger.warning("numba is unavailable: using %s instead of %s", name, algorithm)
    if name == "blue-noise":
        return _blue_noise(rgb)
    if name == "fs-serpentine":
        return _compiled(rgb, FLOYD_STEINBERG, serpentine=True)
    if name == "stucki" and NUMBA_AVAILABLE:
        return _compiled(rgb, STUCKI, serpentine=False)
    return _wavefront(rgb, KERNELS[name])


def dither_image(
    image: Image.Image, algorithm: DitherAlgorithm = "atkinson"
) -> npt.NDArray[np.uint8]:
    """Convenience wrapper around :func:`dither` for Pillow images."""
    return dither(np.asarray(image.convert("RGB"), dtype=np.uint8), algorithm)


def warm_up() -> None:
    """Compile (and cache) the numba kernels so the first real render is fast."""
    sample = np.zeros((8, 8, 3), dtype=np.uint8)
    algorithms: tuple[ConcreteDither, ...] = ("fs-serpentine", "stucki")
    for algorithm in algorithms:
        dither(sample, algorithm)
