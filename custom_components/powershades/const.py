"""Constants for the PowerShades integration."""

from homeassistant.const import Platform

DOMAIN = "powershades"

PLATFORMS = [Platform.COVER, Platform.SENSOR]

CONF_GATEWAY = "gateway"
# Optional user-supplied channel -> name map (when names can't be resolved).
CONF_CHANNEL_NAMES = "channel_names"
# Optional user-defined local groups: {"<name>": [ch1, ch2, ...], ...}.
CONF_GROUPS = "groups"
# Seconds a shade takes to travel 0->100% (full sweep). Used to approximate
# "set to X%" by timing a down command after an initial stop.
CONF_TRAVEL_TIME = "travel_time"
# Per-channel override of the full-sweep travel time (seconds):
# {"<channel>": seconds, ...}. Absent / missing key = use CONF_TRAVEL_TIME.
CONF_TRAVEL_TIMES = "travel_times"
# Diagnostic metrics the user confirmed are available on their gateway:
# one or more of "percent", "battery", "rx", "device_id". Absent = infer at
# setup time (any key seen as non-None in the first gateway read).
CONF_AVAILABLE = "available"
# Per-channel source of "current position" shown by the cover UI:
# {"<channel>": "estimate"|"gateway", ...}. Absent / missing key = "estimate"
# (our own time-based record of the last command), which is the default because
# the gateway's live read can lag or jump while a shade is mid-travel.
CONF_POSITION_SOURCE = "position_source"
# Values for ``position_source``.
POSITION_SOURCE_ESTIMATE = "estimate"
POSITION_SOURCE_GATEWAY = "gateway"

# Local RF gateway ajax vars (colon-separated 30 channels unless noted)
GW_VAR_PERCENT = "percent"
GW_VAR_BATTERY = "battery"
GW_VAR_RX = "rx"
GW_VAR_RFDEVS = "rfdevs"
GW_CHNAME_VARS = ("chnames1", "chnames2", "chnames3")
GW_VARIABLES = (
    GW_VAR_PERCENT,
    GW_VAR_BATTERY,
    GW_VAR_RX,
    GW_VAR_RFDEVS,
    *GW_CHNAME_VARS,
)
GW_AJAX_PATH = "/ajax.shtml"
# Local gateway up/down/stop control (GET with a single channel param).
GW_CMD_QUERY = "ajax.shtml"
GW_CMD_UP = "up"
GW_CMD_DOWN = "down"
GW_CMD_STOP = "stop"

# Diagnostic metric keys (values used to gate sensor creation).
METRIC_PERCENT = "percent"
METRIC_BATTERY = "battery"
METRIC_RX = "rx"
METRIC_DEVICE_ID = "device_id"
