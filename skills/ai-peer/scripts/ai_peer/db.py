"""SQLite database for ai-peer rooms, peers, and messages."""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT DEFAULT 'local' CHECK(mode IN ('local', 'lan', 'public')),
    relay_url TEXT,
    password TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('ai', 'human')),
    tool TEXT,
    machine TEXT,
    host TEXT,
    port INTEGER,
    last_seen TEXT,
    capabilities TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS room_peers (
    room_id TEXT REFERENCES rooms(id),
    peer_id TEXT REFERENCES peers(id),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (room_id, peer_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    peer_id TEXT NOT NULL,
    content TEXT NOT NULL,
    type TEXT DEFAULT 'message' CHECK(type IN ('message', 'system', 'invite')),
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_room_time ON messages(room_id, created_at);
"""

# Migration: add token column if missing (v0.6 upgrade)
MIGRATIONS = [
    "ALTER TABLE rooms ADD COLUMN token TEXT",
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(prefix=""):
    short = uuid.uuid4().hex[:12]
    return f"{prefix}{short}" if prefix else short


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


class PeerDB:
    def __init__(self, db_path: Path):
        import threading
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._run_migrations()
        self._lock = threading.Lock()

    def _run_migrations(self):
        for sql in MIGRATIONS:
            try:
                self.conn.execute(sql)
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

    def close(self):
        self.conn.close()

    # === Rooms ===

    _VALID_ROOM_ID = re.compile(r'^[a-zA-Z0-9_-]+$')

    def create_room(self, name, mode="local", relay_url=None, password=None, room_id=None, token=None):
        rid = room_id or _id("room-")
        if not self._VALID_ROOM_ID.match(rid):
            raise ValueError(f"Invalid room_id: must be alphanumeric/hyphen/underscore, got '{rid}'")
        with self._lock:
            # Use INSERT OR IGNORE + UPDATE to avoid REPLACE cascade-deleting FK children
            existing = self.conn.execute("SELECT id FROM rooms WHERE id=?", (rid,)).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE rooms SET name=?, mode=?, relay_url=?, password=?, token=? WHERE id=?",
                    (name, mode, relay_url, password, token, rid),
                )
            else:
                self.conn.execute(
                    "INSERT INTO rooms (id, name, mode, relay_url, password, token, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rid, name, mode, relay_url, password, token, _now()),
                )
            self.conn.commit()
        return _row_to_dict(self.conn.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone())

    def get_room(self, room_id):
        return _row_to_dict(self.conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone())

    def list_rooms(self):
        return [_row_to_dict(r) for r in self.conn.execute("SELECT * FROM rooms ORDER BY created_at DESC").fetchall()]

    def delete_room(self, room_id):
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE room_id=?", (room_id,))
            self.conn.execute("DELETE FROM room_peers WHERE room_id=?", (room_id,))
            self.conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
            self.conn.commit()

    # === Peers ===

    def register_peer(self, peer_id=None, name="unknown", peer_type="human",
                      tool=None, machine=None, host=None, port=None):
        pid = peer_id or _id("peer-")
        with self._lock:
            self.conn.execute(
                """INSERT INTO peers (id, name, type, tool, machine, host, port, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, type=excluded.type, tool=excluded.tool,
                       machine=excluded.machine, host=excluded.host, port=excluded.port,
                       last_seen=excluded.last_seen""",
                (pid, name, peer_type, tool, machine, host, port, _now()),
            )
            self.conn.commit()
        return _row_to_dict(self.conn.execute("SELECT * FROM peers WHERE id=?", (pid,)).fetchone())

    def get_peer(self, peer_id):
        return _row_to_dict(self.conn.execute("SELECT * FROM peers WHERE id=?", (peer_id,)).fetchone())

    def find_peer_by_tool(self, tool):
        return _row_to_dict(
            self.conn.execute("SELECT * FROM peers WHERE tool=? ORDER BY last_seen DESC", (tool,)).fetchone()
        )

    def list_peers(self):
        return [_row_to_dict(r) for r in self.conn.execute("SELECT * FROM peers ORDER BY last_seen DESC").fetchall()]

    def update_peer_seen(self, peer_id):
        with self._lock:
            self.conn.execute("UPDATE peers SET last_seen=? WHERE id=?", (_now(), peer_id))
            self.conn.commit()

    # === Room membership ===

    def join_room(self, room_id, peer_id):
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO room_peers (room_id, peer_id, joined_at) VALUES (?, ?, ?)",
                (room_id, peer_id, _now()),
            )
            new_member = cur.rowcount > 0
            self.conn.commit()
        return {"room_id": room_id, "peer_id": peer_id, "joined": True, "new_member": new_member}

    def leave_room(self, room_id, peer_id):
        with self._lock:
            self.conn.execute("DELETE FROM room_peers WHERE room_id=? AND peer_id=?", (room_id, peer_id))
            self.conn.commit()

    def room_peers(self, room_id):
        rows = self.conn.execute(
            """SELECT p.* FROM peers p
               JOIN room_peers rp ON p.id = rp.peer_id
               WHERE rp.room_id=?
               ORDER BY rp.joined_at""",
            (room_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # === Messages ===

    def add_message(self, room_id, peer_id, content, msg_type="message", metadata=None):
        mid = _id("msg-")
        with self._lock:
            self.conn.execute(
                """INSERT INTO messages (id, room_id, peer_id, content, type, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (mid, room_id, peer_id, content, msg_type, json.dumps(metadata or {}), _now()),
            )
            self.conn.commit()
        return self._enrich_message(mid)

    def get_messages(self, room_id, since=None, limit=50):
        if since:
            rows = self.conn.execute(
                """SELECT m.*, p.name as peer_name, p.tool as peer_tool, p.type as peer_type
                   FROM messages m LEFT JOIN peers p ON m.peer_id = p.id
                   WHERE m.room_id=? AND m.created_at>?
                   ORDER BY m.created_at ASC LIMIT ?""",
                (room_id, since, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT m.*, p.name as peer_name, p.tool as peer_tool, p.type as peer_type
                   FROM messages m LEFT JOIN peers p ON m.peer_id = p.id
                   WHERE m.room_id=?
                   ORDER BY m.created_at DESC LIMIT ?""",
                (room_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
        return [_row_to_dict(r) for r in rows]

    def _enrich_message(self, msg_id):
        row = self.conn.execute(
            """SELECT m.*, p.name as peer_name, p.tool as peer_tool, p.type as peer_type
               FROM messages m LEFT JOIN peers p ON m.peer_id = p.id
               WHERE m.id=?""",
            (msg_id,),
        ).fetchone()
        return _row_to_dict(row)
