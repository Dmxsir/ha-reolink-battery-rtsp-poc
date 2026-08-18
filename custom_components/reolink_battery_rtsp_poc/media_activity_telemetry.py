"""Timing-only telemetry for the bounded Argus live-view PoC.

This module measures when sanitized media bytes arrive during the manual probe.
It never stores or exports raw BcMedia/H264/H265/audio payload bytes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import live_stream_probe as probe
from .live_stream_compat import _media_chunk_from_frame
from .live_stream_probe import _LiveStreamConnection

_INSTALLED = False


@dataclass(slots=True)
class MediaActivityTelemetry:
    probe_duration_seconds: float = 0.0
    media_bytes_seen: int = 0
    first_half_media_bytes: int = 0
    second_half_media_bytes: int = 0
    first_media_offset_seconds: float | None = None
    last_media_offset_seconds: float | None = None
    cmd3_frames_first_half: int = 0
    cmd3_frames_second_half: int = 0
    raw_values_exposed: bool = False


_LAST = MediaActivityTelemetry()


def _reset() -> None:
    global _LAST
    _LAST = MediaActivityTelemetry()


def snapshot_media_activity_telemetry() -> dict[str, Any]:
    """Return a detached secret-safe snapshot of the latest probe activity."""
    return asdict(_LAST)


def install_media_activity_telemetry() -> None:
    """Install timing-only observation wrappers exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_prepare_live = _LiveStreamConnection.prepare_live_probe
    original_observe_live_frame = _LiveStreamConnection._observe_live_frame
    original_send_live_stream_probe = _LiveStreamConnection.send_live_stream_probe

    def _prepare_live_probe(self, *args: Any, **kwargs: Any):
        _reset()
        self._poc_activity_started_at = None
        self._poc_activity_duration = 0.0
        return original_prepare_live(self, *args, **kwargs)

    def _observe_live_frame(self, frame) -> None:
        if frame.cmd_id == probe.LIVE_START_CMD_ID and frame.body:
            chunk = _media_chunk_from_frame(self, frame)
            started = getattr(self, "_poc_activity_started_at", None)
            duration = float(getattr(self, "_poc_activity_duration", 0.0) or 0.0)
            if chunk and isinstance(started, (int, float)):
                offset = max(0.0, self._loop.time() - float(started))
                _LAST.media_bytes_seen += len(chunk)
                if _LAST.first_media_offset_seconds is None:
                    _LAST.first_media_offset_seconds = round(offset, 3)
                _LAST.last_media_offset_seconds = round(offset, 3)
                if duration > 0 and offset >= duration / 2.0:
                    _LAST.second_half_media_bytes += len(chunk)
                    _LAST.cmd3_frames_second_half += 1
                else:
                    _LAST.first_half_media_bytes += len(chunk)
                    _LAST.cmd3_frames_first_half += 1

        original_observe_live_frame(self, frame)

    async def _send_live_stream_probe(self, *args: Any, **kwargs: Any):
        duration = float(kwargs.get("duration", 0.0) or 0.0)
        _LAST.probe_duration_seconds = duration
        self._poc_activity_duration = duration
        self._poc_activity_started_at = self._loop.time()
        return await original_send_live_stream_probe(self, *args, **kwargs)

    _LiveStreamConnection.prepare_live_probe = _prepare_live_probe
    _LiveStreamConnection._observe_live_frame = _observe_live_frame
    _LiveStreamConnection.send_live_stream_probe = _send_live_stream_probe
    _INSTALLED = True
