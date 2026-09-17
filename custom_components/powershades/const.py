"""Constants for the PowerShades integration."""

from homeassistant.const import Platform

DOMAIN = "powershades"

PLATFORMS = [Platform.BUTTON, Platform.COVER, Platform.SENSOR]

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_API_KEY = "api_key"
CONF_BASE_URL = "base_url"
CONF_GATEWAY = "gateway"
# Optional user-supplied channel -> name map (when names can't be resolved).
CONF_CHANNEL_NAMES = "channel_names"

# How credentials are provided (config flow) — auth mode is derived from keys present.
AUTH_MODE_EMAIL = "email"
AUTH_MODE_API_KEY = "api_key"

DEFAULT_BASE_URL = "https://api.powershades.com"

# API paths
AUTH_JWT = "/auth/jwt/"
AUTH_JWT_REFRESH = "/auth/jwt/refresh/"
SHADES = "/shades/"
SHADES_MOVE = "/shades/move/"
GROUPS = "/groups/"
GROUPS_MOVE = "/groups/move/"
SCENES = "/scenes/"
SCENES_MOVE = "/scenes/move/"
SHADE_ATTRIBUTES = "/shadeattributes/"

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
