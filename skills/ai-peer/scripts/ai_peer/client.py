"""HTTP client — CLI talks to daemon via this module."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .constants import DEFAULT_HOST, DEFAULT_PORT, LAN_HOST, DAEMON_CFG_FILE, PEERS_HOME, LOG_FILE


class PeerClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.base = f"http://{host}:{port}/api"

    def _req(self, method, path, data=None, timeout=30):
        url = f"{self.base}{path}"
        body = json.dumps(data, ensure_ascii=False).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"error": f"HTTP {e.code}"}
        except (urllib.error.URLError, OSError):
            return None  # Daemon not running or connection failed

    def is_alive(self):
        r = self._req("GET", "/health", timeout=3)
        return r is not None and r.get("ok")

    # === Rooms ===

    def create_room(self, name, mode="local", relay_url=None, password=None, room_id=None, token=None):
        data = {"name": name, "mode": mode}
        if relay_url:
            data["relay_url"] = relay_url
        if password:
            data["password"] = password
        if room_id:
            data["room_id"] = room_id
        if token:
            data["token"] = token
        return self._req("POST", "/rooms", data)

    def list_rooms(self):
        return self._req("GET", "/rooms")

    def get_room(self, room_id):
        return self._req("GET", f"/rooms/{room_id}")

    def delete_room(self, room_id):
        return self._req("DELETE", f"/rooms/{room_id}")

    def join_room(self, room_id, peer_id):
        return self._req("POST", f"/rooms/{room_id}/join", {"peer_id": peer_id})

    def leave_room(self, room_id, peer_id):
        return self._req("POST", f"/rooms/{room_id}/leave", {"peer_id": peer_id})

    # === Messages ===

    def send_message(self, room_id, peer_id, content, msg_type="message", metadata=None):
        return self._req("POST", f"/rooms/{room_id}/messages", {
            "peer_id": peer_id, "content": content,
            "type": msg_type, "metadata": metadata or {},
        })

    def get_messages(self, room_id, since=None, limit=50):
        params = f"?limit={limit}"
        if since:
            params += f"&since={urllib.parse.quote(since)}"
        return self._req("GET", f"/rooms/{room_id}/messages{params}")

    def room_peers(self, room_id):
        return self._req("GET", f"/rooms/{room_id}/peers")

    # === Peers ===

    def register_peer(self, peer_id, name, peer_type, tool=None, machine=None, host=None, port=None):
        return self._req("POST", "/peers", {
            "id": peer_id, "name": name, "type": peer_type,
            "tool": tool, "machine": machine, "host": host, "port": port,
        })

    def list_peers(self):
        return self._req("GET", "/peers")


def _save_daemon_cfg(host, port):
    """Persist daemon host:port so all commands know where to connect."""
    PEERS_HOME.mkdir(parents=True, exist_ok=True)
    DAEMON_CFG_FILE.write_text(json.dumps({"host": host, "port": port}), encoding="utf-8")


def _load_daemon_cfg():
    """Load persisted daemon config. Returns (host, port) or defaults."""
    if DAEMON_CFG_FILE.exists():
        try:
            cfg = json.loads(DAEMON_CFG_FILE.read_text(encoding="utf-8"))
            return cfg.get("host", DEFAULT_HOST), cfg.get("port", DEFAULT_PORT)
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_HOST, DEFAULT_PORT


def get_client():
    """Get a client connected to the currently running daemon."""
    host, port = _load_daemon_cfg()
    check_host = DEFAULT_HOST if host == LAN_HOST else host
    return PeerClient(check_host, port)


def ensure_daemon(host=None, port=None):
    """Start daemon if not running. Returns client.

    If host/port not specified, reads from persisted daemon config.
    If daemon is running on different host, stops and restarts.
    """
    # Use persisted config if no explicit host/port
    if host is None and port is None:
        host, port = _load_daemon_cfg()
    elif host is None:
        host = DEFAULT_HOST
    elif port is None:
        port = DEFAULT_PORT

    # Always health-check via loopback — 0.0.0.0 bind is reachable on 127.0.0.1
    check_host = DEFAULT_HOST if host == LAN_HOST else host
    client = PeerClient(check_host, port)

    if client.is_alive():
        # Check if running daemon matches requested config
        cfg_host, cfg_port = _load_daemon_cfg()
        if host != cfg_host or port != cfg_port:
            # Need to restart with new config
            sys.stderr.write(f"⟳ Restarting daemon ({cfg_host}:{cfg_port} → {host}:{port})...\n")
            _stop_daemon()
            time.sleep(0.5)
        else:
            return client

    # Start daemon
    return _start_daemon(host, port)


def _stop_daemon():
    """Stop the running daemon. Uses HTTP shutdown endpoint (cross-platform),
    falls back to SIGTERM/SIGBREAK for older daemons."""
    from .constants import PID_FILE

    # Try HTTP shutdown first (works on all platforms including Windows)
    host, port = _load_daemon_cfg()
    check_host = DEFAULT_HOST if host == LAN_HOST else host
    try:
        req = urllib.request.Request(
            f"http://{check_host}:{port}/api/shutdown", method="POST"
        )
        urllib.request.urlopen(req, timeout=3)
        # Give server time to shut down gracefully
        import time as _time
        _time.sleep(0.5)
    except (urllib.error.URLError, OSError):
        # HTTP shutdown failed — fall back to signal
        if PID_FILE.exists():
            try:
                import signal as _signal
                pid = int(PID_FILE.read_text().strip())
                sig = getattr(_signal, "SIGTERM", getattr(_signal, "SIGBREAK", 15))
                os.kill(pid, sig)
            except (ValueError, ProcessLookupError, OSError):
                pass

    PID_FILE.unlink(missing_ok=True)


def _start_daemon(host, port):
    """Start daemon subprocess. Returns client."""
    import subprocess
    from pathlib import Path

    PEERS_HOME.mkdir(parents=True, exist_ok=True)

    scripts_dir = str(Path(__file__).resolve().parent.parent)
    existing_pypath = os.environ.get("PYTHONPATH", "")
    pypath = scripts_dir + os.pathsep + existing_pypath if existing_pypath else scripts_dir

    with open(LOG_FILE, "a") as log_out:
        popen_kwargs = {
            "stdout": log_out,
            "stderr": log_out,
            "cwd": str(PEERS_HOME),
            "env": {**os.environ, "PYTHONPATH": pypath},
        }
        if sys.platform == "win32":
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            DETACHED_PROCESS = 0x00000008
            popen_kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, "-m", "ai_peer.server", "--host", host, "--port", str(port)],
            **popen_kwargs,
        )

    # Save config so other commands find the daemon
    _save_daemon_cfg(host, port)

    # Wait for startup
    check_host = DEFAULT_HOST if host == LAN_HOST else host
    client = PeerClient(check_host, port)
    for _ in range(20):
        time.sleep(0.2)
        if client.is_alive():
            return client

    # Read last few lines of log for diagnostics
    diag = ""
    try:
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-20:] if len(lines) > 20 else lines
            diag = "\n  ".join(tail)
    except OSError:
        pass
    msg = "Failed to start ai-peer daemon"
    if diag:
        msg += f"\n\nDaemon log (last lines):\n  {diag}"
    raise RuntimeError(msg)
