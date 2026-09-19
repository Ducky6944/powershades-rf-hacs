# PowerShades for Home Assistant

A HACS-installable custom integration for **PowerShades** motorized window shades,
driven over the **local RF gateway** (the integration's own web server on the
gateway at your network). No cloud, no email/password, no API keys — the gateway
is the integration.

## Features

- **Cover** per RF channel — open / close / stop, plus **set to N%** (emulated:
  the gateway only knows up / down / stop, so we time a fractional full-travel move).
  - Both the **state** (open / closed / unknown) and the **position slider** are driven by
    your **live estimate** (what we last commanded), not the gateway's live read. If you *trust*
    the gateway's live read for a particular shade, set its **position source** to *gateway*
    in setup or the options flow.
- **Cover** per **user group** — open / close / stop and **set to N%** all fan out to
  every member channel. The group shows a **position slider only when all members
  agree**; if the shades are spread out, the position reads **unknown** and the extra
  attributes set `position_mixed: true` (with `position_min` / `position_max`).
- **Button** per channel — **Reset** (drive fully open and lock the position at 100%),
  to re-sync a shade whose recorded position has drifted.
- **Number** per channel — **travel time** (seconds for a full 0↔100% travel). Editable
  at any time; drives the accuracy of "set to N%".
- **Sensors** per channel (diagnostic) — gateway **battery**, **RF level (rx)**, device id,
  and an **estimated position** — only created for channels that actually have a paired device.

## How it works

| Concern | How |
|---|---|
| **Control** (open / close / stop / set %, groups) | Local gateway HTTP (`ajax.shtml`) |
| **State / feedback** (battery, rx, device id) | Local gateway HTTP (`ajax.shtml`) |
| **"Set to N%"** | Timed move — up/down for a fraction of a full sweep (`travel_time` is per-channel adjustable) |
| **Position read-out** | The *estimate* (our own timer math) — always shown, independent of the gateway. Opt-in per channel to *prefer* the live read when available |
| **Cover state (open / closed / unknown)** | Derived from the same estimate-based position, so state and slider always agree |
| **Transient gateway hiccups** | Short retry with backoff on connection drops (e.g. "connection reset by peer") before giving up |

The gateway is organized by **RF channel** (1–30), not by shade name. Pair a shade to a
channel in the gateway's web UI (`/device.shtml` → **Pair / Link Feedback**), and that
channel becomes a cover here.

## Semantics

- Gateway `percent`: `0` = fully closed/down, `100` = fully open/up.
- Home Assistant cover position: `0` = closed, `100` = open. The mapping is identity.
- **Set to N%** uses your `travel_time` (per channel) to time a move. Keep it close to
  the shade's real full-travel time, or use the **Reset** button to re-baseline after drift.

## Install (HACS)

1. HACS → **Settings → Custom repositories → +** → add this repo's URL.
2. HACS → **Integrations → PowerShades → Install**.
3. Restart Home Assistant.
4. Settings → **Devices & Services → Add integration → "PowerShades"** — enter the
   gateway's IP/hostname and a *base* travel time, then identify each channel
   (nudge up/down until you see the shade move, name it, set its travel time and
   position source), then optionally define groups.

## Semantics of "set to N%"

- When we already know a channel's position, we move the **shortest** direction
  for just the needed distance — no full-travel calibration leg.
- When the position is **unknown** (e.g. first move after a Home Assistant restart),
  we calibrate first: full up (≈ `travel_time`, known 100%), then down by the remainder.
- If your position is out of sync, press the channel's **Reset** button — it drives
  fully open and locks the position at 100%.

## Development

```bash
git clone <this-repo>
python3 -c "import custom_components.powershades.client as c; print(c.PowerShadesClient)"
```

Lint:

```bash
pip install -r requirements-dev.txt
ruff check custom_components/powershades/
ruff format --check custom_components/powershades/
```

## Layout

```
custom_components/powershades/
  __init__.py        # setup / unload, forwards platforms
  const.py           # domain, config keys, gateway endpoints
  client.py          # local gateway client (ajax.shtml read / up / down / stop)
  _movement.py       # timed move-to + open fully + relative shortest-path move
  config_flow.py     # gateway → per-channel (nudge/name/travel/source) → groups
  coordinator.py     # DataUpdateCoordinator: 3s gateway poll, state + device info
  cover.py           # one cover per channel (+ set position) and per group
  button.py          # per-channel "Reset (open fully)" button
  number.py          # per-channel travel-time number (editable)
  sensor.py          # per-channel diagnostics (battery / rx / device id / estimate)
  types.py           # shared data classes
  brand/             # HACS logo / icon (512x512)
  strings.json       # config + options flow UI strings
  translations/en.json
```

## Notes / limitations

- **Position is set-and-optimistic by design**: the gateway reports a `percent` but
  it can be stale or jump while a shade is mid-travel, so the integration defaults to
  showing your *estimated* position (what you last commanded). You can opt into the
  gateway's live read per channel if you prefer.
- **The gateway has no "set to N%"** endpoint — only up / down / stop. "Set position"
  is a timed approximation; its accuracy depends on how close `travel_time` is to the
  shade's real full-travel time. Use **Reset** to re-baseline after drift.
- **Restart behavior**: after a Home Assistant restart we can't remember each channel's
  position, so the first "set to N%" per channel calibrates from fully open. Open/close
  always work (they end-stop on their own).
- **Polling**: gateway state polled every 3 s.

## Lessons learned (for contributors)

Hard-won, non-obvious things — read before changing the setup/control code:

- **The gateway has no absolute "set to N%"** — only `up` / `down` / `stop`.
  "Set position" is a *timed* move; its accuracy is entirely `travel_time` × distance.
- **`set_position` must know the start.** When it does (our estimate), it moves the
  *shortest* way. When it doesn't (after a restart), it calibrates from fully open.
  Never both — record the estimate *before* overwriting it, or `from_position == target`
  and nothing moves.
- **The estimate is the source of truth — don't gate it on the live read.**
  `resolve_position` returns `estimate(channel)` **independent** of whether the channel
  appears in the last gateway poll. If it first did `ch = data.channel(n); if ch is None:
  return None`, the moment a channel dropped out of a read (flaky gateway, no `percent`
  AND no `device_id`, or a timed-out poll) a perfectly-known 25 became **Unknown**. Group
  + shade positions are *always* calculated off our own timer math, not what the RF gate reports.
- **`position_source = gateway` = "prefer live read, fall back to estimate."** It never
  forces Unknown on its own: if the gateway has a `percent` for that channel, show it;
  otherwise show the estimate. This is the "use the gate if it's actually reporting" case.
- **Cover *state* and *position* must come from the same source.** A cover's open/closed/
  **unknown** state is driven by `is_closed`; if that still read the flaky gateway `percent`
  while the slider read the estimate, the shade showed **Unknown at every percentage** even
  though the slider was right. Derive both from `resolve_position(channel)` (the estimate).
- **The local gateway drops idle/reused sockets** (`[Errno 104] Connection reset by peer`).
  Retry transient `ClientConnectionError`/`TimeoutError` a couple times with a short backoff
  before giving up — otherwise a single blip makes a whole "set to N%" leg "fail" in the log.
- **Use `assumed_state=True` on covers** so Open/Close buttons stay enabled no matter
  what the position reads (the HA frontend greys them out on `open`/`closed` otherwise).
- **`async_show_form` here is called with `step_id=` / `data_schema=` kwargs.** (This HA
  build has no `ConfigEntryOptionsFlow` — use `OptionsFlowWithConfigEntry(config_entry)`.)
- **`vol.Coerce(float)` raises on `""`** — for optional numeric fields, coerce to `str`
  and parse in the handler (blank = unchanged).
- **A module-level helper called as `self.method(...)` is an `AttributeError` on every
  refresh** (looked like "setup failed"). Call it as a bare function.
