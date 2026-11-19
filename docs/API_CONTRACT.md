# Smart Frame Engine API Contract (v1.1)

This is the contract between the engine and **external clients**: the Home Assistant
integration, Node-RED flows, shell scripts, phone shortcuts. It says what a client can rely on
and how the engine will change without breaking it.

- **Source of truth for shapes:** [`openapi.json`](openapi.json) (exported from the running
  code; a test fails if it drifts) and the live `/docs` of any engine.
- **This document** covers what the schema cannot say: compatibility rules, authentication,
  errors, discovery and the change policy.

## 1. Compatibility handshake

Every client should call `GET /api/health` first. It is **always public** (no credentials, in
every authentication mode), so it works before any key or password is configured.

```json
{
  "status": "ok",
  "version": "1.0.0rc2",
  "api_version": 1,
  "min_api_version": 1,
  "instance_id": "3f2b8c1e-5d0a-4c7e-9b61-0a1d2e3f4a5b",
  "instance_name": "Smart Frame Engine 3f2b8c",
  "auth": "key",
  "capabilities": ["activity_log", "ambient_context", "display_immich", "..."]
}
```

| Field | Meaning |
| --- | --- |
| `status` | Always `"ok"` while the process serves requests (liveness). |
| `version` | Engine release, [PEP 440](https://peps.python.org/pep-0440/) (`1.0.0`, `1.0.0rc1`). Informational: never gate behaviour on it, use `api_version` and `capabilities`. |
| `api_version` | Integer contract version the engine speaks. Incremented **only on a breaking change**. It is `1` for every v1.x release. |
| `min_api_version` | Oldest contract version this engine still serves (see the policy below). |
| `instance_id` | Stable random UUID of this engine, generated once and stored in the data volume (`instance.json`). **Use it as your unique key** for this engine: it survives restarts, upgrades and address changes. It is not derived from the hardware and is not a secret. A wiped data volume creates a new id. |
| `instance_name` | Human-readable name, also the mDNS service name. Defaults to `Smart Frame Engine <first 6 characters of the id>`; the operator can set `INSTANCE_NAME`. Not guaranteed unique when set by hand: key on `instance_id`. |
| `auth` | `open`, `key` or `password`: which credentials the other endpoints need (section 3). |
| `capabilities` | Sorted list of feature flags (section 2). |

**Compatibility rule.** A client written for contract `N` is compatible when
`min_api_version <= N <= api_version`. Otherwise it must stop and tell the user which side to
update (engine older than the client: `N > api_version`; client older than the engine:
`N < min_api_version`).

**Graceful degradation.** A client that needs a feature checks `capabilities`, not `version`.
A missing flag means "this engine is older (or built without it)": hide or disable that feature
instead of failing. Ignore unknown fields in responses and unknown flags in `capabilities`.

## 2. Capabilities

Flags are centralized in [`app/core/contract.py`](../app/core/contract.py) and a test ties each
one to a real endpoint. Within a contract version they are only ever **added**.

| Flag | Backed by | What a client can do |
| --- | --- | --- |
| `activity_log` | `GET /api/activity` | Read recent renders and log lines. |
| `ambient_context` | `POST /api/context` | Push presence, weather, season, boosts, family mode with TTLs. |
| `display_immich` | `POST /api/display/immich` | Show an Immich photo (random, album, favorites, search). |
| `display_smart_search` | `POST /api/display/smart-search` | Contextual photo push without Immich IDs. |
| `display_tmdb` | `POST /api/display/tmdb` | Show a movie or TV poster. |
| `ephemeral_images` | `DELETE /api/queue/ephemeral` | Cancel a temporary image; the previous one comes back. |
| `family_mode` | `PUT /api/config` (`family`) | Family Mode settings, also switchable through `ambient_context`. |
| `force_refresh` | `POST /api/refresh` | Re-run the active source rule now. |
| `frame_push` | `POST /api/fraimic/push` | Send the current (or preview) image to the frame. |
| `frame_wakeup` | `POST /api/fraimic/wakeup` | Tell a deep-sleep engine the frame woke up. |
| `now_playing` | `POST /api/display/now-playing` | Show an album cover with artist and album. |
| `now_playing_dedup` | `POST /api/display/now-playing` | Resending tracks of the same album only extends the expiry (response `deduplicated: true`). |
| `playground` | `POST /api/playground/render` | Render without storing or pushing. |
| `random_discovery` | `PUT /api/config` (`smart_mix.discovery_share`) | Occasional whole-library surprise photo. |
| `smart_mix` | `GET /api/smart-mix` | Inspect the weighted playlist. |
| `smart_mix_context` | `POST /api/smart-mix/context` | Set a temporary playlist boost. |
| `smart_mix_kinds` | `GET /api/smart-mix/kinds` | Describe the fields, choices, presets and summary of each rule kind, to build a rule editor. |
| `title_card` | `POST /api/display/title-card` | Show a text card. |
| `upload_image` | `POST /api/display/upload` | Show an image you send. |

## 3. Authentication

The mode is chosen by the operator (`APP_API_KEY`, `ADMIN_PASSWORD`) and reported as `auth` in
`/api/health` (and in the Zeroconf record).

| Mode | Condition | Rule |
| --- | --- | --- |
| `open` | Neither is set (LAN) | No credentials anywhere. |
| `key` | Only `APP_API_KEY` | Every `POST`/`PUT`/`PATCH`/`DELETE`, and every method on `/api/config`, `/api/immich`, `/api/smart-mix`, `/api/context`, `/api/tmdb` and `/api/auth/api-key`, needs the key. Other reads stay public. |
| `password` | `ADMIN_PASSWORD` set | Everything needs credentials except the public endpoints below. An API key is generated if none is configured. |

**Credentials**, for scripts and integrations: the API key as `X-API-Key: <key>`,
`Authorization: Bearer <key>`, or `?api_key=<key>`. (A web UI login uses a signed `HttpOnly`
cookie plus `X-Requested-With: SmartFrame` on changes; integrations should use the key.)

**Always public**, in every mode: `GET /api/health`, `GET /login`, `POST /api/auth/login`,
`GET /api/auth/session`. The image endpoints (`/api/current.jpg`, `/api/current.bin`,
`/api/current_dithered.png`, `/api/preview.jpg`, `/api/preview_dithered.png`) are public unless the
operator sets `PUBLIC_IMAGE_ENDPOINTS=false`, in which case they need the key (`?api_key=` works
for Home Assistant's Generic Camera).

## 4. Errors

Errors are JSON with a `detail` field; clients should branch on the status code.

| Status | Body | Meaning |
| --- | --- | --- |
| `401` | `{"detail": "Missing or invalid API key or session"}` | Credentials needed or wrong. |
| `403` | `{"detail": "..."}` | A session request missed the CSRF header (not for API keys). |
| `404` | `{"detail": "..."}` | No eligible photo matched, or the thing does not exist. |
| `409` | `{"detail": "..."}` | A required source is not configured (Immich, TMDB...) or a conflicting state. |
| `413` | `{"detail": "Request body exceeds the N MB limit"}` | Upload too large. |
| `422` | `{"detail": "text"}` or `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` | Invalid request. Validation errors never echo the submitted values. |
| `429` | `{"detail": "..."}` + `Retry-After` | Too many failed logins. |
| `502` / `504` | `{"detail": "..."}` | An upstream source (Immich, TMDB, the frame) failed or timed out. |

Messages never contain secrets. Successful display calls return the request result with the
fields declared in the schema. `POST /api/fraimic/push` returns `200` when the image was
delivered and `202` when the frame was unreachable and the image stays queued.

## 5. Zeroconf (mDNS)

The engine advertises `_smartframe._tcp.local.` (disable with `ZEROCONF_ENABLED=false`; in Docker
it needs `network_mode: host`). TXT properties, all strings:

| Key | Value |
| --- | --- |
| `version` | Same as `/api/health`. |
| `api_version` | Same as `/api/health`, as a decimal string. |
| `min_api_version` | Same as `/api/health`, as a decimal string. |
| `instance_id` | Same as `/api/health`: match a discovered service to a saved engine with it. |
| `path` | `ROOT_PATH` (`/` when served at the root). |
| `auth` | `open`, `key` or `password`. |

The service **name** is the engine's `instance_name`, so several engines on one network coexist
(if a name is still taken, the responder renames the service rather than failing).
A client can reject an incompatible engine from the record alone. The capability list is **not**
in the record (a TXT string is limited to 255 bytes): read it from `GET /api/health` once you
have the address and port.

## 6. Endpoint index

Generated from `openapi.json` by `scripts/export_openapi.py`; do not edit by hand.

<!-- BEGIN ENDPOINTS (generated by scripts/export_openapi.py) -->

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | `/api/activity` | Recent renders and log lines (newest first) |
| `GET` | `/api/auth/api-key` | Show the API key |
| `POST` | `/api/auth/api-key/regenerate` | Generate a new API key |
| `POST` | `/api/auth/login` | Sign in to the web UI |
| `POST` | `/api/auth/logout` | Sign out of the web UI |
| `GET` | `/api/auth/session` | Current session |
| `GET` | `/api/config` | Read the runtime configuration |
| `PUT` | `/api/config` | Update the runtime configuration |
| `POST` | `/api/config/reset` | Reset saved values to .env / defaults |
| `GET` | `/api/config/schema` | JSON Schema of the runtime configuration |
| `GET` | `/api/context` | The live ambient context |
| `POST` | `/api/context` | Set ambient context fields (each expires on its own) |
| `DELETE` | `/api/context` | Clear the ambient context |
| `GET` | `/api/current.bin` | Current Fraimic frame buffer (.bin) |
| `GET` | `/api/current.jpg` | Current image (enhanced, before dithering) |
| `GET` | `/api/current_dithered.png` | Current image as rendered by the panel (6-color simulation) |
| `POST` | `/api/display/immich` | Select and display an Immich photo |
| `POST` | `/api/display/media` | Display a poster (alias of /api/display/tmdb) |
| `POST` | `/api/display/now-playing` | Display the album cover of the track that is playing |
| `POST` | `/api/display/smart-search` | Display a photo found by a contextual search |
| `POST` | `/api/display/title-card` | Display a typographic card (title, subtitle, caption) |
| `POST` | `/api/display/tmdb` | Display a movie or TV series poster |
| `POST` | `/api/display/upload` | Display an uploaded image |
| `GET` | `/api/fraimic/info` | Frame telemetry (battery, Wi-Fi, firmware) |
| `POST` | `/api/fraimic/push` | Force push the current image to the frame |
| `POST` | `/api/fraimic/wakeup` | Signal that the frame is awake (Home Assistant webhook) |
| `GET` | `/api/health` | Liveness probe |
| `GET` | `/api/immich/albums` | List albums |
| `GET` | `/api/immich/people` | People known to each account (cached) |
| `POST` | `/api/immich/people/refresh` | Reload the people lists from Immich |
| `POST` | `/api/immich/test` | Test an Immich connection (saved or unsaved settings) |
| `GET` | `/api/immich/thumbnail/{account}/{kind}/{item_id}` | Small thumbnail of an asset (album cover) or a person (face) |
| `POST` | `/api/playground/render` | Render an image with several eInk profiles / dithers (never displayed) |
| `GET` | `/api/preview.jpg` | Most recent render (enhanced, before dithering), pushed or not |
| `GET` | `/api/preview_dithered.png` | Most recent render as the panel would show it (6-color simulation) |
| `GET` | `/api/queue` | Inspect the upload queue |
| `DELETE` | `/api/queue/ephemeral` | Cancel the ephemeral image (playback stopped) |
| `POST` | `/api/refresh` | Re-run the active source rule |
| `GET` | `/api/smart-mix` | Show the Smart Mix playlist |
| `POST` | `/api/smart-mix/context` | Boost or override the Smart Mix temporarily |
| `DELETE` | `/api/smart-mix/context` | Remove the Smart Mix boost |
| `GET` | `/api/smart-mix/kinds` | Describe the rules each source kind accepts |
| `GET` | `/api/status` | System status |
| `GET` | `/api/tmdb/search` | Search movies and series (for the web UI picker) |
| `POST` | `/api/tmdb/test` | Test a TMDB API key |

<!-- END ENDPOINTS -->

## 7. Compatibility and breaking-change policy

**Not breaking (allowed in any v1.x release, `api_version` stays):**

- a new endpoint, a new optional request field, a new response field;
- a new capability flag, a new enum value in a response that clients are told to tolerate;
- a new Zeroconf TXT key;
- bug fixes, performance work, UI changes, new defaults for optional behaviour.

**Breaking (requires `api_version` + 1):**

- removing or renaming an endpoint, a field or a capability flag;
- making an optional request field required, or narrowing what is accepted;
- changing the type or meaning of an existing field, or a status code that clients branch on;
- changing the authentication rules so that a previously accepted request is refused;
- removing or renaming a Zeroconf TXT key.

**How a breaking change ships:**

1. Prefer a deprecation window: add the replacement first (a new endpoint or flag), mark the old
   one deprecated in `openapi.json` and in the changelog below.
2. Bump `API_VERSION`. Keep serving the previous contract for at least one minor release and
   keep `MIN_API_VERSION` at the old value during that window, so clients on the old contract
   keep working. Raise `MIN_API_VERSION` only when the old contract is removed.
3. Record it in the changelog below, with a migration note.

**Maintainers:** any PR that changes an `/api/*` endpoint, an exposed Pydantic model, the
capability list, `API_VERSION` or the Zeroconf record must update this file (including the
changelog) and run `python scripts/export_openapi.py` (see `CLAUDE.md`).

## Contract Changelog

### 1.2 (unreleased), additive

- `GET /api/status` gains `release`: the engine version spelled like the git tag and the Docker
  image tag (SemVer, `1.0.0-rc.2`), with `+<build>` appended for images built outside a release
  tag. It is for display only; `version` (PEP 440) on `/api/status` and `/api/health` is
  unchanged. No `api_version` change.
- New capability `smart_mix_kinds`: `GET /api/smart-mix/kinds` returns a descriptor per runnable
  source kind (`kinds`: fields, widgets, choices, presets, summary recipe) and the `placeholders`
  a rule's text may use. Providers own their descriptor; a kind without one has to be edited as
  raw `params`. API-key protected like the rest of `/api/smart-mix`. No `api_version` change.

### 1.1 (engine 1.0.0rc2), additive

- `GET /api/health` gains `instance_id` and `instance_name`; Zeroconf TXT gains `instance_id` and
  the service name becomes the unique `instance_name`, so two engines on one LAN no longer
  clash. Clients should use `instance_id` as the stable key of an engine. No `api_version` change.

### 1.0 (engine 1.0.0rc1)

- Initial contract: handshake on `GET /api/health` (`version`, `api_version`, `min_api_version`,
  `auth`, `capabilities`), Zeroconf TXT `api_version` and `min_api_version`.
- Capability flags: `activity_log`, `ambient_context`, `display_immich`, `display_smart_search`,
  `display_tmdb`, `ephemeral_images`, `family_mode`, `force_refresh`, `frame_push`,
  `frame_wakeup`, `now_playing`, `now_playing_dedup`, `playground`, `random_discovery`,
  `smart_mix`, `smart_mix_context`, `title_card`, `upload_image`.
