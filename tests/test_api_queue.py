from __future__ import annotations

import io

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import ClientFactory

KEY = "queue-key"


def jpeg(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    pixels = np.full((480, 640, 3), color, dtype=np.uint8)
    Image.fromarray(pixels).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload(
    client: TestClient, color: tuple[int, int, int], *, ephemeral: bool, **headers: str
) -> dict[str, object]:
    response = client.post(
        "/api/display/upload",
        files={"file": ("photo.jpg", jpeg(color), "image/jpeg")},
        data={"ephemeral": "true" if ephemeral else "false"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def deepsleep_client(make_client: ClientFactory, **overrides: object) -> TestClient:
    return make_client(fraimic_host="frame.test", fraimic_power_mode="deepsleep", **overrides)


def test_queue_is_empty_at_first(make_client: ClientFactory) -> None:
    body = deepsleep_client(make_client).get("/api/queue").json()
    assert body["power_mode"] == "deepsleep"
    assert body["pending"] is None
    assert body["last_delivery"] is None
    assert body["ephemeral"] == {
        "active": False,
        "source": None,
        "title": None,
        "template": None,
        "expires_at": None,
        "pending": False,
        "has_base": False,
    }


def test_cancel_reverts_an_ephemeral_upload_to_the_previous_image(
    make_client: ClientFactory,
) -> None:
    client = deepsleep_client(make_client)
    base = upload(client, (30, 90, 160), ephemeral=False)
    upload(client, (200, 40, 40), ephemeral=True)

    queue = client.get("/api/queue").json()
    assert queue["ephemeral"]["active"] and queue["ephemeral"]["pending"]
    assert queue["pending"]["ephemeral"] is True
    status = client.get("/api/status").json()
    assert status["ephemeral"]["active"] is True

    outcome = client.delete("/api/queue/ephemeral")
    assert outcome.status_code == 200
    body = outcome.json()
    assert body["cancelled"] and body["dropped_pending"] and body["reverted"]
    assert body["pushed_base"]  # the sleeping frame never received anything yet
    assert body["image"]["sha256"] == base["image"]["sha256"]  # type: ignore[index]

    queue = client.get("/api/queue").json()
    assert not queue["ephemeral"]["active"]
    assert queue["pending"]["sha256"] == base["image"]["sha256"]  # type: ignore[index]
    assert queue["pending"]["ephemeral"] is False
    current = client.get("/api/status").json()["current_image"]
    assert current["sha256"] == base["image"]["sha256"]  # type: ignore[index]


def test_cancel_is_a_harmless_no_op(make_client: ClientFactory) -> None:
    client = deepsleep_client(make_client)
    upload(client, (30, 90, 160), ephemeral=False)
    body = client.delete("/api/queue/ephemeral").json()
    assert body["cancelled"] is False
    assert body["reverted"] is False
    assert client.get("/api/queue").json()["pending"] is not None  # untouched


def test_cancel_requires_the_api_key(make_client: ClientFactory) -> None:
    client = deepsleep_client(make_client, app_api_key=KEY)
    assert client.get("/api/queue").status_code == 200
    assert client.delete("/api/queue/ephemeral").status_code == 401
    response = client.delete("/api/queue/ephemeral", headers={"X-API-Key": KEY})
    assert response.status_code == 200
