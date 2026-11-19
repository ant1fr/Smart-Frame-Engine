"""Render sample movie posters and album covers with the composition templates.

Uses the real providers (TMDB with ``TMDB_API_KEY`` from ``.env``, Deezer, MusicBrainz /
Cover Art Archive) and the real eInk pipeline, but never contacts the frame. For each item
it writes the composition (``.jpg``, what ``/api/current.jpg`` would serve) and the exact
6-color panel simulation (``_dithered.png``), plus four contact sheets.

Usage::

    python scripts/template_samples.py                       # data/template-samples, 13.3" portrait
    python scripts/template_samples.py --panel 315 --orientation landscape --out data/samples-315

Without a TMDB key, posters fall back to title cards.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.clients.http_images import ImageFetcher
from app.core.config_store import ConfigStore
from app.core.security import URLPolicy
from app.core.settings import EnvSettings
from app.processor.pipeline import RenderOptions, RenderResult, render_layout_bytes
from app.processor.templates import (
    AlbumLayout,
    AlbumStyle,
    Canvas,
    Layout,
    TitleCardLayout,
    album_cover_side,
)
from app.processor.typography import load_font
from app.providers.music import ArtworkResolver
from app.providers.tmdb import PosterResolver, TmdbProvider, TmdbRequest

MOVIES: tuple[TmdbRequest, ...] = (
    TmdbRequest(query="Le Fabuleux Destin d'Amélie Poulain", media_type="movie", year=2001),
    TmdbRequest(query="Intouchables", media_type="movie", year=2011),
    TmdbRequest(query="Dune", media_type="movie", year=2021),
    TmdbRequest(query="Inception", media_type="movie", year=2010),
    TmdbRequest(query="Le Dîner de cons", media_type="movie", year=1998),
    TmdbRequest(query="Le Voyage de Chihiro", media_type="movie", year=2001),
    TmdbRequest(query="Interstellar", media_type="movie", year=2014),
    TmdbRequest(query="La La Land", media_type="movie", year=2016),
)
ALBUMS: tuple[tuple[str, str, str, AlbumStyle], ...] = (
    ("So What", "Miles Davis", "Kind of Blue", "sleeve"),
    ("Around the World", "Daft Punk", "Homework", "vinyl"),
    ("Bohemian Rhapsody", "Queen", "A Night at the Opera", "sleeve"),
    ("Get Lucky", "Daft Punk", "Random Access Memories", "vinyl"),
    ("Come Together", "The Beatles", "Abbey Road", "sleeve"),
    ("Dreams", "Fleetwood Mac", "Rumours", "vinyl"),
    ("Rehab", "Amy Winehouse", "Back to Black", "sleeve"),
    ("Nés sous la même étoile", "IAM", "L'École du micro d'argent", "vinyl"),
)

THUMB_SIZE = (300, 400)
SHEET_COLUMNS = 4
SHEET_PAD = 16
SHEET_CAPTION = 34


@dataclass(frozen=True)
class Sample:
    """One rendered item for the contact sheets."""

    label: str
    composed: Image.Image
    dithered: Image.Image


def slug(text: str) -> str:
    """A file-name friendly version of ``text``."""
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")[:40]


def save(result: RenderResult, out: Path, name: str, label: str) -> Sample:
    """Write the composition and the dithered simulation; return the sheet entry."""
    result.composed.save(out / f"{name}.jpg", quality=92)
    dithered = result.dithered_png()
    (out / f"{name}_dithered.png").write_bytes(dithered)
    return Sample(label, result.composed, Image.open(io.BytesIO(dithered)).convert("RGB"))


def contact_sheet(samples: list[Sample], path: Path, *, dithered: bool) -> None:
    """A grid of thumbnails with captions."""
    thumb_w, thumb_h = THUMB_SIZE
    rows = (len(samples) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
    sheet = Image.new(
        "RGB",
        (
            SHEET_COLUMNS * (thumb_w + SHEET_PAD) + SHEET_PAD,
            rows * (thumb_h + SHEET_CAPTION + SHEET_PAD) + SHEET_PAD,
        ),
        (245, 245, 245),
    )
    draw = ImageDraw.Draw(sheet)
    font = load_font("regular", 16)
    for index, sample in enumerate(samples):
        image = sample.dithered if dithered else sample.composed
        thumb = image.copy()
        thumb.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        x = SHEET_PAD + (index % SHEET_COLUMNS) * (thumb_w + SHEET_PAD)
        y = SHEET_PAD + (index // SHEET_COLUMNS) * (thumb_h + SHEET_CAPTION + SHEET_PAD)
        sheet.paste(thumb, (x, y))
        draw.text((x, y + thumb_h + 8), sample.label[:36], font=font, fill=(20, 20, 20))
    sheet.save(path, quality=90)


def build_store(panel: str | None, orientation: str | None) -> ConfigStore:
    """Runtime configuration from ``.env`` (scratch file, never ``/data/config.json``)."""
    env = EnvSettings()
    seed: dict[str, dict[str, Any]] = env.runtime_seed()
    if panel:
        seed.setdefault("fraimic", {})["panel"] = panel
    if orientation:
        seed.setdefault("display", {})["orientation"] = orientation
    store = ConfigStore(Path(tempfile.mkdtemp()) / "config.json", seed)
    store.load()
    return store


async def render_posters(
    resolver: PosterResolver, options: RenderOptions, out: Path
) -> list[Sample]:
    """Resolve and render every movie of :data:`MOVIES`."""
    samples: list[Sample] = []
    for index, request in enumerate(MOVIES, 1):
        started = time.perf_counter()
        asset = await resolver.resolve(request)
        if asset.layout is None:  # pragma: no cover - posters always carry a layout
            raise RuntimeError("Poster asset without a layout")
        result = await asyncio.to_thread(
            render_layout_bytes, asset.data or None, asset.layout, options
        )
        details = asset.details
        origin = details.get("fallback") or (
            f"tmdb {details.get('poster_language')}-{details.get('poster_region')} "
            f"{details.get('poster_size')} (original {details.get('poster_original_width')}px)"
        )
        title = asset.title or request.query
        samples.append(save(result, out, f"poster_{index:02d}_{slug(request.query)}", title))
        print(f"poster {index}: {title} [{origin}] {time.perf_counter() - started:.1f}s")
    return samples


async def render_albums(
    resolver: ArtworkResolver, store: ConfigStore, options: RenderOptions, out: Path
) -> list[Sample]:
    """Look up and render every album of :data:`ALBUMS`."""
    config = store.config
    canvas = Canvas(config.frame_size, config.eink, config.display.safe_margin_px)
    samples: list[Sample] = []
    for index, (title, artist, album, style) in enumerate(ALBUMS, 1):
        started = time.perf_counter()
        side = album_cover_side(canvas, style)
        artwork = await resolver.find(title=title, artist=artist, album=album, min_side=side)
        layout: Layout
        data: bytes | None
        if artwork is not None:
            layout = AlbumLayout(title=title, artist=artist, album=album, style=style)
            data = artwork.data
            with Image.open(io.BytesIO(data)) as cover:
                origin = f"{artwork.match.source}: {artwork.match.album} {cover.size[0]}px"
        else:
            layout = TitleCardLayout(
                title=title, subtitle=artist, caption=album, accent=(0, 0, 255)
            )
            data, origin = None, "title card"
        result = await asyncio.to_thread(render_layout_bytes, data, layout, options)
        label = f"{artist} - {album} ({style})"
        samples.append(save(result, out, f"album_{index:02d}_{slug(album)}_{style}", label))
        print(
            f"album {index}: {label} [{origin}, slot {side}px] {time.perf_counter() - started:.1f}s"
        )
    return samples


async def main(out: Path, panel: str | None, orientation: str | None) -> None:
    """Render all samples into ``out``."""
    out.mkdir(parents=True, exist_ok=True)
    store = build_store(panel, orientation)
    config = store.config
    options = RenderOptions.from_config(config, fit="contain", eink_profile="official")
    fetcher = ImageFetcher(URLPolicy(), EnvSettings().max_upload_bytes)
    provider = TmdbProvider(store, fetcher)
    posters = PosterResolver(provider, fetcher, store, Path(tempfile.mkdtemp()))
    covers = ArtworkResolver(store, fetcher)
    print(
        f"Frame {options.frame_size} (panel {config.fraimic.panel}), TMDB "
        f"{'configured' if provider.configured else 'NOT configured'} "
        f"({config.tmdb.language}, size {config.tmdb.image_size}, poster slot "
        f"{provider.target_width()}px) -> {out}"
    )
    try:
        poster_samples = await render_posters(posters, options, out)
        album_samples = await render_albums(covers, store, options, out)
    finally:
        await covers.aclose()
        await fetcher.aclose()
    for name, samples in (("posters", poster_samples), ("albums", album_samples)):
        contact_sheet(samples, out / f"_sheet_{name}.jpg", dithered=False)
        contact_sheet(samples, out / f"_sheet_{name}_dithered.jpg", dithered=True)


def parse_args() -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("data/template-samples"))
    parser.add_argument("--panel", choices=["133", "315"], default=None)
    parser.add_argument("--orientation", choices=["portrait", "landscape"], default=None)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(main(arguments.out, arguments.panel, arguments.orientation))
