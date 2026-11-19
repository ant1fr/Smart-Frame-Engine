"""API tests for display upload, current image files, frame control and status."""

from __future__ import annotations

import io
import time
from collections.abc import Callable

import httpx
import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from app.api.routes.display import _clean_filename
from tests.conftest import ClientFactory

BASE = "http://frame.test"
INFO_URL = f"{BASE}/api/info"
IMAGE_URL = f"{BASE}/api/image"
INFO = {
    "battery": {"percent": 76, "charging": False},
    "wifi": {"rssi": -55},
    "firmware_version": "2.0",
}
KEY = "phase2-key"


def jpeg_bytes(size: tuple[int, int] = (640, 480)) -> bytes:
    rng = np.random.default_rng(1)
    buffer = io.BytesIO()
    Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8)).save(
        buffer, format="JPEG"
    )
    return buffer.getvalue()


def upload(
    client: TestClient, push: bool = True, data: bytes | None = None, **headers: str
) -> httpx.Response:
    return client.post(
        "/api/display/upload",
        files={"file": ("../../etc/holiday.jpg", data or jpeg_bytes(), "image/jpeg")},
        data={"push_to_fraimic": "true" if push else "false"},
        headers=headers,
    )


def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


@pytest.fixture
def frame_client(make_client: ClientFactory) -> Callable[..., TestClient]:
    def _make(**overrides: object) -> TestClient:
        overrides.setdefault("fraimic_host", "frame.test")
        return make_client(**overrides)

    return _make


# --- upload + current files ------------------------------------------------------------
@respx.mock
def test_upload_renders_delivers_and_serves_files(frame_client: Callable[..., TestClient]) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    frame_upload = respx.post(IMAGE_URL).respond(200)
    client = frame_client()

    response = upload(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["image"]["source"] == "upload"
    assert body["image"]["title"] == "holiday.jpg"
    assert body["image"]["bin_size"] == 960_000
    assert body["image"]["frame_size"] == [1200, 1600]
    assert body["delivery"]["status"] == "delivered"

    bin_response = client.get("/api/current.bin")
    assert bin_response.status_code == 200
    assert bin_response.headers["content-type"] == "application/octet-stream"
    assert len(bin_response.content) == 960_000
    assert frame_upload.calls.last.request.content == bin_response.content

    jpeg = client.get("/api/current.jpg")
    assert jpeg.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(jpeg.content)).size == (1200, 1600)
    assert jpeg.headers["cache-control"] == "no-cache"
    etag = jpeg.headers["etag"]
    assert client.get("/api/current.jpg", headers={"If-None-Match": etag}).status_code == 304
    assert client.head("/api/current.jpg").status_code == 200

    dithered = client.get("/api/current_dithered.png")
    assert dithered.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(dithered.content)).size == (1200, 1600)


def test_current_files_404_before_first_render(make_client: ClientFactory) -> None:
    client = make_client()
    for path in ("/api/current.jpg", "/api/current.bin", "/api/current_dithered.png"):
        assert client.get(path).status_code == 404


@respx.mock
def test_upload_without_push_does_not_contact_frame(
    frame_client: Callable[..., TestClient],
) -> None:
    frame = respx.route(host="frame.test")
    client = frame_client()
    response = upload(client, push=False)
    assert response.status_code == 200
    assert response.json()["delivery"] is None
    assert not frame.called


@respx.mock
def test_upload_landscape_uses_rotated_frame(frame_client: Callable[..., TestClient]) -> None:
    client = frame_client(orientation="landscape")
    body = upload(client, push=False).json()
    assert body["image"]["frame_size"] == [1600, 1200]
    assert body["image"]["rotation"] == 90
    assert client.get("/api/current.jpg").status_code == 404  # not pushed: preview only
    jpeg = Image.open(io.BytesIO(client.get("/api/preview.jpg").content))
    assert jpeg.size == (1600, 1200)


def test_upload_rejects_non_images(make_client: ClientFactory) -> None:
    response = upload(make_client(), push=False, data=b"definitely not an image")
    assert response.status_code == 415


def test_upload_rejects_large_files(make_client: ClientFactory) -> None:
    client = make_client(max_upload_mb=1)
    payload = b"\xff\xd8" + b"\x00" * (1024 * 1024 + 10)
    response = upload(client, push=False, data=payload)
    assert response.status_code == 413
    assert "1 MB" in response.json()["detail"]


def test_body_limit_middleware_rejects_oversized_requests(make_client: ClientFactory) -> None:
    client = make_client(max_upload_mb=1)
    response = client.post(
        "/api/display/upload",
        content=b"x" * (3 * 1024 * 1024),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413


# --- deepsleep queue + wakeup -----------------------------------------------------------
@respx.mock
def test_deepsleep_queue_then_wakeup(frame_client: Callable[..., TestClient]) -> None:
    info = respx.get(INFO_URL).mock(side_effect=httpx.ConnectTimeout("asleep"))
    frame_upload = respx.post(IMAGE_URL).respond(200)
    client = frame_client(fraimic_power_mode="deepsleep")

    body = upload(client).json()
    assert body["delivery"]["status"] == "queued"
    status = client.get("/api/status").json()
    assert status["delivery"]["pending"] is True
    assert status["delivery"]["pending_upload"]["source"] == "upload"
    assert status["current_image"]["title"] == "holiday.jpg"

    # The worker probes the sleeping frame once right after the submit; wait for that
    # failed probe so the wake-up below is what delivers the image.
    def probed() -> bool:
        pending = client.get("/api/status").json()["delivery"]["pending_upload"]
        return pending is not None and pending["attempts"] >= 1

    wait_for(probed)
    info.mock(return_value=httpx.Response(200, json=INFO))
    woke = client.post("/api/fraimic/wakeup", params={"wait": "true"})
    assert woke.status_code == 200
    assert woke.json()["status"] == "delivered"
    assert frame_upload.call_count == 1

    status = client.get("/api/status").json()
    assert status["delivery"]["pending"] is False
    assert status["delivery"]["last_delivery"]["reason"] == "wakeup"
    assert status["frame"]["telemetry"]["online"] is True
    assert status["frame"]["telemetry"]["info"]["battery_percent"] == 76


@respx.mock
def test_wakeup_default_is_non_blocking(frame_client: Callable[..., TestClient]) -> None:
    info = respx.get(INFO_URL).mock(side_effect=httpx.ConnectTimeout("asleep"))
    frame_upload = respx.post(IMAGE_URL).respond(200)
    client = frame_client(fraimic_power_mode="deepsleep")
    upload(client)
    wait_for(lambda: info.called)  # the worker's first probe found the frame asleep
    wait_for(lambda: client.get("/api/status").json()["delivery"]["pending_upload"]["attempts"])
    info.mock(return_value=httpx.Response(200, json=INFO))

    response = client.post("/api/fraimic/wakeup")
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "pending": True}
    wait_for(lambda: not client.get("/api/status").json()["delivery"]["pending"])
    assert frame_upload.call_count == 1


@respx.mock
def test_wakeup_wait_with_unreachable_frame_returns_202(
    frame_client: Callable[..., TestClient],
) -> None:
    respx.get(INFO_URL).mock(side_effect=httpx.ConnectError("down"))
    client = frame_client(fraimic_power_mode="deepsleep")
    upload(client)
    response = client.post("/api/fraimic/wakeup", params={"wait": "true"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


# --- force push -------------------------------------------------------------------------
@respx.mock
def test_force_push(frame_client: Callable[..., TestClient]) -> None:
    respx.get(INFO_URL).respond(json=INFO)
    frame_upload = respx.post(IMAGE_URL).respond(200)
    client = frame_client(fraimic_power_mode="deepsleep")

    assert client.post("/api/fraimic/push").status_code == 409
    upload(client, push=False)
    assert client.post("/api/fraimic/push").status_code == 409  # only a preview exists
    response = client.post("/api/fraimic/push", params={"from_preview": "true"})
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert response.json()["reason"] == "force"
    assert frame_upload.call_count == 1


@respx.mock
def test_force_push_with_unreachable_frame_queues(frame_client: Callable[..., TestClient]) -> None:
    respx.get(INFO_URL).mock(side_effect=httpx.ConnectError("down"))
    client = frame_client()
    upload(client, push=False)
    response = client.post("/api/fraimic/push", params={"from_preview": "true"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert client.get("/api/status").json()["delivery"]["pending"] is True


@respx.mock
def test_force_push_refuses_image_for_other_panel(frame_client: Callable[..., TestClient]) -> None:
    client = frame_client()
    upload(client, push=False)
    assert client.put("/api/config", json={"fraimic": {"panel": "315"}}).status_code == 200
    response = client.post("/api/fraimic/push", params={"from_preview": "true"})
    assert response.status_code == 409
    assert "panel 133" in response.json()["detail"]


# --- telemetry ----------------------------------------------------------------------------
@respx.mock
def test_frame_info_cached_and_refresh(frame_client: Callable[..., TestClient]) -> None:
    route = respx.get(INFO_URL).respond(json=INFO)
    client = frame_client()

    cached = client.get("/api/fraimic/info").json()
    assert cached["online"] is None

    fresh = client.get("/api/fraimic/info", params={"refresh": "true"})
    assert fresh.status_code == 200
    assert fresh.json()["info"]["rssi"] == -55
    assert fresh.json()["info"]["firmware_version"] == "2.0"

    route.mock(side_effect=httpx.ConnectTimeout("asleep"))
    assert client.get("/api/fraimic/info", params={"refresh": "true"}).status_code == 504
    route.mock(return_value=httpx.Response(500))
    assert client.get("/api/fraimic/info", params={"refresh": "true"}).status_code == 502
    assert client.get("/api/fraimic/info").json()["online"] is True


def test_frame_info_refresh_without_host(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.put("/api/config", json={"fraimic": {"host": ""}}).status_code == 200
    assert client.get("/api/fraimic/info", params={"refresh": "true"}).status_code == 409


# --- auth ---------------------------------------------------------------------------------
def test_new_mutating_routes_require_api_key(make_client: ClientFactory) -> None:
    client = make_client(app_api_key=KEY, public_image_endpoints=False)
    assert upload(client, push=False).status_code == 401
    assert client.post("/api/fraimic/push").status_code == 401
    assert client.post("/api/fraimic/wakeup").status_code == 401
    assert client.get("/api/current_dithered.png").status_code == 401
    assert client.get("/api/fraimic/info").status_code == 200
    assert upload(client, push=False, **{"X-API-Key": KEY}).status_code == 200
    assert client.get("/api/preview.jpg").status_code == 401
    assert client.get("/api/preview.jpg", params={"api_key": KEY}).status_code == 200


def test_status_before_any_image(make_client: ClientFactory) -> None:
    body = make_client().get("/api/status").json()
    assert body["current_image"] is None
    assert body["delivery"] == {"pending": False, "pending_upload": None, "last_delivery": None}
    assert body["frame"]["telemetry"]["online"] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("holiday.jpg", "holiday.jpg"),
        ("../../etc/passwd", "passwd"),
        ("C:/Users/me/IMG_01.HEIC", "IMG_01.HEIC"),
        ("bad" + chr(7) + chr(27) + "name.png", "badname.png"),
        ("", None),
        (None, None),
        ("x" * 300 + ".jpg", "x" * 120),
    ],
)
def test_clean_filename(raw: str | None, expected: str | None) -> None:
    assert _clean_filename(raw) == expected


@respx.mock
def test_upload_accepts_heic(frame_client: Callable[..., TestClient]) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), (30, 90, 200)).save(buffer, format="HEIF", quality=85)
    client = frame_client()
    response = upload(client, push=False, data=buffer.getvalue())
    assert response.status_code == 200, response.text
    assert response.json()["image"]["bin_size"] == 960_000


def test_preview_endpoints_serve_the_latest_render(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.get("/api/preview.jpg").status_code == 404
    assert client.get("/api/preview_dithered.png").status_code == 404
    body = upload(client, push=False).json()
    assert body["delivery"] is None
    assert body["image"]["pushed"] is False
    jpeg = client.get("/api/preview.jpg")
    assert jpeg.status_code == 200 and jpeg.headers["content-type"] == "image/jpeg"
    etag = jpeg.headers["etag"]
    assert client.get("/api/preview.jpg", headers={"If-None-Match": etag}).status_code == 304
    png = client.get("/api/preview_dithered.png")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.head("/api/preview.jpg").status_code == 200
    assert client.get("/api/status").json()["current_image"] is None


def test_status_explains_the_render_decision(make_client: ClientFactory) -> None:
    client = make_client()
    upload(client)
    render = client.get("/api/status").json()["current_image"]["render"]
    assert render["context"] == "photo" and render["automatic"] is True
    assert render["profile"] in {"official", "photo", "soft", "vivid", "natural"}
    assert render["dither"] in {"fs", "fs-serpentine", "stucki", "atkinson-classic"}
    assert len(render["reasons"]) == 2

    assert (
        client.put(
            "/api/config", json={"eink": {"profile": "official", "dither": "atkinson"}}
        ).status_code
        == 200
    )
    upload(client)
    manual = client.get("/api/status").json()["current_image"]
    assert manual["dither"] == "atkinson" and manual["render"]["automatic"] is False
