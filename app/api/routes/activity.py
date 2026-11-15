"""Recent activity (renders and log lines) for the web UI dashboard."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Security

from app.api.deps import ActivityLogDep
from app.core.auth import api_key_scheme
from app.services.activity import MAX_LOG_LINES, ActivitySnapshot

router = APIRouter(prefix="/api", tags=["system"], dependencies=[Security(api_key_scheme)])


@router.get(
    "/activity",
    response_model=ActivitySnapshot,
    summary="Recent renders and log lines (newest first)",
)
async def activity(
    log: ActivityLogDep,
    logs: Annotated[
        int, Query(ge=0, le=MAX_LOG_LINES, description="Maximum number of log lines")
    ] = MAX_LOG_LINES,
) -> ActivitySnapshot:
    """The last displayed images (with their render decision) and the last log lines.

    Kept in memory only (lost on restart). Log lines have secrets redacted. Protected by the
    API key (or a web UI session) whenever authentication is enabled.
    """
    return log.snapshot(logs=logs)
