"""eInk enhancement pipeline (official Fraimic order and defaults).

Order: Brightness -> Contrast -> Color (saturation) -> [Sharpness] -> EDGE_ENHANCE ->
SMOOTH -> SHARPEN. With ``sharpness == 1.0`` (default) the extra Sharpness step is
skipped, making the output identical to the official converter's ``process_image``.
Enhancement must run on the picture only (before letterbox padding) so flat bars do
not skew the contrast pivot and filters do not bleed across the picture/bar seam.
"""

from __future__ import annotations

from PIL import Image, ImageEnhance, ImageFilter

from app.core.config_models import EinkConfig


def enhance(image: Image.Image, eink: EinkConfig, *, edge_filters: bool = True) -> Image.Image:
    """Apply the eInk color and detail adjustments.

    Args:
        image: RGB picture (already scaled to its on-panel size).
        eink: Enhancement settings.
        edge_filters: Set to ``False`` to skip the edge-enhance/smooth/sharpen chain
            regardless of ``eink.edge_filters`` (used for blurred backgrounds).
    """
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
