"""Quality gate and burst cooldown inside the Immich provider (fake Immich, no network)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.providers.base import NoCandidateError
from app.providers.immich.models import ImmichDisplayRequest
from tests.immich_fake import FakeImmich
from tests.test_immich_client import mocked
from tests.test_immich_provider import Env

GATE_ON: dict[str, Any] = {"enabled": True}


def encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def good_photo(seed: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    base = rng.integers(20, 235, (24, 32, 3), dtype=np.uint8)
    return encode(Image.fromarray(base).resize((250, 188), Image.Resampling.BICUBIC))


def ui_screen(size: tuple[int, int] = (140, 250)) -> bytes:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for top in range(20, size[1] - 20, 40):
        draw.rectangle((10, top, size[0] - 10, top + 14), fill=(225, 228, 235))
    return encode(image)


def black_frame() -> bytes:
    return encode(Image.new("RGB", (250, 188), "black"))


def camera_exif() -> dict[str, Any]:
    return {"make": "Google", "model": "Pixel", "orientation": "1", "fileSizeInByte": 50_000}


def add_good(fake: FakeImmich, asset_id: str, seed: int = 1, **overrides: Any) -> None:
    """A normal photo: camera EXIF, full-size dimensions, busy thumbnail."""
    overrides.setdefault("width", 4000)
    overrides.setdefault("height", 3000)
    fake.add_asset(asset_id, thumbnail=good_photo(seed), exifInfo=camera_exif(), **overrides)


def thumbnail_requests(fake: FakeImmich) -> list[str]:
    return [p for p, q in fake.downloads() if q.get("size") == "thumbnail"]


def originals(fake: FakeImmich) -> list[str]:
    return [p for p, _ in fake.downloads() if p.endswith("/original")]


async def fetch(env: Env, **request: Any) -> Any:
    return await env.fetch(**request)


# --- the gate -----------------------------------------------------------------
async def test_first_good_photo_is_taken_after_a_single_thumbnail_check(tmp_path: Path) -> None:
    fake = FakeImmich()
    add_good(fake, "g1")
    add_good(fake, "g2", 2)
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env)
    assert asset.details["suitability"] == {"level": 0, "checked": 1, "rejected": []}
    assert len(thumbnail_requests(fake)) == 1  # lazy: no wasted checks
    assert len(originals(fake)) == 1  # the full-size file is downloaded only for the winner


async def test_a_screenshot_is_skipped_for_the_next_candidate(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("shot", thumbnail=ui_screen(), width=1080, height=2400)
    add_good(fake, "good")
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env)
    assert asset.details["asset_id"] == "good"
    # A white UI is both a screenshot and (pale, mostly white) a document.
    suitability = {"level": 0, "checked": 2, "rejected": ["screenshot+document"]}
    assert asset.details["suitability"] == suitability
    assert originals(fake) == ["/api/assets/good/original"]  # the junk was never downloaded


async def test_rejected_reasons_are_reported_and_junk_is_never_downloaded(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset(
        "black", thumbnail=black_frame(), exifInfo=camera_exif(), width=4000, height=3000
    )
    add_good(fake, "tiny", width=300, height=200)
    add_good(fake, "good", 3)
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env)
    assert asset.details["asset_id"] == "good"
    rejected = asset.details["suitability"]["rejected"]  # type: ignore[index]
    assert rejected[0] == "blank" and asset.details["suitability"]["level"] == 0  # type: ignore[index]


async def test_the_gate_can_be_switched_off_in_the_configuration(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("shot", thumbnail=ui_screen(), width=1080, height=2400)
    env = Env(tmp_path, {"home": fake}, quality={"enabled": False})
    with mocked(fake):
        asset = await fetch(env)
    assert asset.details["asset_id"] == "shot" and "suitability" not in asset.details
    assert not thumbnail_requests(fake)


async def test_album_mode_skips_the_gate_unless_asked(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("shot", thumbnail=ui_screen(), width=1080, height=2400)
    fake.add_album("al1", "Receipts", ["shot"])
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        picked = await fetch(env, mode="album", album_name="Receipts")
        assert picked.details["asset_id"] == "shot" and "suitability" not in picked.details
        assert not thumbnail_requests(fake)

        # Forcing the gate on in album mode checks the photo (and relaxes, never fails).
        forced = await fetch(env, mode="album", album_name="Receipts", skip_quality_gate=False)
        assert forced.details["suitability"]["level"] == 3  # type: ignore[index]
        assert len(thumbnail_requests(fake)) == 1


async def test_skip_flag_bypasses_the_gate_in_other_modes(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("shot", thumbnail=ui_screen(), width=1080, height=2400)
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env, skip_quality_gate=True)
    assert "suitability" not in asset.details and not thumbnail_requests(fake)


def test_skip_flag_defaults_to_automatic() -> None:
    assert ImmichDisplayRequest().skip_quality_gate is None
    assert ImmichDisplayRequest(skip_quality_gate=False).skip_quality_gate is False


# --- progressive relaxation ---------------------------------------------------
async def test_relaxation_level_one_accepts_a_small_but_real_photo(tmp_path: Path) -> None:
    fake = FakeImmich()
    add_good(fake, "small", width=400, height=300)
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env)
    assert asset.details["suitability"] == {"level": 1, "checked": 1, "rejected": ["tiny"]}


async def test_all_junk_still_returns_a_photo_at_a_relaxed_level(tmp_path: Path) -> None:
    fake = FakeImmich()
    for index in range(3):
        fake.add_asset(f"s{index}", thumbnail=ui_screen((140, 250)), width=400, height=300)
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env)  # never a 404: tiny + screenshot rules relax away
    suitability = asset.details["suitability"]
    assert suitability["level"] == 3 and suitability["checked"] == 3  # type: ignore[index]
    assert suitability["rejected"] == ["tiny+screenshot+document"] * 3  # type: ignore[index]


async def test_even_all_blank_candidates_end_with_a_result(tmp_path: Path) -> None:
    fake = FakeImmich()
    for name in ("b1", "b2"):
        fake.add_asset(
            name, thumbnail=black_frame(), exifInfo=camera_exif(), width=4000, height=3000
        )
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env)
    assert asset.details["suitability"]["level"] == 4  # type: ignore[index]


async def test_candidates_checked_caps_the_thumbnail_downloads(tmp_path: Path) -> None:
    fake = FakeImmich()
    for index in range(6):
        fake.add_asset(f"j{index}", thumbnail=ui_screen(), width=1080, height=2400)
    add_good(fake, "good")
    env = Env(tmp_path, {"home": fake}, quality={"enabled": True, "candidates_checked": 3})
    with mocked(fake):
        asset = await fetch(env)
    assert len(thumbnail_requests(fake)) == 3
    assert asset.details["suitability"]["checked"] == 3  # type: ignore[index]


async def test_configured_thresholds_are_honoured(tmp_path: Path) -> None:
    fake = FakeImmich()
    add_good(fake, "p1", width=700, height=700)
    strict = Env(tmp_path, {"home": fake}, quality={"enabled": True, "min_dimension": 800})
    with mocked(fake):
        asset = await fetch(strict)
    assert asset.details["suitability"]["rejected"] == ["tiny"]  # type: ignore[index]


# --- the gate never blocks on its own failures --------------------------------
@pytest.mark.parametrize("thumbnail", [500, b"not an image at all", b""])
async def test_unreadable_thumbnails_fail_open(tmp_path: Path, thumbnail: bytes | int) -> None:
    fake = FakeImmich()
    fake.add_asset("x1", thumbnail=thumbnail, width=1080, height=2400)
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        asset = await fetch(env)
    assert asset.details["asset_id"] == "x1"
    assert asset.details["suitability"]["unchecked"] is True  # type: ignore[index]


async def test_missing_thumbnail_endpoint_fails_open(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("x1")  # no thumbnail registered: the fake answers 404
    env = Env(tmp_path, {"home": fake}, quality=GATE_ON)
    with mocked(fake):
        assert (await fetch(env)).details["asset_id"] == "x1"


# --- burst cooldown -----------------------------------------------------------
SHOT = datetime(2024, 7, 14, 10, tzinfo=UTC)


def moment(hour: int, day: int = 14) -> str:
    return f"2024-07-{day:02d}T{hour:02d}:00:00.000Z"


async def burst_env(tmp_path: Path, **immich: Any) -> tuple[Env, FakeImmich]:
    fake = FakeImmich()
    fake.add_asset("a1", localDateTime=moment(10))
    fake.add_asset("a2", localDateTime=moment(11))  # 1 h after a1: same moment
    fake.add_asset("a3", localDateTime=moment(14))  # 4 h after
    fake.add_asset("a4", localDateTime=moment(10, day=20))  # another day
    env = Env(tmp_path, {"home": fake}, **immich)
    # a1 was shown a moment ago, with its capture time recorded.
    await env.history.record(
        "sha1:sum-a1", "immich", taken_at=datetime(2024, 7, 14, 10, tzinfo=UTC)
    )
    return env, fake


async def test_photos_taken_close_to_a_shown_one_are_skipped(tmp_path: Path) -> None:
    env, fake = await burst_env(tmp_path)
    with mocked(fake):
        picked = {(await fetch(env)).details["asset_id"] for _ in range(3)}
    assert picked == {"a3", "a4"}  # a1 (shown) and a2 (same moment) are out


async def test_burst_window_is_configurable_and_can_be_disabled(tmp_path: Path) -> None:
    env, fake = await burst_env(tmp_path, burst_cooldown_hours=6)
    with mocked(fake):
        assert {(await fetch(env)).details["asset_id"] for _ in range(2)} == {"a4"}
    env, fake = await burst_env(tmp_path / "off", burst_cooldown_hours=0)
    with mocked(fake):
        picked = {(await fetch(env)).details["asset_id"] for _ in range(3)}
    assert picked == {"a2", "a3", "a4"}  # only the shown photo itself stays excluded


async def test_ignore_history_also_ignores_the_burst_cooldown(tmp_path: Path) -> None:
    env, fake = await burst_env(tmp_path)
    with mocked(fake):
        picked = {(await fetch(env, ignore_history=True)).details["asset_id"] for _ in range(4)}
    assert picked == {"a1", "a2", "a3", "a4"}


async def test_all_candidates_in_a_burst_explains_why(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("a1", localDateTime=moment(10))
    fake.add_asset("a2", localDateTime=moment(11))
    env = Env(tmp_path, {"home": fake})
    await env.history.record(
        "sha1:sum-a1", "immich", taken_at=datetime(2024, 7, 14, 10, tzinfo=UTC)
    )
    with mocked(fake), pytest.raises(NoCandidateError, match=r"taken within 2 h"):
        await fetch(env)


async def test_burst_memory_follows_anti_repeat_days(tmp_path: Path) -> None:
    env, fake = await burst_env(tmp_path, anti_repeat_days=0)
    with mocked(fake):
        picked = {(await fetch(env)).details["asset_id"] for _ in range(4)}
    assert "a2" in picked  # history memory off: neither the repeat nor the burst rule applies


async def test_the_fetched_asset_carries_its_capture_time(tmp_path: Path) -> None:
    fake = FakeImmich()
    fake.add_asset("a1", localDateTime=moment(10))
    env = Env(tmp_path, {"home": fake})
    with mocked(fake):
        asset = await fetch(env)
    assert asset.taken_at == datetime(2024, 7, 14, 10, tzinfo=UTC)
