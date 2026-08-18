"""Secret-safe BcMedia audio telemetry and optional live audio forwarding.

This observer independently follows the already decoded BcMedia byte stream.
It records only codec/size/header metadata for diagnostics and can optionally
forward complete in-memory audio payloads to an active live consumer. Raw audio
is never persisted, logged or included in diagnostics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from .live_stream_compat import (
    _AUDIO_MAGICS,
    _INFO_MAGICS,
    _MAX_ADDITIONAL_HEADER_BYTES,
    _MAX_MEDIA_PACKET_BYTES,
    _VIDEO_MAGICS,
    _media_chunk_from_frame,
    _padding8,
)
from .live_stream_probe import LIVE_START_CMD_ID, _LiveStreamConnection

AAC_MAGIC = b"05wb"
ADPCM_MAGIC = b"01wb"
_MAX_BUFFER_BYTES = 8 * 1024 * 1024
_INSTALLED = False

AudioSink = Callable[[bytes, str], None]


@dataclass(slots=True)
class AudioPayloadTelemetry:
    packets_observed: int = 0
    payload_bytes_observed: int = 0
    aac_packets: int = 0
    aac_payload_bytes: int = 0
    aac_adts_sync_packets: int = 0
    aac_without_adts_sync_packets: int = 0
    aac_ff_f1_packets: int = 0
    aac_ff_f9_packets: int = 0
    aac_ff_f0_packets: int = 0
    aac_ff_f8_packets: int = 0
    aac_other_sync_packets: int = 0
    adpcm_packets: int = 0
    adpcm_payload_bytes: int = 0
    adpcm_subheader_valid_packets: int = 0
    first_codec: str | None = None
    first_aac_header_class: str | None = None
    min_payload_bytes: int | None = None
    max_payload_bytes: int = 0
    raw_values_exposed: bool = False


_LAST = AudioPayloadTelemetry()


def _reset() -> None:
    global _LAST
    _LAST = AudioPayloadTelemetry()


def snapshot_audio_payload_telemetry() -> dict[str, Any]:
    return asdict(_LAST)


def _record_payload_size(size: int) -> None:
    _LAST.payload_bytes_observed += size
    _LAST.min_payload_bytes = (
        size if _LAST.min_payload_bytes is None else min(_LAST.min_payload_bytes, size)
    )
    _LAST.max_payload_bytes = max(_LAST.max_payload_bytes, size)


def _record_aac_header(payload: bytes) -> None:
    """Classify only the public ADTS sync/header variant, never raw audio."""
    if len(payload) < 2 or payload[0] != 0xFF or (payload[1] & 0xF0) != 0xF0:
        _LAST.aac_without_adts_sync_packets += 1
        if _LAST.first_aac_header_class is None:
            _LAST.first_aac_header_class = "no_adts_sync"
        return

    _LAST.aac_adts_sync_packets += 1
    second = payload[1]
    if second == 0xF1:
        name = "ff_f1"
        _LAST.aac_ff_f1_packets += 1
    elif second == 0xF9:
        name = "ff_f9"
        _LAST.aac_ff_f9_packets += 1
    elif second == 0xF0:
        name = "ff_f0"
        _LAST.aac_ff_f0_packets += 1
    elif second == 0xF8:
        name = "ff_f8"
        _LAST.aac_ff_f8_packets += 1
    else:
        name = "other_adts_sync"
        _LAST.aac_other_sync_packets += 1

    if _LAST.first_aac_header_class is None:
        _LAST.first_aac_header_class = name


def _consume_bcmedia(
    buffer: bytearray,
    audio_sink: AudioSink | None = None,
) -> None:
    """Consume complete BcMedia packets and optionally forward live audio."""
    all_magics = _INFO_MAGICS + _VIDEO_MAGICS + _AUDIO_MAGICS

    while buffer:
        offsets = [offset for magic in all_magics if (offset := buffer.find(magic)) >= 0]
        if not offsets:
            if len(buffer) > 3:
                del buffer[:-3]
            return

        offset = min(offsets)
        if offset:
            del buffer[:offset]
        if len(buffer) < 4:
            return

        magic = bytes(buffer[:4])
        if magic in _INFO_MAGICS:
            if len(buffer) < 32:
                return
            if int.from_bytes(buffer[4:8], "little") != 32:
                del buffer[0]
                continue
            del buffer[:32]
            continue

        if magic in _VIDEO_MAGICS:
            if len(buffer) < 24:
                return
            codec = bytes(buffer[4:8])
            if codec not in (b"H264", b"H265"):
                del buffer[0]
                continue
            payload_size = int.from_bytes(buffer[8:12], "little")
            additional_header_size = int.from_bytes(buffer[12:16], "little")
            if (
                payload_size <= 0
                or payload_size > _MAX_MEDIA_PACKET_BYTES
                or additional_header_size > _MAX_ADDITIONAL_HEADER_BYTES
            ):
                del buffer[0]
                continue
            packet_size = 24 + additional_header_size + payload_size + _padding8(payload_size)
            if len(buffer) < packet_size:
                return
            del buffer[:packet_size]
            continue

        if magic in _AUDIO_MAGICS:
            if len(buffer) < 8:
                return
            payload_size = int.from_bytes(buffer[4:6], "little")
            duplicate_size = int.from_bytes(buffer[6:8], "little")
            if (
                payload_size <= 0
                or payload_size != duplicate_size
                or payload_size > _MAX_MEDIA_PACKET_BYTES
            ):
                del buffer[0]
                continue
            packet_size = 8 + payload_size + _padding8(payload_size)
            if len(buffer) < packet_size:
                return

            payload = bytes(buffer[8 : 8 + payload_size])
            _LAST.packets_observed += 1
            _record_payload_size(payload_size)

            if magic == AAC_MAGIC:
                codec_name = "aac"
                if _LAST.first_codec is None:
                    _LAST.first_codec = codec_name
                _LAST.aac_packets += 1
                _LAST.aac_payload_bytes += payload_size
                _record_aac_header(payload)
            else:
                codec_name = "adpcm"
                if _LAST.first_codec is None:
                    _LAST.first_codec = codec_name
                _LAST.adpcm_packets += 1
                _LAST.adpcm_payload_bytes += payload_size
                if len(payload) >= 2 and int.from_bytes(payload[:2], "little") == 0x0100:
                    _LAST.adpcm_subheader_valid_packets += 1

            if audio_sink is not None:
                audio_sink(payload, codec_name)

            del buffer[:packet_size]
            continue

        del buffer[0]


def install_audio_payload_telemetry() -> None:
    """Install audio observation/forwarding exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_prepare_live = _LiveStreamConnection.prepare_live_probe
    original_observe_live_frame = _LiveStreamConnection._observe_live_frame

    def _prepare_live_probe(self, *args: Any, **kwargs: Any):
        _reset()
        self._poc_audio_telemetry_buffer = bytearray()
        return original_prepare_live(self, *args, **kwargs)

    def _observe_live_frame(self, frame) -> None:
        if frame.cmd_id == LIVE_START_CMD_ID and frame.body:
            chunk = _media_chunk_from_frame(self, frame)
            if chunk:
                rolling = getattr(self, "_poc_audio_telemetry_buffer", None)
                if not isinstance(rolling, bytearray):
                    rolling = bytearray()
                    self._poc_audio_telemetry_buffer = rolling
                rolling.extend(chunk)
                if len(rolling) > _MAX_BUFFER_BYTES:
                    del rolling[: len(rolling) - _MAX_BUFFER_BYTES]
                sink = getattr(self, "_poc_audio_sink", None)
                _consume_bcmedia(
                    rolling,
                    sink if callable(sink) else None,
                )

        original_observe_live_frame(self, frame)

    _LiveStreamConnection.prepare_live_probe = _prepare_live_probe
    _LiveStreamConnection._observe_live_frame = _observe_live_frame
    _INSTALLED = True
