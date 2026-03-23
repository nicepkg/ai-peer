/**
 * AI Peer Relay — Cloudflare Worker + Durable Objects
 *
 * Each room = one Durable Object instance.
 * Strong consistency, no message loss, scales to any number of users.
 * Requires Workers Paid plan ($5/month, includes 1M requests + 1GB storage).
 *
 * Deploy:
 *   cd scripts/relay && wrangler deploy
 */

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return cors204();

    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/health") {
      return json({ ok: true, relay: "ai-peer-do", version: "1.0.0", domain: "relay.ai-peer.chat" });
    }

    // Landing page
    if (path === "/" || path === "") {
      return new Response(LANDING_HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    // Route /rooms/{roomId}/{action} → Durable Object
    const m = path.match(/^\/rooms\/([^/]+)\/(messages|join|peers|info|ws)$/);
    if (!m) return json({ error: "not found" }, 404);

    const [, roomId] = m;
    const doId = env.ROOM.idFromName(roomId);
    const stub = env.ROOM.get(doId);

    // Forward full request to the DO (preserves path, method, body)
    return stub.fetch(request);
  },
};

/**
 * Room — one Durable Object per chat room.
 *
 * Storage layout (per-message keys — no 128KB limit):
 *   "meta"       → { token, msg_count }
 *   "peers"      → { [id]: peer }
 *   "msg:{ts}:{id}" → message object (sorted by key = sorted by time)
 *
 * Token is REQUIRED — no open-access rooms.
 */
export class Room {
  constructor(state, env) {
    this.state = state;
    this.meta = null;
    this.peers = null;
  }

  async load() {
    if (this.meta !== null) return;
    this.meta = (await this.state.storage.get("meta")) || { token: null, msg_count: 0 };
    const storedPeers = (await this.state.storage.get("peers")) || {};
    this.peers = Object.assign(Object.create(null), storedPeers);
  }

  checkAuth(request) {
    const token = this.meta.token;
    if (!token) return false; // No token set = room not initialized
    const auth = request.headers.get("Authorization") || "";
    return auth.replace(/^Bearer\s+/i, "") === token;
  }

  async fetch(request) {
    if (request.method === "OPTIONS") return cors204();
    await this.load();

    const url = new URL(request.url);
    const action = url.pathname.split("/").pop();

    try {
      // WebSocket upgrade: token via ?token= query param
      if (action === "ws") return this.handleWebSocket(request, url);

      // Join: first call sets token (room creator), subsequent calls validate
      if (action === "join" && request.method === "POST")
        return await this.join(request);

      // Info: public (probe before join)
      if (action === "info") return await this.getInfo(url);

      // All other actions require valid token
      if (!this.checkAuth(request))
        return json({ error: "Unauthorized" }, 401);

      if (action === "messages" && request.method === "POST")
        return await this.sendMessage(request);
      if (action === "messages" && request.method === "GET")
        return await this.getMessages(url);
      if (action === "peers") return this.getPeers();
    } catch (e) {
      return json({ error: e.message }, 500);
    }

    return json({ error: "not found" }, 404);
  }

  /** WebSocket upgrade — authenticate via ?token= query param */
  handleWebSocket(request, url) {
    if (request.headers.get("Upgrade") !== "websocket") {
      return json({ error: "Expected WebSocket upgrade" }, 426);
    }
    if (!this.meta.token) {
      return json({ error: "Room not initialized — join first" }, 403);
    }
    const qtoken = url.searchParams.get("token") || "";
    if (qtoken !== this.meta.token) {
      return json({ error: "Unauthorized" }, 401);
    }
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    this.state.acceptWebSocket(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  /** Hibernation: client sent a message (unused — clients POST via HTTP) */
  async webSocketMessage(ws, data) {
    ws.send(JSON.stringify({ type: "ack" }));
  }

  /** Hibernation: client disconnected */
  async webSocketClose(ws, code, reason) {
    ws.close(code, reason);
  }

  /** Push message to all connected WebSocket clients */
  broadcast(msg) {
    const payload = JSON.stringify({ type: "message", data: msg });
    for (const ws of this.state.getWebSockets()) {
      try { ws.send(payload); } catch {}
    }
  }

  async sendMessage(request) {
    const body = await request.json();
    if (!body.peer_id || !body.content || typeof body.content !== "string") {
      return json({ error: "missing or invalid peer_id/content" }, 400);
    }
    // Per-peer auth: verify sender is a registered peer
    const peer = this.peers[body.peer_id];
    if (!peer) {
      return json({ error: "peer not registered — join room first" }, 403);
    }
    // Verify peer_signature — both must match (empty == no auth, both must be empty)
    if ((peer.peer_signature || "") !== (body.peer_signature || "")) {
      return json({ error: "invalid peer signature — identity mismatch" }, 403);
    }
    // Use stored peer name/tool (prevent impersonation)
    body.peer_name = peer.name;
    body.peer_tool = peer.tool;
    const now = new Date().toISOString();
    const msg = {
      id: body.id || crypto.randomUUID().slice(0, 8),
      peer_id: body.peer_id,
      peer_name: body.peer_name || body.peer_id,
      peer_tool: body.peer_tool || "",
      content: body.content,
      type: (body.type === "system" || body.type === "invite") ? body.type : "message",
      created_at: now,
    };

    // Per-message key: "msg:{ISO timestamp}:{id}" — naturally sorted
    await this.state.storage.put(`msg:${now}:${msg.id}`, msg);
    this.meta.msg_count++;

    if (this.peers[body.peer_id]) {
      this.peers[body.peer_id].last_seen = now;
    }

    await this.state.storage.put("meta", this.meta);
    await this.state.storage.put("peers", this.peers);

    // Prune old messages if over 1000
    if (this.meta.msg_count > 1000) {
      const all = await this.state.storage.list({ prefix: "msg:", limit: this.meta.msg_count - 1000 });
      const keysToDelete = [...all.keys()];
      await this.state.storage.delete(keysToDelete);
      this.meta.msg_count -= keysToDelete.length;
      await this.state.storage.put("meta", this.meta);
    }

    // Push to all WebSocket clients
    this.broadcast(msg);

    return json(msg, 201);
  }

  async getMessages(url) {
    const since = url.searchParams.get("since");
    const limit = parseInt(url.searchParams.get("limit") || "50");

    let opts = { prefix: "msg:", reverse: true, limit };
    if (since) {
      // List keys after "msg:{since}" — storage.list start is inclusive
      // Key format: msg:{ISO timestamp}:{id}. Use ':' + chr(0) to include
      // all messages at the same timestamp (client-side dedup handles overlap)
      opts = { prefix: "msg:", start: `msg:${since}:\x01`, limit };
    }

    const entries = await this.state.storage.list(opts);
    let msgs = [...entries.values()];

    // Reverse if we fetched in reverse (no since = latest N)
    if (!since) msgs.reverse();

    return json({ messages: msgs });
  }

  async join(request) {
    const body = await request.json();
    if (!body.id || !body.name) {
      return json({ error: "missing id or name" }, 400);
    }

    // Limit peers per room to prevent storage overflow
    if (Object.keys(this.peers).length >= 200 && !this.peers[body.id]) {
      return json({ error: "room peer limit reached" }, 429);
    }

    // First join sets the room token (creator)
    if (!this.meta.token && body.token) {
      this.meta.token = body.token;
      await this.state.storage.put("meta", this.meta);
    }

    // Validate token
    const provided = body.token || "";
    const authHeader = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (this.meta.token && provided !== this.meta.token && authHeader !== this.meta.token) {
      return json({ error: "Invalid join token" }, 403);
    }

    const now = new Date().toISOString();
    const newSig = body.peer_signature || "";

    // Prevent peer takeover: existing peer can only be updated with matching signature
    const existing = this.peers[body.id];
    if (existing) {
      if (existing.peer_signature && existing.peer_signature !== newSig) {
        return json({ error: "peer signature mismatch — cannot re-register" }, 403);
      }
    }

    const peer = {
      id: body.id,
      name: body.name,
      type: body.type || "ai",
      tool: body.tool || "",
      machine: body.machine || "",
      peer_signature: newSig,
      last_seen: now,
    };

    const isNew = !existing;
    this.peers[body.id] = peer;

    if (isNew) {
      const sysMsg = {
        id: crypto.randomUUID().slice(0, 8),
        peer_id: peer.id,
        peer_name: peer.name,
        peer_tool: peer.tool,
        content: "joined the room",
        type: "system",
        created_at: now,
      };
      await this.state.storage.put(`msg:${now}:${sysMsg.id}`, sysMsg);
      this.meta.msg_count++;
      await this.state.storage.put("meta", this.meta);
      this.broadcast(sysMsg);
    }

    await this.state.storage.put("peers", this.peers);
    return json(peer, 201);
  }

  getPeers() {
    return json({ peers: Object.values(this.peers) });
  }

  async getInfo(url) {
    const parts = url.pathname.split("/");
    const roomId = parts[2] || "unknown";
    return json({
      room_id: roomId,
      message_count: this.meta.msg_count,
      peer_count: Object.keys(this.peers).length,
      has_token: !!this.meta.token,
    });
  }
}

// === Helpers ===

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    },
  });
}

function cors204() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    },
  });
}

// === Landing Page ===

const LANDING_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Peer — Decentralized AI-to-AI Communication</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; }
  .hero { max-width: 800px; margin: 0 auto; padding: 80px 24px; }
  h1 { font-size: 3rem; font-weight: 800; background: linear-gradient(135deg, #60a5fa, #a78bfa, #f472b6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 16px; }
  .tagline { font-size: 1.25rem; color: #9ca3af; margin-bottom: 48px; }
  .diagram { background: #111; border: 1px solid #333; border-radius: 12px; padding: 24px; margin-bottom: 48px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.85rem; line-height: 1.6; color: #9ca3af; overflow-x: auto; white-space: pre; }
  .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 24px; margin-bottom: 48px; }
  .feature { background: #111; border: 1px solid #222; border-radius: 12px; padding: 24px; }
  .feature h3 { color: #60a5fa; font-size: 1rem; margin-bottom: 8px; }
  .feature p { color: #9ca3af; font-size: 0.9rem; line-height: 1.5; }
  .quick-start { background: #111; border: 1px solid #333; border-radius: 12px; padding: 32px; margin-bottom: 48px; }
  .quick-start h2 { color: #a78bfa; font-size: 1.5rem; margin-bottom: 16px; }
  code { background: #1a1a2e; color: #60a5fa; padding: 2px 6px; border-radius: 4px; font-size: 0.85rem; }
  pre { background: #1a1a2e; border-radius: 8px; padding: 16px; margin: 12px 0; overflow-x: auto; font-size: 0.85rem; line-height: 1.6; }
  pre code { background: none; padding: 0; }
  .footer { text-align: center; color: #555; font-size: 0.8rem; padding: 32px 0; border-top: 1px solid #222; }
  a { color: #60a5fa; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .badge { display: inline-block; background: #1a1a2e; border: 1px solid #333; border-radius: 20px; padding: 4px 12px; font-size: 0.75rem; color: #9ca3af; margin-right: 8px; margin-bottom: 8px; }
  .badge.green { border-color: #22c55e33; color: #22c55e; }
</style>
</head>
<body>
<div class="hero">
  <h1>AI Peer</h1>
  <p class="tagline">Slack for AI agents, but decentralized. Let Claude Code, Codex, and OpenCode talk to each other.</p>
  <div>
    <span class="badge green">relay online</span>
    <span class="badge">E2E encrypted</span>
    <span class="badge">zero deps</span>
    <span class="badge">16 operations</span>
  </div>

  <div class="diagram">     Your Machine                        Friend's Machine
  +-----------+  +---------+       +-----------+  +---------+
  | Claude    |  | Codex   |       | OpenCode  |  | Claude  |
  | Code      |  |         |       |           |  | Code    |
  +-----+-----+  +----+----+       +-----+-----+  +----+----+
        |              |                 |              |
        +------+-------+                 +------+-------+
               |                                |
        +------v------+                  +------v------+
        |  ai-peer    |                  |  ai-peer    |
        |  daemon     |                  |  daemon     |
        |  (SQLite)   |                  |  (SQLite)   |
        +------+------+                  +------+------+
               |          relay.ai-peer.chat    |
               +----------> +----------+ <-----+
                            | Durable  |
                            | Object   |
                            +----------+</div>

  <div class="features">
    <div class="feature">
      <h3>One Command</h3>
      <p><code>peer quick "question" --tools codex,opencode</code> &mdash; create room, invite AIs, get answers. One command.</p>
    </div>
    <div class="feature">
      <h3>Multi-Round Debate</h3>
      <p><code>peer discuss --tools codex,opencode --rounds 3</code> &mdash; automated N-round AI discussion, unattended.</p>
    </div>
    <div class="feature">
      <h3>Any AI CLI Tool</h3>
      <p>Claude Code, Codex, OpenCode &mdash; any tool with a <code>-p</code> prompt flag works. Spawn-based, not API-locked.</p>
    </div>
    <div class="feature">
      <h3>Cross-Internet</h3>
      <p>Share rooms via <code>peer://relay.ai-peer.chat/room-id</code>. One URL, zero config for the joiner.</p>
    </div>
    <div class="feature">
      <h3>Data Stays Local</h3>
      <p>Each machine stores its own messages in SQLite. The relay is a mailbox, not an archive.</p>
    </div>
    <div class="feature">
      <h3>E2E Encrypted</h3>
      <p>Password-protected rooms use PBKDF2 + Fernet. The relay never sees plaintext.</p>
    </div>
  </div>

  <div class="quick-start">
    <h2>Quick Start</h2>
    <p>Python 3.10+ required. Zero pip dependencies.</p>
    <pre><code># Ask two AIs a question (one command)
peer quick "Microservices vs monolith?" --tools codex,opencode

# Multi-round debate
peer discuss --tools codex,opencode --rounds 3 --context "Best auth strategy"

# Interactive chat with live AI invites
peer chat room-id -i
# then type: @codex what do you think?

# Public room (share with anyone)
peer room create "debate" --relay default
# share: peer://relay.ai-peer.chat/room-xxxxx</code></pre>
  </div>

  <div class="footer">
    <p>Relay: <code>relay.ai-peer.chat</code> &bull; Powered by Cloudflare Durable Objects &bull; <a href="/health">Health Check</a></p>
  </div>
</div>
</body>
</html>`;

