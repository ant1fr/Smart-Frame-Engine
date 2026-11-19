from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config_models import ImmichAccountConfig
from app.core.config_store import ConfigStore
from app.core.security import URLPolicy
from app.providers.base import InvalidSelectionError, ProviderNotConfiguredError
from app.providers.immich.client import (
    ImmichAuthError,
    ImmichClient,
    ImmichGateway,
    ImmichResponseError,
    ImmichUnreachableError,
)
from app.providers.immich.models import SearchFilters
from tests.immich_fake import FakeImmich, jpeg


@contextmanager
def mocked(*fakes: FakeImmich) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        for fake in fakes:
            fake.mount(router)
        yield router


def account(fake: FakeImmich, key: str | None = None) -> ImmichAccountConfig:
    return ImmichAccountConfig.model_validate({"url": fake.base, "api_key": key or fake.key})


def make_gateway(tmp_path: Path, fakes: dict[str, FakeImmich], **policy: object) -> ImmichGateway:
    accounts = {n: {"url": f.base, "api_key": f.key} for n, f in fakes.items()}
    store = ConfigStore(tmp_path / "config.json", {"immich": {"accounts": accounts}})
    return ImmichGateway(store, URLPolicy(**policy))  # type: ignore[arg-type]


async def test_requests_send_the_api_key_header_only_and_parse_payloads() -> None:
    fake = FakeImmich()
    fake.add_person("p1", "Alice")
    fake.add_asset("a1", people=[("p1", "Alice")])
    fake.add_album("al1", "Family", ["a1"])
    with mocked(fake) as router:
        async with ImmichClient("home", account(fake)) as client:
            assert await client.version() == "3.3.5"
            assert [p.name for p in await client.people()] == ["Alice"]
            assert [a.albumName for a in await client.albums()] == ["Family"]
            assets = await client.search_random(SearchFilters(), 5)
            assert [a.id for a in assets] == ["a1"] and assets[0].people[0].name == "Alice"
            assert (await client.asset("a1")).exifInfo is not None
        sent = [c.request for c in router.calls if c.request.url.path != "/api/server/version"]
        assert sent and all(r.headers["x-api-key"] == "good-key" for r in sent)
        assert all("good-key" not in str(r.url) for r in sent)


async def test_people_are_paged() -> None:
    fake = FakeImmich()
    fake.people_page_size = 2
    for index in range(5):
        fake.add_person(f"p{index}", f"Person {index}")
    with mocked(fake):
        async with ImmichClient("home", account(fake)) as client:
            assert len(await client.people()) == 5
    assert [q["page"] for _, p, q in fake.calls if p == "/api/people"] == ["1", "2", "3"]


async def test_search_bodies_carry_the_filters() -> None:
    fake = FakeImmich()
    fake.add_asset("a1")
    filters = SearchFilters(("p1", "p2"), ("al1",), favorites_only=True, exclude_archived=True)
    with mocked(fake):
        async with ImmichClient("home", account(fake)) as client:
            await client.search_random(filters, 7)
            await client.search_smart("beach", filters, 7)
    random_body, smart_body = (
        fake.bodies("/api/search/random")[0],
        fake.bodies("/api/search/smart")[0],
    )
    assert random_body == {
        "type": "IMAGE",
        "size": 7,
        "withExif": True,
        "withPeople": True,
        "personIds": ["p1", "p2"],
        "albumIds": ["al1"],
        "isFavorite": True,
        "visibility": "timeline",
    }
    assert smart_body == {**random_body, "query": "beach"}
    assert "visibility" not in SearchFilters(exclude_archived=False).body(1)


async def test_error_statuses_map_to_typed_errors() -> None:
    fake = FakeImmich()
    with mocked(fake):
        async with ImmichClient("home", account(fake, key="wrong")) as client:
            with pytest.raises(ImmichAuthError, match="rejected the API key"):
                await client.albums()
        fake.search_error = 500
        async with ImmichClient("home", account(fake)) as client:
            with pytest.raises(ImmichResponseError) as info:
                await client.search_random(SearchFilters(), 5)
            assert info.value.status_code == 500
            with pytest.raises(ImmichResponseError) as missing:
                await client.asset("nope")
            assert missing.value.status_code == 404


async def test_unreachable_and_timeout_errors_do_not_leak_urls_or_keys() -> None:
    fake = FakeImmich()
    with mocked(fake) as router:
        route = router.route(host="immich.test")
        route.mock(side_effect=httpx.ConnectError("boom good-key http://immich.test"))
        async with ImmichClient("home", account(fake)) as client:
            with pytest.raises(ImmichUnreachableError) as info:
                await client.albums()
        assert not info.value.timeout
        assert "good-key" not in str(info.value) and "immich.test" not in str(info.value)

        route.mock(side_effect=httpx.ReadTimeout("slow"))
        async with ImmichClient("home", account(fake)) as client:
            with pytest.raises(ImmichUnreachableError) as slow:
                await client.albums()
        assert slow.value.timeout


async def test_redirects_are_never_followed() -> None:
    fake = FakeImmich()
    with mocked(fake) as router:
        router.route(host="immich.test").respond(302, headers={"location": "http://evil.test/"})
        async with ImmichClient("home", account(fake)) as client:
            with pytest.raises(ImmichResponseError) as info:
                await client.albums()
    assert info.value.status_code == 302


async def test_download_variants_params_and_size_limit() -> None:
    fake = FakeImmich()
    fake.add_asset("a1", original=jpeg((200, 100)))
    fake.add_asset("big", original=b"x" * 5000)
    with mocked(fake):
        async with ImmichClient("home", account(fake)) as client:
            assert len(await client.download("a1", variant="original", max_bytes=10**6)) > 100
            await client.download("a1", variant="preview", max_bytes=10**6)
            await client.download("a1", variant="original", max_bytes=10**6, edited=True)
            with pytest.raises(ImmichResponseError) as info:
                await client.download("big", variant="original", max_bytes=1000)
            assert info.value.status_code == 413
    downloads = fake.downloads()
    assert downloads[0] == ("/api/assets/a1/original", {})
    assert downloads[1] == ("/api/assets/a1/thumbnail", {"size": "preview"})
    assert downloads[2] == ("/api/assets/a1/original", {"edited": "true"})


async def test_faces_endpoint_is_parsed() -> None:
    from tests.immich_fake import face

    fake = FakeImmich()
    fake.add_asset(
        "a1", faces=[face("p1", (10, 20, 110, 140), name="Alice"), face(None, (1, 2, 3, 4))]
    )
    with mocked(fake):
        async with ImmichClient("home", account(fake)) as client:
            faces = await client.faces("a1")
    assert faces[0].person is not None and faces[0].person.id == "p1"
    assert faces[1].person is None and faces[0].boundingBoxX2 == 110


# --- gateway ---------------------------------------------------------------------------------
async def test_gateway_account_selection_errors(tmp_path: Path) -> None:
    fake = FakeImmich()
    gateway = make_gateway(tmp_path, {"home": fake})
    assert list(gateway.accounts()) == ["home"]
    with pytest.raises(InvalidSelectionError, match="Unknown Immich account"):
        gateway.accounts(["nope"])
    empty = ImmichGateway(ConfigStore(tmp_path / "other.json", {}), URLPolicy())
    with pytest.raises(ProviderNotConfiguredError):
        empty.accounts()


async def test_gateway_blocks_private_urls_when_policy_says_so(tmp_path: Path) -> None:
    fake = FakeImmich(base="http://192.168.1.50:2283")
    gateway = make_gateway(tmp_path, {"home": fake}, block_private=True)
    with pytest.raises(ImmichResponseError, match="URL_ALLOWLIST"):
        async with gateway.client("home"):
            pass

    allowed = make_gateway(
        tmp_path, {"home": fake}, block_private=True, hostnames=frozenset({"192.168.1.50"})
    )
    with mocked(fake):
        async with allowed.client("home") as client:
            assert await client.version() == "3.3.5"
