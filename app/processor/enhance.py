"""eInk enhancement pipeline (official Fraimic order and defaults, plus gamut-safe curves).

Order: [tone/chroma curve] -> Brightness -> Contrast -> Color (saturation) -> [Sharpness] ->
EDGE_ENHANCE -> SMOOTH -> SHARPEN. With ``sharpness == 1.0`` (default) the extra Sharpness
step is skipped, making the output identical to the official converter's ``process_image``.
Enhancement must run on the picture only (before letterbox padding) so flat bars do
not skew the contrast pivot and filters do not bleed across the picture/bar seam.

The ``photo``, ``soft`` and ``vivid`` profiles (adapted from the MIT-licensed Stillroom converter by
WillCroPoint, itself based on the official Fraimic converter) work on luminance in floating
point with endpoint-preserving curves, and compress chroma toward that luminance *only* when
it would leave the RGB gamut - one scale for all three channels, so hue is preserved instead of
clipping individual channels. ``natural`` leaves the source tones untouched. ``bw`` (black-and-white
photos) turns the picture into pure grey levels (:func:`mono_tone`); just before dithering
:func:`mono_dither_input` compensates for dots averaging in linear light, and the dithering
itself only uses black and white. No profile can
recover detail that is already clipped, nor remove the limits of a six-color display.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageEnhance, ImageFilter

from app.core.config_models import EinkConfig

_LUMA: Final = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
#: profile -> (shadow lift, midtone S-curve strength, highlight shoulder, chroma gain)
CURVES: Final[dict[str, tuple[float, float, float, float]]] = {
    "photo": (0.06, 0.25, 0.20, 1.07),  # balanced: gentle contrast and a little extra color
    "soft": (0.10, 0.0, 0.30, 1.05),  # lifted shadows, restrained highlights, still colorful
    "vivid": (0.10, 0.50, 0.15, 1.18),  # stronger contrast, richer color, gradual roll-off
}
#: ``bw`` levels: the darkest / brightest share of pixels is clipped before stretching, then a
#: gentle S-curve of this strength is applied.
MONO_CLIP_PERCENT: Final = (0.2, 99.8)
MONO_S_CURVE: Final = 0.25
#: Black/white dots average in *linear* light (50% white dots look like sRGB 188), so the grey
#: levels are darkened by this gamma before dithering, then the white point is lowered so
#: bright areas stay solid white instead of getting sparse black dots. Tuned on real photos.
MONO_GAMMA: Final = 1.8
MONO_WHITE: Final = 0.80
MONO_BLACK: Final = 0.0


def mono_tone(image: Image.Image) -> Image.Image:
    """Grey picture with full-range levels and a gentle S-curve (``bw`` profile).

    Colors (and casts) are dropped, the 0.2% / 99.8% percentiles become black and white, and
    mid-tones get a light contrast boost. Run on the picture only, before any padding.
    """
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    lum = rgb @ _LUMA
    low, high = np.percentile(lum, MONO_CLIP_PERCENT)
    if high - low > 1e-3:
        lum = np.clip((lum - low) / (high - low), 0.0, 1.0)
    lum = lum + MONO_S_CURVE * lum * (1 - lum) * (2 * lum - 1)
    grey = np.rint(np.clip(lum, 0.0, 1.0) * 255).astype(np.uint8)
    return Image.fromarray(np.dstack([grey, grey, grey]))


def mono_dither_input(rgb: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Grey levels to feed the black/white dithering (gamma, then black and white points)."""
    lum = (rgb.astype(np.float32) / 255.0) @ _LUMA
    level = np.clip((lum**MONO_GAMMA - MONO_BLACK) / (MONO_WHITE - MONO_BLACK), 0.0, 1.0)
    grey = np.rint(level * 255).astype(np.uint8)
    return np.ascontiguousarray(np.dstack([grey, grey, grey]))


def tone_curve(image: Image.Image, profile: str) -> Image.Image:
    """Apply the gamut-safe ``soft`` / ``vivid`` curve (see the module docstring)."""
    shadow, strength, shoulder, colour = CURVES[profile]
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    lum = (rgb * _LUMA).sum(axis=2, keepdims=True)
    target = (
        lum
        + shadow * lum * (1 - lum)
        + strength * lum * (1 - lum) * (2 * lum - 1)
        - shoulder * lum**4 * (1 - lum)
    )
    chroma = (rgb - lum) * colour
    # One scale for all channels keeps the hue at the gamut boundary.
    room = np.where(chroma > 0, 1 - target, target)
    scale = np.minimum(1.0, (room / np.maximum(np.abs(chroma), 1e-8)).min(axis=2, keepdims=True))
    mapped = target + chroma * scale
    return Image.fromarray(np.rint(np.clip(mapped, 0, 1) * 255).astype(np.uint8))


def enhance(image: Image.Image, eink: EinkConfig, *, edge_filters: bool = True) -> Image.Image:
    """Apply the eInk color and detail adjustments.

    Args:
        image: RGB picture (already scaled to its on-panel size).
        eink: Enhancement settings (a concrete profile; an unresolved ``auto`` behaves like
            ``official``).
        edge_filters: Set to ``False`` to skip the edge-enhance/smooth/sharpen chain
            regardless of ``eink.edge_filters`` (used for blurred backgrounds).
    """
    if eink.profile in CURVES:
        image = tone_curve(image, eink.profile)
    elif eink.profile == "bw":
        image = mono_tone(image)
    result = ImageEnhance.Brightness(image).enhance(eink.brightness)
    result = ImageEnhance.Contrast(result).enhance(eink.contrast)
    result = ImageEnhance.Color(result).enhance(eink.saturation)
    if eink.sharpness != 1.0:
        result = ImageEnhance.Sharpness(result).enhance(eink.sharpness)
    if edge_filters and eink.edge_filters:
        result = result.filter(ImageFilter.EDGE_ENHANCE)
        result = result.filter(ImageFilter.SMOOTH)
        result = result.filter(ImageFilter.SHARPEN)
    return result
