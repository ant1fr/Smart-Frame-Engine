"""``GET /api/health`` compatibility handshake."""

from __future__ import annotations

from importlib.metadata import version as distribution_version

import pytest

from app import __version__
from app.core.contract import API_VERSION, CAPABILITIES, MIN_API_VERSION, capability_names
from tests.conftest import ClientFactory


def test_health_is_the_compatibility_handshake(make_client: ClientFactory) -> None:
    body = make_client().get("/api/health").json()
    assert body == {
        "status": "ok",
        "version": __version__,
        "api_version": API_VERSION,
        "min_api_version": MIN_API_VERSION,
        "auth": "open",
        "capabilities": capability_names(),
    }


def test_contract_v1_numbers() -> None:
    assert (API_VERSION, MIN_API_VERSION) == (1, 1)
    assert MIN_API_VERSION <= API_VERSION


def test_capabilities_are_sorted_unique_and_deterministic() -> None:
    names = capability_names()
    assert names == sorted(names) and len(names) == len(set(names))
    assert set(names) == set(CAPABILITIES)
    assert {"ambient_context", "family_mode", "smart_mix", "title_card", "force_refresh"} <= set(
        names
    )


def test_version_comes_from_the_package_metadata() -> None:
    assert __version__ == distribution_version("smart-frame-engine")


@pytest.mark.parametrize(
    ("overrides", "mode"),
    [
        ({}, "open"),
        ({"app_api_key": "secret-key"}, "key"),
        ({"admin_password": "pw"}, "password"),
        ({"app_api_key": "secret-key", "admin_password": "pw"}, "password"),
    ],
)
def test_health_needs_no_credentials_in_any_mode(
    make_client: ClientFactory, overrides: dict[str, str], mode: str
) -> None:
    response = make_client(**overrides).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["auth"] == mode
    assert body["capabilities"] == capability_names()
    assert "secret-key" not in response.text


def test_health_is_listed_in_the_openapi_schema_with_the_handshake_fields(
    make_client: ClientFactory,
) -> None:
    schema = make_client().get("/openapi.json").json()
    properties = schema["components"]["schemas"]["HealthResponse"]["properties"]
    assert {"status", "version", "api_version", "min_api_version", "auth", "capabilities"} <= set(
        properties
    )
