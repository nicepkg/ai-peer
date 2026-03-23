"""Room and daemon CLI operations."""
import os
import secrets
import signal
import sys

from .constants import (
    PEERS_HOME, PID_FILE, DEFAULT_HOST, DEFAULT_PORT, LAN_HOST, DEFAULT_RELAY,
)
from .client import PeerClient, ensure_daemon, _stop_daemon, _load_daemon_cfg
from .relay_client import RelayClient
from .helpers import get_or_create_identity, _peer_signature


def _resolve_relay_url(raw):
    """Resolve relay shorthand: 'default' → DEFAULT_RELAY."""
    if not raw:
        return None
    return DEFAULT_RELAY if raw == "default" else raw


def _parse_connection_string(s):
    """Parse peer://relay-host/room-id?token=xxx into (room_id, relay_url, token)."""
    if s.startswith("peer://"):
        rest = s[len("peer://"):]
        token = None
        if "?token=" in rest:
            rest, token = rest.split("?token=", 1)
        parts = rest.split("/", 1)
        if len(parts) == 2:
            relay_host, room_id = parts
            return room_id, f"https://{relay_host}", token
        raise ValueError(f"Invalid connection string: {s}. Expected: peer://<relay-host>/<room-id>")
    return s, None, None


def cmd_daemon(args):
    if not args:
        raise ValueError("Usage: peer daemon start|stop|status [--lan] [--port N]")

    action = args[0]
    lan = "--lan" in args
    port = DEFAULT_PORT
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])

    if action == "start":
        host = LAN_HOST if lan else DEFAULT_HOST
        ensure_daemon(host, port)
        mode = "LAN (0.0.0.0)" if lan else "local (127.0.0.1)"
        return {"status": "started", "port": port, "mode": mode}

    elif action == "stop":
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
            except (ValueError, OSError):
                PID_FILE.unlink(missing_ok=True)
                return {"status": "removed corrupt pid file"}
            _stop_daemon()
            return {"status": "stopped", "pid": pid}
        return {"status": "not running"}

    elif action == "status":
        # Read persisted config for actual host:port (unless --port overrides)
        cfg_host, cfg_port = _load_daemon_cfg()
        status_port = port if "--port" in args else cfg_port
        check_host = DEFAULT_HOST if cfg_host == LAN_HOST else cfg_host
        client = PeerClient(check_host, status_port)
        alive = client.is_alive()
        try:
            pid = int(PID_FILE.read_text().strip()) if PID_FILE.exists() else None
        except (ValueError, OSError):
            pid = None
        return {"running": bool(alive), "pid": pid, "port": status_port,
                "host": cfg_host}

    raise ValueError(f"Unknown daemon action: {action}")


def cmd_room(args):
    if not args:
        raise ValueError(
            "Usage: peer room create|join|list|delete [args]\n"
            "  create <name> [--relay default|<url>] [--lan] [--password pw]\n"
            "  join <room-id|peer://url> [--relay <url>] [--password pw]\n"
            "  list | delete <room-id>"
        )

    action, rest = args[0], args[1:]

    # LAN mode → ensure daemon binds to 0.0.0.0 (auto-restarts if needed)
    is_lan = "--lan" in rest
    client = ensure_daemon(host=LAN_HOST if is_lan else None)

    if action == "create":
        name = rest[0] if rest and not rest[0].startswith("-") else "default"
        mode = "local"
        relay_url = None
        password = None
        if is_lan:
            mode = "lan"
        elif "--public" in rest:
            mode = "public"
        for j, a in enumerate(rest):
            if a == "--relay" and j + 1 < len(rest):
                relay_url = _resolve_relay_url(rest[j + 1])
                mode = "public"
            elif a == "--password" and j + 1 < len(rest):
                password = rest[j + 1]

        token = secrets.token_hex(32) if relay_url else None
        room = client.create_room(name, mode, relay_url=relay_url, password=password, token=token)
        if not room or room.get("error"):
            raise RuntimeError(f"Failed to create room: {(room or {}).get('error', 'daemon unreachable')}")
        if password:
            room["encrypted"] = True

        identity = get_or_create_identity()
        client.register_peer(identity["id"], identity["name"], identity["type"],
                             identity.get("tool"), identity.get("machine"))
        client.join_room(room["id"], identity["id"])

        if relay_url:
            sig = _peer_signature(identity.get("peer_secret", ""), room["id"])
            relay = RelayClient(relay_url, token=token)
            r = relay.join_room(room["id"], identity["id"], identity["name"],
                               identity["type"], identity.get("tool", ""), identity.get("machine", ""),
                               peer_signature=sig)
            if r is None or (isinstance(r, dict) and r.get("error")):
                error_msg = (r or {}).get("error", "unreachable")
                client.delete_room(room["id"])
                raise RuntimeError(f"Failed to join relay: {error_msg}. Room rolled back.")
            relay_host = relay_url.replace("https://", "").replace("http://", "").rstrip("/")
            room["connection_string"] = f"peer://{relay_host}/{room['id']}?token={token}"

        return room

    elif action == "join":
        if not rest:
            raise ValueError(
                "Usage: peer room join <room-id|peer://url> [--relay <url>] [--password pw]\n"
                "  Example: peer room join peer://relay.ai-peer.chat/room-abc?token=xxx"
            )

        join_room_id, parsed_relay, parsed_token = _parse_connection_string(rest[0])
        join_relay_url = parsed_relay
        join_password = None
        join_token = parsed_token
        for j, a in enumerate(rest):
            if a == "--relay" and j + 1 < len(rest):
                join_relay_url = _resolve_relay_url(rest[j + 1])
            elif a == "--password" and j + 1 < len(rest):
                join_password = rest[j + 1]
            elif a == "--token" and j + 1 < len(rest):
                join_token = rest[j + 1]

        if not join_relay_url:
            raise ValueError(
                "--relay <url> required for joining remote rooms.\n"
                "  Or use a connection string: peer room join peer://<relay>/<room-id>?token=xxx"
            )

        probe_relay = RelayClient(join_relay_url)
        info = probe_relay.room_info(join_room_id)
        if not info or info.get("error"):
            sys.stderr.write(f"⚠ Room '{join_room_id}' not yet active on relay. Joining anyway.\n")

        # Check if room already exists locally — update metadata, preserve messages
        existing = client.get_room(join_room_id)
        is_new_room = not existing or existing.get("error")
        # Always create/update — preserves messages (INSERT OR REPLACE keeps room_id)
        # Merge: new values override, but keep existing if new is None
        effective_password = join_password or (existing or {}).get("password")
        effective_token_for_create = join_token or (existing or {}).get("token")
        room = client.create_room(join_room_id, "public",
                                  relay_url=join_relay_url,
                                  password=effective_password,
                                  room_id=join_room_id,
                                  token=effective_token_for_create)

        identity = get_or_create_identity()
        client.register_peer(identity["id"], identity["name"], identity["type"],
                             identity.get("tool"), identity.get("machine"))
        client.join_room(join_room_id, identity["id"])

        if not effective_token_for_create:
            if is_new_room:
                client.delete_room(join_room_id)
            raise ValueError(
                "Missing room token. Use a full connection string:\n"
                "  peer room join peer://<relay>/<room-id>?token=xxx"
            )
        sig = _peer_signature(identity.get("peer_secret", ""), join_room_id)
        relay = RelayClient(join_relay_url, token=effective_token_for_create)
        r = relay.join_room(join_room_id, identity["id"], identity["name"],
                           identity["type"], identity.get("tool", ""), identity.get("machine", ""),
                           peer_signature=sig)
        if r is None or (isinstance(r, dict) and r.get("error")):
            error_msg = (r or {}).get("error", "unreachable")
            if is_new_room:
                client.delete_room(join_room_id)
            elif existing:
                # Restore previous room metadata
                client.create_room(join_room_id, existing.get("mode", "public"),
                                   relay_url=existing.get("relay_url"),
                                   password=existing.get("password"),
                                   room_id=join_room_id,
                                   token=existing.get("token"))
            raise RuntimeError(f"Failed to join relay: {error_msg}.{' Room rolled back.' if is_new_room else ''}")

        return {"joined": join_room_id, "relay": join_relay_url, "remote_info": info}

    elif action == "list":
        return client.list_rooms()

    elif action == "delete":
        if not rest:
            raise ValueError("Usage: peer room delete <room-id>")
        client.delete_room(rest[0])
        return {"deleted": rest[0]}

    raise ValueError(f"Unknown room action: {action}. Available: create, join, list, delete")
