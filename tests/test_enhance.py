from __future__ import annotations

import numpy as np
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
