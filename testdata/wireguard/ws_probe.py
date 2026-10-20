"""Minimal stdlib WebSocket round-trip, run on the wg-server peer.

Usage: ws_probe.py <host> <port>. Prints the server's reply to one "hello" frame.
"""

import base64
import os
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.create_connection((host, port), timeout=10)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall(
    (
        f"GET /api/websocket HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
)
buf = b""
while b"\r\n\r\n" not in buf:
    buf += sock.recv(4096)
head, _, rest = buf.partition(b"\r\n\r\n")
if not head.startswith(b"HTTP/1.1 101"):
    sys.exit(f"no upgrade: {head!r}")

payload = b"hello"
mask = os.urandom(4)
sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
while len(rest) < 2:
    rest += sock.recv(4096)
length = rest[1] & 0x7F
while len(rest) < 2 + length:
    rest += sock.recv(4096)
print(rest[2 : 2 + length].decode())
