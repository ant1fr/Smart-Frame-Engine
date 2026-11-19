"""Human-facing release label, spelled like the git tag and the Docker image tag.

``pyproject.toml`` carries a PEP 440 version (``1.0.0rc2``) while the git tag and the published
image tag are SemVer (``v1.0.0-rc.2`` / ``1.0.0-rc.2``). The API keeps reporting the PEP 440
spelling (``GET /api/health`` -> ``version``, see ``docs/API_CONTRACT.md``); the UI shows the
SemVer spelling so it matches what users pulled.
"""

from __future__ import annotations

import os
import re
from typing import Final

from app import __version__

_PRE_RELEASE_NAMES: Final = {"a": "alpha", "b": "beta", "rc": "rc"}

_PEP440: Final = re.compile(
    r"""^
    (?P<release>\d+(?:\.\d+)*)
    (?:(?P<pre>a|b|rc)(?P<pre_n>\d+))?
    (?:\.post(?P<post>\d+))?
    (?:\.dev(?P<dev>\d+))?
    (?:\+(?P<local>[A-Za-z0-9.]+))?
    $""",
    re.VERBOSE,
)

#: Environment variable set at image build time (``--build-arg APP_BUILD=<short sha>``).
BUILD_ENV: Final = "APP_BUILD"


def display_version(version: str) -> str:
    """Convert a normalized PEP 440 version to the SemVer spelling used by tags.

    ``1.0.0rc2`` -> ``1.0.0-rc.2``; ``1.0.0`` and unparseable values are returned unchanged.
    """
    match = _PEP440.match(version.strip())
    if match is None:
        return version
    result = match["release"]
    identifiers: list[str] = []
    if match["pre"]:
        identifiers.append(f"{_PRE_RELEASE_NAMES[match['pre']]}.{int(match['pre_n'])}")
    if match["post"]:
        identifiers.append(f"post.{int(match['post'])}")
    if match["dev"]:
        identifiers.append(f"dev.{int(match['dev'])}")
    if identifiers:
        result += "-" + ".".join(identifiers)
    if match["local"]:
        result += "+" + match["local"]
    return result


def release_label(version: str = __version__, build: str | None = None) -> str:
    """The version as shown in the UI: SemVer spelling, plus ``+<build>`` for non-tag builds.

    ``build`` defaults to the ``APP_BUILD`` environment variable (empty for release images).
    """
    label = display_version(version)
    if build is None:
        build = os.environ.get(BUILD_ENV, "")
    build = re.sub(r"[^A-Za-z0-9.]", "", build.strip())[:40]
    if build and "+" not in label:
        label += f"+{build}"
    return label
