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
    SmartMixConfig,
    SmartMixEntry,
    SourceRule,
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
    assert (config.eink.profile, config.eink.dither) == ("auto", "auto")
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


def test_scheduler_quiet_hours_must_differ() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        SchedulerConfig(quiet_hours_start="07:00", quiet_hours_end="07:00")


def test_scheduler_retry_and_pending_options() -> None:
    config = SchedulerConfig()
    assert config.skip_if_pending is True
    assert config.retry_minutes == 15
    assert SchedulerConfig(retry_minutes=1440, skip_if_pending=False).retry_minutes == 1440
    for bad in (0, 1441):
        with pytest.raises(ValidationError):
            SchedulerConfig(retry_minutes=bad)


def test_scheduler_pinned_rule() -> None:
    config = SchedulerConfig.model_validate(
        {"source_rule": {"kind": "immich", "params": {"persons": ["Alice"]}}}
    )
    assert config.source_rule is not None
    assert config.source_rule.params == {"persons": ["Alice"]}
    with pytest.raises(ValidationError):
        SchedulerConfig.model_validate({"source_rule": {"kind": "Not A Kind"}})


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


# --- eInk profiles -----------------------------------------------------------------------------
def test_auto_is_the_default_and_stays_unresolved_until_render_time() -> None:
    from app.core.config_models import EinkConfig

    config = EinkConfig(contrast=1.4)
    assert (config.profile, config.dither) == ("auto", "auto")
    assert config.effective() is config


def test_official_profile_leaves_values_untouched_and_floyd_steinberg_is_an_alias() -> None:
    from app.core.config_models import EinkConfig

    config = EinkConfig(profile="official", contrast=1.4, dither="floyd_steinberg")
    assert config.dither == "fs"
    assert config.effective() is config


@pytest.mark.parametrize("profile", ["soft", "vivid", "natural"])
def test_curve_profiles_neutralize_the_plain_factors(profile: str) -> None:
    from app.core.config_models import EinkConfig

    effective = EinkConfig(profile=profile, contrast=1.5).effective()  # type: ignore[arg-type]
    assert effective.profile == profile
    assert (effective.brightness, effective.contrast, effective.saturation) == (1.0, 1.0, 1.0)
    assert effective.edge_filters is False and effective.sharpness == 1.0


def test_photo_profile_overrides_the_five_tuning_fields_but_keeps_dithering() -> None:
    from app.core.config_models import PHOTO_PROFILE, EinkConfig

    config = EinkConfig(profile="photo", contrast=1.5, dither="stucki")
    effective = config.effective()
    for name, value in PHOTO_PROFILE.items():
        assert getattr(effective, name) == value
    assert effective.dither == "stucki"
    assert effective.edge_filters is False and effective.saturation == 0.85
    assert config.contrast == 1.5  # the stored configuration is not modified


def test_effective_profile_can_be_forced_per_source() -> None:
    from app.core.config_models import EinkConfig

    photo = EinkConfig(profile="photo")
    official = photo.effective("official")
    assert official.profile == "official" and official.saturation == 1.2
    assert EinkConfig().effective("photo").saturation == 0.85


def test_source_kind_is_an_open_identifier() -> None:
    assert SourceRule(kind="now_playing").kind == "now_playing"
    for bad in ("", "Immich", "a b", "x" * 65):
        with pytest.raises(ValidationError):
            SourceRule(kind=bad)


def test_smart_mix_defaults_are_inert() -> None:
    config = SmartMixConfig()
    assert config.enabled is False and config.entries == []
    assert config.max_attempts == 3 and config.avoid_consecutive is True


def test_smart_mix_entry_wraps_a_rule() -> None:
    entry = SmartMixEntry.model_validate(
        {"kind": "immich", "params": {"persons": ["Alice"]}, "weight": 2.5, "label": "Alice"}
    )
    assert entry.enabled is True
    assert entry.to_rule() == SourceRule(kind="immich", params={"persons": ["Alice"]})


@pytest.mark.parametrize("weight", [0, -1, 1001])
def test_smart_mix_entry_rejects_bad_weights(weight: float) -> None:
    with pytest.raises(ValidationError):
        SmartMixEntry(kind="immich", weight=weight)


def test_smart_mix_rejects_unknown_fields_and_too_many_entries() -> None:
    with pytest.raises(ValidationError):
        SmartMixEntry.model_validate({"kind": "immich", "bogus": 1})
    with pytest.raises(ValidationError):
        SmartMixConfig(entries=[SmartMixEntry(kind="immich")] * 51)
