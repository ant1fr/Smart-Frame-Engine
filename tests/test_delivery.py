from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.clients.fraimic import FraimicClient, FrameInfo, UploadResult
from app.core.config_models import FraimicConfig
from app.core.config_store import ConfigStore
from app.services.delivery import DeliveryService

BASE = "http://frame.test"
INFO_URL = f"{BASE}/api/info"
IMAGE_URL = f"{BASE}/api/image"
BIN_A = bytes([0x11]) * 960_000
BIN_B = bytes([0x00]) * 960_000
INFO = {"battery": {"percent": 55, "charging": True}, "wifi": {"rssi": -48}}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_config_store(
    tmp_path: Path, power_mode: str = "continuous", **fraimic: Any
) -> ConfigStore:
    seed = {"fraimic": {"host": "frame.test", "power_mode": power_mode, **fraimic}}
    store = ConfigStore(tmp_path / "config.json", seed)
    store.load()
    return store


ServiceFactory = Callable[..., DeliveryService]


@pytest.fixture
async def make_service(tmp_path: Path) -> AsyncIterator[ServiceFactory]:
    services: list[DeliveryService] = []

    def _make(
        power_mode: str = "continuous",
        clock: FakeClock | None = None,
        client_factory: Callable[[FraimicConfig], Any] | None = None,
        **fraimic: Any,
    ) -> DeliveryService:
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        if client_factory is not None:
            kwargs["client_factory"] = client_factory
        service = DeliveryService(
            tmp_path / "queue", make_config_store(tmp_path, power_mode, **fraimic), **kwargs
        )
        service.load()
        services.append(service)
        return service

    yield _make
    for service in services:
        await service.stop()


async def eventually(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def queue_files(tmp_path: Path) -> set[str]:
    queue = tmp_path / "queue"
    return {p.name for p in queue.iterdir()} if queue.exists() else set()


# --- continuous ------------------------------------------------------------------------
@respx.mock
async def test_continuous_submit_delivers_immediately(
    make_service: ServiceFactory, tmp_path: Path
) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("continuous")

    outcome = await service.submit(BIN_A, source="upload", title="a.jpg")

    assert outcome.status == "delivered"
    assert outcome.sha256 == hashlib.sha256(BIN_A).hexdigest()
    assert upload.call_count == 1
    assert upload.calls.last.request.content == BIN_A
    assert service.pending is None
    assert queue_files(tmp_path) == set()
    snapshot = service.snapshot()
    assert snapshot.last_delivery is not None and snapshot.last_delivery.reason == "submit"
    assert snapshot.telemetry.online is True
    assert snapshot.telemetry.info is not None
    assert snapshot.telemetry.info.battery_percent == 55


@respx.mock
async def test_continuous_failure_queues_with_backoff(
    make_service: ServiceFactory, tmp_path: Path
) -> None:
    clock = FakeClock()
    respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).mock(side_effect=httpx.ConnectError("refused"))
    service = make_service("continuous", clock=clock)

    outcome = await service.submit(BIN_A, source="upload")
    assert outcome.status == "queued"
    assert outcome.error and "Cannot connect" in outcome.error
    assert queue_files(tmp_path) == {"pending.bin", "pending.json"}
    assert service.pending is not None and service.pending.attempts == 1
    assert service.next_wait() == pytest.approx(30.0)

    clock.advance(30)
    assert (await service.flush("worker")).status == "queued"
    assert service.pending is not None and service.pending.attempts == 2
    assert service.next_wait() == pytest.approx(60.0)

    upload.mock(return_value=httpx.Response(200))
    clock.advance(60)
    assert (await service.flush("worker")).status == "delivered"
    assert queue_files(tmp_path) == set()


async def test_backoff_is_capped(make_service: ServiceFactory) -> None:
    clock = FakeClock()
    with respx.mock:
        respx.get(INFO_URL).mock(side_effect=httpx.ConnectTimeout("t"))
        service = make_service("continuous", clock=clock)
        await service.submit(BIN_A)
        for _ in range(8):
            await service.flush("worker")
    assert service.next_wait() == pytest.approx(900.0)


@respx.mock
async def test_status_endpoint_errors_do_not_block_upload(make_service: ServiceFactory) -> None:
    respx.get(INFO_URL).respond(404)
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("continuous")
    assert (await service.submit(BIN_A)).status == "delivered"
    assert upload.called


@respx.mock
async def test_unconfigured_host_keeps_image_queued(make_service: ServiceFactory) -> None:
    service = make_service("continuous", host="")
    outcome = await service.submit(BIN_A)
    assert outcome.status == "queued"
    assert outcome.error == "No Fraimic frame host is configured"
    assert service.snapshot().telemetry.online is None


async def test_submit_rejects_wrong_size(make_service: ServiceFactory) -> None:
    with pytest.raises(ValueError, match="960000"):
        await make_service().submit(b"\x11" * 10)


# --- deepsleep ---------------------------------------------------------------------------
@respx.mock
async def test_deepsleep_submit_only_queues(make_service: ServiceFactory, tmp_path: Path) -> None:
    info = respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("deepsleep", poll_interval_s=20)

    outcome = await service.submit(BIN_A, source="upload")

    assert outcome.status == "queued"
    assert not info.called and not upload.called
    assert queue_files(tmp_path) == {"pending.bin", "pending.json"}
    assert (tmp_path / "queue" / "pending.bin").read_bytes() == BIN_A
    assert service.next_wait() == 20.0


@respx.mock
async def test_deepsleep_worker_delivers_once_frame_answers(make_service: ServiceFactory) -> None:
    info = respx.get(INFO_URL).mock(side_effect=httpx.ConnectTimeout("asleep"))
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("deepsleep")
    await service.start()

    await service.submit(BIN_A)
    await eventually(lambda: service.pending is not None and service.pending.attempts >= 1)
    assert not upload.called
    assert service.snapshot().telemetry.online is False

    info.mock(return_value=httpx.Response(200, json=INFO))
    service.wake()  # e.g. the next poll tick
    await eventually(lambda: service.pending is None)
    assert upload.call_count == 1
    assert service.snapshot().telemetry.online is True


@respx.mock
async def test_failed_forced_push_reschedules_an_idle_deepsleep_worker(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the worker kept the sleep it computed *before* a failed forced push.

    An idle deepsleep worker waits "until woken"; after ``POST /api/fraimic/push`` failed
    against a sleeping frame nothing woke it, so the frame stayed unreachable for the
    app even after it woke up.
    """
    info = respx.get(INFO_URL).mock(side_effect=httpx.ConnectTimeout("asleep"))
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("deepsleep")
    monkeypatch.setattr(service, "next_wait", lambda: None if service.pending is None else 0.05)
    await service.start()
    await asyncio.sleep(0.05)  # the worker is now sleeping on its idle timer

    outcome = await service.submit(BIN_A, immediate=True)  # e.g. POST /api/fraimic/push
    assert outcome.status == "queued"

    info.mock(return_value=httpx.Response(200, json=INFO))  # the frame wakes up
    await eventually(lambda: service.pending is None)  # no manual wake()
    assert upload.call_count == 1


@respx.mock
async def test_retry_schedule_does_not_spin(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = respx.get(INFO_URL).mock(side_effect=httpx.ConnectTimeout("asleep"))
    service = make_service("deepsleep")
    monkeypatch.setattr(service, "next_wait", lambda: None if service.pending is None else 0.1)
    await service.start()
    await service.submit(BIN_A, immediate=True)
    await asyncio.sleep(0.5)
    assert 2 <= info.call_count <= 8  # roughly one probe per 0.1 s, never a hot loop


@respx.mock
async def test_wakeup_wait_delivers(make_service: ServiceFactory) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("deepsleep")
    await service.submit(BIN_A)

    outcome = await service.wakeup(wait=True)
    assert outcome is not None and outcome.status == "delivered"
    assert outcome.reason == "wakeup"
    assert upload.call_count == 1


@respx.mock
async def test_wakeup_background_delivers(make_service: ServiceFactory) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("deepsleep")
    await service.submit(BIN_A)

    assert await service.wakeup(wait=False) is None
    await eventually(lambda: service.pending is None)
    assert upload.call_count == 1


@respx.mock
async def test_wakeup_without_pending_refreshes_telemetry(make_service: ServiceFactory) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    service = make_service("deepsleep")
    outcome = await service.wakeup(wait=True)
    assert outcome is not None and outcome.status == "nothing_pending"
    await eventually(lambda: service.telemetry.online is True)


@respx.mock
async def test_concurrent_flushes_upload_once(make_service: ServiceFactory) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    upload = respx.post(IMAGE_URL).respond(200)
    service = make_service("deepsleep")
    await service.submit(BIN_A)

    results = await asyncio.gather(service.flush("wakeup"), service.flush("worker"))

    assert sorted(r.status for r in results) == ["delivered", "nothing_pending"]
    assert upload.call_count == 1


# --- queue semantics ----------------------------------------------------------------------
@respx.mock
async def test_newer_submit_replaces_pending(make_service: ServiceFactory, tmp_path: Path) -> None:
    service = make_service("deepsleep")
    await service.submit(BIN_A, source="first")
    await service.submit(BIN_B, source="second")
    assert service.pending is not None
    assert service.pending.sha256 == hashlib.sha256(BIN_B).hexdigest()
    assert (tmp_path / "queue" / "pending.bin").read_bytes() == BIN_B


class GatedClient(FraimicClient):
    """Fake client whose upload blocks until released."""

    def __init__(self) -> None:
        super().__init__(base_url=BASE)
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.uploads: list[bytes] = []

    async def get_info(self) -> FrameInfo:
        return FrameInfo()

    async def upload_bin(self, data: bytes, *, expected_size: int) -> UploadResult:
        self.started.set()
        await self.release.wait()
        self.uploads.append(data)
        return UploadResult(200, len(data), 0.1)


async def test_image_submitted_during_upload_stays_pending(make_service: ServiceFactory) -> None:
    client = GatedClient()
    service = make_service("deepsleep", client_factory=lambda _: client)
    await service.submit(BIN_A)

    flush = asyncio.create_task(service.flush("wakeup"))
    await client.started.wait()
    await service.submit(BIN_B)  # arrives while BIN_A is being uploaded
    client.release.set()
    outcome = await flush

    assert outcome.status == "delivered"
    assert outcome.sha256 == hashlib.sha256(BIN_A).hexdigest()
    assert service.pending is not None
    assert service.pending.sha256 == hashlib.sha256(BIN_B).hexdigest()

    assert (await service.flush("worker")).status == "delivered"
    assert client.uploads == [BIN_A, BIN_B]
    assert service.pending is None


async def test_pending_survives_restart_and_tampering_is_detected(tmp_path: Path) -> None:
    store = make_config_store(tmp_path, "deepsleep")
    first = DeliveryService(tmp_path / "queue", store)
    first.load()
    await first.submit(BIN_A, source="upload", title="a.jpg")

    restarted = DeliveryService(tmp_path / "queue", store)
    restored = restarted.load()
    assert restored is not None
    assert restored.title == "a.jpg"
    assert restored.sha256 == hashlib.sha256(BIN_A).hexdigest()

    (tmp_path / "queue" / "pending.bin").write_bytes(BIN_B)
    tampered = DeliveryService(tmp_path / "queue", store)
    assert tampered.load() is None
    assert queue_files(tmp_path) == set()


async def test_panel_change_discards_pending(make_service: ServiceFactory) -> None:
    service = make_service("deepsleep")
    await service.start()
    await service.submit(BIN_A)
    await service._config_store.update({"fraimic": {"panel": "315"}})
    assert service.pending is None


# --- telemetry ------------------------------------------------------------------------------
@respx.mock
async def test_refresh_info_updates_telemetry(make_service: ServiceFactory) -> None:
    clock = FakeClock()
    route = respx.get(INFO_URL).respond(json=INFO)
    service = make_service("continuous", clock=clock)

    info = await service.refresh_info()
    assert info.rssi == -48
    assert service.telemetry.online is True
    assert service.telemetry.last_seen == clock.now

    route.mock(side_effect=httpx.ConnectError("gone"))
    clock.advance(10)
    with pytest.raises(Exception, match="Cannot connect"):
        await service.refresh_info()
    assert service.telemetry.online is False
    assert service.telemetry.last_checked == clock.now
    assert service.telemetry.info is not None  # last known values are kept


def test_next_wait_when_idle(make_service: ServiceFactory) -> None:
    assert make_service("continuous").next_wait() == 300.0
    assert make_service("deepsleep").next_wait() is None
