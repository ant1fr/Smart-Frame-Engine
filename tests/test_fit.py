from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app.processor.fit import (
    FocusRegion,
    fit_inside,
    make_background,
    paste_centered,
    smart_crop,
)
from app.processor.pipeline import decode_image
from tests.reference import fraimic_converter as ref


def noise(width: int, height: int, seed: int = 0) -> Image.Image:
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (height, width, 3), dtype=np.uint8))


def with_marker(width: int, height: int, box: tuple[int, int, int, int]) -> Image.Image:
    image = Image.new("RGB", (width, height), (255, 255, 255))
    image.paste((255, 0, 0), box)
    return image


def red_fraction(image: Image.Image) -> float:
    arr = np.asarray(image).astype(int)
    red = (arr[..., 0] > 200) & (arr[..., 1] < 60) & (arr[..., 2] < 60)
    return float(red.mean())


# --- FocusRegion -----------------------------------------------------------------
def test_focus_region_from_pixels_normalizes_and_clamps() -> None:
    region = FocusRegion.from_pixels(300, 50, 100, 150, 400, 200)
    assert (region.x, region.y) == (0.25, 0.25)
    assert (region.width, region.height) == (0.5, 0.5)
    clamped = FocusRegion.from_pixels(-10, -10, 500, 300, 400, 200)
    assert (clamped.x, clamped.y, clamped.width, clamped.height) == (0, 0, 1, 1)


@pytest.mark.parametrize(
    "args", [(-0.1, 0, 0.5, 0.5), (0, 0, 0, 0.5), (0.8, 0, 0.5, 0.5), (0, 1.1, 0.1, 0.1)]
)
def test_focus_region_validation(args: tuple[float, float, float, float]) -> None:
    with pytest.raises(ValueError):
        FocusRegion(*args)


# --- smart_crop ------------------------------------------------------------------
@pytest.mark.parametrize("source", [(4000, 3000), (3000, 4000), (1200, 1600), (300, 200)])
@pytest.mark.parametrize("target", [(1200, 1600), (1600, 1200)])
def test_smart_crop_without_regions_equals_official_crop(
    source: tuple[int, int], target: tuple[int, int]
) -> None:
    image = noise(*source)
    expected = ref.scale_to_frame(image, "crop", ref.Panel(*target))
    result = smart_crop(image, target)
    assert result.size == target
    assert np.array_equal(np.asarray(result), np.asarray(expected))


def test_smart_crop_keeps_face_near_horizontal_edge() -> None:
    # Landscape photo, portrait frame: only a centered vertical slice survives a plain crop.
    image = with_marker(4000, 3000, (3600, 1200, 3900, 1500))
    region = FocusRegion.from_pixels(3600, 1200, 3900, 1500, 4000, 3000)
    assert red_fraction(smart_crop(image, (1200, 1600))) == 0.0
    cropped = smart_crop(image, (1200, 1600), [region])
    assert red_fraction(cropped) > 0.01


def test_smart_crop_keeps_face_near_top_with_headroom() -> None:
    # Portrait photo, landscape frame: the face at the very top would be cut by a centre crop.
    image = with_marker(3000, 4000, (1300, 100, 1700, 500))
    region = FocusRegion.from_pixels(1300, 100, 1700, 500, 3000, 4000)
    assert red_fraction(smart_crop(image, (1600, 1200))) == 0.0
    cropped = smart_crop(image, (1600, 1200), [region])
    rows = np.where(np.asarray(cropped)[..., 1].min(axis=1) < 60)[0]
    assert rows.size > 0
    assert rows.min() >= 0 and rows.max() < 1200


def test_smart_crop_union_of_regions_stays_inside() -> None:
    image = with_marker(4000, 3000, (1000, 1000, 1100, 1100))
    image.paste((255, 0, 0), (2300, 1000, 2400, 1100))
    regions = [
        FocusRegion.from_pixels(1000, 1000, 1100, 1100, 4000, 3000),
        FocusRegion.from_pixels(2300, 1000, 2400, 1100, 4000, 3000),
    ]
    cropped = np.asarray(smart_crop(image, (1200, 1600), regions))
    cols = np.where(cropped[..., 1].min(axis=0) < 60)[0]
    assert cols.min() > 0 and cols.max() < 1199
    assert cols.max() - cols.min() > 400  # both faces are in frame


# --- contain ----------------------------------------------------------------------
@pytest.mark.parametrize("source", [(4000, 3000), (3000, 4000), (500, 500)])
@pytest.mark.parametrize("style", ["black", "white"])
def test_contain_with_flat_background_equals_official_letterbox(
    source: tuple[int, int], style: str
) -> None:
    image = noise(*source, seed=3)
    panel = ref.Panel(1200, 1600)
    expected = ref.pad_to_frame(
        ref.scale_to_frame(image, "letterbox", panel), panel, ref.LETTERBOX_COLORS[style]
    )
    background = make_background(image, (1200, 1600), style)  # type: ignore[arg-type]
    result = paste_centered(background, fit_inside(image, (1200, 1600)))
    assert np.array_equal(np.asarray(result), np.asarray(expected))


def test_contain_keeps_aspect_ratio_and_corners() -> None:
    picture = fit_inside(noise(4000, 2000), (1200, 1600))
    assert picture.size == (1200, 600)
    framed = paste_centered(make_background(picture, (1200, 1600), "white"), picture)
    assert framed.getpixel((0, 0)) == (255, 255, 255)
    assert framed.getpixel((1199, 1599)) == (255, 255, 255)


def test_blur_background_fills_frame() -> None:
    image = Image.new("RGB", (400, 300), (200, 40, 40))
    background = make_background(image, (1200, 1600), "blur")
    assert background.size == (1200, 1600)
    corner = background.getpixel((0, 0))
    assert isinstance(corner, tuple)
    assert corner[0] > corner[1]  # derived from the image, not a flat fill
    assert corner[0] < 200  # darkened


# --- decoding -----------------------------------------------------------------------
def test_decode_applies_exif_orientation() -> None:
    image = Image.new("RGB", (40, 20), (10, 200, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW on display
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    assert decode_image(buffer.getvalue()).size == (20, 40)


def test_decode_flattens_transparency_on_white() -> None:
    image = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    decoded = decode_image(buffer.getvalue())
    assert decoded.mode == "RGB"
    assert decoded.getpixel((5, 5)) == (255, 255, 255)
