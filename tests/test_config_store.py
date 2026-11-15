from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config_models import MASK_PREFIX, RuntimeConfig
from app.core.config_store import ConfigStore, ConfigValidationError, UnknownConfigPathError


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def make_store(path: Path, seed: dict[str, dict[str, object]] | None = None) -> ConfigStore:
    store = ConfigStore(path, seed or {})
    store.load()
    return store


def saved(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


async def test_precedence_default_env_config_and_reset(config_path: Path) -> None:
    store = make_store(config_path, {"fraimic": {"host": "env-frame.lan"}})
    assert store.config.fraimic.host == "env-frame.lan"
    assert store.sources()["fraimic.host"] == "env"
    assert store.sources()["fraimic.panel"] == "default"

    await store.update({"fraimic": {"host": "ui-frame.lan"}})
    assert store.config.fraimic.host == "ui-frame.lan"
    assert store.sources()["fraimic.host"] == "config"
    assert saved(config_path) == {"fraimic": {"host": "ui-frame.lan"}}

    # A restart keeps the saved value over the environment.
    restarted = make_store(config_path, {"fraimic": {"host": "env-frame.lan"}})
    assert restarted.config.fraimic.host == "ui-frame.lan"

    await restarted.reset(["fraimic.host"])
    assert restarted.config.fraimic.host == "env-frame.lan"
    assert restarted.sources()["fraimic.host"] == "env"
    assert saved(config_path) == {}


async def test_overrides_are_sparse_and_canonical(config_path: Path) -> None:
    store = make_store(config_path)
    await store.update({"immich": {"album": " Family ", "persons": ["A", " A "]}})
    assert saved(config_path) == {"immich": {"album": " Family ", "persons": ["A"]}}


async def test_unchanged_values_do_not_freeze_env_fallback(config_path: Path) -> None:
    store = make_store(config_path, {"fraimic": {"host": "env-frame.lan"}})
    full_form = store.public_view()["config"]
    full_form["eink"]["contrast"] = 1.5
    await store.update(full_form)
    assert saved(config_path) == {"eink": {"contrast": 1.5}}
    assert store.sources()["fraimic.host"] == "env"


async def test_masked_secret_round_trip_keeps_value(config_path: Path) -> None:
    store = make_store(config_path, {"tmdb": {"api_key": "env-tmdb-secret-0001"}})
    view = store.public_view()
    assert view["config"]["tmdb"]["api_key"] == MASK_PREFIX + "0001"

    await store.update(view["config"])
    assert store.config.tmdb.api_key.get_secret_value() == "env-tmdb-secret-0001"
    assert not config_path.exists() or saved(config_path) == {}

    await store.update({"tmdb": {"api_key": "new-key"}})
    assert store.config.tmdb.api_key.get_secret_value() == "new-key"
    assert saved(config_path) == {"tmdb": {"api_key": "new-key"}}
    assert "new-key" in store.secret_values()


async def test_invalid_update_is_not_persisted(config_path: Path) -> None:
    store = make_store(config_path)
    await store.update({"eink": {"contrast": 1.4}})
    with pytest.raises(ConfigValidationError) as info:
        await store.update({"eink": {"contrast": 99}, "tmdb": {"api_key": "leak-me"}})
    assert info.value.errors[0]["loc"] == ["eink", "contrast"]
    assert "leak-me" not in json.dumps(info.value.errors)
    assert "99" not in json.dumps(info.value.errors)
    assert store.config.eink.contrast == 1.4
    assert saved(config_path) == {"eink": {"contrast": 1.4}}


@pytest.mark.parametrize(
    "patch",
    [{"nope": {"x": 1}}, {"immich": {"nope": 1}}, {"immich": "not-a-section"}],
)
async def test_unknown_paths_rejected(config_path: Path, patch: dict[str, object]) -> None:
    store = make_store(config_path)
    with pytest.raises(UnknownConfigPathError):
        await store.update(patch)


async def test_reset_section_and_all(config_path: Path) -> None:
    store = make_store(config_path)
    await store.update(
        {"eink": {"contrast": 1.5, "saturation": 1.0}, "tmdb": {"language": "de-DE"}}
    )
    await store.reset(["eink"])
    assert store.overrides == {"tmdb": {"language": "de-DE"}}
    await store.reset()
    assert store.overrides == {}
    assert store.config == RuntimeConfig()
    with pytest.raises(UnknownConfigPathError):
        await store.reset(["eink.nope"])


async def test_reset_that_breaks_validation_is_refused(config_path: Path) -> None:
    store = make_store(config_path)
    await store.update({"scheduler": {"quiet_hours_start": "22:00", "quiet_hours_end": "07:00"}})
    with pytest.raises(ConfigValidationError):
        await store.reset(["scheduler.quiet_hours_start"])
    assert store.config.scheduler.quiet_hours_start == "22:00"


def test_invalid_environment_fails_fast(config_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match=r"fraimic\.panel"):
        ConfigStore(config_path, {"fraimic": {"panel": "999"}})


def test_corrupt_file_is_moved_aside(config_path: Path) -> None:
    config_path.write_text("{not json", encoding="utf-8")
    store = make_store(config_path)
    assert store.config == RuntimeConfig()
    assert not config_path.exists()
    assert len(list(config_path.parent.glob("config.json.corrupt-*"))) == 1


def test_stale_and_invalid_saved_values_are_ignored(config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "legacy": {"x": 1},
                "immich": {"removed_field": True, "album": "Family"},
                "eink": {"contrast": "very high", "saturation": 1.3},
                "scheduler": {"quiet_hours_start": "22:00"},
            }
        ),
        encoding="utf-8",
    )
    store = make_store(config_path)
    assert store.overrides == {"immich": {"album": "Family"}, "eink": {"saturation": 1.3}}
    assert store.config.eink.contrast == 1.2


async def test_listeners_are_notified(config_path: Path) -> None:
    store = make_store(config_path)
    seen: list[float] = []

    async def listener(config: RuntimeConfig) -> None:
        seen.append(config.eink.contrast)

    async def broken(_: RuntimeConfig) -> None:
        raise RuntimeError("boom")

    store.add_listener(broken)
    store.add_listener(listener)
    await store.update({"eink": {"contrast": 1.7}})
    await store.reset()
    assert seen == [1.7, 1.2]


# --- Immich accounts: nested secrets -------------------------------------------------------
ACCOUNTS = {
    "home": {"url": "http://immich.local:2283", "api_key": "home-secret-key-1111"},
    "partner": {"url": "https://photos.example.org", "api_key": "partner-secret-key-2222"},
}


async def test_account_keys_are_masked_but_stored_and_logged_as_secrets(config_path: Path) -> None:
    store = make_store(config_path)
    await store.update({"immich": {"accounts": ACCOUNTS}})

    view = store.public_view()["config"]["immich"]["accounts"]
    assert view["home"]["api_key"] == MASK_PREFIX + "1111"
    assert view["partner"]["url"] == "https://photos.example.org"
    assert "secret-key" not in str(store.public_view())
    assert saved(config_path)["immich"]["accounts"]["home"]["api_key"] == "home-secret-key-1111"
    assert {"home-secret-key-1111", "partner-secret-key-2222"} <= set(store.secret_values())


async def test_masked_account_keys_round_trip_and_survive_removals(config_path: Path) -> None:
    store = make_store(config_path)
    await store.update({"immich": {"accounts": ACCOUNTS}})

    form = store.public_view()["config"]["immich"]
    await store.update({"immich": form})  # the UI sends the masked view back
    assert store.config.immich.accounts["home"].api_key.get_secret_value() == "home-secret-key-1111"

    form["accounts"].pop("partner")  # omitting an account deletes it
    form["accounts"]["home"]["url"] = "http://immich.lan:2283"
    await store.update({"immich": form})
    assert set(store.config.immich.accounts) == {"home"}
    home = store.config.immich.accounts["home"]
    assert home.url == "http://immich.lan:2283"
    assert home.api_key.get_secret_value() == "home-secret-key-1111"

    await store.update({"immich": {"accounts": {"home": {"url": "http://immich.lan:2283"}}}})
    assert store.config.immich.accounts["home"].api_key.get_secret_value() == "home-secret-key-1111"


async def test_new_account_without_a_real_key_is_rejected(config_path: Path) -> None:
    store = make_store(config_path)
    for bad in (
        {"url": "http://immich"},
        {"url": "http://immich", "api_key": MASK_PREFIX + "1111"},
    ):
        with pytest.raises(ConfigValidationError):
            await store.update({"immich": {"accounts": {"new": bad}}})
    assert not config_path.exists()


async def test_env_seed_account_and_config_json_precedence(config_path: Path) -> None:
    seed = {"immich": {"accounts": {"default": ACCOUNTS["home"]}}}
    store = make_store(config_path, seed)
    assert set(store.config.immich.accounts) == {"default"}
    assert store.sources()["immich.accounts"] == "env"

    await store.update({"immich": {"accounts": {"partner": ACCOUNTS["partner"]}}})
    assert set(store.config.immich.accounts) == {"partner"}  # config.json always wins
    assert store.sources()["immich.accounts"] == "config"


def test_legacy_single_account_settings_are_migrated(config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "immich": {
                    "url": "http://immich.local:2283",
                    "api_key": "legacy-secret-key-9999",
                    "album": "Family",
                }
            }
        ),
        encoding="utf-8",
    )
    store = make_store(config_path)
    store.load()
    account = store.config.immich.accounts["default"]
    assert account.url == "http://immich.local:2283"
    assert account.api_key.get_secret_value() == "legacy-secret-key-9999"
    assert store.config.immich.album == "Family"
    assert "legacy-secret-key-9999" in store.secret_values()


def test_incomplete_legacy_settings_are_dropped(config_path: Path) -> None:
    config_path.write_text(
        json.dumps({"immich": {"url": "http://immich.local:2283", "album": "Family"}}),
        encoding="utf-8",
    )
    store = make_store(config_path)
    store.load()
    assert store.config.immich.accounts == {}
    assert store.config.immich.album == "Family"
