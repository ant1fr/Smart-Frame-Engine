from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import __version__
from app.core.config_models import MASK_PREFIX
from tests.conftest import ClientFactory


def test_health(make_client: ClientFactory) -> None:
    response = make_client().get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_status_summarizes_without_secrets(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMMICH_URL", "http://immich.local:2283")
    monkeypatch.setenv("IMMICH_API_KEY", "immich-secret-value")
    client = make_client(app_api_key="k")
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_enabled"] is True
    assert body["frame"]["panel"] == "133"
    assert body["frame"]["frame_size"] == [1200, 1600]
    assert body["services"] == {"immich": True, "tmdb": False}
    assert "immich-secret-value" not in response.text


def test_data_dir_is_created(make_client: ClientFactory, data_dir: Path) -> None:
    make_client()
    assert data_dir.is_dir()
    assert (data_dir / "cache").is_dir()
    assert (data_dir / "queue").is_dir()


def test_config_read_update_reset_flow(make_client: ClientFactory, data_dir: Path) -> None:
    client = make_client(tmdb_api_key="tmdb-env-secret-abcd")

    view = client.get("/api/config").json()
    assert view["config"]["tmdb"]["api_key"] == MASK_PREFIX + "abcd"
    assert view["sources"]["tmdb.api_key"] == "env"
    assert view["frame_size"] == [1200, 1600]
    assert "tmdb-env-secret-abcd" not in json.dumps(view)

    updated = client.put(
        "/api/config",
        json={"display": {"orientation": "landscape"}, "tmdb": view["config"]["tmdb"]},
    )
    assert updated.status_code == 200
    assert updated.json()["frame_size"] == [1600, 1200]
    assert updated.json()["sources"]["display.orientation"] == "config"
    saved = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    assert saved == {"display": {"orientation": "landscape"}}

    reset = client.post("/api/config/reset", json={"paths": ["display.orientation"]})
    assert reset.status_code == 200
    assert reset.json()["frame_size"] == [1200, 1600]

    assert client.post("/api/config/reset").status_code == 200


def test_config_validation_error_is_sanitized(make_client: ClientFactory) -> None:
    client = make_client()
    response = client.put(
        "/api/config", json={"tmdb": {"api_key": "do-not-echo", "language": "klingon"}}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "Invalid configuration"
    assert body["errors"][0]["loc"] == ["tmdb", "language"]
    assert "do-not-echo" not in response.text
    assert "klingon" not in response.text


def test_config_unknown_path_returns_400(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.put("/api/config", json={"nope": {"x": 1}}).status_code == 400
    assert client.post("/api/config/reset", json={"paths": ["eink.nope"]}).status_code == 400


def test_request_validation_error_does_not_echo_input(make_client: ClientFactory) -> None:
    client = make_client()
    response = client.put("/api/config", json={"immich": "secret-looking-value"})
    assert response.status_code == 422
    assert "secret-looking-value" not in response.text


def test_config_schema(make_client: ClientFactory) -> None:
    schema = make_client().get("/api/config/schema").json()
    assert set(schema["properties"]) == {
        "immich",
        "tmdb",
        "fraimic",
        "display",
        "eink",
        "scheduler",
    }


def test_openapi_documents_api_key_scheme(make_client: ClientFactory) -> None:
    spec = make_client().get("/openapi.json").json()
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]
    assert spec["paths"]["/api/config"]["put"]["security"] == [{"APIKeyHeader": []}]


def test_index_redirects_to_docs(make_client: ClientFactory) -> None:
    response = make_client().get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
