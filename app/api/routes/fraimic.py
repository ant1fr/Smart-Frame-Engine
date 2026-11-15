"""Frame control endpoints: force push, wake-up webhook and telemetry."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import DeliveryServiceDep, DisplayServiceDep
from app.clients.fraimic import (
    FraimicError,
    FraimicNotConfiguredError,
    FraimicUnreachableError,
)
from app.core.auth import api_key_scheme
from app.services.delivery import DeliveryOutcome, FrameTelemetry
from app.services.display import NoCurrentImageError

router = APIRouter(prefix="/api/fraimic", tags=["fraimic"])
protected = [Security(api_key_scheme)]


class WakeupAccepted(BaseModel):
    """Response of a non-blocking wake-up."""

    accepted: bool = True
    pending: bool = Field(description="Whether an image was waiting for the frame")


def _outcome_response(outcome: DeliveryOutcome) -> JSONResponse:
    status_code = 200 if outcome.status in {"delivered", "nothing_pending", "dropped"} else 202
    return JSONResponse(outcome.model_dump(mode="json"), status_code=status_code)


_OUTCOME_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": DeliveryOutcome, "description": "Delivered (or nothing was pending)"},
    202: {"model": DeliveryOutcome, "description": "Frame unreachable; the image stays queued"},
}


@router.post(
    "/push",
    dependencies=protected,
    responses={**_OUTCOME_RESPONSES, 409: {"description": "No current image to push"}},
    summary="Force push the current image to the frame",
)
async def force_push(display: DisplayServiceDep) -> JSONResponse:
    """Send ``current.bin`` now, whatever the power mode.

    Returns ``200`` when delivered and ``202`` when the frame could not be reached; the
    image then stays queued and is retried automatically.
    """
    try:
        outcome = await display.push_current()
    except NoCurrentImageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _outcome_response(outcome)


@router.post(
    "/wakeup",
    dependencies=protected,
    responses={
        **_OUTCOME_RESPONSES,
        202: {
            "model": WakeupAccepted | DeliveryOutcome,
            "description": "Accepted (wait=false), or frame unreachable (wait=true)",
        },
    },
    summary="Signal that the frame is awake (Home Assistant webhook)",
)
async def wakeup(
    delivery: DeliveryServiceDep,
    wait: Annotated[
        bool, Query(description="Wait for the upload to finish and return its outcome")
    ] = False,
) -> JSONResponse:
    """Flush the queued image now that the frame is reachable.

    By default the upload runs in the background and the call returns ``202``
    immediately (Home Assistant ``rest_command`` timeouts are short). With
    ``?wait=true`` the delivery outcome is returned.
    """
    pending = delivery.pending is not None
    outcome = await delivery.wakeup(wait=wait)
    if outcome is None:
        return JSONResponse(WakeupAccepted(pending=pending).model_dump(), status_code=202)
    return _outcome_response(outcome)


@router.get(
    "/info",
    response_model=FrameTelemetry,
    responses={
        409: {"description": "No frame host configured"},
        502: {"description": "The frame answered with an error"},
        504: {"description": "The frame is unreachable (asleep or offline)"},
    },
    summary="Frame telemetry (battery, Wi-Fi, firmware)",
)
async def frame_info(
    delivery: DeliveryServiceDep,
    refresh: Annotated[
        bool, Query(description="Query the frame now instead of returning cached values")
    ] = False,
) -> FrameTelemetry:
    """Return the last known telemetry, or query the frame with ``?refresh=true``."""
    if refresh:
        try:
            await delivery.refresh_info()
        except FraimicNotConfiguredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FraimicUnreachableError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except FraimicError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return delivery.telemetry
