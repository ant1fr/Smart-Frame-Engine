# Smart Frame Engine

Self-hosted web app and REST API that fetches images from modular sources (Immich,
TMDB movie/TV posters, direct URLs), optimizes them for a color eInk panel (Spectra 6),
and pushes them to a [Fraimic](https://fraimic.com) picture frame. Home Assistant
friendly, runs standalone in Docker.

> **Status:** early development (`v0.x`). The core service, the eInk pipeline and frame
> delivery (continuous and deep-sleep) are in place; image providers and the web UI are
> being added milestone by milestone.

## Quickstart

```bash
cp .env.example .env        # then fill in your values
mkdir -p data               # must be writable by PUID:PGID (default 1000:1000)
docker compose up -d
```

- API docs: `http://<host>:8000/docs`
- Health: `http://<host>:8000/api/health`

Runtime settings are stored in `./data/config.json` and can be edited with
`GET`/`PUT /api/config`. Values saved there take precedence over `.env`, which in turn
takes precedence over built-in defaults. `POST /api/config/reset` removes saved values.

## eInk pipeline

Images are fitted to the panel (`smart_crop` around faces, or `contain` on a blurred /
black / white background), enhanced (brightness 1.1, contrast 1.2, saturation 1.2,
edge-enhance, smooth, sharpen) and dithered onto the 6-color Spectra 6 palette with
the official tuned color metric. Dithering is vectorized along anti-diagonals: the
Atkinson output is byte-identical to the official Fraimic converter while running in
about 0.7 s (13.3") / 1.1 s (31.5") instead of ~40 s. Landscape mode rotates the image
into the panel's portrait buffer *before* dithering, like the official `--fit rotate`,
so its `.bin` is byte-identical too. HEIC/HEIF (iPhone) images decode natively.

**Photos vs the official look.** The official settings (contrast and saturation +20%,
edge-enhance/sharpen chain) suit posters and graphics, but make dim indoor photos look
outlined, over-saturated and grainy on the panel. Set `eink.profile` to `photo` for a softer
result (no edge filters, saturation 0.85, sharpness 0.8, brightness and contrast 1.0; dithering
stays as configured). `official` (default) keeps the values of the `eink` fields, whose
defaults are the official converter's. Tested on a real 13.3" panel: lifting brightness,
white-balance correction and dither jitter did not improve on it.

If a mat or bezel hides the panel edges, set `display.safe_margin_px` (default `0`):
the picture is inset by that many pixels on every side and the margin is filled with
`display.contain_background`.

Generated files (`./data/cache/`):

| File | Endpoint | Content |
| --- | --- | --- |
| `current.jpg` | `GET /api/current.jpg` | Enhanced composition, before dithering (HA Generic Camera) |
| `current_dithered.png` | `GET /api/current_dithered.png` | Exact 6-color preview of the panel |
| `current.bin` | `GET /api/current.bin` | Frame buffer sent to the frame (960,000 / 2,304,000 bytes) |

## Immich

Photos come from one or several [Immich](https://immich.app) accounts. Pinned against
Immich 3.3.5 (`/api/spec.json`); payloads are parsed leniently.

```bash
# Quickstart: one account from .env (named "default")
IMMICH_URL=http://immich.lan:2283
IMMICH_API_KEY=...

# A random photo with Alice and Bob together
curl -X POST http://localhost:8000/api/display/immich -H 'Content-Type: application/json' \
  -d '{"persons": ["Alice", "Bob"], "match_mode": "all"}'

# CLIP search, favorites, or an album
curl -X POST .../api/display/immich -H 'Content-Type: application/json' -d '{"mode": "search", "clip_queries": ["snowy mountains"]}'
curl -X POST .../api/display/immich -H 'Content-Type: application/json' -d '{"mode": "favorites"}'
curl -X POST .../api/display/immich -H 'Content-Type: application/json' -d '{"mode": "album", "album_name": "Holidays"}'
```

| Request field | Meaning |
| --- | --- |
| `mode` | `random` (default), `album`, `favorites` or `search` (CLIP, needs `clip_queries`) |
| `persons`, `match_mode` | Plain person **names** (never Home Assistant entity IDs); `any` or `all` of them |
| `always_include` | Names merged into every selection (e.g. a child without an HA account). Defaults to `immich.always_include`; send `[]` to switch it off |
| `album_name` / `album_id` | Restrict to an album (names are case-insensitive) |
| `accounts` | Limit to some accounts |
| `fit`, `push_to_fraimic` | Fit override, push to the frame (default true) |
| `ignore_history` | Allow photos shown within `anti_repeat_days` or taken close to one |
| `skip_quality_gate` | Skip the suitability check (default: skipped in `album` mode, checked otherwise) |

Unset filters fall back to the `immich` section of the settings. All modes share one
query path, so persons, albums and favorites combine freely.

**Accounts.** `immich.accounts` is a named map (`url` + `api_key`); `IMMICH_URL` /
`IMMICH_API_KEY` seed an account called `default` (saved settings win over `.env`). API keys
are masked in `GET /api/config`, and sending the masked view back keeps the stored key.

```json
{"immich": {
  "accounts": {"home": {"url": "http://immich.lan:2283", "api_key": "..."},
               "partner": {"url": "https://photos.example.org", "api_key": "..."}},
  "always_include": ["Kid"],
  "person_aliases": {"Marie": {"partner": ["Maman", "0b7e1c3a-0000-0000-0000-000000000000"]}}
}}
```

**People.** Names are resolved per account from `GET /api/immich/people` (cached for
`people_cache_ttl_s`, refresh with `POST /api/immich/people/refresh`). Two people with the
same name in one account are rejected with a hint to pin an ID in `person_aliases`;
unknown names get "did you mean" suggestions. `person_aliases` maps a canonical name to
IDs or names that exist in a given account.

**Selection.** Candidates of all accounts are merged, de-duplicated by content checksum
(the same photo in two accounts counts once) and filtered by the anti-repeat history
(`immich.anti_repeat_days`, shared across accounts), then one is picked at random. The
original is downloaded at full resolution (HEIC works); RAW files and files larger than
`MAX_UPLOAD_MB` use Immich's converted JPEG (`fullsize`, then `preview`).

**Quality gate.** Rather than force an unsuitable image onto a 6-color panel, photos are
vetted *before* the full-size download, using a ~250 px thumbnail and metadata (no pixels
are ever modified; about 10 ms of NumPy per candidate, and normally one candidate is
enough). Rejected: blank/near-black frames, tiny images (short side below
`quality.min_dimension`, default 600 px), screenshots and graphics (no camera EXIF *and* a
large bright flat area or a tall phone-screen shape) and document scans. Kept on purpose:
black-and-white portraits, night shots, scanned family photos and low-contrast snow
scenes. Thresholds come from a calibration on ~400 real photos (4.3% rejected) and live in
the `quality` settings section. If none of `quality.candidates_checked` (default 8)
passes, the rules relax in four steps (halve the size limit, drop the phone-shape and
document rules, keep only blank and flat-color screenshots, keep only blank) so a request
never dead-ends; the outcome is reported in the image details under `suitability`
(`level`, `checked`, `rejected`). A thumbnail that cannot be read never blocks a photo.

**Burst cooldown.** The history stores each photo's capture time. After a photo was
shown, others taken within `immich.burst_cooldown_hours` (default 2) of it are skipped for
`anti_repeat_days`, so rapid-fire shots of one moment are not shown back-to-back
(`0` disables it; `ignore_history` bypasses it).

**Face-aware crop.** Face boxes of the requested people drive `smart_crop` (all faces are
the fallback), padded by 25% for hair and shoulders. When two faces are too far apart for
the crop window, the larger one is kept whole instead of slicing both. Edited Immich assets
are cropped without face boxes.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/display/immich` | Select, render, deliver and record a photo |
| `GET /api/immich/albums` | Albums across accounts (`?accounts=`) |
| `GET /api/immich/people` / `POST /api/immich/people/refresh` | Resolved people per account, ambiguity and alias mapping |

`GET /api/immich/*` requires the API key when `APP_API_KEY` is set (it lists family member
names). A LAN Immich needs `URL_ALLOWLIST` when `BLOCK_PRIVATE_URLS=true`.

## Frame delivery

The frame is driven through its local REST API (`GET /api/info`, `POST /api/image`).

- **`continuous`** (mains powered): every new image is pushed immediately; failures are
  retried with exponential backoff.
- **`deepsleep`** (battery): the image is queued in `./data/queue/` and pushed as soon as
  the frame answers (probed every `poll_interval_s`), or immediately when Home Assistant
  calls `POST /api/fraimic/wakeup`.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/display/upload` | Upload an image (multipart `file`, optional `fit`, `push_to_fraimic`) |
| `POST /api/fraimic/push` | Force push the current image now (200 delivered, 202 queued) |
| `POST /api/fraimic/wakeup` | Frame is awake: flush the queue (202, or `?wait=true` for the outcome) |
| `GET /api/fraimic/info` | Last known battery / Wi-Fi / firmware (`?refresh=true` queries the frame) |
| `GET /api/status` | Frame telemetry, queue state and current image metadata |

## Development

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy app && pytest
DATA_DIR=./data python -m app      # serve on http://localhost:8000
```

## Credits

The palette, color metric, Atkinson kernel and `.bin` layouts follow the official
[Fraimic Bin Converter](https://github.com/Fraimic/fraimic_bin_converter) (MIT), which
builds on Toon-nooT's
[PhotoPainter Spectra 6 converter](https://github.com/Toon-nooT/PhotoPainter-E-Ink-Spectra-6-image-converter).

## License

[MIT](LICENSE)
