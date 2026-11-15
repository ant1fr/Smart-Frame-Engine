# Project: Smart Frame Engine (Immich / Media -> Fraimic & Home Assistant)

## Role & Objective
You are a senior software architect specializing in Python, Docker, lightweight web interfaces, and home automation (Home Assistant, Immich).
You are building a standalone, containerized web application and REST API capable of fetching images from modular sources (Immich as primary, movie/TV show posters via TMDB, direct URLs), optimizing them for a color eInk display, pushing them to a Fraimic eInk picture frame, and providing a clean web UI for control and configuration.

## Mandatory Tech Stack
- **Backend:** Python 3.11+, FastAPI, Uvicorn, Pydantic v2, httpx (async).
- **Image Processing:** Pillow (PIL) and NumPy (fast dithering).
- **Standalone Scheduler:** APScheduler (to run autonomously without Home Assistant if desired).
- **Persistence:** Local JSON files in a Docker volume `/data/config.json` and `/data/history.json`.
- **Frontend (UI):** Single-page web app served by FastAPI (`templates/index.html`) using Tailwind CSS (CDN) and Alpine.js (CDN) — no Node.js build step.
- **Deployment:** Lightweight `Dockerfile` (`python:3.11-slim`) + `docker-compose.yml` with `/data` volume and environment variables via `.env`.

## Architecture & Features

### 1. Extensible Image Sources (`providers/`)
Create an abstract base class `BaseImageProvider` returning a standardized `ImageAsset` object (raw image bytes, metadata, recommended fit mode, regions of interest / face bounding boxes).
Implement two providers initially:
- **`ImmichProvider`:**
  - Connect via `IMMICH_URL` and `IMMICH_API_KEY`.
  - Combinable filters: random selection, list of person names (`persons=["Alice", "Bob"]` resolving names to Immich `personIds` with `any` or `all` match mode), filter by `album` name, favorites only (`is_favorite`), exclude archived assets.
  - Extract metadata (capture date, location) and face `boundingBox` coordinates for smart cropping.
  - Configurable anti-repeat history (avoid displaying the same photo within X days).
- **`MediaPosterProvider` (HD Movie & TV Posters):**
  - Query the free TMDB API (`TMDB_API_KEY`) using title and media type (`movie`, `tv`, or `auto`), prioritizing the configured language (`fr-FR` or `en-US`) and highest resolution (`w1280` or `original`).
  - Automatic fallback to a provided `fallback_url` (e.g., Home Assistant `entity_picture`) if no TMDB match is found.
  - Automatically enforce `contain` fit mode so posters are never cropped.
- Structure `providers/` cleanly to make adding future providers effortless (e.g., `UnsplashProvider`, `LocalFolderProvider`).

### 2. Image Processing & eInk Pipeline (`processor/`)
- **Fit Modes:**
  - `smart_crop`: Intelligent crop centered on face bounding boxes detected by Immich, fitted to target frame dimensions (`FRAME_WIDTH`, `FRAME_HEIGHT`, `ORIENTATION=landscape|portrait`).
  - `contain`: Full image display with adaptive padding or blurred background (ideal for movie/TV posters).
- **eInk Optimization:** Configurable contrast, saturation, and sharpness adjustments.
- **Fraimic Binary Encoding:** Implement the exact `.bin` binary format and error-diffusion dithering with the official tuned color metric: `eink.profile` / `eink.dither` default to `auto` (decided per image from its context and cheap statistics; explicit values always win), with `atkinson` kept byte-identical to the official converter, plus `atkinson-classic`, `fs`, `fs-serpentine`, `stucki` and `blue-noise` on the official Fraimic color palette, strictly following the reference documentation and official converter code located in `/fraimic-docs` (specifically `/fraimic-docs/fraimic_bin_converter`).
- **Cache:** Save the latest processed image in `/data/cache/`: `current.jpg` (clean composition for the web UI and Home Assistant), `current_dithered.png` (exact 6-color preview), `current.bin` (for Fraimic) and `current.json` (metadata, written last); `base.*` keeps the last pushed non-ephemeral image (restored when an ephemeral one expires or is cancelled) and `preview.*` the latest render (`push_to_fraimic=false` only writes the preview). The deep-sleep upload queue lives in `/data/queue/` (`pending.bin` + `pending.json`); the last delivery is persisted in `/data/last_delivery.json`.

### 3. Fraimic Communication & Power Management (`clients/fraimic.py`)
Support two configurable power modes via the UI and `.env` (`FRAIMIC_POWER_MODE`):
- **`continuous` mode (Mains-powered / Always-On):**
  - Frame is always reachable. Every newly generated image is pushed immediately (`POST /api/image` on the frame, raw `.bin` as `application/octet-stream`).
- **`deepsleep` mode (Battery-powered / Power-saving):**
  - Frame turns off Wi-Fi during deep sleep and wakes up periodically or on button press.
  - When a new image is ready, queue it (`pending_upload = True`) without blocking the API.
  - Run a lightweight background worker (and expose a wake-up webhook `POST /api/fraimic/wakeup` for Home Assistant) that checks frame availability (lightweight HTTP ping to the frame's `GET /api/info` every X seconds while an upload is pending) and automatically pushes `current.bin` as soon as the frame responds.
- **Autonomous Scheduler:** Configurable periodic auto-refresh (every X hours, with a quiet-hours night window) controllable from the UI.

### 4. Built-in Web UI (`GET /`)
Clean, responsive (mobile/desktop) English web interface allowing users to:
- Preview the currently active image, view its metadata (source, date, location, people/title), and check connection status (Immich, TMDB, Fraimic).
- Manually trigger actions: "Next Photo" button, quick filter by person or album, manual movie/TV poster search, local image file upload, and "Force Push to Fraimic" button.
- Configure all application settings without editing files manually (persisted to `/data/config.json`).

### 5. REST API (`GET /docs`)
- `POST /api/display/immich`: Select and display an Immich photo (`mode` random|album|favorites|search, `persons` as plain names, `match_mode`, `always_include`, `album_name`/`album_id`, `clip_queries`, `accounts`, `fit`, `push_to_fraimic`, `ignore_history`, `skip_quality_gate`). Photos are vetted by a thumbnail-based quality gate (`quality` settings section) and a burst cooldown before the original is downloaded.
- `POST /api/display/smart-search`: Contextual push (`query`/`queries`, `persons`, `on_this_day`, `taken_after`/`taken_before`, `ephemeral` default true, `hold_minutes`) without Immich IDs.
- `GET /api/smart-mix`, `POST`/`DELETE /api/smart-mix/context`: Inspect the weighted Smart Mix playlist (`smart_mix` settings section; source-agnostic `kind` + `params` entries) and set/clear a temporary boost. Rule precedence for scheduled refreshes and `POST /api/refresh`: pinned `scheduler.source_rule` > Smart Mix > last non-ephemeral manual display. Direct pushes (`ephemeral`, `hold_minutes`) reset the timer without altering the playlist. `/api/smart-mix` is API-key protected for every method.
- `GET /api/immich/albums`, `GET /api/immich/people`, `POST /api/immich/people/refresh`: Discovery across the configured Immich accounts (`immich.accounts`; keys are masked and `GET /api/immich/*` is API-key protected when `APP_API_KEY` is set).
- `POST /api/display/tmdb`: Search and display an HD movie/TV poster with the no-crop `poster` template (`query`/`title`, `media_type` movie|tv|multi, `year`, `season`, `language`, `fallback_url`/`fallback_base64`, `push_to_fraimic`, `ephemeral` default true, `ttl_minutes`, `hold_minutes`). Regional poster ranking (`tmdb.language`), fallbacks: HA picture -> `/data/assets/tmdb_fallback.png` -> title card. `POST /api/display/media` is a deprecated alias. `tmdb` is a replayable runner kind.
- `POST /api/display/now-playing`: Album cover with the `album` template (`title`, `artist`, `album`, `artwork_url`/`artwork_base64`, `enrich_artwork` via Deezer then MusicBrainz/Cover Art Archive, `style` sleeve|vinyl, `duration_seconds`/`position_seconds`/`ttl_seconds`, `hold_minutes`, `push_to_fraimic`). Ephemeral by default; the same track is deduplicated (expiry extended only).
- `GET /api/queue` & `DELETE /api/queue/ephemeral`: Inspect the upload queue and cancel the ephemeral image. Ephemeral images carry `expires_at`; on expiry/cancel the last pushed non-ephemeral image (`/data/cache/base.*`) is restored and re-sent only if the frame may not show it.
- `GET /api/preview.jpg` & `GET /api/preview_dithered.png`: Latest render; `push_to_fraimic=false` only updates the preview (`POST /api/fraimic/push?from_preview=true` sends it).
- `POST /api/fraimic/wakeup`: Webhook signaling frame wake-up to flush any queued upload immediately.
- `POST /api/refresh`: Re-run the currently active source rule.
- `GET /api/current.jpg` & `GET /api/current.bin`: Serve generated files (compatible with Home Assistant *Generic Camera*).
- `GET /api/config` & `PUT /api/config`: Read and update runtime configuration.
- `GET /api/status`: Overall system status, queue state, and active image metadata.

## Development Constraints
- All code, comments, docstrings, UI text, commit messages, and documentation must be written in **English**.
- Produce clean, strictly typed (`typing`), modular, well-documented code.
- Always provide complete file implementations (never use `# TODO` or `...` placeholders).
- Include a ready-to-use Home Assistant integration guide (`Generic Camera` + YAML automation examples for presence detection and TV media playback).

## Git & Gitflow Workflow
- **Branches:**
  - `main`: Stable, production-ready code / tagged releases. Never commit directly to `main`.
  - `develop`: Main integration branch.
  - `feat/<name>`: New features (e.g., `feat/core-setup`, `feat/eink-fraimic`, `feat/providers-api`, `feat/web-ui`).
  - `fix/<name>`: Bug fixes.
  - `docs/<name>`: Documentation updates.
- **Versioning:**
  - `vX`: Major version (new core architecture, new provider, breaking changes). Start at `v0`.
  - `vX.Y`: Minor version (enhancements, minor features). Start at `v0.1`.
  - `vX.Y.Z`: Hotfix patch.
- **Commits:** Strictly follow **Conventional Commits** in English (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`).
- **Cadence:** Make atomic commits at each validated milestone.

## Security, Privacy & Reverse Proxy Compatibility
- **Zero Hardcoded Secrets & Clean Logs:** Never commit API keys, personal local IPs, or passwords. Use `.env` (documented in `.env.example`) and `/data/config.json` (gitignored). Mask tokens/API keys in logs.
- **Adaptive Authentication (`APP_API_KEY`):**
  - If `APP_API_KEY` is empty (default for pure LAN usage), API and UI require no authentication.
  - If `APP_API_KEY` is set, all `POST`/`PUT`/`DELETE` routes and `/api/config` require the `X-API-Key` header (or `?api_key=` query parameter).
  - The UI (`index.html`) must detect `401` responses and display a modal prompting for the API key, storing it in `localStorage`.
  - Support `PUBLIC_IMAGE_ENDPOINTS=true` (default `true`) so `/api/current.jpg` and `/api/current.bin` remain readable without headers (required for Home Assistant *Generic Camera* and Fraimic pull), while accepting `?api_key=` when set to `false`.
- **SSRF & Upload Protection:** Validate external URLs (`http`/`https` only, block unauthorized private/local IP ranges if configured), enforce a 20 MB file size limit, and verify real image magic bytes via Pillow.
- **Reverse Proxy Compatibility (Nginx Proxy Manager, Traefik, Caddy, Cloudflare):**
  - Run Uvicorn with `--proxy-headers` and `FORWARDED_ALLOW_IPS` (default `*`) to properly handle `X-Forwarded-Proto` and `X-Forwarded-For`.
  - Support optional `ROOT_PATH` env var (e.g., `/fraimic`) passed to `FastAPI(root_path=...)` so `/docs` and OpenAPI work under subpaths.
  - Use strictly relative paths (`./api/...`) in the frontend (`index.html`) to avoid broken links behind proxies.
  - Configure FastAPI CORS middleware via `CORS_ORIGINS` (default `*`).

## GitHub Standards, Documentation & Quality
- **Community & Repo Files:** Include `LICENSE` (MIT), `.gitignore` (ignoring `.env`, `/data/`, `/fraimic-docs/`, `__pycache__/`, `.venv/`, `*.bin`, `*.jpg`), `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`, and `.github/ISSUE_TEMPLATE/` (bug report + new provider request).
- **CI (`.github/workflows/ci.yml`):** Run `ruff check .` and `pytest` on every push/PR targeting `develop` and `main`.
- **Docker CD (`.github/workflows/release.yml`):** On tag push `v*`, build and push multi-arch Docker images (`linux/amd64,linux/arm64`) to `ghcr.io/${{ github.repository }}`.
- **Code & Tests:** Strict Python type hints, `ruff`-compliant formatting, and `pytest` unit tests with mocked HTTP calls (`respx` or `pytest-httpx`) covering Immich/TMDB providers and `.bin` conversion.
- **Comprehensive `README.md` in English including:**
  - Project overview + ASCII architecture diagram.
  - Quickstart guide (`docker-compose.yml` + `.env.example`).
  - Environment variables reference table.
  - Step-by-step Home Assistant setup guide (`Generic Camera` + ready-to-copy YAML automations).
  - Developer guide: *"How to add a new Image Provider in 3 steps"*.
  - Credits referencing the official [Fraimic Bin Converter](https://github.com/Fraimic/fraimic_bin_converter).

## Model & Effort Strategy (Claude Code)
Before starting each task or new Gitflow branch, explicitly recommend which Claude model (`/model`) and effort level to activate based on this matrix:
- **Opus + High Effort:** Initial architecture, `.bin` binary encoding and eInk dithering algorithm, complex async workers (`deepsleep` queue), critical bug debugging.
- **Sonnet + Medium Effort:** Implementing providers (`ImmichProvider`, `MediaPosterProvider`), FastAPI endpoints, web UI (`index.html`), `pytest` unit test suites.
- **Sonnet + Low Effort (or Haiku):** Writing `README.md`, Home Assistant YAML examples, GitHub Actions workflows, issue templates, minor style fixes.