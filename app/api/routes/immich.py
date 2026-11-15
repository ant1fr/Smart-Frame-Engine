"""Immich endpoints: display a photo, list albums, inspect and refresh the people mapping."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel, Field

from app.api.deps import (
    ConfigStoreDep,
    DisplayServiceDep,
    ImmichProviderDep,
    PeopleDirectoryDep,
    SchedulerDep,
)
from app.api.errors import PROVIDER_ERROR_RESPONSES, provider_http_error
from app.core.auth import api_key_scheme
from app.core.security import InvalidImageError
from app.providers.base import InvalidSelectionError, ProviderError
from app.providers.immich.models import AlbumInfo, ImmichDisplayRequest
from app.providers.immich.people import PeopleSnapshot, resolve_person
from app.providers.immich.provider import ImmichProvider
from app.services.display import DisplayResult, DisplayService
from app.services.scheduler import SchedulerService, immich_rule

display_router = APIRouter(
    prefix="/api/display", tags=["display"], dependencies=[Security(api_key_scheme)]
)
router = APIRouter(prefix="/api/immich", tags=["immich"], dependencies=[Security(api_key_scheme)])

AccountsQuery = Annotated[
    list[str] | None, Query(description="Only these account names (default: all)")
]

_ERRORS: dict[int | str, dict[str, Any]] = {
    **PROVIDER_ERROR_RESPONSES,
    409: {"description": "No Immich account is configured"},
}


@display_router.post(
    "/immich",
    response_model=DisplayResult,
    responses=_ERRORS,
    summary="Select and display an Immich photo",
)
async def display_immich(
    request: ImmichDisplayRequest,
    provider: ImmichProviderDep,
    display: DisplayServiceDep,
    scheduler: SchedulerDep,
) -> DisplayResult:
    """Pick a photo from Immich, render it (face-aware crop) and make it the current image.

    People are plain names resolved per account; ``persons``, ``match_mode``, ``album_*``
    and ``always_include`` fall back to the ``immich`` section of the configuration.
    The photo is recorded in the history so it is not shown again within
    ``immich.anti_repeat_days``. When pushed to the frame, the request becomes the rule
    replayed by ``POST /api/refresh`` and the scheduler, and the refresh timer restarts.
    With ``ephemeral`` the request is not stored as that rule; ``hold_minutes`` keeps the
    image up for at least that long. A direct push never changes the Smart Mix playlist.
    """
    return await show_immich(request, provider, display, scheduler)


async def show_immich(
    request: ImmichDisplayRequest,
    provider: ImmichProvider,
    display: DisplayService,
    scheduler: SchedulerService,
) -> DisplayResult:
    """Fetch, render and (when pushed) record one Immich display request."""
    try:
        asset = await provider.fetch(request)
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    try:
        result = await display.display_asset(asset, fit=request.fit, push=request.push_to_fraimic)
    except InvalidImageError as exc:
        raise HTTPException(
            status_code=502, detail="Immich returned an image that cannot be decoded"
        ) from exc
    if request.push_to_fraimic:
        await scheduler.record_manual(
            immich_rule(request), ephemeral=request.ephemeral, hold_minutes=request.hold_minutes
        )
    return result


class AlbumsResponse(BaseModel):
    """Albums across the configured accounts."""

    albums: list[AlbumInfo]
    errors: dict[str, str] = Field(
        default_factory=dict, description="Accounts that could not be queried, with the reason"
    )


@router.get("/albums", response_model=AlbumsResponse, responses=_ERRORS, summary="List albums")
async def list_albums(
    provider: ImmichProviderDep, accounts: AccountsQuery = None
) -> AlbumsResponse:
    """Albums (owned and shared) of every selected account."""
    try:
        albums, errors = await provider.albums(accounts)
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    return AlbumsResponse(albums=albums, errors=errors)


class PersonView(BaseModel):
    id: str
    name: str


class AccountPeopleView(BaseModel):
    """People of one account as the provider sees them."""

    account: str
    people: list[PersonView] = Field(default_factory=list)
    cached_at: datetime | None = Field(default=None, description="When the list was fetched")
    ambiguous: list[str] = Field(
        default_factory=list,
        description="Names shared by several people; pin one in immich.person_aliases",
    )
    aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Configured canonical names resolved to person IDs in this account",
    )
    error: str | None = None


class PeopleResponse(BaseModel):
    accounts: list[AccountPeopleView]


def _view(snapshot: PeopleSnapshot, aliases: dict[str, dict[str, list[str]]]) -> AccountPeopleView:
    view = AccountPeopleView(account=snapshot.account, error=snapshot.error)
    if snapshot.people is None:
        return view
    people = snapshot.people
    view.cached_at = people.fetched_at
    view.ambiguous = snapshot.ambiguous
    view.people = [
        PersonView(id=person_id, name=name)
        for person_id, name in sorted(people.by_id.items(), key=lambda item: item[1].casefold())
    ]
    for canonical, per_account in aliases.items():
        if snapshot.account not in per_account:
            continue
        try:
            view.aliases[canonical] = resolve_person(canonical, snapshot.account, people, aliases)
        except InvalidSelectionError:
            view.aliases[canonical] = []
    return view


async def _people_response(
    directory: PeopleDirectoryDep,
    config: ConfigStoreDep,
    accounts: list[str] | None,
    *,
    refresh: bool,
) -> PeopleResponse:
    try:
        names = list(directory.account_names(accounts))
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    snapshots = await directory.snapshot(names, refresh=refresh)
    aliases = config.config.immich.person_aliases
    return PeopleResponse(accounts=[_view(snapshot, aliases) for snapshot in snapshots])


@router.get(
    "/people",
    response_model=PeopleResponse,
    responses=_ERRORS,
    summary="People known to each account (cached)",
)
async def list_people(
    directory: PeopleDirectoryDep, config: ConfigStoreDep, accounts: AccountsQuery = None
) -> PeopleResponse:
    """Named people per account (name to ID), served from the cache while it is fresh."""
    return await _people_response(directory, config, accounts, refresh=False)


@router.post(
    "/people/refresh",
    response_model=PeopleResponse,
    responses=_ERRORS,
    summary="Reload the people lists from Immich",
)
async def refresh_people(
    directory: PeopleDirectoryDep, config: ConfigStoreDep, accounts: AccountsQuery = None
) -> PeopleResponse:
    """Bypass the cache and fetch the people lists again."""
    return await _people_response(directory, config, accounts, refresh=True)
