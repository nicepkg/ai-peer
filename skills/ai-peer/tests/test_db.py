"""Unit tests for ai_peer.db — no daemon needed."""
import tempfile
from pathlib import Path

import pytest
from ai_peer.db import PeerDB


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield PeerDB(Path(tmp) / "test.db")


class TestRooms:
    def test_create_and_get(self, db):
        room = db.create_room("test-room", "local")
        assert room["name"] == "test-room"
        assert room["mode"] == "local"
        assert room["id"].startswith("room-")

        fetched = db.get_room(room["id"])
        assert fetched["id"] == room["id"]

    def test_list_rooms(self, db):
        db.create_room("a")
        db.create_room("b")
        rooms = db.list_rooms()
        assert len(rooms) == 2

    def test_delete_room(self, db):
        room = db.create_room("to-delete")
        db.delete_room(room["id"])
        assert db.get_room(room["id"]) is None

    def test_get_nonexistent(self, db):
        assert db.get_room("room-nonexistent") is None


class TestPeers:
    def test_register_and_list(self, db):
        peer = db.register_peer("p1", "Ace", "ai", "claude-code", "mac")
        assert peer["name"] == "Ace"
        assert peer["type"] == "ai"
        peers = db.list_peers()
        assert len(peers) == 1

    def test_upsert(self, db):
        db.register_peer("p1", "Ace", "ai", "claude-code")
        db.register_peer("p1", "Ace-v2", "ai", "claude-code")
        peers = db.list_peers()
        assert len(peers) == 1
        assert peers[0]["name"] == "Ace-v2"

    def test_find_by_tool(self, db):
        db.register_peer("p1", "Ace", "ai", "claude-code")
        db.register_peer("p2", "Codex", "ai", "codex")
        found = db.find_peer_by_tool("codex")
        assert found["name"] == "Codex"


class TestMessages:
    def test_send_and_get(self, db):
        room = db.create_room("msg-test")
        db.register_peer("p1", "Ace", "ai", "claude-code")
        msg = db.add_message(room["id"], "p1", "hello world")
        assert msg["content"] == "hello world"
        assert msg["peer_name"] == "Ace"

        msgs = db.get_messages(room["id"])
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello world"

    def test_since_filter(self, db):
        room = db.create_room("filter-test")
        db.register_peer("p1", "Ace", "ai")
        m1 = db.add_message(room["id"], "p1", "first")
        m2 = db.add_message(room["id"], "p1", "second")

        msgs = db.get_messages(room["id"], since=m1["created_at"])
        assert len(msgs) == 1
        assert msgs[0]["content"] == "second"

    def test_limit(self, db):
        room = db.create_room("limit-test")
        db.register_peer("p1", "X", "human")
        for i in range(10):
            db.add_message(room["id"], "p1", f"msg-{i}")
        msgs = db.get_messages(room["id"], limit=3)
        assert len(msgs) == 3


class TestRoomPeers:
    def test_join_and_list(self, db):
        room = db.create_room("join-test")
        db.register_peer("p1", "Ace", "ai")
        db.register_peer("p2", "Boss", "human")
        db.join_room(room["id"], "p1")
        db.join_room(room["id"], "p2")

        peers = db.room_peers(room["id"])
        assert len(peers) == 2

    def test_leave(self, db):
        room = db.create_room("leave-test")
        db.register_peer("p1", "Ace", "ai")
        db.join_room(room["id"], "p1")
        db.leave_room(room["id"], "p1")
        assert len(db.room_peers(room["id"])) == 0

    def test_idempotent_join(self, db):
        room = db.create_room("idem-test")
        db.register_peer("p1", "Ace", "ai")
        db.join_room(room["id"], "p1")
        db.join_room(room["id"], "p1")  # Should not raise
        assert len(db.room_peers(room["id"])) == 1
