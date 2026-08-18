"""Preserve and parse multiple Baichuan live messages in one receive buffer.

The bounded live PoC can receive more than one complete cmd3/cmd4 Baichuan
message in a single UDP-reassembled data chunk. The original PoC observer parsed
the first live message and cleared the entire buffer, silently discarding any
following complete messages. This compatibility layer consumes exactly one
message at a time and keeps the remainder for the next parse iteration.

No raw media data is persisted or exposed.
"""

from __future__ import annotations

from . import live_stream_probe as probe
from .live_stream_probe import _LiveStreamProtocol

_INSTALLED = False
_ORIGINAL_PARSE_BC_DATA = _LiveStreamProtocol.parse_bc_data


def _header_length(raw: bytes) -> int | None:
    if len(raw) < 20:
        return None
    message_class = int.from_bytes(raw[18:20], "little")
    return 24 if message_class in (0x0000, 0x6414, 0x6482) else 20


def install_live_buffer_compat() -> None:
    """Install multi-message live buffer parsing exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    def _parse_bc_data(self) -> None:
        while True:
            raw = self._data
            frame = self._live_frame(raw)
            if frame is None:
                # Preserve the stock parser for login/control/non-live traffic and
                # for incomplete live headers/bodies that still need more bytes.
                _ORIGINAL_PARSE_BC_DATA(self)
                return

            header_length = _header_length(raw)
            if header_length is None:
                return
            consumed = header_length + frame.body_length
            if consumed > len(raw):
                return

            trace = self._live_trace
            if trace is not None and frame.cmd_id == probe.LIVE_START_CMD_ID:
                trace.cmd3_frames += 1
                trace.total_body_bytes += frame.body_length
                if frame.body:
                    trace.body_frames += 1
                if (
                    trace.first_cmd3_delay_ms is None
                    and self._live_started_at is not None
                ):
                    trace.first_cmd3_delay_ms = round(
                        max(0.0, self._loop.time() - self._live_started_at)
                        * 1000.0,
                        3,
                    )

            if self._live_observer is not None:
                self._live_observer(frame)

            future = (
                self._live_start_future
                if frame.cmd_id == probe.LIVE_START_CMD_ID
                else self._live_stop_future
            )
            if future is not None and not future.done():
                future.set_result(frame)

            # Critical difference from the original PoC: consume only the
            # message we just parsed. A reassembled UDP payload may already
            # contain one or more additional Baichuan messages.
            self._data = raw[consumed:]
            if not self._data:
                return

            # Continue immediately for another complete Baichuan message. For a
            # partial next header/body, _live_frame() will return None and the
            # stock parser will retain the partial bytes until more data arrives.

    _LiveStreamProtocol.parse_bc_data = _parse_bc_data
    _INSTALLED = True
