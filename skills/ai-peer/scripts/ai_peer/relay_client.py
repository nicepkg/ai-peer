"""HTTP client for the ai-peer relay (Cloudflare Worker)."""
import json
import urllib.error
import urllib.parse
import urllib.request

from .constants import VERSION


class RelayClient:
    """Talks to the public relay for cross-internet rooms."""

    def __init__(self, relay_url, token=None):
        self.base = relay_url.rstrip("/")
        self.token = token  # Auth token for this room

    def _req(self, method, path, data=None, timeout=15):
        url = f"{self.base}{path}"
        body = json.dumps(data, ensure_ascii=False).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", f"ai-peer/{VERSION}")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
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
            return None

    def is_alive(self):
        r = self._req("GET", "/health", timeout=5)
        return r is not None and r.get("ok")

    def join_room(self, room_id, peer_id, name, peer_type="ai", tool="", machine="", peer_signature=""):
        data = {
            "id": peer_id, "name": name, "type": peer_type,
            "tool": tool, "machine": machine,
        }
        if self.token:
            data["token"] = self.token
        if peer_signature:
            data["peer_signature"] = peer_signature
        return self._req("POST", f"/rooms/{room_id}/join", data)

    def send_message(self, room_id, peer_id, content, peer_name="", peer_tool="",
                     msg_type="message", msg_id=None, peer_signature=""):
        data = {
            "peer_id": peer_id, "peer_name": peer_name, "peer_tool": peer_tool,
            "content": content, "type": msg_type,
        }
        if msg_id:
            data["id"] = msg_id
        if peer_signature:
            data["peer_signature"] = peer_signature
        return self._req("POST", f"/rooms/{room_id}/messages", data)

    def get_messages(self, room_id, since=None, limit=50):
        params = f"?limit={limit}"
        if since:
            params += f"&since={urllib.parse.quote(since)}"
        return self._req("GET", f"/rooms/{room_id}/messages{params}")

    def get_peers(self, room_id):
        return self._req("GET", f"/rooms/{room_id}/peers")

    def room_info(self, room_id):
        return self._req("GET", f"/rooms/{room_id}/info")
