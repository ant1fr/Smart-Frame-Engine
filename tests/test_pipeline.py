from __future__ import annotations

import io
from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.core.config_models import RuntimeConfig
from app.processor.packing import decode_bin
from app.processor.pipeline import RenderOptions, compose, render, render_bytes
from tests.reference import fraimic_converter as ref

RED, WHITE = 3, 1


def config(**sections: Any) -> RuntimeConfig:
    return RuntimeConfig.model_validate(sections)


def neutral_eink() -> dict[str, Any]:
    """No enhancement so flat test colors survive untouched."""
    return {
        "brightness": 1.0,
        "contrast": 1.0,
        "saturation": 1.0,
        "edge_filters": False,
        "dither": "floyd_steinberg",
    }


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("panel", "orientation", "frame", "size"),
    [
        ("133", "portrait", (1200, 1600), 960_000),
        ("133", "landscape", (1600, 1200), 960_000),
        ("315", "portrait", (1440, 2560), 2_304_000),
        ("315", "landscape", (2560, 1440), 2_304_000),
    ],
)
def test_render_produces_exact_bin_for_every_panel_and_orientation(
    panel: str, orientation: str, frame: tuple[int, int], size: int
) -> None:
    options = RenderOptions.from_config(
        config(fraimic={"panel": panel}, display={"orientation": orientation})
    )
    assert options.frame_size == frame
    image = Image.new("RGB", (640, 480), (30, 120, 200))
    result = render(image, options)
    assert len(result.bin) == size
    assert result.composed.size == frame
    assert result.indices.shape == (frame[1], frame[0])
    assert len(result.sha256) == 64


@pytest.mark.parametrize(
    ("rotation", "corner"),
    [(90, "bottom-left"), (270, "top-right")],
)
def test_landscape_rotation_places_top_left_marker(rotation: int, corner: str) -> None:
    options = RenderOptions.from_config(
        config(
            display={
                "orientation": "landscape",
                "landscape_rotation": rotation,
                "default_fit": "contain",
            },
            eink=neutral_eink(),
        )
    )
    image = Image.new("RGB", (1600, 1200), (255, 255, 255))
    image.paste((255, 0, 0), (0, 0, 100, 100))  # top-left marker in viewing orientation
    result = render(image, options)
    assert result.indices[50, 50] == RED

    portrait = decode_bin(result.bin, options.panel)  # native 1200x1600 portrait buffer
    assert portrait.shape == (1600, 1200)
    marker = {
        "bottom-left": portrait[1600 - 100 :, :100],
        "top-right": portrait[:100, 1200 - 100 :],
    }[corner]
    assert np.all(marker == RED)
    assert np.count_nonzero(portrait == RED) == 100 * 100
    assert np.count_nonzero(portrait == WHITE) == 1200 * 1600 - 100 * 100


def test_portrait_render_is_not_rotated() -> None:
    options = RenderOptions.from_config(
        config(display={"default_fit": "contain"}, eink=neutral_eink())
    )
    image = Image.new("RGB", (1200, 1600), (255, 255, 255))
    image.paste((255, 0, 0), (0, 0, 100, 100))
    portrait = decode_bin(render(image, options).bin, options.panel)
    assert np.all(portrait[:100, :100] == RED)


def test_contain_compose_equals_official_letterbox_pipeline() -> None:
    rng = np.random.default_rng(8)
    image = Image.fromarray(rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8))
    options = RenderOptions.from_config(
        config(display={"default_fit": "contain", "contain_background": "black"})
    )
    panel = ref.Panel(1200, 1600)
    expected = ref.pad_to_frame(
        ref.enhance(ref.scale_to_frame(image, "letterbox", panel)), panel, (0, 0, 0)
    )
    assert np.array_equal(np.asarray(compose(image, options)), np.asarray(expected))


def test_smart_crop_compose_equals_official_crop_pipeline() -> None:
    rng = np.random.default_rng(9)
    image = Image.fromarray(rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8))
    options = RenderOptions.from_config(config(display={"default_fit": "smart_crop"}))
    expected = ref.enhance(ref.scale_to_frame(image, "crop", ref.Panel(1200, 1600)))
    assert np.array_equal(np.asarray(compose(image, options)), np.asarray(expected))


def test_blur_background_is_not_edge_filtered_but_picture_is() -> None:
    options = RenderOptions.from_config(
        config(display={"default_fit": "contain", "contain_background": "blur"})
    )
    composed = compose(Image.new("RGB", (1600, 400), (200, 60, 60)), options)
    assert composed.size == (1200, 1600)
    corner = composed.getpixel((0, 0))
    assert isinstance(corner, tuple) and corner != (0, 0, 0)


def test_previews_are_in_viewing_orientation() -> None:
    options = RenderOptions.from_config(config(display={"orientation": "landscape"}))
    result = render(Image.new("RGB", (800, 600), (90, 90, 200)), options)
    jpeg = Image.open(io.BytesIO(result.preview_jpeg()))
    dithered = Image.open(io.BytesIO(result.dithered_png()))
    assert jpeg.format == "JPEG" and jpeg.size == (1600, 1200)
    assert dithered.format == "PNG" and dithered.size == (1600, 1200)
    colors = {tuple(c) for c in np.asarray(dithered.convert("RGB")).reshape(-1, 3)[::97]}
    allowed = {(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0), (0, 0, 255), (0, 255, 0)}
    assert colors <= allowed


def test_render_bytes_decodes_and_validates() -> None:
    options = RenderOptions.from_config(config())
    image = Image.new("RGBA", (300, 400), (0, 0, 255, 255))
    assert len(render_bytes(png_bytes(image), options).bin) == 960_000


def test_render_options_reject_inconsistent_frame() -> None:
    base = RenderOptions.from_config(config())
    with pytest.raises(ValueError, match="frame_size"):
        RenderOptions(
            panel=base.panel,
            frame_size=(1600, 1200),
            rotation=0,
            fit=base.fit,
            background=base.background,
            eink=base.eink,
        )


def _official_rotate_image(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    panel = ref.Panel(1200, 1600)
    return ref.pad_to_frame(
        ref.enhance(ref.scale_to_frame(image, "rotate", panel)), panel, background
    )


def _landscape_options(**eink: Any) -> RenderOptions:
    return RenderOptions.from_config(
        config(
            display={
                "orientation": "landscape",
                "landscape_rotation": 90,
                "default_fit": "contain",
                "contain_background": "white",
            },
            eink=eink,
        )
    )


def test_landscape_composition_equals_official_rotate_fit() -> None:
    rng = np.random.default_rng(21)
    image = Image.fromarray(rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8))
    composed = compose(image, _landscape_options())
    expected = _official_rotate_image(image, (255, 255, 255))
    assert np.array_equal(np.rot90(np.asarray(composed)), np.asarray(expected))


@pytest.mark.slow
def test_landscape_bin_is_byte_identical_to_official_rotate_fit() -> None:
    rng = np.random.default_rng(22)
    base = rng.integers(0, 256, (36, 64, 3), dtype=np.uint8)
    image = Image.fromarray(base).resize((1600, 900), Image.Resampling.BICUBIC)
    options = _landscape_options(dither="atkinson")
    official_indices = ref.quantize_atkinson_indexed(_official_rotate_image(image, (255, 255, 255)))
    expected = ref.pack_el133uf1(ref.COLOR_CODES[official_indices]).tobytes()
    assert render(image, options).bin == expected


# --- safe margin ---------------------------------------------------------------------------
def _margin_options(margin: int, fit: str, background: str = "white") -> RenderOptions:
    return RenderOptions.from_config(
        config(
            display={
                "default_fit": fit,
                "contain_background": background,
                "safe_margin_px": margin,
            },
            eink=neutral_eink(),
        )
    )


@pytest.mark.parametrize("fit", ["smart_crop", "contain"])
def test_safe_margin_zero_is_the_unchanged_composition(fit: str) -> None:
    rng = np.random.default_rng(31)
    image = Image.fromarray(rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8))
    default = RenderOptions.from_config(config(display={"default_fit": fit}))
    explicit = RenderOptions.from_config(config(display={"default_fit": fit, "safe_margin_px": 0}))
    assert np.array_equal(np.asarray(compose(image, default)), np.asarray(compose(image, explicit)))


@pytest.mark.parametrize(
    ("fit", "background", "fill"),
    [
        ("smart_crop", "white", (255, 255, 255)),
        ("smart_crop", "black", (0, 0, 0)),
        ("contain", "black", (0, 0, 0)),
    ],
)
def test_safe_margin_insets_the_picture_and_fills_the_rim(
    fit: str, background: str, fill: tuple[int, int, int]
) -> None:
    margin = 40
    image = Image.new("RGB", (1200, 1600), (200, 30, 30))
    composed = np.asarray(compose(image, _margin_options(margin, fit, background)))
    assert composed.shape == (1600, 1200, 3)
    assert np.all(composed[:margin] == fill) and np.all(composed[-margin:] == fill)
    assert np.all(composed[:, :margin] == fill) and np.all(composed[:, -margin:] == fill)
    assert np.all(composed[margin + 20 : -margin - 20, margin + 20 : -margin - 20] != fill)


def test_safe_margin_blur_background_fills_the_rim_with_the_photo() -> None:
    rng = np.random.default_rng(32)
    image = Image.fromarray(rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8))
    composed = np.asarray(compose(image, _margin_options(60, "smart_crop", "blur")))
    assert composed.shape == (1600, 1200, 3)
    assert composed[:60].std() > 1  # a blurred copy of the photo, not a flat fill


def test_safe_margin_is_applied_in_viewing_orientation_and_survives_rotation() -> None:
    options = RenderOptions.from_config(
        config(
            display={
                "orientation": "landscape",
                "safe_margin_px": 50,
                "contain_background": "white",
            },
            eink=neutral_eink(),
        )
    )
    result = render(Image.new("RGB", (1600, 1200), (255, 0, 0)), options)
    assert result.composed.size == (1600, 1200)
    assert np.all(result.indices[:50] == WHITE) and np.all(result.indices[:, :50] == WHITE)
    assert result.indices[600, 800] == RED
    portrait = decode_bin(result.bin, options.panel)
    assert np.all(portrait[:50] == WHITE) and portrait[800, 600] == RED


def test_safe_margin_validation() -> None:
    with pytest.raises(ValueError):
        config(display={"safe_margin_px": -1})
    with pytest.raises(ValueError):
        config(display={"safe_margin_px": 251})
