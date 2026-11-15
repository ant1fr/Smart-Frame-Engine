from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from app.core.config_models import RuntimeConfig
from app.processor.pipeline import RenderOptions, RenderResult, render
from app.services.current_image import CurrentImageStore


@pytest.fixture(scope="module")
def result() -> RenderResult:
    options = RenderOptions.from_config(RuntimeConfig())
    return render(Image.new("RGB", (300, 400), (40, 90, 160)), options)


def test_save_writes_all_files_and_reloads(tmp_path: Path, result: RenderResult) -> None:
    store = CurrentImageStore(tmp_path)
    assert store.load() is None

    meta = store.save(result, source="upload", title="beach.jpg", details={"size": 123})
    assert meta.sha256 == result.sha256
    assert meta.bin_size == 960_000
    assert meta.frame_size == (1200, 1600)
    assert store.bin_path.read_bytes() == result.bin
    assert store.jpeg_path.read_bytes()[:2] == b"\xff\xd8"
    assert store.dithered_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert json.loads(store.meta_path.read_text(encoding="utf-8"))["title"] == "beach.jpg"

    reloaded = CurrentImageStore(tmp_path)
    assert reloaded.load() == meta
    loaded = reloaded.read_bin()
    assert loaded is not None
    assert loaded[0] == result.bin
    assert loaded[1].source == "upload"


def test_mismatched_bin_is_ignored(tmp_path: Path, result: RenderResult) -> None:
    store = CurrentImageStore(tmp_path)
    store.save(result, source="upload")
    store.bin_path.write_bytes(b"\x11" * 960_000)  # e.g. interrupted overwrite
    assert store.read_bin() is None
    assert CurrentImageStore(tmp_path).load() is None


def test_missing_or_invalid_meta(tmp_path: Path, result: RenderResult) -> None:
    store = CurrentImageStore(tmp_path)
    store.save(result, source="upload")
    store.bin_path.unlink()
    assert CurrentImageStore(tmp_path).load() is None

    store.meta_path.write_text('{"source": 1}', encoding="utf-8")
    assert CurrentImageStore(tmp_path).load() is None


# --- slots ---------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def other() -> RenderResult:
    options = RenderOptions.from_config(RuntimeConfig())
    return render(Image.new("RGB", (300, 400), (200, 40, 40)), options)


def slot_shas(store: CurrentImageStore) -> dict[str, str | None]:
    return {
        slot: (meta.sha256 if (meta := store.slot_meta(slot)) else None)
        for slot in ("current", "base", "preview")
    }


@pytest.mark.parametrize(
    ("push", "ephemeral", "slots"),
    [
        (True, False, {"current", "base", "preview"}),
        (True, True, {"current", "preview"}),
        (False, False, {"preview"}),
        (False, True, {"preview"}),
    ],
)
def test_slot_policy(
    tmp_path: Path, result: RenderResult, push: bool, ephemeral: bool, slots: set[str]
) -> None:
    store = CurrentImageStore(tmp_path)
    store.save(result, source="x", push=push, ephemeral=ephemeral)
    written = {slot for slot, sha in slot_shas(store).items() if sha == result.sha256}
    assert written == slots
    for slot in ("current", "base", "preview"):
        assert store.paths(slot).meta.exists() == (slot in slots)  # type: ignore[arg-type]


def test_preview_render_leaves_the_current_image_alone(
    tmp_path: Path, result: RenderResult, other: RenderResult
) -> None:
    store = CurrentImageStore(tmp_path)
    store.save(result, source="immich")
    store.save(other, source="tmdb", push=False)
    assert store.meta is not None and store.meta.sha256 == result.sha256
    assert store.preview_meta is not None and store.preview_meta.sha256 == other.sha256
    assert store.preview_meta.pushed is False


def test_restore_base_brings_back_the_previous_image(
    tmp_path: Path, result: RenderResult, other: RenderResult
) -> None:
    store = CurrentImageStore(tmp_path)
    assert store.restore_base() is None
    store.save(result, source="immich", title="Beach")
    store.save(other, source="music", ephemeral=True, content_key="song")
    assert store.meta is not None and store.meta.ephemeral
    assert store.base_meta is not None and store.base_meta.sha256 == result.sha256

    restored = store.restore_base()
    assert restored is not None and restored.title == "Beach"
    assert store.meta == restored
    assert store.bin_path.read_bytes() == result.bin
    reloaded = CurrentImageStore(tmp_path)
    assert reloaded.load() == restored


def test_restore_base_refuses_a_damaged_base(tmp_path: Path, result: RenderResult) -> None:
    store = CurrentImageStore(tmp_path)
    store.save(result, source="immich")
    store.paths("base").bin.write_bytes(b"\x00" * 960_000)
    assert store.restore_base() is None
    assert store.base_meta is None


def test_legacy_current_image_seeds_the_base_slot(tmp_path: Path, result: RenderResult) -> None:
    store = CurrentImageStore(tmp_path)
    store.save(result, source="upload")
    for slot in ("base", "preview"):
        for path in store.paths(slot).all():  # type: ignore[arg-type]
            path.unlink()
    reloaded = CurrentImageStore(tmp_path)
    reloaded.load()
    assert reloaded.base_meta is not None and reloaded.base_meta.sha256 == result.sha256
    assert reloaded.paths("base").bin.read_bytes() == result.bin


def test_an_ephemeral_current_image_does_not_seed_the_base(
    tmp_path: Path, result: RenderResult
) -> None:
    store = CurrentImageStore(tmp_path)
    store.save(result, source="music", ephemeral=True)
    assert CurrentImageStore(tmp_path).load() is not None
    assert not store.paths("base").meta.exists()


def test_extend_expiry_only_touches_matching_ephemeral_content(
    tmp_path: Path, result: RenderResult
) -> None:
    store = CurrentImageStore(tmp_path)
    start = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    store.save(result, source="music", ephemeral=True, expires_at=start, content_key="a")
    assert store.extend_expiry("b", start + timedelta(minutes=5)) is None
    updated = store.extend_expiry("a", start + timedelta(minutes=5))
    assert updated is not None and updated.expires_at == start + timedelta(minutes=5)
    assert CurrentImageStore(tmp_path).load() == updated
    assert updated.expired(start + timedelta(minutes=6))
    assert not updated.expired(start + timedelta(minutes=4))


def test_promote_preview(tmp_path: Path, result: RenderResult, other: RenderResult) -> None:
    store = CurrentImageStore(tmp_path)
    assert store.promote_preview() is None
    store.save(result, source="immich")
    store.save(other, source="tmdb", push=False)
    promoted = store.promote_preview()
    assert promoted is not None and promoted.pushed
    assert store.meta == promoted and store.base_meta == promoted
    assert store.bin_path.read_bytes() == other.bin

    store.save(result, source="music", push=False, ephemeral=True)
    ephemeral = store.promote_preview()
    assert ephemeral is not None and store.meta == ephemeral
    assert store.base_meta == promoted  # an ephemeral preview never becomes the base
