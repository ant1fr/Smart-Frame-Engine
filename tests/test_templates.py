from __future__ import annotations

import io
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image

from app.core.config_models import EinkConfig, RuntimeConfig
from app.processor.pipeline import (
    RenderOptions,
    finalize,
    render_layout,
    render_layout_bytes,
)
from app.processor.templates import (
    VINYL_BLACK,
    AlbumLayout,
    Canvas,
    PosterLayout,
    TitleCardLayout,
    background_from,
    compose_layout,
    dominant_color,
)
from app.processor.typography import WHITE

NEUTRAL = EinkConfig(brightness=1.0, contrast=1.0, saturation=1.0, edge_filters=False)
MAGENTA = (255, 0, 255)
CYAN = (0, 255, 255)
FRAMES = [(1200, 1600), (1600, 1200), (1440, 2560), (2560, 1440)]


def canvas(size: tuple[int, int], margin: int = 0) -> Canvas:
    return Canvas(size, NEUTRAL, margin)


def mask_bbox(image: Image.Image, color: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """Bounding box (x0, y0, x1, y1 exclusive) of the pixels exactly equal to ``color``."""
    pixels: npt.NDArray[np.uint8] = np.asarray(image)
    ys, xs = np.nonzero(np.all(pixels == np.array(color, np.uint8), axis=-1))
    assert xs.size, f"no {color} pixel found"
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def options(**sections: Any) -> RenderOptions:
    return RenderOptions.from_config(RuntimeConfig.model_validate(sections), fit="contain")


@pytest.mark.parametrize("size", FRAMES)
@pytest.mark.parametrize(
    "layout",
    [
        PosterLayout(),
        AlbumLayout(title="Song", artist="Artist", album="Album"),
        AlbumLayout(title="Song", artist="Artist", album="Album", style="vinyl"),
        TitleCardLayout(title="Title", subtitle="2001", caption="Caption"),
    ],
)
def test_every_template_fills_the_frame(size: tuple[int, int], layout: Any) -> None:
    art = Image.new("RGB", (500, 750), (40, 90, 160))
    composed = compose_layout(art, layout, canvas(size))
    assert composed.size == size
    assert composed.mode == "RGB"


@pytest.mark.parametrize("size", FRAMES)
def test_poster_is_never_cropped(size: tuple[int, int]) -> None:
    poster = Image.new("RGB", (400, 600), MAGENTA)
    composed = compose_layout(poster, PosterLayout(), canvas(size))
    x0, y0, x1, y1 = mask_bbox(composed, MAGENTA)
    width, height = x1 - x0, y1 - y0
    assert abs(width / height - 400 / 600) < 0.01
    inset = round(min(size) * 0.05)
    assert x0 >= inset and y0 >= inset
    assert x1 <= size[0] - inset and y1 <= size[1] - inset
    # Fills its slot along the limiting axis (fit, not shrink).
    assert width == size[0] - 2 * inset or height == size[1] - 2 * inset


def test_poster_has_a_light_border_and_a_darker_backdrop() -> None:
    poster = Image.new("RGB", (400, 600), MAGENTA)
    composed = compose_layout(poster, PosterLayout(), canvas((1200, 1600)))
    x0, y0, _, _ = mask_bbox(composed, MAGENTA)
    border = composed.getpixel((x0 - 1, y0 + 50))
    assert border == (235, 235, 235)
    corner = composed.getpixel((5, 5))
    assert isinstance(corner, tuple)
    assert corner[0] < 255 * 0.6  # blurred and darkened


def test_poster_respects_a_larger_safe_margin() -> None:
    poster = Image.new("RGB", (400, 600), MAGENTA)
    composed = compose_layout(poster, PosterLayout(), canvas((1200, 1600), margin=150))
    x0, y0, x1, y1 = mask_bbox(composed, MAGENTA)
    assert min(x0, y0) >= 150
    assert x1 <= 1050 and y1 <= 1450


@pytest.mark.parametrize("size", FRAMES)
@pytest.mark.parametrize("style", ["sleeve", "vinyl"])
def test_album_cover_is_square_and_uncropped(size: tuple[int, int], style: str) -> None:
    cover = Image.new("RGB", (800, 600), CYAN)  # not square: must be padded, not cropped
    layout = AlbumLayout(title="Title", artist="Artist", album="Album", style=style)  # type: ignore[arg-type]
    composed = compose_layout(cover, layout, canvas(size))
    x0, y0, x1, y1 = mask_bbox(composed, CYAN)
    assert abs((x1 - x0) / (y1 - y0) - 800 / 600) < 0.01
    assert x0 > 0 and y0 > 0 and x1 < size[0] and y1 < size[1]


@pytest.mark.parametrize("size", [(1200, 1600), (1600, 1200)])
def test_vinyl_record_peeks_out_of_the_sleeve(size: tuple[int, int]) -> None:
    cover = Image.new("RGB", (600, 600), CYAN)
    sleeve = compose_layout(cover, AlbumLayout(title="T", style="sleeve"), canvas(size))
    vinyl = compose_layout(cover, AlbumLayout(title="T", style="vinyl"), canvas(size))
    _, _, sleeve_right, _ = mask_bbox(vinyl, CYAN)
    disc = np.all(np.asarray(vinyl) == np.array(VINYL_BLACK, np.uint8), axis=-1)
    assert disc[:, sleeve_right:].any()
    assert not np.all(np.asarray(sleeve) == np.array(VINYL_BLACK, np.uint8), axis=-1).any()


def test_album_text_is_white_on_a_dark_cover_and_black_on_a_light_one() -> None:
    dark = compose_layout(
        Image.new("RGB", (600, 600), (30, 20, 60)),
        AlbumLayout(title="Night Drive", artist="Someone"),
        canvas((1200, 1600)),
    )
    text_area = dark.crop((0, 1150, 1200, 1600))
    assert WHITE in {color for _, color in text_area.getcolors(maxcolors=100_000) or []}
    light = compose_layout(
        Image.new("RGB", (600, 600), (250, 240, 200)),
        AlbumLayout(title="Morning", artist="Someone"),
        canvas((1200, 1600)),
    )
    text_area = light.crop((0, 1150, 1200, 1600))
    assert (0, 0, 0) in {color for _, color in text_area.getcolors(maxcolors=100_000) or []}


def test_background_color_comes_from_the_cover_and_avoids_mid_grey() -> None:
    cover = Image.new("RGB", (100, 100), (200, 30, 40))
    assert dominant_color(cover) == (200, 30, 40)
    red, green, blue = background_from(cover)
    assert red > green and red > blue  # same hue family
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    assert luminance < 90 or luminance > 200


def test_long_album_text_stays_inside_the_frame() -> None:
    layout = AlbumLayout(
        title="An extremely long track title " * 6,
        artist="A band with an incredibly long name " * 3,
        album="Deluxe remastered anniversary edition " * 3,
    )
    composed = compose_layout(Image.new("RGB", (600, 600), CYAN), layout, canvas((1600, 1200)))
    assert composed.size == (1600, 1200)


def test_poster_and_album_need_artwork_but_the_title_card_does_not() -> None:
    with pytest.raises(ValueError, match="needs an image"):
        compose_layout(None, PosterLayout(), canvas((1200, 1600)))
    with pytest.raises(ValueError, match="needs an image"):
        compose_layout(None, AlbumLayout(title="x"), canvas((1200, 1600)))
    card = compose_layout(None, TitleCardLayout(title="No poster"), canvas((1200, 1600)))
    assert card.getpixel((10, 10)) == (0, 0, 0)


def test_title_card_draws_the_accent_rule() -> None:
    card = compose_layout(
        None, TitleCardLayout(title="Movie", accent=(0, 0, 255)), canvas((1200, 1600))
    )
    x0, _, x1, _ = mask_bbox(card, (0, 0, 255))
    assert abs((x0 + x1) / 2 - 600) <= 1


@pytest.mark.parametrize(
    ("panel", "orientation", "size"),
    [("133", "portrait", 960_000), ("133", "landscape", 960_000), ("315", "portrait", 2_304_000)],
)
def test_render_layout_encodes_a_bin_for_the_panel(panel: str, orientation: str, size: int) -> None:
    opts = options(fraimic={"panel": panel}, display={"orientation": orientation})
    result = render_layout(Image.new("RGB", (500, 750), (90, 30, 30)), PosterLayout(), opts)
    assert len(result.bin) == size
    assert result.composed.size == opts.frame_size


def test_render_layout_bytes_decodes_untrusted_data() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), CYAN).save(buffer, format="PNG")
    opts = options()
    result = render_layout_bytes(buffer.getvalue(), AlbumLayout(title="x"), opts)
    assert len(result.bin) == 960_000
    card = render_layout_bytes(None, TitleCardLayout(title="x"), opts)
    assert len(card.bin) == 960_000


def test_finalize_rejects_a_composition_of_the_wrong_size() -> None:
    with pytest.raises(ValueError, match="expected"):
        finalize(Image.new("RGB", (100, 100)), options())


def test_upscaled_artwork_skips_the_edge_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.processor.templates as templates

    calls: list[bool] = []
    real = templates.enhance

    def spy(image: Image.Image, eink: EinkConfig, *, edge_filters: bool = True) -> Image.Image:
        calls.append(edge_filters)
        return real(image, eink, edge_filters=edge_filters)

    monkeypatch.setattr(templates, "enhance", spy)
    sharp = EinkConfig()  # official: edge filters on
    compose_layout(Image.new("RGB", (2000, 3000)), PosterLayout(), Canvas((1200, 1600), sharp))
    assert calls[-1] is True  # downscaled poster: filters applied
    compose_layout(Image.new("RGB", (600, 900)), PosterLayout(), Canvas((1200, 1600), sharp))
    assert calls[-1] is False  # enlarged poster: no sharpening of the blur
    compose_layout(
        Image.new("RGB", (500, 500)), AlbumLayout(title="x"), Canvas((1200, 1600), sharp)
    )
    assert calls[-1] is False
    compose_layout(
        Image.new("RGB", (1200, 1200)), AlbumLayout(title="x"), Canvas((1200, 1600), sharp)
    )
    assert calls[-1] is True
