"""AI operations: invite, quick, discuss."""
import hashlib
import hmac
import re
import secrets
import sys

from .constants import get_machine_id
from .client import ensure_daemon
from .relay_client import RelayClient
from .spawn import build_conversation_prompt, spawn_ai
from .helpers import (
    _get_room_ctx, _post_message, _read_messages,
    get_or_create_identity, make_ai_peer_id, _peer_signature,
    _resolve_identity,
)


def cmd_invite(args):
    if not args:
        raise ValueError(
            "Usage: peer invite --tool <tool> --room <room-id> [--context <text>] [--timeout N]\n"
            "  Run 'peer discover' to see available tools."
        )

    tool = room_id = context = None
    timeout = 120
    i = 0
    while i < len(args):
        if args[i] == "--tool" and i + 1 < len(args):
            tool = args[i + 1]; i += 2
        elif args[i] == "--room" and i + 1 < len(args):
            room_id = args[i + 1]; i += 2
        elif args[i] == "--context" and i + 1 < len(args):
            context = args[i + 1]; i += 2
        elif args[i] == "--timeout" and i + 1 < len(args):
            timeout = int(args[i + 1]); i += 2
        elif not tool:
            tool = args[i]; i += 1
        else:
            i += 1

    if not tool or not room_id:
        raise ValueError("Both --tool and --room are required")

    client = ensure_daemon()
    relay, password = _get_room_ctx(client, room_id)

    identity = get_or_create_identity()
    human_secret = identity.get("peer_secret", "")
    # Derive per-AI secret from human secret + tool name (true per-peer auth)
    ai_secret = hmac.new(human_secret.encode(), tool.encode(), hashlib.sha256).hexdigest() if human_secret else ""
    ai_peer_id = make_ai_peer_id(tool)
    client.register_peer(ai_peer_id, tool, "ai", tool, get_machine_id())
    client.join_room(room_id, ai_peer_id)
    # Register AI peer on relay too (for cross-machine peer visibility)
    if relay:
        sig = _peer_signature(ai_secret, room_id)
        r = relay.join_room(room_id, ai_peer_id, tool, "ai", tool, get_machine_id(),
                           peer_signature=sig)
        if r is None or (isinstance(r, dict) and r.get("error")):
            raise RuntimeError(f"Relay join for {tool} failed: {(r or {}).get('error', 'unreachable')}")

    resp = _read_messages(client, room_id, relay, password, limit=30)
    messages = resp.get("messages", [])

    sender_name = "Someone"
    last_msg = ""
    for msg in reversed(messages):
        if msg.get("type") != "system" and msg["peer_id"] != ai_peer_id:
            sender_name = msg.get("peer_name", msg["peer_id"])
            last_msg = msg["content"]
            break

    if not last_msg and context:
        last_msg = context

    prompt = build_conversation_prompt(messages, last_msg, sender_name, context)
    sys.stderr.write(f"⏳ Spawning {tool}...\n")
    response, error = spawn_ai(tool, prompt, timeout=timeout)

    sig = _peer_signature(ai_secret, room_id)

    if error:
        _post_message(client, room_id, ai_peer_id, f"[spawn error] {error}",
                      relay=relay, password=password,
                      peer_name=tool, peer_tool=tool, peer_signature=sig)
        raise RuntimeError(f"Failed to spawn {tool}: {error}")

    msg, relay_ok = _post_message(
        client, room_id, ai_peer_id, response,
        relay=relay, password=password,
        peer_name=tool, peer_tool=tool, peer_signature=sig,
    )

    mentions = re.findall(r"@(claude-code|codex|opencode)\b", response, re.IGNORECASE)
    mentions = list(dict.fromkeys(m.lower() for m in mentions if m.lower() != tool.lower()))

    result = {"tool": tool, "response": response, "room": room_id}
    if mentions:
        result["mentions"] = mentions
    if relay and not relay_ok:
        result["warning"] = "Relay unreachable — response saved locally only"
    return result


def cmd_quick(args):
    """Create room + send question + invite AIs — one command."""
    if not args:
        raise ValueError(
            "Usage: peer quick \"<question>\" --tools <tool1,tool2> [--relay default] [--password pw]\n"
            "  Creates a room, sends your question, invites each AI, returns the full conversation."
        )

    question = tools_csv = relay_url = password = room_name = None
    timeout = 120
    i = 0
    while i < len(args):
        if args[i] == "--tools" and i + 1 < len(args):
            tools_csv = args[i + 1]; i += 2
        elif args[i] == "--relay" and i + 1 < len(args):
            from .ops_room import _resolve_relay_url
            relay_url = _resolve_relay_url(args[i + 1]); i += 2
        elif args[i] == "--password" and i + 1 < len(args):
            password = args[i + 1]; i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            room_name = args[i + 1]; i += 2
        elif args[i] == "--timeout" and i + 1 < len(args):
            timeout = int(args[i + 1]); i += 2
        elif not question and not args[i].startswith("-"):
            question = args[i]; i += 1
        else:
            i += 1

    if not question:
        raise ValueError("A question/prompt is required.")
    if not tools_csv:
        raise ValueError("--tools required (e.g., --tools codex,opencode)")

    tools = [t.strip() for t in tools_csv.split(",") if t.strip()]
    client = ensure_daemon()

    mode = "public" if relay_url else "local"
    token = secrets.token_hex(32) if relay_url else None
    room = client.create_room(room_name or "quick-chat", mode,
                              relay_url=relay_url, password=password, token=token)
    if not room or room.get("error"):
        raise RuntimeError(f"Failed to create room: {(room or {}).get('error', 'daemon unreachable')}")
    room_id = room["id"]

    relay_obj = RelayClient(relay_url, token=token) if relay_url else None
    try:
        pid, pname, ptool, ptype, psecret = _resolve_identity(
            client, room_id, relay=relay_obj
        )
    except RuntimeError:
        # Relay join failed — roll back local room
        client.delete_room(room_id)
        raise

    relay_ctx, pw_ctx = _get_room_ctx(client, room_id)
    sig = _peer_signature(psecret, room_id) if psecret else ""
    _post_message(client, room_id, pid, question,
                  relay=relay_ctx, password=pw_ctx,
                  peer_name=pname, peer_tool=ptool, peer_signature=sig)

    responses = {}
    for tool in tools:
        sys.stderr.write(f"⏳ Inviting {tool}...\n")
        try:
            result = cmd_invite(["--tool", tool, "--room", room_id,
                                "--timeout", str(timeout)])
            responses[tool] = result.get("response", "")
            sys.stderr.write(f"✓ {tool} responded ({len(responses[tool])} chars)\n")
        except Exception as e:
            responses[tool] = f"[error] {e}"
            sys.stderr.write(f"✗ {tool} failed: {e}\n")

    resp = _read_messages(client, room_id, relay_ctx, pw_ctx, limit=100)

    out = {"room": room_id, "question": question, "tools": tools,
           "responses": responses, "conversation": resp.get("messages", [])}
    if relay_url and token:
        relay_host = relay_url.replace("https://", "").replace("http://", "").rstrip("/")
        out["connection_string"] = f"peer://{relay_host}/{room_id}?token={token}"
    return out


def cmd_discuss(args):
    """Run a multi-round discussion between AIs."""
    if not args:
        raise ValueError(
            "Usage: peer discuss --tools <t1,t2> --rounds N [--room <id>] [--context \"topic\"]\n"
            "  Without --room, creates a new room automatically."
        )

    room_id = tools_csv = context = None
    rounds = 2
    timeout = 120
    i = 0
    while i < len(args):
        if args[i] == "--room" and i + 1 < len(args):
            room_id = args[i + 1]; i += 2
        elif args[i] == "--tools" and i + 1 < len(args):
            tools_csv = args[i + 1]; i += 2
        elif args[i] == "--rounds" and i + 1 < len(args):
            rounds = int(args[i + 1]); i += 2
        elif args[i] == "--context" and i + 1 < len(args):
            context = args[i + 1]; i += 2
        elif args[i] == "--timeout" and i + 1 < len(args):
            timeout = int(args[i + 1]); i += 2
        else:
            i += 1

    if not tools_csv:
        raise ValueError("--tools required (e.g., --tools codex,opencode)")

    tools = [t.strip() for t in tools_csv.split(",") if t.strip()]
    if len(tools) < 2:
        raise ValueError("At least 2 tools required for a discussion.")

    client = ensure_daemon()

    if not room_id:
        room = client.create_room("discussion", "local")
        if not room or room.get("error"):
            raise RuntimeError(f"Failed to create room: {(room or {}).get('error', 'daemon unreachable')}")
        room_id = room["id"]

    # Always register + join + post context (even with --room)
    relay_ctx, pw_ctx = _get_room_ctx(client, room_id)
    pid, pname, ptool, ptype, psecret = _resolve_identity(
        client, room_id, relay=relay_ctx
    )
    if context:
        sig = _peer_signature(psecret, room_id) if psecret else ""
        _post_message(client, room_id, pid, context,
                      relay=relay_ctx, password=pw_ctx,
                      peer_name=pname, peer_tool=ptool, peer_signature=sig)

    round_log = []
    for r in range(rounds):
        sys.stderr.write(f"\n--- Round {r + 1}/{rounds} ---\n")
        for tool in tools:
            sys.stderr.write(f"⏳ {tool}...\n")
            try:
                result = cmd_invite(["--tool", tool, "--room", room_id,
                                    "--timeout", str(timeout)])
                snippet = result.get("response", "")[:200]
                round_log.append({"round": r + 1, "tool": tool, "snippet": snippet})
                sys.stderr.write(f"✓ {tool} ({len(result.get('response', ''))} chars)\n")
            except Exception as e:
                round_log.append({"round": r + 1, "tool": tool, "error": str(e)})
                sys.stderr.write(f"✗ {tool}: {e}\n")

    relay_ctx, pw_ctx = _get_room_ctx(client, room_id)
    resp = _read_messages(client, room_id, relay_ctx, pw_ctx, limit=500)

    return {
        "room": room_id, "tools": tools, "rounds": rounds,
        "total_invites": rounds * len(tools),
        "log": round_log,
        "conversation": resp.get("messages", []),
    }
