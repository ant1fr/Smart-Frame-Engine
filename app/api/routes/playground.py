"""Render playground: compare eInk profiles and dithering on an image, without displaying it."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, File, Form, HTTPException, Security, UploadFile
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.api.deps import ConfigStoreDep, DisplayServiceDep, SettingsDep
from app.core.auth import api_key_scheme
from app.core.config_models import DitherAlgorithm, EinkProfile, FitMode, RuntimeConfig
from app.core.security import InvalidImageError, PayloadTooLargeError, read_upload
from app.processor.auto import RenderDecision
from app.processor.pipeline import RenderOptions, RenderResult, decode_image, render, render_layout
from app.processor.templates import (
    AlbumLayout,
    AlbumStyle,
    Layout,
    PosterLayout,
    TitleCardLayout,
)

router = APIRouter(
    prefix="/api/playground", tags=["playground"], dependencies=[Security(api_key_scheme)]
)

MAX_VARIANTS: Final = 4
MAX_TEXT_LENGTH: Final = 200
PREVIEW_JPEG_QUALITY: Final = 85
PlaygroundTemplate = Literal["none", "poster", "album", "title_card"]


class PlaygroundVariant(BaseModel):
    """One rendering to try: ``auto`` lets the engine decide, like a real display."""

    model_config = ConfigDict(extra="forbid")

    profile: EinkProfile = "auto"
    dither: DitherAlgorithm = "auto"


_VARIANTS: Final = TypeAdapter(list[PlaygroundVariant])
DEFAULT_VARIANTS: Final = '[{"profile": "auto", "dither": "auto"}]'


class VariantResult(BaseModel):
    """The output of one variant, as data URLs ready for an ``<img>``."""

    requested: PlaygroundVariant
    decision: RenderDecision | None = Field(description="What was actually used, and why")
    composed: str = Field(description="data:image/jpeg URL of the composition (before dither)")
    dithered: str = Field(description="data:image/png URL of the exact 6-color panel output")
    elapsed_ms: float = Field(description="Rendering time (compose + dither + encode)")


class PlaygroundResult(BaseModel):
    """Body of ``POST /api/playground/render``."""

    template: PlaygroundTemplate
    frame_size: tuple[int, int]
    variants: list[VariantResult]


def _parse_variants(raw: str) -> list[PlaygroundVariant]:
    try:
        variants = _VARIANTS.validate_python(json.loads(raw or DEFAULT_VARIANTS))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=422, detail="variants must be a JSON list of {profile, dither}"
        ) from exc
    if not 1 <= len(variants) <= MAX_VARIANTS:
        raise HTTPException(status_code=422, detail=f"Give between 1 and {MAX_VARIANTS} variants")
    return variants


def _layout(
    template: PlaygroundTemplate,
    title: str | None,
    subtitle: str | None,
    caption: str | None,
    style: AlbumStyle,
) -> Layout | None:
    if template == "poster":
        return PosterLayout()
    if template == "album":
        return AlbumLayout(title=title or None, artist=subtitle, album=caption, style=style)
    if template == "title_card":
        return TitleCardLayout(title=title or "Title card", subtitle=subtitle, caption=caption)
    return None


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _run_variants(
    data: bytes | None,
    layout: Layout | None,
    fit: FitMode | None,
    config: RuntimeConfig,
    variants: list[PlaygroundVariant],
) -> list[VariantResult]:
    """Decode once, then render every variant (worker thread)."""
    image = decode_image(data) if data is not None else None
    base = RenderOptions.from_config(config, fit="contain" if layout is not None else fit)
    results: list[VariantResult] = []
    for variant in variants:
        eink = config.eink.model_copy(
            update={"profile": variant.profile, "dither": variant.dither}
        ).effective()
        options = replace(base, eink=eink)
        started = time.perf_counter()
        result: RenderResult
        if layout is not None:
            result = render_layout(image, layout, options)
        elif image is not None:
            result = render(image, options)
        else:
            raise InvalidImageError("No image to render")
        elapsed = (time.perf_counter() - started) * 1000
        results.append(
            VariantResult(
                requested=variant,
                decision=result.decision,
                composed=_data_url("image/jpeg", result.preview_jpeg(PREVIEW_JPEG_QUALITY)),
                dithered=_data_url("image/png", result.dithered_png()),
                elapsed_ms=round(elapsed, 1),
            )
        )
    return results


_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {"description": "File larger than MAX_UPLOAD_MB"},
    415: {"description": "Not a supported image"},
    422: {"description": "Invalid variants, or no image for a template that needs one"},
}


@router.post(
    "/render",
    response_model=PlaygroundResult,
    responses=_RESPONSES,
    summary="Render an image with several eInk profiles / dithers (never displayed)",
)
async def render_playground(
    settings: SettingsDep,
    config: ConfigStoreDep,
    display: DisplayServiceDep,
    file: Annotated[
        UploadFile | None, File(description="Image (not needed for the title card)")
    ] = None,
    template: Annotated[
        PlaygroundTemplate,
        Form(description="none = a photo with the fit mode; or a composition template"),
    ] = "none",
    fit: Annotated[FitMode | None, Form(description="Fit mode without a template")] = None,
    variants: Annotated[
        str,
        Form(description=f"JSON list of up to {MAX_VARIANTS} {{profile, dither}} objects"),
    ] = DEFAULT_VARIANTS,
    title: Annotated[str | None, Form(max_length=MAX_TEXT_LENGTH)] = None,
    subtitle: Annotated[
        str | None, Form(max_length=MAX_TEXT_LENGTH, description="Album artist / card subtitle")
    ] = None,
    caption: Annotated[
        str | None, Form(max_length=MAX_TEXT_LENGTH, description="Album name / card caption")
    ] = None,
    style: Annotated[AlbumStyle, Form(description="Album template style")] = "sleeve",
) -> PlaygroundResult:
    """Compare renderings side by side, e.g. ``auto`` against ``vivid`` + ``stucki``.

    Each variant returns the composition and the exact 6-color dithered output as data
    URLs, the decision actually applied and the rendering time. Nothing is stored, shown
    on the frame or added to the history; renders wait for any display in progress.
    """
    chosen = _parse_variants(variants)
    data: bytes | None = None
    if file is not None:
        try:
            data = await read_upload(file, settings.max_upload_bytes)
        except PayloadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        finally:
            await file.close()
    if data is None and template != "title_card":
        raise HTTPException(status_code=422, detail="Upload an image to render")
    layout = _layout(template, title, subtitle, caption, style)
    snapshot = config.config
    try:
        results = await display.render_detached(
            lambda: _run_variants(
                data if template != "title_card" else None, layout, fit, snapshot, chosen
            )
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    return PlaygroundResult(template=template, frame_size=snapshot.frame_size, variants=results)
