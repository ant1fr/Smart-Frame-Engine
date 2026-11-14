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
    await store.update({"immich": {"url": "http://immich.local:2283/", "persons": ["A", " A "]}})
    assert saved(config_path) == {"immich": {"url": "http://immich.local:2283", "persons": ["A"]}}


async def test_unchanged_values_do_not_freeze_env_fallback(config_path: Path) -> None:
    store = make_store(config_path, {"fraimic": {"host": "env-frame.lan"}})
    full_form = store.public_view()["config"]
    full_form["eink"]["contrast"] = 1.5
    await store.update(full_form)
    assert saved(config_path) == {"eink": {"contrast": 1.5}}
    assert store.sources()["fraimic.host"] == "env"


async def test_masked_secret_round_trip_keeps_value(config_path: Path) -> None:
    store = make_store(config_path, {"immich": {"api_key": "env-immich-secret-0001"}})
    view = store.public_view()
    assert view["config"]["immich"]["api_key"] == MASK_PREFIX + "0001"

    await store.update(view["config"])
    assert store.config.immich.api_key.get_secret_value() == "env-immich-secret-0001"
    assert not config_path.exists() or saved(config_path) == {}

    await store.update({"immich": {"api_key": "new-key"}})
    assert store.config.immich.api_key.get_secret_value() == "new-key"
    assert saved(config_path) == {"immich": {"api_key": "new-key"}}
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
        {"eink": {"contrast": 1.5, "saturation": 1.0}, "tmdb": {"language": "fr-FR"}}
    )
    await store.reset(["eink"])
    assert store.overrides == {"tmdb": {"language": "fr-FR"}}
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
