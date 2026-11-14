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
about 0.7 s (13.3") / 1.1 s (31.5") instead of ~40 s.

Generated files (`./data/cache/`):

| File | Endpoint | Content |
| --- | --- | --- |
| `current.jpg` | `GET /api/current.jpg` | Enhanced composition, before dithering (HA Generic Camera) |
| `current_dithered.png` | `GET /api/current_dithered.png` | Exact 6-color preview of the panel |
| `current.bin` | `GET /api/current.bin` | Frame buffer sent to the frame (960,000 / 2,304,000 bytes) |

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
