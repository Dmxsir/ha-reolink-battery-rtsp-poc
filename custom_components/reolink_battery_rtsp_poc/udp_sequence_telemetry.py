"""Secret-safe UDP sequencing telemetry for the Argus live-view PoC.

This module observes reolink-aio's existing Baichuan UDP sequencing/ACK logic.
It does not alter packet ordering, retransmission requests, ACK contents or media.
Only counters/timing metadata are exported; raw sequence IDs are never exposed.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from .live_stream_probe import _LiveStreamConnection, _LiveStreamProtocol

_INSTALLED = False


@dataclass(slots=True)
class UdpSequenceTelemetry:
    bc_packets_received: int = 0
    in_order_packets: int = 0
    out_of_order_events: int = 0
    duplicate_or_old_packets: int = 0
    ack_sent_count: int = 0
    max_gap_packets: int = 0
    max_pending_packets: int = 0
    final_pending_packets: int = 0
    gap_recovery_events: int = 0
    first_gap_offset_seconds: float | None = None
    last_bc_packet_offset_seconds: float | None = None
    last_ack_offset_seconds: float | None = None
    longest_gap_seconds: float = 0.0
    unresolved_gap_seconds: float | None = None
    final_gap_open: bool = False
    raw_sequence_ids_exposed: bool = False


_LAST = UdpSequenceTelemetry()
_STARTED_AT: float | None = None
_GAP_STARTED_AT: float | None = None


def _reset() -> None:
    global _LAST, _STARTED_AT, _GAP_STARTED_AT
    _LAST = UdpSequenceTelemetry()
    _STARTED_AT = time.monotonic()
    _GAP_STARTED_AT = None


def _offset() -> float | None:
    if _STARTED_AT is None:
        return None
    return round(max(0.0, time.monotonic() - _STARTED_AT), 3)


def _update_gap_state(protocol: _LiveStreamProtocol) -> None:
    global _GAP_STARTED_AT

    pending = len(getattr(protocol, "_seq_data", {}))
    _LAST.final_pending_packets = pending
    _LAST.max_pending_packets = max(_LAST.max_pending_packets, pending)

    now = time.monotonic()
    if pending > 0:
        if _GAP_STARTED_AT is None:
            _GAP_STARTED_AT = now
            if _LAST.first_gap_offset_seconds is None:
                _LAST.first_gap_offset_seconds = _offset()
        _LAST.final_gap_open = True
        return

    if _GAP_STARTED_AT is not None:
        duration = max(0.0, now - _GAP_STARTED_AT)
        _LAST.longest_gap_seconds = max(_LAST.longest_gap_seconds, duration)
        _LAST.gap_recovery_events += 1
        _GAP_STARTED_AT = None
    _LAST.final_gap_open = False


def snapshot_udp_sequence_telemetry() -> dict[str, Any]:
    """Return the current secret-safe UDP sequencing snapshot."""
    result = asdict(_LAST)
    if _GAP_STARTED_AT is not None:
        unresolved = max(0.0, time.monotonic() - _GAP_STARTED_AT)
        result["unresolved_gap_seconds"] = round(unresolved, 3)
        result["longest_gap_seconds"] = round(
            max(float(result["longest_gap_seconds"]), unresolved), 3
        )
        result["final_gap_open"] = True
    else:
        result["longest_gap_seconds"] = round(
            float(result["longest_gap_seconds"]), 3
        )
    return result


def install_udp_sequence_telemetry() -> None:
    """Install observation-only wrappers exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_prepare_live = _LiveStreamConnection.prepare_live_probe
    original_parse_udp_bc = _LiveStreamProtocol.parse_udp_bc
    original_send_ack = _LiveStreamProtocol.send_ack

    def _prepare_live_probe(self, *args: Any, **kwargs: Any):
        _reset()
        return original_prepare_live(self, *args, **kwargs)

    def _parse_udp_bc(self, port: int) -> None:
        data = getattr(self, "_udp_data", b"")
        if len(data) >= 20:
            seq_id = int.from_bytes(data[12:16], "little")
            expected = int(getattr(self, "_recv_seq_id", -1)) + 1
            _LAST.bc_packets_received += 1
            _LAST.last_bc_packet_offset_seconds = _offset()
            if seq_id == expected:
                _LAST.in_order_packets += 1
            elif seq_id > expected:
                _LAST.out_of_order_events += 1
                _LAST.max_gap_packets = max(
                    _LAST.max_gap_packets, seq_id - expected
                )
            else:
                _LAST.duplicate_or_old_packets += 1

        before_pending = len(getattr(self, "_seq_data", {}))
        original_parse_udp_bc(self, port)
        after_pending = len(getattr(self, "_seq_data", {}))
        if before_pending > 0 and after_pending == 0:
            # _update_gap_state accounts for the recovered gap duration.
            pass
        _update_gap_state(self)

    def _send_ack(self) -> None:
        original_send_ack(self)
        if int(getattr(self, "_recv_seq_id", -1)) >= 0:
            _LAST.ack_sent_count += 1
            _LAST.last_ack_offset_seconds = _offset()
        _update_gap_state(self)

    _LiveStreamConnection.prepare_live_probe = _prepare_live_probe
    _LiveStreamProtocol.parse_udp_bc = _parse_udp_bc
    _LiveStreamProtocol.send_ack = _send_ack
    _INSTALLED = True
