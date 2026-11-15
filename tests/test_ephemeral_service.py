from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from PIL import Image

from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.services.current_image import CurrentImageStore
from app.services.delivery import DeliveryService
from app.services.display import DisplayService
from app.services.ephemeral import EphemeralService

BASE = "http://frame.test"
INFO_URL = f"{BASE}/api/info"
IMAGE_URL = f"{BASE}/api/image"
INFO = {"battery": {"percent": 80}}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 15, 20, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (240, 320), color).save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class Env:
    clock: FakeClock
    current: CurrentImageStore
    delivery: DeliveryService
    display: DisplayService
    ephemeral: EphemeralService
    holds_cleared: list[bool] = field(default_factory=list)
    refreshes: list[bool] = field(default_factory=list)

    async def show(self, color: tuple[int, int, int], **kwargs: Any) -> str:
        result = await self.display.display_bytes(png(color), source="test", **kwargs)
        return result.image.sha256


EnvFactory = Callable[..., Env]


@pytest.fixture
async def make_env(tmp_path: Path) -> AsyncIterator[EnvFactory]:
    created: list[Env] = []

    def _make(power_mode: str = "deepsleep", *, real_clock: bool = False) -> Env:
        clock = FakeClock()
        config = ConfigStore(
            tmp_path / "config.json",
            {"fraimic": {"host": "frame.test", "power_mode": power_mode}},
        )
        config.load()
        history = HistoryStore(tmp_path / "history.json")
        history.load()
        current = CurrentImageStore(tmp_path / "cache")
        current.load()
        clock_kwargs: dict[str, Any] = {} if real_clock else {"clock": clock}
        delivery = DeliveryService(
            tmp_path / "queue",
            config,
            last_delivery_path=tmp_path / "last_delivery.json",
            **clock_kwargs,
        )
        delivery.load()
        display = DisplayService(config, current, delivery, history)
        env = Env(clock, current, delivery, display, None)  # type: ignore[arg-type]

        async def clear_hold() -> None:
            env.holds_cleared.append(True)

        def refresh() -> bool:
            env.refreshes.append(True)
            return True

        env.ephemeral = EphemeralService(
            current, display, delivery, clear_hold=clear_hold, refresh=refresh, **clock_kwargs
        )
        created.append(env)
        return env

    yield _make
    for env in created:
        await env.ephemeral.stop()
        await env.delivery.stop()


async def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@respx.mock
async def test_expired_queued_cover_is_dropped_and_frame_is_left_alone(
    make_env: EnvFactory,
) -> None:
    """Deep sleep: the song ended before the frame woke up; it still shows the photo."""
    respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).respond(200)
    env = make_env("deepsleep")
    await env.ephemeral.start()
    base = await env.show((30, 90, 160))
    assert (await env.delivery.wakeup(wait=True)).status == "delivered"  # type: ignore[union-attr]

    cover = await env.show(
        (200, 40, 40), ephemeral=True, expires_at=env.clock.now + timedelta(minutes=3)
    )
    assert env.current.meta is not None and env.current.meta.sha256 == cover
    assert env.ephemeral.view().pending

    env.clock.advance(200)
    outcome = await env.delivery.wakeup(wait=True)
    assert outcome is not None and outcome.status == "dropped"
    await eventually(lambda: bool(env.holds_cleared))  # the revert ran to completion
    assert env.current.meta is not None and env.current.meta.sha256 == base

    assert upload.call_count == 1  # only the photo: the frame was never woken for nothing
    assert env.delivery.pending is None
    assert env.holds_cleared == [True]
    assert env.refreshes == []
    assert not env.ephemeral.view().active


@respx.mock
async def test_expiry_of_a_delivered_cover_resends_the_previous_image(
    make_env: EnvFactory,
) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).respond(200)
    env = make_env("continuous")
    base = await env.show((30, 90, 160))
    cover = await env.show(
        (200, 40, 40), ephemeral=True, expires_at=env.clock.now + timedelta(minutes=3)
    )
    assert upload.call_count == 2
    assert env.delivery.last_delivery is not None
    assert env.delivery.last_delivery.sha256 == cover

    assert await env.ephemeral.expire_if_due() is None  # not yet
    env.clock.advance(181)
    revert = await env.ephemeral.expire_if_due()

    assert revert is not None and revert.reverted and revert.pushed_base
    assert env.current.meta is not None and env.current.meta.sha256 == base
    pending = env.delivery.pending
    assert pending is not None and pending.sha256 == base and not pending.ephemeral
    assert await env.ephemeral.expire_if_due() is None  # nothing left to end


async def test_expiry_without_a_previous_image_starts_a_refresh(make_env: EnvFactory) -> None:
    env = make_env("deepsleep")
    await env.show((200, 40, 40), ephemeral=True, expires_at=env.clock.now + timedelta(minutes=1))
    env.clock.advance(61)

    revert = await env.ephemeral.expire_if_due()
    assert revert is not None and not revert.reverted and not revert.has_base
    assert env.refreshes == [True]
    assert env.holds_cleared == [True]
    # Ended once: the timer does not spin on the still-current expired image.
    assert env.ephemeral.next_expiry() is None
    assert await env.ephemeral.expire_if_due() is None
    assert env.refreshes == [True]


async def test_a_newer_display_is_never_undone_by_an_old_expiry(make_env: EnvFactory) -> None:
    env = make_env("deepsleep")
    await env.show((30, 90, 160))
    await env.show((200, 40, 40), ephemeral=True, expires_at=env.clock.now + timedelta(minutes=1))
    old_pending = env.delivery.pending
    assert old_pending is not None
    newest = await env.show((20, 160, 60))  # a scheduled refresh replaced the cover

    env.clock.advance(120)
    assert await env.ephemeral.expire_if_due() is None
    await env.ephemeral.handle_dropped(old_pending, "expired")
    assert env.current.meta is not None and env.current.meta.sha256 == newest
    assert env.holds_cleared == []


async def test_cancel_drops_the_queued_cover_and_reverts(make_env: EnvFactory) -> None:
    env = make_env("deepsleep")
    base = await env.show((30, 90, 160))
    await env.show((200, 40, 40), ephemeral=True, expires_at=env.clock.now + timedelta(hours=2))

    outcome = await env.ephemeral.cancel()

    assert outcome.cancelled and outcome.dropped_pending and outcome.reverted
    assert outcome.image is not None and outcome.image.sha256 == base
    # The frame never received anything: the previous image is queued for it.
    assert outcome.pushed_base
    pending = env.delivery.pending
    assert pending is not None and pending.sha256 == base
    assert env.holds_cleared == [True]

    again = await env.ephemeral.cancel()
    assert not again.cancelled and not again.reverted and not again.dropped_pending


async def test_cancel_without_a_previous_image_refreshes(make_env: EnvFactory) -> None:
    env = make_env("deepsleep")
    await env.show((200, 40, 40), ephemeral=True)
    outcome = await env.ephemeral.cancel()
    assert outcome.cancelled and outcome.dropped_pending
    assert not outcome.reverted and outcome.refreshing
    assert env.refreshes == [True]


async def test_view_describes_the_ephemeral_image(make_env: EnvFactory) -> None:
    env = make_env("deepsleep")
    view = env.ephemeral.view()
    assert not view.active and not view.pending and not view.has_base
    await env.show((30, 90, 160))
    expires = env.clock.now + timedelta(minutes=5)
    await env.display.display_bytes(
        png((200, 40, 40)), source="music", title="So What", ephemeral=True, expires_at=expires
    )
    view = env.ephemeral.view()
    assert view.active and view.pending and view.has_base
    assert view.source == "music" and view.title == "So What" and view.expires_at == expires
    assert env.ephemeral.next_expiry() == expires


async def test_timer_reverts_on_expiry(make_env: EnvFactory) -> None:
    env = make_env("deepsleep", real_clock=True)
    await env.ephemeral.start()
    base = await env.show((30, 90, 160))
    await env.show(
        (200, 40, 40),
        ephemeral=True,
        expires_at=datetime.now(UTC) + timedelta(seconds=0.3),
    )
    await eventually(lambda: bool(env.holds_cleared))
    assert env.holds_cleared == [True]
    assert env.current.meta is not None and env.current.meta.sha256 == base


async def test_expired_cover_restored_from_disk_is_reverted_at_startup(
    make_env: EnvFactory,
) -> None:
    first = make_env("deepsleep", real_clock=True)
    base = await first.show((30, 90, 160))
    await first.show(
        (200, 40, 40), ephemeral=True, expires_at=datetime.now(UTC) + timedelta(seconds=0.1)
    )
    await asyncio.sleep(0.2)  # the app was down while the cover expired

    restarted = make_env("deepsleep", real_clock=True)
    assert restarted.current.meta is not None and restarted.current.meta.ephemeral
    await restarted.ephemeral.start()
    await restarted.delivery.start()
    await eventually(lambda: bool(restarted.holds_cleared))
    assert restarted.current.meta is not None and restarted.current.meta.sha256 == base
    pending = restarted.delivery.pending
    assert pending is not None and pending.sha256 == base
