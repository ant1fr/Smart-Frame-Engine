from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import EnvSettings
from tests.conftest import SettingsFactory


def test_defaults(make_settings: SettingsFactory, data_dir: Path) -> None:
    settings = make_settings()
    assert settings.data_dir == data_dir
    assert settings.port == 8000
    assert settings.auth_enabled is False
    assert settings.public_image_endpoints is True
    assert settings.root_path == ""
    assert settings.cors_origin_list == ["*"]
    assert settings.max_upload_bytes == 20 * 1024 * 1024
    assert settings.config_path == data_dir / "config.json"
    assert settings.history_path == data_dir / "history.json"
    assert settings.cache_dir == data_dir / "cache"
    assert settings.runtime_seed() == {}
    assert settings.secret_values() == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("/", ""), ("fraimic", "/fraimic"), ("/fraimic/", "/fraimic"), (" /a/b ", "/a/b")],
)
def test_root_path_normalization(make_settings: SettingsFactory, raw: str, expected: str) -> None:
    assert make_settings(root_path=raw).root_path == expected


def test_reads_environment(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("APP_API_KEY", "  topsecret  ")
    monkeypatch.setenv("PUBLIC_IMAGE_ENDPOINTS", "false")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example,")
    monkeypatch.setenv("URL_ALLOWLIST", "10.0.0.0/8, immich.lan")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("IMMICH_URL", "http://immich.local:2283")
    monkeypatch.setenv("IMMICH_API_KEY", "immich-key")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("FRAIMIC_PANEL", "315")
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("REFRESH_INTERVAL_HOURS", "3")
    monkeypatch.setenv("QUIET_HOURS_START", " 22:30 ")
    monkeypatch.setenv("QUIET_HOURS_END", "07:00")
    monkeypatch.setenv("TZ", "Europe/Paris")

    settings = EnvSettings(_env_file=None)

    assert settings.api_key == "topsecret"
    assert settings.auth_enabled is True
    assert settings.public_image_endpoints is False
    assert settings.cors_origin_list == ["https://a.example", "https://b.example"]
    assert settings.url_allowlist_entries == ["10.0.0.0/8", "immich.lan"]
    assert settings.log_level == "DEBUG"
    assert settings.tmdb_api_key is None
    assert settings.runtime_seed() == {
        "immich": {
            "accounts": {"default": {"url": "http://immich.local:2283", "api_key": "immich-key"}}
        },
        "fraimic": {"panel": "315"},
        "scheduler": {
            "enabled": True,
            "interval_hours": 3.0,
            "quiet_hours_start": "22:30",
            "quiet_hours_end": "07:00",
            "timezone": "Europe/Paris",
        },
    }
    assert sorted(settings.secret_values()) == ["immich-key", "topsecret"]


def test_reads_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_API_KEY=from-file\nFRAIMIC_HOST=frame.lan\n", encoding="utf-8")
    settings = EnvSettings(_env_file=env_file, data_dir=tmp_path)
    assert settings.api_key == "from-file"
    assert settings.runtime_seed() == {"fraimic": {"host": "frame.lan"}}


def test_invalid_port_rejected(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError):
        make_settings(port=70000)


def test_settings_are_frozen(make_settings: SettingsFactory) -> None:
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.port = 1234


def test_blank_quiet_hours_are_unset(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("QUIET_HOURS_START", "")
    monkeypatch.setenv("QUIET_HOURS_END", "  ")
    settings = EnvSettings(_env_file=None, data_dir=data_dir)
    assert settings.quiet_hours_start is None and settings.quiet_hours_end is None
    assert "scheduler" not in settings.runtime_seed()
