"""Typography for composition templates: bundled fonts, wrapping, fitting and contrast.

The Inter font family (SIL Open Font License 1.1, see ``app/assets/fonts/OFL.txt``) is
bundled with the package so text never falls back to Pillow's tiny bitmap font.

Text is always drawn in pure palette black or white: those are exact Spectra 6 colors,
so glyphs survive dithering without colored fringes. :func:`pick_text_color` chooses the
one with the higher WCAG contrast against the background it is drawn on.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Final, Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FontWeight = Literal["regular", "bold"]
RGB = tuple[int, int, int]
Align = Literal["left", "center"]

FONT_FILES: Final[dict[FontWeight, str]] = {
    "regular": "Inter-Regular.ttf",
    "bold": "Inter-Bold.ttf",
}
BLACK: Final[RGB] = (0, 0, 0)
WHITE: Final[RGB] = (255, 255, 255)
ELLIPSIS: Final = "\u2026"
#: Characters stripped before an ellipsis so lines never end with "word,…".
_TRAILING_PUNCTUATION: Final = " ,;:-\u2013\u2014/|"
#: Font size step when shrinking text to fit (fraction of the current size).
_SHRINK_STEP: Final = 0.92
DEFAULT_LINE_SPACING: Final = 1.18


# --- Fonts ------------------------------------------------------------------------------
@lru_cache(maxsize=len(FONT_FILES))
def font_bytes(weight: FontWeight) -> bytes:
    """Raw TTF data of a bundled font weight (read once from the package)."""
    resource = resources.files("app").joinpath("assets", "fonts", FONT_FILES[weight])
    return resource.read_bytes()


@lru_cache(maxsize=128)
def load_font(weight: FontWeight, size: int) -> ImageFont.FreeTypeFont:
    """The bundled Inter font at ``size`` pixels (cached per weight and size).

    Raises:
        ValueError: If ``size`` is not positive.
    """
    if size <= 0:
        raise ValueError("Font size must be positive")
    return ImageFont.truetype(io.BytesIO(font_bytes(weight)), size)


def line_height(font: ImageFont.FreeTypeFont, spacing: float = DEFAULT_LINE_SPACING) -> int:
    """Distance between two baselines for ``font``."""
    ascent, descent = font.getmetrics()
    return round((ascent + descent) * spacing)


# --- Contrast ---------------------------------------------------------------------------
def _linearize(channel: np.ndarray) -> np.ndarray:
    srgb = channel / 255.0
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def relative_luminance(color: RGB) -> float:
    """WCAG 2.x relative luminance of an sRGB color (``0`` black .. ``1`` white)."""
    r, g, b = (_linearize(np.array(float(c))) for c in color)
    return float(0.2126 * r + 0.7152 * g + 0.0722 * b)


def contrast_ratio(luminance_a: float, luminance_b: float) -> float:
    """WCAG contrast ratio between two relative luminances (``1`` .. ``21``)."""
    light, dark = max(luminance_a, luminance_b), min(luminance_a, luminance_b)
    return (light + 0.05) / (dark + 0.05)


def mean_luminance(region: Image.Image) -> float:
    """Average relative luminance of the pixels of ``region`` (any mode)."""
    pixels = np.asarray(region.convert("RGB"), dtype=np.float64)
    linear = _linearize(pixels)
    luminance = 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]
    return float(luminance.mean())


def pick_text_color(background: Image.Image | RGB) -> RGB:
    """Black or white, whichever contrasts more with ``background``.

    Args:
        background: The area the text is drawn on (sampled pixel by pixel), or a flat color.
    """
    if isinstance(background, Image.Image):
        luminance = mean_luminance(background)
    else:
        luminance = relative_luminance(background)
    on_black = contrast_ratio(luminance, 0.0)
    on_white = contrast_ratio(luminance, 1.0)
    return BLACK if on_black >= on_white else WHITE


# --- Wrapping ---------------------------------------------------------------------------
def _fits(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> bool:
    return font.getlength(text) <= max_width


def ellipsize(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, *, force: bool = False
) -> str:
    """``text`` truncated with an ellipsis so it fits ``max_width``.

    Args:
        text: Text to shorten.
        font: Font used to measure.
        max_width: Available width in pixels.
        force: Always end with an ellipsis (the text is known to continue); otherwise text
            that fits is returned unchanged.
    """
    text = text.strip()
    if force:
        forced = text.rstrip(_TRAILING_PUNCTUATION) + ELLIPSIS
        if _fits(forced, font, max_width):
            return forced
    elif _fits(text, font, max_width):
        return text
    cut = text
    while cut:
        cut = cut[:-1]
        candidate = cut.rstrip(_TRAILING_PUNCTUATION) + ELLIPSIS
        if _fits(candidate, font, max_width):
            return candidate
    return ELLIPSIS if _fits(ELLIPSIS, font, max_width) else ""


def _split_long_word(word: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Hard-break a word wider than ``max_width`` into pieces that each fit."""
    pieces: list[str] = []
    rest = word
    while rest and not _fits(rest, font, max_width):
        cut = len(rest) - 1
        while cut > 1 and not _fits(rest[:cut], font, max_width):
            cut -= 1
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def wrap_lines(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word wrap of ``text`` into lines no wider than ``max_width`` (no limit)."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if _fits(candidate, font, max_width):
            current = candidate
            continue
        if current:
            lines.append(current)
        pieces = _split_long_word(word, font, max_width)
        lines.extend(pieces[:-1])
        current = pieces[-1] if pieces else ""
    if current:
        lines.append(current)
    return lines


def wrap_text(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int
) -> tuple[list[str], bool]:
    """Wrap ``text`` into at most ``max_lines`` lines, ellipsizing the last one if needed.

    Returns:
        ``(lines, truncated)`` where ``truncated`` tells whether text was cut.
    """
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    lines = wrap_lines(text, font, max_width)
    if len(lines) <= max_lines:
        return lines, False
    kept = lines[:max_lines]
    kept[-1] = ellipsize(kept[-1], font, max_width, force=True)
    return kept, True


# --- Fitting & drawing ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FittedText:
    """Wrapped lines and the font they fit with."""

    lines: tuple[str, ...]
    font: ImageFont.FreeTypeFont
    size: int
    truncated: bool
    spacing: float = DEFAULT_LINE_SPACING

    @property
    def line_height(self) -> int:
        """Baseline-to-baseline distance."""
        return line_height(self.font, self.spacing)

    @property
    def height(self) -> int:
        """Total block height (last line without trailing spacing)."""
        if not self.lines:
            return 0
        ascent, descent = self.font.getmetrics()
        return self.line_height * (len(self.lines) - 1) + ascent + descent

    @property
    def width(self) -> int:
        """Width of the widest line."""
        return max((round(self.font.getlength(line)) for line in self.lines), default=0)


def fit_text(
    text: str,
    *,
    weight: FontWeight,
    max_width: int,
    max_lines: int,
    max_size: int,
    min_size: int,
    spacing: float = DEFAULT_LINE_SPACING,
) -> FittedText:
    """Largest font size in ``[min_size, max_size]`` at which ``text`` fits without cutting.

    If even ``min_size`` needs more than ``max_lines`` lines, the text is ellipsized at
    ``min_size``.
    """
    if min_size > max_size:
        raise ValueError("min_size must not exceed max_size")
    text = " ".join(text.split())
    size = max_size
    while True:
        font = load_font(weight, size)
        lines, truncated = wrap_text(text, font, max_width, max_lines) if text else ([], False)
        if not truncated or size <= min_size:
            return FittedText(tuple(lines), font, size, truncated, spacing)
        size = max(min_size, int(size * _SHRINK_STEP))


def draw_text(
    draw: ImageDraw.ImageDraw,
    fitted: FittedText,
    *,
    x: int,
    y: int,
    width: int,
    fill: RGB,
    align: Align = "left",
) -> int:
    """Draw ``fitted`` with its top-left at ``(x, y)`` inside a box ``width`` wide.

    Text is rendered without anti-aliasing: grey edge pixels would be dithered into
    colored fringes (yellow next to white glyphs on dark backgrounds), whereas aliased
    glyphs stay a single exact palette color and look crisp at panel resolution.

    Returns:
        The y coordinate just below the block.
    """
    draw.fontmode = "1"
    baseline_step = fitted.line_height
    for index, line in enumerate(fitted.lines):
        line_x = x
        if align == "center":
            line_x = x + (width - round(fitted.font.getlength(line))) // 2
        position = (line_x, y + index * baseline_step)
        draw.text(position, line, font=fitted.font, fill=fill, anchor="la")
    return y + fitted.height
