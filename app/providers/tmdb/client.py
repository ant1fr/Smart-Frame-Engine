"""Minimal async TMDB (v3 API) client: search and poster listings."""

from __future__ import annotations

import logging
from typing import Any, Final, Self

import httpx

from app.clients.http_images import USER_AGENT
from app.providers.base import ProviderUnavailableError
from app.providers.tmdb.models import ResultMediaType, SearchResult, TmdbImage, TmdbMediaType

logger = logging.getLogger(__name__)

API_BASE: Final = "https://api.themoviedb.org/3"
IMAGE_BASE: Final = "https://image.tmdb.org/t/p"
CONNECT_TIMEOUT_S: Final = 5.0
READ_TIMEOUT_S: Final = 15.0


def is_bearer_token(api_key: str) -> bool:
    """Whether ``api_key`` is a v4 read access token (a JWT) rather than a v3 key."""
    return api_key.startswith("eyJ") and api_key.count(".") == 2


def image_url(file_path: str, size: str) -> str:
    """Full image URL of a poster ``file_path`` at ``size`` (``w780``, ``original``...)."""
    return f"{IMAGE_BASE}/{size}{file_path}"


class TmdbClient:
    """One TMDB session. Use as ``async with`` (closes the connection pool)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        params: dict[str, str] = {}
        if is_bearer_token(api_key):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            params["api_key"] = api_key
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            params=params,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """GET a JSON object; ``None`` on 404.

        Raises:
            ProviderUnavailableError: Network error, timeout, rejected key or bad payload.
        """
        try:
            response = await self._http.get(
                path, params={k: v for k, v in params.items() if v is not None}
            )
        except httpx.TimeoutException:
            raise ProviderUnavailableError("TMDB did not respond in time", timeout=True) from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"Cannot connect to TMDB ({type(exc).__name__})"
            ) from None
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise ProviderUnavailableError("TMDB rejected the API key")
        if response.status_code == 429:
            raise ProviderUnavailableError("TMDB rate limit reached; try again shortly")
        if response.status_code != 200:
            raise ProviderUnavailableError(f"TMDB answered HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ProviderUnavailableError("TMDB returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ProviderUnavailableError("TMDB returned an unexpected payload")
        return payload

    async def search(
        self, query: str, media_type: TmdbMediaType, *, year: int | None, language: str
    ) -> list[SearchResult]:
        """Movies and/or series matching ``query``, most relevant first."""
        params: dict[str, Any] = {"query": query, "language": language, "include_adult": "false"}
        if media_type == "movie":
            params["year"] = year
        elif media_type == "tv":
            params["first_air_date_year"] = year
        payload = await self._get(f"/search/{media_type}", params) or {}
        results: list[SearchResult] = []
        for raw in payload.get("results") or []:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("media_type", media_type)
            if kind not in {"movie", "tv"}:
                continue  # people in a multi search
            parsed = self._parse_result(raw, kind)
            if parsed is not None:
                results.append(parsed)
        return results

    @staticmethod
    def _parse_result(raw: dict[str, Any], kind: ResultMediaType) -> SearchResult | None:
        title = raw.get("title") if kind == "movie" else raw.get("name")
        if not isinstance(title, str) or not isinstance(raw.get("id"), int):
            return None
        return SearchResult.model_validate(
            {
                "id": raw["id"],
                "media_type": kind,
                "title": title,
                "original_title": raw.get("original_title" if kind == "movie" else "original_name"),
                "release_date": raw.get("release_date" if kind == "movie" else "first_air_date"),
                "poster_path": raw.get("poster_path"),
                "popularity": raw.get("popularity") or 0.0,
            }
        )

    async def posters(
        self,
        media_type: ResultMediaType,
        tmdb_id: int,
        *,
        include_languages: str,
        season: int | None = None,
    ) -> list[TmdbImage]:
        """Posters of a movie or series (or of one season), unranked."""
        if season is not None:
            path = f"/tv/{tmdb_id}/season/{season}/images"
        else:
            path = f"/{media_type}/{tmdb_id}/images"
        payload = await self._get(path, {"include_image_language": include_languages}) or {}
        posters: list[TmdbImage] = []
        for raw in payload.get("posters") or []:
            if isinstance(raw, dict) and isinstance(raw.get("file_path"), str):
                posters.append(TmdbImage.model_validate(raw))
        return posters
