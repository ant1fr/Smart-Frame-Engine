from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.clients.fraimic import (
    FraimicClient,
    FraimicNotConfiguredError,
    FraimicPayloadError,
    FraimicResponseError,
    FraimicUnreachableError,
    FrameInfo,
)
from app.core.config_models import FraimicConfig

BASE = "http://frame.test"
INFO = {
    "battery": {"percent": 87, "charging": False},
    "wifi": {"rssi": -61, "ip": "192.0.2.10"},
    "firmware_version": "1.4.2",
    "display": {"busy": False},
}


@pytest.fixture
async def client() -> AsyncIterator[FraimicClient]:
    async with FraimicClient(base_url=BASE, timeout_s=10) as frame:
        yield frame


@respx.mock
async def test_get_info_parses_telemetry(client: FraimicClient) -> None:
    respx.get(f"{BASE}/api/info").respond(json=INFO)
    info = await client.get_info()
    assert info.battery_percent == 87.0
    assert info.charging is False
    assert info.rssi == -61
    assert info.ip == "192.0.2.10"
    assert info.firmware_version == "1.4.2"
    assert info.raw["display"] == {"busy": False}


def test_frame_info_is_lenient() -> None:
    info = FrameInfo.from_payload(
        {"battery": "n/a", "wifi": {"rssi": "-70.4"}, "firmware_version": 3}
    )
    assert info.battery_percent is None
    assert info.rssi == -70
    assert info.firmware_version == "3"
    with pytest.raises(FraimicResponseError):
        FrameInfo.from_payload(["not", "an", "object"])


@respx.mock
async def test_get_info_rejects_invalid_json(client: FraimicClient) -> None:
    respx.get(f"{BASE}/api/info").respond(text="<html>")
    with pytest.raises(FraimicResponseError, match="JSON"):
        await client.get_info()


@respx.mock
async def test_upload_sends_raw_octet_stream(client: FraimicClient) -> None:
    route = respx.post(f"{BASE}/api/image").respond(200, text="OK")
    payload = bytes(range(256)) * 3750  # 960,000 bytes
    result = await client.upload_bin(payload, expected_size=960_000)
    assert result.status_code == 200
    assert result.bytes_sent == 960_000
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/octet-stream"
    assert request.headers["content-length"] == "960000"
    assert request.content == payload


@respx.mock
async def test_upload_rejects_wrong_size_without_request(client: FraimicClient) -> None:
    route = respx.post(f"{BASE}/api/image")
    with pytest.raises(FraimicPayloadError, match="960000"):
        await client.upload_bin(b"\x11" * 10, expected_size=960_000)
    assert not route.called


@respx.mock
async def test_http_error_status(client: FraimicClient) -> None:
    respx.post(f"{BASE}/api/image").respond(500, text="decoder busy")
    with pytest.raises(FraimicResponseError, match="HTTP 500: decoder busy") as info:
        await client.upload_bin(b"\x11" * 960_000, expected_size=960_000)
    assert info.value.status_code == 500


@respx.mock
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("disconnected"),
    ],
)
async def test_transport_errors_are_unreachable(client: FraimicClient, error: Exception) -> None:
    respx.get(f"{BASE}/api/info").mock(side_effect=error)
    with pytest.raises(FraimicUnreachableError):
        await client.get_info()


@respx.mock
async def test_mdns_hint_for_local_hosts() -> None:
    respx.get("http://fraimic.local/api/info").mock(side_effect=httpx.ConnectError("no such host"))
    async with FraimicClient.from_config(FraimicConfig(host="fraimic.local")) as frame:
        with pytest.raises(FraimicUnreachableError, match="IP address"):
            await frame.get_info()


@respx.mock
async def test_custom_paths_from_config() -> None:
    route = respx.get("http://192.0.2.5:8080/status").respond(json={})
    config = FraimicConfig(host="192.0.2.5:8080", status_path="/status")
    async with FraimicClient.from_config(config) as frame:
        await frame.get_info()
    assert route.called


async def test_unconfigured_client() -> None:
    async with FraimicClient(base_url="") as frame:
        assert not frame.configured
        with pytest.raises(FraimicNotConfiguredError):
            await frame.get_info()
