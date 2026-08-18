"""Manual controls for the isolated Reolink Battery RTSP PoC."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo

from . import ReolinkBatteryRtspPocConfigEntry, source_entry_for
from .const import (
    DOMAIN,
    MANUFACTURER,
    SOURCE_CONF_DEVICE_NAME,
    SOURCE_CONF_DEVICE_PASSWORD,
    SOURCE_CONF_DEVICE_USERNAME,
    SOURCE_CONF_INTERFACE,
    SOURCE_CONF_MODEL,
    SOURCE_CONF_UID,
)
from .diagnostics import async_get_config_entry_diagnostics
from .github_upload import (
    async_upload_diagnostics,
    github_upload_configured,
)
from .live_stream_buffer_compat import install_live_buffer_compat
from .live_stream_compat import install_preauth_heartbeat_compat
from .live_stream_diagnostics import (
    apply_live_probe_error,
    apply_live_probe_result,
    apply_parser_telemetry,
    reset_live_probe_state,
)
from .live_stream_probe import LiveStreamProbeError, async_probe_live_stream
from .media_activity_telemetry import (
    install_media_activity_telemetry,
    snapshot_media_activity_telemetry,
)
from .probe_parser_telemetry import (
    install_parser_telemetry,
    snapshot_parser_telemetry,
)
from .udp_media_keepalive import install_udp_media_keepalive

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


# Keep the PoC transport behavior aligned with the physically proven production
# UID/P2P lifetime while remaining entirely inside this experimental domain.
install_preauth_heartbeat_compat()
# Preserve every complete cmd3/cmd4 message when a receive buffer already holds
# multiple Baichuan messages.
install_live_buffer_compat()
# Observation-only parser wrapper. Install this after the compatibility layers so
# it cannot bypass the working auth/stream behavior.
install_parser_telemetry()
# Neolink-style Baichuan UDP cmd234 keepalive.
install_udp_media_keepalive()
# Timing-only observation wrapper. Install last so it sees the final stream stack
# without altering any protocol bytes.
install_media_activity_telemetry()


PROBE_DESCRIPTION = ButtonEntityDescription(
    key="probe_live_stream",
    translation_key="probe_live_stream",
)


async def async_setup_entry(
    hass,
    entry: ReolinkBatteryRtspPocConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities((ReolinkProbeLiveStreamButton(entry),))


class ReolinkProbeLiveStreamButton(ButtonEntity):
    """Run one explicit 20-second main-stream probe."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description = PROBE_DESCRIPTION

    def __init__(self, entry: ReolinkBatteryRtspPocConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_probe_live_stream"

    def _source_entry(self):
        return source_entry_for(
            self.hass, self._entry.runtime_data.source_entry_id
        )

    async def _async_upload_diagnostics_if_enabled(self) -> None:
        """Upload the sanitized diagnostics document without affecting the probe."""
        if not github_upload_configured(self._entry):
            return
        diagnostics = await async_get_config_entry_diagnostics(
            self.hass, self._entry
        )
        live = diagnostics.get("live_stream_probe")
        if isinstance(live, dict):
            activity = snapshot_media_activity_telemetry()
            live["bounded_duration_seconds"] = activity.get(
                "probe_duration_seconds", 20.0
            )
            live["media_activity"] = activity
        await async_upload_diagnostics(self.hass, self._entry, diagnostics)

    @property
    def available(self) -> bool:
        source = self._source_entry()
        return bool(
            source is not None
            and source.runtime_data is not None
            and getattr(source.runtime_data, "local_operation_lock", None) is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        source = self._source_entry()
        source_data = source.data if source is not None else {}
        uid = source_data.get(SOURCE_CONF_UID, self._entry.entry_id)
        return DeviceInfo(
            identifiers={(DOMAIN, uid)},
            manufacturer=MANUFACTURER,
            model=source_data.get(SOURCE_CONF_MODEL, "Battery camera"),
            name=f"RTSP PoC - {source_data.get(SOURCE_CONF_DEVICE_NAME, 'Reolink Battery')}",
        )

    async def async_press(self) -> None:
        source = self._source_entry()
        if source is None:
            raise HomeAssistantError("SOURCE_INTEGRATION_NOT_FOUND")
        if source.runtime_data is None:
            raise HomeAssistantError("SOURCE_INTEGRATION_NOT_LOADED")

        lock = getattr(source.runtime_data, "local_operation_lock", None)
        if lock is None:
            raise HomeAssistantError("SOURCE_OPERATION_LOCK_UNAVAILABLE")

        required = (
            SOURCE_CONF_UID,
            SOURCE_CONF_DEVICE_USERNAME,
            SOURCE_CONF_DEVICE_PASSWORD,
            SOURCE_CONF_INTERFACE,
        )
        if any(not source.data.get(key) for key in required):
            raise HomeAssistantError("SOURCE_LOCAL_CREDENTIALS_INCOMPLETE")

        reset_live_probe_state(self._entry.entry_id, stream_kind="main")
        probe_error: LiveStreamProbeError | None = None
        result = None
        try:
            # Share only the source integration's operation lock. The PoC has its
            # own transport/session, so a recording download and live probe can
            # never wake/use the battery camera concurrently.
            async with lock:
                result = await async_probe_live_stream(
                    source.data[SOURCE_CONF_UID],
                    source.data[SOURCE_CONF_DEVICE_USERNAME],
                    source.data[SOURCE_CONF_DEVICE_PASSWORD],
                    ipaddress.ip_interface(source.data[SOURCE_CONF_INTERFACE]),
                    stream="main",
                    duration=20.0,
                )
        except LiveStreamProbeError as err:
            probe_error = err
            apply_live_probe_error(self._entry.entry_id, err)
        else:
            apply_live_probe_result(self._entry.entry_id, result)

        # Parser telemetry is metadata-only and is captured even when the live
        # session itself fails. This lets the next diagnostic explain exactly
        # how far the BcMedia stream progressed without exposing media bytes.
        apply_parser_telemetry(
            self._entry.entry_id,
            snapshot_parser_telemetry(),
        )

        # Export after telemetry has been finalized, including failed probes.
        # GitHub failures are tracked separately and never replace the camera
        # probe result.
        await self._async_upload_diagnostics_if_enabled()

        if probe_error is not None:
            raise HomeAssistantError(probe_error.stage) from None
        if result is None or not result.trace.bcmedia_observed:
            raise HomeAssistantError("LIVE_MEDIA_NOT_OBSERVED")
