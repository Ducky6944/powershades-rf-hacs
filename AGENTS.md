# AGENTS.md

Working notes, lessons, and good practices for building the **PowerShades** Home Assistant / HACS integration. Read this before doing anything.

## Project goal
A HACS-installable Home Assistant custom integration for **PowerShades** smart window shades using the **cloud REST API** at `https://api.powershades.com` (Django REST Framework). User supplies **email** + **password** during the config flow (JWT auth). We add features incrementally, committing per-feature.

## Non-negotiables
- **Do not guess.** When behavior is unclear, probe the live API (curl), read the real docs, or read a working implementation's source. Prefer evidence over theory.
- **Commit everything.** Baseline + every feature = its own commit.
- Keep secrets out of the repo. The test credentials (`user@example.com` / `your_password`) are for local testing only; never hardcode them in integration source. Read env or config.
- Follow existing conventions before inventing new ones.

## Environment (verified 2026-09-17)
- macOS (darwin), git 2.50, Python 3.13.2 via asdf.
- `aiohttp` and `voluptuous` are importable (HA deps present locally). `ruff` is NOT installed — add to `pyproject`/dev when we set up lint.
- Home Assistant ships `aiohttp` — use it for the client (do NOT pull in `requests` into the integration itself).

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

## Open questions (resolve, don't guess)
1. **How do we read live position?** Consumer account gets 403 on `/devices/`. Options: (a) user is dealer / has device-permission; (b) position is derivable from `/shades/` + `/shadeattributes/` (need to confirm which field); (c) a different endpoint not in the CoreAPI doc. **Must confirm before exposing cover entities.**
2. Percentage direction (0 = open vs 100 = open?).
3. Exact schema of `/devices/` (unseen) for mapping position/battery.

## Good practices for the HACS integration
- Modern async: `aiohttp.ClientSession` via `httpx`? No — use `aiohttp` (HA-native). Store session on the config entry; close in `async_unload_entry`.
- `ConfigEntryAuth` + `OAuthTokenProvider`-style token refresh is overkill; use a small auth wrapper that logs in with email/password, refreshes on 401, and re-logs on refresh failure. Store **only** email+password + base_url in the config entry data (secrets) — re-mint JWTs at runtime.
- `DataUpdateCoordinator` to fetch `/shades/` + `/shadeattributes/` + `/groups/` and hand out covers.
- Config flow: validate credentials in the flow (call `/auth/jwt/`), handle unique_id per credential.
- Cover platform maps to `cover` entities; scenes → `scene`/`button`; groups → `cover` too.
- HACS: needs `manifest.json` + `custom_components/powershades/`. For HACS listing the repo should be a standalone repo (this one) with proper `name`, `domain` powershades.
