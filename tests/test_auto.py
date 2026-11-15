from __future__ import annotations

import io
from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.core.config_models import EinkConfig, RuntimeConfig
from app.processor import auto
from app.processor.auto import (
    ContentStats,
    decide_dither,
    decide_profile,
    measure_content,
    resolve,
)
from app.processor.dither import NUMBA_AVAILABLE
from app.processor.pipeline import RenderOptions, render, render_layout
from app.processor.templates import AlbumLayout, PosterLayout, TitleCardLayout

SERPENTINE = "fs-serpentine" if NUMBA_AVAILABLE else "fs"


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


def photo(seed: int = 0, size: tuple[int, int] = (240, 320)) -> Image.Image:
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8))


# --- measurements ---------------------------------------------------------------------------
def test_measure_content_tells_noise_from_gradients_and_graphics() -> None:
    noisy = measure_content(photo())
    assert noisy.detail > 25 and noisy.smooth_share == 0.0
    assert not noisy.is_graphic("photo") and not noisy.is_smooth("photo")

    ramp = np.tile(np.linspace(40, 200, 320, dtype=np.float32), (240, 1))
    sky = measure_content(Image.fromarray(np.dstack([ramp, ramp, ramp + 20]).astype(np.uint8)))
    assert sky.smooth_share > 0.9 and sky.detail < 2

    logo = Image.new("RGB", (300, 300), (255, 255, 255))
    logo.paste((200, 20, 20), (100, 100, 200, 200))
    flat = measure_content(logo)
    assert flat.is_graphic("album") and flat.dominant_colors <= 3 and flat.flat_share > 60
    assert not flat.is_graphic("photo")  # a photo is never graphic, whatever its colors


def test_measure_content_handles_tiny_images() -> None:
    assert measure_content(Image.new("RGB", (3, 2), (10, 20, 30))).smooth_share == 0.0


# --- profile ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("context", "overrides", "expected"),
    [
        ("title_card", {}, "natural"),
        ("album", {"dominant_colors": 6}, "official"),  # graphic cover
        ("album", {"flat_share": 60.0}, "official"),
        ("album", {}, "soft"),  # photographic cover
        ("poster", {"highlight_share": 6.0}, "photo"),  # bright faces: deep blacks, rich colors
        ("poster", {"luma_std": 70.0}, "photo"),  # contrasty
        ("poster", {"luma_std": 40.0, "highlight_share": 0.5}, "vivid"),  # flat, muted
        ("poster", {"dominant_colors": 13}, "official"),  # flat-color graphic poster
        ("poster", {"flat_share": 60.0, "luma_std": 40.0}, "vivid"),  # dark backdrop only
        ("photo", {"luma_mean": 60.0}, "soft"),  # dark
        ("photo", {"luma_std": 30.0}, "soft"),  # low contrast
        ("photo", {"luma_mean": 178.0, "luma_std": 34.0, "black_share": 0.0}, "vivid"),  # snow
        ("photo", {"luma_mean": 170.0, "black_share": 6.0}, "photo"),  # bright but has blacks
        ("photo", {}, "photo"),
    ],
)
def test_profile_decision(context: Any, overrides: dict[str, Any], expected: str) -> None:
    assert decide_profile(context, stats(**overrides))[0] == expected


def test_a_title_card_or_missing_artwork_is_natural() -> None:
    assert decide_profile("poster", None)[0] == "natural"


def test_a_dark_photo_with_a_big_flat_background_is_not_graphic() -> None:
    """Real case: a night photo (3 dominant colors, 78% flat black) was read as a drawing."""
    night = stats(dominant_colors=3, flat_share=78.0, luma_mean=20.0, black_share=78.0)
    assert not night.is_graphic("photo")
    profile, reason = decide_profile("photo", night)
    assert profile == "soft" and "dark photo" in reason
    assert decide_dither("photo", night)[0] != "atkinson-classic"
    # ...while the same numbers on an album cover are a graphic.
    assert decide_profile("album", night)[0] == "official"


def test_a_bright_snow_photo_keeps_its_whites() -> None:
    snow = stats(luma_mean=178.0, luma_std=34.0, black_share=0.0, highlight_share=0.0)
    profile, reason = decide_profile("photo", snow)
    assert profile == "vivid" and "bright photo" in reason
    assert decide_profile("photo", stats(luma_mean=60.0, black_share=0.0))[0] == "soft"  # dark wins


# --- dither ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("context", "overrides", "expected"),
    [
        ("title_card", {}, "atkinson-classic"),
        ("album", {"dominant_colors": 5}, "atkinson-classic"),
        ("poster", {"dominant_colors": 8}, "atkinson-classic"),  # flat graphic poster
        ("photo", {"dominant_colors": 3, "flat_share": 78.0}, SERPENTINE),  # night photo
        ("photo", {"smooth_share": 0.7}, "stucki"),
        ("photo", {}, SERPENTINE),
        ("poster", {"smooth_share": 0.9}, SERPENTINE),  # posters keep their detail
        ("album", {}, "atkinson-classic"),  # lettering must not smear
    ],
)
def test_dither_decision(context: Any, overrides: dict[str, Any], expected: str) -> None:
    assert decide_dither(context, stats(**overrides))[0] == expected


def test_serpentine_falls_back_to_fs_without_numba(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.processor.dither as dither_module

    monkeypatch.setattr(dither_module, "NUMBA_AVAILABLE", False)
    assert decide_dither("photo", stats())[0] == "fs"


# --- resolve ---------------------------------------------------------------------------------
def test_auto_settings_are_filled_in_and_explained() -> None:
    concrete, decision = resolve(EinkConfig(), "poster", stats(highlight_share=8.0))
    assert (concrete.profile, concrete.dither) == ("photo", SERPENTINE)
    assert decision.automatic and decision.context == "poster"
    assert len(decision.reasons) == 2 and "contrasty poster" in decision.reasons[0]
    assert (concrete.brightness, concrete.contrast, concrete.edge_filters) == (1.0, 1.0, False)


def test_explicit_settings_always_win() -> None:
    eink = EinkConfig(profile="official", dither="stucki", contrast=1.4)
    concrete, decision = resolve(eink, "title_card", None)
    assert (concrete.profile, concrete.dither, concrete.contrast) == ("official", "stucki", 1.4)
    assert not decision.automatic
    assert all("set in the settings" in reason for reason in decision.reasons)


def test_a_manual_profile_with_an_automatic_dither() -> None:
    concrete, decision = resolve(EinkConfig(profile="vivid"), "photo", stats(smooth_share=0.8))
    assert (concrete.profile, concrete.dither) == ("vivid", "stucki")
    assert decision.automatic


# --- pipeline --------------------------------------------------------------------------------
def options(**eink: Any) -> RenderOptions:
    return RenderOptions.from_config(RuntimeConfig.model_validate({"eink": eink}))


def test_render_reports_its_decision_and_uses_it() -> None:
    result = render(photo(1), options())
    assert result.decision is not None and result.decision.automatic
    assert result.options.eink.profile == result.decision.profile
    assert result.options.eink.dither == result.decision.dither
    assert len(result.bin) == 960_000


def test_manual_override_is_reported_as_such() -> None:
    result = render(photo(2), options(profile="official", dither="atkinson"))
    assert result.decision is not None and not result.decision.automatic
    assert (result.options.eink.profile, result.options.eink.dither) == ("official", "atkinson")


@pytest.mark.parametrize(
    ("layout", "expected_context"),
    [
        (PosterLayout(), "poster"),
        (AlbumLayout(title="Song", artist="Artist"), "album"),
        (TitleCardLayout(title="Nothing here"), "title_card"),
    ],
)
def test_templates_decide_from_their_layout(layout: Any, expected_context: str) -> None:
    art = None if isinstance(layout, TitleCardLayout) else photo(3, (300, 450))
    result = render_layout(art, layout, options())
    assert result.decision is not None and result.decision.context == expected_context
    if expected_context == "title_card":
        assert (result.decision.profile, result.decision.dither) == (
            "natural",
            "atkinson-classic",
        )


def test_image_bytes_round_trip_through_auto() -> None:
    buffer = io.BytesIO()
    photo(4).save(buffer, format="PNG")
    from app.processor.pipeline import render_bytes

    result = render_bytes(buffer.getvalue(), options())
    assert result.decision is not None


def test_auto_module_exports_its_thresholds() -> None:
    assert 0 < auto.SMOOTH_PHOTO_SHARE < 1 and auto.GRAPHIC_MAX_COLORS > 0
