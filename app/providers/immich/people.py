"""People discovery and name resolution across Immich accounts.

The engine only ever deals in plain person *names* (never Home Assistant entity IDs).
This module turns a name into the Immich person IDs of one account:

1. ``immich.person_aliases`` wins: ``{"Marie": {"partner": ["Maman", "<person-id>"]}}``
   maps the canonical name to IDs or names that only exist in that account.
2. Otherwise the name is looked up (case-insensitively) in the account's people list.

Two people sharing a name in one account is an error unless an alias pins an ID.
"""

from __future__ import annotations

import difflib
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from app.core.config_store import ConfigStore
from app.providers.base import InvalidSelectionError, ProviderError
from app.providers.immich.client import ImmichGateway
from app.providers.immich.models import PersonDto

_UUID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class AmbiguousPersonError(InvalidSelectionError):
    """Several people of one account carry the requested name."""


@dataclass(frozen=True, slots=True)
class AccountPeople:
    """The named people of one account."""

    by_id: dict[str, str]
    by_name: dict[str, list[str]]
    fetched_at: datetime
    fingerprint: tuple[str, str]

    @classmethod
    def build(
        cls, people: Iterable[PersonDto], fingerprint: tuple[str, str], fetched_at: datetime
    ) -> AccountPeople:
        by_id: dict[str, str] = {}
        by_name: dict[str, list[str]] = {}
        for person in people:
            name = person.name.strip()
            if not name or person.isHidden:
                continue
            by_id[person.id] = name
            by_name.setdefault(name.casefold(), []).append(person.id)
        return cls(by_id, by_name, fetched_at, fingerprint)

    def names(self) -> list[str]:
        """Distinct display names, sorted."""
        return sorted(set(self.by_id.values()), key=str.casefold)


@dataclass(slots=True)
class PeopleSnapshot:
    """What ``GET /api/immich/people`` reports for one account."""

    account: str
    people: AccountPeople | None = None
    error: str | None = None
    ambiguous: list[str] = field(default_factory=list)


def resolve_person(
    name: str,
    account: str,
    people: AccountPeople,
    aliases: Mapping[str, Mapping[str, list[str]]],
) -> list[str]:
    """Immich person IDs of ``name`` in ``account`` (empty when the account has no such person).

    Raises:
        AmbiguousPersonError: Several people match and no alias pins one.
    """
    folded = name.casefold()
    alias_entries: list[str] = []
    for canonical, per_account in aliases.items():
        if canonical.casefold() == folded and account in per_account:
            alias_entries = per_account[account]
    candidates = alias_entries or [name]

    ids: list[str] = []
    for entry in candidates:
        if entry in people.by_id or _UUID_RE.match(entry):
            matched = [entry]
        else:
            matched = people.by_name.get(entry.casefold(), [])
            if len(matched) > 1 and not alias_entries:
                raise AmbiguousPersonError(
                    f"Several people are named {entry!r} in Immich account {account!r}; "
                    f"pin one ID in immich.person_aliases[{name!r}][{account!r}]"
                )
        for person_id in matched:
            if person_id not in ids:
                ids.append(person_id)
    return ids


def suggest_names(name: str, available: Iterable[str]) -> list[str]:
    """Close matches of ``name`` among ``available`` person names (for error messages)."""
    return difflib.get_close_matches(name, sorted(set(available)), n=3, cutoff=0.6)


class PeopleDirectory:
    """Per-account cache of the people list with TTL and manual refresh."""

    def __init__(
        self,
        config_store: ConfigStore,
        gateway: ImmichGateway,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config_store = config_store
        self._gateway = gateway
        self._clock = clock
        self._now = now
        self._cache: dict[str, tuple[float, AccountPeople]] = {}

    def _fingerprint(self, account: str) -> tuple[str, str]:
        config = self._gateway.accounts([account])[account]
        return config.url, config.api_key.get_secret_value()

    async def get(self, account: str, *, refresh: bool = False) -> AccountPeople:
        """The people of ``account``, from cache unless expired, changed or ``refresh``."""
        fingerprint = self._fingerprint(account)
        ttl = self._config_store.config.immich.people_cache_ttl_s
        cached = self._cache.get(account)
        if (
            not refresh
            and cached is not None
            and cached[1].fingerprint == fingerprint
            and self._clock() - cached[0] < ttl
        ):
            return cached[1]
        async with self._gateway.client(account) as client:
            people = AccountPeople.build(await client.people(), fingerprint, self._now())
        self._cache[account] = (self._clock(), people)
        return people

    async def snapshot(self, accounts: Iterable[str], *, refresh: bool) -> list[PeopleSnapshot]:
        """People of several accounts; a failing account is reported, not raised."""
        result: list[PeopleSnapshot] = []
        for account in accounts:
            entry = PeopleSnapshot(account)
            try:
                entry.people = await self.get(account, refresh=refresh)
            except ProviderError as exc:
                entry.error = str(exc)
            else:
                entry.ambiguous = sorted(
                    {
                        entry.people.by_id[ids[0]]
                        for ids in entry.people.by_name.values()
                        if len(ids) > 1
                    },
                    key=str.casefold,
                )
            result.append(entry)
        return result

    def forget(self) -> None:
        """Drop every cached list."""
        self._cache.clear()
