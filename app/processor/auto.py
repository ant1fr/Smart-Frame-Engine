"""Automatic tone-profile and dithering choice (``eink.profile`` / ``eink.dither`` = ``auto``).

Nobody should have to tune a color profile or a dithering algorithm per image. The engine
decides from the *context* of the image (a photo, a movie poster, an album cover or a title
card) and a handful of cheap statistics measured on a <= 256 px thumbnail (a few
milliseconds, so it is free next to dithering, even on a Raspberry Pi):

Profile (tone and chroma curve)::

    title card ............................ natural   (flat colors stay exactly as drawn)
    graphic ............................... official  (the official converter's look)
    album cover (photographic) ............ soft      (lifted shadows, restrained highlights)
    poster, contrasty / bright highlights . photo     (deep blacks, rich colors, no burnt skin)
    poster, flat or muted ................. vivid     (richer colors)
    photo, black and white ................ bw        (grey tones; black/white-only dithering)
    photo, dark ........................... soft      (lifted shadows)
    photo, bright (snow, beach, fog) ...... vivid     (keeps the bright whites)
    photo, low contrast ................... soft
    other photos .......................... photo     (balanced curve: gentle contrast, a bit
                                                      more color; tuned on a real panel)

"Graphic" depends on what the image is: title cards always; album covers with very few
colors or large flat areas; posters with very few colors. A photo is never graphic: a night
shot with a big black sky or a snowfield is still a photograph.

Dithering::

    title card, graphic, album cover ..... atkinson-classic  (crisp edges, clean flats; covers
                                                              carry flat lettering that plain
                                                              error diffusion smears downward)
    photo with large smooth regions ...... stucki            (smooth skies and gradients)
    everything else ...................... fs-serpentine     (detail without "worm" artifacts;
                                                              plain fs if numba is missing)

An explicit value in the settings always wins; ``auto`` only fills in what was left to the
engine. The decision (with its reasons) is reported with the image in ``GET /api/status``.
Thresholds are module constants, calibrated with ``scripts/quality_ab.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from app.core.config_models import ConcreteDither, ConcreteProfile, EinkConfig
from app.processor.dither import effective_algorithm
from app.processor.quality import measure

ContentContext = Literal["photo", "poster", "album", "title_card"]

THUMBNAIL_LIMIT: Final = 256
#: 8x8 blocks whose luma standard deviation is at most this count as smooth.
SMOOTH_BLOCK_STD: Final = 4.0
#: Share of smooth blocks from which a photo counts as "mostly smooth" (sky, gradient, fog).
SMOOTH_PHOTO_SHARE: Final = 0.55
#: Graphic album covers: this few colors cover 90% of the pixels, or one color covers this
#: share (%). Posters often have a big dark background, so only the color count counts.
GRAPHIC_MAX_COLORS: Final = 12
GRAPHIC_FLAT_SHARE: Final = 45.0
GRAPHIC_POSTER_MAX_COLORS: Final = 24
#: Dark photos (mean luma) and flat-lit ones (luma standard deviation).
DARK_LUMA_MEAN: Final = 60.0
LOW_CONTRAST_STD: Final = 30.0
#: Bright photos (snow, beach, fog): a high mean and almost no black. They keep their whites
#: with ``vivid`` instead of turning grey.
HIGH_KEY_LUMA_MEAN: Final = 150.0
HIGH_KEY_MAX_BLACK_SHARE: Final = 2.0
#: Posters with this share (%) of near-white pixels, or this much contrast, are "contrasty":
#: they keep deep blacks and natural skin tones (photo) instead of being pushed (vivid).
HIGHLIGHT_LUMA: Final = 235.0
BLACK_LUMA: Final = 20.0
POSTER_HIGHLIGHT_SHARE: Final = 3.0
POSTER_HIGH_CONTRAST_STD: Final = 60.0
#: Photos with a Hasler-Susstrunk colorfulness below this are black and white (measured: B&W
#: scans and conversions 0-2, colour photos 40+; sepia and toned prints stay well above).
MONO_COLORFULNESS: Final = 8.0


@dataclass(frozen=True, slots=True)
class ContentStats:
    """Cheap statistics of a (thumbnail-sized) image."""

    luma_mean: float
    luma_std: float
    highlight_share: float  # % of pixels with luma >= HIGHLIGHT_LUMA
    flat_share: float  # % of pixels sharing the most common (5-bit quantized) color
    dominant_colors: int  # number of 5-bit colors covering 90% of the pixels
    detail: float  # mean absolute luma gradient (0..255): high = textured
    smooth_share: float  # share (0..1) of 8x8 blocks that are nearly flat
    black_share: float = 0.0  # % of pixels with luma <= BLACK_LUMA
    colorfulness: float = 50.0  # Hasler-Susstrunk: ~0 for black and white, 40+ for colour

    def is_graphic(self, context: ContentContext) -> bool:
        """A drawing rather than a photograph (logo, illustration, typographic cover).

        Photos are never graphic, however flat their background: a night shot is 24-78%
        black and a snowfield is almost white, yet both are photographs.
        """
        if context == "title_card":
            return True
        if context == "album":
            return (
                self.dominant_colors <= GRAPHIC_MAX_COLORS or self.flat_share >= GRAPHIC_FLAT_SHARE
            )
        if context == "poster":
            return self.dominant_colors <= GRAPHIC_POSTER_MAX_COLORS
        return False

    def is_smooth(self, context: ContentContext) -> bool:
        """A photo with large smooth regions (sky, fog, studio backdrop)."""
        return (
            context == "photo"
            and not self.is_graphic(context)
            and self.smooth_share >= SMOOTH_PHOTO_SHARE
        )

    def is_monochrome(self, context: ContentContext) -> bool:
        """A black-and-white photograph (no color to speak of)."""
        return context == "photo" and self.colorfulness < MONO_COLORFULNESS

    @property
    def high_key(self) -> bool:
        """Bright and without blacks (snow, beach, fog)."""
        return self.luma_mean >= HIGH_KEY_LUMA_MEAN and self.black_share < HIGH_KEY_MAX_BLACK_SHARE


class RenderDecision(BaseModel):
    """What the renderer used for one image, and why (reported in the image metadata)."""

    model_config = ConfigDict(frozen=True)

    context: ContentContext
    profile: ConcreteProfile
    dither: ConcreteDither
    automatic: bool = Field(description="At least one of profile / dither was chosen by 'auto'")
    reasons: list[str] = Field(default_factory=list)


def measure_content(image: Image.Image) -> ContentStats:
    """Measure :class:`ContentStats` on a copy downscaled to at most 256 px."""
    small = image.convert("RGB")
    small.thumbnail((THUMBNAIL_LIMIT, THUMBNAIL_LIMIT))
    base = measure(small)
    rgb = np.asarray(small, dtype=np.float32)
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    if luma.shape[0] > 1 and luma.shape[1] > 1:
        detail = float(
            (np.abs(np.diff(luma, axis=1)).mean() + np.abs(np.diff(luma, axis=0)).mean()) / 2
        )
    else:
        detail = 0.0
    rows, cols = (luma.shape[0] // 8) * 8, (luma.shape[1] // 8) * 8
    if rows and cols:
        blocks = luma[:rows, :cols].reshape(rows // 8, 8, cols // 8, 8).std(axis=(1, 3))
        smooth_share = float((blocks <= SMOOTH_BLOCK_STD).mean())
    else:
        smooth_share = 0.0

    quantized = rgb.astype(np.uint8) >> 3
    codes = (
        quantized[..., 0].astype(np.int32) * 1024
        + quantized[..., 1].astype(np.int32) * 32
        + quantized[..., 2].astype(np.int32)
    ).ravel()
    counts = np.sort(np.bincount(codes))[::-1]
    covered = np.cumsum(counts) / codes.size
    return ContentStats(
        luma_mean=base.luma_mean,
        luma_std=base.luma_std,
        highlight_share=float((luma >= HIGHLIGHT_LUMA).mean() * 100),
        black_share=float((luma <= BLACK_LUMA).mean() * 100),
        colorfulness=base.colorfulness,
        flat_share=base.flat_share,
        dominant_colors=int(np.searchsorted(covered, 0.9) + 1),
        detail=detail,
        smooth_share=smooth_share,
    )


def decide_profile(
    context: ContentContext, stats: ContentStats | None
) -> tuple[ConcreteProfile, str]:
    """The tone and chroma profile for an image (``stats`` is ``None`` without artwork)."""
    if context == "title_card" or stats is None:
        return "natural", "title card: flat colors stay as drawn"
    if stats.is_graphic(context):
        return "official", (
            f"graphic ({stats.dominant_colors} dominant colors, {stats.flat_share:.0f}% flat)"
        )
    if context == "album":
        return "soft", "photographic album cover: lifted shadows, restrained highlights"
    if context == "poster":
        if (
            stats.highlight_share >= POSTER_HIGHLIGHT_SHARE
            or stats.luma_std >= POSTER_HIGH_CONTRAST_STD
        ):
            return "photo", (
                f"contrasty poster ({stats.highlight_share:.1f}% highlights, "
                f"contrast {stats.luma_std:.0f}): deep blacks, rich colors, natural skin"
            )
        return "vivid", f"flat or muted poster (contrast {stats.luma_std:.0f}): richer colors"
    if stats.is_monochrome(context):
        return "bw", (
            f"black and white photo (colorfulness {stats.colorfulness:.1f}): grey tones, "
            "black and white dots only"
        )
    if stats.luma_mean < DARK_LUMA_MEAN:
        return "soft", f"dark photo (mean luma {stats.luma_mean:.0f}): lift shadows"
    if stats.high_key:
        return "vivid", (
            f"bright photo (mean luma {stats.luma_mean:.0f}, {stats.black_share:.1f}% black): "
            "keeps the whites"
        )
    if stats.luma_std < LOW_CONTRAST_STD:
        return "soft", (
            f"low-contrast photo (contrast {stats.luma_std:.0f}): gentle tones keep the detail"
        )
    return "photo", "photo"


def decide_dither(
    context: ContentContext, stats: ContentStats | None
) -> tuple[ConcreteDither, str]:
    """The dithering algorithm for an image."""
    if context == "title_card" or stats is None:
        return "atkinson-classic", "title card: crisp text edges and clean flats"
    if stats.is_graphic(context):
        return "atkinson-classic", "graphic: crisp edges and clean flats"
    if context == "album":
        return "atkinson-classic", "album cover: lettering stays crisp, no error smearing"
    if stats.is_smooth(context):
        return "stucki", (
            f"mostly smooth ({stats.smooth_share * 100:.0f}% flat blocks): smooth gradients"
        )
    return effective_algorithm("fs-serpentine"), "detailed image: no directional artifacts"


def resolve(
    eink: EinkConfig, context: ContentContext, stats: ContentStats | None
) -> tuple[EinkConfig, RenderDecision]:
    """Resolve ``auto`` settings into a concrete :class:`EinkConfig` and the decision.

    Explicit profile and dither settings are kept as they are.
    """
    reasons: list[str] = []
    automatic = False
    if eink.profile == "auto":
        profile, reason = decide_profile(context, stats)
        reasons.append(f"profile {profile}: {reason}")
        automatic = True
    else:
        profile = eink.profile
        reasons.append(f"profile {profile}: set in the settings")
    if eink.dither == "auto":
        dither, reason = decide_dither(context, stats)
        reasons.append(f"dither {dither}: {reason}")
        automatic = True
    else:
        dither = "fs" if eink.dither == "floyd_steinberg" else eink.dither
        reasons.append(f"dither {dither}: set in the settings")
    concrete = eink.resolved(profile).model_copy(update={"dither": dither})
    decision = RenderDecision(
        context=context, profile=profile, dither=dither, automatic=automatic, reasons=reasons
    )
    return concrete, decision
