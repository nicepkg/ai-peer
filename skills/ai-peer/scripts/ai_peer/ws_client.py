"""Minimal WebSocket client using Python stdlib (no external deps).

Implements RFC 6455 just enough for ai-peer relay push:
- Connect with wss:// + token auth via query param
- Receive text frames (JSON messages from relay)
- No send needed (clients POST via HTTP)
"""
import hashlib
import base64
import json
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlparse

from ai_peer.constants import VERSION


class WebSocketClient:
    """Minimal WebSocket client for receiving relay push messages."""

    def __init__(self, url, on_message=None, on_error=None, on_close=None):
        self.url = url
        self.on_message = on_message or (lambda msg: None)
        self.on_error = on_error or (lambda e: None)
        self.on_close = on_close or (lambda: None)
        self._sock = None
        self._running = False
        self._thread = None
        self._frag_buf = b""
        self._frag_opcode = None

    def connect(self):
        """Connect and start receiving in a background thread."""
        parsed = urlparse(self.url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")

        # TCP + TLS
        raw = socket.create_connection((host, port), timeout=10)
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            self._sock = raw

        # WebSocket handshake
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: ai-peer/{VERSION}\r\n"
            f"\r\n"
        )
        self._sock.sendall(handshake.encode())

        # Read response headers
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed: connection closed")
            resp += chunk

        # Preserve any bytes after the HTTP headers (may contain first WS frame)
        header_end = resp.index(b"\r\n\r\n") + 4
        self._recv_buf = resp[header_end:]

        status_line = resp[:resp.index(b"\r\n")].decode()
        if "101" not in status_line:
            raise ConnectionError(f"WebSocket handshake failed: {status_line}")

        # Remove handshake timeout — recv should block indefinitely for idle chat rooms
        self._sock.settimeout(None)

        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _recv_loop(self):
        """Read WebSocket frames in a loop."""
        try:
            while self._running:
                msg = self._read_frame()
                if msg is None:
                    break
                try:
                    data = json.loads(msg)
                    self.on_message(data)
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            if self._running:
                self.on_error(e)
        finally:
            self._running = False
            self.on_close()

    _MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB fragment buffer limit

    def _read_frame(self):
        """Read one WebSocket text frame. Returns str or None on close.

        Uses iterative loop instead of recursion to prevent stack overflow
        from ping floods or thousands of tiny fragments.
        """
        while True:
            header = self._recv_exact(2)
            if not header:
                return None

            fin = (header[0] & 0x80) != 0
            opcode = header[0] & 0x0F
            if opcode == 0x8:  # Close frame
                return None
            if opcode == 0x9:  # Ping → send pong, then continue reading
                length = header[1] & 0x7F
                payload = self._recv_exact(length) if length else b""
                self._send_pong(payload or b"")
                continue
            if opcode == 0xA:  # Pong (ignore), continue reading
                length = header[1] & 0x7F
                if length:
                    self._recv_exact(length)
                continue

            masked = (header[1] & 0x80) != 0
            length = header[1] & 0x7F

            if length == 126:
                ext = self._recv_exact(2)
                if not ext:
                    return None
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(8)
                if not ext:
                    return None
                length = struct.unpack("!Q", ext)[0]

            if masked:
                mask = self._recv_exact(4)
                if not mask:
                    return None
                data = self._recv_exact(length)
                if data is None:
                    return None
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            else:
                data = self._recv_exact(length)

            if data is None:
                return None

            # Handle fragmented messages per RFC 6455
            if opcode != 0x0 and not fin:
                # First fragment of a new message
                self._frag_opcode = opcode
                self._frag_buf = data
                continue
            elif opcode == 0x0:
                # Continuation frame — append data with size limit
                self._frag_buf += data
                if len(self._frag_buf) > self._MAX_MESSAGE_SIZE:
                    self._frag_buf = b""
                    self._frag_opcode = None
                    return None  # Drop oversized message
                if fin:
                    result = self._frag_buf
                    self._frag_buf = b""
                    self._frag_opcode = None
                    return result.decode("utf-8")
                continue

            # Non-fragmented frame (opcode != 0, fin=1)
            return data.decode("utf-8")

    def _send_pong(self, payload):
        """Send a masked pong frame (RFC 6455: client frames MUST be masked)."""
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x8A, 0x80 | len(payload)]) + mask + masked
        try:
            self._sock.sendall(frame)
        except Exception:
            pass

    def _recv_exact(self, n):
        """Read exactly n bytes, consuming from handshake buffer first."""
        buf = b""
        # Drain any leftover bytes from HTTP handshake
        if hasattr(self, '_recv_buf') and self._recv_buf:
            take = min(n, len(self._recv_buf))
            buf = self._recv_buf[:take]
            self._recv_buf = self._recv_buf[take:]
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


def connect_room_ws(relay_url, room_id, token, on_message, on_error=None):
    """Connect to a room's WebSocket push endpoint.

    Returns WebSocketClient (call .close() to disconnect).
    """
    # Convert https://relay.ai-peer.chat → wss://relay.ai-peer.chat
    ws_url = relay_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/rooms/{room_id}/ws?token={token}"

    client = WebSocketClient(ws_url, on_message=on_message, on_error=on_error)
    client.connect()
    return client
