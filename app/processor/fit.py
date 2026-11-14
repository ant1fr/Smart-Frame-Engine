"""Fit modes: face-aware ``smart_crop`` and ``contain`` with a padded background.

Scaling follows the official Fraimic converter (Lanczos resampling, ``int(round())``
sizes, centered placement), so that ``smart_crop`` without focus regions equals the
official ``--fit crop`` and ``contain`` with a black/white background equals the
official ``--fit letterbox``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from PIL import Image, ImageEnhance, ImageFilter

BackgroundStyle = Literal["blur", "black", "white"]
Size = tuple[int, int]

FLAT_BACKGROUNDS: Final[dict[str, tuple[int, int, int]]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
}
#: Vertical position of the faces' centre in a portrait-ish crop (leaves headroom).
VERTICAL_FOCUS_ANCHOR: Final = 0.4
#: The blurred background is computed at 1/BLUR_DOWNSCALE resolution for speed.
BLUR_DOWNSCALE: Final = 8
BLUR_RADIUS: Final = 5
BLUR_DARKEN: Final = 0.8


@dataclass(frozen=True, slots=True)
class FocusRegion:
    """A region of interest (e.g. a detected face) in normalized coordinates.

    Coordinates are fractions (``0..1``) of the EXIF-corrected image, with ``(x, y)``
    the top-left corner.
    """

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.x <= 1.0 and 0.0 <= self.y <= 1.0):
            raise ValueError("FocusRegion origin must be within 0..1")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("FocusRegion size must be positive")
        if self.x + self.width > 1.0 + 1e-6 or self.y + self.height > 1.0 + 1e-6:
            raise ValueError("FocusRegion must lie inside the image")

    @classmethod
    def from_pixels(
        cls, x1: float, y1: float, x2: float, y2: float, image_width: int, image_height: int
    ) -> FocusRegion:
        """Build a region from pixel corners measured on an image of the given size."""
        if image_width <= 0 or image_height <= 0:
            raise ValueError("Image size must be positive")
        left = min(max(min(x1, x2) / image_width, 0.0), 1.0)
        top = min(max(min(y1, y2) / image_height, 0.0), 1.0)
        right = min(max(max(x1, x2) / image_width, 0.0), 1.0)
        bottom = min(max(max(y1, y2) / image_height, 0.0), 1.0)
        return cls(left, top, max(right - left, 1e-6), max(bottom - top, 1e-6))

    @property
    def area(self) -> float:
        """Normalized area (fraction of the image)."""
        return self.width * self.height

    def padded(self, fraction: float) -> FocusRegion:
        """Grow every side by ``fraction`` of the region's own size, clamped to the image.

        Used for faces so hair, chin and shoulders are not cut at the crop edge.
        """
        if fraction < 0:
            raise ValueError("Padding fraction must not be negative")
        dx, dy = self.width * fraction, self.height * fraction
        left, top = max(self.x - dx, 0.0), max(self.y - dy, 0.0)
        right, bottom = min(self.x + self.width + dx, 1.0), min(self.y + self.height + dy, 1.0)
        return FocusRegion(left, top, right - left, bottom - top)


def _scaled_size(size: Size, ratio: float) -> Size:
    return max(1, round(size[0] * ratio)), max(1, round(size[1] * ratio))


def _place_window(lo: float, hi: float, window: int, total: int, anchor: float) -> int:
    """Start offset of a ``window`` along an axis of length ``total``.

    The window is positioned so the focus span ``[lo, hi]`` sits at ``anchor`` (fraction
    of the window). When the span fits, it is kept entirely inside the window.
    """
    start = (lo + hi) / 2 - anchor * window
    if hi - lo <= window:
        start = min(max(start, hi - window), lo)
    return round(min(max(start, 0.0), float(total - window)))


def regions_that_fit(
    regions: Sequence[FocusRegion], max_width: float, max_height: float
) -> list[FocusRegion]:
    """The largest-first subset of ``regions`` whose union fits a crop window.

    ``max_width``/``max_height`` are the window size as fractions of the image (values
    ``>= 1`` mean that axis is never cropped, so it never limits the selection). The
    biggest region is always kept - even when it alone exceeds the window - and every
    other region is added only if the union still fits. Two people on opposite edges of
    a wide photo therefore yield one whole face instead of two sliced ones.
    """
    if not regions:
        return []
    ordered = sorted(regions, key=lambda region: region.area, reverse=True)
    kept = [ordered[0]]
    x1, y1 = ordered[0].x, ordered[0].y
    x2, y2 = x1 + ordered[0].width, y1 + ordered[0].height
    for region in ordered[1:]:
        nx1, ny1 = min(x1, region.x), min(y1, region.y)
        nx2, ny2 = max(x2, region.x + region.width), max(y2, region.y + region.height)
        if nx2 - nx1 <= max_width + 1e-9 and ny2 - ny1 <= max_height + 1e-9:
            kept.append(region)
            x1, y1, x2, y2 = nx1, ny1, nx2, ny2
    return kept


def smart_crop(image: Image.Image, size: Size, regions: Sequence[FocusRegion] = ()) -> Image.Image:
    """Scale to fill ``size`` and crop the overflow, keeping focus regions in frame.

    Without regions this is a centered crop identical to the official ``crop`` fit.
    With regions, the ones that fit the crop window together (see
    :func:`regions_that_fit`) decide where it goes: it is moved along the overflowing
    axis so their union is centered horizontally, or placed at 40% of the height.
    """
    target_w, target_h = size
    ratio = max(target_w / image.width, target_h / image.height)
    resized_w, resized_h = _scaled_size(image.size, ratio)
    resized = image.resize((resized_w, resized_h), Image.Resampling.LANCZOS)

    left = (resized_w - target_w) // 2
    top = (resized_h - target_h) // 2
    regions = regions_that_fit(regions, target_w / resized_w, target_h / resized_h)
    if regions:
        x1 = min(r.x for r in regions) * resized_w
        x2 = max(r.x + r.width for r in regions) * resized_w
        y1 = min(r.y for r in regions) * resized_h
        y2 = max(r.y + r.height for r in regions) * resized_h
        if resized_w > target_w:
            left = _place_window(x1, x2, target_w, resized_w, 0.5)
        if resized_h > target_h:
            top = _place_window(y1, y2, target_h, resized_h, VERTICAL_FOCUS_ANCHOR)
    return resized.crop((left, top, left + target_w, top + target_h))


def fit_inside(image: Image.Image, size: Size) -> Image.Image:
    """Scale ``image`` to fit entirely inside ``size`` (no cropping)."""
    ratio = min(size[0] / image.width, size[1] / image.height)
    return image.resize(_scaled_size(image.size, ratio), Image.Resampling.LANCZOS)


def blurred_background(image: Image.Image, size: Size) -> Image.Image:
    """A soft, darkened, frame-filling version of ``image`` used behind ``contain``."""
    small = (max(1, size[0] // BLUR_DOWNSCALE), max(1, size[1] // BLUR_DOWNSCALE))
    cover = smart_crop(image, small).filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    upscaled = cover.resize(size, Image.Resampling.BICUBIC)
    return ImageEnhance.Brightness(upscaled).enhance(BLUR_DARKEN)


def make_background(image: Image.Image, size: Size, style: BackgroundStyle) -> Image.Image:
    """Frame-sized background for the ``contain`` fit mode."""
    if style == "blur":
        return blurred_background(image, size)
    return Image.new("RGB", size, FLAT_BACKGROUNDS[style])


def paste_centered(background: Image.Image, picture: Image.Image) -> Image.Image:
    """Center ``picture`` on a copy of ``background`` (official ``pad_to_frame`` offsets)."""
    if picture.size == background.size:
        return picture
    framed = background.copy()
    framed.paste(
        picture,
        ((background.width - picture.width) // 2, (background.height - picture.height) // 2),
    )
    return framed
