"""A minimal SNTP server that answers with this machine's clock.

The emulated ESP32 has no real-time clock and, like a real board, will not open
a TLS connection until SNTP has told it the time. Under QEMU's user-mode
networking the guest reaches the host at 10.0.2.2, so run_device.sh starts this
next to QEMU and provisions 10.0.2.2 as the device's time server. That keeps the
test independent of public NTP pools while exercising the same SNTP client code
path a board uses.

Answers RFC 4330 client requests with stratum 1 and reference id LOCL. Standard
library only.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

# Seconds between the NTP epoch (1900) and the Unix epoch (1970).
NTP_DELTA = 2_208_988_800


def ntp_timestamp(unix_seconds: float) -> bytes:
    seconds = int(unix_seconds) + NTP_DELTA
    fraction = int((unix_seconds % 1) * (1 << 32)) & 0xFFFFFFFF
    return struct.pack("!II", seconds & 0xFFFFFFFF, fraction)


def response(request: bytes, received_at: float) -> bytes | None:
    if len(request) < 48:
        return None
    version = (request[0] >> 3) & 0x7
    mode = request[0] & 0x7
    if mode != 3:  # only client requests
        return None
    header = bytes([(version << 3) | 4, 1, request[2], 0xEC])  # LI 0, mode 4, stratum 1, precision 2^-20
    root_delay_dispersion = struct.pack("!II", 0, 0)
    reference_id = b"LOCL"
    reference = ntp_timestamp(received_at)
    # The client's transmit timestamp comes back as the originate timestamp,
    # which is how the client matches the reply to its request.
    originate = request[40:48]
    receive = ntp_timestamp(received_at)
    transmit = ntp_timestamp(time.time())
    return header + root_delay_dispersion + reference_id + reference + originate + receive + transmit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=123)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))
    print(f"[sntp] answering on {args.bind}:{args.port}", file=sys.stderr, flush=True)
    while True:
        data, peer = sock.recvfrom(512)
        reply = response(data, time.time())
        if reply is not None:
            sock.sendto(reply, peer)


if __name__ == "__main__":
    sys.exit(main())
