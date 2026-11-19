from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.core.config_models import EinkConfig
from app.processor.enhance import enhance
from tests.reference import fraimic_converter as ref


def sample() -> Image.Image:
    rng = np.random.default_rng(5)
    return Image.fromarray(rng.integers(0, 256, (120, 90, 3), dtype=np.uint8))


def test_defaults_match_official_chain() -> None:
    image = sample()
    expected = ref.enhance(image)
    assert np.array_equal(np.asarray(enhance(image, EinkConfig())), np.asarray(expected))


def test_custom_factors_match_official_chain() -> None:
    image = sample()
    config = EinkConfig(brightness=0.9, contrast=1.4, saturation=1.6)
    expected = ref.enhance(image, brightness=0.9, contrast=1.4, saturation=1.6)
    assert np.array_equal(np.asarray(enhance(image, config)), np.asarray(expected))


def test_neutral_settings_leave_image_unchanged() -> None:
    image = sample()
    config = EinkConfig(brightness=1.0, contrast=1.0, saturation=1.0, edge_filters=False)
    assert np.array_equal(np.asarray(enhance(image, config)), np.asarray(image))


def test_edge_filters_can_be_suppressed_per_call() -> None:
    image = sample()
    config = EinkConfig(brightness=1.0, contrast=1.0, saturation=1.0, edge_filters=True)
    assert np.array_equal(np.asarray(enhance(image, config, edge_filters=False)), np.asarray(image))
    assert not np.array_equal(np.asarray(enhance(image, config)), np.asarray(image))


def test_sharpness_is_applied_when_not_neutral() -> None:
    image = sample()
    base = EinkConfig(edge_filters=False)
    sharp = EinkConfig(edge_filters=False, sharpness=2.0)
    assert not np.array_equal(np.asarray(enhance(image, base)), np.asarray(enhance(image, sharp)))


# --- gamut-safe curves ---------------------------------------------------------------------------
def _curve(profile: str, pixels: np.ndarray) -> np.ndarray:
    from app.core.config_models import EinkConfig as Eink

    image = Image.fromarray(pixels.astype(np.uint8))
    return np.asarray(enhance(image, Eink().resolved(profile)))  # type: ignore[arg-type]


def test_natural_leaves_the_source_untouched() -> None:
    pixels = np.random.default_rng(1).integers(0, 256, (20, 30, 3), dtype=np.uint8)
    assert np.array_equal(_curve("natural", pixels), pixels)


@pytest.mark.parametrize("profile", ["soft", "vivid"])
def test_curves_preserve_black_and_white(profile: str) -> None:
    ends = np.array([[[0, 0, 0], [255, 255, 255]]], dtype=np.uint8)
    assert _curve(profile, ends).tolist() == ends.tolist()


@pytest.mark.parametrize("profile", ["soft", "vivid"])
def test_curves_are_monotonic_on_greys(profile: str) -> None:
    ramp = np.arange(256, dtype=np.uint8)
    out = _curve(profile, np.dstack([ramp[None, :]] * 3))[0, :, 0].astype(int)
    assert np.all(np.diff(out) >= 0)


def test_soft_lifts_shadows_and_vivid_adds_contrast() -> None:
    grey = lambda v: np.full((1, 1, 3), v, dtype=np.uint8)  # noqa: E731
    assert _curve("soft", grey(30))[0, 0, 0] > 30
    assert _curve("soft", grey(250))[0, 0, 0] < 250  # restrained highlights
    assert _curve("vivid", grey(60))[0, 0, 0] < 60  # S-curve: darker below the middle...
    assert _curve("vivid", grey(190))[0, 0, 0] > 190  # ...lighter above it


@pytest.mark.parametrize("profile", ["soft", "vivid"])
def test_curves_keep_hue_instead_of_clipping_channels(profile: str) -> None:
    rng = np.random.default_rng(7)
    pixels = rng.integers(0, 256, (60, 80, 3), dtype=np.uint8)
    out = _curve(profile, pixels).astype(int)
    src = pixels.astype(int)
    distinct = (
        (src[..., 0] != src[..., 1]) & (src[..., 1] != src[..., 2]) & (src[..., 0] != src[..., 2])
    )
    order_in = np.argsort(src, axis=2)[distinct]
    order_out = np.argsort(out, axis=2)[distinct]
    assert (order_in == order_out).mean() > 0.98  # chroma ordering survives (allow rounding ties)
    assert out.min() >= 0 and out.max() <= 255


def test_vivid_saturates_a_muted_color_without_leaving_the_gamut() -> None:
    muted = np.full((1, 1, 3), (150, 110, 100), dtype=np.uint8)
    pure = np.full((1, 1, 3), (255, 0, 0), dtype=np.uint8)
    out = _curve("vivid", muted)[0, 0].astype(int)
    assert out.max() - out.min() > 150 - 100
    red = _curve("vivid", pure)[0, 0]
    assert red[0] > 200 and red[1] < 20 and red[2] < 20  # still pure red (toned down, hue kept)
