"""On-demand H264/AAC source built on the proven Argus Baichuan transport."""

from __future__ import annotations

import asyncio
import ipaddress
from contextlib import suppress
from typing import Callable

from reolink_aio.api import Host
from reolink_aio.enums import ConnectionEnum
from reolink_aio.exceptions import ReolinkError

from . import live_stream_probe as probe
from .live_stream_probe import (
    ACCEPTED_RESPONSE_CODES,
    LIVE_START_CMD_ID,
    LIVE_STOP_CMD_ID,
    START_TIMEOUT_SECONDS,
    STOP_TIMEOUT_SECONDS,
    LiveStreamProbeError,
    LiveStreamTrace,
    _LiveStreamConnection,
    _LiveStreamProtocol,
    _prepare_standalone_channel_zero,
)
from .transport import (
    UID_RESOLVE_TIMEOUT_SECONDS,
    UidResolveTrace,
    linux_ipv4_interface,
    resolve_uid_lan,
    validate_local_lan_route,
)
from .udp_media_keepalive import _keepalive_loop, _send_once, _stop_keepalive

H264FrameSink = Callable[[bytes, str], None]
AudioFrameSink = Callable[[bytes, str], None]


async def async_stream_h264(
    uid: str,
    username: str,
    password: str,
    interface: ipaddress.IPv4Interface,
    *,
    frame_sink: H264FrameSink,
    stop_event: asyncio.Event,
    audio_sink: AudioFrameSink | None = None,
    resolve_timeout: float = UID_RESOLVE_TIMEOUT_SECONDS,
    command_timeout: int = 30,
) -> LiveStreamTrace:
    """Stream main H264 and optional audio until consumers disconnect.

    Raw media exists only in memory long enough to be forwarded to the supplied
    sinks. Nothing is written to disk or diagnostics by this function.
    """
    lease = None
    host = None
    connection: _LiveStreamConnection | None = None
    trace = LiveStreamTrace(attempted=True, stream_kind="main")
    uid_trace = UidResolveTrace(timeout_seconds=float(resolve_timeout))
    failure_stage = "UID_RESOLVE_ERROR"

    try:
        interface_name, _ = await asyncio.to_thread(
            linux_ipv4_interface,
            str(interface.ip),
        )
        lease = await asyncio.to_thread(
            resolve_uid_lan,
            uid,
            interface,
            resolve_timeout,
            uid_trace,
        )
        await asyncio.to_thread(
            validate_local_lan_route,
            interface,
            lease.host,
            interface_name,
        )

        host = Host(
            host=lease.host,
            username=username,
            password=password,
            bc_only=True,
            bc_connection=ConnectionEnum.udp,
            uid=uid,
            timeout=command_timeout,
        )
        _prepare_standalone_channel_zero(host)
        host._uid[0] = uid

        connection = _LiveStreamConnection(
            lease.host,
            lease.source_ip,
            0,
            host.baichuan._push_callback,
            host.baichuan._close_callback,
            uid=uid,
            handoff_lease=lease,
        )
        host.baichuan._connection = connection

        failure_stage = "LIVE_WAKE_ERROR"
        await connection.connect()
        if lease.socket is not None:
            raise RuntimeError("single lease handoff was not adopted")
        lease = None

        failure_stage = "LIVE_AUTH_ERROR"
        host.baichuan._first_login = False
        await host.baichuan.login()

        trace = connection.prepare_live_probe(
            host.baichuan._aes_decrypt,
            stream_kind="main",
        )
        connection._poc_h264_sink = frame_sink
        connection._poc_audio_sink = audio_sink
        connection.start_heartbeat()

        start_wire, start_request = probe._build_preview_wire(
            host.baichuan,
            cmd_id=LIVE_START_CMD_ID,
            stream="main",
        )
        stop_wire, _ = probe._build_preview_wire(
            host.baichuan,
            cmd_id=LIVE_STOP_CMD_ID,
            stream="main",
            msg_num=start_request.msg_num,
        )

        protocol = connection._protocol
        if not isinstance(protocol, _LiveStreamProtocol):
            raise RuntimeError("unexpected live-stream UDP protocol")

        trace.start_attempted = True
        start_future, stop_future = protocol.arm_live_probe(
            start_request.msg_num,
            trace,
            connection._observe_live_frame,
        )

        await _send_once(connection)
        connection._poc_udp_media_keepalive_task = connection._loop.create_task(
            _keepalive_loop(connection)
        )

        failure_stage = "LIVE_STREAM_START_ERROR"
        await connection.send_without_wait(
            start_wire,
            cmd_id=LIVE_START_CMD_ID,
            timeout=5,
        )
        start_frame = await asyncio.wait_for(
            asyncio.shield(start_future),
            timeout=START_TIMEOUT_SECONDS,
        )
        trace.start_response_code = start_frame.response_code
        trace.start_accepted = start_frame.response_code in ACCEPTED_RESPONSE_CODES
        if not trace.start_accepted:
            raise LiveStreamProbeError(
                "LIVE_STREAM_REJECTED",
                response_code=trace.start_response_code,
                trace=trace,
                uid_resolve_trace=uid_trace,
            )

        while connection.connection_open and not stop_event.is_set():
            await asyncio.sleep(0.05)

        if connection.connection_open:
            trace.stop_attempted = True
            with suppress(TimeoutError, asyncio.TimeoutError, ReolinkError, OSError):
                await connection.send_without_wait(
                    stop_wire,
                    cmd_id=LIVE_STOP_CMD_ID,
                    timeout=2,
                )
                stop_frame = await asyncio.wait_for(
                    asyncio.shield(stop_future),
                    timeout=STOP_TIMEOUT_SECONDS,
                )
                trace.stop_response_code = stop_frame.response_code
                trace.stop_accepted = (
                    stop_frame.response_code in ACCEPTED_RESPONSE_CODES
                )

        trace.termination_reason = (
            "consumer_disconnected" if stop_event.is_set() else "connection_closed"
        )
        return trace

    except LiveStreamProbeError:
        raise
    except (
        ReolinkError,
        OSError,
        TimeoutError,
        asyncio.TimeoutError,
        RuntimeError,
        ValueError,
    ) as err:
        rsp_code = getattr(err, "rspCode", None)
        raise LiveStreamProbeError(
            failure_stage,
            failure_type=type(err).__name__,
            response_code=rsp_code if isinstance(rsp_code, int) else None,
            trace=trace,
            uid_resolve_trace=uid_trace,
        ) from None
    finally:
        stop_event.set()
        if connection is not None:
            connection._poc_h264_sink = None
            connection._poc_audio_sink = None
            with suppress(Exception):
                await _stop_keepalive(connection)
            protocol = connection._protocol
            if isinstance(protocol, _LiveStreamProtocol):
                protocol.clear_live_probe()
            connection._live_decryptor = None

        try:
            if host is not None:
                with suppress(ReolinkError, OSError, TimeoutError, asyncio.TimeoutError):
                    await host.logout()
        finally:
            try:
                if connection is not None and connection.connection_open:
                    with suppress(ReolinkError, OSError, TimeoutError, asyncio.TimeoutError):
                        await connection.close()
            finally:
                if lease is not None:
                    lease.close()
                password = ""
