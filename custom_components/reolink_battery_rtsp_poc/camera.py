"""Camera platform for the Reolink Battery live-view PoC."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.entity import DeviceInfo

from . import ReolinkBatteryRtspPocConfigEntry, source_entry_for
from .const import (
    DOMAIN,
    MANUFACTURER,
    SOURCE_CONF_DEVICE_NAME,
    SOURCE_CONF_MODEL,
    SOURCE_CONF_UID,
)
from .http_stream import ReolinkBatteryAvHub

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass,
    entry: ReolinkBatteryRtspPocConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the on-demand Argus camera entity."""
    async_add_entities((ReolinkBatteryLiveCamera(entry),))


class ReolinkBatteryLiveCamera(Camera):
    """Expose the go2rtc bridge as a native Home Assistant camera entity."""

    _attr_has_entity_name = True
    _attr_translation_key = "live_view"
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = MANUFACTURER

    def __init__(self, entry: ReolinkBatteryRtspPocConfigEntry) -> None:
        super().__init__()
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_live_camera"

        source = source_entry_for(
            entry.runtime_data.av_hub.hass,
            entry.runtime_data.source_entry_id,
        ) if isinstance(entry.runtime_data.av_hub, ReolinkBatteryAvHub) else None
        source_data = source.data if source is not None else {}
        self._attr_model = source_data.get(SOURCE_CONF_MODEL, "Battery camera")

    def _source_entry(self):
        return source_entry_for(
            self.hass, self._entry.runtime_data.source_entry_id
        )

    @property
    def available(self) -> bool:
        """Return whether the source integration and bridge URL are available."""
        source = self._source_entry()
        bridge = self._entry.runtime_data.go2rtc_bridge
        return bool(
            source is not None
            and source.runtime_data is not None
            and bridge is not None
            and bridge.rtsp_url
        )

    @property
    def is_streaming(self) -> bool:
        """Return whether the shared camera session currently has a producer."""
        hub = self._entry.runtime_data.av_hub
        return bool(isinstance(hub, ReolinkBatteryAvHub) and hub.is_active)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the PoC device information."""
        source = self._source_entry()
        source_data = source.data if source is not None else {}
        uid = source_data.get(SOURCE_CONF_UID, self._entry.entry_id)
        return DeviceInfo(
            identifiers={(DOMAIN, uid)},
            manufacturer=MANUFACTURER,
            model=source_data.get(SOURCE_CONF_MODEL, "Battery camera"),
            name=f"RTSP PoC - {source_data.get(SOURCE_CONF_DEVICE_NAME, 'Reolink Battery')}",
        )

    async def stream_source(self) -> str | None:
        """Return the go2rtc RTSP source used by Home Assistant stream."""
        bridge = self._entry.runtime_data.go2rtc_bridge
        return bridge.rtsp_url if bridge is not None else None

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Do not wake the battery camera merely to generate dashboard stills."""
        return None
