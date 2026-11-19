"""Shared fixtures: isolated environment, temporary data dir, app/client factories."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.settings import EnvSettings
from app.main import create_app

SettingsFactory = Callable[..., EnvSettings]
ClientFactory = Callable[..., TestClient]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure the developer's shell environment never leaks into tests."""
    for name in EnvSettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("ZEROCONF_ENABLED", "false")  # tests never touch the network


_LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1", "testserver"}


@pytest.fixture(autouse=True)
def _no_external_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast instead of resolving real hostnames (tests must never hit the network)."""
    real_getaddrinfo = socket.getaddrinfo

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in _LOCAL_HOSTS:
            raise socket.gaierror(f"External DNS lookups are disabled in tests ({host!r})")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def make_settings(data_dir: Path) -> SettingsFactory:
    """Build settings from keyword overrides and the (monkeypatched) environment."""

    def _make(**overrides: Any) -> EnvSettings:
        overrides.setdefault("data_dir", data_dir)
        return EnvSettings(_env_file=None, **overrides)

    return _make


@pytest.fixture
def make_client(make_settings: SettingsFactory) -> Iterator[ClientFactory]:
    """Create started TestClients (lifespan executed); they are closed after the test."""
    clients: list[TestClient] = []

    def _make(**overrides: Any) -> TestClient:
        client = TestClient(create_app(make_settings(**overrides)))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
