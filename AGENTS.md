# AGENTS.md

Working notes, lessons, and good practices for building the **PowerShades** Home Assistant / HACS integration. Read this before doing anything.

## Project goal
A HACS-installable Home Assistant custom integration for **PowerShades** smart window shades using the **cloud REST API** at `https://api.powershades.com` (Django REST Framework). User supplies **email** + **password** during the config flow (JWT auth). We add features incrementally, committing per-feature.

## Non-negotiables
- **Do not guess.** When behavior is unclear, probe the live API (curl), read the real docs, or read a working implementation's source. Prefer evidence over theory.
- **Commit everything.** Baseline + every feature = its own commit.
- Keep secrets out of the repo. All credentials / API keys / gateway addresses live only in `.env` (gitignored) — never in integration source or docs. Read them from the env for local testing: `POWERSHADES_API_KEY`, `POWERSHADES_EMAIL`, `POWERSHADES_PASSWORD`, `POWERSHADES_GATEWAY`.
- Follow existing conventions before inventing new ones.

## Environment (verified 2026-09-18)
- macOS (darwin), git 2.50, Python 3.13.2 via asdf.
- **Local HA package is importable for static import checks** at `/Users/crash/.asdf/installs/python/3.13.2/lib/python3.13/site-packages/homeassistant` — **but its version (2026.2.3) is OLDER than the user's live HA (2026.9.1)**. Do NOT trust `hasattr`/imports against the local HA for *removal*; older versions lack newer symbols, newer versions remove older ones. **Always verify API symbols against the LIVE box's exact HA version** (see "Verify against the live box" below), not the local one.
- `aiohttp` and `voluptuous` are importable locally. **`ruff` IS installed** via `python3 -m ruff` (0.16.8) — run `python3 -m ruff check` + `python3 -m ruff format --check` before committing.
- Home Assistant ships `aiohttp` — use it for the client (do NOT pull in `requests` into the integration itself).
- **HA API note (this build): there is NO `ConfigEntryOptionsFlow`.** Use `OptionsFlowWithConfigEntry` (present in HA 2026.8–2026.9.2), passed `config_entry` **positionally** (no `domain=` class kwarg). This was the cause of the 0.1.4 "Invalid handler specified" (an import that failed → the `powershades` config-flow handler never registered).

## PowerShades Cloud API — VERIFIED facts
> Base: `https://api.powershades.com` (no `/api` prefix on paths). Authenticated = `Authorization: Bearer <access>`.

### Auth (JWT)
- **Login:** `POST /auth/jwt/` body `{"email": "...", "password": "..."}` → `{"access": "...", "refresh": "..."}`. (Field is `email`, not `username`.)
- **Refresh:** `POST /auth/jwt/refresh/` body `{"refresh": "..."` → `{"access": ...}`.
- **Verify:** `POST /auth/jwt/verify/` body `{"token": "..."}`.
- Access tokens are **short-lived** (observed expiry ~14400s ≈ 4h, but treat as transient); refresh on 401. The working client retries: on 401 → refresh once → retry; if still failing and email/password mode → re-login. See `references/homebridge_api.js` (a known-working reference client).
- `WWW-Authenticate: Bearer realm="api"` is returned on unauthenticated `/api/`.
- Session/cookie login also exists at `/accounts/login/` (Django) but we use JWT.

### Data reads (list endpoints — confirmed working with a valid access token)
- `GET /shades/` → **list** of `{id, name, device_id, created_at, updated_at, property_id}`. `name` is the human shade name (e.g. "FIREPLACE"). **No position field here.** No detail endpoint (`/shades/{id}/` → 404).
- `GET /shadeattributes/` → list of `{id, shade_id, attribute, value, created_at}`. Attributes: `Direction` (South), `Floor`, `Room`, `Window`, `User Percent`. **`User Percent` is STATIC metadata (did not change after moving a shade) — it is NOT live position.** Useful as cover `extra_state_attributes` (room/window/floor/direction).
- `GET /groups/` → list of `{id, name, rule, shades:[shade_ids...], property_id, ...}`. Groups map shade ids. (Note: one group had duplicate shade ids.)
- `GET /scenes/` → `[]` (user has none).
- `GET /schedules/` → `[]`.
- `GET /devices/` → **403 "You do not have permission"** for consumer accounts. This is almost certainly the **live position** endpoint but it is dealer-gated. **Open question.**
- `GET /users/`, `/users/{id}/`, `POST /populate-dashboard/` → 403 (dealer-only).

### Commands (writes — confirmed working, 200)
- **Move single shade:** `POST /shades/move/` body `{"shade_name": "<name>", "percentage": <int 0-100>}` → `{"message": "Moving shade named 'SLIDER', exactly 75 percent"}`.
- **Move group:** `POST /groups/move/` body `{"group_name": "<name>", "percentage": <int>}` → `{"message": "Moving shade group named 'Great Room', exactly 60 percent"}`.
- **Activate scene:** `POST /scenes/move/` body `{"scene_name": "<name>"}` (no percentage).
- Percentage semantics: 0 = fully raised/open, 100 = fully down/closed (assumed; confirm against real device). "exactly N percent" per the message.

### Response shapes / quirks
- List endpoints return a **bare JSON array** (not `{"results": [...]}`). Defensively handle both (the reference client checks `Array.isArray(data.results)`).
- Accept header is picky on some routes: send `Accept: application/json` or `*/*`; `/api/` root is CoreAPI (browsable HTML) — not a data route.
- No WebSocket/push on cloud — **poll** state (reference clients poll; local-UDP variant pushes ~every 10s while moving, but that's a different transport).

### Known prior art (read for reference, do not vendor wholesale)
- `dstocking/powershades-homeassistant` — HA integration using **local UDP** (not cloud). Good for cover/sensor structure.
- `vemboy200/Pyowershades` — asyncio **UDP** lib; `PROTOCOL.md`, `KNOWN_BEHAVIERS.md`.
- `apumapho/homebridge-powershades` — **cloud REST** client in `api.js` (saved at `references/homebridge_api.js`). Best cloud auth/endpoint reference.
- `developer-powershades/savant-powershades-profile` — official vendor Savant profile.

## Local RF Gateway — VERIFIED (2026-09-17)

The shade system has a **local RF Gateway** (lwIP web server) at `http://192.0.2.48` (serial `1010110000000000`, 30 RF channels, "Connected to Powershades Cloud"). This is the **live state / feedback plane** and the local control plane.

### Read (GET `ajax.shtml?var=<name1>,<name2>` → JSON **array**, one element per requested var in order)
| var | shape | meaning |
|-----|-------|---------|
| `percent` | 30 colon-seps, `0..100` or `-1` | per-channel position, -1 = no shade on channel / not reporting |
| `battery` | 30, **mV** (`*0.001`→V) | per-channel battery; 0 = none |
| `rx` | 30, dB | per-channel RF rssi |
| `rfdevs` | 30, RF id or `0` | per-channel linked device id; 0 = unlinked |
| `chnames1` / `chnames2` / `chnames3` | 10 each (ch 1-10 / 11-20 / 21-30) | per-channel names |
| `netsts` `rfsts` `rssi` `version` `curfwpg` | single | gateway status / firmware |

**Current state:** `rfdevs` all `0`, `chnames` blank, `percent`/`battery` all `-1`/0 → **no RF device linked to any channel yet.** That's why positions look empty. Commands still "work" (200) but nothing physically responds until a device is paired via the gateway UI (`/device.shtml`: Pair / Link Feedback buttons) or a shade reports.

### Commands (GET, `200` w/ empty body)
- `ajax.shtml?up=<ch>` / `down=<ch>` / `stop=<ch>` — **local has NO absolute percent set**; only up/down/stop.
- `?pair=<ch>` → pairing mode, `?link=<ch>` → link feedback, `?p2=<ch>`.
- So **absolute position control (open/close/percent) must go through the CLOUD API** (`/shades/move/`), while the **gateway gives live position/battery/feedback** for readback once a device reports.

### Decided integration strategy
- **Control plane = cloud API** (email/password JWT): open/close/set_position + groups + scenes. This is the user's primary want (send open/close).
- **State plane = local gateway** (optional in options flow): read `percent`/`battery`/`rx`/`rfdevs`/`chnames` per channel → live cover position + battery sensor + rx sensor. Only populated once a device reports on a channel.
- **Open/close semantics:** cloud API `percentage`: `0 = open`, `100 = closed`. HA cover: `0 = closed`, `100 = open` → map `cloud_pct = 100 - ha_pos`; open→0, close→100.

## Open questions (resolve, don't guess)
1. ~~How do we read live position?~~ → **Local gateway `ajax.shtml?var=percent,battery,rfdevs`** (see above). Confirm a device actually reports by pairing/linking one channel to a real shade and re-reading.
2. ~~Percentage direction~~ → **cloud 0=open, 100=closed** (verified in move messages).
3. Does channel N on the gateway map 1:1 to a specific cloud shade? (No explicit channel field in `/shades/` or `/shadeattributes/`. Resolve by pairing one channel and observing which shade's `percent` changes.)

## Good practices for the HACS integration
- Modern async: `aiohttp.ClientSession` via `httpx`? No — use `aiohttp` (HA-native). Store session on the config entry; close in `async_unload_entry`.
- `ConfigEntryAuth` + `OAuthTokenProvider`-style token refresh is overkill; use a small auth wrapper that logs in with email/password, refreshes on 401, and re-logs on refresh failure. Store **only** email+password + base_url in the config entry data (secrets) — re-mint JWTs at runtime.
- `DataUpdateCoordinator` to fetch `/shades/` + `/shadeattributes/` + `/groups/` and hand out covers.
- Config flow: validate credentials in the flow (call `/auth/jwt/`), handle unique_id per credential.
- Cover platform maps to `cover` entities; scenes → `scene`/`button`; groups → `cover` too.
- HACS: needs `manifest.json` + `custom_components/powershades/`. For HACS listing the repo should be a standalone repo (this one) with proper `name`, `domain` powershades.

## What's built (as of 2026-09-17)
Working scaffold committed. Structure at `custom_components/powershades/`:
- **client.py** — JWT auth (login/refresh/re-login, one 401 retry), cloud reads (`/shades/`,`/groups/`,`/scenes/`,`/shadeattributes/`) + commands (`/shades/move/`,`/groups/move/`,`/scenes/move/`). `aiohttp` under the hood.
- **config_flow.py** — email + password (+ optional base_url, optional local-gateway address). Validates via real login before creating the entry. `unique_id = email.lower()`.
- **coordinator.py** — `DataUpdateCoordinator`. Cloud lists **cached** (re-fetched ≤ every 5 min) + local-gateway state **re-fetched every ~10 s** (it's the live plane). Cloud failure falls back to last-good cache.
- **cover.py** — one cover per shade + one per group. open→cloud 0, close→cloud 100, `set_position N`→cloud `100-N`.
- **button.py** — one button per scene.
- **sensor.py** — per linked gateway channel: position, battery (V), rx (dB).
- Lint clean under ruff 0.16.8 (`python3 -m ruff check` + `ruff format --check`).

**Verified live:** 10 shades + 2 groups resolved; move commands issue correct percentages (open→0, close→100, set 40→cloud 60, group 60→cloud 40); local gateway reports channels 1/2/3/5 linked (positions 100/100/100/100, battery 12.06/12.32/12.57 V, hex device ids). Position is "set-and-optimistic" (cloud has no consumer read); live position comes from the gateway sensors.

## Still to do (incremental, one commit each)
1. **Install & test in a real Home Assistant** (copy `custom_components/` into HA's config dir, or use HACS local). Confirm config flow UI, cover open/close, sensors, device registry grouping.
2. **Stop support** — no cloud stop endpoint; consider a gateway `stop=<ch>` where a channel is linked, else document as unsupported.
3. **Options flow** — change password / disable gateway / tune poll interval without recreating the entry.
4. **`manifest.json` codeowners** — replace `@crash` with real GitHub handle before HACS listing.
5. **Icon** — `icons.png` is a generated placeholder; swap for real PowerShades branding.
6. **Scene button test** — create a scene in the account to verify the button path.
7. **Tests** — add `tests/` with pytest fixtures (mocked client) for coordinator parse + refresh, no live network in CI.
8. **HACS listing** — `integration_type: third_party`; request listing once install+tests are green.
