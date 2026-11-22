"""Black-and-white photos: detection, grey tone, gamma compensation, black/white dithering."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.core.config_models import EinkConfig, RuntimeConfig
from app.processor.auto import ContentStats, decide_dither, decide_profile, measure_content, resolve
from app.processor.dither import dither
from app.processor.enhance import MONO_WHITE, enhance, mono_dither_input, mono_tone
from app.processor.palette import match_palette
from app.processor.pipeline import RenderOptions, render

ALGORITHMS = ("atkinson", "atkinson-classic", "fs", "fs-serpentine", "stucki", "blue-noise")


def grey_ramp(height: int = 40, width: int = 256) -> np.ndarray:
    ramp = np.tile(np.arange(width, dtype=np.uint8), (height, 1))
    return np.dstack([ramp, ramp, ramp])


def grey_photo(seed: int = 0, size: tuple[int, int] = (240, 320)) -> Image.Image:
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (size[1], size[0]), dtype=np.uint8)
    return Image.fromarray(np.dstack([base, base, base]))


def colour_photo(seed: int = 0) -> Image.Image:
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (320, 240, 3), dtype=np.uint8))


def stats(**overrides: Any) -> ContentStats:
    base: dict[str, Any] = {
        "luma_mean": 120.0,
        "luma_std": 55.0,
        "highlight_share": 0.5,
        "black_share": 1.0,
        "flat_share": 5.0,
        "dominant_colors": 300,
        "detail": 12.0,
        "smooth_share": 0.1,
    }
    return ContentStats(**{**base, **overrides})


# --- palette and dithering ----------------------------------------------------------------------
def test_match_palette_can_be_restricted_to_black_and_white() -> None:
    greys = np.array([[v, v, v] for v in range(0, 256, 5)], dtype=np.int64)
    assert set(match_palette(greys, 2).tolist()) <= {0, 1}
    # Without the restriction the tuned metric builds mid-greys from colored inks.
    assert set(match_palette(np.array([[128, 128, 128]], dtype=np.int64)).tolist()) - {0, 1}


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_monochrome_dithering_only_uses_black_and_white(algorithm: str) -> None:
    out = dither(grey_ramp(), algorithm, monochrome=True)  # type: ignore[arg-type]
    assert set(np.unique(out).tolist()) == {0, 1}


# Atkinson deliberately diffuses only part of the error, so it does not conserve the tone.
@pytest.mark.parametrize("algorithm", ("fs", "fs-serpentine", "stucki", "blue-noise"))
def test_monochrome_dithering_preserves_the_average_tone(algorithm: str) -> None:
    flat = np.full((64, 64, 3), 64, dtype=np.uint8)
    white_share = float(dither(flat, algorithm, monochrome=True).mean())  # type: ignore[arg-type]
    assert abs(white_share - 64 / 255) < 0.06


def test_colour_dithering_is_unchanged_by_the_monochrome_option() -> None:
    rgb = np.random.default_rng(3).integers(0, 256, (30, 40, 3), dtype=np.uint8)
    assert np.array_equal(dither(rgb, "fs"), dither(rgb, "fs", monochrome=False))


# --- tone ---------------------------------------------------------------------------------------
def test_mono_tone_is_neutral_grey_and_uses_the_full_range() -> None:
    tinted = Image.fromarray(
        (np.random.default_rng(2).integers(60, 160, (50, 60, 3)) * [1.0, 0.9, 0.7]).astype(np.uint8)
    )
    out = np.asarray(mono_tone(tinted)).astype(int)
    assert np.array_equal(out[..., 0], out[..., 1]) and np.array_equal(out[..., 1], out[..., 2])
    assert out.min() <= 5 and out.max() >= 250  # a dull, low-contrast picture is stretched


def test_mono_tone_survives_a_flat_image() -> None:
    out = np.asarray(mono_tone(Image.new("RGB", (8, 8), (90, 90, 90))))
    assert out.shape == (8, 8, 3) and (out == out[0, 0]).all()


def test_mono_dither_input_keeps_the_ends_and_darkens_mid_greys() -> None:
    out = mono_dither_input(grey_ramp())[0, :, 0].astype(int)
    assert out[0] == 0 and out[255] == 255
    assert np.all(np.diff(out) >= 0)
    assert out[128] < 128  # dots average in linear light: mid-grey needs fewer white dots
    assert out[int(MONO_WHITE ** (1 / 1.8) * 255) + 2] == 255  # bright areas stay solid white


def test_the_bw_profile_runs_the_tone_step() -> None:
    config = EinkConfig().resolved("bw")
    image = Image.fromarray(np.random.default_rng(4).integers(60, 160, (30, 40, 3), dtype=np.uint8))
    out = np.asarray(enhance(image, config)).astype(int)
    assert np.array_equal(out[..., 0], out[..., 1])


# --- detection and decision -----------------------------------------------------------------------
def test_measure_content_reports_colorfulness() -> None:
    assert measure_content(grey_photo()).colorfulness < 1.0
    assert measure_content(colour_photo()).colorfulness > 30.0


@pytest.mark.parametrize(
    ("context", "colorfulness", "expected"),
    [
        ("photo", 1.0, "bw"),
        ("photo", 7.9, "bw"),
        ("photo", 12.0, "photo"),  # sepia / toned prints keep their color
        ("photo", 50.0, "photo"),
        ("poster", 1.0, "vivid"),  # posters and covers are not affected
        ("album", 1.0, "soft"),
    ],
)
def test_profile_decision_for_monochrome(context: Any, colorfulness: float, expected: str) -> None:
    assert decide_profile(context, stats(colorfulness=colorfulness))[0] == expected


def test_a_dark_black_and_white_photo_is_still_bw() -> None:
    profile, reason = decide_profile("photo", stats(colorfulness=0.5, luma_mean=40.0))
    assert profile == "bw" and "black and white" in reason


def test_bw_photos_keep_the_automatic_dither_choice() -> None:
    smooth = stats(colorfulness=0.5, smooth_share=0.8)
    assert decide_dither("photo", smooth)[0] == "stucki"
    concrete, decision = resolve(EinkConfig(), "photo", smooth)
    assert (concrete.profile, concrete.dither) == ("bw", "stucki") and decision.automatic


def test_an_explicit_profile_wins_over_the_detection() -> None:
    concrete, _ = resolve(EinkConfig(profile="vivid"), "photo", stats(colorfulness=0.5))
    assert concrete.profile == "vivid"


# --- pipeline -----------------------------------------------------------------------------------
def options(**eink: Any) -> RenderOptions:
    return RenderOptions.from_config(RuntimeConfig.model_validate({"eink": eink}))


def test_a_black_and_white_photo_renders_without_colored_dots() -> None:
    result = render(grey_photo(1), options())
    assert result.decision is not None and result.decision.profile == "bw"
    assert set(np.unique(result.indices).tolist()) <= {0, 1}
    assert len(result.bin) == 960_000
    composed = np.asarray(result.composed).astype(int)
    assert np.array_equal(composed[..., 0], composed[..., 2])  # the preview is grey


def test_a_colour_photo_is_not_touched() -> None:
    result = render(colour_photo(1), options())
    assert result.decision is not None and result.decision.profile != "bw"
    assert set(np.unique(result.indices).tolist()) - {0, 1}


def test_the_bw_profile_can_be_forced_on_a_colour_photo() -> None:
    result = render(colour_photo(2), options(profile="bw", dither="fs"))
    assert set(np.unique(result.indices).tolist()) <= {0, 1}
