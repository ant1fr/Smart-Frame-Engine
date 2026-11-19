"""Admin login, sessions, API key management and the lockdown rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.auth import AuthState, needs_credentials
from app.core.secrets_store import ApiKeyManagedByEnvError, SecretsStore
from app.core.sessions import (
    COOKIE_NAME,
    FREE_ATTEMPTS,
    LoginThrottle,
    issue_token,
    signing_key,
    token_expiry,
)
from tests.conftest import ClientFactory

PASSWORD = "correct horse battery staple"
ENV_KEY = "env-api-key-0123456789"
CSRF = {"X-Requested-With": "SmartFrame"}


def login(client: TestClient, password: str = PASSWORD, user: str = "admin") -> int:
    response = client.post("/api/auth/login", json={"username": user, "password": password})
    return response.status_code


# --- rules --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "mode", "public_images", "expected"),
    [
        ("GET", "/api/status", "open", True, False),
        ("POST", "/api/refresh", "open", True, False),
        ("GET", "/api/status", "key", True, False),  # today's rules
        ("POST", "/api/refresh", "key", True, True),
        ("GET", "/api/auth/api-key", "key", True, True),  # never leak the key
        ("GET", "/api/status", "password", True, True),  # lockdown
        ("GET", "/docs", "password", True, True),
        ("GET", "/", "password", True, True),
        ("GET", "/api/health", "password", True, False),
        ("GET", "/login", "password", True, False),
        ("POST", "/api/auth/login", "password", True, False),
        ("POST", "/api/auth/login", "key", True, False),
        ("GET", "/api/auth/session", "password", True, False),
        ("GET", "/api/current.jpg", "password", True, False),
        ("GET", "/api/current.jpg", "password", False, True),
        ("OPTIONS", "/api/status", "password", True, False),
    ],
)
def test_needs_credentials(
    method: str, path: str, mode: str, public_images: bool, expected: bool
) -> None:
    result = needs_credentials(method, path, mode, public_image_endpoints=public_images)  # type: ignore[arg-type]
    assert result is expected


# --- sessions ------------------------------------------------------------------------------
def test_session_tokens_expire_and_resist_tampering() -> None:
    key = signing_key("secret", "pw")
    token, expires = issue_token(key, 60, now=1000.0)
    assert expires == 1060
    assert token_expiry(token, key, now=1059.0) == 1060
    assert token_expiry(token, key, now=1061.0) is None  # expired
    assert token_expiry(token, signing_key("secret", "other password"), now=1001.0) is None
    assert token_expiry(token, signing_key("other secret", "pw"), now=1001.0) is None
    version, expiry, nonce, signature = token.split(".")
    forged = f"{version}.{int(expiry) + 999}.{nonce}.{signature}"
    assert token_expiry(forged, key, now=1001.0) is None
    for junk in ("", "v1", "v2.1.2.3", "v1.x.y.z", None):
        assert token_expiry(junk, key, now=1001.0) is None


def test_login_throttle_backs_off_exponentially() -> None:
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(FREE_ATTEMPTS - 1):
        throttle.failure("1.2.3.4")
        assert throttle.retry_after("1.2.3.4") == 0
    throttle.failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == pytest.approx(1.0)
    throttle.failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == pytest.approx(2.0)
    assert throttle.retry_after("5.6.7.8") == 0  # per client
    throttle.success("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 0


# --- secrets store -------------------------------------------------------------------------
def test_secrets_store_generates_persists_and_respects_env(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretsStore(path)
    store.load()
    assert store.api_key == "" and store.api_key_source == "none"
    key = store.ensure_api_key()
    assert len(key) >= 32 and store.api_key_source == "generated"
    assert store.ensure_api_key() == key  # idempotent
    session_secret = store.session_secret
    reloaded = SecretsStore(path)
    reloaded.load()
    assert reloaded.api_key == key and reloaded.session_secret == session_secret
    assert reloaded.regenerate_api_key() != key
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"api_key", "session_secret"}

    env = SecretsStore(path, env_api_key=ENV_KEY, env_session_secret="env-secret")
    env.load()
    assert env.api_key == ENV_KEY and env.api_key_source == "env"
    assert env.session_secret == "env-secret"
    with pytest.raises(ApiKeyManagedByEnvError):
        env.regenerate_api_key()


def test_auth_state_modes(tmp_path: Path) -> None:
    open_state = AuthState(SecretsStore(tmp_path / "a.json"))
    open_state.load()
    assert open_state.mode == "open" and open_state.api_key == ""

    key_state = AuthState(SecretsStore(tmp_path / "b.json", env_api_key=ENV_KEY))
    key_state.load()
    assert key_state.mode == "key"
    assert key_state.check_login("anyone", ENV_KEY)  # the key works as a password
    assert not key_state.check_login("admin", "nope")

    locked = AuthState(SecretsStore(tmp_path / "c.json"), password=PASSWORD)
    locked.load()
    assert locked.mode == "password"
    assert locked.api_key  # generated: Home Assistant needs one once the UI is locked
    assert locked.check_login("admin", PASSWORD)
    assert not locked.check_login("root", PASSWORD)
    assert not locked.check_login("admin", "wrong")


# --- open and key modes --------------------------------------------------------------------
def test_open_mode_has_no_login(make_client: ClientFactory) -> None:
    client = make_client()
    session = client.get("/api/auth/session").json()
    assert session["mode"] == "open" and session["authenticated"] is True
    assert login(client) == 409
    assert client.get("/api/status").json()["auth_mode"] == "open"


def test_key_mode_keeps_todays_rules_and_accepts_bearer(make_client: ClientFactory) -> None:
    client = make_client(app_api_key=ENV_KEY)
    assert client.get("/api/status").status_code == 200  # still public
    assert client.post("/api/refresh").status_code == 401
    assert (
        client.get("/api/config", headers={"Authorization": f"Bearer {ENV_KEY}"}).status_code == 200
    )
    assert client.get("/api/config", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert (
        client.get("/api/config", headers={"Authorization": f"Basic {ENV_KEY}"}).status_code == 401
    )
    assert client.get("/api/auth/api-key").status_code == 401
    view = client.get("/api/auth/api-key", headers={"X-API-Key": ENV_KEY}).json()
    assert view == {"api_key": ENV_KEY, "source": "env", "regenerable": False}


def test_key_mode_login_uses_the_api_key(make_client: ClientFactory) -> None:
    client = make_client(app_api_key=ENV_KEY)
    assert login(client, password="wrong") == 401
    assert login(client, password=ENV_KEY) == 200
    assert client.get("/api/config").status_code == 200  # the cookie works


# --- password mode -------------------------------------------------------------------------
def test_password_mode_locks_everything_but_health_login_and_images(
    make_client: ClientFactory,
) -> None:
    client = make_client(admin_password=PASSWORD)
    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/api/auth/session").json() == {
        "mode": "password",
        "authenticated": False,
        "via": None,
        "user": None,
        "expires_at": None,
        "api_key_source": "generated",
    }
    for path in ("/api/status", "/api/queue", "/openapi.json", "/api/smart-mix"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/current.jpg").status_code == 404  # public, nothing rendered yet


def test_browsers_are_redirected_to_the_login_page(make_client: ClientFactory) -> None:
    client = make_client(admin_password=PASSWORD)
    for path in ("/", "/docs"):
        page = client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"] == "/login"
    api = client.get("/api/status", headers={"Accept": "text/html"}, follow_redirects=False)
    assert api.status_code == 401  # APIs never redirect


def test_login_session_cookie_and_logout(make_client: ClientFactory) -> None:
    client = make_client(admin_password=PASSWORD)
    response = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in cookie
    for attribute in ("HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert "secure" not in cookie.lower()  # plain http
    session = client.get("/api/auth/session").json()
    assert session["authenticated"] and session["via"] == "session" and session["user"] == "admin"
    assert client.get("/api/status").status_code == 200

    assert client.post("/api/auth/logout").status_code == 403  # CSRF header missing
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200
    assert client.get("/api/status").status_code == 401


def test_cookie_mutations_need_the_csrf_header_but_keys_do_not(
    make_client: ClientFactory,
) -> None:
    client = make_client(admin_password=PASSWORD)
    key = client.app.state.auth_state.api_key  # type: ignore[attr-defined]
    assert login(client) == 200
    body = {"eink": {"contrast": 1.3}}
    forged = client.put("/api/config", json=body)
    assert forged.status_code == 403 and "X-Requested-With" in forged.json()["detail"]
    assert client.put("/api/config", json=body, headers=CSRF).status_code == 200
    client.cookies.clear()
    assert client.put("/api/config", json=body, headers={"X-API-Key": key}).status_code == 200


def test_wrong_credentials_and_throttling(make_client: ClientFactory) -> None:
    client = make_client(admin_password=PASSWORD)
    assert login(client, user="root") == 401
    for _ in range(FREE_ATTEMPTS - 1):
        assert login(client, password="wrong") == 401
    blocked = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1


def test_tampered_or_foreign_cookies_are_rejected(make_client: ClientFactory) -> None:
    client = make_client(admin_password=PASSWORD)
    client.cookies.set(COOKIE_NAME, "v1.99999999999.nonce.signature")
    assert client.get("/api/status").status_code == 401


def test_changing_the_password_signs_everyone_out(
    make_client: ClientFactory, data_dir: Path
) -> None:
    first = make_client(admin_password=PASSWORD)
    assert login(first) == 200
    token = first.cookies.get(COOKIE_NAME)
    second = make_client(admin_password="a new password", data_dir=data_dir)
    second.cookies.set(COOKIE_NAME, token or "")
    assert second.get("/api/status").status_code == 401


def test_sessions_survive_a_restart(make_client: ClientFactory, data_dir: Path) -> None:
    first = make_client(admin_password=PASSWORD)
    assert login(first) == 200
    token = first.cookies.get(COOKIE_NAME)
    again = make_client(admin_password=PASSWORD, data_dir=data_dir)
    again.cookies.set(COOKIE_NAME, token or "")
    assert again.get("/api/status").status_code == 200


def test_api_key_view_and_regeneration(make_client: ClientFactory) -> None:
    client = make_client(admin_password=PASSWORD)
    assert login(client) == 200
    view = client.get("/api/auth/api-key").json()
    assert view["source"] == "generated" and view["regenerable"] is True
    old = view["api_key"]
    assert client.get("/api/status", headers={"X-API-Key": old}).status_code == 200

    new = client.post("/api/auth/api-key/regenerate", headers=CSRF).json()["api_key"]
    assert new != old
    client.cookies.clear()
    assert client.get("/api/status", headers={"X-API-Key": old}).status_code == 401
    assert client.get("/api/status", headers={"X-API-Key": new}).status_code == 200
    # A key cannot rotate itself: only a signed-in admin can.
    denied = client.post("/api/auth/api-key/regenerate", headers={"X-API-Key": new})
    assert denied.status_code == 403


def test_env_managed_key_cannot_be_regenerated(make_client: ClientFactory) -> None:
    client = make_client(admin_password=PASSWORD, app_api_key=ENV_KEY)
    assert login(client) == 200
    assert client.get("/api/auth/api-key").json()["source"] == "env"
    response = client.post("/api/auth/api-key/regenerate", headers=CSRF)
    assert response.status_code == 409 and "APP_API_KEY" in response.json()["detail"]


def test_secrets_never_reach_the_logs(
    make_client: ClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    client = make_client(admin_password=PASSWORD)
    key = client.app.state.auth_state.api_key  # type: ignore[attr-defined]
    logging.getLogger("app.test").warning("leak %s and %s", PASSWORD, key)
    text = " ".join(
        handler.format(record)
        for handler in logging.getLogger().handlers
        for record in caplog.records
        if record.name == "app.test"
    )
    assert PASSWORD not in text and key not in text


# --- reverse proxy -------------------------------------------------------------------------
def test_sub_path_cookie_redirect_and_https(make_client: ClientFactory) -> None:
    client = make_client(admin_password=PASSWORD, root_path="/fraimic")
    client.base_url = "https://testserver"  # type: ignore[assignment]  # behind a TLS proxy
    page = client.get("/fraimic/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/fraimic/login"
    assert client.get("/fraimic/login").status_code == 200
    response = client.post(
        "/fraimic/api/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    cookie = response.headers["set-cookie"].lower()
    assert "path=/fraimic" in cookie and "secure" in cookie
    assert client.get("/fraimic/api/status").status_code == 200
    # A proxy that strips the prefix forwards the bare path (with the browser's cookie).
    token = client.cookies.get(COOKIE_NAME)
    stripped = client.get("/api/status", headers={"Cookie": f"{COOKIE_NAME}={token}"})
    assert stripped.status_code == 200


def test_login_page_uses_relative_urls_only(make_client: ClientFactory) -> None:
    html = make_client(admin_password=PASSWORD).get("/login").text
    for absolute in ('href="/', 'src="/', 'fetch("/', "fetch('/", 'action="/'):
        assert absolute not in html, absolute
    assert 'fetch("./api/auth/login"' in html
