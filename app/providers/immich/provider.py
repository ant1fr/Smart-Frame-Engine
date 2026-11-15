"""The Immich image provider: select a photo, fetch it with its face regions.

One query path serves every mode. For each selected account the provider resolves the
requested people/album, runs random (``random``/``album``/``favorites``) or CLIP smart
(``search``) queries, merges the accounts, removes duplicates by content checksum and
photos still on the anti-repeat cooldown, then picks one at random.
"""

from __future__ import annotations

import io
import itertools
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Final
from zoneinfo import ZoneInfo

from PIL import Image
from pydantic import JsonValue

from app.core.config_models import ImmichConfig, QualityConfig
from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.security import InvalidImageError, verify_image_bytes
from app.processor.fit import FocusRegion
from app.processor.quality import LEVELS, AssetFacts, ImageStats, measure, violations
from app.providers.base import (
    BaseImageProvider,
    ImageAsset,
    InvalidSelectionError,
    NoCandidateError,
    ProviderUnavailableError,
)
from app.providers.immich.client import ImmichClient, ImmichGateway, ImmichResponseError
from app.providers.immich.dates import DateWindow, on_this_day_windows
from app.providers.immich.faces import face_regions
from app.providers.immich.models import (
    AlbumInfo,
    AssetDto,
    ImmichDisplayRequest,
    SearchFilters,
)
from app.providers.immich.people import PeopleDirectory, resolve_person, suggest_names

logger = logging.getLogger(__name__)

SOURCE: Final = "immich"
MAX_QUERIES_PER_ACCOUNT: Final = 10
MAX_PERSON_COMBINATIONS: Final = 8
MAX_DATED_QUERIES: Final = 30
MAX_TITLE_PEOPLE: Final = 4
THUMBNAIL_MAX_BYTES: Final = 2 * 1024 * 1024

RAW_EXTENSIONS: Final = frozenset(
    {
        ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf",
        ".rw2", ".pef", ".raw", ".3fr", ".erf", ".kdc", ".mrw", ".x3f", ".srw", ".rwl", ".iiq",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Plan:
    names: tuple[str, ...]
    match_mode: str
    album_name: str
    album_id: str
    favorites: bool
    exclude_archived: bool
    queries: tuple[str, ...]
    size: int
    windows: tuple[DateWindow | None, ...] = (None,)


@dataclass(slots=True)
class _AccountResult:
    assets: list[AssetDto] = field(default_factory=list)
    resolved: set[str] = field(default_factory=set)
    known_names: list[str] = field(default_factory=list)
    target_ids: set[str] = field(default_factory=set)
    album_found: bool = False


@dataclass(frozen=True, slots=True)
class _Candidate:
    account: str
    asset: AssetDto
    key: str

    @property
    def taken_at(self) -> datetime | None:
        return _parse_date(self.asset)

    @property
    def facts(self) -> AssetFacts:
        exif = self.asset.exifInfo
        return AssetFacts(
            has_camera=bool(exif and (exif.make or exif.model)),
            width=self.asset.width,
            height=self.asset.height,
        )


def history_key(account: str, asset: AssetDto) -> str:
    """Content-based key (SHA-1 checksum) so one photo in two accounts is one photo."""
    return f"sha1:{asset.checksum}" if asset.checksum else f"{account}:{asset.id}"


def is_raw(asset: AssetDto) -> bool:
    """RAW camera files cannot be decoded by Pillow; use Immich's converted JPEG instead."""
    suffix = PurePath(asset.originalFileName).suffix.lower()
    mime = asset.originalMimeType.lower()
    return suffix in RAW_EXTENSIONS or (mime.startswith("image/x-") and "bmp" not in mime)


def _merge_names(first: Sequence[str], second: Sequence[str]) -> tuple[str, ...]:
    merged: dict[str, str] = {}
    for name in (*first, *second):
        stripped = name.strip()
        if stripped:
            merged.setdefault(stripped.casefold(), stripped)
    return tuple(merged.values())


def _parse_date(asset: AssetDto) -> datetime | None:
    for raw in (asset.localDateTime, asset.exifInfo.dateTimeOriginal if asset.exifInfo else None):
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                continue
    return None


def build_title_and_details(
    account: str, asset: AssetDto, quality: str, faces_used: int
) -> tuple[str, dict[str, JsonValue]]:
    """Human-readable title (people - date - place) and metadata for the UI."""
    people = [person.name.strip() for person in asset.people if person.name.strip()]
    taken = _parse_date(asset)
    exif = asset.exifInfo
    place = ", ".join(part for part in (exif.city, exif.country) if part) if exif else ""
    parts = [
        ", ".join(people[:MAX_TITLE_PEOPLE]),
        f"{taken.day} {taken:%b %Y}" if taken else "",
        place,
    ]
    title = " · ".join(part for part in parts if part) or asset.originalFileName or "Immich photo"
    details: dict[str, JsonValue] = {
        "account": account,
        "asset_id": asset.id,
        "filename": asset.originalFileName or None,
        "taken_at": taken.isoformat() if taken else None,
        "location": place or None,
        "people": list(people),
        "quality": quality,
        "faces_used": faces_used,
    }
    return title, details


class ImmichProvider(BaseImageProvider[ImmichDisplayRequest]):
    """Selects and downloads photos from one or several Immich accounts."""

    name = SOURCE

    def __init__(
        self,
        config_store: ConfigStore,
        gateway: ImmichGateway,
        people: PeopleDirectory,
        history: HistoryStore,
        *,
        max_bytes: int,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_store = config_store
        self._gateway = gateway
        self._people = people
        self._history = history
        self._max_bytes = max_bytes
        self._rng = rng or random.Random()  # noqa: S311 - not security relevant
        self._clock = clock or (lambda: datetime.now(UTC))

    # --- public API ----------------------------------------------------------------------
    async def fetch(self, request: ImmichDisplayRequest) -> ImageAsset:
        config = self._config_store.config.immich
        accounts = list(self._gateway.accounts(request.accounts))
        plan = self._plan(request, config)

        results: dict[str, _AccountResult] = {}
        errors: dict[str, ProviderUnavailableError] = {}
        for account in accounts:
            try:
                results[account] = await self._gather(account, plan, config)
            except ProviderUnavailableError as exc:
                errors[account] = exc
        if not results:
            raise next(iter(errors.values()))
        for account, failure in errors.items():
            logger.warning("Immich account %r skipped: %s", account, failure)

        self._check_resolution(plan, results)
        candidates = self._merge(results)
        eligible = self._eligible(candidates, config, request.ignore_history)
        gate_config = self._config_store.config.quality
        skip = (
            request.mode == "album"
            if request.skip_quality_gate is None
            else (request.skip_quality_gate)
        )
        chosen, suitability = await self._select(eligible, gate_config, gate=not skip)

        account_result = results[chosen.account]
        async with self._gateway.client(chosen.account) as client:
            asset = await client.asset(chosen.asset.id)
            data, quality = await self._download(client, asset)
            regions = await self._regions(client, asset, account_result.target_ids)
        title, details = build_title_and_details(chosen.account, asset, quality, len(regions))
        if suitability is not None:
            details["suitability"] = suitability
        return ImageAsset(
            data=data,
            source=SOURCE,
            history_key=chosen.key,
            title=title,
            details=details,
            regions=tuple(regions),
            taken_at=_parse_date(asset),
        )

    async def albums(
        self, accounts: Sequence[str] | None = None
    ) -> tuple[list[AlbumInfo], dict[str, str]]:
        """Albums of the selected accounts plus per-account error messages."""
        found: list[AlbumInfo] = []
        errors: dict[str, str] = {}
        for account in self._gateway.accounts(accounts):
            try:
                async with self._gateway.client(account) as client:
                    for album in await client.albums():
                        found.append(
                            AlbumInfo(
                                account=account,
                                id=album.id,
                                name=album.albumName,
                                asset_count=album.assetCount,
                                shared=album.shared,
                            )
                        )
            except ProviderUnavailableError as exc:
                errors[account] = str(exc)
        found.sort(key=lambda album: (album.name.casefold(), album.account))
        return found, errors

    # --- planning ------------------------------------------------------------------------
    def _plan(self, request: ImmichDisplayRequest, config: ImmichConfig) -> _Plan:
        persons = config.persons if request.persons is None else request.persons
        always = config.always_include if request.always_include is None else request.always_include
        album_id = (request.album_id or "").strip()
        album_name = (request.album_name or "").strip()
        if not album_id and not album_name:
            album_name = config.album.strip()
        if request.mode == "album" and not album_id and not album_name:
            raise InvalidSelectionError("mode 'album' needs album_name or album_id")
        return _Plan(
            names=_merge_names(persons, always),
            match_mode=request.match_mode or config.match_mode,
            album_name=album_name,
            album_id=album_id,
            favorites=request.mode == "favorites" or config.favorites_only,
            exclude_archived=config.exclude_archived,
            queries=tuple(request.clip_queries) if request.mode == "search" else (),
            size=config.search_candidates,
            windows=self._windows(request),
        )

    def _windows(self, request: ImmichDisplayRequest) -> tuple[DateWindow | None, ...]:
        """Date windows to query: one explicit range, one per past year, or no restriction."""
        if request.on_this_day is not None:
            tz = ZoneInfo(self._config_store.config.scheduler.timezone)
            return tuple(on_this_day_windows(self._clock(), tz, request.on_this_day))
        if request.taken_after is not None or request.taken_before is not None:
            return ((request.taken_after, request.taken_before),)
        return (None,)

    # --- candidate gathering ---------------------------------------------------------------
    async def _gather(self, account: str, plan: _Plan, config: ImmichConfig) -> _AccountResult:
        result = _AccountResult()
        slots: list[list[str]] = []
        if plan.names:
            people = await self._people.get(account)
            result.known_names = people.names()
            for name in plan.names:
                ids = resolve_person(name, account, people, config.person_aliases)
                if ids:
                    result.resolved.add(name.casefold())
                slots.append(ids)
            result.target_ids = {person_id for ids in slots for person_id in ids}

        async with self._gateway.client(account) as client:
            album_ids: tuple[str, ...] = ()
            if plan.album_id or plan.album_name:
                album_ids = await self._album_ids(client, plan)
                result.album_found = bool(album_ids)
                if not album_ids:
                    return result

            combos = self._person_combinations(slots, plan.match_mode)
            if combos is None:
                return result
            seen: set[str] = set()
            queries = plan.queries or ("",)
            plan_queries = itertools.product(queries, combos, plan.windows)
            for query, combo, window in itertools.islice(plan_queries, MAX_DATED_QUERIES):
                filters = SearchFilters(
                    combo,
                    album_ids,
                    plan.favorites,
                    plan.exclude_archived,
                    taken_after=window[0] if window else None,
                    taken_before=window[1] if window else None,
                )
                found = (
                    await client.search_smart(query, filters, plan.size)
                    if query
                    else await client.search_random(filters, plan.size)
                )
                for asset in found:
                    if asset.id not in seen:
                        seen.add(asset.id)
                        result.assets.append(asset)
        return result

    @staticmethod
    def _person_combinations(
        slots: list[list[str]], match_mode: str
    ) -> list[tuple[str, ...]] | None:
        """Person-ID tuples to query, or ``None`` when this account cannot satisfy the request."""
        if not slots:
            return [()]
        if match_mode == "all":
            if any(not ids for ids in slots):
                return None
            return list(itertools.islice(itertools.product(*slots), MAX_PERSON_COMBINATIONS))
        flat = [person_id for ids in slots for person_id in ids]
        if not flat:
            return None
        singles: list[tuple[str, ...]] = [(person_id,) for person_id in dict.fromkeys(flat)]
        return singles[:MAX_QUERIES_PER_ACCOUNT]

    @staticmethod
    async def _album_ids(client: ImmichClient, plan: _Plan) -> tuple[str, ...]:
        albums = await client.albums()
        if plan.album_id:
            return tuple(album.id for album in albums if album.id == plan.album_id)
        wanted = plan.album_name.casefold()
        return tuple(album.id for album in albums if album.albumName.casefold() == wanted)

    @staticmethod
    def _check_resolution(plan: _Plan, results: dict[str, _AccountResult]) -> None:
        known = [name for result in results.values() for name in result.known_names]
        for name in plan.names:
            if not any(name.casefold() in result.resolved for result in results.values()):
                hint = suggest_names(name, known)
                suffix = f" Did you mean: {', '.join(hint)}?" if hint else ""
                raise InvalidSelectionError(
                    f"Unknown person {name!r} in the selected accounts.{suffix}"
                )
        if (plan.album_id or plan.album_name) and not any(r.album_found for r in results.values()):
            label = plan.album_name or plan.album_id
            raise InvalidSelectionError(f"Album {label!r} was not found in the selected accounts")

    # --- selection ---------------------------------------------------------------------------
    @staticmethod
    def _merge(results: dict[str, _AccountResult]) -> list[_Candidate]:
        merged: list[_Candidate] = []
        seen: set[str] = set()
        for account, result in results.items():
            for asset in result.assets:
                if asset.isTrashed or asset.type.upper() != "IMAGE":
                    continue
                key = history_key(account, asset)
                if key not in seen:
                    seen.add(key)
                    merged.append(_Candidate(account, asset, key))
        return merged

    def _eligible(
        self, candidates: list[_Candidate], config: ImmichConfig, ignore_history: bool
    ) -> list[_Candidate]:
        """Candidates left after the archive filter, anti-repeat history and burst cooldown."""
        if config.exclude_archived:
            candidates = [c for c in candidates if not c.asset.isArchived]
        if not candidates:
            raise NoCandidateError("No photo matched the request")
        if ignore_history:
            return candidates
        recent = self._history.recent_ids(SOURCE, config.anti_repeat_days)
        eligible = [
            c
            for c in candidates
            if c.key not in recent
            and not self._history.near_recent(
                SOURCE,
                c.taken_at,
                within_hours=config.burst_cooldown_hours,
                shown_within_days=config.burst_cooldown_days,
            )
        ]
        if not eligible:
            raise NoCandidateError(
                f"All {len(candidates)} matching photo(s) were shown within the last "
                f"{config.anti_repeat_days} day(s) or were taken within "
                f"{config.burst_cooldown_hours:g} h of a photo shown in the last "
                f"{config.burst_cooldown_days} day(s); retry with ignore_history "
                "or widen the filters"
            )
        return eligible

    async def _select(
        self, eligible: list[_Candidate], quality: QualityConfig, *, gate: bool
    ) -> tuple[_Candidate, dict[str, JsonValue] | None]:
        """Pick one candidate, vetting random ones against the quality gate.

        Candidates are inspected one at a time (thumbnail + metadata, never the original)
        until one passes the strict rules; typically that is the first. If none of the
        ``candidates_checked`` passes, the rules relax level by level over the inspected
        ones, so a request never dead-ends. A candidate whose thumbnail cannot be read is
        accepted as is (the gate never blocks on its own failures).
        """
        if not gate or not quality.enabled:
            return self._rng.choice(eligible), None

        pool = list(eligible)
        inspected: list[tuple[_Candidate, ImageStats]] = []
        rejected: list[str] = []
        while pool and len(inspected) < quality.candidates_checked:
            candidate = self._rng.choice(pool)
            pool.remove(candidate)
            stats = await self._inspect(candidate)
            if stats is None:
                return candidate, self._suitability(0, len(inspected) + 1, rejected, unchecked=True)
            inspected.append((candidate, stats))
            broken = violations(stats, candidate.facts, quality, 0)
            if not broken:
                return candidate, self._suitability(0, len(inspected), rejected)
            rejected.append("+".join(broken))

        for level in range(1, LEVELS + 1):
            for candidate, stats in inspected:
                if not violations(stats, candidate.facts, quality, level):
                    logger.info(
                        "Quality gate relaxed to level %d after %d rejections", level, len(rejected)
                    )
                    return candidate, self._suitability(level, len(inspected), rejected)
        raise AssertionError("unreachable: the last relaxation level accepts everything")

    @staticmethod
    def _suitability(
        level: int, checked: int, rejected: list[str], *, unchecked: bool = False
    ) -> dict[str, JsonValue]:
        info: dict[str, JsonValue] = {
            "level": level,
            "checked": checked,
            "rejected": list(rejected),
        }
        if unchecked:
            info["unchecked"] = True
        return info

    async def _inspect(self, candidate: _Candidate) -> ImageStats | None:
        """Pixel statistics of a candidate's thumbnail, or ``None`` if it cannot be read."""
        try:
            async with self._gateway.client(candidate.account) as client:
                data = await client.download(
                    candidate.asset.id, variant="thumbnail", max_bytes=THUMBNAIL_MAX_BYTES
                )
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                return measure(image)
        except (ProviderUnavailableError, OSError, ValueError, Image.DecompressionBombError) as exc:
            logger.info("Quality gate could not inspect a candidate (%s); accepting it", exc)
            return None

    # --- download --------------------------------------------------------------------------------
    async def _download(self, client: ImmichClient, asset: AssetDto) -> tuple[bytes, str]:
        """Original at full resolution; Immich's converted JPEG for RAW, oversized or odd files."""
        size = asset.exifInfo.fileSizeInByte if asset.exifInfo else None
        variants: list[str] = []
        if not is_raw(asset) and (size is None or size <= self._max_bytes):
            variants.append("original")
        variants += ["fullsize", "preview"]
        last: Exception | None = None
        for variant in variants:
            try:
                data = await client.download(
                    asset.id,
                    variant=variant,
                    max_bytes=self._max_bytes,
                    edited=asset.isEdited,
                )
                verify_image_bytes(data)
            except (ImmichResponseError, InvalidImageError) as exc:
                logger.info("Immich %s download of an asset failed (%s); trying next", variant, exc)
                last = exc
                continue
            return data, variant
        raise ProviderUnavailableError(
            f"Could not download a usable image from Immich account {client.name!r}"
        ) from last

    async def _regions(
        self, client: ImmichClient, asset: AssetDto, target_ids: set[str]
    ) -> list[FocusRegion]:
        """Face regions for smart cropping (none for edited assets: boxes refer to the original)."""
        if asset.isEdited:
            return []
        try:
            faces = await client.faces(asset.id)
        except ProviderUnavailableError as exc:
            logger.warning("Could not load faces from Immich: %s", exc)
            return []
        if not faces:
            return []
        image_size = (
            (asset.width, asset.height)
            if asset.width and asset.height
            else (faces[0].imageWidth, faces[0].imageHeight)
        )
        return face_regions(
            faces,
            image_size=image_size,
            target_person_ids=target_ids,
            exif_orientation=asset.exifInfo.orientation if asset.exifInfo else None,
        )
