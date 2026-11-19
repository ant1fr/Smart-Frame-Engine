"""Ambient context API: ``POST``/``GET``/``DELETE /api/context``.

Home Assistant pushes short-lived facts (who is home, the weather, guests over...) and the
engine adapts the Smart Mix and Family Mode. Every field expires by itself.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel, Field

from app.api.deps import ConfigStoreDep, ImmichProviderDep, SchedulerDep
from app.core.auth import api_key_scheme
from app.services.ambient import AmbientRequest, AmbientState

router = APIRouter(prefix="/api/context", tags=["context"], dependencies=[Security(api_key_scheme)])

AmbientField = Literal["presence", "family_mode", "weather", "season", "boost_tags", "boost_people"]


class AmbientView(BaseModel):
    """The live ambient context and what it currently does."""

    context: AmbientState
    family_active: bool = Field(description="Whether Family Mode applies right now")
    smart_mix_enabled: bool = Field(
        description="Presence, boosts and weather only act while the Smart Mix is enabled"
    )
    extra_candidates: list[str] = Field(description="Labels of the candidates added to the mix")


def _view(
    scheduler: SchedulerDep, config: ConfigStoreDep, provider: ImmichProviderDep
) -> AmbientView:
    return AmbientView(
        context=scheduler.live_ambient(),
        family_active=provider.family_active(),
        smart_mix_enabled=config.config.smart_mix.enabled,
        extra_candidates=[c.label or c.rule.kind for c in scheduler.ambient_extras()],
    )


@router.post(
    "",
    response_model=AmbientView,
    responses={422: {"description": "Unknown weather keyword or season"}},
    summary="Set ambient context fields (each expires on its own)",
)
async def set_context(
    body: AmbientRequest,
    scheduler: SchedulerDep,
    config: ConfigStoreDep,
    provider: ImmichProviderDep,
) -> AmbientView:
    """Merge the given fields into the live context.

    Unset fields are left alone; an empty list or text clears the field. Each field takes
    either the bare value or ``{"values"|"value": ..., "ttl_minutes": n}``.
    """
    live = scheduler.live_ambient()
    try:
        updated = body.apply(live, config.config.ambient, scheduler.now())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await scheduler.set_ambient(updated)
    return _view(scheduler, config, provider)


@router.get("", response_model=AmbientView, summary="The live ambient context")
async def get_context(
    scheduler: SchedulerDep, config: ConfigStoreDep, provider: ImmichProviderDep
) -> AmbientView:
    """Fields that have not expired, with their expiry, and their effect."""
    return _view(scheduler, config, provider)


@router.delete("", response_model=AmbientView, summary="Clear the ambient context")
async def clear_context(
    scheduler: SchedulerDep,
    config: ConfigStoreDep,
    provider: ImmichProviderDep,
    field: Annotated[AmbientField | None, Query(description="Clear only this field")] = None,
) -> AmbientView:
    """Drop every field, or only ``field``."""
    if field is None:
        await scheduler.set_ambient(AmbientState())
    else:
        await scheduler.set_ambient(scheduler.live_ambient().model_copy(update={field: None}))
    return _view(scheduler, config, provider)
