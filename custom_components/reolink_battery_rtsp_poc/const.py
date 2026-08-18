"""Constants for the isolated Reolink Battery RTSP PoC integration."""

DOMAIN = "reolink_battery_rtsp_poc"
SOURCE_DOMAIN = "reolink_battery"
MANUFACTURER = "Reolink"

CONF_SOURCE_ENTRY_ID = "source_entry_id"
CONF_GITHUB_DIAGNOSTICS_ENABLED = "github_diagnostics_enabled"
CONF_GITHUB_TOKEN = "github_token"

GITHUB_REPOSITORY = "Dmxsir/ha-reolink-battery-rtsp-poc"
GITHUB_DIAGNOSTICS_BRANCH = "diagnostics"
GITHUB_DIAGNOSTICS_PATH = "diagnostics/latest.json"
GITHUB_API_VERSION = "2026-03-10"

# Data keys owned by the source Reolink Battery config entry. They are copied
# as names only; credentials remain stored exclusively in the source entry.
SOURCE_CONF_UID = "uid"
SOURCE_CONF_DEVICE_NAME = "device_name"
SOURCE_CONF_MODEL = "model"
SOURCE_CONF_DEVICE_USERNAME = "device_username"
SOURCE_CONF_DEVICE_PASSWORD = "device_password"
SOURCE_CONF_INTERFACE = "interface"
