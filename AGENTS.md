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
- So **absolute "set to N%" is emulated locally** in `_movement.py` (see strategy below), while the **gateway gives live position/battery/feedback** for readback once a device reports.

### Decided integration strategy (REVISED — local-first, **cloud dropped** as of 0.1.7)
- **Everything local. No cloud.** Cloud was dropped because its group control "completely not functional" and it added config burden with no payoff. The gateway is the single state+control plane.
- **Control plane = local gateway:** `up`/`down`/`stop` per channel. **Absolute "set to N%" is emulated** (gateway has no percent-set) — a timed routine in `_movement.py`: `stop` → `up` for `travel_time` (→ known fully-open 100%) → `down` for `(100-N) * travel_time / 100` s → `stop`. **`travel_time` (full 0→100 sweep, seconds) is a user-config field** — a **base** value *plus* **per-channel overrides** (`travel_times`, keyed by channel) since taller shades are slower. Set per shade during the identify step; editable any time via options flow. Cover uses `coordinator.travel_time_for(channel)`. Accuracy depends on it.
- **State plane = local gateway:** `percent`/`battery`/`rx`/`rfdevs` per channel. All diagnostic (DIAGNOSTIC entity category); the gateway read is **flaky**, so a per-channel **estimated-position** diagnostic is also emitted = the position we *intended* last.
- **Percent direction (gateway, user-verified):** `0 = fully closed/down`, `100 = fully open/up`. HA cover: `0 = closed`, `100 = open` → **identity mapping** (`current_cover_position = percent`).
- **Buttons always on:** every cover sets `_attr_assumed_state = True` so the HA UI never greys Open/Close due to a bogus/absent reported position (verified: frontend `canOpen`/`canClose` are forced true when `assumed_state === true`).

## Open questions (resolve, don't guess)
1. ~~How do we read live position?~~ → **Local gateway `ajax.shtml?var=percent,battery,rfdevs`** (see above). Confirm a device actually reports by pairing/linking one channel to a real shade and re-reading.
2. ~~Percentage direction~~ → **cloud 0=open, 100=closed** (verified in move messages).
3. Does channel N on the gateway map 1:1 to a specific cloud shade? (No explicit channel field in `/shades/` or `/shadeattributes/`. Resolve by pairing one channel and observing which shade's `percent` changes.)

## Good practices for the HACS integration
- Modern async: `aiohttp.ClientSession` (HA-native, don't pull in `requests`).
- `DataUpdateCoordinator` to poll the gateway every 3 s and hand out covers/sensors. Keep last-good read so a flaky read doesn't blank the UI.
- Config flow: probe the gateway to learn linked channels; `unique_id = gateway host:port`.
- **`TextSelector(selector.TextSelectorConfig(multiline=True))`** renders a multi-line textarea in the HA UI — use it for "one line per item" inputs (groups, per-channel travel times, names). Plain `vol.Coerce(str)` renders a single-line box. Verified available in the local HA build.
- **Gotcha (0.1.7 crash):** a helper defined at **module level** must be called as a bare function, NOT `self.method(...)`. `client.py` had `self._parse_gateway(...)` while `_parse_gateway` was a module fn → `AttributeError` on every refresh, which looked like "setup failed". Fixed in 0.1.8.
- **`vol.Coerce(float)` raises on `""`.** For optional numeric fields in a schema, use `vol.Coerce(str)` + parse in the handler (blank stays blank).
- HACS: needs `manifest.json` + `custom_components/powershades/`; standalone repo with `name`, `domain` = powershades.

## What's built (as of 2026-09-18 — v0.1.8, local-first, cloud dropped)
Structure at `custom_components/powershades/` (all `aiohttp`, lint clean under ruff 0.16.8):
- **client.py** — local gateway only: `fetch_gateway(vars)`, `gateway_up/down/stop(ch)`. `_parse_gateway(raw)` (module fn) → list of per-channel dicts. No cloud.
- **_movement.py** — timed routines: `open_channel`/`close_channel`/`stop`, and `set_position(client, ch, target, travel_time)` = stop → up(travel) → down(`(100-target)*travel/100`) → stop.
- **config_flow.py** — **Step 1** gateway + *base* travel_time → **Step 2** per linked channel: pick Up/Down to **nudge** (physically moves; can repeat as many times as needed — nothing advances until you tick "move on") + optional name + optional **per-channel travel time** → **Step 3** optional groups (multiline textarea, `Name: 1,2,3` per line) → done. `unique_id = gateway host:port`. Also an options flow (base travel_time + per-channel travel times + names + groups, all multiline, all optional).
- **coordinator.py** — `DataUpdateCoordinator`, polls gateway every **3 s**; keeps last-good channels; exposes `travel_time` (base), `travel_time_for(channel)` (per-shade override → else base), `record_estimate/estimate`, `user_groups`, `available_metrics`, device-info helpers.
- **cover.py** — one cover per linked channel (open/close/stop + **SET_POSITION via `_movement.set_position`**, using `travel_time_for(channel)`) + one per user group (open/close/stop fan-out). All set `assumed_state=True`. Identity percent mapping; `available` requires the channel linked.
- **sensor.py** — per linked channel, **DIAGNOSTIC**, gated by `available_metrics`: percent, battery (V), rx (dB), device_id, plus an `estimated` position sensor.
- Lint clean: `python3 -m ruff check` + `ruff format --check` pass.

**0.1.8 (this) fixes 0.1.7's 4 reported issues:** (1) per-shade travel times; (2) identify loop no longer auto-advances + up/down re-issuable + name field surfaced; (3) groups = multiline textarea; (4) **the 0.1.7 crash** — `client.py` called `self._parse_gateway(...)` but it's a module fn → fixed to a bare call (this was why "setup failed" and no state ever loaded).

**Verified live (0.1.5/0.1.6 test rounds):** integration now lists under Settings → Devices & Services → Integrations (manifest `integration_type: hub` fixed the missing entry); up/down/stop always enabled (`assumed_state`); the `set_position` timing routine unit-tested (target 40, 20 s travel → up 20 s then down 12 s).

## Still to do (incremental, one commit each)
1. **Confirm in the real HA box (0.1.8):** HACS update → restart → re-run setup (identify loop: nudge until shaded window matches, name it, set its travel time), then set_position lands near the requested % **per shade**, group fan-out, and the 3 s poll feels responsive. Confirm no more `AttributeError` in the log.
2. **`travel_time` accuracy** — it's a user-supplied guess (now per-shade); consider auto-measuring it during setup (drive up from known-down, time it) if the gateway reports `percent`.
3. **Tests** — `tests/` with pytest fixtures (mocked client) for `_parse_gateway`, the `set_position` timing sequence, and group/travel-time parsing. No live network in CI.
4. **Icon** — no `icons.png` currently; add real PowerShades branding before HACS listing.
5. **HACS listing** — `integration_type: hub`; request listing once install + tests are green.
