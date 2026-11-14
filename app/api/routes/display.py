"""Display endpoints (manual sources). Provider-based sources arrive in later milestones."""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Security, UploadFile

from app.api.deps import DisplayServiceDep, SettingsDep
from app.core.auth import api_key_scheme
from app.core.config_models import FitMode
from app.core.security import InvalidImageError, PayloadTooLargeError, read_upload
from app.services.display import DisplayResult

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
    415: {"description": "Not a supported image (JPEG, PNG, WebP, GIF, TIFF, BMP)"},
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
    file: Annotated[UploadFile, File(description="Image file (max MAX_UPLOAD_MB)")],
    fit: Annotated[FitMode | None, Form(description="Override the default fit mode")] = None,
    push_to_fraimic: Annotated[
        bool, Form(description="Send the result to the frame (or queue it in deepsleep mode)")
    ] = True,
) -> DisplayResult:
    """Render an uploaded image for the frame and make it the current image.

    In ``continuous`` mode the response includes the delivery outcome; in ``deepsleep``
    mode the image is queued until the frame wakes up.
    """
    try:
        data = await read_upload(file, settings.max_upload_bytes)
    except PayloadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    finally:
        await file.close()

    title = _clean_filename(file.filename)
    try:
        return await display.display_bytes(
            data,
            source="upload",
            title=title,
            details={"filename": title, "bytes": len(data)},
            fit=fit,
            push=push_to_fraimic,
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
