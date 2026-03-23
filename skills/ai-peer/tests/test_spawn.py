"""Unit tests for ai_peer.spawn — no daemon or AI tools needed."""
from ai_peer.spawn import discover_tools, build_conversation_prompt, spawn_ai


class TestDiscoverTools:
    def test_returns_list(self):
        tools = discover_tools()
        assert isinstance(tools, list)
        # Each tool has required keys
        for t in tools:
            assert "tool" in t
            assert "binary" in t
            assert "machine" in t


class TestBuildPrompt:
    def test_basic_prompt(self):
        prompt = build_conversation_prompt([], "hello?", "Boss")
        assert "hello?" in prompt
        assert "Boss" in prompt

    def test_with_history(self):
        messages = [
            {"peer_name": "Ace", "peer_tool": "claude-code", "content": "hi", "type": "message"},
            {"peer_name": "Boss", "content": "question?", "type": "message"},
        ]
        prompt = build_conversation_prompt(messages, "new msg", "Boss")
        assert "[Ace (claude-code)]" in prompt
        assert "new msg" in prompt

    def test_with_context(self):
        prompt = build_conversation_prompt([], "hi", "X", context="review the code")
        assert "review the code" in prompt

    def test_system_messages(self):
        messages = [
            {"peer_name": "Ace", "content": "joined the room", "type": "system"},
        ]
        prompt = build_conversation_prompt(messages, "hi", "Boss")
        assert "joined the room" in prompt


class TestSpawnAi:
    def test_unknown_tool(self):
        response, error = spawn_ai("nonexistent-tool", "hello")
        assert response is None
        assert "Unknown tool" in error

    def test_missing_binary(self):
        # Temporarily add a fake tool
        from ai_peer.constants import TOOL_COMMANDS
        TOOL_COMMANDS["_test_fake"] = {"cmd": ["_nonexistent_binary_xyz", "{prompt}"], "env_unset": []}
        try:
            response, error = spawn_ai("_test_fake", "hello")
            assert response is None
            assert "not found" in error
        finally:
            del TOOL_COMMANDS["_test_fake"]
