"""AI CLI spawning — invoke local AI tools and capture responses."""
import os
import shutil
import subprocess
import sys
from .constants import TOOL_COMMANDS, get_machine_id


def discover_tools():
    """Discover locally installed AI CLI tools."""
    tools = []
    for tool_name, config in TOOL_COMMANDS.items():
        binary = config["cmd"][0]
        path = shutil.which(binary)
        if path:
            tools.append({"tool": tool_name, "binary": path, "machine": get_machine_id()})
    return tools


def build_conversation_prompt(messages, new_message, sender_name, context=None):
    """Build a prompt with conversation history for an invited AI."""
    lines = [
        "You are joining a multi-AI conversation room. Read the history, then respond to the latest message.",
        ""
    ]

    if messages:
        lines.append("=== Conversation History ===")
        for msg in messages:
            if msg.get("type") == "system":
                lines.append(f"  * {msg.get('peer_name', '?')} {msg['content']}")
            else:
                name = msg.get("peer_name", msg.get("peer_id", "?"))
                tool = msg.get("peer_tool", "")
                tag = f" ({tool})" if tool else ""
                lines.append(f"[{name}{tag}]: {msg['content']}")
        lines.append("=== End History ===\n")

    lines.append(f"New message from {sender_name}:")
    lines.append(new_message)

    if context:
        lines.append(f"\nAdditional context: {context}")

    lines.append(
        "\nRespond concisely. If you want another AI's opinion, say @<tool-name> (e.g., @codex, @opencode)."
        "\nYou can also use the ai-peer skill to send messages if available."
    )

    return "\n".join(lines)


def spawn_ai(tool_name, prompt, timeout=120):
    """Spawn an AI CLI tool with a prompt, return (response, error).

    Passes prompt via stdin where possible to avoid leaking it in `ps aux`
    and to prevent ARG_MAX issues with large prompts.
    """
    config = TOOL_COMMANDS.get(tool_name)
    if not config:
        return None, f"Unknown tool: {tool_name}. Available: {', '.join(TOOL_COMMANDS)}"

    binary = config["cmd"][0]
    if not shutil.which(binary):
        return None, f"'{binary}' not found in PATH. Install {tool_name} first."

    resolved = shutil.which(binary) or binary
    use_stdin = config.get("stdin_prompt", False)

    if use_stdin:
        # Strip {prompt} placeholder only — keep flags like -p intact
        # e.g. ["claude", "-p", "{prompt}", "--output-format", "text"]
        #    → ["claude", "-p", "--output-format", "text"]
        # claude -p (no arg) reads prompt from stdin
        cmd = [resolved if part == binary else part for part in config["cmd"] if part != "{prompt}"]
    else:
        # Fallback: pass prompt as argument (truncate to prevent ARG_MAX)
        max_arg = 32000 if sys.platform == "win32" else 131072
        safe_prompt = prompt[:max_arg] if len(prompt) > max_arg else prompt
        cmd = [safe_prompt if part == "{prompt}" else (resolved if part == binary else part) for part in config["cmd"]]

    # Clean environment
    env = os.environ.copy()
    for var in config.get("env_unset", []):
        env.pop(var, None)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            text=True, env=env, encoding="utf-8",
        )
        stdout, stderr = proc.communicate(input=prompt if use_stdin else None, timeout=timeout)
        response = stdout.strip()
        if proc.returncode != 0 and not response:
            response = stderr.strip() or f"{tool_name} exited with code {proc.returncode}"
        return response, None
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None, f"{tool_name} timed out after {timeout}s"
    except FileNotFoundError:
        return None, f"'{binary}' not found. Install {tool_name} first."
    except Exception as e:
        return None, str(e)
