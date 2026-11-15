from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app.processor.typography import (
    BLACK,
    ELLIPSIS,
    WHITE,
    contrast_ratio,
    draw_text,
    ellipsize,
    fit_text,
    font_bytes,
    load_font,
    pick_text_color,
    relative_luminance,
    wrap_lines,
    wrap_text,
)


def test_bundled_fonts_load_with_their_family_and_style() -> None:
    assert font_bytes("regular")[:4] == b"\x00\x01\x00\x00"
    assert load_font("regular", 32).getname() == ("Inter", "Regular")
    assert load_font("bold", 32).getname() == ("Inter", "Bold")
    assert load_font("bold", 32) is load_font("bold", 32)  # cached


def test_font_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        load_font("regular", 0)


def test_relative_luminance_and_contrast_follow_wcag() -> None:
    assert relative_luminance(BLACK) == pytest.approx(0.0)
    assert relative_luminance(WHITE) == pytest.approx(1.0)
    assert contrast_ratio(1.0, 0.0) == pytest.approx(21.0)


@pytest.mark.parametrize(
    ("background", "expected"),
    [
        ((10, 10, 10), WHITE),
        ((0, 0, 255), WHITE),  # panel blue is dark
        ((255, 0, 0), BLACK),  # panel red reads better with black (WCAG)
        ((255, 255, 0), BLACK),
        ((240, 240, 230), BLACK),
        ((60, 25, 10), WHITE),
    ],
)
def test_pick_text_color_on_flat_colors(
    background: tuple[int, int, int], expected: tuple[int, int, int]
) -> None:
    assert pick_text_color(background) == expected


def test_pick_text_color_samples_the_region_under_the_text() -> None:
    mostly_dark = Image.new("RGB", (100, 40), (20, 20, 20))
    ImageDraw.Draw(mostly_dark).rectangle((0, 0, 9, 39), fill=(255, 255, 255))
    assert pick_text_color(mostly_dark) == WHITE
    mostly_light = Image.new("RGB", (100, 40), (250, 250, 250))
    ImageDraw.Draw(mostly_light).rectangle((0, 0, 9, 39), fill=(0, 0, 0))
    assert pick_text_color(mostly_light) == BLACK


def test_wrap_lines_respects_the_width() -> None:
    font = load_font("regular", 40)
    text = "The quick brown fox jumps over the lazy dog again and again"
    lines = wrap_lines(text, font, 400)
    assert len(lines) > 1
    assert all(font.getlength(line) <= 400 for line in lines)
    assert " ".join(lines) == text


def test_wrap_lines_hard_breaks_words_wider_than_the_box() -> None:
    font = load_font("bold", 40)
    word = "Supercalifragilisticexpialidocious"
    lines = wrap_lines(f"A {word}", font, 200)
    assert all(font.getlength(line) <= 200 for line in lines)
    assert "".join(lines[1:]) == word


def test_wrap_text_ellipsizes_the_last_allowed_line() -> None:
    font = load_font("regular", 40)
    text = " ".join(["word"] * 40)
    lines, truncated = wrap_text(text, font, 300, 2)
    assert truncated
    assert len(lines) == 2
    assert lines[-1].endswith(ELLIPSIS)
    assert all(font.getlength(line) <= 300 for line in lines)


def test_wrap_text_keeps_short_text_intact() -> None:
    font = load_font("regular", 40)
    assert wrap_text("Kind of Blue", font, 600, 1) == (["Kind of Blue"], False)
    assert wrap_text("", font, 600, 1) == ([], False)
    with pytest.raises(ValueError):
        wrap_text("x", font, 600, 0)


def test_ellipsize_drops_trailing_punctuation_before_the_ellipsis() -> None:
    font = load_font("regular", 40)
    result = ellipsize("Hello, wonderful world of eInk displays", font, 160)
    assert result.endswith(ELLIPSIS)
    assert not result[:-1].endswith((",", " "))
    assert font.getlength(result) <= 160
    assert ellipsize("Hi", font, 160) == "Hi"
    assert ellipsize("Hi", font, 160, force=True) == f"Hi{ELLIPSIS}"


def test_fit_text_shrinks_before_truncating() -> None:
    text = "A rather long title that does not fit at the largest size"
    fitted = fit_text(text, weight="bold", max_width=700, max_lines=2, max_size=90, min_size=30)
    assert not fitted.truncated
    assert 30 <= fitted.size < 90
    assert len(fitted.lines) <= 2
    assert fitted.width <= 700


def test_fit_text_truncates_at_the_minimum_size() -> None:
    fitted = fit_text(
        "word " * 200, weight="regular", max_width=300, max_lines=1, max_size=60, min_size=40
    )
    assert fitted.truncated
    assert fitted.size == 40
    assert fitted.lines[0].endswith(ELLIPSIS)


def test_fit_text_validates_its_size_range() -> None:
    with pytest.raises(ValueError):
        fit_text("x", weight="regular", max_width=100, max_lines=1, max_size=10, min_size=20)


def test_draw_text_renders_aliased_single_color_glyphs() -> None:
    image = Image.new("RGB", (600, 200), BLACK)
    fitted = fit_text(
        "Crisp text", weight="bold", max_width=560, max_lines=1, max_size=80, min_size=40
    )
    bottom = draw_text(
        ImageDraw.Draw(image), fitted, x=20, y=20, width=560, fill=WHITE, align="center"
    )
    assert bottom == 20 + fitted.height
    colors = {color for _, color in image.getcolors(maxcolors=1024) or []}
    assert colors == {BLACK, WHITE}  # no anti-aliased greys that would dither to color
