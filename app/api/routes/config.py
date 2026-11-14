"""Runtime configuration endpoints (always API-key protected when a key is set)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Security
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import ConfigStoreDep
from app.core.auth import api_key_scheme
from app.core.config_models import RuntimeConfig
from app.core.config_store import ConfigError, ConfigValidationError

router = APIRouter(
    prefix="/api/config",
    tags=["config"],
    dependencies=[Security(api_key_scheme)],
)


class ConfigView(BaseModel):
    """Effective configuration with masked secrets and value provenance."""

    config: dict[str, dict[str, Any]] = Field(
        description="Effective configuration; secrets are masked"
    )
    sources: dict[str, str] = Field(
        description="Origin of each value: 'default', 'env' (.env) or 'config' (saved)"
    )
    frame_size: tuple[int, int] = Field(description="Composition size (width, height)")


class ResetRequest(BaseModel):
    """Paths to reset; omit or send ``null`` to reset everything."""

    paths: list[str] | None = Field(
        default=None,
        description="Entries like 'immich' (whole section) or 'immich.url' (single field)",
        examples=[["immich.url", "eink"]],
    )


class ConfigErrorResponse(BaseModel):
    """Validation error payload (never echoes submitted values)."""

    detail: str
    errors: list[dict[str, Any]] = Field(default_factory=list)


PATCH_EXAMPLES: dict[str, Any] = {
    "fraimic": {
        "summary": "Battery-powered frame",
        "value": {"fraimic": {"host": "fraimic.local", "power_mode": "deepsleep"}},
    },
    "immich": {
        "summary": "Immich defaults",
        "value": {"immich": {"persons": ["Alice", "Bob"], "match_mode": "any"}},
    },
}


def _view(store: ConfigStoreDep) -> ConfigView:
    public = store.public_view()
    return ConfigView(
        config=public["config"], sources=public["sources"], frame_size=store.config.frame_size
    )


def _error_response(exc: ConfigError) -> JSONResponse:
    status_code = 422 if isinstance(exc, ConfigValidationError) else 400
    payload = ConfigErrorResponse(detail=exc.message, errors=exc.errors)
    return JSONResponse(payload.model_dump(), status_code=status_code)


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ConfigErrorResponse, "description": "Unknown section or field"},
    422: {"model": ConfigErrorResponse, "description": "Resulting configuration is invalid"},
}


@router.get("", response_model=ConfigView, summary="Read the runtime configuration")
async def read_config(store: ConfigStoreDep) -> ConfigView:
    """Return the effective configuration (secrets masked) and where each value comes from."""
    return _view(store)


@router.get("/schema", summary="JSON Schema of the runtime configuration")
async def config_schema() -> dict[str, Any]:
    """JSON Schema describing every section and field (used to render settings forms)."""
    return RuntimeConfig.model_json_schema()


@router.put(
    "",
    response_model=ConfigView,
    responses=_ERROR_RESPONSES,
    summary="Update the runtime configuration",
)
async def update_config(
    store: ConfigStoreDep,
    patch: Annotated[
        dict[str, dict[str, Any]],
        Body(
            description="Partial update: {section: {field: value}}. Masked secrets are ignored.",
            openapi_examples=PATCH_EXAMPLES,
        ),
    ],
) -> ConfigView | JSONResponse:
    """Validate and save a partial update to ``/data/config.json``.

    Saved values take precedence over ``.env``. Nothing is written if validation fails.
    """
    try:
        await store.update(patch)
    except ConfigError as exc:
        return _error_response(exc)
    return _view(store)


@router.post(
    "/reset",
    response_model=ConfigView,
    responses=_ERROR_RESPONSES,
    summary="Reset saved values to .env / defaults",
)
async def reset_config(
    store: ConfigStoreDep,
    request: Annotated[ResetRequest | None, Body()] = None,
) -> ConfigView | JSONResponse:
    """Remove saved overrides so the values fall back to ``.env`` or built-in defaults."""
    try:
        await store.reset(None if request is None else request.paths)
    except ConfigError as exc:
        return _error_response(exc)
    return _view(store)
