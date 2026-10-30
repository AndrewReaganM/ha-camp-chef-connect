"""Constants for the Camp Chef Connect integration."""

from homeassistant.const import Platform

DOMAIN = "camp_chef_connect"
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
]

API_URL = "https://api.campchef.app"
WS_URL = "wss://ws.api.campchef.app/?path=multiplex"

CONF_PIN = "pin"

DEFAULT_MIN_TEMP_F = 160
DEFAULT_MAX_TEMP_F = 500
TEMP_STEP_F = 5
SMOKE_MIN = 1
SMOKE_MAX = 10
DEFAULT_SMOKE = 3
