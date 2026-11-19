"""Render a visual gallery: 8 movie posters and 8 album covers on all 4 frame layouts.

Frames: 13.3" and 31.5" panels, each in portrait and landscape. Artwork is looked up
**once** (TMDB posters at ``original`` size, covers sized for the largest slot) and rendered
for every frame, so all frames show the same sources. For each item and frame it writes the
composition (``.jpg``) and the exact Spectra 6 simulation (``_dithered.png``), plus contact
sheets per frame. Nothing is sent to the frame.

Usage::

    python scripts/preview_gallery.py                 # writes preview_gallery/ (gitignored)
    python scripts/preview_gallery.py --out some/dir
"""

from __future__ import annotations

import argparse
import asyncio
import io
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw
from template_samples import ALBUMS, MOVIES, Sample, build_store, save, slug

from app.clients.http_images import ImageFetcher
from app.core.security import URLPolicy
from app.core.settings import EnvSettings
from app.processor.pipeline import RenderOptions, render_layout_bytes
from app.processor.templates import (
    AlbumLayout,
    AlbumStyle,
    Canvas,
    Layout,
    PosterLayout,
    TitleCardLayout,
    album_cover_side,
)
from app.processor.typography import load_font
from app.providers.music import ArtworkResolver
from app.providers.music.artwork import Artwork
from app.providers.tmdb import PosterResolver, TmdbProvider
from app.providers.tmdb.provider import ImageAsset

FRAMES: tuple[tuple[str, str], ...] = (
    ("133", "portrait"),
    ("133", "landscape"),
    ("315", "portrait"),
    ("315", "landscape"),
)
THUMB_LONG = 420
SHEET_COLUMNS = 4
SHEET_PAD = 16
SHEET_CAPTION = 34


@dataclass(frozen=True)
class AlbumSource:
    """An album's metadata and its downloaded cover (``None`` = title card)."""

    title: str
    artist: str
    album: str
    style: AlbumStyle
    artwork: Artwork | None


def frame_name(panel: str, orientation: str) -> str:
    """Directory name of a frame layout."""
    return f"{panel}-{orientation}"


def sheet(samples: list[Sample], path: Path, *, dithered: bool) -> None:
    """A contact sheet whose cells follow the frame's aspect ratio."""
    first = samples[0].composed
    scale = THUMB_LONG / max(first.size)
    cell_w, cell_h = round(first.width * scale), round(first.height * scale)
    rows = (len(samples) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
    canvas = Image.new(
        "RGB",
        (
            SHEET_COLUMNS * (cell_w + SHEET_PAD) + SHEET_PAD,
            rows * (cell_h + SHEET_CAPTION + SHEET_PAD) + SHEET_PAD,
        ),
        (245, 245, 245),
    )
    draw = ImageDraw.Draw(canvas)
    font = load_font("regular", 16)
    for index, sample in enumerate(samples):
        image = (sample.dithered if dithered else sample.composed).resize(
            (cell_w, cell_h), Image.Resampling.LANCZOS
        )
        x = SHEET_PAD + (index % SHEET_COLUMNS) * (cell_w + SHEET_PAD)
        y = SHEET_PAD + (index // SHEET_COLUMNS) * (cell_h + SHEET_CAPTION + SHEET_PAD)
        canvas.paste(image, (x, y))
        draw.text((x, y + cell_h + 8), sample.label[:44], font=font, fill=(20, 20, 20))
    canvas.save(path, quality=90)


async def fetch_sources(
    fetcher: ImageFetcher,
) -> tuple[list[tuple[str, ImageAsset]], list[AlbumSource]]:
    """Look up every poster and cover once, sized for the largest frame."""
    store = build_store("315", "portrait")  # the largest poster slot (1296 px)
    await store.update({"tmdb": {"image_size": "original"}})
    provider = TmdbProvider(store, fetcher)
    posters = PosterResolver(provider, fetcher, store, Path(tempfile.mkdtemp()))
    covers = ArtworkResolver(store, fetcher)
    config = store.config
    canvas = Canvas(config.frame_size, config.eink, config.display.safe_margin_px)
    cover_side = max(album_cover_side(canvas, style) for style in ("sleeve", "vinyl"))
    print(f"TMDB {'configured' if provider.configured else 'NOT configured'}, size original")

    movie_assets: list[tuple[str, ImageAsset]] = []
    for request in MOVIES:
        asset = await posters.resolve(request)
        details = asset.details
        with_data = Image.open(io.BytesIO(asset.data)).size if asset.data else None
        print(f"poster: {asset.title} [{details.get('fallback') or 'tmdb'}] source {with_data}")
        movie_assets.append((request.query, asset))

    albums: list[AlbumSource] = []
    try:
        for title, artist, album, style in ALBUMS:
            artwork = await covers.find(
                title=title, artist=artist, album=album, min_side=cover_side
            )
            size = Image.open(io.BytesIO(artwork.data)).size if artwork else None
            origin = artwork.match.source if artwork else "title card"
            print(f"album: {artist} - {album} ({style}) [{origin}] source {size}")
            albums.append(AlbumSource(title, artist, album, style, artwork))
    finally:
        await covers.aclose()
    return movie_assets, albums


async def render_frame(
    panel: str,
    orientation: str,
    movies: list[tuple[str, ImageAsset]],
    albums: list[AlbumSource],
    out: Path,
) -> None:
    """Render every poster and album for one frame layout."""
    store = build_store(panel, orientation)
    options = RenderOptions.from_config(store.config, fit="contain", eink_profile="official")
    directory = out / frame_name(panel, orientation)
    directory.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []
    for index, (query, asset) in enumerate(movies, 1):
        layout: Layout = asset.layout or PosterLayout()
        result = await asyncio.to_thread(render_layout_bytes, asset.data or None, layout, options)
        label = asset.title or query
        samples.append(save(result, directory, f"poster_{index:02d}_{slug(query)}", label))
    for index, source in enumerate(albums, 1):
        if source.artwork is not None:
            layout = AlbumLayout(
                title=source.title, artist=source.artist, album=source.album, style=source.style
            )
            data: bytes | None = source.artwork.data
        else:
            layout = TitleCardLayout(
                title=source.title, subtitle=source.artist, caption=source.album
            )
            data = None
        result = await asyncio.to_thread(render_layout_bytes, data, layout, options)
        label = f"{source.artist} - {source.album} ({source.style})"
        samples.append(
            save(result, directory, f"album_{index:02d}_{slug(source.album)}_{source.style}", label)
        )
    posters, covers = samples[: len(movies)], samples[len(movies) :]
    for name, group in (("posters", posters), ("albums", covers)):
        sheet(group, directory / f"_sheet_{name}.jpg", dithered=False)
        sheet(group, directory / f"_sheet_{name}_dithered.jpg", dithered=True)
    print(f"{frame_name(panel, orientation)}: {options.frame_size} done")


async def main(out: Path) -> None:
    """Fetch the artwork once and render all four frame layouts."""
    out.mkdir(parents=True, exist_ok=True)
    fetcher = ImageFetcher(URLPolicy(), EnvSettings().max_upload_bytes)
    try:
        movies, albums = await fetch_sources(fetcher)
    finally:
        await fetcher.aclose()
    for panel, orientation in FRAMES:
        await render_frame(panel, orientation, movies, albums, out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("preview_gallery"))
    asyncio.run(main(parser.parse_args().out))
