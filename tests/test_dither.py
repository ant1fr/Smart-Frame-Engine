from __future__ import annotations

import time

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image

from app.processor.dither import (
    ATKINSON,
    FLOYD_STEINBERG,
    DitherKernel,
    DitherTap,
    dither,
    dither_image,
)
from app.processor.palette import PALETTE_ARRAY, PALETTE_RGB
from tests.reference import fraimic_converter as ref


def sequential_reference(rgb: npt.NDArray[np.uint8], kernel: DitherKernel) -> npt.NDArray[np.uint8]:
    """Plain row-by-row error diffusion with the official per-pixel metric."""
    work = rgb.astype(np.float32)
    height, width, _ = work.shape
    out = np.zeros((height, width), dtype=np.uint8)
    # Classic application order: right first, then the row below left-to-right.
    ordered = sorted(kernel.taps, key=lambda tap: (tap.dy, tap.dx))
    for y in range(height):
        for x in range(width):
            old = work[y, x].copy()
            idx = ref.closest_palette_color(tuple(np.clip(old, 0, 255).astype(int)))
            new = PALETTE_ARRAY[idx]
            work[y, x] = new
            out[y, x] = idx
            error = old - new
            for tap in ordered:
                ny, nx = y + tap.dy, x + tap.dx
                if 0 <= ny < height and 0 <= nx < width:
                    work[ny, nx] += error * tap.weight
    return out


def random_image(height: int, width: int, seed: int) -> npt.NDArray[np.uint8]:
    return np.random.default_rng(seed).integers(0, 256, (height, width, 3), dtype=np.uint8)


def gradient_image(height: int, width: int) -> npt.NDArray[np.uint8]:
    gx, gy = np.meshgrid(np.linspace(0, 255, width), np.linspace(0, 255, height))
    return np.dstack([gx, gy, np.full_like(gx, 128)]).astype(np.uint8)


CASES = [
    pytest.param(random_image(23, 37, 1), id="random-37x23"),
    pytest.param(random_image(48, 64, 2), id="random-64x48"),
    pytest.param(random_image(61, 17, 3), id="random-17x61"),
    pytest.param(random_image(1, 50, 4), id="single-row"),
    pytest.param(random_image(50, 1, 5), id="single-column"),
    pytest.param(random_image(1, 1, 6), id="single-pixel"),
    pytest.param(random_image(2, 2, 7), id="2x2"),
    pytest.param(gradient_image(90, 120), id="gradient"),
]


@pytest.mark.parametrize("rgb", CASES)
def test_atkinson_is_bit_identical_to_official(rgb: npt.NDArray[np.uint8]) -> None:
    expected = ref.quantize_atkinson_indexed(Image.fromarray(rgb))
    assert np.array_equal(dither(rgb, "atkinson"), expected)


@pytest.mark.parametrize("rgb", CASES)
def test_floyd_steinberg_matches_sequential_loop(rgb: npt.NDArray[np.uint8]) -> None:
    expected = sequential_reference(rgb, FLOYD_STEINBERG)
    assert np.array_equal(dither(rgb, "floyd_steinberg"), expected)


def test_sequential_reference_agrees_with_official_atkinson() -> None:
    rgb = random_image(20, 30, 9)
    expected = ref.quantize_atkinson_indexed(Image.fromarray(rgb))
    assert np.array_equal(sequential_reference(rgb, ATKINSON), expected)


@pytest.mark.parametrize("algorithm", ["atkinson", "floyd_steinberg"])
def test_output_range_and_dtype(algorithm: str) -> None:
    result = dither(random_image(40, 30, 11), algorithm)  # type: ignore[arg-type]
    assert result.dtype == np.uint8
    assert result.shape == (40, 30)
    assert set(np.unique(result).tolist()) <= set(range(6))


@pytest.mark.parametrize("algorithm", ["atkinson", "floyd_steinberg"])
@pytest.mark.parametrize("index", range(6))
def test_flat_palette_colors_stay_flat(algorithm: str, index: int) -> None:
    rgb = np.full((16, 24, 3), PALETTE_RGB[index], dtype=np.uint8)
    assert np.all(dither(rgb, algorithm) == index)  # type: ignore[arg-type]


@pytest.mark.parametrize("algorithm", ["atkinson", "floyd_steinberg"])
@pytest.mark.parametrize("level", [64, 128, 192])
def test_flat_grey_preserves_average_color(algorithm: str, level: int) -> None:
    """Greys are rendered as color mixes (e.g. yellow + blue) whose average stays grey."""
    rgb = np.full((64, 64, 3), level, dtype=np.uint8)
    rendered = np.array(PALETTE_RGB, dtype=np.float64)[dither(rgb, algorithm)]  # type: ignore[arg-type]
    mean = rendered.reshape(-1, 3).mean(axis=0)
    assert np.all(np.abs(mean - level) < 25), mean.round(1).tolist()


def test_dither_image_accepts_pillow_images() -> None:
    image = Image.fromarray(random_image(10, 12, 3)).convert("RGBA")
    assert dither_image(image).shape == (10, 12)


@pytest.mark.parametrize("shape", [(10, 10), (10, 10, 4), (0, 5, 3)])
def test_rejects_bad_shapes(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        dither(np.zeros(shape, dtype=np.uint8))


def test_kernel_validation() -> None:
    with pytest.raises(ValueError, match="wavefront"):
        DitherKernel("bad", (DitherTap(1, -2, 0.5),))
    with pytest.raises(ValueError, match="wavefront"):
        DitherKernel("bad", (DitherTap(2, 0, 0.5),))


@pytest.mark.slow
@pytest.mark.parametrize(("width", "height", "budget_s"), [(1200, 1600, 5.0), (1440, 2560, 8.0)])
def test_full_panel_performance(width: int, height: int, budget_s: float) -> None:
    rgb = random_image(height, width, 42)
    started = time.perf_counter()
    result = dither(rgb, "atkinson")
    elapsed = time.perf_counter() - started
    assert result.shape == (height, width)
    assert elapsed < budget_s, f"{width}x{height} took {elapsed:.2f}s"
