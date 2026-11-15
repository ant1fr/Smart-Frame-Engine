# Smart Frame Engine

Self-hosted web app and REST API that fetches images from modular sources (Immich,
TMDB movie/TV posters, direct URLs), optimizes them for a color eInk panel (Spectra 6),
and pushes them to a [Fraimic](https://fraimic.com) picture frame. Home Assistant
friendly, runs standalone in Docker.

> **Status:** early development (`v0.x`). The core service, the eInk pipeline, frame
> delivery (continuous and deep-sleep), the Immich provider, TMDB posters, now-playing
> album covers, ephemeral images and the autonomous scheduler are in place; the web UI is
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
| `preview.jpg`, `preview_dithered.png` | `GET /api/preview.jpg`, `GET /api/preview_dithered.png` | The latest render, including `push_to_fraimic=false` ones |
| `base.*` | - | The last pushed non-ephemeral image, restored after an [ephemeral image](#ephemeral-images-and-the-queue) |

`current.*` always matches what the frame shows (or will show once a sleeping frame wakes
up). A render with `push_to_fraimic=false` only updates `preview.*`, so a template can be
inspected without touching the frame or Home Assistant's camera;
`POST /api/fraimic/push?from_preview=true` then sends it. (Before this release,
`push_to_fraimic=false` replaced `current.*`.)

### Composition templates

Posters and album covers are never cropped. Instead of a fit mode they use a template that
lays the artwork out for the frame (portrait or landscape, 13.3" or 31.5"), always with the
`official` eInk profile:

- **`poster`** - the full poster centered over a blurred, darkened backdrop made from the
  poster itself, with a thin light border and a soft shadow.
- **`album`** - the full square cover (non-square art is padded, not cropped) with the title,
  artist and album underneath (portrait) or beside it (landscape), on a flat background
  color sampled from the cover. `style: "vinyl"` slides a record out of the sleeve.
- **`title_card`** - a clean typographic card, used when no artwork can be found.

Text uses the bundled [Inter](https://rsms.me/inter/) font (never Pillow's bitmap
default), wraps and shrinks to fit, ends with an ellipsis when it really is too long, and is
drawn in pure black or white, whichever contrasts more with the background. It is rendered
without anti-aliasing, so glyphs stay a single panel color after dithering instead of
picking up colored fringes.
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
| `taken_after`, `taken_before` | Only photos taken in this range (ISO 8601 with a timezone) |
| `on_this_day` | Memories: `{"window_days": 3, "years_back": 10, "min_years_ago": 1}` around today's date in past years (exclusive with the date range) |
| `ephemeral`, `hold_minutes` | Temporary push: do not remember it as the rule to replay; keep it up at least this long (see [Smart Mix](#smart-mix-and-smart-search)) |

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
`immich.burst_cooldown_days` (default 3), so rapid-fire shots of one moment are not shown
back-to-back while other photos of the same trip can still appear later (`0` for either
setting disables it; `ignore_history` bypasses it). The exact-repeat memory stays
`anti_repeat_days` (default 30).

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
| `POST /api/display/upload` | Upload an image (multipart `file`, optional `fit`, `push_to_fraimic`, `ephemeral`, `hold_minutes`) |
| `POST /api/fraimic/push` | Force push the current image now (200 delivered, 202 queued); `?from_preview=true` sends the latest preview |
| `POST /api/fraimic/wakeup` | Frame is awake: flush the queue (202, or `?wait=true` for the outcome) |
| `GET /api/fraimic/info` | Last known battery / Wi-Fi / firmware (`?refresh=true` queries the frame) |
| `GET /api/status` | Frame telemetry, queue state, current image metadata and scheduler state |

## Scheduler

The frame can update itself without Home Assistant. The scheduler replays the **active
rule** every `scheduler.interval_hours` hours (`REFRESH_INTERVAL_HOURS`, default 6, minimum
0.25) once `scheduler.enabled` is on (`SCHEDULER_ENABLED=true`):

- **Active rule**, in order of precedence: (1) the rule pinned in `scheduler.source_rule`;
  (2) the [Smart Mix](#smart-mix-and-smart-search) playlist, when enabled and non-empty;
  (3) the last non-ephemeral request made through `POST /api/display/immich` (pushed to the
  frame; previews with `push_to_fraimic=false` do not count), so showing "Alice and Bob
  at the beach" once keeps rotating through those photos with the anti-repeat history.
  Pin a fixed rule in `scheduler.source_rule` to override the others:

  ```bash
  curl -X PUT http://localhost:8000/api/config -H 'Content-Type: application/json'     -d '{"scheduler": {"enabled": true, "source_rule":
         {"kind": "immich", "params": {"mode": "favorites", "persons": ["Alice"]}}}}'
  ```

  `params` takes the fields of `POST /api/display/immich` (except `push_to_fraimic` and
  `ignore_history`, `ephemeral`, `hold_minutes`), or of `POST /api/display/tmdb` for
  `"kind": "tmdb"` rules. The last rule is kept in `./data/scheduler_state.json`, so
  displaying a photo never rewrites your settings.
- **Timer.** Any display (a manual photo, an upload, a refresh) restarts the interval, so
  a picture chosen by hand stays up for a full interval. A refresh that became due while
  the app was down runs about 30 s after startup.
- **Quiet hours.** `QUIET_HOURS_START` / `QUIET_HOURS_END` (24h `HH:MM`, may cross
  midnight, e.g. `22:00` -> `07:00`) in the timezone `TZ` (IANA, e.g. `Europe/Paris`; the
  container otherwise runs in UTC). A refresh due inside the window runs when it ends;
  daylight saving changes are handled.
- **Failures.** If Immich is down or nothing matches, the refresh is retried after
  `scheduler.retry_minutes` (default 15), doubling each time, capped by the interval.
- **Sleeping frames.** With `scheduler.skip_if_pending` (default on), a scheduled refresh
  is skipped while the previous image still waits in the deep-sleep queue, so an image the
  frame never showed is not replaced.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/refresh` | Replay the active rule now and push the result (ignores quiet hours; 409 when there is nothing to replay or a refresh is running) |
| `GET /api/status` | `scheduler`: `next_run_at`, `in_quiet_hours`, `last_run` (`ok`/`failed`/`skipped` and why), `consecutive_failures`, `active_rule` (`pinned`, `smart_mix` or `last`), `hold_until`, `smart_mix` |

```yaml
# Home Assistant: a "next photo" button
rest_command:
  frame_next_photo:
    url: http://smart-frame.lan:8000/api/refresh
    method: POST
    timeout: 60   # selecting, downloading and dithering a photo takes a few seconds
    headers:
      X-API-Key: !secret smart_frame_api_key   # only when APP_API_KEY is set
```

## Smart Mix and Smart Search

**Smart Mix** is a weighted playlist of source rules for scheduled refreshes, so the frame
shows a varied rotation (family, favorites, "this season", memories...) instead of one
filter. It knows nothing about providers: each entry wraps a rule (`kind` + `params`, the
body of the matching display endpoint), and the runner registered for that `kind` plays it.
Adding a provider later needs no change to the configuration.

```bash
curl -X PUT http://localhost:8000/api/config -H 'Content-Type: application/json' -d '{
  "scheduler": {"enabled": true, "interval_hours": 3},
  "smart_mix": {"enabled": true, "entries": [
    {"label": "Family", "kind": "immich", "weight": 5,
     "params": {"persons": ["Alice", "Bob"], "match_mode": "any"}},
    {"label": "This season", "kind": "immich", "weight": 2,
     "params": {"mode": "search", "clip_queries": ["{season} landscape", "{month_name} light"]}},
    {"label": "On this day", "kind": "immich", "weight": 1,
     "params": {"on_this_day": {"window_days": 3, "years_back": 10}}}
  ]}
}'
```

- **Weights** are relative. Each refresh orders the enabled entries by weighted random
  sampling and tries them in turn: if one has no candidate (nothing left in the anti-repeat
  history, an empty album) or fails, the next one is tried, up to `smart_mix.max_attempts`
  (default 3). With `avoid_consecutive` (default) the entry shown last is tried last.
- **Context placeholders.** `{season}`, `{month}`, `{month_name}` and `{year}` in string
  params are expanded each time the entry runs (`smart_mix.hemisphere` flips the seasons;
  the timezone is `scheduler.timezone`). `on_this_day` searches `window_days` around today's
  date in each of the last `years_back` years; `taken_after` / `taken_before` give a fixed
  date range. No album IDs are needed.
- **Precedence** for scheduled refreshes and `POST /api/refresh`: the pinned
  `scheduler.source_rule`, then the Smart Mix (enabled with at least one enabled entry),
  then the last non-ephemeral manual display. Mix refreshes never overwrite that last
  display, so turning the mix off restores the previous behavior.
- **Direct pushes always win.** `POST /api/display/immich`, `/upload` and `/smart-search`
  display immediately, restart the refresh timer and never touch the playlist or your
  settings. Add `"ephemeral": true` so a temporary push (a movie poster, a song) is not
  remembered as the rule to replay, and `"hold_minutes": 150` so no scheduled refresh
  replaces it before then (`hold_until` in `GET /api/status`). A manual
  `POST /api/refresh` ignores the hold.

**Smart Search** is the same idea for one-off pushes: describe what you want, never an ID.

```bash
curl -X POST http://localhost:8000/api/display/smart-search -H 'Content-Type: application/json' \
  -d '{"query": "rainy sunday", "persons": ["Alice"], "hold_minutes": 120}'
curl -X POST .../api/display/smart-search -H 'Content-Type: application/json' \
  -d '{"on_this_day": true}'
```

`query` / `queries` are CLIP phrases (placeholders allowed), combined freely with `persons`,
`match_mode`, `accounts`, `on_this_day`, `taken_after` / `taken_before`, `fit`,
`push_to_fraimic` and `ignore_history`. Smart-search pushes are ephemeral by default
(`"ephemeral": false` stores one as the rule to replay).

A **context boost** shapes the *next scheduled refreshes* for a while, without displaying
anything now: it joins the playlist with its own weight (default 3), or replaces it with
`"exclusive": true`, until `ttl_minutes` pass.

```bash
curl -X POST .../api/smart-mix/context -H 'Content-Type: application/json' \
  -d '{"queries": ["cozy rainy day"], "weight": 5, "ttl_minutes": 240, "label": "rain"}'
curl -X DELETE .../api/smart-mix/context
```

For another source, send `kind` and `params` instead of `queries`. Unknown kinds are
rejected: `GET /api/smart-mix` lists the supported ones.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/display/smart-search` | Contextual search and immediate (ephemeral) push |
| `POST /api/smart-mix/context` | Temporary boost or override of the playlist (409 when the mix is disabled) |
| `DELETE /api/smart-mix/context` | Remove the boost |
| `GET /api/smart-mix` | Playlist with each entry's probability, resolved placeholders, live boost and supported kinds |

`/api/smart-mix` requires the API key for every method when `APP_API_KEY` is set.
`GET /api/status` reports `scheduler.smart_mix`, `hold_until`, and the entry shown by the
last refresh (`last_run.entry_label`).

```yaml
# Home Assistant: push a contextual photo on demand, boost the mix while it rains
rest_command:
  frame_smart_search:
    url: http://smart-frame.lan:8000/api/display/smart-search
    method: POST
    content_type: application/json
    payload: '{"query": "{{ query }}", "hold_minutes": {{ hold | default(120) }}}'
    timeout: 60
    headers:
      X-API-Key: !secret smart_frame_api_key
  frame_rain_boost:
    url: http://smart-frame.lan:8000/api/smart-mix/context
    method: POST
    content_type: application/json
    payload: '{"queries": ["cozy rainy day"], "weight": 5, "ttl_minutes": 240, "label": "rain"}'
    headers:
      X-API-Key: !secret smart_frame_api_key

automation:
  - alias: Frame - rainy day mood
    trigger:
      - platform: state
        entity_id: weather.home
        to: rainy
    action:
      - service: rest_command.frame_rain_boost
```

## Movie and TV posters (TMDB)

`POST /api/display/tmdb` finds a movie or series on [TMDB](https://www.themoviedb.org)
(`TMDB_API_KEY`: a v3 API key or a v4 read access token) and shows its best poster with the
`poster` template.

```bash
curl -X POST http://localhost:8000/api/display/tmdb -H 'Content-Type: application/json' \
  -d '{"query": "Le Fabuleux Destin d'"'"'Amélie Poulain", "media_type": "movie", "year": 2001}'
curl -X POST .../api/display/tmdb -H 'Content-Type: application/json' \
  -d '{"query": "The Office", "media_type": "tv", "season": 3, "language": "en-US"}'
```

| Request field | Meaning |
| --- | --- |
| `query` (or `title`) | Movie or series title |
| `media_type` | `movie`, `tv` or `multi` (both, default; `auto` is accepted) |
| `year`, `season` | Release year; TV season whose poster is preferred (falls back to the series poster) |
| `language` | Overrides `tmdb.language` (default `fr-FR`), e.g. `fr-CA` |
| `fallback_url`, `fallback_base64` | Picture used when TMDB has nothing, e.g. the Home Assistant `entity_picture` (base64 is tried first) |
| `ephemeral`, `ttl_minutes`, `hold_minutes` | Temporary by default: the previous image returns after `ttl_minutes` (default `tmdb.ephemeral_ttl_minutes`, 180) |
| `push_to_fraimic` | `false` only renders a preview |

**Regional posters.** TMDB tags posters with a language and, for regional variants, a
country (`fr`/`FR` for France, `fr`/`CA` for Québec). For `fr-FR`, posters are ranked:
exact language and region, then the same language (any or no region), then textless
artwork, then English, then anything else; ties go to the best rated
(`vote_average`, then `vote_count`). Posters are downloaded at `tmdb.image_size`
(`w780` by default; `original` is sharper on the 31.5" panel).

**Fallbacks.** When TMDB is not configured, has no match or fails: `fallback_base64` /
`fallback_url`, then `./data/assets/tmdb_fallback.png` if you provide one, then a title card
(set `tmdb.fallback_title_card: false` to get the TMDB error instead). `details.fallback`
tells which one was used.

A non-ephemeral poster (`"ephemeral": false`) becomes the rule replayed by
`POST /api/refresh`, and `"kind": "tmdb"` entries can join the [Smart Mix](#smart-mix-and-smart-search)
(`{"kind": "tmdb", "params": {"query": "Amélie", "media_type": "movie"}, "weight": 1}`).
`POST /api/display/media` is a deprecated alias.

## Now playing (music)

`POST /api/display/now-playing` shows the cover of the track that is playing with the
`album` template.

```bash
curl -X POST http://localhost:8000/api/display/now-playing -H 'Content-Type: application/json' \
  -d '{"title": "So What", "artist": "Miles Davis", "album": "Kind of Blue",
       "duration_seconds": 545, "position_seconds": 12, "style": "vinyl"}'
```

| Request field | Meaning |
| --- | --- |
| `title`, `artist`, `album` | Track details (`media_title`, `media_artist`, `media_album_name` in Home Assistant) |
| `artwork_url`, `artwork_base64` | Cover provided by the player (base64 is tried first) |
| `enrich_artwork` | When no usable cover was provided, look one up (default true; also `music.enrich_artwork`) |
| `style` | `sleeve` or `vinyl` (default `music.default_style`) |
| `duration_seconds`, `position_seconds`, `ttl_seconds` | When the cover expires (see below) |
| `ephemeral`, `hold_minutes`, `push_to_fraimic` | As for posters |

**Cover lookup.** No API key is needed: the
[Deezer](https://developers.deezer.com/api) public search (1000x1000 `cover_xl`), then
[MusicBrainz](https://musicbrainz.org) and the [Cover Art Archive](https://coverartarchive.org)
(studio albums are preferred over compilations, live albums and videos; requests are
rate-limited to one per second as MusicBrainz asks). Lookups are cached in memory
(`music.lookup_cache_size`). Without any cover, a typographic card is shown. Disable a
service with `music.deezer_enabled` / `music.musicbrainz_enabled`.

**Lifetime.** The cover expires when the track should be over: `duration_seconds -
position_seconds + music.ttl_grace_seconds` (30), or `ttl_seconds`, or
`music.default_ttl_minutes` (10). Sending the track that is already displayed only moves
its expiry (`"deduplicated": true` in the response): nothing is rendered or sent again, so
the Home Assistant automation can fire on every attribute change without waking a
battery-powered frame.

## Ephemeral images and the queue

A push with `"ephemeral": true` (the default for posters, covers and smart searches) is
temporary: it never becomes the image to come back to, nor the rule replayed by
`POST /api/refresh`.

- **Expiry.** Posters and covers carry an `expires_at`. When it passes, the previous
  non-ephemeral image becomes the current image again. It is re-sent only if the frame may
  not be showing it: a deep-sleeping frame that never received the cover is left alone,
  and an expired image is dropped from the queue instead of being uploaded when the frame
  wakes up.
- **Cancellation.** `DELETE /api/queue/ephemeral` (e.g. playback paused or stopped) drops
  a queued ephemeral image and restores the previous one immediately. It is idempotent
  (`"cancelled": false` when nothing was ephemeral).
- **No previous image yet?** A refresh of the active rule runs in the background instead.
- Ending an ephemeral image also lifts its `hold_minutes` protection; it does not restart
  the refresh timer.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/queue` | The upload waiting for the frame (expired images are never listed), the last delivery, and the ephemeral image shown or queued with its `expires_at` |
| `DELETE /api/queue/ephemeral` | Cancel the ephemeral image now: `cancelled`, `dropped_pending`, `reverted`, `pushed_base`, `refreshing` |

`GET /api/status` includes the same `ephemeral` block. The last successful delivery is kept
in `./data/last_delivery.json`, so what the frame shows is still known after a restart.

## Home Assistant: TV and music

The app must be able to reach Home Assistant to download `entity_picture` (add its host
to `URL_ALLOWLIST` when `BLOCK_PRIVATE_URLS=true`). Replace `media_player.living_room` and
`http://homeassistant.lan:8123` with yours. Picture URLs embed short-lived access tokens;
the app never writes them to its logs.

```yaml
rest_command:
  frame_now_playing:
    url: http://smart-frame.lan:8000/api/display/now-playing
    method: POST
    content_type: "application/json; charset=utf-8"
    timeout: 30
    headers:
      X-API-Key: !secret smart_frame_api_key   # only when APP_API_KEY is set
    payload: >-
      {% set p = 'media_player.living_room' %}
      {% set pic = state_attr(p, 'entity_picture') %}
      {{ {
        "title": state_attr(p, 'media_title'),
        "artist": state_attr(p, 'media_artist') or 'Unknown artist',
        "album": state_attr(p, 'media_album_name'),
        "artwork_url": ('http://homeassistant.lan:8123' ~ pic) if pic else none,
        "duration_seconds": state_attr(p, 'media_duration'),
        "position_seconds": state_attr(p, 'media_position')
      } | to_json }}
  frame_tv_poster:
    url: http://smart-frame.lan:8000/api/display/tmdb
    method: POST
    content_type: "application/json; charset=utf-8"
    timeout: 30
    headers:
      X-API-Key: !secret smart_frame_api_key
    payload: >-
      {% set p = 'media_player.living_room' %}
      {% set pic = state_attr(p, 'entity_picture') %}
      {% set series = state_attr(p, 'media_series_title') %}
      {{ {
        "query": series or state_attr(p, 'media_title'),
        "media_type": 'tv' if series else 'movie',
        "season": state_attr(p, 'media_season') if series else none,
        "fallback_url": ('http://homeassistant.lan:8123' ~ pic) if pic else none,
        "ttl_minutes": [((state_attr(p, 'media_duration') or 7200) / 60) | int + 15, 1440] | min
      } | to_json }}
  frame_cancel_ephemeral:
    url: http://smart-frame.lan:8000/api/queue/ephemeral
    method: DELETE
    headers:
      X-API-Key: !secret smart_frame_api_key

automation:
  - alias: Frame - now playing
    mode: queued
    trigger:
      - platform: state
        entity_id: media_player.living_room
        to: playing
      - platform: state
        entity_id: media_player.living_room
        attribute: media_title
    condition:
      - condition: state
        entity_id: media_player.living_room
        state: playing
    action:
      - choose:
          - conditions: "{{ state_attr('media_player.living_room', 'media_content_type') == 'music' }}"
            sequence:
              - service: rest_command.frame_now_playing
          - conditions: "{{ state_attr('media_player.living_room', 'media_content_type') in ['movie', 'tvshow', 'episode'] }}"
            sequence:
              - service: rest_command.frame_tv_poster
  - alias: Frame - playback stopped
    trigger:
      - platform: state
        entity_id: media_player.living_room
        from: playing
        for: "00:02:00"   # ignore short pauses
    action:
      - service: rest_command.frame_cancel_ephemeral
```

The now-playing call is deduplicated, so the `media_title` trigger only re-renders when the
track really changes. A deep-sleeping frame wakes up to the right picture: covers whose
song has ended are dropped from the queue, and the previous photo comes back.

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

Templates use the [Inter](https://github.com/rsms/inter) typeface by Rasmus Andersson,
bundled under the SIL Open Font License 1.1 (`app/assets/fonts/OFL.txt`).

This product uses the TMDB API but is not endorsed or certified by
[TMDB](https://www.themoviedb.org). Album covers come from the
[Deezer API](https://developers.deezer.com), [MusicBrainz](https://musicbrainz.org) and the
[Cover Art Archive](https://coverartarchive.org).

## License

[MIT](LICENSE)
