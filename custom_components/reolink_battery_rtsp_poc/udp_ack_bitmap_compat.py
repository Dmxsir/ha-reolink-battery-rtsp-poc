"""Neolink-compatible inclusive UDP ACK bitmap for the isolated live PoC.

reolink-aio builds the selective ACK bitmap using a half-open Python range whose
upper bound is the highest pending sequence ID. That omits the highest packet
from the bitmap. Neolink includes the highest received packet when advertising
which packets after the last contiguous sequence are already present.

This compatibility patch is deliberately scoped to the PoC protocol class. It
does not modify reolink-aio globally and does not persist or expose packet data.
"""

from __future__ import annotations

from .live_stream_probe import _LiveStreamProtocol

_INSTALLED = False
_ORIGINAL_SEND_ACK = _LiveStreamProtocol.send_ack
_MAGIC_UDP_ACK = bytes.fromhex("20cf872a")


def install_udp_ack_bitmap_compat() -> None:
    """Install an inclusive selective-ACK bitmap exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    def _send_ack(self) -> None:
        transport = getattr(self, "_transport", None)
        host_id = getattr(self, "host_id", None)
        recv_seq_id = int(getattr(self, "_recv_seq_id", -1))

        # Preserve the library's existing fallback/error handling whenever the
        # protocol is not in a state where an ACK can be constructed safely.
        if transport is None or host_id is None:
            _ORIGINAL_SEND_ACK(self)
            return
        if recv_seq_id < 0:
            return

        pending = getattr(self, "_seq_data", {})
        payload = b""
        if pending:
            highest = max(pending)
            if highest > recv_seq_id:
                # Inclusive upper bound is the critical difference. Neolink
                # advertises every sequence from recv_seq+1 through the highest
                # packet already present in its receive buffer.
                payload = bytes(
                    1 if seq_id in pending else 0
                    for seq_id in range(recv_seq_id + 1, highest + 1)
                )

        host_id_bytes = int(host_id).to_bytes(4, byteorder="little")
        seq_id_bytes = recv_seq_id.to_bytes(4, byteorder="little")
        payload_len_bytes = len(payload).to_bytes(4, byteorder="little")
        udp_header = (
            _MAGIC_UDP_ACK
            + host_id_bytes
            + bytes.fromhex("0000000000000000")
            + seq_id_bytes
            + bytes.fromhex("00000000")
            + payload_len_bytes
        )
        transport.sendto(
            udp_header + payload,
            (self._host, self.remote_port),
        )

    _LiveStreamProtocol.send_ack = _send_ack
    _INSTALLED = True
