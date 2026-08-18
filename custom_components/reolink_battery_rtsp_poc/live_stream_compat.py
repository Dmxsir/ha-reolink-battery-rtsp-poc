"""PoC-only compatibility for the proven Argus live-view transport.

This module keeps the experimental RTSP PoC isolated from the production
``reolink_battery`` integration while aligning a few protocol details with the
physically proven Argus behavior and the documented Baichuan/BcMedia framing.

Applied compatibility behavior:
- start the P2P heartbeat immediately after UID/LAN socket handoff;
- reuse the handoff transaction id during Baichuan authentication;
- switch to fresh heartbeat transaction ids only after login succeeds;
- classify BcMedia I/P frame magics as channel-digit + frame-type + ``dc``;
- allocate a fresh Baichuan message number for cmd4 Preview stop;
- match the cmd4 response against that separate stop message number.

Nothing in this module imports or patches the production ``reolink_battery``
package.
"""

from __future__ import annotations

import secrets
from typing import Any

from . import live_stream_probe as probe
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


def _scan_bcmedia_corrected(data: bytes, trace) -> bool:
    """Count documented BcMedia frame magics without exposing media bytes.

    The little-endian BcMedia magic range encodes the channel as the first ASCII
    digit and the frame type as the second: channel 0 I-frame = ``00dc`` and
    channel 0 P-frame = ``01dc``. The original PoC accidentally treated the two
    digits as a decimal number, which detected channel-0 I-frames but missed its
    P-frames.
    """
    found = False

    for marker in (b"1001", b"1002"):
        count = data.count(marker)
        if count:
            trace.bcmedia_info_frames += count
            found = True

    for channel in range(10):
        for frame_type, counter_name in ((0, "iframe_frames"), (1, "pframe_frames")):
            prefix = f"{channel}{frame_type}dc".encode("ascii")
            for codec in (b"H264", b"H265"):
                count = data.count(prefix + codec)
                if not count:
                    continue
                setattr(
                    trace,
                    counter_name,
                    int(getattr(trace, counter_name, 0)) + count,
                )
                trace.video_frames += count
                if codec == b"H264":
                    trace.h264_frames += count
                else:
                    trace.h265_frames += count
                found = True

    if found:
        trace.bcmedia_observed = True
    return found


def install_preauth_heartbeat_compat() -> None:
    """Install all PoC-only transport/framing compatibility exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    def _apply_handoff_protocol(self, protocol, lease) -> None:
        # Preserve the UID discovery transaction identity before the lease object
        # is released. The Argus expects this identity to remain stable through
        # the authentication phase.
        self._poc_handoff_transaction_id = lease.transaction_id
        _ORIGINAL_APPLY_HANDOFF(self, protocol, lease)

    async def _connect(self) -> None:
        await _ORIGINAL_CONNECT(self)
        if getattr(self, "_handoff_active", False):
            # Start immediately after handoff, before host.baichuan.login().
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
        # This method is called only after Baichuan login succeeds. From this
        # point onward match the proven production behavior and use fresh TIDs.
        trace = _ORIGINAL_PREPARE_LIVE(self, *args, **kwargs)
        self._poc_fresh_heartbeat_tid_enabled = True
        return trace

    def _build_preview_wire(
        baichuan,
        *,
        cmd_id: int,
        stream,
        msg_num: int | None = None,
    ):
        # cmd4 is a separate Baichuan transaction. Even though the original PoC
        # passed cmd3's number here, allocate the next message number for Stop.
        if cmd_id == probe.LIVE_STOP_CMD_ID:
            msg_num = None
        return _ORIGINAL_BUILD_PREVIEW_WIRE(
            baichuan,
            cmd_id=cmd_id,
            stream=stream,
            msg_num=msg_num,
        )

    def _live_frame(self, raw: bytes):
        # The stock PoC parser has one expected msgNum for the whole probe. Keep
        # that for cmd3, but temporarily match cmd4 against its own request number.
        if len(raw) >= 16 and int.from_bytes(raw[4:8], "little") == probe.LIVE_STOP_CMD_ID:
            stop_msg_num = getattr(self, "_poc_stop_msg_num", None)
            if isinstance(stop_msg_num, int):
                start_msg_num = self._live_msg_num
                self._live_msg_num = stop_msg_num
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
    _LiveStreamConnection.send_live_stream_probe = _send_live_stream_probe
    _LiveStreamProtocol._live_frame = _live_frame
    _LiveStreamProtocol.clear_live_probe = _clear_live_probe
    probe._scan_bcmedia = _scan_bcmedia_corrected
    probe._build_preview_wire = _build_preview_wire
    _INSTALLED = True
