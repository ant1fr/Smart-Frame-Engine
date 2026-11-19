"""Instance identity: stable random id, unique default name, mDNS service naming."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.core.identity import DEFAULT_NAME, load_identity
from app.services.discovery import ZeroconfAdvertiser
from tests.conftest import ClientFactory


def test_the_id_is_generated_once_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "instance.json"
    first = load_identity(path)
    assert str(uuid.UUID(first.instance_id)) == first.instance_id
    assert json.loads(path.read_text(encoding="utf-8")) == {"instance_id": first.instance_id}
    assert load_identity(path) == first  # a restart keeps the identity


def test_default_name_is_unique_per_instance(tmp_path: Path) -> None:
    a = load_identity(tmp_path / "a.json")
    b = load_identity(tmp_path / "b.json")
    assert a.instance_id != b.instance_id
    assert a.instance_name != b.instance_name
    assert a.instance_name == f"{DEFAULT_NAME} {a.instance_id[:6]}"


def test_name_override_is_trimmed_and_capped(tmp_path: Path) -> None:
    named = load_identity(tmp_path / "i.json", "  Living   room  ")
    assert named.instance_name == "Living room"
    assert len(load_identity(tmp_path / "j.json", "x" * 200).instance_name) == 60


def test_a_corrupt_id_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "instance.json"
    path.write_text(json.dumps({"instance_id": "not-a-uuid"}), encoding="utf-8")
    identity = load_identity(path)
    assert uuid.UUID(identity.instance_id)
    assert json.loads(path.read_text(encoding="utf-8"))["instance_id"] == identity.instance_id


def test_health_reports_the_persisted_identity(make_client: ClientFactory, data_dir: Path) -> None:
    body = make_client().get("/api/health").json()
    stored = json.loads((data_dir / "instance.json").read_text(encoding="utf-8"))
    assert body["instance_id"] == stored["instance_id"]
    assert body["instance_name"].startswith(DEFAULT_NAME)


def test_health_uses_the_configured_name(make_client: ClientFactory) -> None:
    body = make_client(instance_name="Hallway frame").get("/api/health").json()
    assert body["instance_name"] == "Hallway frame"


class RecordingZeroconf:
    def __init__(self) -> None:
        self.registered: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []

    async def async_register_service(self, info: Any, **kwargs: Any) -> None:
        self.registered.append(info)
        self.kwargs.append(kwargs)

    async def async_unregister_service(self, info: Any) -> None:
        self.registered.remove(info)

    async def async_close(self) -> None:
        return None


async def test_two_engines_advertise_distinct_service_names() -> None:
    zeroconf = RecordingZeroconf()
    advertisers = [
        ZeroconfAdvertiser(
            port=8080,
            version="1",
            root_path="",
            auth="open",
            instance=f"Smart Frame Engine {suffix}",
            instance_id=f"id-{suffix}",
            zeroconf_factory=lambda: zeroconf,
            addresses=lambda: ["192.0.2.10"],
        )
        for suffix in ("aaaaaa", "bbbbbb")
    ]
    for advertiser in advertisers:
        assert await advertiser.start() is True
    names = {info.name for info in zeroconf.registered}
    assert len(names) == 2
    assert {info.decoded_properties["instance_id"] for info in zeroconf.registered} == {
        "id-aaaaaa",
        "id-bbbbbb",
    }
    assert all(kwargs == {"allow_name_change": True} for kwargs in zeroconf.kwargs)
