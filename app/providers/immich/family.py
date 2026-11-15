"""Family Mode filters for Immich candidates (and the always-on Locked Folder rule).

Two independent layers:

* **Locked Folder** (always on): assets Immich marks ``locked`` are never selected, whatever
  the mode, unless a caller asks for one explicitly by asset ID (no such path exists today).
* **Family Mode** (``family`` settings, overridable by ``POST /api/context``): keeps photos
  showing at least one *member* (when members are set) and drops photos in a *private album*
  or showing a *private person*. It needs no per-photo curation: the rules are evaluated
  against every candidate.

The set of private-album asset IDs is cached per account with the people-cache TTL.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config_models import FamilyConfig
from app.core.config_store import ConfigStore
from app.providers.immich.client import ImmichClient, ImmichGateway
from app.providers.immich.models import AssetDto
from app.providers.immich.people import PeopleDirectory, resolve_person

logger = logging.getLogger(__name__)

LOCKED = "locked"


def is_locked(asset: AssetDto) -> bool:
    """Whether the asset lives in Immich's Locked Folder."""
    return asset.visibility.lower() == LOCKED


@dataclass(frozen=True, slots=True)
class FamilyRules:
    """The Family Mode rules resolved for one account."""

    member_ids: frozenset[str] = frozenset()
    require_member: bool = False
    private_people_ids: frozenset[str] = frozenset()
    private_asset_ids: frozenset[str] = frozenset()

    def allows(self, asset: AssetDto) -> bool:
        """Whether Family Mode lets this asset through."""
        if asset.id in self.private_asset_ids:
            return False
        present = {person.id for person in asset.people}
        if present & self.private_people_ids:
            return False
        return not (self.require_member and not present & self.member_ids)


class FamilyDirectory:
    """Resolves :class:`FamilyRules` per account, caching private-album membership."""

    def __init__(
        self,
        config_store: ConfigStore,
        gateway: ImmichGateway,
        people: PeopleDirectory,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_store = config_store
        self._gateway = gateway
        self._people = people
        self._clock = clock
        self._albums: dict[tuple[str, tuple[str, ...]], tuple[float, frozenset[str]]] = {}

    async def rules(self, account: str, client: ImmichClient, family: FamilyConfig) -> FamilyRules:
        """Rules for ``account`` (unknown names are ignored there: other accounts may know them)."""
        immich = self._config_store.config.immich
        people = await self._people.get(account)

        def ids(names: list[str]) -> frozenset[str]:
            return frozenset(
                person_id
                for name in names
                for person_id in resolve_person(name, account, people, immich.person_aliases)
            )

        return FamilyRules(
            member_ids=ids(family.members),
            require_member=family.require_member and bool(family.members),
            private_people_ids=ids(family.private_people),
            private_asset_ids=await self._private_assets(account, client, family.private_albums),
        )

    async def _private_assets(
        self, account: str, client: ImmichClient, albums: list[str]
    ) -> frozenset[str]:
        if not albums:
            return frozenset()
        key = (account, tuple(sorted(albums, key=str.casefold)))
        ttl = self._config_store.config.immich.people_cache_ttl_s
        cached = self._albums.get(key)
        if cached is not None and self._clock() - cached[0] < ttl:
            return cached[1]
        wanted = {name.casefold() for name in albums}
        album_ids = [
            album.id
            for album in await client.albums()
            if album.id.casefold() in wanted or album.albumName.casefold() in wanted
        ]
        assets = frozenset(await client.album_asset_ids(album_ids)) if album_ids else frozenset()
        self._albums[key] = (self._clock(), assets)
        logger.info(
            "Family Mode: %d private album(s), %d asset(s) excluded in account %r",
            len(album_ids),
            len(assets),
            account,
        )
        return assets

    def forget(self) -> None:
        """Drop the cached private-album membership."""
        self._albums.clear()
