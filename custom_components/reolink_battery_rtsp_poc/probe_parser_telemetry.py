"""Secret-safe parser telemetry for the bounded Argus live-view PoC.

This module observes the already working PoC parser without changing the media
bytes sent to or received from the camera. It records only sizes, counters and
packet types. No raw image/audio payload is persisted or exported.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import live_stream_probe as probe
from .live_stream_compat import _media_chunk_from_frame
from .live_stream_probe import _LiveStreamConnection

_INSTALLED = False


@dataclass(slots=True)
class ParserTelemetry:
    media_bytes_seen: int = 0
    rolling_buffer_bytes: int = 0
    rolling_buffer_peak_bytes: int = 0
    info_header_hits: int = 0
    iframe_header_hits: int = 0
    pframe_header_hits: int = 0
    audio_header_hits: int = 0
    first_video_codec: str | None = None
    first_video_frame_type: str | None = None
    first_video_declared_payload_bytes: int | None = None
    first_video_additional_header_bytes: int | None = None
    pending_packet_type: str | None = None
    pending_codec: str | None = None
    pending_declared_payload_bytes: int | None = None
    pending_additional_header_bytes: int | None = None
    pending_total_bytes: int | None = None
    pending_available_bytes: int = 0
    cmd3_frames_at_stop_send: int | None = None
    cmd3_frames_after_stop_send: int | None = None
    stop_quiet_observed: bool | None = None
    raw_values_exposed: bool = False


_LAST = ParserTelemetry()

_INFO_MAGICS = (b"1001", b"1002")
_AUDIO_MAGICS = (b"05wb", b"01wb")
_VIDEO_MAGICS = tuple(
    (f"{channel}{frame_type}dc".encode("ascii"), frame_type)
    for channel in range(10)
    for frame_type in (0, 1)
)
_MARKERS_WITH_CODEC = tuple(
    (magic + codec, frame_type, codec.decode("ascii"))
    for magic, frame_type in _VIDEO_MAGICS
    for codec in (b"H264", b"H265")
)
_HEADER_OVERLAP_BYTES = 23
_MAGIC_OVERLAP_BYTES = 7


def _reset() -> None:
    global _LAST
    _LAST = ParserTelemetry()


def snapshot_parser_telemetry() -> dict[str, Any]:
    """Return a detached secret-safe snapshot of the latest manual probe."""
    return asdict(_LAST)


def _count_markers(connection: _LiveStreamConnection, chunk: bytes) -> None:
    """Count header-like signatures once across cmd3 body boundaries."""
    overlap = getattr(connection, "_poc_telemetry_magic_overlap", b"")
    scan = overlap + chunk

    _LAST.info_header_hits += sum(scan.count(magic) for magic in _INFO_MAGICS)
    _LAST.audio_header_hits += sum(scan.count(magic) for magic in _AUDIO_MAGICS)
    for marker, frame_type, _codec in _MARKERS_WITH_CODEC:
        count = scan.count(marker)
        if frame_type == 0:
            _LAST.iframe_header_hits += count
        else:
            _LAST.pframe_header_hits += count

    connection._poc_telemetry_magic_overlap = scan[-_MAGIC_OVERLAP_BYTES:]


def _capture_first_video_header(connection: _LiveStreamConnection, chunk: bytes) -> None:
    tail = getattr(connection, "_poc_telemetry_header_tail", b"")
    scan = tail + chunk

    if _LAST.first_video_declared_payload_bytes is None:
        earliest: tuple[int, int, str] | None = None
        for marker, frame_type, codec in _MARKERS_WITH_CODEC:
            offset = scan.find(marker)
            if offset < 0:
                continue
            candidate = (offset, frame_type, codec)
            if earliest is None or candidate[0] < earliest[0]:
                earliest = candidate

        if earliest is not None:
            offset, frame_type, codec = earliest
            if len(scan) >= offset + 24:
                _LAST.first_video_frame_type = "iframe" if frame_type == 0 else "pframe"
                _LAST.first_video_codec = codec
                _LAST.first_video_declared_payload_bytes = int.from_bytes(
                    scan[offset + 8 : offset + 12], "little"
                )
                _LAST.first_video_additional_header_bytes = int.from_bytes(
                    scan[offset + 12 : offset + 16], "little"
                )

    connection._poc_telemetry_header_tail = scan[-_HEADER_OVERLAP_BYTES:]


def _update_pending(connection: _LiveStreamConnection) -> None:
    rolling = getattr(connection, "_poc_media_buffer", None)
    if not isinstance(rolling, bytearray):
        return

    _LAST.rolling_buffer_bytes = len(rolling)
    _LAST.rolling_buffer_peak_bytes = max(
        _LAST.rolling_buffer_peak_bytes, len(rolling)
    )
    _LAST.pending_available_bytes = len(rolling)
    _LAST.pending_packet_type = None
    _LAST.pending_codec = None
    _LAST.pending_declared_payload_bytes = None
    _LAST.pending_additional_header_bytes = None
    _LAST.pending_total_bytes = None

    if len(rolling) < 4:
        return

    magic = bytes(rolling[:4])
    if magic in _INFO_MAGICS:
        _LAST.pending_packet_type = "info"
        _LAST.pending_total_bytes = 32
        return

    for video_magic, frame_type in _VIDEO_MAGICS:
        if magic != video_magic:
            continue
        _LAST.pending_packet_type = "iframe" if frame_type == 0 else "pframe"
        if len(rolling) < 24:
            return
        codec_bytes = bytes(rolling[4:8])
        if codec_bytes in (b"H264", b"H265"):
            _LAST.pending_codec = codec_bytes.decode("ascii")
        payload_size = int.from_bytes(rolling[8:12], "little")
        additional_header_size = int.from_bytes(rolling[12:16], "little")
        _LAST.pending_declared_payload_bytes = payload_size
        _LAST.pending_additional_header_bytes = additional_header_size
        padding = 0 if payload_size % 8 == 0 else 8 - (payload_size % 8)
        _LAST.pending_total_bytes = (
            24 + additional_header_size + payload_size + padding
        )
        return

    if magic in _AUDIO_MAGICS:
        _LAST.pending_packet_type = "audio"
        if len(rolling) >= 8:
            payload_size = int.from_bytes(rolling[4:6], "little")
            _LAST.pending_declared_payload_bytes = payload_size
            padding = 0 if payload_size % 8 == 0 else 8 - (payload_size % 8)
            _LAST.pending_total_bytes = 8 + payload_size + padding
        return

    _LAST.pending_packet_type = "unsynced"


def install_parser_telemetry() -> None:
    """Install the observation-only wrappers exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    # Capture the methods at install time, after live_stream_compat has installed
    # its proven Argus compatibility wrappers. This makes telemetry purely
    # additive and prevents it from bypassing the working auth/parser behavior.
    original_prepare_live = _LiveStreamConnection.prepare_live_probe
    original_observe_live_frame = _LiveStreamConnection._observe_live_frame
    original_send_without_wait = _LiveStreamConnection.send_without_wait
    original_send_live_stream_probe = _LiveStreamConnection.send_live_stream_probe

    def _prepare_live_probe(self, *args: Any, **kwargs: Any):
        _reset()
        self._poc_telemetry_magic_overlap = b""
        self._poc_telemetry_header_tail = b""
        return original_prepare_live(self, *args, **kwargs)

    def _observe_live_frame(self, frame) -> None:
        if frame.cmd_id == probe.LIVE_START_CMD_ID and frame.body:
            chunk = _media_chunk_from_frame(self, frame)
            if chunk:
                _LAST.media_bytes_seen += len(chunk)
                _count_markers(self, chunk)
                _capture_first_video_header(self, chunk)

        original_observe_live_frame(self, frame)
        _update_pending(self)

    async def _send_without_wait(self, data: bytes, *args: Any, **kwargs: Any):
        cmd_id = kwargs.get("cmd_id")
        if cmd_id is None and args:
            cmd_id = args[0]
        if cmd_id == probe.LIVE_STOP_CMD_ID:
            _LAST.cmd3_frames_at_stop_send = int(
                getattr(self._live_trace, "cmd3_frames", 0)
            )
        return await original_send_without_wait(self, data, *args, **kwargs)

    async def _send_live_stream_probe(self, *args: Any, **kwargs: Any):
        trace = await original_send_live_stream_probe(self, *args, **kwargs)
        if _LAST.cmd3_frames_at_stop_send is not None:
            after = max(
                0,
                int(trace.cmd3_frames) - _LAST.cmd3_frames_at_stop_send,
            )
            _LAST.cmd3_frames_after_stop_send = after
            _LAST.stop_quiet_observed = after == 0
        _update_pending(self)
        return trace

    _LiveStreamConnection.prepare_live_probe = _prepare_live_probe
    _LiveStreamConnection._observe_live_frame = _observe_live_frame
    _LiveStreamConnection.send_without_wait = _send_without_wait
    _LiveStreamConnection.send_live_stream_probe = _send_live_stream_probe
    _INSTALLED = True
