# PowerShades for Home Assistant

A HACS-installable custom integration for **PowerShades** motorized window shades, driven by the official **PowerShades cloud API** plus (optionally) the local **RF Gateway** for live state feedback.

## Features

- **Covers** for every shade in your account — open / close / set position (percentage).
- **Covers** for every named **group** — open / close / set position.
- **Buttons** for every named **scene** (one press = activate the scene).
- **Sensors** (per local-gateway channel) for **position**, **battery**, and **RF level** — only created for channels that have a paired device.
- **JWT auth** (email + password) with automatic token refresh and re-login on expiry.

## How it works

| Concern | Transport |
|---|---|
| **Control** (open / close / set %, groups, scenes) | PowerShades **cloud REST API** (`api.powershades.com`) — the canonical control plane. |
| **State / feedback** (position, battery, RF) | Local **RF Gateway** web server (`ajax.shtml`) — optional; only reports for channels that have a paired device. |

> The cloud API has **no per-shade position read** available to consumer accounts (the `/devices/` endpoint is dealer-gated). Live position comes from the **local gateway**, which is organized by **RF channel** (not by shade name). We do **not** try to map channels to shade names; the two planes are exposed independently.

To get live positions, pair a shade to a gateway channel via the gateway web UI (`/device.shtml` → **Pair / Link Feedback**), then the corresponding channel sensor appears.

## Semantics

- Cloud `percentage`: `0` = fully open/raised, `100` = fully down/closed.
- Home Assistant cover position: `0` = closed, `100` = open.
- The integration maps these automatically (HA `0` → cloud `100`, HA `100` → cloud `0`).

## Install (HACS)

1. In HACS: **Settings → Custom repositories → **+** → add this repo's URL** (or install as a local integration).
2. HACS → **Integrations → search "PowerShades" → Install**.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add integration → "PowerShades"**.

## Config flow

| Field | Required | Notes |
|---|---|---|
| **Email** | yes | Your PowerShades cloud account email |
| **Password** | yes | That account's password |
| **API base URL** | no | Default `https://api.powershades.com` (self-hosted / test override) |
| **Local RF Gateway address** | no | e.g. `192.0.2.48` (no scheme, IP only) — enables live sensors |

## Development

```bash
git clone <this-repo>
# test the client against your account without HA:
python3 -c "import custom_components.powershades.client as c; print(c.PowerShadesClient)"
```

Run the linter:

```bash
pip install -r requirements-dev.txt
ruff check custom_components/powershades/
```

## Layout

```
custom_components/powershades/
  __init__.py       # setup / unload, forwards platforms
  const.py          # domain, config keys, endpoint paths
  client.py         # JWT auth + cloud API (aiohttp)
  config_flow.py    # email/password flow, validates login
  coordinator.py    # DataUpdateCoordinator: cloud lists + gateway state
  cover.py          # covers (shades + groups)
  button.py         # scene buttons
  sensor.py         # per-channel gateway sensors (position/battery/rx)
  strings.json      # config flow UI strings
  translations/en.json
  icons.png         # HACS icon
```

## Notes / limitations

- **Position is "set-and-optimistic" until a local gateway channel is paired** — the cloud API has no consumer-readable per-shade position. Once paired, the channel sensors show the real reported position.
- Cloud tokens are short-lived; the client refreshes on 401 and re-logs if the refresh fails.
- Polling: gateway state is checked every ~10 s; cloud lists are cached and re-fetched at most every few minutes to avoid hammering the cloud.
