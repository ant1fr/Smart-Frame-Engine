from __future__ import annotations

import logging

import pytest

from app.core.logging_config import REDACTED, SecretMaskingFilter, masking_filter
from tests.conftest import ClientFactory


@pytest.fixture
def masking() -> SecretMaskingFilter:
    masking = SecretMaskingFilter()
    masking.set_secrets(["immich-secret", "tmdb-secret", "", "abc"])
    return masking


def _record(msg: str, args: object = None) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)


def test_masks_secrets_in_message(masking: SecretMaskingFilter) -> None:
    record = _record("Calling Immich with immich-secret and tmdb-secret")
    masking.filter(record)
    assert record.getMessage() == f"Calling Immich with {REDACTED} and {REDACTED}"


def test_masks_uvicorn_access_log_arguments(masking: SecretMaskingFilter) -> None:
    record = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.2:5000", "PUT", "/api/config?api_key=supersecret&x=1", "1.1", 200),
    )
    masking.filter(record)
    message = record.getMessage()
    assert "supersecret" not in message
    assert f"api_key={REDACTED}&x=1" in message
    assert message.endswith(" 200")


def test_masks_dict_arguments(masking: SecretMaskingFilter) -> None:
    record = _record("key=%(key)s", ({"key": "tmdb-secret"},))
    masking.filter(record)
    assert record.getMessage() == f"key={REDACTED}"


def test_short_secrets_are_ignored(masking: SecretMaskingFilter) -> None:
    assert masking.mask("abc") == "abc"


def test_secrets_can_be_replaced(masking: SecretMaskingFilter) -> None:
    masking.set_secrets(["new-secret-value"])
    assert masking.mask("immich-secret new-secret-value") == f"immich-secret {REDACTED}"


def test_app_registers_env_and_saved_secrets(make_client: ClientFactory) -> None:
    client = make_client(app_api_key="app-level-key")
    response = client.put(
        "/api/config",
        json={"tmdb": {"api_key": "saved-tmdb-key"}},
        headers={"X-API-Key": "app-level-key"},
    )
    assert response.status_code == 200
    assert masking_filter.mask("app-level-key saved-tmdb-key") == f"{REDACTED} {REDACTED}"
