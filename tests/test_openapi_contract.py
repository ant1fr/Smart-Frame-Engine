"""Guardrail: ``docs/openapi.json`` and the contract's endpoint index match the code."""

from __future__ import annotations

import importlib.util
import json
from types import ModuleType

from app.core.contract import CAPABILITIES
from scripts import export_openapi as exporter

HOW_TO_FIX = (
    "docs/openapi.json or docs/API_CONTRACT.md is out of date with the code.\n"
    "Regenerate both from the repository root and commit the result:\n\n"
    "    python scripts/export_openapi.py\n\n"
    "Then describe the change in the 'Contract Changelog' of docs/API_CONTRACT.md."
)


def test_exporter_is_importable() -> None:
    assert isinstance(exporter, ModuleType)
    assert importlib.util.find_spec("scripts.export_openapi") is not None


def test_openapi_json_matches_the_application() -> None:
    committed = json.loads(exporter.OPENAPI_PATH.read_text(encoding="utf-8"))
    assert committed == exporter.build_schema(), HOW_TO_FIX


def test_openapi_json_is_canonical() -> None:
    """The file is exactly what the exporter writes (sorted keys, trailing newline)."""
    committed = exporter.OPENAPI_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert committed == exporter.render_schema(exporter.build_schema()), HOW_TO_FIX


def test_contract_endpoint_index_is_current() -> None:
    document = exporter.CONTRACT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected = exporter.splice_endpoints(
        document, exporter.render_endpoints(exporter.build_schema())
    )
    assert document == expected, HOW_TO_FIX


def test_every_capability_is_backed_by_a_real_endpoint() -> None:
    schema = exporter.build_schema()
    for flag, (method, path) in CAPABILITIES.items():
        operations = schema["paths"].get(path, {})
        assert method.lower() in operations, f"capability {flag!r}: {method} {path} is missing"


def test_every_capability_is_documented_in_the_contract() -> None:
    document = exporter.CONTRACT_PATH.read_text(encoding="utf-8")
    for flag in CAPABILITIES:
        assert f"`{flag}`" in document, f"capability {flag!r} is not in docs/API_CONTRACT.md"
