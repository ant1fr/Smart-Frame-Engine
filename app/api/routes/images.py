"""Image files (Home Assistant Generic Camera, UI preview, Fraimic pull).

* ``/api/current.*`` - what the frame shows (or will show once a sleeping frame wakes up).
* ``/api/preview.*`` - the most recent render, including ``push_to_fraimic=false`` renders
  used to inspect a template without touching the frame.

Readable without an API key unless ``PUBLIC_IMAGE_ENDPOINTS=false`` (enforced by the
auth middleware). Responses carry an ``ETag`` derived from the ``.bin`` hash and
``Cache-Control: no-cache`` so clients revalidate cheaply and never show stale images.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.api.deps import CurrentImageStoreDep

router = APIRouter(prefix="/api", tags=["images"])


def _responses(media_type: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"content": {media_type: {}}},
        404: {"description": "No image has been rendered yet"},
    }


def _serve(
    request: Request,
    path: Path,
    etag_base: str | None,
    suffix: str,
    media_type: str,
    filename: str | None = None,
) -> Response:
    if etag_base is None or not path.is_file():
        raise HTTPException(status_code=404, detail="No image has been rendered yet")
    etag = f'"{etag_base[:32]}-{suffix}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(
        path,
        media_type=media_type,
        headers=headers,
        filename=filename,
        content_disposition_type="inline" if filename is None else "attachment",
    )


@router.head("/current.jpg", include_in_schema=False)
@router.get(
    "/current.jpg",
    responses=_responses("image/jpeg"),
    summary="Current image (enhanced, before dithering)",
)
async def current_jpeg(request: Request, store: CurrentImageStoreDep) -> Response:
    """The current composition as JPEG, in viewing orientation."""
    meta = store.meta
    return _serve(request, store.jpeg_path, meta.sha256 if meta else None, "jpg", "image/jpeg")


@router.head("/current_dithered.png", include_in_schema=False)
@router.get(
    "/current_dithered.png",
    responses=_responses("image/png"),
    summary="Current image as rendered by the panel (6-color simulation)",
)
async def current_dithered(request: Request, store: CurrentImageStoreDep) -> Response:
    """Exact 6-color dithered preview, in viewing orientation."""
    meta = store.meta
    return _serve(request, store.dithered_path, meta.sha256 if meta else None, "png", "image/png")


@router.head("/current.bin", include_in_schema=False)
@router.get(
    "/current.bin",
    responses=_responses("application/octet-stream"),
    summary="Current Fraimic frame buffer (.bin)",
)
async def current_bin(request: Request, store: CurrentImageStoreDep) -> Response:
    """The raw ``.bin`` sent to the frame (960,000 or 2,304,000 bytes)."""
    meta = store.meta
    filename = f"current_{meta.panel}.bin" if meta else None
    return _serve(
        request,
        store.bin_path,
        meta.sha256 if meta else None,
        "bin",
        "application/octet-stream",
        filename=filename,
    )


@router.head("/preview.jpg", include_in_schema=False)
@router.get(
    "/preview.jpg",
    responses=_responses("image/jpeg"),
    summary="Most recent render (enhanced, before dithering), pushed or not",
)
async def preview_jpeg(request: Request, store: CurrentImageStoreDep) -> Response:
    """The latest composition as JPEG, including ``push_to_fraimic=false`` renders."""
    meta = store.preview_meta
    path = store.paths("preview").jpeg
    return _serve(request, path, meta.sha256 if meta else None, "pjpg", "image/jpeg")


@router.head("/preview_dithered.png", include_in_schema=False)
@router.get(
    "/preview_dithered.png",
    responses=_responses("image/png"),
    summary="Most recent render as the panel would show it (6-color simulation)",
)
async def preview_dithered(request: Request, store: CurrentImageStoreDep) -> Response:
    """Exact 6-color dithered preview of the latest render, in viewing orientation."""
    meta = store.preview_meta
    path = store.paths("preview").dithered
    return _serve(request, path, meta.sha256 if meta else None, "ppng", "image/png")
