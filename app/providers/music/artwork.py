"""High-resolution album cover lookup for now-playing displays (no API key needed).

1. **Deezer** public search API: ``/search/album`` (artist + album) or ``/search`` (artist +
   track) and the ``cover_xl`` picture (1000x1000).
2. **MusicBrainz** release-group search (rate-limited to one request per second as their
   policy requires, with a descriptive User-Agent) and the matching **Cover Art Archive**
   front cover (``front-1200``; the archive answers with a redirect to its storage).

Lookups are cached in memory (including "no cover" answers, but never transient errors),
so Home Assistant attribute updates do not hammer the services. The resolver never
raises: a failed lookup returns ``None`` and the caller renders a typographic card.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from app.clients.http_images import USER_AGENT, ImageFetcher, ImageFetchError
from app.core.config_store import ConfigStore
from app.core.text import fold

logger = logging.getLogger(__name__)

DEEZER_API: Final = "https://api.deezer.com"
MUSICBRAINZ_API: Final = "https://musicbrainz.org/ws/2"
COVER_ART_ARCHIVE: Final = "https://coverartarchive.org"
MUSICBRAINZ_INTERVAL_S: Final = 1.0
CONNECT_TIMEOUT_S: Final = 5.0
READ_TIMEOUT_S: Final = 10.0
SEARCH_LIMIT: Final = 10
#: Minimum MusicBrainz search score (0-100) to accept a release group.
MIN_MUSICBRAINZ_SCORE: Final = 80
#: Recordings fetched per search: a hit song exists as dozens of equally scored recordings
#: (live, compilations, DJ mixes); the studio album often is not among the first five.
RECORDING_SEARCH_LIMIT: Final = 25
#: Preferred release-group primary types for a recording's cover (best first).
PRIMARY_TYPE_RANK: Final[dict[str, int]] = {"album": 0, "single": 1, "ep": 1}
_LUCENE_SPECIAL: Final = '+-&|!(){}[]^"~*?:\\/'

ArtworkSource = Literal["deezer", "musicbrainz"]


class ArtworkLookupError(Exception):
    """A transient lookup failure (network, rate limit): the answer must not be cached."""


@dataclass(frozen=True, slots=True)
class ArtworkMatch:
    """A cover found for a track or album."""

    url: str
    source: ArtworkSource
    album: str | None = None
    artist: str | None = None


@dataclass(frozen=True, slots=True)
class Artwork:
    """Downloaded cover bytes and where they came from."""

    data: bytes
    match: ArtworkMatch


def lucene_quote(value: str) -> str:
    """``value`` as a quoted Lucene phrase with special characters escaped."""
    escaped = "".join(f"\\{char}" if char in _LUCENE_SPECIAL else char for char in value)
    return f'"{escaped}"'


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and fold(a or "") == fold(b or "")


def _artist_matches(wanted: str, candidate: str | None) -> bool:
    """Exact folded match, or one name containing the other ("Queen" / "Queen + Bowie")."""
    if not candidate:
        return False
    a, b = fold(wanted), fold(candidate)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


class MinIntervalLimiter:
    """Allows one call per ``interval_s`` seconds (shared by concurrent callers)."""

    def __init__(
        self,
        interval_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        """Wait until the next call is allowed."""
        async with self._lock:
            if self._last is not None:
                delay = self._last + self._interval - self._clock()
                if delay > 0:
                    await self._sleep(delay)
            self._last = self._clock()


class ArtworkResolver:
    """Finds and downloads album covers from Deezer, then MusicBrainz / Cover Art Archive."""

    def __init__(
        self,
        config_store: ConfigStore,
        fetcher: ImageFetcher,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: MinIntervalLimiter | None = None,
    ) -> None:
        self._config_store = config_store
        self._fetcher = fetcher
        self._http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            follow_redirects=False,
            transport=transport,
        )
        self._limiter = limiter or MinIntervalLimiter(MUSICBRAINZ_INTERVAL_S)
        self._cache: OrderedDict[tuple[str, str, str], ArtworkMatch | None] = OrderedDict()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    # --- Public API -----------------------------------------------------------------------
    async def find(self, *, title: str, artist: str, album: str | None) -> Artwork | None:
        """Download the best cover for a track (``None`` when nothing usable was found)."""
        match = await self.lookup(title=title, artist=artist, album=album)
        if match is None:
            return None
        try:
            data = await self._fetcher.fetch(match.url)
        except ImageFetchError as exc:
            logger.info("Cover download from %s failed: %s", match.source, exc)
            return None
        return Artwork(data, match)

    async def lookup(self, *, title: str, artist: str, album: str | None) -> ArtworkMatch | None:
        """The cover URL for a track, from the cache or the services."""
        key = (fold(artist), fold(album or ""), fold(title))
        size = self._config_store.config.music.lookup_cache_size
        if size and key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        try:
            match = await self._search(title=title, artist=artist, album=album)
        except ArtworkLookupError as exc:
            logger.info("Cover lookup failed (not cached): %s", exc)
            return None
        if size:
            self._cache[key] = match
            while len(self._cache) > size:
                self._cache.popitem(last=False)
        return match

    # --- Search ---------------------------------------------------------------------------
    async def _search(self, *, title: str, artist: str, album: str | None) -> ArtworkMatch | None:
        config = self._config_store.config.music
        transient = False
        if config.deezer_enabled:
            try:
                match = await self._deezer(title=title, artist=artist, album=album)
                if match is not None:
                    return match
            except ArtworkLookupError as exc:
                logger.info("Deezer cover lookup failed: %s", exc)
                transient = True
        if config.musicbrainz_enabled:
            try:
                match = await self._musicbrainz(title=title, artist=artist, album=album)
                if match is not None:
                    return match
            except ArtworkLookupError as exc:
                logger.info("MusicBrainz cover lookup failed: %s", exc)
                transient = True
        if transient:
            raise ArtworkLookupError("no cover found and a service failed")
        return None

    async def _get_json(self, url: str, params: dict[str, Any], service: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ArtworkLookupError(f"{service} unreachable ({type(exc).__name__})") from None
        if response.status_code in {429, 503}:
            raise ArtworkLookupError(f"{service} rate limit (HTTP {response.status_code})")
        if response.status_code != 200:
            raise ArtworkLookupError(f"{service} answered HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ArtworkLookupError(f"{service} returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ArtworkLookupError(f"{service} returned an unexpected payload")
        return payload

    async def _deezer(self, *, title: str, artist: str, album: str | None) -> ArtworkMatch | None:
        if album:
            payload = await self._get_json(
                f"{DEEZER_API}/search/album",
                {"q": f'artist:"{artist}" album:"{album}"', "limit": SEARCH_LIMIT},
                "Deezer",
            )
            self._check_deezer_error(payload)
            match = self._pick_deezer_album(payload.get("data"), artist, album)
            if match is not None:
                return match
        payload = await self._get_json(
            f"{DEEZER_API}/search",
            {"q": f'artist:"{artist}" track:"{title}"', "limit": SEARCH_LIMIT},
            "Deezer",
        )
        self._check_deezer_error(payload)
        return self._pick_deezer_track(payload.get("data"), artist, album)

    @staticmethod
    def _check_deezer_error(payload: dict[str, Any]) -> None:
        error = payload.get("error")
        if isinstance(error, dict):
            # Deezer reports quota errors (code 4) with HTTP 200.
            raise ArtworkLookupError(f"Deezer error {error.get('code')}: {error.get('message')}")

    @staticmethod
    def _pick_deezer_album(data: object, artist: str, album: str) -> ArtworkMatch | None:
        if not isinstance(data, list):
            return None
        candidates = [
            item
            for item in data
            if isinstance(item, dict)
            and isinstance(item.get("cover_xl"), str)
            and _artist_matches(artist, (item.get("artist") or {}).get("name"))
        ]
        candidates.sort(key=lambda item: not _same(item.get("title"), album))
        if not candidates:
            return None
        best = candidates[0]
        return ArtworkMatch(
            url=best["cover_xl"],
            source="deezer",
            album=best.get("title"),
            artist=(best.get("artist") or {}).get("name"),
        )

    @staticmethod
    def _pick_deezer_track(data: object, artist: str, album: str | None) -> ArtworkMatch | None:
        if not isinstance(data, list):
            return None
        candidates = []
        for item in data:
            if not isinstance(item, dict):
                continue
            item_album = item.get("album") or {}
            if not isinstance(item_album.get("cover_xl"), str):
                continue
            if not _artist_matches(artist, (item.get("artist") or {}).get("name")):
                continue
            candidates.append(item)
        if album:
            candidates.sort(
                key=lambda item: not _same((item.get("album") or {}).get("title"), album)
            )
        if not candidates:
            return None
        best = candidates[0]
        return ArtworkMatch(
            url=best["album"]["cover_xl"],
            source="deezer",
            album=best["album"].get("title"),
            artist=(best.get("artist") or {}).get("name"),
        )

    async def _musicbrainz(
        self, *, title: str, artist: str, album: str | None
    ) -> ArtworkMatch | None:
        if album:
            query = f"releasegroup:{lucene_quote(album)} AND artist:{lucene_quote(artist)}"
            await self._limiter.wait()
            payload = await self._get_json(
                f"{MUSICBRAINZ_API}/release-group/",
                {"query": query, "fmt": "json", "limit": 5},
                "MusicBrainz",
            )
            group = self._pick_release_group(payload.get("release-groups"), artist)
        else:
            query = f"recording:{lucene_quote(title)} AND artist:{lucene_quote(artist)}"
            await self._limiter.wait()
            payload = await self._get_json(
                f"{MUSICBRAINZ_API}/recording/",
                {"query": query, "fmt": "json", "limit": RECORDING_SEARCH_LIMIT},
                "MusicBrainz",
            )
            group = self._group_from_recordings(payload.get("recordings"), artist)
        if group is None:
            return None
        group_id, group_title = group
        return ArtworkMatch(
            url=f"{COVER_ART_ARCHIVE}/release-group/{group_id}/front-1200",
            source="musicbrainz",
            album=group_title,
            artist=artist,
        )

    @staticmethod
    def _credited(item: dict[str, Any], artist: str) -> bool:
        credits = item.get("artist-credit") or []
        names = [c.get("name") for c in credits if isinstance(c, dict)]
        return any(_artist_matches(artist, name) for name in names)

    @classmethod
    def _pick_release_group(cls, groups: object, artist: str) -> tuple[str, str | None] | None:
        if not isinstance(groups, list):
            return None
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("id"), str):
                continue
            if int(group.get("score") or 0) < MIN_MUSICBRAINZ_SCORE:
                continue
            if cls._credited(group, artist):
                return group["id"], group.get("title")
        return None

    @staticmethod
    def _release_group_rank(group: dict[str, Any]) -> int:
        """Lower is better: studio albums, then singles/EPs, then anything else.

        A recording appears on many releases (compilations, live albums, videos); the
        original album cover is the one people recognize.
        """
        primary = str(group.get("primary-type") or "").casefold()
        secondary = group.get("secondary-types") or []
        rank = PRIMARY_TYPE_RANK.get(primary, len(PRIMARY_TYPE_RANK))
        return rank * 2 + (1 if secondary else 0)

    @classmethod
    def _group_from_recordings(
        cls, recordings: object, artist: str
    ) -> tuple[str, str | None] | None:
        if not isinstance(recordings, list):
            return None
        best: tuple[int, int, str, str | None] | None = None
        order = 0
        for recording in recordings:
            if not isinstance(recording, dict):
                continue
            if int(recording.get("score") or 0) < MIN_MUSICBRAINZ_SCORE:
                continue
            if not cls._credited(recording, artist):
                continue
            for release in recording.get("releases") or []:
                group = release.get("release-group") if isinstance(release, dict) else None
                if not isinstance(group, dict) or not isinstance(group.get("id"), str):
                    continue
                candidate = (
                    cls._release_group_rank(group),
                    order,
                    group["id"],
                    group.get("title") or release.get("title"),
                )
                order += 1
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
        return (best[2], best[3]) if best is not None else None
