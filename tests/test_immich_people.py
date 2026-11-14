from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.config_store import ConfigStore
from app.core.security import URLPolicy
from app.providers.immich.client import ImmichGateway
from app.providers.immich.models import PersonDto
from app.providers.immich.people import (
    AccountPeople,
    AmbiguousPersonError,
    PeopleDirectory,
    resolve_person,
    suggest_names,
)
from tests.immich_fake import FakeImmich
from tests.test_immich_client import mocked

NOW = datetime(2026, 1, 1, tzinfo=UTC)
UUID_A = "11111111-2222-3333-4444-555555555555"


def build(*people: tuple[str, str]) -> AccountPeople:
    dtos = [PersonDto(id=pid, name=name) for pid, name in people]
    return AccountPeople.build(dtos, ("http://x", "k"), NOW)


def test_names_are_case_insensitive_and_hidden_or_unnamed_are_skipped() -> None:
    people = AccountPeople.build(
        [
            PersonDto(id="1", name="Alice"),
            PersonDto(id="2", name=""),
            PersonDto(id="3", name="Hidden", isHidden=True),
        ],
        ("u", "k"),
        NOW,
    )
    assert resolve_person("  alice".strip(), "home", people, {}) == ["1"]
    assert resolve_person("ALICE", "home", people, {}) == ["1"]
    assert resolve_person("Hidden", "home", people, {}) == []
    assert people.names() == ["Alice"]


def test_unknown_name_resolves_to_nothing_for_this_account() -> None:
    assert resolve_person("Zed", "home", build(("1", "Alice")), {}) == []


def test_duplicate_names_are_ambiguous_until_an_alias_pins_an_id() -> None:
    people = build(("1", "Marie"), ("2", "Marie"), ("3", "Bob"))
    with pytest.raises(AmbiguousPersonError, match="person_aliases"):
        resolve_person("Marie", "home", people, {})
    assert resolve_person("Marie", "home", people, {"Marie": {"home": ["2"]}}) == ["2"]
    # An alias is per account: another account without one is still ambiguous.
    with pytest.raises(AmbiguousPersonError):
        resolve_person("Marie", "home", people, {"Marie": {"partner": ["2"]}})


def test_aliases_map_a_canonical_name_to_ids_or_other_names_per_account() -> None:
    partner = build(("p1", "Maman"), ("p2", "Maman Sylvie"))
    aliases = {"Marie": {"partner": ["Maman", UUID_A]}}
    assert resolve_person("marie", "partner", partner, aliases) == ["p1", UUID_A]
    # Without an alias entry for the account, the canonical name is looked up directly.
    assert resolve_person("Marie", "home", build(("h1", "Marie")), aliases) == ["h1"]
    assert resolve_person("Marie", "partner", build(("x", "Other")), {"Marie": {}}) == []


def test_alias_listing_a_known_id_keeps_it() -> None:
    people = build(("not-a-uuid-id", "Bob"))
    assert resolve_person("Robert", "home", people, {"Robert": {"home": ["not-a-uuid-id"]}}) == [
        "not-a-uuid-id"
    ]


def test_suggestions_are_close_matches() -> None:
    suggestions = suggest_names("Alise", ["Alice", "Bob", "Alicia"])
    assert suggestions[0] == "Alice" and "Bob" not in suggestions
    assert suggest_names("Zzz", ["Alice"]) == []


# --- directory (cache, TTL, refresh, errors) -------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_directory(
    tmp_path: Path, fake: FakeImmich, ttl: int = 60
) -> tuple[PeopleDirectory, ConfigStore, Clock]:
    store = ConfigStore(
        tmp_path / "config.json",
        {
            "immich": {
                "accounts": {"home": {"url": fake.base, "api_key": fake.key}},
                "people_cache_ttl_s": ttl,
            }
        },
    )
    clock = Clock()
    directory = PeopleDirectory(store, ImmichGateway(store, URLPolicy()), clock=clock)
    return directory, store, clock


def people_calls(fake: FakeImmich) -> int:
    return len([1 for _, path, _ in fake.calls if path == "/api/people"])


async def test_directory_caches_until_ttl_or_refresh(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_person("1", "Alice")
    directory, _, clock = make_directory(tmp_path, fake, ttl=60)
    with mocked(fake):
        first = await directory.get("home")
        assert await directory.get("home") is first
        assert people_calls(fake) == 1

        fake.add_person("2", "Bob")
        assert (await directory.get("home", refresh=True)).names() == ["Alice", "Bob"]
        assert people_calls(fake) == 2

        clock.now += 61
        await directory.get("home")
        assert people_calls(fake) == 3


async def test_directory_refetches_when_the_account_changes(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_person("1", "Alice")
    directory, store, _ = make_directory(tmp_path, fake)
    with mocked(fake):
        await directory.get("home")
        await store.update(
            {"immich": {"accounts": {"home": {"url": fake.base, "api_key": "good-key"}}}}
        )  # same values: cache stays valid
        await directory.get("home")
        assert people_calls(fake) == 1
        fake.key = "rotated-key"
        await store.update(
            {"immich": {"accounts": {"home": {"url": fake.base, "api_key": "rotated-key"}}}}
        )
        await directory.get("home")
        assert people_calls(fake) == 2


async def test_ttl_zero_disables_the_cache(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_person("1", "Alice")
    directory, _, _ = make_directory(tmp_path, fake, ttl=0)
    with mocked(fake):
        await directory.get("home")
        await directory.get("home")
    assert people_calls(fake) == 2


async def test_snapshot_reports_ambiguity_and_errors_per_account(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_person("1", "Marie")
    fake.add_person("2", "marie")
    fake.add_person("3", "Bob")
    directory, _, _ = make_directory(tmp_path, fake)
    with mocked(fake):
        [entry] = await directory.snapshot(["home"], refresh=True)
        assert entry.error is None and entry.ambiguous == ["Marie"]
        fake.key = "changed"  # the stored key no longer matches the server
        [failed] = await directory.snapshot(["home"], refresh=True)
    assert failed.people is None and failed.error and "rejected the API key" in failed.error
