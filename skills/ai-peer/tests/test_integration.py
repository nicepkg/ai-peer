"""Integration tests — needs running daemon."""
import pytest


@pytest.mark.integration
class TestFullFlow:
    def test_room_lifecycle(self, daemon):
        client = daemon

        # Create room
        room = client.create_room("integration-test", "local")
        assert room["id"].startswith("room-")
        room_id = room["id"]

        # Register peers
        client.register_peer("test-ai", "TestAI", "ai", "claude-code", "test-machine")
        client.register_peer("test-human", "Boss", "human", "cli", "test-machine")

        # Join room
        client.join_room(room_id, "test-ai")
        client.join_room(room_id, "test-human")
        peers = client.room_peers(room_id)
        assert len(peers["peers"]) == 2

        # Send messages
        msg1 = client.send_message(room_id, "test-ai", "Hello from AI")
        assert msg1["content"] == "Hello from AI"
        assert msg1["peer_name"] == "TestAI"

        msg2 = client.send_message(room_id, "test-human", "Hello from human")
        assert msg2["peer_name"] == "Boss"

        # Read messages (2 join system + 2 chat = 4)
        msgs = client.get_messages(room_id)
        assert len(msgs["messages"]) == 4
        chat_msgs = [m for m in msgs["messages"] if m["type"] == "message"]
        assert len(chat_msgs) == 2

        # Since filter
        msgs_since = client.get_messages(room_id, since=msg1["created_at"])
        assert len(msgs_since["messages"]) == 1
        assert msgs_since["messages"][0]["content"] == "Hello from human"

        # Leave + rejoin
        client.leave_room(room_id, "test-ai")
        peers = client.room_peers(room_id)
        assert len(peers["peers"]) == 1

        # Cleanup
        client.delete_room(room_id)
        assert client.get_room(room_id).get("error") == "room not found"

    def test_peer_discovery(self, daemon):
        client = daemon
        peers = client.list_peers()
        assert "peers" in peers

    def test_health(self, daemon):
        client = daemon
        assert client.is_alive()
