"""No-crop composition templates: movie posters, album covers and typographic cards.

Templates turn artwork (and optional text) into a frame-sized composition in *viewing*
orientation. They never crop the artwork: posters and covers are scaled to fit inside
their slot. The result then goes through the regular rotation/dithering/encoding stage
(:func:`app.processor.pipeline.finalize`).

* ``poster`` - the full poster centered over a blurred, darkened backdrop made from the
  poster itself, with a thin light border and a soft drop shadow.
* ``album`` - the full square cover with a typography block (title, artist, album) on a
  flat background color sampled from the cover. ``style="vinyl"`` adds a record peeking
  out of the sleeve. Portrait frames stack cover and text; landscape frames put the text
  on the right.
* ``title_card`` - a clean typographic card used when no artwork could be found.

Every template adapts to the frame size (portrait or landscape, 13.3" or 31.5" panel) and
keeps ``safe_margin_px`` clear on every side.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import Final, Literal

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from app.core.config_models import EinkConfig
from app.processor.enhance import enhance
from app.processor.fit import Size, blurred_background, fit_inside
from app.processor.palette import PALETTE_RGB
from app.processor.typography import (
    RGB,
    Align,
    FittedText,
    draw_text,
    fit_text,
    pick_text_color,
)

AlbumStyle = Literal["sleeve", "vinyl"]
TemplateName = Literal["poster", "album", "title_card"]

#: Extra darkening of the poster backdrop (on top of the blur's own darkening).
POSTER_BACKDROP_DARKEN: Final = 0.7
#: Poster inset as a fraction of the frame's short side.
POSTER_INSET: Final = 0.05
POSTER_BORDER_COLOR: Final[RGB] = (235, 235, 235)
SHADOW_OPACITY: Final = 150
#: Fraction of the vinyl disc that sticks out of the sleeve.
VINYL_PEEK: Final = 0.32
VINYL_BLACK: Final[RGB] = (12, 12, 12)
VINYL_GROOVE: Final[RGB] = (58, 58, 58)
#: Background colors are pulled this much toward the nearest panel color (cleaner dither).
PALETTE_PULL: Final = 0.35
TITLE_CARD_BACKGROUND: Final[RGB] = (0, 0, 0)
_SUPERSAMPLE: Final = 2


# --- Layouts ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PosterLayout:
    """Full poster over a blurred backdrop (movies, TV series)."""

    name: TemplateName = "poster"


@dataclass(frozen=True, slots=True)
class AlbumLayout:
    """Square cover with a title / artist / album typography block."""

    title: str
    artist: str | None = None
    album: str | None = None
    style: AlbumStyle = "sleeve"
    name: TemplateName = "album"


@dataclass(frozen=True, slots=True)
class TitleCardLayout:
    """Typographic card (no artwork): the last-resort fallback."""

    title: str
    subtitle: str | None = None
    caption: str | None = None
    accent: RGB = (255, 0, 0)
    name: TemplateName = "title_card"


Layout = PosterLayout | AlbumLayout | TitleCardLayout


@dataclass(frozen=True, slots=True)
class Canvas:
    """Frame geometry and enhancement settings shared by every template."""

    size: Size
    eink: EinkConfig
    safe_margin_px: int = 0

    @property
    def short_side(self) -> int:
        """The frame's shorter dimension."""
        return min(self.size)

    @property
    def portrait(self) -> bool:
        """Whether the frame is taller than wide."""
        return self.size[1] >= self.size[0]

    def pad(self, fraction: float) -> int:
        """A padding of ``fraction`` of the short side, never inside the safe margin."""
        return max(self.safe_margin_px, round(self.short_side * fraction))


# --- Color helpers ----------------------------------------------------------------------
def _nearest_palette(color: RGB) -> RGB:
    return min(PALETTE_RGB, key=lambda p: sum((a - b) ** 2 for a, b in zip(color, p, strict=True)))


def _blend(a: RGB, b: RGB, amount: float) -> RGB:
    r, g, bl = (round(x + (y - x) * amount) for x, y in zip(a, b, strict=True))
    return r, g, bl


def dominant_color(image: Image.Image) -> RGB:
    """The most frequent clearly colored tone of ``image`` (else its most frequent tone)."""
    small = image.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
    quantized = small.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    total = sum(count for count, _ in counts) or 1
    colors: list[tuple[int, RGB]] = []
    for count, pixel in counts:
        index = int(pixel)  # type: ignore[call-overload]
        rgb = (palette[index * 3], palette[index * 3 + 1], palette[index * 3 + 2])
        colors.append((count, rgb))
    if not colors:
        return (0, 0, 0)
    for count, rgb in colors:
        _, lightness, saturation = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        if count / total >= 0.08 and saturation >= 0.3 and 0.12 <= lightness <= 0.88:
            return rgb
    return colors[0][1]


def background_from(image: Image.Image) -> RGB:
    """A flat background color matching ``image``, pushed away from mid-grey.

    The lightness is moved to a clearly dark or clearly light value so black or white text
    reads well on it, then nudged toward the nearest panel color so the flat area dithers
    with little noise.
    """
    hue, lightness, saturation = colorsys.rgb_to_hls(*(c / 255 for c in dominant_color(image)))
    lightness = min(lightness, 0.22) if lightness < 0.55 else max(lightness, 0.84)
    saturation = min(saturation, 0.75)
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    color: RGB = (round(r * 255), round(g * 255), round(b * 255))
    return _blend(color, _nearest_palette(color), PALETTE_PULL)


def accent_from(image: Image.Image) -> RGB:
    """A saturated accent color taken from ``image`` (vinyl label)."""
    hue, lightness, saturation = colorsys.rgb_to_hls(*(c / 255 for c in dominant_color(image)))
    r, g, b = colorsys.hls_to_rgb(hue, min(max(lightness, 0.35), 0.6), max(saturation, 0.55))
    return round(r * 255), round(g * 255), round(b * 255)


# --- Drawing helpers --------------------------------------------------------------------
def _drop_shadow(canvas: Image.Image, box: tuple[int, int, int, int], offset: int) -> None:
    """Darken a soft, offset rectangle under ``box`` (in place)."""
    mask = Image.new("L", canvas.size, 0)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(mask).rectangle(
        (x0 + offset, y0 + offset, x1 + offset, y1 + offset), fill=SHADOW_OPACITY
    )
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, round(offset * 1.5))))
    canvas.paste((0, 0, 0), (0, 0), mask)


def _paste_framed(
    canvas: Image.Image, picture: Image.Image, origin: tuple[int, int], *, border: int
) -> None:
    """Paste ``picture`` at ``origin`` with a shadow and an optional light border."""
    x, y = origin
    box = (x, y, x + picture.width - 1, y + picture.height - 1)
    _drop_shadow(canvas, box, max(3, round(min(canvas.size) * 0.01)))
    if border > 0:
        ImageDraw.Draw(canvas).rectangle(
            (x - border, y - border, box[2] + border, box[3] + border), fill=POSTER_BORDER_COLOR
        )
    canvas.paste(picture, (x, y))


def _square_cover(image: Image.Image, side: int, background: RGB) -> Image.Image:
    """``image`` fitted (uncropped) into a ``side`` square, padded with ``background``."""
    fitted = fit_inside(image, (side, side))
    if fitted.size == (side, side):
        return fitted
    square = Image.new("RGB", (side, side), background)
    square.paste(fitted, ((side - fitted.width) // 2, (side - fitted.height) // 2))
    return square


def _vinyl_disc(diameter: int, label: RGB) -> Image.Image:
    """An anti-aliased RGBA record (supersampled, then downscaled)."""
    big = diameter * _SUPERSAMPLE
    disc = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(disc)
    draw.ellipse((0, 0, big - 1, big - 1), fill=(*VINYL_BLACK, 255))
    radius = big / 2
    groove_width = max(1, round(big * 0.002))
    fraction = 0.92
    while fraction > 0.42:
        inset = radius * (1 - fraction)
        draw.ellipse(
            (inset, inset, big - 1 - inset, big - 1 - inset),
            outline=(*VINYL_GROOVE, 255),
            width=groove_width,
        )
        fraction -= 0.07
    label_inset = radius * (1 - 0.34)
    draw.ellipse(
        (label_inset, label_inset, big - 1 - label_inset, big - 1 - label_inset),
        fill=(*label, 255),
    )
    hole_inset = radius * (1 - 0.035)
    draw.ellipse(
        (hole_inset, hole_inset, big - 1 - hole_inset, big - 1 - hole_inset),
        fill=(*VINYL_BLACK, 255),
    )
    return disc.resize((diameter, diameter), Image.Resampling.LANCZOS)


@dataclass(frozen=True, slots=True)
class _TextBlock:
    """Fitted lines of a template, top to bottom, with the gap before each one."""

    parts: tuple[tuple[FittedText, int], ...]

    @property
    def height(self) -> int:
        return sum(fitted.height + gap for fitted, gap in self.parts if fitted.lines)

    def draw(
        self, image: Image.Image, *, x: int, y: int, width: int, fill: RGB, align: Align
    ) -> None:
        draw = ImageDraw.Draw(image)
        cursor = y
        for fitted, gap in self.parts:
            if not fitted.lines:
                continue
            cursor += gap
            cursor = draw_text(draw, fitted, x=x, y=cursor, width=width, fill=fill, align=align)


def _album_text(layout: AlbumLayout, width: int, unit: int, scale: float) -> _TextBlock:
    """Title (bold, up to 2 lines), artist and album; sizes relative to ``unit``."""

    def size(fraction: float) -> int:
        return max(8, round(unit * fraction * scale))

    title = fit_text(
        layout.title,
        weight="bold",
        max_width=width,
        max_lines=2,
        max_size=size(0.072),
        min_size=size(0.042),
    )
    artist = fit_text(
        layout.artist or "",
        weight="regular",
        max_width=width,
        max_lines=1,
        max_size=size(0.05),
        min_size=size(0.034),
    )
    album = fit_text(
        layout.album or "",
        weight="regular",
        max_width=width,
        max_lines=1,
        max_size=size(0.038),
        min_size=size(0.028),
    )
    return _TextBlock(((title, 0), (artist, size(0.02)), (album, size(0.014))))


def _fit_album_text(layout: AlbumLayout, width: int, height: int, unit: int) -> _TextBlock:
    """The album text block, scaled down until it fits ``height``."""
    scale = 1.0
    block = _album_text(layout, width, unit, scale)
    while block.height > height and scale > 0.5:
        scale *= 0.9
        block = _album_text(layout, width, unit, scale)
    return block


# --- Templates --------------------------------------------------------------------------
def compose_poster(image: Image.Image, canvas: Canvas) -> Image.Image:
    """The full poster centered over a blurred, darkened backdrop of itself."""
    width, height = canvas.size
    backdrop = blurred_background(image, canvas.size)
    backdrop = ImageEnhance.Brightness(backdrop).enhance(POSTER_BACKDROP_DARKEN)
    backdrop = enhance(backdrop, canvas.eink, edge_filters=False)
    inset = canvas.pad(POSTER_INSET)
    poster = enhance(fit_inside(image, (width - 2 * inset, height - 2 * inset)), canvas.eink)
    border = max(2, round(canvas.short_side * 0.003))
    origin = ((width - poster.width) // 2, (height - poster.height) // 2)
    _paste_framed(backdrop, poster, origin, border=border)
    return backdrop


def compose_album(image: Image.Image, layout: AlbumLayout, canvas: Canvas) -> Image.Image:
    """Square cover (sleeve or vinyl) with the track's typography block."""
    width, height = canvas.size
    background = background_from(image)
    result = Image.new("RGB", canvas.size, background)
    text_color = pick_text_color(background)
    pad = canvas.pad(0.06)
    gap = round(canvas.short_side * 0.045)
    vinyl = layout.style == "vinyl"
    group_factor = 1 + VINYL_PEEK if vinyl else 1.0

    if canvas.portrait:
        min_text = round(height * 0.2)
        cover = min(round((width - 2 * pad) / group_factor), height - 2 * pad - gap - min_text)
        if not vinyl:
            cover = min(cover, round(width * 0.82))
        text_width = width - 2 * pad
        text_height = height - 2 * pad - cover - gap
        block = _fit_album_text(layout, text_width, text_height, canvas.short_side)
        group_height = cover + gap + block.height
        top = pad + max(0, (height - 2 * pad - group_height) // 2)
        left = (width - round(cover * group_factor)) // 2
        text_box = (pad, top + cover + gap)
        align: Align = "center"
    else:
        text_min = round(width * 0.3)
        cover = min(height - 2 * pad, round((width - 2 * pad - gap - text_min) / group_factor))
        left = pad
        top = (height - cover) // 2
        text_x = left + round(cover * group_factor) + gap
        text_width = width - pad - text_x
        block = _fit_album_text(layout, text_width, height - 2 * pad, canvas.short_side)
        text_box = (text_x, (height - block.height) // 2)
        align = "left"

    sleeve = enhance(_square_cover(image, cover, background), canvas.eink)
    if vinyl:
        disc = _vinyl_disc(cover - 2 * max(2, round(cover * 0.02)), accent_from(image))
        disc_x = left + round(cover * VINYL_PEEK)
        disc_y = top + (cover - disc.height) // 2
        result.paste(disc, (disc_x, disc_y), disc)
    _paste_framed(result, sleeve, (left, top), border=0)
    block.draw(result, x=text_box[0], y=text_box[1], width=text_width, fill=text_color, align=align)
    return result


def compose_title_card(layout: TitleCardLayout, canvas: Canvas) -> Image.Image:
    """A centered title, an accent rule, then subtitle and caption, on black."""
    width, height = canvas.size
    result = Image.new("RGB", canvas.size, TITLE_CARD_BACKGROUND)
    fill = pick_text_color(TITLE_CARD_BACKGROUND)
    pad = canvas.pad(0.09)
    text_width = width - 2 * pad
    unit = canvas.short_side
    title = fit_text(
        layout.title,
        weight="bold",
        max_width=text_width,
        max_lines=4,
        max_size=round(unit * 0.1),
        min_size=round(unit * 0.05),
    )
    subtitle = fit_text(
        layout.subtitle or "",
        weight="regular",
        max_width=text_width,
        max_lines=2,
        max_size=round(unit * 0.05),
        min_size=round(unit * 0.034),
    )
    caption = fit_text(
        layout.caption or "",
        weight="regular",
        max_width=text_width,
        max_lines=2,
        max_size=round(unit * 0.036),
        min_size=round(unit * 0.026),
    )
    rule_height = max(4, round(unit * 0.008))
    rule_gap = round(unit * 0.045)
    below = _TextBlock(((subtitle, 0), (caption, round(unit * 0.02))))
    total = title.height + rule_gap * 2 + rule_height + below.height
    top = max(pad, (height - total) // 2)
    draw = ImageDraw.Draw(result)
    cursor = draw_text(draw, title, x=pad, y=top, width=text_width, fill=fill, align="center")
    cursor += rule_gap
    rule_width = round(text_width * 0.22)
    rule_x = (width - rule_width) // 2
    rule = (rule_x, cursor, rule_x + rule_width - 1, cursor + rule_height - 1)
    draw.rectangle(rule, fill=layout.accent)
    cursor += rule_height + rule_gap
    below.draw(result, x=pad, y=cursor, width=text_width, fill=fill, align="center")
    return result


def compose_layout(image: Image.Image | None, layout: Layout, canvas: Canvas) -> Image.Image:
    """Compose ``layout`` for ``canvas`` (viewing orientation, RGB, frame-sized).

    Raises:
        ValueError: If the layout needs artwork and ``image`` is ``None``.
    """
    if isinstance(layout, TitleCardLayout):
        composed = compose_title_card(layout, canvas)
    elif image is None:
        raise ValueError(f"The {layout.name} template needs an image")
    elif isinstance(layout, PosterLayout):
        composed = compose_poster(image.convert("RGB"), canvas)
    else:
        composed = compose_album(image.convert("RGB"), layout, canvas)
    if composed.size != canvas.size:  # pragma: no cover - defensive
        raise RuntimeError(f"Template produced {composed.size}, expected {canvas.size}")
    return composed
