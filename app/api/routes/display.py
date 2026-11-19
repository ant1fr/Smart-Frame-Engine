"""Display endpoints (manual sources). Provider-based sources arrive in later milestones."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import PurePath
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, File, Form, HTTPException, Security, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import DisplayServiceDep, SchedulerDep, SettingsDep
from app.core.auth import api_key_scheme
from app.core.config_models import FitMode
from app.core.security import InvalidImageError, PayloadTooLargeError, read_upload
from app.processor.templates import TitleCardLayout
from app.services.display import DisplayMeta, DisplayResult

router = APIRouter(prefix="/api/display", tags=["display"], dependencies=[Security(api_key_scheme)])

_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_TITLE_LENGTH = 120


def _clean_filename(name: str | None) -> str | None:
    """Base name of an uploaded file, stripped of control characters and truncated."""
    if not name:
        return None
    base = PurePath(name.replace("\\", "/")).name
    cleaned = _UNSAFE_CHARS.sub("", base).strip()
    return cleaned[:_MAX_TITLE_LENGTH] or None


_UPLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {"description": "File larger than MAX_UPLOAD_MB"},
    415: {"description": "Not a supported image (JPEG, PNG, WebP, GIF, TIFF, BMP, HEIC)"},
}


@router.post(
    "/upload",
    response_model=DisplayResult,
    responses=_UPLOAD_RESPONSES,
    summary="Display an uploaded image",
)
async def display_upload(
    settings: SettingsDep,
    display: DisplayServiceDep,
    scheduler: SchedulerDep,
    file: Annotated[UploadFile, File(description="Image file (max MAX_UPLOAD_MB)")],
    fit: Annotated[FitMode | None, Form(description="Override the default fit mode")] = None,
    push_to_fraimic: Annotated[
        bool,
        Form(
            description="Send the result to the frame (or queue it in deepsleep mode); "
            "false only renders a preview (/api/preview.jpg)"
        ),
    ] = True,
    ephemeral: Annotated[
        bool,
        Form(
            description="Temporary push: the previous image comes back after "
            "DELETE /api/queue/ephemeral (uploads are never replayed)"
        ),
    ] = False,
    hold_minutes: Annotated[
        int | None,
        Form(
            ge=1, le=10080, description="Keep it up at least this long before a scheduled refresh"
        ),
    ] = None,
) -> DisplayResult:
    """Render an uploaded image for the frame and make it the current image.

    In ``continuous`` mode the response includes the delivery outcome; in ``deepsleep``
    mode the image is queued until the frame wakes up. A pushed upload restarts the
    refresh timer but keeps the rule replayed by ``POST /api/refresh``.
    """
    try:
        data = await read_upload(file, settings.max_upload_bytes)
    except PayloadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    finally:
        await file.close()

    title = _clean_filename(file.filename)
    try:
        result = await display.display_bytes(
            data,
            source="upload",
            title=title,
            details={"filename": title, "bytes": len(data)},
            fit=fit,
            push=push_to_fraimic,
            ephemeral=ephemeral,
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    if push_to_fraimic:
        await scheduler.record_manual(None, ephemeral=ephemeral, hold_minutes=hold_minutes)
    return result


# --- Title card ---------------------------------------------------------------------------
TITLE_CARD_SOURCE: Final = "title_card"
#: Accent colors of the rule under the title (exact panel colors).
ACCENTS: Final[dict[str, tuple[int, int, int]]] = {
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
    "blue": (0, 0, 255),
    "green": (0, 255, 0),
    "white": (255, 255, 255),
}


class TitleCardRequest(BaseModel):
    """Body of ``POST /api/display/title-card``."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200, description="Large centered text")
    subtitle: str | None = Field(default=None, max_length=200)
    caption: str | None = Field(default=None, max_length=200, description="Small text below")
    accent: Literal["red", "yellow", "blue", "green", "white"] = Field(
        default="red", description="Color of the rule under the title"
    )
    push_to_fraimic: bool = Field(
        default=True, description="false only renders a preview (/api/preview.jpg)"
    )
    ephemeral: bool = Field(
        default=True, description="Temporary: the previous image comes back afterwards"
    )
    ttl_minutes: int | None = Field(
        default=60,
        ge=1,
        le=10080,
        description="Ephemeral only: minutes before the previous image returns "
        "(null = until cancelled or replaced)",
    )
    hold_minutes: int | None = Field(
        default=None, ge=1, le=10080, description="Protect it from scheduled refreshes"
    )

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        cleaned = _UNSAFE_CHARS.sub(" ", value).strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("subtitle", "caption")
    @classmethod
    def _clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _UNSAFE_CHARS.sub(" ", value).strip() or None


@router.post(
    "/title-card",
    response_model=DisplayResult,
    summary="Display a typographic card (title, subtitle, caption)",
)
async def display_title_card(
    body: TitleCardRequest, display: DisplayServiceDep, scheduler: SchedulerDep
) -> DisplayResult:
    """Render large centered text on black with an accent rule, e.g. a welcome message.

    Ephemeral by default: after ``ttl_minutes`` (or ``DELETE /api/queue/ephemeral``) the
    previous image comes back.
    """
    layout = TitleCardLayout(
        title=body.title,
        subtitle=body.subtitle,
        caption=body.caption,
        accent=ACCENTS[body.accent],
    )
    expires_at = (
        scheduler.now() + timedelta(minutes=body.ttl_minutes)
        if body.ephemeral and body.ttl_minutes is not None
        else None
    )
    details: dict[str, Any] = {"subtitle": body.subtitle, "caption": body.caption}
    meta = DisplayMeta(
        TITLE_CARD_SOURCE,
        body.title,
        {key: value for key, value in details.items() if value},
        push=body.push_to_fraimic,
        ephemeral=body.ephemeral,
        expires_at=expires_at,
    )
    result = await display.display_layout(None, layout, meta)
    if body.push_to_fraimic:
        await scheduler.record_manual(
            None, ephemeral=body.ephemeral, hold_minutes=body.hold_minutes
        )
    return result
