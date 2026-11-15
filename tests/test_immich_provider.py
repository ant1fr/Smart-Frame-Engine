from __future__ import annotations

import io
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.security import URLPolicy
from app.providers.base import (
    InvalidSelectionError,
    NoCandidateError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.providers.immich.client import ImmichAuthError, ImmichGateway
from app.providers.immich.models import ImmichDisplayRequest
from app.providers.immich.people import AmbiguousPersonError, PeopleDirectory
from app.providers.immich.provider import ImmichProvider, build_title_and_details, is_raw
from tests.immich_fake import FakeImmich, face, heic
from tests.test_immich_client import mocked

MAX_BYTES = 20 * 1024 * 1024


class Cycle(random.Random):
    """Deterministic ``choice``: walks the eligible candidates in order, round-robin."""

    def __init__(self) -> None:
        super().__init__(0)
        self.turn = 0

    def choice(self, seq: Any) -> Any:
        self.turn += 1
        return seq[(self.turn - 1) % len(seq)]


class Env:
    def __init__(
        self,
        tmp_path: Path,
        fakes: dict[str, FakeImmich],
        quality: dict[str, Any] | None = None,
        **immich: Any,
    ) -> None:
        accounts = {n: {"url": f.base, "api_key": f.key} for n, f in fakes.items()}
        self.store = ConfigStore(
            tmp_path / "config.json",
            {
                "immich": {"accounts": accounts, **immich},
                # The quality gate has its own tests (test_immich_quality.py).
                "quality": {"enabled": False} if quality is None else quality,
            },
        )
        self.gateway = ImmichGateway(self.store, URLPolicy())
        self.history = HistoryStore(tmp_path / "history.json")
        self.provider = ImmichProvider(
            self.store,
            self.gateway,
            PeopleDirectory(self.store, self.gateway),
            self.history,
            max_bytes=MAX_BYTES,
            rng=Cycle(),
        )

    async def fetch(self, **request: Any) -> Any:
        return await self.provider.fetch(ImmichDisplayRequest(**request))


def family(fake: FakeImmich | None = None) -> FakeImmich:
    fake = fake or FakeImmich()
    for pid, name in (("p1", "Alice"), ("p2", "Bob"), ("p3", "Kid")):
        fake.add_person(pid, name)
    fake.add_asset("a1", people=[("p1", "Alice")], faces=[face("p1", (300, 100, 400, 200))])
    fake.add_asset("a2", people=[("p2", "Bob")])
    fake.add_asset(
        "a3",
        people=[("p1", "Alice"), ("p2", "Bob")],
        faces=[
            face("p1", (50, 100, 150, 200)),
            face("p2", (400, 100, 500, 200)),
            face(None, (0, 0, 30, 30)),
        ],
    )
    fake.add_asset("a4", people=[("p3", "Kid")])
    fake.add_asset("a5", isFavorite=True)
    fake.add_asset("a6", people=[("p1", "Alice")], isArchived=True)
    return fake


def bodies(fake: FakeImmich) -> list[dict[str, Any]]:
    return fake.bodies("/api/search/random")


# --- basics -------------------------------------------------------------------
async def test_random_pick_returns_asset_with_title_details_and_regions(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_person("p1", "Alice")
    fake.add_asset("a1", people=[("p1", "Alice")], faces=[face("p1", (300, 100, 400, 200))])
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch()

    assert asset.source == "immich" and asset.history_key == "sha1:sum-a1"
    assert asset.title == "Alice · 14 Jul 2024 · Lyon, France"
    assert asset.details == {
        "account": "home",
        "asset_id": "a1",
        "filename": "a1.jpg",
        "taken_at": "2024-07-14T10:22:11+00:00",
        "location": "Lyon, France",
        "people": ["Alice"],
        "quality": "original",
        "faces_used": 1,
    }
    assert Image.open(io.BytesIO(asset.data)).size == (640, 480)
    assert len(asset.regions) == 1 and asset.recommended_fit is None
    assert bodies(fake)[0]["visibility"] == "timeline" and bodies(fake)[0]["type"] == "IMAGE"


async def test_archived_and_trashed_assets_are_never_picked(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("only", isArchived=True)
    fake.add_asset("gone", isTrashed=True)
    env = Env(tmp_path, {"home": fake})
    with mocked(fake), pytest.raises(NoCandidateError):
        await env.fetch()
    # Archived assets are allowed when the config says so (the server filter is dropped too).
    env = Env(tmp_path, {"home": fake}, exclude_archived=False)
    with mocked(fake):
        asset = await env.fetch()
    assert asset.details["asset_id"] == "only"
    assert "visibility" not in bodies(fake)[-1]


async def test_no_account_is_not_configured(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfiguredError):
        await Env(tmp_path, {}).fetch()


async def test_rejected_api_key_is_an_auth_error(tmp_path: Path) -> None:
    server = family()
    env = Env(tmp_path, {"home": server})
    server.key = "rotated"  # the stored key no longer matches the server
    with mocked(server), pytest.raises(ImmichAuthError):
        await env.fetch()


# --- people -------------------------------------------------------------------
async def test_any_runs_one_query_per_person_and_all_runs_one_combined_query(
    tmp_path: Path,
) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        any_asset = await env.fetch(persons=["Alice", "Bob"], match_mode="any")
        assert any_asset.details["asset_id"] in {"a1", "a2", "a3"}
        assert [b["personIds"] for b in bodies(fake)] == [["p1"], ["p2"]]

        fake.calls.clear()
        all_asset = await env.fetch(persons=["alice", "BOB"], match_mode="all")
    assert [b["personIds"] for b in bodies(fake)] == [["p1", "p2"]]
    assert all_asset.details["asset_id"] == "a3"
    assert all_asset.details["people"] == ["Alice", "Bob"]


async def test_person_defaults_always_include_merge_and_override(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake}, persons=["Alice"], always_include=["Kid"])
    with mocked(fake):
        await env.fetch()  # config persons + always_include
        assert [b["personIds"] for b in bodies(fake)] == [["p1"], ["p3"]]

        fake.calls.clear()
        await env.fetch(persons=["Bob"])  # request persons replace config persons, keep always
        assert [b["personIds"] for b in bodies(fake)] == [["p2"], ["p3"]]

        fake.calls.clear()
        await env.fetch(persons=["Bob"], always_include=[])  # explicit [] switches it off
        assert [b["personIds"] for b in bodies(fake)] == [["p2"]]

        fake.calls.clear()
        await env.fetch(persons=[], always_include=[])  # nothing requested: unfiltered
        assert "personIds" not in bodies(fake)[0]


async def test_persons_and_always_include_are_deduplicated_case_insensitively(
    tmp_path: Path,
) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake}, always_include=["ALICE"])
    with mocked(fake):
        await env.fetch(persons=["alice", "Alice"], match_mode="all")
    assert [b["personIds"] for b in bodies(fake)] == [["p1"]]


async def test_unknown_person_is_rejected_with_suggestions(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        with pytest.raises(InvalidSelectionError, match="Did you mean: Alice"):
            await env.fetch(persons=["Alise"])
        with pytest.raises(InvalidSelectionError, match="Unknown person 'Zed'"):
            await env.fetch(persons=["Zed"])
    assert not bodies(fake)  # rejected before any search


async def test_ambiguous_person_needs_an_alias_that_pins_an_id(tmp_path: Path) -> None:
    fake = family()
    fake.add_person("p9", "Alice")  # a second Alice
    fake.add_asset("a9", people=[("p9", "Alice")])
    with mocked(fake):
        with pytest.raises(AmbiguousPersonError):
            await Env(tmp_path, {"home": fake}).fetch(persons=["Alice"])
        pinned = Env(tmp_path, {"home": fake}, person_aliases={"Alice": {"home": ["p9"]}})
        asset = await pinned.fetch(persons=["Alice"])
    assert asset.details["asset_id"] == "a9"


async def test_aliases_map_one_person_across_accounts(tmp_path: Path) -> None:
    home, partner = (
        family(FakeImmich("http://home.test", "k-home")),
        FakeImmich("http://partner.test", "k-partner"),
    )
    partner.add_person("q1", "Maman")
    partner.add_asset("b1", people=[("q1", "Maman")], checksum="sum-b1")
    env = Env(
        tmp_path,
        {"home": home, "partner": partner},
        person_aliases={"Alice": {"partner": ["Maman"]}},
    )
    with mocked(home, partner):
        seen = {
            (await env.fetch(persons=["Alice"], ignore_history=True)).details["account"]
            for _ in range(4)
        }
    assert seen == {"home", "partner"}
    assert partner.bodies("/api/search/random")[0]["personIds"] == ["q1"]
    assert home.bodies("/api/search/random")[0]["personIds"] == ["p1"]


async def test_account_without_the_person_contributes_nothing_in_all_mode(tmp_path: Path) -> None:
    home, partner = (
        family(FakeImmich("http://home.test", "k-home")),
        FakeImmich("http://partner.test", "k-partner"),
    )
    partner.add_person("q1", "Alice")
    partner.add_asset("b1", people=[("q1", "Alice")])
    partner_only = FakeImmich("http://third.test", "k-third")
    partner_only.add_asset("c1")
    env = Env(tmp_path, {"home": home, "partner": partner, "third": partner_only})
    with mocked(home, partner, partner_only):
        await env.fetch(persons=["Alice", "Bob"], match_mode="all")
    assert not partner.bodies("/api/search/random")  # no Bob there: not queried
    assert not partner_only.bodies("/api/search/random")
    assert home.bodies("/api/search/random")[0]["personIds"] == ["p1", "p2"]


# --- modes --------------------------------------------------------------------
async def test_favorites_mode_and_config_flag(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch(mode="favorites")
        assert asset.details["asset_id"] == "a5" and bodies(fake)[0]["isFavorite"] is True
        fake.calls.clear()
        await Env(tmp_path, {"home": fake}, favorites_only=True).fetch()
    assert bodies(fake)[0]["isFavorite"] is True


async def test_album_mode_by_name_by_id_and_default_from_config(tmp_path: Path) -> None:
    fake = family()
    fake.add_album("al1", "Summer", ["a4"])
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        by_name = await env.fetch(mode="album", album_name="summer")
        by_id = await env.fetch(mode="album", album_id="al1")
        assert by_name.details["asset_id"] == by_id.details["asset_id"] == "a4"
        assert bodies(fake)[0]["albumIds"] == ["al1"]

        configured = await Env(tmp_path, {"home": fake}, album="SUMMER").fetch()
        assert configured.details["asset_id"] == "a4"

        with pytest.raises(InvalidSelectionError, match="needs album_name or album_id"):
            await env.fetch(mode="album")
        with pytest.raises(InvalidSelectionError, match="Album 'Nope' was not found"):
            await env.fetch(mode="album", album_name="Nope")


async def test_clip_search_queries_each_phrase_and_picks_among_candidates(tmp_path: Path) -> None:
    fake = family()
    fake.smart_results = {"beach": ["a2", "a4"], "sunset": ["a5"]}
    env = Env(tmp_path, {"home": fake}, search_candidates=12)
    with mocked(fake):
        picks = {
            (
                await env.fetch(
                    mode="search", clip_queries=["beach", " sunset "], ignore_history=True
                )
            ).details["asset_id"]
            for _ in range(4)
        }
    smart = fake.bodies("/api/search/smart")
    assert [b["query"] for b in smart[:2]] == ["beach", "sunset"]
    assert all(b["size"] == 12 for b in smart)
    assert picks == {"a2", "a4", "a5"}


async def test_clip_search_combines_with_people_and_albums(tmp_path: Path) -> None:
    fake = family()
    fake.add_album("al1", "Trips", ["a1", "a3"])
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        await env.fetch(
            mode="search", clip_queries=["mountain"], persons=["Alice"], album_name="Trips"
        )
    body = fake.bodies("/api/search/smart")[0]
    assert body["personIds"] == ["p1"] and body["albumIds"] == ["al1"]


def test_search_mode_requires_a_query() -> None:
    with pytest.raises(ValueError, match="clip_queries"):
        ImmichDisplayRequest(mode="search")
    with pytest.raises(ValueError, match="clip_queries"):
        ImmichDisplayRequest(mode="search", clip_queries=["  "])


# --- accounts, dedupe, history ------------------------------------------------
async def test_same_photo_in_two_accounts_is_one_candidate_and_one_history_key(
    tmp_path: Path,
) -> None:
    home, partner = (
        FakeImmich("http://home.test", "k-home"),
        FakeImmich("http://partner.test", "k-partner"),
    )
    home.add_asset("h1", checksum="same-photo")
    partner.add_asset("p-copy", checksum="same-photo")
    env = Env(tmp_path, {"home": home, "partner": partner})
    with mocked(home, partner):
        asset = await env.fetch()
    assert asset.history_key == "sha1:same-photo"
    assert asset.details["account"] == "home"  # first configured account wins

    await env.history.record(asset.history_key, "immich")
    with mocked(home, partner), pytest.raises(NoCandidateError, match="were shown within"):
        await env.fetch()  # the copy in the other account is on cooldown too


async def test_assets_without_checksum_fall_back_to_account_and_id(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("n1", checksum=None)
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        assert (await env.fetch()).history_key == "home:n1"


async def test_history_cooldown_excludes_recent_photos_and_can_be_ignored(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("a1")
    fake.add_asset("a2")
    env = Env(tmp_path, {"home": fake}, anti_repeat_days=30)
    await env.history.record("sha1:sum-a1", "immich")
    with mocked(fake):
        assert {(await env.fetch()).details["asset_id"] for _ in range(2)} == {"a2"}
        await env.history.record("sha1:sum-a2", "immich")
        with pytest.raises(NoCandidateError, match="ignore_history"):
            await env.fetch()
        assert (await env.fetch(ignore_history=True)).details["asset_id"] in {"a1", "a2"}


async def test_history_of_other_sources_does_not_block_immich(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("a1")
    env = Env(tmp_path, {"home": fake})
    await env.history.record("sha1:sum-a1", "media")  # same key, different source
    with mocked(fake):
        assert (await env.fetch()).details["asset_id"] == "a1"


async def test_zero_day_cooldown_disables_the_history_filter(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("a1")
    env = Env(tmp_path, {"home": fake}, anti_repeat_days=0)
    await env.history.record("sha1:sum-a1", "immich")
    with mocked(fake):
        assert (await env.fetch()).details["asset_id"] == "a1"


async def test_a_failing_account_is_skipped_but_all_failing_raises(tmp_path: Path) -> None:
    good, bad = FakeImmich("http://good.test", "k-good"), FakeImmich("http://bad.test", "k-bad")
    good.add_asset("g1")
    bad.search_error = 500
    env = Env(tmp_path, {"bad": bad, "good": good})
    with mocked(good, bad):
        assert (await env.fetch()).details["account"] == "good"
        good.search_error = 503
        with pytest.raises(ProviderUnavailableError):
            await env.fetch()


async def test_account_subset_and_unknown_account(tmp_path: Path) -> None:
    home, partner = (
        FakeImmich("http://home.test", "k-home"),
        FakeImmich("http://partner.test", "k-partner"),
    )
    home.add_asset("h1")
    partner.add_asset("p1")
    env = Env(tmp_path, {"home": home, "partner": partner})
    with mocked(home, partner):
        for _ in range(2):
            assert (await env.fetch(accounts=["partner"], ignore_history=True)).details[
                "account"
            ] == "partner"
        with pytest.raises(InvalidSelectionError, match="Unknown Immich account"):
            await env.fetch(accounts=["nobody"])
    assert not home.bodies("/api/search/random")


# --- download variants --------------------------------------------------------
async def test_original_is_used_and_heic_decodes(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset(
        "h1", original=heic(), originalMimeType="image/heic", originalFileName="IMG_1.HEIC"
    )
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch()
    assert asset.details["quality"] == "original"
    assert Image.open(io.BytesIO(asset.data)).format == "HEIF"
    assert [p for p, _ in fake.downloads()] == ["/api/assets/h1/original"]


@pytest.mark.parametrize(
    ("name", "mime"),
    [("IMG_1.DNG", "image/x-adobe-dng"), ("x.NEF", "image/jpeg"), ("x.bin", "image/x-canon-cr2")],
)
async def test_raw_files_use_the_converted_jpeg_never_the_original(
    tmp_path: Path, name: str, mime: str
) -> None:
    fake = FakeImmich()
    fake.add_asset("r1", original=b"RAW-DATA", originalFileName=name, originalMimeType=mime)
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch()
    assert asset.details["quality"] == "fullsize"
    assert [p for p, _ in fake.downloads()] == ["/api/assets/r1/thumbnail"]
    assert fake.downloads()[0][1] == {"size": "fullsize"}


async def test_fullsize_missing_falls_back_to_preview(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("r1", originalFileName="a.ARW")
    fake.files[("r1", "fullsize")] = 404
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch()
    assert asset.details["quality"] == "preview"
    assert [q.get("size") for _, q in fake.downloads()] == ["fullsize", "preview"]


async def test_oversized_original_and_undecodable_original_fall_back(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("big", exifInfo={"fileSizeInByte": MAX_BYTES + 1, "orientation": "1"})
    fake.add_asset("bad", original=b"definitely not an image")
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        picks = {(await env.fetch(ignore_history=True)).details["asset_id"]: None for _ in range(4)}
    assert set(picks) == {"big", "bad"}
    paths = [p for p, _ in fake.downloads()]
    assert "/api/assets/big/original" not in paths  # skipped on size alone
    assert "/api/assets/bad/original" in paths  # tried, rejected, replaced by fullsize


async def test_every_variant_failing_is_a_provider_error(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("x1")
    for variant in ("original", "fullsize", "preview"):
        fake.files[("x1", variant)] = 500
    env = Env(tmp_path, {"home": fake})
    with mocked(fake), pytest.raises(ProviderUnavailableError, match="Could not download"):
        await env.fetch()


async def test_edited_assets_download_the_edit_and_skip_face_regions(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_person("p1", "Alice")
    fake.add_asset(
        "e1", people=[("p1", "Alice")], isEdited=True, faces=[face("p1", (300, 100, 400, 200))]
    )
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch()
    assert asset.regions == ()
    assert fake.downloads()[0][1] == {"edited": "true"}
    assert not [c for c in fake.calls if c[1] == "/api/faces"]


async def test_face_lookup_failure_only_loses_the_regions(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    fake.faces_error = 500
    with mocked(fake):
        asset = await env.fetch(persons=["Alice"], match_mode="all", ignore_history=True)
    assert asset.regions == () and asset.data


async def test_target_faces_are_prioritized_in_the_returned_regions(tmp_path: Path) -> None:
    fake = family()
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch(persons=["Alice", "Bob"], match_mode="all")
    assert asset.details["asset_id"] == "a3" and asset.details["faces_used"] == 2
    assert len(asset.regions) == 2  # the unassigned background face is ignored
    assert asset.regions[0].area >= asset.regions[1].area


# --- albums listing -----------------------------------------------------------
async def test_albums_across_accounts_with_per_account_errors(tmp_path: Path) -> None:
    home, partner = (
        FakeImmich("http://home.test", "k-home"),
        FakeImmich("http://partner.test", "k-partner"),
    )
    home.add_album("h-al", "Zoo", ["a"])
    partner.add_album("p-al", "Alps", ["a", "b"])
    env = Env(tmp_path, {"home": home, "partner": partner})
    with mocked(home, partner) as router:
        albums, errors = await env.provider.albums()
        assert [(a.name, a.account, a.asset_count) for a in albums] == [
            ("Alps", "partner", 2),
            ("Zoo", "home", 1),
        ]
        assert errors == {}
        router.route(host="partner.test").respond(500)
        albums, errors = await env.provider.albums()
    assert [a.name for a in albums] == ["Zoo"] and "partner" in errors


# --- helpers ------------------------------------------------------------------
def test_title_falls_back_to_the_file_name_and_caps_people() -> None:
    from app.providers.immich.models import AssetDto

    bare = AssetDto(id="x", originalFileName="IMG_9.jpg")
    assert build_title_and_details("home", bare, "original", 0)[0] == "IMG_9.jpg"
    crowd = AssetDto.model_validate(
        {"id": "y", "people": [{"id": str(i), "name": f"P{i}"} for i in range(6)]}
    )
    title, details = build_title_and_details("home", crowd, "preview", 2)
    assert title == "P0, P1, P2, P3" and details["people"] == [f"P{i}" for i in range(6)]
    assert details["taken_at"] is None and details["location"] is None


def test_raw_detection() -> None:
    from app.providers.immich.models import AssetDto

    assert is_raw(AssetDto(id="a", originalFileName="x.CR3"))
    assert is_raw(AssetDto(id="a", originalMimeType="image/x-fuji-raf"))
    assert not is_raw(AssetDto(id="a", originalFileName="x.jpg", originalMimeType="image/jpeg"))
    assert not is_raw(AssetDto(id="a", originalMimeType="image/x-ms-bmp"))
    assert not is_raw(AssetDto(id="a", originalFileName="x.heic", originalMimeType="image/heic"))


# --- date filters and "on this day" --------------------------------------------
def dated(fake: FakeImmich) -> FakeImmich:
    fake.add_asset("old", localDateTime="2021-11-15T09:00:00.000Z")
    fake.add_asset("edge", localDateTime="2022-11-17T20:00:00.000Z")
    fake.add_asset("far", localDateTime="2022-11-25T09:00:00.000Z")
    fake.add_asset("now", localDateTime="2026-11-15T09:00:00.000Z")
    return fake


async def test_taken_after_and_before_filter_the_search(tmp_path: Path) -> None:
    fake = dated(FakeImmich())
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await env.fetch(
            taken_after="2022-01-01T00:00:00Z", taken_before="2022-12-31T00:00:00Z"
        )

    assert asset.details["asset_id"] in {"edge", "far"}
    body = bodies(fake)[0]
    assert body["takenAfter"] == "2022-01-01T00:00:00.000Z"
    assert body["takenBefore"] == "2022-12-31T00:00:00.000Z"


async def test_on_this_day_queries_one_window_per_past_year(tmp_path: Path) -> None:
    fake = dated(FakeImmich())
    clock = lambda: datetime(2026, 11, 15, 12, 0, tzinfo=UTC)  # noqa: E731
    env = Env(tmp_path, {"home": fake})
    env.provider._clock = clock
    with mocked(fake):
        asset = await env.fetch(on_this_day={"window_days": 3, "years_back": 5})

    # 2021 and 2022 hold matches; 2026 (this year) is excluded; 'far' is outside the window.
    assert asset.details["asset_id"] in {"old", "edge"}
    sent = bodies(fake)
    assert len(sent) == 5
    assert sent[0]["takenAfter"] == "2025-11-12T00:00:00.000Z"
    assert sent[0]["takenBefore"] == "2025-11-18T23:59:59.999Z"


async def test_on_this_day_without_matches_raises_no_candidate(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("a1", localDateTime="2024-03-01T09:00:00.000Z")
    env = Env(tmp_path, {"home": fake})
    env.provider._clock = lambda: datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    with mocked(fake), pytest.raises(NoCandidateError):
        await env.fetch(on_this_day={})


def test_on_this_day_rejects_explicit_dates() -> None:
    with pytest.raises(ValueError, match="on_this_day"):
        ImmichDisplayRequest(on_this_day={}, taken_after="2022-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="earlier"):
        ImmichDisplayRequest(
            taken_after="2022-02-01T00:00:00Z", taken_before="2022-01-01T00:00:00Z"
        )
