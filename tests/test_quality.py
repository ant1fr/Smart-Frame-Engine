from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.core.config_models import QualityConfig
from app.processor.quality import LEVELS, AssetFacts, ImageStats, measure, violations

CONFIG = QualityConfig()
CAMERA = AssetFacts(has_camera=True, width=4000, height=3000)
NO_CAMERA = AssetFacts(has_camera=False, width=4000, height=3000)


def photo(seed: int = 1, size: tuple[int, int] = (320, 240)) -> Image.Image:
    """A busy, colorful, photo-like image."""
    rng = np.random.default_rng(seed)
    base = rng.integers(20, 235, (size[1] // 8, size[0] // 8, 3), dtype=np.uint8)
    return Image.fromarray(base).resize(size, Image.Resampling.BICUBIC)


def ui_screenshot(size: tuple[int, int] = (270, 600)) -> Image.Image:
    """White background with a few gray widgets, like an app screen."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for top in range(40, size[1] - 40, 80):
        draw.rectangle((20, top, size[0] - 20, top + 30), fill=(225, 228, 235))
    draw.rectangle((0, 0, size[0], 24), fill=(40, 120, 200))
    return image


def check(image: Image.Image, facts: AssetFacts, level: int = 0) -> list[str]:
    return violations(measure(image), facts, CONFIG, level)


# --- measure ------------------------------------------------------------------------------------
def test_measure_flat_and_busy_images() -> None:
    black = measure(Image.new("RGB", (100, 80), "black"))
    assert black.luma_mean == 0 and black.luma_std == 0 and black.flat_share == 100
    assert black.flat_luma < 10 and black.white_share == 0

    white = measure(Image.new("RGB", (100, 80), "white"))
    assert white.luma_mean > 250 and white.flat_share == 100
    assert white.flat_luma > 240 and white.white_share == 100

    busy = measure(photo())
    assert 60 < busy.luma_mean < 190 and busy.luma_std > 30
    assert busy.flat_share < 10 and busy.colorfulness > 25


def test_measure_works_on_a_downscaled_copy_and_does_not_touch_the_input() -> None:
    big = photo(size=(2000, 1500))
    before = big.tobytes()
    stats = measure(big)
    assert big.size == (2000, 1500) and big.tobytes() == before
    assert 60 < stats.luma_mean < 190


def test_measure_colorfulness_separates_gray_from_color() -> None:
    gray = Image.fromarray(np.tile(np.linspace(0, 255, 200, dtype=np.uint8), (100, 1)))
    assert measure(gray).colorfulness < 1
    assert measure(photo()).colorfulness > measure(gray).colorfulness + 20


# --- rules ----------------------------------------------------------------------------------------
def test_a_normal_photo_passes() -> None:
    assert check(photo(), CAMERA) == []
    assert check(photo(), NO_CAMERA) == []  # missing EXIF alone is never a reason


@pytest.mark.parametrize(
    "image",
    [Image.new("RGB", (200, 150), "black"), Image.new("RGB", (200, 150), (9, 9, 9))],
)
def test_blank_frames_are_rejected_even_with_camera_exif(image: Image.Image) -> None:
    assert "blank" in check(image, CAMERA)


def test_flat_mid_gray_is_blank_by_contrast() -> None:
    assert "blank" in check(Image.new("RGB", (200, 150), (120, 120, 120)), CAMERA)


def test_tiny_images_are_rejected_by_their_short_side() -> None:
    assert check(photo(), AssetFacts(True, 800, 599)) == ["tiny"]
    assert check(photo(), AssetFacts(True, 600, 4000)) == []  # exactly at the limit
    assert check(photo(), AssetFacts(True, None, None)) == []  # unknown size: not tiny


def test_screenshots_need_missing_exif() -> None:
    shot = ui_screenshot()
    assert "screenshot" in check(shot, AssetFacts(False, 1080, 2400))
    assert check(shot, AssetFacts(True, 1080, 2400)) == []  # a camera took it: keep


def test_phone_shaped_images_without_exif_are_screenshots_even_when_busy() -> None:
    assert check(photo(), AssetFacts(False, 1080, 2400)) == ["screenshot"]
    # Wide or large images are not phone screens.
    assert check(photo(), AssetFacts(False, 4000, 3000)) == []
    assert check(photo(), AssetFacts(False, 3024, 6000)) == []  # short side above the limit


def test_dark_flat_backgrounds_are_not_screenshots() -> None:
    """A lit subject on black (night shot, studio model) must survive without EXIF."""
    image = Image.new("RGB", (320, 240), (4, 4, 6))
    ImageDraw.Draw(image).ellipse((100, 60, 220, 180), fill=(250, 190, 60))
    stats = measure(image)
    assert stats.flat_share > 35 and stats.flat_luma < 20
    assert check(image, NO_CAMERA) == []


def test_documents_are_pale_and_mostly_white() -> None:
    rng = np.random.default_rng(5)
    page = rng.integers(236, 256, (240, 320), dtype=np.uint8)
    page[40:44, 30:290] = 40  # a printed line
    image = Image.fromarray(np.dstack([page, page, page]))
    assert "document" in check(image, NO_CAMERA)
    assert "document" not in check(image, CAMERA)


def test_black_and_white_portraits_and_night_shots_are_kept() -> None:
    ramp = np.tile(np.linspace(10, 245, 320, dtype=np.uint8), (240, 1))
    noise = np.random.default_rng(7).integers(-25, 25, ramp.shape)
    gray = np.clip(ramp.astype(int) + noise, 0, 255).astype(np.uint8)
    portrait = Image.fromarray(np.dstack([gray, gray, gray]))
    assert measure(portrait).colorfulness < 1
    assert check(portrait, NO_CAMERA) == []  # scanned B&W family photo
    assert check(portrait, CAMERA) == []

    night = Image.new("RGB", (320, 240), (3, 3, 5))
    ImageDraw.Draw(night).polygon([(160, 20), (190, 220), (130, 220)], fill=(255, 200, 80))
    assert check(night, CAMERA) == []


def test_reasons_accumulate() -> None:
    reasons = check(Image.new("RGB", (100, 100), "black"), AssetFacts(False, 300, 300))
    assert reasons == ["blank", "tiny"]


# --- progressive relaxation -----------------------------------------------------------------------
def test_relaxation_loosens_rules_step_by_step() -> None:
    small_ui = ui_screenshot((400, 300))
    facts = AssetFacts(False, 400, 300)
    assert check(small_ui, facts, 0) == ["tiny", "screenshot"]
    assert check(small_ui, facts, 1) == ["screenshot"]  # tiny limit halved to 300
    assert check(small_ui, facts, 2) == ["screenshot"]  # the flat-color rule still applies
    assert check(small_ui, facts, 3) == []  # only blank frames remain
    assert check(small_ui, facts, LEVELS) == []


def test_relaxation_keeps_blank_until_the_last_level() -> None:
    black = Image.new("RGB", (400, 300), "black")
    for level in range(LEVELS):
        assert "blank" in check(black, CAMERA, level)
    assert check(black, CAMERA, LEVELS) == []


def test_phone_shape_and_document_rules_only_apply_at_level_zero() -> None:
    assert check(photo(), AssetFacts(False, 1080, 2400), 0) == ["screenshot"]
    assert check(photo(), AssetFacts(False, 1080, 2400), 1) == []


def test_thresholds_come_from_the_config() -> None:
    strict = QualityConfig(min_dimension=1000, blank_luma_mean=100)
    stats = ImageStats(80, 40, 2, 100, 40, 0)
    assert violations(stats, AssetFacts(True, 900, 900), strict) == ["blank", "tiny"]
    assert violations(stats, AssetFacts(True, 900, 900), QualityConfig()) == []


def test_quality_config_validation() -> None:
    assert QualityConfig().enabled and QualityConfig().min_dimension == 600
    with pytest.raises(ValidationError):
        QualityConfig(min_dimension=-1)
    with pytest.raises(ValidationError):
        QualityConfig(screenshot_flat_share=0)
    with pytest.raises(ValidationError):
        QualityConfig(candidates_checked=0)
    with pytest.raises(ValidationError):
        QualityConfig(unknown_field=1)  # type: ignore[call-arg]
