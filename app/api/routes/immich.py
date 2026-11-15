"""Immich endpoints: display a photo, list albums, inspect and refresh the people mapping."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr, ValidationError

from app.api.deps import (
    ConfigStoreDep,
    DisplayServiceDep,
    ImmichGatewayDep,
    ImmichProviderDep,
    PeopleDirectoryDep,
    SchedulerDep,
    URLPolicyDep,
)
from app.api.errors import PROVIDER_ERROR_RESPONSES, provider_http_error
from app.core.auth import api_key_scheme
from app.core.config_models import ImmichAccountConfig, is_masked
from app.core.security import InvalidImageError, UnsafeURLError, validate_external_url
from app.providers.base import InvalidSelectionError, ProviderError
from app.providers.immich.client import ImmichClient
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
        result = await display.display_asset(
            asset, fit=request.fit, push=request.push_to_fraimic, ephemeral=request.ephemeral
        )
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


# --- UI helpers: thumbnails and connection test ----------------------------------------------
_ITEM_ID: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
THUMBNAIL_MAX_BYTES: Final = 2 * 1024 * 1024


def _image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


@router.get(
    "/thumbnail/{account}/{kind}/{item_id}",
    responses={**_ERRORS, 200: {"content": {"image/jpeg": {}, "image/webp": {}}}},
    summary="Small thumbnail of an asset (album cover) or a person (face)",
)
async def thumbnail(
    account: str,
    kind: Literal["asset", "person"],
    item_id: str,
    gateway: ImmichGatewayDep,
) -> Response:
    """Proxy an Immich thumbnail with the account key, so the browser never sees Immich keys."""
    if not _ITEM_ID.match(item_id):
        raise HTTPException(status_code=422, detail="Invalid id")
    try:
        async with gateway.client(account) as client:
            if kind == "asset":
                data = await client.download(
                    item_id, variant="thumbnail", max_bytes=THUMBNAIL_MAX_BYTES
                )
            else:
                data = await client.person_thumbnail(item_id, max_bytes=THUMBNAIL_MAX_BYTES)
    except ProviderError as exc:
        status = getattr(exc, "status_code", None)
        if status == 404:
            raise HTTPException(status_code=404, detail="No thumbnail") from exc
        raise provider_http_error(exc) from exc
    return Response(
        data, media_type=_image_type(data), headers={"Cache-Control": "private, max-age=86400"}
    )


class ConnectionTestRequest(BaseModel):
    """Test a configured account, or unsaved settings typed in the UI."""

    account: str | None = Field(default=None, description="A configured account name")
    url: str | None = Field(default=None, description="Unsaved Immich URL")
    api_key: str | None = Field(
        default=None, description="Unsaved API key (a masked value means the stored one)"
    )


class ConnectionTestResult(BaseModel):
    """Outcome of an Immich connection test."""

    ok: bool
    version: str | None = None
    people: int | None = None
    albums: int | None = None
    error: str | None = None


@router.post(
    "/test",
    response_model=ConnectionTestResult,
    summary="Test an Immich connection (saved or unsaved settings)",
)
async def test_connection(
    body: ConnectionTestRequest, config: ConfigStoreDep, policy: URLPolicyDep
) -> ConnectionTestResult:
    """Check reachability, the API key and what the key can see (people and albums)."""
    stored = config.config.immich.accounts.get(body.account) if body.account else None
    url = body.url or (stored.url if stored else None)
    key = body.api_key
    if (key is None or is_masked(key)) and stored is not None:
        key = stored.api_key.get_secret_value()
    if not url or not key:
        return ConnectionTestResult(ok=False, error="Give a URL and an API key")
    try:
        account = ImmichAccountConfig(url=url, api_key=SecretStr(key))
        await validate_external_url(account.url, policy)
    except (ValidationError, UnsafeURLError) as exc:
        detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        return ConnectionTestResult(ok=False, error=detail)
    name = body.account or "test"
    try:
        async with ImmichClient(name, account) as client:
            version = await client.version()
            people, albums = await asyncio.gather(client.people(), client.albums())
    except ProviderError as exc:
        return ConnectionTestResult(ok=False, error=str(exc))
    return ConnectionTestResult(ok=True, version=version, people=len(people), albums=len(albums))
