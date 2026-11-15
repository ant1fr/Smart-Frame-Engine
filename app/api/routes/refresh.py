"""``POST /api/refresh``: replay the active source rule now."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Security
from pydantic import BaseModel, Field

from app.api.deps import SchedulerDep
from app.api.errors import PROVIDER_ERROR_RESPONSES, provider_http_error
from app.core.auth import api_key_scheme
from app.core.security import InvalidImageError
from app.providers.base import ProviderError
from app.services.display import DisplayResult
from app.services.scheduler import InvalidRuleError, RefreshError, RuleSource

router = APIRouter(prefix="/api", tags=["display"], dependencies=[Security(api_key_scheme)])


class RefreshResult(BaseModel):
    """Outcome of a manual refresh."""

    reason: Literal["manual"] = "manual"
    rule_kind: str = Field(description="Source of the replayed rule (immich, tmdb)")
    rule_source: RuleSource = Field(
        description="pinned = scheduler.source_rule; smart_mix = the weighted playlist; "
        "last = the last manual display request"
    )
    entry_label: str | None = Field(
        default=None, description="Smart Mix entry that was shown (smart_mix only)"
    )
    display: DisplayResult


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **PROVIDER_ERROR_RESPONSES,
    409: {
        "description": "Nothing to replay yet, the rule's source cannot be replayed, the "
        "source is not configured, or a refresh is already running"
    },
    422: {"description": "The rule's parameters are invalid, or name unknown people/albums"},
    502: {"description": "The source failed or returned an image that cannot be decoded"},
}


@router.post(
    "/refresh",
    response_model=RefreshResult,
    responses=_RESPONSES,
    summary="Re-run the active source rule",
)
async def refresh(scheduler: SchedulerDep) -> RefreshResult:
    """Show a new image chosen by the active rule and push it to the frame.

    The active rule is the pinned ``scheduler.source_rule`` when set, otherwise the last
    request made through ``POST /api/display/immich``. Unlike scheduled refreshes, a
    manual refresh ignores quiet hours and a pending upload. It restarts the timer.
    """
    try:
        outcome = await scheduler.run("manual")
    except InvalidRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RefreshError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    except InvalidImageError as exc:
        raise HTTPException(
            status_code=502, detail="The source returned an image that cannot be decoded"
        ) from exc
    return RefreshResult(
        rule_kind=outcome.rule.kind,
        rule_source=outcome.active.source,
        entry_label=outcome.entry_label,
        display=outcome.display,
    )
