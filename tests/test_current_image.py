from __future__ import annotations

import json
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
