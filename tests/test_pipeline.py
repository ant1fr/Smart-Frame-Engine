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
