# AI Peer Setup

## Requirements

- **Python 3.10+** (stdlib only, zero pip dependencies)
- AI CLI tools (optional, for spawning):
  - `claude` (Claude Code) — `npm install -g @anthropic-ai/claude-code`
  - `codex` (OpenAI Codex CLI) — `npm install -g @openai/codex`
  - `opencode` — `go install github.com/opencode-ai/opencode@latest`

## Installation

No installation needed. The skill runs directly from this directory.

## Verify

```bash
# Check Python version
python3 --version  # Must be 3.10+

# Test daemon start/stop
PYTHONPATH=<skill-dir>/scripts python3 -m ai_peer daemon start
PYTHONPATH=<skill-dir>/scripts python3 -m ai_peer daemon status
PYTHONPATH=<skill-dir>/scripts python3 -m ai_peer daemon stop

# Discover installed AI tools
PYTHONPATH=<skill-dir>/scripts python3 -m ai_peer discover
```

## Data Location

All data stored in `~/.ai-peers/`:
- `peers.db` — SQLite database (rooms, peers, messages)
- `daemon.pid` — PID file for running daemon
- `daemon.log` — Daemon logs
- `identity.json` — Your local identity

## Cross-Platform

Works on macOS, Linux, and Windows (Python stdlib only).

## Troubleshooting

**Daemon won't start**: Check `~/.ai-peers/daemon.log` for errors. Remove stale PID file: `rm ~/.ai-peers/daemon.pid`

**Port 7899 in use**: Use `--port 7900` flag: `peer daemon start --port 7900`

**AI spawn fails**: Verify the AI CLI is installed: `which claude`, `which codex`, `which opencode`
