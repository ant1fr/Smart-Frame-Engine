"""Smart Frame Engine: modular image sources -> color eInk pipeline -> Fraimic frame."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    #: Single source of truth: ``version`` in ``pyproject.toml`` (PEP 440, e.g. ``1.0.0rc1``).
    __version__ = _distribution_version("smart-frame-engine")
except PackageNotFoundError:  # pragma: no cover - running from a bare source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
