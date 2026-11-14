from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core import atomic
from app.core.atomic import atomic_write_bytes, remove_quietly
from app.core.json_store import atomic_write_json, read_json


def test_atomic_write_bytes_creates_parents_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "file.bin"
    atomic_write_bytes(target, b"first")
    atomic_write_bytes(target, b"second")
    assert target.read_bytes() == b"second"
    assert [p.name for p in target.parent.iterdir()] == ["file.bin"]


def test_atomic_write_bytes_cleans_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"original")

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["file.bin"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_atomic_write_bytes_applies_mode(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    atomic_write_bytes(target, b"{}", file_mode=0o600)
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_json_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"
    atomic_write_json(target, {"name": "Élodie", "n": [1, 2]})
    raw = target.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw) == {"name": "Élodie", "n": [1, 2]}
    assert read_json(target, dict) == {"name": "Élodie", "n": [1, 2]}


def test_remove_quietly(tmp_path: Path) -> None:
    present = tmp_path / "a"
    present.write_bytes(b"x")
    remove_quietly(present, tmp_path / "missing")
    assert not present.exists()
