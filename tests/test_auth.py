from __future__ import annotations

import pytest

from app.core.auth import extract_api_key, keys_match, requires_api_key, route_path
from tests.conftest import ClientFactory

KEY = "s3cret-key"


@pytest.mark.parametrize(
    ("method", "path", "public_images", "expected"),
    [
        ("GET", "/", True, False),
        ("GET", "/docs", True, False),
        ("GET", "/openapi.json", True, False),
        ("GET", "/api/health", True, False),
        ("GET", "/api/status", True, False),
        ("GET", "/api/config", True, True),
        ("GET", "/api/config/", True, True),
        ("GET", "/api/config/schema", True, True),
        ("GET", "/api/configuration", True, False),
        ("POST", "/api/refresh", True, True),
        ("PUT", "/api/config", True, True),
        ("PATCH", "/api/anything", True, True),
        ("DELETE", "/api/anything", True, True),
        ("OPTIONS", "/api/config", True, False),
        ("GET", "/api/current.jpg", True, False),
        ("GET", "/api/current.bin", True, False),
        ("HEAD", "/api/current.jpg", True, False),
        ("GET", "/api/current.jpg", False, True),
        ("GET", "/api/current.bin", False, True),
        ("GET", "/api/current_dithered.png", True, False),
        ("GET", "/api/current_dithered.png", False, True),
        ("GET", "/api/fraimic/info", True, False),
        ("POST", "/api/fraimic/wakeup", True, True),
    ],
)
def test_requires_api_key_rules(
    method: str, path: str, public_images: bool, expected: bool
) -> None:
    assert requires_api_key(method, path, public_image_endpoints=public_images) is expected


@pytest.mark.parametrize(
    ("path", "root_path", "expected"),
    [
        ("/api/config", "", "/api/config"),
        ("/api/config", "/fraimic", "/api/config"),
        ("/fraimic/api/config", "/fraimic", "/api/config"),
        ("/fraimic", "/fraimic", "/"),
        ("/fraimicx/api/config", "/fraimic", "/fraimicx/api/config"),
    ],
)
def test_route_path(path: str, root_path: str, expected: str) -> None:
    assert route_path({"type": "http", "path": path, "root_path": root_path}) == expected


def test_extract_api_key_header_wins_over_query() -> None:
    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b" header-key ")],
        "query_string": b"api_key=query-key",
    }
    assert extract_api_key(scope) == "header-key"
    assert extract_api_key({"type": "http", "headers": [], "query_string": b"a=1&api_key=q"}) == "q"
    assert extract_api_key({"type": "http", "headers": [], "query_string": b""}) is None


def test_keys_match() -> None:
    assert keys_match(KEY, KEY)
    assert not keys_match("wrong", KEY)
    assert not keys_match(None, KEY)
    assert not keys_match("", "")


def test_no_key_configured_everything_open(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.get("/api/config").status_code == 200
    assert client.put("/api/config", json={"eink": {"contrast": 1.3}}).status_code == 200


def test_protected_routes_require_key(make_client: ClientFactory) -> None:
    client = make_client(app_api_key=KEY)

    denied = client.get("/api/config")
    assert denied.status_code == 401
    assert denied.json() == {"detail": "Missing or invalid API key"}
    assert denied.headers["www-authenticate"].startswith("APIKey")

    assert client.get("/api/config", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/config", headers={"X-API-Key": KEY}).status_code == 200
    assert client.get("/api/config", params={"api_key": KEY}).status_code == 200
    assert client.put("/api/config", json={"eink": {"contrast": 1.3}}).status_code == 401
    assert (
        client.put(
            "/api/config", json={"eink": {"contrast": 1.3}}, headers={"X-API-Key": KEY}
        ).status_code
        == 200
    )
    # A POST to an unknown route is still rejected before routing (no route discovery).
    assert client.post("/api/does-not-exist").status_code == 401


def test_public_routes_stay_open_with_key(make_client: ClientFactory) -> None:
    client = make_client(app_api_key=KEY)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


@pytest.mark.parametrize(("public", "expected"), [(True, 404), (False, 401)])
def test_image_endpoints_follow_public_flag(
    make_client: ClientFactory, public: bool, expected: int
) -> None:
    # The image routes arrive in a later milestone: 404 proves the request was let through.
    client = make_client(app_api_key=KEY, public_image_endpoints=public)
    assert client.get("/api/current.jpg").status_code == expected
    assert client.get("/api/current.jpg", params={"api_key": KEY}).status_code == 404


def test_cors_preflight_and_headers_on_401(make_client: ClientFactory) -> None:
    client = make_client(app_api_key=KEY)
    preflight = client.options(
        "/api/config",
        headers={
            "Origin": "https://ha.example",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "X-API-Key",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"

    denied = client.get("/api/config", headers={"Origin": "https://ha.example"})
    assert denied.status_code == 401
    assert denied.headers["access-control-allow-origin"] == "*"


def test_cors_restricted_origins(make_client: ClientFactory) -> None:
    client = make_client(cors_origins="https://ha.example")
    allowed = client.get("/api/health", headers={"Origin": "https://ha.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://ha.example"
    blocked = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in blocked.headers
