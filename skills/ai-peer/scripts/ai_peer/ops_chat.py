"""Chat and interactive mode operations."""
import sys
import threading
import time

from .client import ensure_daemon
from .crypto import decrypt
from .helpers import (
    _get_room_ctx, _post_message, _read_messages,
    _resolve_identity, _merge_peers, _peer_signature,
)


def cmd_chat(args):
    if not args:
        raise ValueError(
            "Usage: peer chat <room-id> [message] [--as name] [--tool tool] [-i]\n"
            "  No message = read recent. --tool auto-infers AI identity."
        )

    room_id = args[0]
    rest = args[1:]
    client = ensure_daemon()

    peer_name = peer_tool = peer_type = None
    message_parts = []
    interactive = False
    limit = 20
    i = 0
    while i < len(rest):
        if rest[i] == "--as" and i + 1 < len(rest):
            peer_name = rest[i + 1]; i += 2
        elif rest[i] == "--tool" and i + 1 < len(rest):
            peer_tool = rest[i + 1]; i += 2
        elif rest[i] == "--type" and i + 1 < len(rest):
            peer_type = rest[i + 1]; i += 2
        elif rest[i] in ("--interactive", "-i"):
            interactive = True; i += 1
        elif rest[i] == "-n" and i + 1 < len(rest):
            limit = int(rest[i + 1]); i += 2
        else:
            message_parts.append(rest[i]); i += 1

    relay, password = _get_room_ctx(client, room_id)
    pid, pname, ptool, ptype, psecret = _resolve_identity(
        client, room_id, peer_name, peer_tool, peer_type, relay=relay
    )

    if interactive:
        # Send initial message if provided before entering REPL
        initial_msg = " ".join(message_parts) if message_parts else None
        if initial_msg:
            sig = _peer_signature(psecret, room_id) if psecret else ""
            _post_message(client, room_id, pid, initial_msg,
                         relay=relay, password=password,
                         peer_name=pname, peer_tool=ptool, peer_signature=sig)
            sys.stderr.write(f"[You]: {initial_msg}\n")
        return _interactive_chat(client, room_id, pid, pname, ptool, relay, password, psecret)

    message = " ".join(message_parts)

    if not message:
        return _read_messages(client, room_id, relay, password, limit=limit)

    sig = _peer_signature(psecret, room_id) if psecret else ""
    msg, relay_ok = _post_message(
        client, room_id, pid, message,
        relay=relay, password=password,
        peer_name=pname, peer_tool=ptool,
        peer_signature=sig,
    )

    result = {"sent": msg}
    if relay and not relay_ok:
        result["warning"] = "Relay unreachable — message saved locally only"
        sys.stderr.write("⚠ Relay unreachable — message saved locally only\n")
    return result


def _interactive_chat(client, room_id, my_peer_id, my_name, my_tool,
                      relay, password, peer_secret=""):
    """Interactive chat REPL — WebSocket push with HTTP polling fallback."""
    last_ts = [None]
    running = [True]
    consecutive_errors = [0]
    ws_connected = [False]
    displayed_ids = set()  # Dedup WS push + HTTP poll messages

    def _display_msg(msg):
        """Display a message. Content must already be decrypted."""
        msg_id = msg.get("id")
        if msg_id and msg_id in displayed_ids:
            return
        if msg_id:
            displayed_ids.add(msg_id)
        if msg["peer_id"] == my_peer_id:
            return
        name = msg.get("peer_name", msg["peer_id"])
        tool = msg.get("peer_tool", "")
        tag = f" ({tool})" if tool else ""
        content = msg["content"]
        if msg.get("type") == "system":
            sys.stderr.write(f"\r  * {name} {content}\n")
        else:
            sys.stderr.write(f"\r[{name}{tag}]: {content}\n")
        sys.stderr.write("[You]: ")
        sys.stderr.flush()

    def _on_ws_message(data):
        if data.get("type") == "message" and data.get("data"):
            msg = data["data"]
            # WS delivers raw ciphertext — decrypt here (poll path decrypts in _read_messages)
            if password and msg.get("type") != "system":
                msg["content"] = decrypt(msg["content"], password, room_id, strict=True)
            _display_msg(msg)
            if msg.get("created_at"):
                last_ts[0] = msg["created_at"]

    def _on_ws_error(e):
        ws_connected[0] = False
        sys.stderr.write(f"\r⚠ WebSocket disconnected: {e}. Falling back to polling.\n")
        sys.stderr.write("[You]: ")
        sys.stderr.flush()

    # Try WebSocket
    ws_client = None
    if relay and relay.token:
        try:
            from .ws_client import connect_room_ws
            def _on_ws_close():
                ws_connected[0] = False

            ws_client = connect_room_ws(
                relay.base, room_id, relay.token,
                on_message=_on_ws_message, on_error=_on_ws_error,
            )
            ws_client.on_close = _on_ws_close
            ws_connected[0] = True
        except Exception:
            pass

    # Always fetch initial context regardless of WS status
    try:
        init_resp = _read_messages(client, room_id, relay, password, limit=20)
        for msg in init_resp.get("messages", []):
            _display_msg(msg)
            last_ts[0] = msg["created_at"]
    except Exception:
        pass

    def poll():
        base_interval = 2.0
        while running[0]:
            if ws_connected[0]:
                time.sleep(base_interval)
                continue
            try:
                resp = _read_messages(client, room_id, relay, password,
                                     since=last_ts[0], limit=20)
                for msg in resp.get("messages", []):
                    _display_msg(msg)
                    last_ts[0] = msg["created_at"]
                consecutive_errors[0] = 0
            except Exception as e:
                consecutive_errors[0] += 1
                if consecutive_errors[0] == 3:
                    sys.stderr.write(f"\r⚠ Connection issue: {e}. Retrying...\n")
                    sys.stderr.write("[You]: ")
                    sys.stderr.flush()
            # Exponential backoff on consecutive errors, reset on success
            if consecutive_errors[0] > 0:
                interval = min(base_interval * (2 ** consecutive_errors[0]), 60.0)
            else:
                interval = base_interval
            time.sleep(interval)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()

    mode_label = "encrypted " if password else ""
    ws_label = " [WebSocket]" if ws_connected[0] else " [polling]"
    relay_label = (" + relay" + ws_label) if relay else ""
    sys.stderr.write(f"Room {room_id} — {mode_label}interactive mode{relay_label}. Ctrl+C to exit.\n")
    sys.stderr.write("Commands: @<tool> <msg> = invite AI | /help | /who\n\n")

    try:
        while True:
            line = input("[You]: ")
            if not line.strip():
                continue

            if line.strip() == "/help":
                sys.stderr.write(
                    "  @codex <question>  — invite Codex to respond\n"
                    "  @opencode <question> — invite OpenCode\n"
                    "  @claude-code <question> — invite Claude Code\n"
                    "  /who  — show room participants\n"
                    "  /help — this help\n"
                    "  Ctrl+C — leave room\n"
                )
                continue
            if line.strip() == "/who":
                peers = _merge_peers(client, room_id, relay)
                for p in peers:
                    tag = f" ({p.get('tool', '')})" if p.get("tool") else ""
                    sys.stderr.write(f"  {p.get('name', '?')}{tag} [{p.get('type', '?')}]\n")
                continue

            if line.strip().startswith("@"):
                from .ops_ai import cmd_invite
                parts = line.strip().split(None, 1)
                tool = parts[0][1:]
                context = parts[1] if len(parts) > 1 else "Please share your thoughts."
                # Post the human's message to the room first
                human_msg = line.strip()
                sig = _peer_signature(peer_secret, room_id) if peer_secret else ""
                _post_message(
                    client, room_id, my_peer_id, human_msg,
                    relay=relay, password=password,
                    peer_name=my_name, peer_tool=my_tool,
                    peer_signature=sig,
                )
                sys.stderr.write(f"  ⏳ Inviting {tool}...\n")
                try:
                    result = cmd_invite(["--tool", tool, "--room", room_id, "--context", context])
                    # Don't print response here — WS/poll will display it via _display_msg
                    # with dedup. If neither has shown it yet, the next poll cycle will.
                except Exception as e:
                    sys.stderr.write(f"  ✗ Failed to invite {tool}: {e}\n")
            else:
                sig = _peer_signature(peer_secret, room_id) if peer_secret else ""
                _, relay_ok = _post_message(
                    client, room_id, my_peer_id, line.strip(),
                    relay=relay, password=password,
                    peer_name=my_name, peer_tool=my_tool,
                    peer_signature=sig,
                )
                if relay and not relay_ok:
                    sys.stderr.write("  ⚠ Relay unreachable — saved locally only\n")
    except (KeyboardInterrupt, EOFError):
        running[0] = False
        if ws_client:
            ws_client.close()
        sys.stderr.write("\nLeft the room.\n")
        return {"status": "exited"}
