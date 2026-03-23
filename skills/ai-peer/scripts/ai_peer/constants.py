"""AI Peer constants and configuration."""
import os
import platform
from pathlib import Path

# Directories
PEERS_HOME = Path(os.environ.get("AI_PEERS_HOME", Path.home() / ".ai-peers"))
DB_PATH = PEERS_HOME / "peers.db"
PID_FILE = PEERS_HOME / "daemon.pid"
LOG_FILE = PEERS_HOME / "daemon.log"
IDENTITY_FILE = PEERS_HOME / "identity.json"
DAEMON_CFG_FILE = PEERS_HOME / "daemon.json"

# Daemon
DEFAULT_PORT = 7899
DEFAULT_HOST = "127.0.0.1"
LAN_HOST = "0.0.0.0"

VERSION = "1.0.0"

# Default relay for public rooms
DEFAULT_RELAY = "https://relay.ai-peer.chat"

# AI tool spawn commands — {prompt} replaced at runtime
TOOL_COMMANDS = {
    "claude-code": {
        "cmd": ["claude", "-p", "{prompt}", "--output-format", "text"],
        "env_unset": ["CLAUDECODE"],
        "stdin_prompt": True,  # Pass prompt via stdin to avoid ps aux leakage
    },
    "codex": {
        "cmd": ["codex", "exec", "--full-auto", "{prompt}"],
        "env_unset": ["CLAUDECODE"],
    },
    "opencode": {
        "cmd": ["opencode", "-p", "{prompt}"],
        "env_unset": [],
    },
}


def get_machine_id():
    """Stable machine identifier."""
    return platform.node()
