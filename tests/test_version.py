"""Release label shown in the UI: spelled like the git tag and the Docker image tag."""

from __future__ import annotations

import pytest

from app import __version__
from app.version import display_version, release_label
from tests.conftest import ClientFactory


@pytest.mark.parametrize(
    ("pep440", "expected"),
    [
        ("1.0.0", "1.0.0"),
        ("1.0.0rc2", "1.0.0-rc.2"),
        ("1.2.0b1", "1.2.0-beta.1"),
        ("2.0.0a3", "2.0.0-alpha.3"),
        ("1.0.1.post1", "1.0.1-post.1"),
        ("1.1.0.dev4", "1.1.0-dev.4"),
        ("1.0.0rc2+g1a2b3c", "1.0.0-rc.2+g1a2b3c"),
        ("0.0.0+unknown", "0.0.0+unknown"),
        ("not a version", "not a version"),
    ],
)
def test_display_version(pep440: str, expected: str) -> None:
    assert display_version(pep440) == expected


def test_release_label_without_build() -> None:
    assert release_label("1.0.0rc2", build="") == "1.0.0-rc.2"


def test_release_label_marks_non_release_builds() -> None:
    assert release_label("1.0.0rc2", build="abc1234") == "1.0.0-rc.2+abc1234"
    assert release_label("1.0.0rc2", build="dev") == "1.0.0-rc.2+dev"


def test_release_label_does_not_double_the_local_segment() -> None:
    assert release_label("1.0.0rc2+g1a2b3c", build="abc") == "1.0.0-rc.2+g1a2b3c"


def test_release_label_sanitizes_the_build() -> None:
    assert release_label("1.0.0", build=" a/b c!\n") == "1.0.0+abc"


def test_release_label_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_BUILD", "deadbee")
    assert release_label("1.0.0") == "1.0.0+deadbee"
    monkeypatch.delenv("APP_BUILD")
    assert release_label("1.0.0") == "1.0.0"


def test_status_reports_both_spellings(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APP_BUILD", raising=False)
    body = make_client().get("/api/status").json()
    assert body["version"] == __version__
    assert body["release"] == display_version(__version__)
    assert make_client().get("/api/health").json()["version"] == __version__
