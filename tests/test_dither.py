from __future__ import annotations

import time

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image

from app.processor import dither as dither_module
from app.processor.dither import (
    ATKINSON,
    ATKINSON_CLASSIC,
    FLOYD_STEINBERG,
    NUMBA_AVAILABLE,
    STUCKI,
    DitherKernel,
    DitherTap,
    _compiled,
    _wavefront,
    blue_noise_thresholds,
    dither,
    dither_image,
    effective_algorithm,
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


ALL_ALGORITHMS = [
    "atkinson",
    "atkinson-classic",
    "fs",
    "floyd_steinberg",
    "fs-serpentine",
    "stucki",
    "blue-noise",
]

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


@pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
def test_output_range_and_dtype(algorithm: str) -> None:
    result = dither(random_image(40, 30, 11), algorithm)  # type: ignore[arg-type]
    assert result.dtype == np.uint8
    assert result.shape == (40, 30)
    assert set(np.unique(result).tolist()) <= set(range(6))


@pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
@pytest.mark.parametrize("index", range(6))
def test_flat_palette_colors_stay_flat(algorithm: str, index: int) -> None:
    rgb = np.full((16, 24, 3), PALETTE_RGB[index], dtype=np.uint8)
    assert np.all(dither(rgb, algorithm) == index)  # type: ignore[arg-type]


@pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
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
    for backwards in (DitherTap(0, -1, 0.5), DitherTap(0, 0, 0.5), DitherTap(-1, 0, 0.5)):
        with pytest.raises(ValueError, match="causal"):
            DitherKernel("bad", (backwards,))
    DitherKernel("wide", (DitherTap(1, -3, 0.5), DitherTap(2, 0, 0.5)))  # any forward tap


def test_wavefront_skew_follows_the_widest_down_left_tap() -> None:
    assert ATKINSON.skew == FLOYD_STEINBERG.skew == ATKINSON_CLASSIC.skew == 2
    assert STUCKI.skew == 3
    assert DitherKernel("right-only", (DitherTap(0, 1, 1.0),)).skew == 1


@pytest.mark.skipif(not NUMBA_AVAILABLE, reason="needs numba")
@pytest.mark.parametrize(
    "kernel", [FLOYD_STEINBERG, STUCKI, ATKINSON_CLASSIC], ids=lambda k: k.name
)
@pytest.mark.parametrize("rgb", CASES)
def test_compiled_loop_matches_the_wavefront(
    kernel: DitherKernel, rgb: npt.NDArray[np.uint8]
) -> None:
    assert np.array_equal(_compiled(rgb, kernel, serpentine=False), _wavefront(rgb, kernel))


@pytest.mark.parametrize("kernel", [STUCKI, ATKINSON_CLASSIC], ids=lambda k: k.name)
@pytest.mark.parametrize("rgb", CASES)
def test_wide_kernels_match_the_sequential_loop(
    kernel: DitherKernel, rgb: npt.NDArray[np.uint8]
) -> None:
    """Only the float summation order of overlapping taps may differ (rare pixel flips)."""
    expected = sequential_reference(rgb, kernel)
    assert (_wavefront(rgb, kernel) == expected).mean() > 0.995


@pytest.mark.skipif(not NUMBA_AVAILABLE, reason="needs numba")
def test_serpentine_differs_from_plain_fs_but_keeps_the_mean() -> None:
    rgb = gradient_image(60, 80)
    plain = dither(rgb, "fs")
    serpentine = dither(rgb, "fs-serpentine")
    assert not np.array_equal(plain, serpentine)
    palette = np.array(PALETTE_RGB, dtype=np.float64)
    assert np.allclose(
        palette[plain].reshape(-1, 3).mean(axis=0),
        palette[serpentine].reshape(-1, 3).mean(axis=0),
        atol=6,
    )
    # Row 0 scans left to right like FS; row 1 runs right to left.
    assert np.array_equal(plain[0], serpentine[0])


def test_fs_serpentine_falls_back_to_fs_without_numba(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dither_module, "NUMBA_AVAILABLE", False)
    rgb = random_image(30, 40, 12)
    assert effective_algorithm("fs-serpentine") == "fs"
    assert effective_algorithm("stucki") == "stucki"
    assert np.array_equal(dither(rgb, "fs-serpentine"), dither(rgb, "fs"))
    assert np.array_equal(dither(rgb, "stucki"), _wavefront(rgb, STUCKI))


def test_auto_must_be_resolved_before_dithering() -> None:
    with pytest.raises(ValueError, match="auto"):
        dither(random_image(4, 4, 1), "auto")


def test_blue_noise_tile_is_a_permutation_with_even_density() -> None:
    thresholds = blue_noise_thresholds()
    assert thresholds.shape == (64, 64)
    assert np.unique(thresholds).size == 64 * 64
    for level in (0.1, 0.5, 0.9):
        mask = thresholds > level * 255
        blocks = mask.reshape(4, 16, 4, 16).mean(axis=(1, 3))
        assert np.all(np.abs(blocks - (1 - level)) < 0.08), level


def test_blue_noise_greys_are_black_and_white_only_and_tile_seamlessly() -> None:
    result = dither(np.full((130, 150, 3), 128, dtype=np.uint8), "blue-noise")
    assert set(np.unique(result).tolist()) == {0, 1}
    assert 0.45 < (result == 1).mean() < 0.55
    assert np.array_equal(result[:64, :64], result[64:128, 64:128])  # tiled


def test_blue_noise_maps_cyan_and_magenta_to_panel_colors() -> None:
    cyan = dither(np.full((8, 8, 3), (0, 255, 255), dtype=np.uint8), "blue-noise")
    magenta = dither(np.full((8, 8, 3), (255, 0, 255), dtype=np.uint8), "blue-noise")
    assert np.unique(cyan).size == 1 and np.unique(magenta).size == 1
    assert int(cyan[0, 0]) in range(6) and int(magenta[0, 0]) in range(6)


@pytest.mark.slow
@pytest.mark.parametrize("algorithm", ["fs-serpentine", "stucki", "blue-noise", "atkinson-classic"])
@pytest.mark.parametrize(("width", "height", "budget_s"), [(1200, 1600, 5.0), (1440, 2560, 8.0)])
def test_new_algorithms_stay_fast(algorithm: str, width: int, height: int, budget_s: float) -> None:
    rgb = random_image(height, width, 43)
    dither(rgb[:16, :16], algorithm)  # type: ignore[arg-type]  # compile outside the timing
    started = time.perf_counter()
    dither(rgb, algorithm)  # type: ignore[arg-type]
    assert time.perf_counter() - started < budget_s


@pytest.mark.slow
@pytest.mark.parametrize(("width", "height", "budget_s"), [(1200, 1600, 5.0), (1440, 2560, 8.0)])
def test_full_panel_performance(width: int, height: int, budget_s: float) -> None:
    rgb = random_image(height, width, 42)
    started = time.perf_counter()
    result = dither(rgb, "atkinson")
    elapsed = time.perf_counter() - started
    assert result.shape == (height, width)
    assert elapsed < budget_s, f"{width}x{height} took {elapsed:.2f}s"
