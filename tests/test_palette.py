from __future__ import annotations

import numpy as np

from app.processor.palette import (
    ALLOWED_CODES,
    DEVICE_CODES,
    PALETTE_ARRAY,
    PALETTE_LUMA,
    PALETTE_RGB,
    indices_to_rgb,
    match_palette,
)
from tests.reference import fraimic_converter as ref


def test_device_codes_skip_0x4() -> None:
    assert DEVICE_CODES.tolist() == [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]
    assert sorted(ALLOWED_CODES) == [0, 1, 2, 3, 5, 6]
    assert 0x4 not in ALLOWED_CODES


def test_palette_matches_official_constants() -> None:
    assert list(PALETTE_RGB) == ref.PALETTE_COLORS
    assert np.array_equal(PALETTE_ARRAY, ref.PALETTE_ARRAY)
    assert np.array_equal(PALETTE_LUMA, ref.PALETTE_LUMA_ARRAY)
    assert np.array_equal(DEVICE_CODES, ref.COLOR_CODES)


def test_pure_palette_colors_map_to_themselves() -> None:
    colors = np.array(PALETTE_RGB, dtype=np.int64)
    assert match_palette(colors).tolist() == list(range(6))


def test_match_palette_equals_official_metric() -> None:
    rng = np.random.default_rng(2026)
    colors = rng.integers(0, 256, size=(20_000, 3), dtype=np.int64)
    # Include the grey axis and channel extremes, where ties are most likely.
    grey = np.repeat(np.arange(256, dtype=np.int64)[:, None], 3, axis=1)
    corners = np.array(
        [[r, g, b] for r in (0, 1, 254, 255) for g in (0, 1, 254, 255) for b in (0, 1, 254, 255)],
        dtype=np.int64,
    )
    samples = np.concatenate([colors, grey, corners])

    expected = [int(ref.closest_palette_color(tuple(color))) for color in samples]
    assert match_palette(samples).tolist() == expected


def test_indices_to_rgb() -> None:
    indices = np.array([[0, 1], [4, 5]], dtype=np.uint8)
    rgb = indices_to_rgb(indices)
    assert rgb.shape == (2, 2, 3)
    assert rgb[1, 0].tolist() == [0, 0, 255]
    assert rgb[1, 1].tolist() == [0, 255, 0]
