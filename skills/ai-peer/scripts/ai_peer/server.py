"""AI Peer daemon — lightweight HTTP server with SQLite storage."""
import json
import logging
import os
import re
import signal
import sqlite3
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from .constants import PEERS_HOME, DB_PATH, PID_FILE, LOG_FILE, DEFAULT_PORT, DEFAULT_HOST, VERSION
from .db import PeerDB

logger = logging.getLogger("ai-peer")


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    db: PeerDB = None
    _server_ref = None  # Set by run_server for shutdown endpoint

    # (method, regex) → handler_name
    ROUTES = [
        ("GET",  r"/api/health$",                        "handle_health"),
        ("POST", r"/api/shutdown$",                      "handle_shutdown"),
        ("POST", r"/api/rooms$",                         "handle_create_room"),
        ("GET",  r"/api/rooms$",                         "handle_list_rooms"),
        ("GET",  r"/api/rooms/(?P<room_id>[^/]+)$",      "handle_get_room"),
        ("DELETE", r"/api/rooms/(?P<room_id>[^/]+)$",    "handle_delete_room"),
        ("POST", r"/api/rooms/(?P<room_id>[^/]+)/join$", "handle_join_room"),
        ("POST", r"/api/rooms/(?P<room_id>[^/]+)/leave$","handle_leave_room"),
        ("POST", r"/api/rooms/(?P<room_id>[^/]+)/messages$", "handle_send_message"),
        ("GET",  r"/api/rooms/(?P<room_id>[^/]+)/messages$", "handle_get_messages"),
        ("GET",  r"/api/rooms/(?P<room_id>[^/]+)/peers$",    "handle_room_peers"),
        ("POST", r"/api/peers$",                         "handle_register_peer"),
        ("GET",  r"/api/peers$",                         "handle_list_peers"),
    ]

    def _route(self, method):
        path = urlparse(self.path).path
        for m, pattern, handler_name in self.ROUTES:
            if m != method:
                continue
            match = re.fullmatch(pattern, path)
            if match:
                try:
                    return getattr(self, handler_name)(**match.groupdict())
                except sqlite3.IntegrityError as e:
                    return self._json({"error": f"Constraint violation: {e}"}, 400)
                except sqlite3.ProgrammingError:
                    return self._json({"error": "Database unavailable"}, 503)
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._json({"error": "Invalid Content-Length"}, 400)
            return None
        if length > 10 * 1024 * 1024:  # 10MB limit
            self._json({"error": "Request body too large"}, 413)
            return None
        if not length:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "Invalid JSON body"}, 400)
            return None
        if not isinstance(parsed, dict):
            self._json({"error": "Expected JSON object"}, 400)
            return None
        return parsed

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def log_message(self, fmt, *args):
        logger.info(fmt % args)

    # === Handlers ===

    def handle_health(self):
        self._json({"ok": True, "version": VERSION})

    def handle_shutdown(self):
        """Graceful shutdown via HTTP — cross-platform alternative to SIGTERM."""
        self._json({"ok": True, "message": "shutting down"})
        if self._server_ref:
            import threading
            threading.Thread(target=self._server_ref.shutdown, daemon=True).start()

    def handle_create_room(self):
        b = self._body()
        if b is None:
            return
        room = self.db.create_room(name=b.get("name", "unnamed"), mode=b.get("mode", "local"),
                                   relay_url=b.get("relay_url"), password=b.get("password"),
                                   room_id=b.get("room_id"), token=b.get("token"))
        self._json(room, 201)

    def handle_list_rooms(self):
        self._json({"rooms": self.db.list_rooms()})

    def handle_get_room(self, room_id):
        room = self.db.get_room(room_id)
        self._json(room if room else {"error": "room not found"}, 200 if room else 404)

    def handle_delete_room(self, room_id):
        self.db.delete_room(room_id)
        self._json({"ok": True})

    def handle_join_room(self, room_id):
        b = self._body()
        if b is None:
            return
        peer_id = b.get("peer_id")
        if not peer_id:
            return self._json({"error": "missing peer_id"}, 400)
        result = self.db.join_room(room_id, peer_id)
        if result.get("new_member"):
            self.db.add_message(room_id, peer_id, "joined the room", msg_type="system")
        self._json(result)

    def handle_leave_room(self, room_id):
        b = self._body()
        if b is None:
            return
        peer_id = b.get("peer_id")
        if not peer_id:
            return self._json({"error": "missing peer_id"}, 400)
        self.db.leave_room(room_id, peer_id)
        self.db.add_message(room_id, peer_id, "left the room", msg_type="system")
        self._json({"ok": True})

    def handle_send_message(self, room_id):
        b = self._body()
        if b is None:
            return
        if not b.get("peer_id") or not b.get("content") or not isinstance(b.get("content"), str):
            return self._json({"error": "missing peer_id or content"}, 400)
        msg = self.db.add_message(
            room_id=room_id, peer_id=b["peer_id"], content=b["content"],
            msg_type=b.get("type", "message"), metadata=b.get("metadata"),
        )
        self._json(msg, 201)

    def handle_get_messages(self, room_id):
        q = self._query()
        try:
            limit = max(1, min(int(q.get("limit", 50)), 10000))
        except (ValueError, TypeError):
            limit = 50
        msgs = self.db.get_messages(room_id, since=q.get("since"), limit=limit)
        self._json({"messages": msgs})

    def handle_room_peers(self, room_id):
        self._json({"peers": self.db.room_peers(room_id)})

    def handle_register_peer(self):
        b = self._body()
        if b is None:
            return
        if not b.get("name") or not b.get("type"):
            return self._json({"error": "missing name or type"}, 400)
        peer = self.db.register_peer(
            peer_id=b.get("id"), name=b["name"], peer_type=b["type"],
            tool=b.get("tool"), machine=b.get("machine"),
            host=b.get("host"), port=b.get("port"),
        )
        self._json(peer, 201)

    def handle_list_peers(self):
        self._json({"peers": self.db.list_peers()})


def run_server(host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Run the daemon (blocking). Called as subprocess."""
    PEERS_HOME.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)],
    )
    db = PeerDB(DB_PATH)
    Handler.db = db
    server = ThreadedServer((host, port), Handler)
    Handler._server_ref = server

    _shutdown_flag = [False]

    def shutdown(sig, frame):
        if _shutdown_flag[0]:
            return
        _shutdown_flag[0] = True
        logger.info("Shutting down...")
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    PID_FILE.write_text(str(os.getpid()))
    logger.info(f"AI Peer daemon on {host}:{port}")
    try:
        server.serve_forever()
    finally:
        db.close()
        PID_FILE.unlink(missing_ok=True)
        logger.info("Cleanup complete.")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args()
    run_server(args.host, args.port)
