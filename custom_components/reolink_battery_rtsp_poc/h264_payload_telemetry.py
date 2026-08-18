"""Secret-safe H264 payload shape telemetry for the live-view PoC.

Only counters and NAL-unit types are retained. Raw H264 bytes are never stored,
logged, uploaded or exposed in diagnostics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class H264PayloadTelemetry:
    frames_observed: int = 0
    iframe_payloads: int = 0
    pframe_payloads: int = 0
    payload_bytes_observed: int = 0
    annexb_prefixed_frames: int = 0
    non_annexb_prefixed_frames: int = 0
    three_byte_start_codes: int = 0
    four_byte_start_codes: int = 0
    nal_units_observed: int = 0
    nal_type_counts: dict[str, int] = field(default_factory=dict)
    raw_values_exposed: bool = False


_LAST = H264PayloadTelemetry()

_NAL_NAMES = {
    1: "non_idr_slice",
    5: "idr_slice",
    6: "sei",
    7: "sps",
    8: "pps",
    9: "aud",
}


def reset_h264_payload_telemetry() -> None:
    global _LAST
    _LAST = H264PayloadTelemetry()


def _start_codes(payload: bytes) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    i = 0
    end = len(payload)
    while i + 3 <= end:
        if i + 4 <= end and payload[i : i + 4] == b"\x00\x00\x00\x01":
            result.append((i, 4))
            i += 4
            continue
        if payload[i : i + 3] == b"\x00\x00\x01":
            result.append((i, 3))
            i += 3
            continue
        i += 1
    return result


def observe_h264_payload(payload: bytes, *, frame_type: str) -> None:
    """Observe one complete H264 frame without retaining its bytes."""
    if not payload:
        return

    _LAST.frames_observed += 1
    _LAST.payload_bytes_observed += len(payload)
    if frame_type == "iframe":
        _LAST.iframe_payloads += 1
    elif frame_type == "pframe":
        _LAST.pframe_payloads += 1

    prefixed = payload.startswith(b"\x00\x00\x00\x01") or payload.startswith(
        b"\x00\x00\x01"
    )
    if prefixed:
        _LAST.annexb_prefixed_frames += 1
    else:
        _LAST.non_annexb_prefixed_frames += 1

    starts = _start_codes(payload)
    for _offset, width in starts:
        if width == 4:
            _LAST.four_byte_start_codes += 1
        else:
            _LAST.three_byte_start_codes += 1

    for index, (offset, width) in enumerate(starts):
        nal_offset = offset + width
        next_offset = starts[index + 1][0] if index + 1 < len(starts) else len(payload)
        if nal_offset >= next_offset:
            continue
        nal_type = payload[nal_offset] & 0x1F
        name = _NAL_NAMES.get(nal_type, f"type_{nal_type}")
        _LAST.nal_type_counts[name] = _LAST.nal_type_counts.get(name, 0) + 1
        _LAST.nal_units_observed += 1


def snapshot_h264_payload_telemetry() -> dict[str, Any]:
    return asdict(_LAST)
