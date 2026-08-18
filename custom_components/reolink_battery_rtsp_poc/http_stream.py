"""Local-network HTTP H264/AAC sources for go2rtc.

Both endpoints share one Argus Baichuan session. The first consumer wakes the
camera; the session remains active while at least one video/audio consumer is
connected and closes when the final consumer disconnects.
"""

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
HTTP_AAC_PATH = "/api/reolink_battery_rtsp_poc/main.aac"
_MAX_VIDEO_QUEUE = 64
_MAX_AUDIO_QUEUE = 512
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


class ReolinkBatteryAvHub:
    """Fan out one on-demand camera session to H264 and AAC HTTP consumers."""

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._guard = asyncio.Lock()
        self._video_queues: set[asyncio.Queue[bytes | None]] = set()
        self._audio_queues: set[asyncio.Queue[bytes | None]] = set()
        self._producer_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    async def subscribe(self, kind: str) -> asyncio.Queue[bytes | None]:
        maxsize = _MAX_VIDEO_QUEUE if kind == "video" else _MAX_AUDIO_QUEUE
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=maxsize)
        async with self._guard:
            target = self._video_queues if kind == "video" else self._audio_queues
            target.add(queue)
            if self._producer_task is None or self._producer_task.done():
                self._stop_event = asyncio.Event()
                self._producer_task = asyncio.create_task(self._run_source())
        return queue

    async def unsubscribe(self, kind: str, queue: asyncio.Queue[bytes | None]) -> None:
        async with self._guard:
            target = self._video_queues if kind == "video" else self._audio_queues
            target.discard(queue)
            if not self._video_queues and not self._audio_queues and self._stop_event is not None:
                self._stop_event.set()

    def _fanout(self, queues: set[asyncio.Queue[bytes | None]], payload: bytes) -> None:
        for queue in tuple(queues):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Preserve A/V integrity. Reconnect from a fresh keyframe rather
                # than silently dropping reference video or desynchronizing audio.
                if self._stop_event is not None:
                    self._stop_event.set()
                return

    def _video_sink(self, payload: bytes, _frame_type: str) -> None:
        self._fanout(self._video_queues, payload)

    def _audio_sink(self, payload: bytes, codec: str) -> None:
        # The hardware probe proved this Argus emits AAC with ADTS headers.
        if codec == "aac":
            self._fanout(self._audio_queues, payload)

    async def _run_source(self) -> None:
        runtime = self.entry.runtime_data
        source = self.hass.config_entries.async_get_entry(runtime.source_entry_id)
        stop_event = self._stop_event
        if source is None or source.runtime_data is None or stop_event is None:
            await self._finish_producer()
            return

        operation_lock = getattr(source.runtime_data, "local_operation_lock", None)
        stream_lock = getattr(runtime, "stream_lock", None)
        required = (
            SOURCE_CONF_UID,
            SOURCE_CONF_DEVICE_USERNAME,
            SOURCE_CONF_DEVICE_PASSWORD,
            SOURCE_CONF_INTERFACE,
        )
        if (
            operation_lock is None
            or stream_lock is None
            or any(not source.data.get(key) for key in required)
        ):
            await self._finish_producer()
            return

        try:
            async with stream_lock:
                async with operation_lock:
                    await async_stream_h264(
                        source.data[SOURCE_CONF_UID],
                        source.data[SOURCE_CONF_DEVICE_USERNAME],
                        source.data[SOURCE_CONF_DEVICE_PASSWORD],
                        ipaddress.ip_interface(source.data[SOURCE_CONF_INTERFACE]),
                        frame_sink=self._video_sink,
                        audio_sink=self._audio_sink,
                        stop_event=stop_event,
                    )
        except Exception as err:  # secret-safe: class only
            _LOGGER.debug("Shared A/V source ended with %s", type(err).__name__)
        finally:
            await self._finish_producer()

    async def _finish_producer(self) -> None:
        async with self._guard:
            for queue in tuple(self._video_queues) + tuple(self._audio_queues):
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(None)
            self._producer_task = None
            self._stop_event = None

    async def async_stop(self) -> None:
        async with self._guard:
            task = self._producer_task
            if self._stop_event is not None:
                self._stop_event.set()
        if task is not None and not task.done():
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class _ReolinkBatteryMediaView(HomeAssistantView):
    """Base view for one track from the shared on-demand Argus session."""

    requires_auth = False
    media_kind = "video"
    content_type_value = "application/octet-stream"

    async def get(self, request: web.Request) -> web.StreamResponse:
        if not _local_request(request.remote):
            raise web.HTTPForbidden()

        hass = request.app[KEY_HASS]
        entry = _loaded_entry(hass)
        if entry is None:
            raise web.HTTPServiceUnavailable(
                text="Exactly one loaded Reolink Battery RTSP PoC entry is required"
            )

        hub = getattr(entry.runtime_data, "av_hub", None)
        if not isinstance(hub, ReolinkBatteryAvHub):
            raise web.HTTPServiceUnavailable(text="PoC A/V runtime is unavailable")

        response = web.StreamResponse(
            status=HTTPStatus.OK,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
        response.content_type = self.content_type_value
        await response.prepare(request)

        queue = await hub.subscribe(self.media_kind)
        try:
            while True:
                payload = await queue.get()
                if payload is None:
                    break
                await response.write(payload)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            await hub.unsubscribe(self.media_kind, queue)
            with suppress(ConnectionResetError, RuntimeError):
                await response.write_eof()

        return response


class ReolinkBatteryH264View(_ReolinkBatteryMediaView):
    """Serve the H264 Annex-B track."""

    url = HTTP_H264_PATH
    name = "api:reolink_battery_rtsp_poc:h264"
    media_kind = "video"
    content_type_value = "video/h264"


class ReolinkBatteryAacView(_ReolinkBatteryMediaView):
    """Serve the AAC ADTS track."""

    url = HTTP_AAC_PATH
    name = "api:reolink_battery_rtsp_poc:aac"
    media_kind = "audio"
    content_type_value = "audio/aac"
