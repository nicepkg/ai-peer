"""Unified message helpers + identity management.

ALL message I/O MUST go through _post_message() and _read_messages().
"""
import hashlib
import hmac
import json
import os
import secrets
import sys

from .constants import PEERS_HOME, IDENTITY_FILE, get_machine_id
from .client import PeerClient
from .crypto import encrypt, decrypt
from .relay_client import RelayClient


# === Message I/O (single source of truth) ===

def _get_room_ctx(client, room_id):
    """Get (relay_client_or_None, password_or_None) for a room.

    Raises ValueError if room does not exist.
    """
    room = client.get_room(room_id)
    if not room or room.get("error"):
        raise ValueError(
            f"Room '{room_id}' not found. Run 'peer room list' to see available rooms."
        )
    token = room.get("token")
    relay = RelayClient(room["relay_url"], token=token) if room.get("relay_url") else None
    password = room.get("password")
    return relay, password


def _post_message(client, room_id, peer_id, content, relay=None, password=None,
                  msg_type="message", peer_name="", peer_tool="", peer_signature=""):
    """Send: encrypt → local write → relay dual-write. Returns (msg, relay_ok)."""
    wire = encrypt(content, password, room_id) if password and msg_type != "system" else content
    msg = client.send_message(room_id, peer_id, wire, msg_type=msg_type)

    relay_ok = True
    if relay:
        r = relay.send_message(
            room_id, peer_id, wire,
            peer_name=peer_name, peer_tool=peer_tool,
            msg_type=msg_type,
            msg_id=msg.get("id") if msg else None,
            peer_signature=peer_signature,
        )
        relay_ok = r is not None and not r.get("error")

    if msg:
        msg["content"] = content
    return msg, relay_ok


_PAGE_SIZE = 10000  # Local server limit (relay is lower, ~1000)


def _paginated_get(fetcher, since=None, limit=50):
    """Paginate fetcher(since, limit) to collect up to `limit` messages.

    Two modes:
    - since=<timestamp>: forward pagination (ASC), straightforward
    - since=None (latest N): single fetch (local server supports up to 10000)
    """
    if since is None or limit <= _PAGE_SIZE:
        # No cursor: let backend handle limit natively (DESC + LIMIT)
        return fetcher(since, limit)

    # Forward pagination from cursor
    all_msgs = []
    remaining = limit
    cursor = since
    while remaining > 0:
        batch_limit = min(remaining, _PAGE_SIZE)
        resp = fetcher(cursor, batch_limit)
        msgs = (resp or {}).get("messages", [])
        if not msgs:
            break
        all_msgs.extend(msgs)
        remaining -= len(msgs)
        if len(msgs) < batch_limit:
            break
        cursor = msgs[-1].get("created_at")
    return {"messages": all_msgs}


def _read_messages(client, room_id, relay=None, password=None,
                   since=None, limit=50):
    """Read: local + relay merge → deduplicate → decrypt."""
    local = _paginated_get(
        lambda s, l: client.get_messages(room_id, since=s, limit=l),
        since=since, limit=limit,
    )
    relay_ok = True

    if relay:
        remote = _paginated_get(
            lambda s, l: relay.get_messages(room_id, since=s, limit=l),
            since=since, limit=limit,
        )
        relay_ok = remote is not None and not (isinstance(remote, dict) and remote.get("error"))
        resp = _merge_messages(local, remote)
    else:
        resp = local

    messages = (resp or {}).get("messages", [])
    if password:
        for msg in messages:
            if msg.get("type") != "system":
                msg["content"] = decrypt(msg["content"], password, room_id, strict=True)

    return {"messages": messages, "relay_ok": relay_ok}


def _merge_messages(local_resp, relay_resp):
    """Merge local + relay messages, deduplicate by ID, sort by time.

    System messages (join/leave) are also deduped by peer_id+content to avoid
    duplicates from local daemon and relay both writing join events.
    """
    local_msgs = (local_resp or {}).get("messages", [])
    relay_msgs = (relay_resp or {}).get("messages", [])

    seen_ids = set()
    seen_sys = set()  # peer_id:content for system message dedup
    merged = []
    for msg in local_msgs + relay_msgs:
        msg_id = msg.get("id")
        if msg_id and msg_id in seen_ids:
            continue
        if msg_id:
            seen_ids.add(msg_id)

        # Dedup system messages (join/leave) by peer_id + content + timestamp
        # Include timestamp so legitimate rejoin events are preserved
        if msg.get("type") == "system":
            sys_key = f"{msg.get('peer_id', '')}:{msg.get('content', '')}:{msg.get('created_at', '')[:19]}"
            if sys_key in seen_sys:
                continue
            seen_sys.add(sys_key)
        elif not msg_id:
            # Non-system without ID: use content hash as fallback
            fallback = hashlib.md5(
                f"{msg.get('peer_id', '')}:{msg.get('content', '')}:{msg.get('created_at', '')}".encode()
            ).hexdigest()
            if fallback in seen_ids:
                continue
            seen_ids.add(fallback)

        merged.append(msg)

    merged.sort(key=lambda m: m.get("created_at", ""))
    return {"messages": merged}


def _merge_peers(client, room_id, relay=None):
    """Merge local + relay peers for complete participant list."""
    peers_resp = client.room_peers(room_id)
    peers = (peers_resp or {}).get("peers", []) if peers_resp else []
    if relay:
        relay_peers_resp = relay.get_peers(room_id)
        if relay_peers_resp and not relay_peers_resp.get("error"):
            relay_peers = relay_peers_resp.get("peers", [])
            if isinstance(relay_peers, list):
                seen_ids = {p.get("id") for p in peers}
                for rp in relay_peers:
                    if rp.get("id") not in seen_ids:
                        peers.append(rp)
    return peers


def _peer_signature(peer_secret, room_id):
    """Generate HMAC-SHA256 signature for per-peer auth.

    Returns empty string if no secret — relay treats empty as "no auth".
    """
    if not peer_secret:
        return ""
    return hmac.new(peer_secret.encode(), room_id.encode(), hashlib.sha256).hexdigest()


# === Identity ===

def get_or_create_identity():
    """Get or create local human identity. Auto-migrates old PID-based IDs."""
    username = os.environ.get("USER", os.environ.get("USERNAME", "user"))
    expected_id = f"{get_machine_id()}-human-{username}"

    if IDENTITY_FILE.exists():
        try:
            identity = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            identity = None
        if not identity or not isinstance(identity, dict):
            IDENTITY_FILE.unlink(missing_ok=True)
        else:
            old_id = identity.get("id", "")
            needs_write = False
            if "-human-" in old_id and old_id != expected_id:
                identity["id"] = expected_id
                identity["name"] = username
                needs_write = True
            # Ensure peer_secret exists (migration from pre-auth versions)
            if "peer_secret" not in identity:
                identity["peer_secret"] = secrets.token_hex(32)
                needs_write = True
            if needs_write:
                IDENTITY_FILE.write_text(json.dumps(identity, indent=2, ensure_ascii=False), encoding="utf-8")
            return identity

    PEERS_HOME.mkdir(parents=True, exist_ok=True)
    identity = {
        "id": expected_id,
        "name": username,
        "type": "human",
        "tool": "cli",
        "machine": get_machine_id(),
        "peer_secret": secrets.token_hex(32),
    }
    IDENTITY_FILE.write_text(json.dumps(identity, indent=2, ensure_ascii=False), encoding="utf-8")
    return identity


def make_ai_peer_id(tool_name):
    """Generate a peer ID for a local AI tool."""
    return f"{get_machine_id()}-{tool_name}"


def _resolve_identity(client, room_id, peer_name=None, peer_tool=None, peer_type=None,
                      relay=None):
    """Resolve identity from flags. Auto-infers type=ai from --tool.

    If relay is provided, also registers peer on relay (needed for public rooms).
    Raises RuntimeError if relay join fails.
    Returns (peer_id, display_name, tool, type, peer_secret).
    """
    identity = get_or_create_identity()
    peer_secret = identity.get("peer_secret", "")

    if peer_tool:
        resolved_type = peer_type or "ai"
        resolved_name = peer_name or peer_tool
        pid = make_ai_peer_id(peer_tool) if resolved_type == "ai" else f"{get_machine_id()}-{resolved_name}"
        # Derive per-AI secret from human secret + tool name (matches cmd_invite)
        if resolved_type == "ai" and peer_secret:
            effective_secret = hmac.new(
                peer_secret.encode(), peer_tool.encode(), hashlib.sha256
            ).hexdigest()
        else:
            effective_secret = peer_secret
        client.register_peer(pid, resolved_name, resolved_type, peer_tool, get_machine_id())
        client.join_room(room_id, pid)
        if relay:
            sig = _peer_signature(effective_secret, room_id)
            r = relay.join_room(room_id, pid, resolved_name, resolved_type,
                               peer_tool, get_machine_id(), peer_signature=sig)
            if r is None or (isinstance(r, dict) and r.get("error")):
                raise RuntimeError(f"Relay join failed: {(r or {}).get('error', 'unreachable')}")
        return pid, resolved_name, peer_tool, resolved_type, effective_secret

    pid = identity["id"]
    display_name = peer_name or identity["name"]
    display_type = peer_type or identity["type"]
    client.register_peer(pid, display_name, display_type,
                         identity.get("tool"), identity.get("machine"))
    client.join_room(room_id, pid)
    if relay:
        sig = _peer_signature(peer_secret, room_id)
        r = relay.join_room(room_id, pid, display_name, display_type,
                           identity.get("tool", "cli"), identity.get("machine", ""),
                           peer_signature=sig)
        if r is None or (isinstance(r, dict) and r.get("error")):
            raise RuntimeError(f"Relay join failed: {(r or {}).get('error', 'unreachable')}")
    return pid, display_name, identity.get("tool", "cli"), display_type, peer_secret
