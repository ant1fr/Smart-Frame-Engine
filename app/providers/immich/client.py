"""Async Immich REST client and the gateway that opens clients for configured accounts.

Only the endpoints the engine needs are implemented. Contracts were pinned against the
OpenAPI document of Immich 3.3.5 (``/api/spec.json``); every payload is parsed leniently
(unknown fields ignored) so minor server upgrades do not break the provider.

Security: API keys travel in the ``x-api-key`` header only, redirects are never followed,
URLs go through the SSRF policy and error messages never include response bodies.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from functools import lru_cache
from types import TracebackType
from typing import Any, Final, Self, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from app.core.config_models import ImmichAccountConfig
from app.core.config_store import ConfigStore
from app.core.security import (
    PayloadTooLargeError,
    UnsafeURLError,
    URLPolicy,
    read_limited,
    validate_external_url,
)
from app.providers.base import (
    InvalidSelectionError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.providers.immich.models import (
    AlbumDto,
    AssetDto,
    FaceDto,
    PersonDto,
    SearchFilters,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

CONNECT_TIMEOUT_S: Final = 10.0
READ_TIMEOUT_S: Final = 30.0
PEOPLE_PAGE_SIZE: Final = 500
MAX_PEOPLE_PAGES: Final = 50

_ASSETS: Final = TypeAdapter(list[AssetDto])
_ALBUMS: Final = TypeAdapter(list[AlbumDto])
_FACES: Final = TypeAdapter(list[FaceDto])
_PEOPLE: Final = TypeAdapter(list[PersonDto])


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """One TLS context for every short-lived client (building it is the slowest part)."""
    return httpx.create_ssl_context()


class ImmichError(ProviderUnavailableError):
    """Base class of Immich communication failures (messages never contain secrets)."""


class ImmichUnreachableError(ImmichError):
    """The server did not answer (DNS, connection refused, timeout...)."""


class ImmichAuthError(ImmichError):
    """The server rejected the API key."""


class ImmichResponseError(ImmichError):
    """The server answered with an error status or an unexpected payload."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ImmichClient:
    """One Immich account. Use as ``async with`` (closes the HTTP connection pool)."""

    def __init__(
        self,
        name: str,
        account: ImmichAccountConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._http = httpx.AsyncClient(
            base_url=account.url,
            headers={"x-api-key": account.api_key.get_secret_value(), "Accept": "application/json"},
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            follow_redirects=False,
            verify=_ssl_context(),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    # --- plumbing -----------------------------------------------------------------------
    def _fail(self, exc: Exception) -> ImmichError:
        if isinstance(exc, httpx.TimeoutException):
            return ImmichUnreachableError(
                f"Immich account {self.name!r} did not respond in time", timeout=True
            )
        return ImmichUnreachableError(
            f"Cannot connect to Immich account {self.name!r} ({type(exc).__name__})"
        )

    def _check(self, response: httpx.Response) -> None:
        status = response.status_code
        if status in {401, 403}:
            raise ImmichAuthError(f"Immich account {self.name!r} rejected the API key")
        if status >= 400 or 300 <= status < 400:
            raise ImmichResponseError(
                f"Immich account {self.name!r} answered HTTP {status}", status
            )

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str | int | bool] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise self._fail(exc) from None
        self._check(response)
        try:
            return response.json()
        except ValueError:
            raise ImmichResponseError(
                f"Immich account {self.name!r} returned invalid JSON", response.status_code
            ) from None

    def _parse(self, adapter: TypeAdapter[T], payload: Any) -> T:
        try:
            return adapter.validate_python(payload)
        except ValidationError:
            raise ImmichResponseError(
                f"Immich account {self.name!r} returned an unexpected payload"
            ) from None

    # --- endpoints ----------------------------------------------------------------------
    async def version(self) -> str:
        """``GET /api/server/version`` (no authentication needed) as ``major.minor.patch``."""
        data = await self._json("GET", "/api/server/version")
        try:
            return f"{data['major']}.{data['minor']}.{data['patch']}"
        except (KeyError, TypeError):
            raise ImmichResponseError(
                f"Immich account {self.name!r} returned an unexpected version payload"
            ) from None

    async def people(self) -> list[PersonDto]:
        """Every visible person (``GET /api/people``, all pages)."""
        people: list[PersonDto] = []
        for page in range(1, MAX_PEOPLE_PAGES + 1):
            data = await self._json(
                "GET",
                "/api/people",
                params={"withHidden": False, "page": page, "size": PEOPLE_PAGE_SIZE},
            )
            if not isinstance(data, dict):
                raise ImmichResponseError(
                    f"Immich account {self.name!r} returned a bad people list"
                )
            page_people: list[PersonDto] = self._parse(_PEOPLE, data.get("people", []))
            people.extend(page_people)
            if not data.get("hasNextPage"):
                break
        return people

    async def albums(self) -> list[AlbumDto]:
        """Albums the key can see (owned and shared)."""
        return self._parse(_ALBUMS, await self._json("GET", "/api/albums"))

    async def search_random(self, filters: SearchFilters, size: int) -> list[AssetDto]:
        """``POST /api/search/random``."""
        data = await self._json("POST", "/api/search/random", json=filters.body(size))
        return self._parse(_ASSETS, data)

    async def search_smart(self, query: str, filters: SearchFilters, size: int) -> list[AssetDto]:
        """``POST /api/search/smart`` (CLIP): the top ``size`` matches of ``query``."""
        body = {**filters.body(size), "query": query}
        data = await self._json("POST", "/api/search/smart", json=body)
        items = data.get("assets", {}).get("items", []) if isinstance(data, dict) else None
        if items is None:
            raise ImmichResponseError(f"Immich account {self.name!r} returned a bad search result")
        return self._parse(_ASSETS, items)

    async def asset(self, asset_id: str) -> AssetDto:
        """Full metadata of one asset."""
        data = await self._json("GET", f"/api/assets/{asset_id}")
        return self._parse(TypeAdapter(AssetDto), data)

    async def faces(self, asset_id: str) -> list[FaceDto]:
        """Detected faces of an asset (``GET /api/faces?id=``), with their person."""
        return self._parse(_FACES, await self._json("GET", "/api/faces", params={"id": asset_id}))

    async def person_thumbnail(self, person_id: str, *, max_bytes: int) -> bytes:
        """The face thumbnail of a person (``GET /api/people/{id}/thumbnail``).

        Raises:
            ImmichResponseError: Error status (``status_code`` set) or oversized body.
        """
        return await self._bytes(f"/api/people/{person_id}/thumbnail", {}, max_bytes)

    async def _bytes(self, path: str, params: dict[str, str | bool], max_bytes: int) -> bytes:
        try:
            async with self._http.stream(
                "GET", path, params=params, headers={"Accept": "*/*"}
            ) as response:
                self._check(response)
                declared = response.headers.get("content-length")
                return await read_limited(
                    response.aiter_bytes(),
                    max_bytes,
                    declared_length=int(declared) if declared and declared.isdigit() else None,
                )
        except PayloadTooLargeError:
            raise ImmichResponseError(
                f"Immich asset exceeds the {max_bytes // (1024 * 1024)} MB limit", 413
            ) from None
        except httpx.HTTPError as exc:
            raise self._fail(exc) from None

    async def download(
        self,
        asset_id: str,
        *,
        variant: str,
        max_bytes: int,
        edited: bool = False,
    ) -> bytes:
        """Download an asset image.

        Args:
            asset_id: Immich asset ID.
            variant: ``original`` (the file as uploaded) or a thumbnail size
                (``fullsize`` or ``preview``).
            max_bytes: Abort once the body exceeds this size.
            edited: Ask for the edited version of an edited asset.

        Raises:
            ImmichResponseError: Error status (``status_code`` set) or oversized body.
        """
        params: dict[str, str | bool] = {}
        if edited:
            params["edited"] = True
        if variant == "original":
            path = f"/api/assets/{asset_id}/original"
        else:
            path = f"/api/assets/{asset_id}/thumbnail"
            params["size"] = variant
        return await self._bytes(path, params, max_bytes)


class ImmichGateway:
    """Resolves configured accounts and opens validated clients for them."""

    def __init__(
        self,
        config_store: ConfigStore,
        url_policy: URLPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config_store = config_store
        self._url_policy = url_policy
        self._transport = transport

    def accounts(self, names: Sequence[str] | None = None) -> dict[str, ImmichAccountConfig]:
        """Configured accounts (optionally only ``names``), in configuration order.

        Raises:
            ProviderNotConfiguredError: No account is configured.
            InvalidSelectionError: ``names`` references an unknown account.
        """
        configured = self._config_store.config.immich.accounts
        if not configured:
            raise ProviderNotConfiguredError(
                "No Immich account is configured (set IMMICH_URL and IMMICH_API_KEY, "
                "or add immich.accounts in the settings)"
            )
        if names is None:
            return dict(configured)
        unknown = [name for name in names if name not in configured]
        if unknown:
            raise InvalidSelectionError(
                f"Unknown Immich account(s): {', '.join(sorted(set(unknown)))}. "
                f"Configured: {', '.join(configured)}"
            )
        return {name: configured[name] for name in configured if name in names}

    @asynccontextmanager
    async def client(self, name: str) -> AsyncIterator[ImmichClient]:
        """Open a client for account ``name`` after checking its URL against the SSRF policy."""
        account = self.accounts([name])[name]
        try:
            await validate_external_url(account.url, self._url_policy)
        except UnsafeURLError as exc:
            raise ImmichResponseError(
                f"Immich account {name!r} URL is blocked by the URL policy: {exc} "
                "(add its host to URL_ALLOWLIST)"
            ) from None
        async with ImmichClient(name, account, transport=self._transport) as client:
            yield client
