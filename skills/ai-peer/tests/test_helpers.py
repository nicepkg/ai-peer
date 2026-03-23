"""Tests for helpers.py and ops_room.py — new v0.3-v1.0 features."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add package to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class TestParseConnectionString:
    """Test peer:// connection string parsing."""

    def setup_method(self):
        from ai_peer.ops_room import _parse_connection_string
        self.parse = _parse_connection_string

    def test_plain_room_id(self):
        room_id, relay, token = self.parse("room-abc12345")
        assert room_id == "room-abc12345"
        assert relay is None
        assert token is None

    def test_peer_url(self):
        room_id, relay, token = self.parse("peer://relay.ai-peer.chat/room-abc")
        assert room_id == "room-abc"
        assert relay == "https://relay.ai-peer.chat"
        assert token is None

    def test_peer_url_with_token(self):
        room_id, relay, token = self.parse(
            "peer://relay.ai-peer.chat/room-abc?token=deadbeef1234"
        )
        assert room_id == "room-abc"
        assert relay == "https://relay.ai-peer.chat"
        assert token == "deadbeef1234"

    def test_invalid_peer_url(self):
        with pytest.raises(ValueError, match="Invalid connection string"):
            self.parse("peer://no-room-id")


class TestResolveRelayUrl:
    def setup_method(self):
        from ai_peer.ops_room import _resolve_relay_url
        self.resolve = _resolve_relay_url

    def test_default(self):
        from ai_peer.constants import DEFAULT_RELAY
        assert self.resolve("default") == DEFAULT_RELAY

    def test_custom(self):
        assert self.resolve("https://my-relay.com") == "https://my-relay.com"

    def test_none(self):
        assert self.resolve(None) is None


class TestGetRoomCtx:
    def test_raises_on_missing_room(self):
        from ai_peer.helpers import _get_room_ctx
        client = MagicMock()
        client.get_room.return_value = None

        with pytest.raises(ValueError, match="not found"):
            _get_room_ctx(client, "nonexistent-room")

    def test_raises_on_error_response(self):
        from ai_peer.helpers import _get_room_ctx
        client = MagicMock()
        client.get_room.return_value = {"error": "HTTP 404"}

        with pytest.raises(ValueError, match="not found"):
            _get_room_ctx(client, "bad-room")

    def test_returns_relay_with_token(self):
        from ai_peer.helpers import _get_room_ctx
        client = MagicMock()
        client.get_room.return_value = {
            "id": "room-1", "relay_url": "https://relay.test", "token": "abc123", "password": None
        }

        relay, password = _get_room_ctx(client, "room-1")
        assert relay is not None
        assert relay.token == "abc123"
        assert password is None

    def test_returns_none_relay_for_local(self):
        from ai_peer.helpers import _get_room_ctx
        client = MagicMock()
        client.get_room.return_value = {
            "id": "room-1", "relay_url": None, "token": None, "password": "secret"
        }

        relay, password = _get_room_ctx(client, "room-1")
        assert relay is None
        assert password == "secret"


class TestIdentityMigration:
    def test_migrates_pid_based_id(self):
        from ai_peer.helpers import get_or_create_identity
        from ai_peer.constants import IDENTITY_FILE, PEERS_HOME, get_machine_id

        with tempfile.TemporaryDirectory() as tmp:
            identity_file = Path(tmp) / "identity.json"
            old_identity = {
                "id": f"{get_machine_id()}-human-12345",
                "name": "old-user",
                "type": "human",
                "tool": "cli",
                "machine": get_machine_id(),
            }
            identity_file.write_text(json.dumps(old_identity))

            with patch("ai_peer.helpers.IDENTITY_FILE", identity_file):
                result = get_or_create_identity()

            username = os.environ.get("USER", os.environ.get("USERNAME", "user"))
            assert result["id"] == f"{get_machine_id()}-human-{username}"
            assert result["name"] == username


class TestMergeMessages:
    def test_dedup_by_id(self):
        from ai_peer.helpers import _merge_messages

        local = {"messages": [
            {"id": "m1", "content": "hello", "created_at": "2026-01-01T00:00:00"},
            {"id": "m2", "content": "world", "created_at": "2026-01-01T00:01:00"},
        ]}
        relay = {"messages": [
            {"id": "m2", "content": "world", "created_at": "2026-01-01T00:01:00"},
            {"id": "m3", "content": "!", "created_at": "2026-01-01T00:02:00"},
        ]}

        result = _merge_messages(local, relay)
        assert len(result["messages"]) == 3
        ids = [m["id"] for m in result["messages"]]
        assert ids == ["m1", "m2", "m3"]

    def test_handles_none(self):
        from ai_peer.helpers import _merge_messages
        result = _merge_messages(None, None)
        assert result["messages"] == []


class TestPeerAuth:
    """Test per-peer auth signature."""

    def test_peer_signature_deterministic(self):
        from ai_peer.helpers import _peer_signature
        sig1 = _peer_signature("secret123", "room-abc")
        sig2 = _peer_signature("secret123", "room-abc")
        assert sig1 == sig2
        assert len(sig1) == 64  # HMAC-SHA256 hex

    def test_peer_signature_differs_by_room(self):
        from ai_peer.helpers import _peer_signature
        sig1 = _peer_signature("secret", "room-a")
        sig2 = _peer_signature("secret", "room-b")
        assert sig1 != sig2

    def test_peer_signature_differs_by_secret(self):
        from ai_peer.helpers import _peer_signature
        sig1 = _peer_signature("secret-1", "room-a")
        sig2 = _peer_signature("secret-2", "room-a")
        assert sig1 != sig2

    def test_identity_has_peer_secret(self):
        from ai_peer.helpers import get_or_create_identity
        from ai_peer.constants import PEERS_HOME, IDENTITY_FILE

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                __import__("ai_peer.constants", fromlist=["PEERS_HOME"]),
                "PEERS_HOME", Path(tmp),
            ), patch.object(
                __import__("ai_peer.helpers", fromlist=["PEERS_HOME"]),
                "PEERS_HOME", Path(tmp),
            ), patch.object(
                __import__("ai_peer.helpers", fromlist=["IDENTITY_FILE"]),
                "IDENTITY_FILE", Path(tmp) / "identity.json",
            ):
                identity = get_or_create_identity()
                assert "peer_secret" in identity
                assert len(identity["peer_secret"]) == 64  # 32 bytes hex


class TestMergePeers:
    """Test _merge_peers helper."""

    def test_local_only(self):
        from ai_peer.helpers import _merge_peers
        client = MagicMock()
        client.room_peers.return_value = {"peers": [
            {"id": "p1", "name": "Alice"},
            {"id": "p2", "name": "Bob"},
        ]}
        result = _merge_peers(client, "room-1")
        assert len(result) == 2

    def test_merges_relay(self):
        from ai_peer.helpers import _merge_peers
        client = MagicMock()
        client.room_peers.return_value = {"peers": [{"id": "p1", "name": "Alice"}]}
        relay = MagicMock()
        relay.get_peers.return_value = {"peers": [
            {"id": "p1", "name": "Alice"},
            {"id": "p3", "name": "Charlie"},
        ]}
        result = _merge_peers(client, "room-1", relay)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"Alice", "Charlie"}


class TestDaemonConfig:
    """Test daemon config persistence."""

    def test_save_and_load(self):
        from ai_peer.client import _save_daemon_cfg, _load_daemon_cfg

        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "daemon.json"
            with patch("ai_peer.client.DAEMON_CFG_FILE", cfg_file), \
                 patch("ai_peer.client.PEERS_HOME", Path(tmp)):
                _save_daemon_cfg("0.0.0.0", 8000)
                host, port = _load_daemon_cfg()
                assert host == "0.0.0.0"
                assert port == 8000

    def test_defaults_on_missing(self):
        from ai_peer.client import _load_daemon_cfg

        with tempfile.TemporaryDirectory() as tmp:
            with patch("ai_peer.client.DAEMON_CFG_FILE", Path(tmp) / "nope.json"):
                host, port = _load_daemon_cfg()
                assert host == "127.0.0.1"
                assert port == 7899


class TestDBMigration:
    def test_token_column_exists(self):
        """DB should have token column after migration."""
        from ai_peer.db import PeerDB

        with tempfile.TemporaryDirectory() as tmp:
            db = PeerDB(Path(tmp) / "test.db")
            room = db.create_room("test", "public", token="my-secret-token")
            assert room["token"] == "my-secret-token"

            fetched = db.get_room(room["id"])
            assert fetched["token"] == "my-secret-token"
            db.close()


class TestDispatch:
    def test_quick_in_dispatch(self):
        from ai_peer.ops import run_command
        with pytest.raises(ValueError, match="question"):
            run_command(["quick"])

    def test_discuss_in_dispatch(self):
        from ai_peer.ops import run_command
        with pytest.raises(ValueError, match="tools"):
            run_command(["discuss"])

    def test_help_text_contains_new_commands(self):
        from ai_peer.ops import run_command
        with pytest.raises(ValueError, match="quick") as exc_info:
            run_command([])
        assert "discuss" in str(exc_info.value)
