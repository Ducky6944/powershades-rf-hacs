"""Constants for the PowerShades integration."""

from homeassistant.const import Platform

DOMAIN = "powershades"

PLATFORMS = [Platform.BUTTON, Platform.COVER, Platform.SENSOR]

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_BASE_URL = "base_url"
CONF_GATEWAY = "gateway"

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
