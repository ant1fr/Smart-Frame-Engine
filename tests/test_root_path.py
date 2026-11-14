"""Reverse proxy sub-path support (``ROOT_PATH``)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import ClientFactory

KEY = "proxy-key"


def test_openapi_advertises_root_path(make_client: ClientFactory) -> None:
    client = make_client(root_path="fraimic/")
    spec = client.get("/openapi.json").json()
    assert spec["servers"] == [{"url": "/fraimic"}]


def test_docs_reference_prefixed_openapi(make_client: ClientFactory) -> None:
    client = make_client(root_path="/fraimic")
    assert "/fraimic/openapi.json" in client.get("/docs").text


def test_routes_work_when_proxy_strips_prefix(make_client: ClientFactory) -> None:
    client = make_client(root_path="/fraimic")
    assert client.get("/api/health").status_code == 200


def test_routes_work_when_proxy_keeps_prefix(make_client: ClientFactory) -> None:
    client = make_client(root_path="/fraimic")
    assert client.get("/fraimic/api/health").status_code == 200


def test_auth_applies_under_prefixed_paths(make_client: ClientFactory) -> None:
    client = make_client(root_path="/fraimic", app_api_key=KEY)
    assert client.get("/fraimic/api/config").status_code == 401
    assert client.get("/fraimic/api/config", headers={"X-API-Key": KEY}).status_code == 200
    assert client.get("/fraimic/api/health").status_code == 200


def test_index_redirect_honors_root_path(make_client: ClientFactory) -> None:
    client = make_client(root_path="/fraimic")
    response = client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/fraimic/docs"


def test_runner_trusts_forwarded_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m app`` enables X-Forwarded-Proto/For handling for the configured proxies."""
    from app import __main__ as entry
    from app.core import settings as settings_module

    captured: dict[str, Any] = {}
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.16.0.0/12")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs))
    settings_module.get_env_settings.cache_clear()
    try:
        entry.main()
    finally:
        settings_module.get_env_settings.cache_clear()

    assert captured["factory"] is True
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "172.16.0.0/12"
    assert captured["port"] == 9000
    assert captured["log_config"] is None
