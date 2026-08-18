"""PoC-only compatibility for the proven Argus UID/P2P auth lifetime.

The production Reolink Battery download path starts its P2P heartbeat immediately
after the UID/LAN socket handoff, reusing the handoff transaction id while
Baichuan authentication is in progress. Only after login succeeds does it move
to fresh heartbeat transaction ids.

The first RTSP PoC implementation started heartbeat only after login, which can
allow this Argus session to expire during authentication. This module applies
the proven lifetime behavior only to the PoC's private transport classes. It
never imports or patches the production ``reolink_battery`` package.
"""

from __future__ import annotations

import secrets
from typing import Any

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


def install_preauth_heartbeat_compat() -> None:
    """Install the proven pre-auth heartbeat behavior once for this PoC."""
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

    BoundBaichuanUdpConnection._apply_handoff_protocol = _apply_handoff_protocol
    _LiveStreamConnection.connect = _connect
    _LiveStreamConnection._send_heartbeat = _send_heartbeat
    _LiveStreamConnection.prepare_live_probe = _prepare_live_probe
    _INSTALLED = True
