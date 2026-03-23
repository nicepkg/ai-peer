"""Unit tests for CLI dispatch — no daemon needed."""
import pytest
from ai_peer.ops import run_command


class TestDispatch:
    def test_unknown_command(self):
        with pytest.raises(ValueError, match="Unknown command"):
            run_command(["nonexistent"])

    def test_no_args(self):
        with pytest.raises(ValueError):
            run_command([])

    def test_daemon_no_subcommand(self):
        with pytest.raises(ValueError, match="Usage"):
            run_command(["daemon"])

    def test_room_no_subcommand(self):
        with pytest.raises(ValueError, match="Usage"):
            run_command(["room"])

    def test_identity(self):
        result = run_command(["identity"])
        assert "id" in result
        assert "name" in result
        assert "type" in result


class TestMentionDetection:
    def test_detect_mentions_in_response(self):
        """@mention detection is in cmd_invite, tested via regex directly."""
        import re
        text = "I think @codex should review this. Also ask @opencode for perf."
        mentions = re.findall(r"@(claude-code|codex|opencode)\b", text, re.IGNORECASE)
        assert "codex" in mentions
        assert "opencode" in mentions

    def test_no_mentions(self):
        import re
        text = "This looks good to me. No need for other opinions."
        mentions = re.findall(r"@(claude-code|codex|opencode)\b", text, re.IGNORECASE)
        assert mentions == []
