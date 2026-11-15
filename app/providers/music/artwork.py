"""High-resolution album cover lookup for now-playing displays (no API key needed).

1. **Deezer** public search API: ``/search/album`` (artist + album), then ``/search``
   (artist + track, whose results carry the real album) and the ``cover_xl`` picture
   (1000x1000). Only a result whose album title matches is accepted at this stage: album
   searches are often flooded with covers, singles and remix EPs of the same name.
2. **MusicBrainz** release-group search (rate-limited to one request per second as their
   policy requires, with a descriptive User-Agent, and retried with a backoff when the
   service throttles) and the matching **Cover Art Archive** front cover (``front-1200``;
   the archive answers with a redirect to its storage).
3. The best approximate Deezer result, as a last resort.

Lookups are cached in memory (including "no cover" answers, but never transient errors),
so Home Assistant attribute updates do not hammer the services. The resolver never
raises: a failed lookup returns ``None`` and the caller renders a typographic card.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
_TRAILING_PARENTHESIS: Final = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$")
_EDITION_WORDS: Final = re.compile(
    r"\b(remaster(ed)?|deluxe|edition|anniversary|expanded|mono|stereo|bonus|version|"
    r"\d{4} mix)\b"
)
_VARIANT_WORDS: Final = re.compile(
    r"\b(remix(es)?|live|karaoke|instrumental|tribute|covers?|acoustic|lullab(y|ies)|"
    r"piano|ukulele|8[- ]?bit)\b"
)
#: Delays before retrying a throttled MusicBrainz request (HTTP 503/429).
MUSICBRAINZ_RETRY_DELAYS_S: Final = (2.0, 4.0)
#: Recordings fetched per search: a hit song exists as dozens of equally scored recordings
#: (live, compilations, DJ mixes); the studio album often is not among the first five.
RECORDING_SEARCH_LIMIT: Final = 25
#: Preferred release-group primary types for a recording's cover (best first).
PRIMARY_TYPE_RANK: Final[dict[str, int]] = {"album": 0, "single": 1, "ep": 1}
_LUCENE_SPECIAL: Final = '+-&|!(){}[]^"~*?:\\/'

ArtworkSource = Literal["deezer", "musicbrainz"]


class ArtworkLookupError(Exception):
    """A transient lookup failure (network, rate limit): the answer must not be cached."""


class ArtworkRateLimitError(ArtworkLookupError):
    """The service throttled the request (HTTP 429/503)."""


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


def artist_match_level(wanted: str, candidate: str | None) -> int | None:
    """0 = same artist, 1 = one name contains the other ("Queen" / "Queen + Bowie", but
    also tribute acts), ``None`` = different artist."""
    if not candidate:
        return None
    a, b = fold(wanted), fold(candidate)
    if not a or not b:
        return None
    if a == b:
        return 0
    return 1 if a in b or b in a else None


def _artist_matches(wanted: str, candidate: str | None) -> bool:
    return artist_match_level(wanted, candidate) is not None


def _strip_edition(title: str) -> str:
    """``title`` without trailing edition suffixes: "Abbey Road (Remastered 2009)"."""
    while (suffix := _TRAILING_PARENTHESIS.search(title)) is not None:
        inside = suffix.group(1).casefold()
        if not _EDITION_WORDS.search(inside) or _VARIANT_WORDS.search(inside):
            break
        title = title[: suffix.start()]
    return title


def title_match_level(wanted: str | None, candidate: str | None) -> int:
    """0 = same title, 1 = same title with an edition suffix, 2 = something else.

    Remixes, live albums, tributes, karaoke or cover versions are never editions.
    """
    if not wanted or not candidate:
        return 2
    target = fold(wanted)
    if fold(candidate) == target:
        return 0
    return 1 if fold(_strip_edition(candidate)) == target else 2


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

    async def backoff(self, seconds: float) -> None:
        """Hold every caller for ``seconds`` (the service asked us to slow down)."""
        async with self._lock:
            await self._sleep(seconds)
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
        approximate: ArtworkMatch | None = None
        if config.deezer_enabled:
            try:
                match, exact = await self._deezer(title=title, artist=artist, album=album)
                if match is not None and exact:
                    return match
                approximate = match
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
        if approximate is not None:
            return approximate
        if transient:
            raise ArtworkLookupError("no cover found and a service failed")
        return None

    async def _get_json(self, url: str, params: dict[str, Any], service: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ArtworkLookupError(f"{service} unreachable ({type(exc).__name__})") from None
        if response.status_code in {429, 503}:
            raise ArtworkRateLimitError(f"{service} rate limit (HTTP {response.status_code})")
        if response.status_code != 200:
            raise ArtworkLookupError(f"{service} answered HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ArtworkLookupError(f"{service} returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ArtworkLookupError(f"{service} returned an unexpected payload")
        return payload

    async def _deezer(
        self, *, title: str, artist: str, album: str | None
    ) -> tuple[ArtworkMatch | None, bool]:
        """The Deezer cover and whether its album title matches exactly.

        Without a requested album, any track result by the artist counts as exact.
        """
        approximate: ArtworkMatch | None = None
        if album:
            payload = await self._get_json(
                f"{DEEZER_API}/search/album",
                {"q": f'artist:"{artist}" album:"{album}"', "limit": SEARCH_LIMIT},
                "Deezer",
            )
            self._check_deezer_error(payload)
            found = self._pick_deezer(payload.get("data"), artist, album, track_results=False)
            if found is not None and found[1]:
                return found[0], True
            approximate = found[0] if found else None
        payload = await self._get_json(
            f"{DEEZER_API}/search",
            {"q": f'artist:"{artist}" track:"{title}"', "limit": SEARCH_LIMIT},
            "Deezer",
        )
        self._check_deezer_error(payload)
        found = self._pick_deezer(payload.get("data"), artist, album, track_results=True)
        if found is not None and found[1]:
            return found[0], True
        return approximate or (found[0] if found else None), False

    @staticmethod
    def _check_deezer_error(payload: dict[str, Any]) -> None:
        error = payload.get("error")
        if isinstance(error, dict):
            # Deezer reports quota errors (code 4) with HTTP 200.
            raise ArtworkLookupError(f"Deezer error {error.get('code')}: {error.get('message')}")

    @staticmethod
    def _pick_deezer(
        data: object, artist: str, album: str | None, *, track_results: bool
    ) -> tuple[ArtworkMatch, bool] | None:
        """The best Deezer album (or track's album) and whether it is a sure match.

        Ranked by artist (exact name before a name merely containing it, e.g. a tribute
        act), then album title (exact, then the same title with an edition suffix), then
        real albums before EPs and singles. A sure match has the exact artist and the exact
        (or edition) title; without a requested album, the exact artist is enough.
        """
        if not isinstance(data, list):
            return None
        ranked: list[tuple[tuple[int, int, bool], dict[str, Any]]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            record = (item.get("album") or {}) if track_results else item
            if not isinstance(record, dict) or not isinstance(record.get("cover_xl"), str):
                continue
            artist_rank = artist_match_level(artist, (item.get("artist") or {}).get("name"))
            if artist_rank is None:
                continue
            title_rank = title_match_level(album, record.get("title")) if album else 0
            is_album = (record.get("record_type") or item.get("record_type") or "album") == "album"
            ranked.append(((artist_rank, title_rank, not is_album), item))
        if not ranked:
            return None
        (artist_rank, title_rank, _), best = min(ranked, key=lambda pair: pair[0])
        record = (best.get("album") or {}) if track_results else best
        match = ArtworkMatch(
            url=record["cover_xl"],
            source="deezer",
            album=record.get("title"),
            artist=(best.get("artist") or {}).get("name"),
        )
        return match, artist_rank == 0 and title_rank <= 1

    async def _musicbrainz(
        self, *, title: str, artist: str, album: str | None
    ) -> ArtworkMatch | None:
        if album:
            query = f"releasegroup:{lucene_quote(album)} AND artist:{lucene_quote(artist)}"
            payload = await self._musicbrainz_get(
                f"{MUSICBRAINZ_API}/release-group/", {"query": query, "fmt": "json", "limit": 5}
            )
            group = self._pick_release_group(payload.get("release-groups"), artist)
        else:
            query = f"recording:{lucene_quote(title)} AND artist:{lucene_quote(artist)}"
            payload = await self._musicbrainz_get(
                f"{MUSICBRAINZ_API}/recording/",
                {"query": query, "fmt": "json", "limit": RECORDING_SEARCH_LIMIT},
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

    async def _musicbrainz_get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """A rate-limited MusicBrainz request, retried with a backoff when throttled."""
        for delay in (*MUSICBRAINZ_RETRY_DELAYS_S, None):
            await self._limiter.wait()
            try:
                return await self._get_json(url, params, "MusicBrainz")
            except ArtworkRateLimitError:
                if delay is None:
                    raise
                logger.debug("MusicBrainz throttled the request; retrying in %.0f s", delay)
                await self._limiter.backoff(delay)
        raise AssertionError("unreachable")  # pragma: no cover

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
