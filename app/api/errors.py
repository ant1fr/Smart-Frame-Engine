"""HTTP mapping of the errors image providers raise on purpose."""

from __future__ import annotations

from typing import Any, Final

from fastapi import HTTPException

from app.providers.base import (
    InvalidSelectionError,
    NoCandidateError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)

#: OpenAPI ``responses`` documenting :func:`provider_http_error`.
PROVIDER_ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"description": "No eligible photo matched (or all are on the anti-repeat cooldown)"},
    409: {"description": "The source is not configured"},
    422: {"description": "Unknown or ambiguous person, album or account"},
    502: {"description": "The source failed or rejected its API key"},
    504: {"description": "The source did not respond in time"},
}


def provider_http_error(exc: ProviderError) -> HTTPException:
    """Map a provider error to an HTTP error (messages never contain secrets)."""
    if isinstance(exc, ProviderNotConfiguredError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidSelectionError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, NoCandidateError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ProviderUnavailableError):
        return HTTPException(status_code=504 if exc.timeout else 502, detail=str(exc))
    return HTTPException(status_code=500, detail="Image provider error")
