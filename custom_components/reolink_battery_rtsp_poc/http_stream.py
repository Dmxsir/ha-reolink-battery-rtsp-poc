"""Local-network HTTP H264 source for go2rtc."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from contextlib import suppress
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView

from .const import (
    DOMAIN,
    SOURCE_CONF_DEVICE_PASSWORD,
    SOURCE_CONF_DEVICE_USERNAME,
    SOURCE_CONF_INTERFACE,
    SOURCE_CONF_UID,
)
from .live_h264_source import async_stream_h264

_LOGGER = logging.getLogger(__name__)

HTTP_H264_PATH = "/api/reolink_battery_rtsp_poc/main.h264"
_MAX_QUEUED_FRAMES = 64
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _local_request(remote: str | None) -> bool:
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    if address.is_loopback or address.is_private:
        return True
    return isinstance(address, ipaddress.IPv4Address) and address in _CGNAT


def _loaded_entry(hass):
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]
    return entries[0] if len(entries) == 1 else None


class ReolinkBatteryH264View(HomeAssistantView):
    """Serve one on-demand Annex-B H264 stream to a local consumer."""

    url = HTTP_H264_PATH
    name = "api:reolink_battery_rtsp_poc:h264"
    requires_auth = False

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Wake the camera only for the lifetime of this HTTP connection."""
        if not _local_request(request.remote):
            raise web.HTTPForbidden()

        hass = request.app[KEY_HASS]
        entry = _loaded_entry(hass)
        if entry is None:
            raise web.HTTPServiceUnavailable(
                text="Exactly one loaded Reolink Battery RTSP PoC entry is required"
            )

        runtime = entry.runtime_data
        stream_lock = getattr(runtime, "stream_lock", None)
        if stream_lock is None:
            raise web.HTTPServiceUnavailable(text="PoC stream runtime is unavailable")
        if stream_lock.locked():
            raise web.HTTPConflict(text="A live H264 consumer is already active")

        source = hass.config_entries.async_get_entry(runtime.source_entry_id)
        if source is None or source.runtime_data is None:
            raise web.HTTPServiceUnavailable(text="Source integration is unavailable")

        operation_lock = getattr(source.runtime_data, "local_operation_lock", None)
        if operation_lock is None:
            raise web.HTTPServiceUnavailable(text="Source operation lock is unavailable")

        required = (
            SOURCE_CONF_UID,
            SOURCE_CONF_DEVICE_USERNAME,
            SOURCE_CONF_DEVICE_PASSWORD,
            SOURCE_CONF_INTERFACE,
        )
        if any(not source.data.get(key) for key in required):
            raise web.HTTPServiceUnavailable(text="Source local credentials are incomplete")

        response = web.StreamResponse(
            status=HTTPStatus.OK,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
        response.content_type = "video/h264"
        await response.prepare(request)

        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_MAX_QUEUED_FRAMES)
        stop_event = asyncio.Event()

        def _frame_sink(payload: bytes, _frame_type: str) -> None:
            if stop_event.is_set():
                return
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Never drop H264 reference frames silently. Terminate the
                # producer instead so a reconnect starts from a clean keyframe.
                stop_event.set()

        async def _run_source() -> None:
            async with stream_lock:
                async with operation_lock:
                    await async_stream_h264(
                        source.data[SOURCE_CONF_UID],
                        source.data[SOURCE_CONF_DEVICE_USERNAME],
                        source.data[SOURCE_CONF_DEVICE_PASSWORD],
                        ipaddress.ip_interface(source.data[SOURCE_CONF_INTERFACE]),
                        frame_sink=_frame_sink,
                        stop_event=stop_event,
                    )

        source_task = asyncio.create_task(_run_source())
        try:
            while True:
                if source_task.done() and queue.empty():
                    break

                frame_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {frame_task, source_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if frame_task in done:
                    await response.write(frame_task.result())
                else:
                    frame_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await frame_task

                if source_task in done:
                    with suppress(Exception):
                        await source_task
                    if queue.empty():
                        break
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            stop_event.set()
        finally:
            stop_event.set()
            if not source_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(source_task), timeout=8.0)
                except (TimeoutError, asyncio.TimeoutError):
                    source_task.cancel()
                except Exception as err:  # secret-safe: type only
                    _LOGGER.debug("H264 source ended with %s", type(err).__name__)
            with suppress(asyncio.CancelledError, Exception):
                await source_task
            with suppress(ConnectionResetError, RuntimeError):
                await response.write_eof()

        return response
