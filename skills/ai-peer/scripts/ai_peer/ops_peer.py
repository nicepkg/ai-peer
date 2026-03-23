"""Peer discovery, history, and export operations."""
import json

from .constants import get_machine_id
from .client import ensure_daemon
from .spawn import discover_tools
from .helpers import (
    _get_room_ctx, _read_messages, _merge_peers,
    get_or_create_identity, make_ai_peer_id,
)


def cmd_discover(args):
    tools = discover_tools()
    client = ensure_daemon()
    registered = []
    for t in tools:
        pid = make_ai_peer_id(t["tool"])
        peer = client.register_peer(pid, t["tool"], "ai", t["tool"], t["machine"])
        registered.append(peer)
    return {"discovered": tools, "registered": registered}


def cmd_list(args):
    client = ensure_daemon()
    return client.list_peers()


def cmd_register(args):
    if len(args) < 3:
        raise ValueError("Usage: peer register <name> <type:ai|human> <tool> [--host host] [--port N]")

    name, ptype, tool = args[0], args[1], args[2]
    host = port = None
    for i, a in enumerate(args[3:], 3):
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1]
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])

    client = ensure_daemon()
    pid = f"{host or get_machine_id()}-{tool}"
    return client.register_peer(pid, name, ptype, tool, host or get_machine_id(), host, port)


def cmd_history(args):
    room_id = args[0] if args and not args[0].startswith("-") else None
    limit = 50
    for i, a in enumerate(args or []):
        if a == "-n" and i + 1 < len(args):
            limit = int(args[i + 1])

    client = ensure_daemon()

    if not room_id:
        return client.list_rooms()

    relay, password = _get_room_ctx(client, room_id)
    return _read_messages(client, room_id, relay, password, limit=limit)


def cmd_export(args):
    if not args:
        raise ValueError("Usage: peer export <room-id> [--format md|json] [--output file]")

    room_id = args[0]
    fmt = "md"
    output_path = None
    i = 1
    while i < len(args):
        if args[i] == "--format" and i + 1 < len(args):
            fmt = args[i + 1]; i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]; i += 2
        else:
            i += 1

    client = ensure_daemon()
    room = client.get_room(room_id)
    if not room or room.get("error"):
        raise ValueError(f"Room not found: {room_id}. Run 'peer room list' to see available rooms.")

    relay, password = _get_room_ctx(client, room_id)
    resp = _read_messages(client, room_id, relay, password, limit=9999)
    messages = resp.get("messages", [])
    peers = _merge_peers(client, room_id, relay)

    if fmt == "json":
        content = {"room": room, "peers": peers, "messages": messages}
    else:
        content = _export_markdown(room, peers, messages)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            if fmt == "json":
                f.write(json.dumps(content, ensure_ascii=False, indent=2))
            else:
                f.write(content)
        return {"exported": output_path, "format": fmt, "messages": len(messages)}

    if fmt == "md":
        return {"markdown": content, "messages": len(messages)}
    return content


def _export_markdown(room, peers, messages):
    """Format conversation as readable Markdown."""
    lines = [
        f"# {room['name']}",
        f"",
        f"- **Room**: `{room['id']}`",
        f"- **Mode**: {room['mode']}",
        f"- **Created**: {room['created_at'][:19]}",
        f"- **Participants**: {', '.join(p['name'] + ' (' + (p.get('tool') or p['type']) + ')' for p in peers)}",
        f"",
        f"---",
        f"",
    ]
    for msg in messages:
        name = msg.get("peer_name", msg.get("peer_id", "?"))
        tool = msg.get("peer_tool", "")
        ts = msg.get("created_at", "")[:19].replace("T", " ")
        if msg.get("type") == "system":
            lines.append(f"*{name} {msg['content']}* — {ts}")
        else:
            tag = f" ({tool})" if tool else ""
            lines.append(f"**{name}{tag}** [{ts}]:")
            lines.append(f"")
            lines.append(msg["content"])
        lines.append("")
    return "\n".join(lines)
