"""Image processing pipeline for Spectra 6 color eInk panels.

* :mod:`~app.processor.fit` - ``smart_crop`` (face-aware) and ``contain`` fit modes.
* :mod:`~app.processor.enhance` - official brightness/contrast/saturation/filter chain.
* :mod:`~app.processor.dither` - exact vectorized Atkinson / Floyd-Steinberg dithering.
* :mod:`~app.processor.packing` - EL133UF1 / EL315 ``.bin`` encoding.
* :mod:`~app.processor.pipeline` - end-to-end :func:`render`.
"""

from app.processor.fit import FocusRegion
from app.processor.pipeline import RenderOptions, RenderResult, decode_image, render, render_bytes

__all__ = [
    "FocusRegion",
    "RenderOptions",
    "RenderResult",
    "decode_image",
    "render",
    "render_bytes",
]
