"""PoC-only compatibility for the proven Argus live-view transport.

This module keeps the experimental RTSP PoC isolated from the production
``reolink_battery`` integration while aligning protocol details with the
physically observed Argus behavior and the documented Baichuan/BcMedia framing.

Applied compatibility behavior:
- start the P2P heartbeat immediately after UID/LAN socket handoff;
- reuse the handoff transaction id during Baichuan authentication;
- switch to fresh heartbeat transaction ids only after login succeeds;
- parse BcMedia as one rolling byte stream across cmd3 message boundaries;
- count complete Info/I/P/audio packets from their declared lengths;
- optionally forward complete H264 payloads to an in-memory consumer;
- allocate a separate Baichuan message number for cmd4 Preview stop;
- during this diagnostic PoC, accept the active cmd4 response regardless of the
  echoed msgNum so we can distinguish a header-match issue from no response.

Nothing in this module imports or patches the production ``reolink_battery``
package. Raw media is never persisted or exposed in diagnostics.
"""

from __future__ import annotations

import secrets
from typing import Any, Callable

from . import live_stream_probe as probe
from .h264_payload_telemetry import (
    observe_h264_payload,
    reset_h264_payload_telemetry,
)
from .live_stream_probe import (
    _LiveStreamConnection,
    _LiveStreamProtocol,
    _encode_p2p_heartbeat,
)
from .transport import BoundBaichuanUdpConnection

_INSTALLED = False

_ORIGINAL_APPLY_HANDOFF = BoundBaichuanUdpConnection._apply_handoff_protocol
_ORIGINAL_CONNECT = _LiveStreamConnection.connect
_ORIGINAL_SEND_HEARTBEAT = _LiveStreamConnection._send_heartbeat
_ORIGINAL_PREPARE_LIVE = _LiveStreamConnection.prepare_live_probe
_ORIGINAL_BUILD_PREVIEW_WIRE = probe._build_preview_wire
_ORIGINAL_LIVE_FRAME = _LiveStreamProtocol._live_frame
_ORIGINAL_SEND_LIVE_STREAM_PROBE = _LiveStreamConnection.send_live_stream_probe
_ORIGINAL_CLEAR_LIVE_PROBE = _LiveStreamProtocol.clear_live_probe
_ORIGINAL_OBSERVE_LIVE_FRAME = _LiveStreamConnection._observe_live_frame

_INFO_MAGICS = (b"1001", b"1002")
_VIDEO_MAGICS = tuple(
    f"{channel}{frame_type}dc".encode("ascii")
    for channel in range(10)
    for frame_type in (0, 1)
)
_AUDIO_MAGICS = (b"05wb", b"01wb")
_ALL_MAGICS = _INFO_MAGICS + _VIDEO_MAGICS + _AUDIO_MAGICS
_MAX_MEDIA_PACKET_BYTES = 4 * 1024 * 1024
_MAX_ADDITIONAL_HEADER_BYTES = 64 * 1024
_MAX_ROLLING_BUFFER_BYTES = 8 * 1024 * 1024

H264Sink = Callable[[bytes, str], None]


def _earliest_magic_offset(data: bytearray) -> int | None:
    offsets = [offset for magic in _ALL_MAGICS if (offset := data.find(magic)) >= 0]
    return min(offsets) if offsets else None


def _padding8(payload_size: int) -> int:
    remainder = payload_size % 8
    return 0 if remainder == 0 else 8 - remainder


def _parse_rolling_bcmedia(
    buffer: bytearray,
    trace,
    h264_sink: H264Sink | None = None,
) -> int:
    """Consume complete BcMedia packets and return how many were parsed."""
    parsed = 0

    while buffer:
        offset = _earliest_magic_offset(buffer)
        if offset is None:
            if len(buffer) > 3:
                del buffer[:-3]
            break
        if offset > 0:
            del buffer[:offset]

        if len(buffer) < 4:
            break
        magic = bytes(buffer[:4])

        if magic in _INFO_MAGICS:
            if len(buffer) < 32:
                break
            header_size = int.from_bytes(buffer[4:8], "little")
            if header_size != 32:
                del buffer[0]
                continue
            del buffer[:32]
            trace.bcmedia_info_frames += 1
            trace.bcmedia_observed = True
            parsed += 1
            continue

        if magic in _VIDEO_MAGICS:
            if len(buffer) < 24:
                break
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

            packet_size = (
                24
                + additional_header_size
                + payload_size
                + _padding8(payload_size)
            )
            if packet_size > _MAX_MEDIA_PACKET_BYTES + _MAX_ADDITIONAL_HEADER_BYTES + 32:
                del buffer[0]
                continue
            if len(buffer) < packet_size:
                break

            frame_type = magic[1:2]
            if frame_type == b"0":
                frame_type_name = "iframe"
                trace.iframe_frames += 1
            elif frame_type == b"1":
                frame_type_name = "pframe"
                trace.pframe_frames += 1
            else:
                del buffer[0]
                continue

            payload_start = 24 + additional_header_size
            payload_end = payload_start + payload_size
            if codec == b"H264":
                payload = bytes(buffer[payload_start:payload_end])
                observe_h264_payload(payload, frame_type=frame_type_name)
                if h264_sink is not None:
                    h264_sink(payload, frame_type_name)

            trace.video_frames += 1
            if codec == b"H264":
                trace.h264_frames += 1
            else:
                trace.h265_frames += 1
            trace.bcmedia_observed = True
            parsed += 1
            del buffer[:packet_size]
            continue

        if magic in _AUDIO_MAGICS:
            if len(buffer) < 8:
                break
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
                break
            del buffer[:packet_size]
            trace.bcmedia_observed = True
            parsed += 1
            continue

        del buffer[0]

    return parsed


def _media_chunk_from_frame(connection, frame) -> bytes:
    """Return the single binary-media chunk represented by one cmd3 body."""
    body = frame.body
    if not body:
        return b""

    if frame.payload_offset <= 0:
        return body

    enc_extension = body[: frame.payload_offset]
    payload = body[frame.payload_offset :]
    if not payload:
        return b""

    extension = connection._try_aes(enc_extension, frame.header) or b""
    encrypt_len = probe._extension_encrypt_len(extension)
    if encrypt_len and encrypt_len > 0:
        encrypt_len = min(encrypt_len, len(payload))
        decrypted_prefix = connection._try_aes(payload[:encrypt_len], frame.header)
        if decrypted_prefix is not None:
            return decrypted_prefix + payload[encrypt_len:]

    return payload


def _scan_bcmedia_corrected(data: bytes, trace) -> bool:
    """Stateless fallback scanner kept for compatibility with base PoC helpers."""
    before = (
        trace.bcmedia_info_frames,
        trace.video_frames,
        trace.iframe_frames,
        trace.pframe_frames,
    )
    temp = bytearray(data)
    _parse_rolling_bcmedia(temp, trace)
    after = (
        trace.bcmedia_info_frames,
        trace.video_frames,
        trace.iframe_frames,
        trace.pframe_frames,
    )
    return after != before


def install_preauth_heartbeat_compat() -> None:
    """Install all PoC-only transport/framing compatibility exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    def _apply_handoff_protocol(self, protocol, lease) -> None:
        self._poc_handoff_transaction_id = lease.transaction_id
        _ORIGINAL_APPLY_HANDOFF(self, protocol, lease)

    async def _connect(self) -> None:
        await _ORIGINAL_CONNECT(self)
        if getattr(self, "_handoff_active", False):
            self.start_heartbeat()

    def _send_heartbeat(self) -> bool:
        protocol = self._protocol
        transport = self._transport
        if (
            not isinstance(protocol, _LiveStreamProtocol)
            or transport is None
            or protocol.client_id is None
            or protocol.host_id is None
        ):
            return False

        if getattr(self, "_poc_fresh_heartbeat_tid_enabled", False):
            transaction_id = secrets.randbelow(999_000) + 1_000
        else:
            transaction_id = getattr(self, "_poc_handoff_transaction_id", None)
            if not isinstance(transaction_id, int):
                transaction_id = secrets.randbelow(999_000) + 1_000

        packet = _encode_p2p_heartbeat(
            transaction_id,
            protocol.client_id,
            protocol.host_id,
        )
        transport.sendto(packet, (self._host, self._port))
        self._live_trace.heartbeat_count += 1
        return True

    def _prepare_live_probe(self, *args: Any, **kwargs: Any):
        trace = _ORIGINAL_PREPARE_LIVE(self, *args, **kwargs)
        self._poc_fresh_heartbeat_tid_enabled = True
        self._poc_media_buffer = bytearray()
        reset_h264_payload_telemetry()
        return trace

    def _observe_live_frame(self, frame) -> None:
        if frame.cmd_id != probe.LIVE_START_CMD_ID or not frame.body:
            return

        chunk = _media_chunk_from_frame(self, frame)
        if not chunk:
            self._live_trace.unknown_body_frames += 1
            return

        rolling = getattr(self, "_poc_media_buffer", None)
        if not isinstance(rolling, bytearray):
            rolling = bytearray()
            self._poc_media_buffer = rolling
        rolling.extend(chunk)
        if len(rolling) > _MAX_ROLLING_BUFFER_BYTES:
            del rolling[: len(rolling) - _MAX_ROLLING_BUFFER_BYTES]

        sink = getattr(self, "_poc_h264_sink", None)
        parsed = _parse_rolling_bcmedia(
            rolling,
            self._live_trace,
            sink if callable(sink) else None,
        )
        if parsed == 0:
            self._live_trace.unknown_body_frames += 1

    def _build_preview_wire(
        baichuan,
        *,
        cmd_id: int,
        stream,
        msg_num: int | None = None,
    ):
        if cmd_id == probe.LIVE_STOP_CMD_ID:
            msg_num = None
        return _ORIGINAL_BUILD_PREVIEW_WIRE(
            baichuan,
            cmd_id=cmd_id,
            stream=stream,
            msg_num=msg_num,
        )

    def _live_frame(self, raw: bytes):
        if len(raw) >= 16 and int.from_bytes(raw[4:8], "little") == probe.LIVE_STOP_CMD_ID:
            raw_msg_num = int.from_bytes(raw[14:16], "little")
            start_msg_num = self._live_msg_num
            self._live_msg_num = raw_msg_num
            try:
                return _ORIGINAL_LIVE_FRAME(self, raw)
            finally:
                self._live_msg_num = start_msg_num
        return _ORIGINAL_LIVE_FRAME(self, raw)

    async def _send_live_stream_probe(
        self,
        start_wire: bytes,
        stop_wire: bytes,
        *,
        msg_num: int,
        duration: float,
    ):
        protocol = self._protocol
        if isinstance(protocol, _LiveStreamProtocol) and len(stop_wire) >= 16:
            protocol._poc_stop_msg_num = int.from_bytes(stop_wire[14:16], "little")
        return await _ORIGINAL_SEND_LIVE_STREAM_PROBE(
            self,
            start_wire,
            stop_wire,
            msg_num=msg_num,
            duration=duration,
        )

    def _clear_live_probe(self) -> None:
        try:
            _ORIGINAL_CLEAR_LIVE_PROBE(self)
        finally:
            self._poc_stop_msg_num = None

    BoundBaichuanUdpConnection._apply_handoff_protocol = _apply_handoff_protocol
    _LiveStreamConnection.connect = _connect
    _LiveStreamConnection._send_heartbeat = _send_heartbeat
    _LiveStreamConnection.prepare_live_probe = _prepare_live_probe
    _LiveStreamConnection._observe_live_frame = _observe_live_frame
    _LiveStreamConnection.send_live_stream_probe = _send_live_stream_probe
    _LiveStreamProtocol._live_frame = _live_frame
    _LiveStreamProtocol.clear_live_probe = _clear_live_probe
    probe._scan_bcmedia = _scan_bcmedia_corrected
    probe._build_preview_wire = _build_preview_wire
    _INSTALLED = True
