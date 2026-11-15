"""Smart Mix and Smart Search endpoints (built for Home Assistant automations).

* ``POST /api/display/smart-search``: an immediate, contextual push ("beach vacation",
  "rainy sunday", a person, "on this day"...) without knowing any Immich ID. It is a direct
  push: it resets the refresh timer, is ephemeral by default and never changes the playlist.
* ``/api/smart-mix``: inspect the playlist and set a temporary context boost that shapes the
  next scheduled refreshes (and expires by itself).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Security
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.api.deps import (
    ConfigStoreDep,
    DisplayServiceDep,
    ImmichProviderDep,
    SchedulerDep,
)
from app.api.errors import PROVIDER_ERROR_RESPONSES
from app.api.routes.immich import show_immich
from app.core.auth import api_key_scheme
from app.core.config_models import FitMode, SourceKind, SourceRule
from app.providers.immich.models import ImmichDisplayRequest, MatchMode, OnThisDay
from app.providers.tmdb.models import TmdbDisplayRequest
from app.services.display import DisplayResult
from app.services.scheduler import ContextView, validation_summary
from app.services.smart_mix import DEFAULT_CONTEXT_WEIGHT, MixContext

display_router = APIRouter(
    prefix="/api/display", tags=["smart-mix"], dependencies=[Security(api_key_scheme)]
)
router = APIRouter(
    prefix="/api/smart-mix", tags=["smart-mix"], dependencies=[Security(api_key_scheme)]
)

_MAX_MINUTES = 10080  # one week


# --- Smart search ----------------------------------------------------------------------
class SmartSearchRequest(BaseModel):
    """A contextual search over the photo library (people are plain names)."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, description='One semantic phrase, e.g. "rainy sunday"')
    queries: list[str] = Field(
        default_factory=list, max_length=10, description="More phrases (a photo matching any)"
    )
    persons: list[str] | None = Field(default=None, description="Person names (optional)")
    match_mode: MatchMode | None = None
    accounts: list[str] | None = None
    on_this_day: bool | OnThisDay | None = Field(
        default=None,
        description="Memories around today's date in past years (true = defaults)",
    )
    taken_after: AwareDatetime | None = None
    taken_before: AwareDatetime | None = None
    fit: FitMode | None = None
    push_to_fraimic: bool = Field(
        default=True, description="Send to the frame; false only renders a preview"
    )
    ephemeral: bool = Field(
        default=True,
        description="Temporary push: not stored as the rule replayed by POST /api/refresh",
    )
    hold_minutes: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_MINUTES,
        description="Protect the image from scheduled refreshes",
    )
    ignore_history: bool = False
    skip_quality_gate: bool | None = None

    def phrases(self) -> list[str]:
        """All non-empty search phrases."""
        return [p.strip() for p in [self.query or "", *self.queries] if p.strip()]


@display_router.post(
    "/smart-search",
    response_model=DisplayResult,
    responses=PROVIDER_ERROR_RESPONSES,
    summary="Display a photo found by a contextual search",
)
async def display_smart_search(
    body: SmartSearchRequest,
    provider: ImmichProviderDep,
    display: DisplayServiceDep,
    scheduler: SchedulerDep,
) -> DisplayResult:
    """Search the library by meaning, time or people and display the best fresh match.

    Phrases may use ``{season}``, ``{month_name}``, ``{month}`` and ``{year}`` placeholders.
    Without a phrase, ``on_this_day`` or a date range is required.
    """
    phrases = body.phrases()
    if not phrases and not body.on_this_day and not (body.taken_after or body.taken_before):
        raise HTTPException(
            status_code=422,
            detail="Give a query, or on_this_day / a taken_after-taken_before range",
        )
    fields: dict[str, Any] = body.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"query", "queries", "on_this_day"},
    )
    fields["mode"] = "search" if phrases else "random"
    if phrases:
        fields["clip_queries"] = phrases
    if body.on_this_day:
        fields["on_this_day"] = (
            body.on_this_day.model_dump() if isinstance(body.on_this_day, OnThisDay) else {}
        )
    try:
        request = ImmichDisplayRequest.model_validate(scheduler.expand(fields))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=validation_summary(exc)) from exc
    return await show_immich(request, provider, display, scheduler)


# --- Context boost ---------------------------------------------------------------------
class ContextRequest(BaseModel):
    """A temporary boost of the Smart Mix, e.g. "it is raining: favour cosy photos"."""

    model_config = ConfigDict(extra="forbid")

    kind: SourceKind = Field(default="immich", description="Source of the boosted rule")
    params: dict[str, JsonValue] = Field(
        default_factory=dict, description="Raw rule parameters (the display endpoint body)"
    )
    queries: list[str] = Field(
        default_factory=list, max_length=10, description="Immich shorthand: search phrases"
    )
    persons: list[str] | None = Field(default=None, description="Immich shorthand: names")
    on_this_day: bool = Field(default=False, description="Immich shorthand: memories")
    weight: float = Field(default=DEFAULT_CONTEXT_WEIGHT, gt=0, le=1000)
    ttl_minutes: int = Field(default=60, ge=1, le=_MAX_MINUTES, description="Boost lifetime")
    exclusive: bool = Field(
        default=False, description="Only the boost is used until it expires (an override)"
    )
    label: str | None = Field(default=None, max_length=80)


class ContextResponse(ContextView):
    """The boost that was set."""

    rule: SourceRule
    effective: bool = Field(
        description="false while a pinned scheduler.source_rule takes precedence over the mix"
    )


#: Request models validating the params of a boosted rule, per source kind.
_RULE_MODELS: dict[str, type[BaseModel]] = {
    "immich": ImmichDisplayRequest,
    "tmdb": TmdbDisplayRequest,
}


def _rule_from(body: ContextRequest) -> SourceRule:
    params: dict[str, JsonValue] = dict(body.params)
    shorthand = bool(body.queries or body.persons or body.on_this_day)
    if shorthand and body.kind != "immich":
        raise HTTPException(
            status_code=422, detail="queries/persons/on_this_day only apply to kind 'immich'"
        )
    if body.queries:
        params["mode"] = "search"
        params["clip_queries"] = list(body.queries)
    if body.persons is not None:
        params["persons"] = list(body.persons)
    if body.on_this_day:
        params["on_this_day"] = {}
    if not params:
        raise HTTPException(status_code=422, detail="Give params or queries for the boost")
    model = _RULE_MODELS.get(body.kind)
    if model is not None:
        try:
            model.model_validate(params)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=validation_summary(exc)) from exc
    return SourceRule(kind=body.kind, params=params)


@router.post(
    "/context",
    response_model=ContextResponse,
    responses={
        409: {"description": "Smart Mix is disabled (smart_mix.enabled)"},
        422: {"description": "Unknown source kind or invalid parameters"},
    },
    summary="Boost or override the Smart Mix temporarily",
)
async def set_context(
    body: ContextRequest, scheduler: SchedulerDep, config: ConfigStoreDep
) -> ContextResponse:
    """Add a rule to the playlist (or use it alone) until ``ttl_minutes`` pass.

    Scheduled refreshes pick it according to ``weight``; nothing is displayed right now (use
    ``POST /api/display/smart-search`` for that). A new boost replaces the previous one.
    """
    if not config.config.smart_mix.enabled:
        raise HTTPException(status_code=409, detail="Smart Mix is disabled (smart_mix.enabled)")
    if body.kind not in scheduler.supported_kinds:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported kind {body.kind!r}; supported: {scheduler.supported_kinds}",
        )
    rule = _rule_from(body)
    context = MixContext(
        rule=rule,
        weight=body.weight,
        expires_at=scheduler.now() + timedelta(minutes=body.ttl_minutes),
        exclusive=body.exclusive,
        label=body.label,
    )
    await scheduler.set_context(context)
    active = scheduler.active_rule()
    return ContextResponse(
        label=context.label,
        kind=rule.kind,
        weight=context.weight,
        exclusive=context.exclusive,
        expires_at=context.expires_at,
        rule=rule,
        effective=active is not None and active.source == "smart_mix",
    )


class ClearedResponse(BaseModel):
    cleared: bool = Field(description="Whether a boost was active")


@router.delete("/context", response_model=ClearedResponse, summary="Remove the Smart Mix boost")
async def clear_context(scheduler: SchedulerDep) -> ClearedResponse:
    """Drop the temporary boost immediately."""
    had = scheduler.live_context() is not None
    await scheduler.set_context(None)
    return ClearedResponse(cleared=had)


# --- Inspection ------------------------------------------------------------------------
class MixEntryView(BaseModel):
    index: int
    label: str | None
    kind: str
    weight: float
    enabled: bool
    probability: float = Field(description="Chance of being tried first at the next refresh")
    params: dict[str, JsonValue]
    resolved_params: dict[str, JsonValue] = Field(
        description="params with {season}/{month_name}... expanded for now"
    )


class SmartMixView(BaseModel):
    enabled: bool
    effective: bool = Field(description="Whether scheduled refreshes currently use the mix")
    supported_kinds: list[str] = Field(description="Source kinds that can run (runner registry)")
    max_attempts: int
    avoid_consecutive: bool
    hemisphere: str
    entries: list[MixEntryView]
    context: ContextView | None
    last_entry_index: int | None
    last_entry_label: str | None


@router.get("", response_model=SmartMixView, summary="Show the Smart Mix playlist")
async def get_smart_mix(scheduler: SchedulerDep, config: ConfigStoreDep) -> SmartMixView:
    """The playlist with each entry's effective probability and the live boost."""
    mix = config.config.smart_mix
    pool = scheduler.mix_pool() if mix.enabled else []
    total = sum(candidate.weight for candidate in pool)
    chance = {c.index: c.weight / total for c in pool if c.index is not None and total > 0}
    entries = [
        MixEntryView(
            index=index,
            label=entry.label,
            kind=entry.kind,
            weight=entry.weight,
            enabled=entry.enabled,
            probability=round(chance.get(index, 0.0), 4),
            params=entry.params,
            resolved_params=scheduler.expand(entry.params),
        )
        for index, entry in enumerate(mix.entries)
    ]
    active = scheduler.active_rule()
    last = scheduler.state.last_run
    return SmartMixView(
        enabled=mix.enabled,
        effective=active is not None and active.source == "smart_mix",
        supported_kinds=scheduler.supported_kinds,
        max_attempts=mix.max_attempts,
        avoid_consecutive=mix.avoid_consecutive,
        hemisphere=mix.hemisphere,
        entries=entries,
        context=scheduler.context_view(),
        last_entry_index=scheduler.state.last_mix_index,
        last_entry_label=last.entry_label if last else None,
    )
