from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config_models import (
    MASK_PREFIX,
    PANEL_SPECS,
    FraimicConfig,
    ImmichAccountConfig,
    ImmichConfig,
    RuntimeConfig,
    SchedulerConfig,
    TmdbConfig,
    dump_config,
    is_masked,
    mask_secret,
    nested_secret_values,
    secret_field_paths,
)


def test_defaults_follow_official_converter() -> None:
    config = RuntimeConfig()
    assert config.fraimic.panel == "133"
    assert config.fraimic.upload_path == "/api/image"
    assert config.fraimic.status_path == "/api/info"
    assert config.eink.dither == "atkinson"
    assert (config.eink.brightness, config.eink.contrast, config.eink.saturation) == (
        1.1,
        1.2,
        1.2,
    )
    assert config.eink.edge_filters is True


@pytest.mark.parametrize(
    ("panel", "orientation", "expected"),
    [
        ("133", "portrait", (1200, 1600)),
        ("133", "landscape", (1600, 1200)),
        ("315", "portrait", (1440, 2560)),
        ("315", "landscape", (2560, 1440)),
    ],
)
def test_frame_size(panel: str, orientation: str, expected: tuple[int, int]) -> None:
    config = RuntimeConfig.model_validate(
        {"fraimic": {"panel": panel}, "display": {"orientation": orientation}}
    )
    assert config.frame_size == expected


def test_panel_specs_match_bin_sizes() -> None:
    for spec in PANEL_SPECS.values():
        assert spec.width * spec.height // 2 <= spec.bin_size
    assert PANEL_SPECS["133"].bin_size == 960_000
    assert PANEL_SPECS["315"].bin_size == 2_304_000


@pytest.mark.parametrize(
    ("host", "base_url"),
    [
        ("fraimic.local", "http://fraimic.local"),
        ("192.168.1.50", "http://192.168.1.50"),
        ("https://frame.example/", "https://frame.example"),
        ("", ""),
    ],
)
def test_fraimic_host_normalization(host: str, base_url: str) -> None:
    assert FraimicConfig(host=host).base_url == base_url


@pytest.mark.parametrize("host", ["ftp://frame", "http://user:pw@frame", "http://frame/?x=1"])
def test_fraimic_host_rejects_bad_values(host: str) -> None:
    with pytest.raises(ValidationError):
        FraimicConfig(host=host)


@pytest.mark.parametrize("path", ["api/info", "http://x/api", "/api?x=1"])
def test_fraimic_paths_must_be_absolute(path: str) -> None:
    with pytest.raises(ValidationError):
        FraimicConfig(status_path=path)


def test_immich_persons_and_account_are_cleaned() -> None:
    config = ImmichConfig(
        persons=[" Alice", "Bob", "Alice", ""],
        always_include=[" Kid ", "Kid"],
        accounts={"home": {"url": " http://immich.local:2283/ ", "api_key": " key-1 "}},  # type: ignore[dict-item]
        person_aliases={" Marie ": {"home": [" Marie ", "", "7f0c"]}},
    )
    assert config.persons == ["Alice", "Bob"]
    assert config.always_include == ["Kid"]
    assert config.accounts["home"].url == "http://immich.local:2283"
    assert config.accounts["home"].api_key.get_secret_value() == "key-1"
    assert config.person_aliases == {"Marie": {"home": ["Marie", "7f0c"]}}


@pytest.mark.parametrize("url", ["", "immich.local", "file:///etc/passwd", "http://u:p@immich"])
def test_immich_account_url_rejects_bad_values(url: str) -> None:
    with pytest.raises(ValidationError):
        ImmichAccountConfig(url=url, api_key=SecretStr("k"))


def test_immich_account_requires_a_key_and_valid_names() -> None:
    with pytest.raises(ValidationError):
        ImmichAccountConfig(url="http://immich", api_key=SecretStr("  "))
    account = {"url": "http://immich", "api_key": "k"}
    with pytest.raises(ValidationError):
        ImmichConfig.model_validate({"accounts": {"bad name!": account}})
    with pytest.raises(ValidationError):
        ImmichConfig.model_validate({"person_aliases": {"Marie": {"bad name": ["x"]}}})
    with pytest.raises(ValidationError):
        ImmichConfig.model_validate({"person_aliases": {" ": {}}})


@pytest.mark.parametrize("language", ["fr-FR", "en-US", "de-DE"])
def test_tmdb_language_accepts_locale_codes(language: str) -> None:
    assert TmdbConfig(language=language).language == language


@pytest.mark.parametrize("language", ["fr", "FR-fr", "english"])
def test_tmdb_language_rejects_other_formats(language: str) -> None:
    with pytest.raises(ValidationError):
        TmdbConfig(language=language)


def test_scheduler_quiet_hours_must_be_paired() -> None:
    with pytest.raises(ValidationError):
        SchedulerConfig(quiet_hours_start="22:00")
    config = SchedulerConfig(quiet_hours_start="22:00", quiet_hours_end="07:30")
    assert (config.quiet_hours_start, config.quiet_hours_end) == ("22:00", "07:30")
    assert SchedulerConfig(quiet_hours_start="", quiet_hours_end=" ").quiet_hours_start is None


@pytest.mark.parametrize("value", ["24:00", "7:30", "12:60"])
def test_scheduler_rejects_bad_times(value: str) -> None:
    with pytest.raises(ValidationError):
        SchedulerConfig(quiet_hours_start=value, quiet_hours_end="06:00")


def test_scheduler_timezone_validated() -> None:
    assert SchedulerConfig(timezone="Europe/Paris").timezone == "Europe/Paris"
    with pytest.raises(ValidationError):
        SchedulerConfig(timezone="Mars/Olympus")


def test_unknown_fields_are_rejected() -> None:
    payload: dict[str, Any] = {"immich": {"nope": 1}}
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(payload)


def test_secret_masking() -> None:
    assert mask_secret("") == ""
    assert mask_secret("short") == MASK_PREFIX
    assert mask_secret("0123456789abcdefWXYZ") == MASK_PREFIX + "WXYZ"
    assert is_masked(mask_secret("anything"))
    assert not is_masked("plain")
    assert not is_masked(None)


def test_dump_config_reveals_or_masks_secrets() -> None:
    config = RuntimeConfig(
        immich=ImmichConfig(
            accounts={
                "home": ImmichAccountConfig(
                    url="http://immich", api_key=SecretStr("immich-secret-key-1234")
                )
            }
        ),
        tmdb=TmdbConfig(api_key=SecretStr("tmdb")),
    )
    assert set(secret_field_paths()) == {("tmdb", "api_key")}
    assert nested_secret_values(config) == ["immich-secret-key-1234"]
    revealed = dump_config(config, reveal_secrets=True)
    masked = dump_config(config, reveal_secrets=False)
    assert revealed["immich"]["accounts"]["home"]["api_key"] == "immich-secret-key-1234"
    assert masked["immich"]["accounts"]["home"]["api_key"] == MASK_PREFIX + "1234"
    assert "immich-secret-key-1234" not in str(masked)
    assert masked["tmdb"]["api_key"] == MASK_PREFIX
    assert RuntimeConfig.model_validate(revealed) == config
