"""Isolated Reolink Battery RTSP/Live View proof-of-concept integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_SOURCE_ENTRY_ID, DOMAIN, SOURCE_DOMAIN
from .http_stream import (
    ReolinkBatteryAacView,
    ReolinkBatteryAvHub,
    ReolinkBatteryH264View,
)

PLATFORMS = (Platform.BUTTON,)
_HTTP_VIEW_REGISTERED = "http_view_registered"


@dataclass(slots=True)
class ReolinkBatteryRtspPocRuntime:
    """Runtime data for the isolated PoC."""

    source_entry_id: str
    stream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    av_hub: object | None = None


ReolinkBatteryRtspPocConfigEntry = ConfigEntry[ReolinkBatteryRtspPocRuntime]


def source_entry_for(
    hass: HomeAssistant, source_entry_id: str
) -> ConfigEntry | None:
    """Resolve the referenced production Reolink Battery config entry."""
    return next(
        (
            item
            for item in hass.config_entries.async_entries(SOURCE_DOMAIN)
            if item.entry_id == source_entry_id
        ),
        None,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ReolinkBatteryRtspPocConfigEntry
) -> bool:
    """Set up the isolated PoC without modifying the source integration."""
    source_entry_id = entry.data[CONF_SOURCE_ENTRY_ID]
    if source_entry_for(hass, source_entry_id) is None:
        return False

    entry.runtime_data = ReolinkBatteryRtspPocRuntime(
        source_entry_id=source_entry_id
    )

    # Forward the button platform first. Importing it installs the already
    # hardware-proven PoC transport/ACK/audio observers before an HTTP consumer
    # can create a live source session.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.runtime_data.av_hub = ReolinkBatteryAvHub(hass, entry)

    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(_HTTP_VIEW_REGISTERED):
        hass.http.register_view(ReolinkBatteryH264View())
        hass.http.register_view(ReolinkBatteryAacView())
        domain_data[_HTTP_VIEW_REGISTERED] = True

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ReolinkBatteryRtspPocConfigEntry
) -> bool:
    """Unload the isolated PoC and stop any active on-demand stream."""
    hub = getattr(entry.runtime_data, "av_hub", None)
    if isinstance(hub, ReolinkBatteryAvHub):
        await hub.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
