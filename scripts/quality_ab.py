"""Visual A/B test: the previous look (official + atkinson) against ``auto``.

For a dark photo, a bright photo, a movie poster and an album cover it renders both settings
for the same frame and writes side-by-side images (left = A, right = B): the composition
(``<case>_composed.jpg``) and the exact Spectra 6 simulation (``<case>_dithered.png``), with
what ``auto`` chose and why, and the render times. Nothing is sent to the frame.

Sources:

* photos: ``--dark PATH`` / ``--bright PATH``, else a CLIP search on the configured Immich
  (``IMMICH_URL`` / ``IMMICH_API_KEY`` in ``.env``), else the case is skipped;
* poster: TMDB (``TMDB_API_KEY``), falling back to a title card;
* album cover: Deezer, then MusicBrainz / Cover Art Archive.

Usage::

    python scripts/quality_ab.py                          # preview_gallery/quality-ab
    python scripts/quality_ab.py --dark night.jpg --bright beach.jpg --panel 315
"""

from __future__ import annotations

import argparse
import asyncio
import io
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw
from template_samples import build_store

from app.clients.http_images import ImageFetcher
from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.security import URLPolicy
from app.core.settings import EnvSettings
from app.processor.fit import FocusRegion
from app.processor.pipeline import (
    RenderOptions,
    RenderResult,
    decode_image,
    render,
    render_layout,
)
from app.processor.templates import AlbumLayout, Layout, PosterLayout, TitleCardLayout
from app.processor.typography import load_font
from app.providers.immich import (
    ImmichDisplayRequest,
    ImmichGateway,
    ImmichProvider,
    PeopleDirectory,
)
from app.providers.music import ArtworkResolver
from app.providers.tmdb import PosterResolver, TmdbProvider, TmdbRequest

CAPTION_HEIGHT = 96
GAP = 24
DARK_QUERY = "night, dark scene, low light"
BRIGHT_QUERY = "bright sunny beach, snow, white sky"


@dataclass(frozen=True)
class Case:
    """One image to compare."""

    name: str
    image: Image.Image | None
    layout: Layout | None = None
    regions: tuple[FocusRegion, ...] = ()


def run(case: Case, options: RenderOptions) -> tuple[RenderResult, float]:
    """Render ``case`` and return the result with its render time."""
    started = time.perf_counter()
    if case.layout is not None:
        result = render_layout(case.image, case.layout, options)
    elif case.image is not None:
        result = render(case.image, options, case.regions)
    else:  # pragma: no cover - cases always have an image or a layout
        raise ValueError(f"{case.name}: nothing to render")
    return result, time.perf_counter() - started


def side_by_side(
    left: Image.Image, right: Image.Image, captions: tuple[list[str], list[str]]
) -> Image.Image:
    """Two same-size images next to each other, each with a caption block underneath."""
    width, height = left.size
    sheet = Image.new("RGB", (2 * width + GAP, height + CAPTION_HEIGHT), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)
    font = load_font("regular", 22)
    for column, (image, lines) in enumerate(zip((left, right), captions, strict=True)):
        x = column * (width + GAP)
        sheet.paste(image, (x, 0))
        for row, line in enumerate(lines[:3]):
            draw.text((x + 8, height + 8 + row * 28), line, font=font, fill=(20, 20, 20))
    return sheet


async def immich_photo(
    store: ConfigStore, fetcher_policy: URLPolicy, query: str, max_bytes: int, tmp: Path
) -> tuple[Image.Image, tuple[FocusRegion, ...]] | None:
    """A photo found by a CLIP search on the configured Immich, or ``None``."""
    if not store.config.immich.accounts:
        return None
    gateway = ImmichGateway(store, fetcher_policy)
    history = HistoryStore(tmp / "history.json")
    history.load()
    provider = ImmichProvider(
        store, gateway, PeopleDirectory(store, gateway), history, max_bytes=max_bytes
    )
    request = ImmichDisplayRequest.model_validate(
        {
            "mode": "search",
            "clip_queries": [query],
            "ignore_history": True,
            "skip_quality_gate": True,
            "push_to_fraimic": False,
        }
    )
    try:
        asset = await provider.fetch(request)
    except Exception as exc:  # network, no match...: the case is simply skipped
        print(f"Immich search {query!r} failed: {exc}")
        return None
    return decode_image(asset.data), asset.regions


async def load_cases(args: argparse.Namespace, store: ConfigStore, tmp: Path) -> list[Case]:
    """Build the four cases from files, Immich, TMDB and Deezer."""
    env = EnvSettings()
    policy = URLPolicy()
    fetcher = ImageFetcher(policy, env.max_upload_bytes)
    cases: list[Case] = []
    try:
        for name, path, query in (
            ("dark photo", args.dark, DARK_QUERY),
            ("bright photo", args.bright, BRIGHT_QUERY),
        ):
            if path is not None:
                cases.append(Case(name, decode_image(path.read_bytes())))
                continue
            found = await immich_photo(store, policy, query, env.max_upload_bytes, tmp)
            if found is None:
                print(f"{name}: no file given and no Immich photo found; skipped")
                continue
            cases.append(Case(name, found[0], regions=found[1]))

        posters = PosterResolver(TmdbProvider(store, fetcher), fetcher, store, tmp)
        asset = await posters.resolve(TmdbRequest(query=args.poster, media_type="movie"))
        poster_image = decode_image(asset.data) if asset.data else None
        layout: Layout = asset.layout or PosterLayout()
        cases.append(Case(f"poster: {args.poster}", poster_image, layout))

        covers = ArtworkResolver(store, fetcher)
        try:
            artwork = await covers.find(title=args.track, artist=args.artist, album=args.album)
        finally:
            await covers.aclose()
        if artwork is not None:
            album: Layout = AlbumLayout(title=args.track, artist=args.artist, album=args.album)
            cases.append(Case(f"album: {args.album}", decode_image(artwork.data), album))
        else:
            card = TitleCardLayout(title=args.track, subtitle=args.artist, caption=args.album)
            cases.append(Case("album (title card)", None, card))
    finally:
        await fetcher.aclose()
    return cases


async def main(args: argparse.Namespace) -> None:
    """Render every case with the previous settings (A) and with ``auto`` (B)."""
    args.out.mkdir(parents=True, exist_ok=True)
    store = build_store(args.panel, args.orientation)
    config = store.config
    tmp = Path(tempfile.mkdtemp())
    cases = await load_cases(args, store, tmp)

    auto_eink = config.eink.model_copy(update={"profile": "auto", "dither": "auto"})
    official_eink = config.eink.model_copy(update={"profile": "official", "dither": "atkinson"})
    for case in cases:
        base = RenderOptions.from_config(config, fit="smart_crop")
        a_result, a_time = run(case, replace(base, eink=official_eink))
        b_result, b_time = run(case, replace(base, eink=auto_eink))
        decision = b_result.decision
        chosen = f"{decision.profile} + {decision.dither}" if decision else "?"
        reasons = decision.reasons if decision else []
        captions = (
            ["A: official + atkinson (previous default)", f"render {a_time:.2f} s"],
            [f"B: auto -> {chosen}", f"render {b_time:.2f} s", *(r[:70] for r in reasons)],
        )
        slug = "".join(c if c.isalnum() else "_" for c in case.name.lower()).strip("_")
        side_by_side(a_result.composed, b_result.composed, captions).save(
            args.out / f"{slug}_composed.jpg", quality=92
        )
        a_dither = Image.open(io.BytesIO(a_result.dithered_png())).convert("RGB")
        b_dither = Image.open(io.BytesIO(b_result.dithered_png())).convert("RGB")
        side_by_side(a_dither, b_dither, captions).save(args.out / f"{slug}_dithered.png")
        print(f"{case.name}: A {a_time:.2f}s | B {b_time:.2f}s | {chosen}")
        for reason in reasons:
            print(f"    {reason}")
    print(f"-> {args.out}")


def parse_args() -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("preview_gallery/quality-ab"))
    parser.add_argument("--panel", choices=["133", "315"], default=None)
    parser.add_argument("--orientation", choices=["portrait", "landscape"], default=None)
    parser.add_argument("--dark", type=Path, default=None, help="a dark photo (default: Immich)")
    parser.add_argument("--bright", type=Path, default=None, help="a bright photo (Immich)")
    parser.add_argument("--poster", default="Intouchables", help="movie title for TMDB")
    parser.add_argument("--track", default="So What")
    parser.add_argument("--artist", default="Miles Davis")
    parser.add_argument("--album", default="Kind of Blue")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
