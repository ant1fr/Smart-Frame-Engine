"""Rule descriptors stay in sync with the request models and are served by the API."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from app.api.routes.smart_mix import _RULE_MODELS
from app.core.context import RuleContext, expand_params
from app.providers.immich.models import ImmichDisplayRequest
from app.providers.tmdb.models import TmdbDisplayRequest
from app.services.rule_catalog import (
    Condition,
    RuleKindSpec,
    default_catalog,
    placeholders,
)
from tests.conftest import ClientFactory

SPECS = default_catalog().all()
IDS = [spec.kind for spec in SPECS]


def conditions(spec: RuleKindSpec) -> list[Condition]:
    found: list[Condition] = []
    for field in spec.fields:
        found += field.show_when + field.required_when
    for part in spec.summary:
        found += part.when
    return found


def model_of(spec: RuleKindSpec) -> type[BaseModel]:
    return _RULE_MODELS[spec.kind]


def test_catalog_covers_the_built_in_kinds() -> None:
    assert IDS == ["immich", "tmdb"]
    assert _RULE_MODELS["immich"] is ImmichDisplayRequest
    assert _RULE_MODELS["tmdb"] is TmdbDisplayRequest


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_every_field_is_a_field_of_the_request_model(spec: RuleKindSpec) -> None:
    model_fields = set(model_of(spec).model_fields)
    assert spec.field_keys() <= model_fields
    assert len(spec.field_keys()) == len(spec.fields), "duplicate field key"


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_conditions_and_summary_reference_known_fields(spec: RuleKindSpec) -> None:
    keys = spec.field_keys()
    assert {condition.field for condition in conditions(spec)} <= keys
    assert {part.field for part in spec.summary if part.field} <= keys


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_stored_rules_never_carry_non_replayed_fields(spec: RuleKindSpec) -> None:
    from app.services.scheduler import NON_REPLAYED_FIELDS

    assert not spec.field_keys() & set(NON_REPLAYED_FIELDS)


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_presets_are_valid_rules(spec: RuleKindSpec) -> None:
    assert spec.presets
    for preset in spec.presets:
        assert set(preset.params) <= spec.field_keys(), preset.id
        model_of(spec).model_validate(_expand(preset.params))
    assert len({preset.id for preset in spec.presets}) == len(spec.presets)


def _expand(params: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    context = RuleContext(now=datetime(2026, 3, 14, tzinfo=UTC), tz=ZoneInfo("UTC"))
    expanded = expand_params(params, context)
    assert isinstance(expanded, dict)
    return expanded


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_segmented_fields_offer_valid_choices(spec: RuleKindSpec) -> None:
    for field in spec.fields:
        if field.widget == "segmented":
            assert len(field.options) >= 2, field.key
            allowed = {option.value for option in field.options}
            if field.default is not None:
                assert field.default in allowed, field.key
        else:
            assert not field.options, field.key


def test_placeholders_match_the_expander() -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    context = RuleContext(now=datetime(2026, 3, 14, tzinfo=UTC), tz=ZoneInfo("UTC"))
    assert {item.name for item in placeholders()} == set(context.variables)


# --- GET /api/smart-mix/kinds ------------------------------------------------------------
def test_kinds_endpoint_serves_the_descriptors(make_client: ClientFactory) -> None:
    response = make_client().get("/api/smart-mix/kinds")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [kind["kind"] for kind in body["kinds"]] == ["immich", "tmdb"]
    immich = body["kinds"][0]
    assert immich["service"] == "immich"
    assert {"mode", "persons", "album_name", "clip_queries"} <= {f["key"] for f in immich["fields"]}
    assert body["placeholders"][0]["name"] == "season"


def test_kinds_endpoint_only_lists_runnable_kinds(make_client: ClientFactory) -> None:
    client = make_client()
    runnable = set(client.get("/api/smart-mix").json()["supported_kinds"])
    kinds = {kind["kind"] for kind in client.get("/api/smart-mix/kinds").json()["kinds"]}
    assert kinds <= runnable


def test_kinds_endpoint_needs_the_api_key(make_client: ClientFactory) -> None:
    client = make_client(app_api_key="secret")
    assert client.get("/api/smart-mix/kinds").status_code == 401
    ok = client.get("/api/smart-mix/kinds", headers={"X-API-Key": "secret"})
    assert ok.status_code == 200
