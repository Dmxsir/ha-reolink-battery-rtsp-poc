"""PoC-only Baichuan UDP media keepalive for Argus live view.

Neolink keeps UDP Baichuan sessions alive with a header-only cmd234 message
(MSG_ID_UDP_KEEP_ALIVE) sent roughly every 500 ms. This is distinct from the
UID/P2P C2D_HB heartbeat and from cmd93 used for event subscriptions.

This module applies that behavior only while the bounded manual live-view probe
is running. It does not touch the production ``reolink_battery`` integration.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import secrets
from typing import Any

from reolink_aio.exceptions import ReolinkError

from .live_stream_probe import (
    LIVE_MESSAGE_CLASS,
    _LiveStreamConnection,
)
from .transport import BAICHUAN_MAGIC

UDP_MEDIA_KEEPALIVE_CMD_ID = 234
UDP_MEDIA_KEEPALIVE_INTERVAL_SECONDS = 0.5

_INSTALLED = False

_ORIGINAL_SEND_LIVE_STREAM_PROBE = _LiveStreamConnection.send_live_stream_probe
_ORIGINAL_CLOSE = _LiveStreamConnection.close


@dataclass(slots=True)
class UdpMediaKeepaliveTelemetry:
    enabled: bool = True
    interval_seconds: float = UDP_MEDIA_KEEPALIVE_INTERVAL_SECONDS
    started: bool = False
    stopped: bool = False
    sent_count: int = 0
    error_count: int = 0
    last_error_type: str | None = None
    raw_values_exposed: bool = False


_LAST = UdpMediaKeepaliveTelemetry()


def _reset() -> None:
    global _LAST
    _LAST = UdpMediaKeepaliveTelemetry()


def snapshot_udp_media_keepalive() -> dict[str, Any]:
    """Return a detached, secret-safe telemetry snapshot."""
    return asdict(_LAST)


def _build_keepalive_wire(msg_num: int) -> bytes:
    """Build one header-only modern Baichuan cmd234 keepalive."""
    return (
        BAICHUAN_MAGIC
        + UDP_MEDIA_KEEPALIVE_CMD_ID.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + (0).to_bytes(1, "little")  # channel_id
        + (0).to_bytes(1, "little")  # stream_type
        + msg_num.to_bytes(2, "little")
        + (0).to_bytes(2, "little")  # request/response code
        + LIVE_MESSAGE_CLASS.to_bytes(2, "little")
        + (0).to_bytes(4, "little")  # payload_offset
    )


async def _send_once(connection: _LiveStreamConnection) -> None:
    if not connection.connection_open:
        return

    msg_num = getattr(connection, "_poc_udp_media_keepalive_msg_num", None)
    if not isinstance(msg_num, int):
        msg_num = secrets.randbelow(65536)
        connection._poc_udp_media_keepalive_msg_num = msg_num

    wire = _build_keepalive_wire(msg_num)
    try:
        await connection.send_without_wait(
            wire,
            cmd_id=UDP_MEDIA_KEEPALIVE_CMD_ID,
            timeout=2,
        )
    except (ReolinkError, OSError, TimeoutError, asyncio.TimeoutError) as err:
        _LAST.error_count += 1
        _LAST.last_error_type = type(err).__name__
    else:
        _LAST.sent_count += 1


async def _keepalive_loop(connection: _LiveStreamConnection) -> None:
    try:
        while connection.connection_open:
            await asyncio.sleep(UDP_MEDIA_KEEPALIVE_INTERVAL_SECONDS)
            if not connection.connection_open:
                break
            await _send_once(connection)
    except asyncio.CancelledError:
        raise


async def _stop_keepalive(connection: _LiveStreamConnection) -> None:
    task = getattr(connection, "_poc_udp_media_keepalive_task", None)
    connection._poc_udp_media_keepalive_task = None
    if task is None or task is asyncio.current_task():
        _LAST.stopped = True
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _LAST.stopped = True


def install_udp_media_keepalive() -> None:
    """Install cmd234 keepalive around the bounded PoC stream exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    async def _send_live_stream_probe(
        self,
        start_wire: bytes,
        stop_wire: bytes,
        *,
        msg_num: int,
        duration: float,
    ):
        _reset()
        self._poc_udp_media_keepalive_msg_num = secrets.randbelow(65536)
        _LAST.started = True

        # Send one cmd234 immediately before cmd3, then keep the exact same
        # Baichuan keepalive message active at the cadence used by Neolink.
        await _send_once(self)
        self._poc_udp_media_keepalive_task = self._loop.create_task(
            _keepalive_loop(self)
        )
        try:
            return await _ORIGINAL_SEND_LIVE_STREAM_PROBE(
                self,
                start_wire,
                stop_wire,
                msg_num=msg_num,
                duration=duration,
            )
        finally:
            await _stop_keepalive(self)

    async def _close(self) -> None:
        await _stop_keepalive(self)
        await _ORIGINAL_CLOSE(self)

    _LiveStreamConnection.send_live_stream_probe = _send_live_stream_probe
    _LiveStreamConnection.close = _close
    _INSTALLED = True
