"""Tests for WebSocket client frame parsing."""
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ai_peer.ws_client import WebSocketClient


class TestFrameParsing:
    """Test WebSocket frame reading (RFC 6455)."""

    def _make_text_frame(self, text):
        """Build an unmasked text frame."""
        payload = text.encode("utf-8")
        length = len(payload)
        if length < 126:
            return bytes([0x81, length]) + payload
        elif length < 65536:
            return bytes([0x81, 126]) + struct.pack("!H", length) + payload
        else:
            return bytes([0x81, 127]) + struct.pack("!Q", length) + payload

    def _make_close_frame(self):
        return bytes([0x88, 0x00])

    def _make_ping_frame(self, payload=b""):
        return bytes([0x89, len(payload)]) + payload

    def test_read_short_text_frame(self):
        frame_data = self._make_text_frame('{"type":"message","data":"hello"}')
        ws = WebSocketClient("ws://test")
        ws._sock = MagicMock()
        ws._sock.recv = MagicMock(side_effect=[frame_data[:2], frame_data[2:]])

        # Mock _recv_exact to return frame data correctly
        pos = [0]
        def recv_exact(n):
            result = frame_data[pos[0]:pos[0]+n]
            pos[0] += n
            return result if result else None

        ws._recv_exact = recv_exact
        result = ws._read_frame()
        assert result == '{"type":"message","data":"hello"}'

    def test_read_close_frame(self):
        ws = WebSocketClient("ws://test")
        close = self._make_close_frame()
        pos = [0]
        def recv_exact(n):
            result = close[pos[0]:pos[0]+n]
            pos[0] += n
            return result if result else None
        ws._recv_exact = recv_exact

        result = ws._read_frame()
        assert result is None  # Close frame returns None

    def test_client_init(self):
        ws = WebSocketClient("wss://relay.ai-peer.chat/rooms/test/ws?token=abc")
        assert ws.url == "wss://relay.ai-peer.chat/rooms/test/ws?token=abc"
        assert ws._running is False
