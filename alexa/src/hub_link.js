// HubLink: one hibernating Durable Object. The hub keeps an outbound WebSocket
// to wss://<worker>/hub. Alexa requests are pushed down that socket and the
// matching answer comes back on it. No inbound port on the home network.
//
// verify.js is not involved here. Signature, timestamp and skill-id checks
// stay in the Alexa POST path.

export const HUB_OBJECT_NAME = "hub";
export const PROGRESSIVE_MS = 1200;
export const CUTOFF_MS = 6500;
export const STALE_MS = 30 * 1000;
export const MAX_SPEECH_CHARS = 6000;
export const PROGRESSIVE_LINE = "One moment…";
export const FRAME_LINE = "That's taking a while. I'll put it on the kitchen frame.";
export const DEFAULT_REPROMPT = "Anything else?";
const DISPATCH_GRACE_MS = 8000;

const OPEN = 1;

export function bearerToken(header) {
  if (!header || typeof header !== "string") return "";
  const m = /^Bearer ([^\s]+)\s*$/i.exec(header);
  return m ? m[1] : "";
}

// SHA-256 both sides, then compare the digests. Empty expected never matches,
// including empty-against-empty. Length of the secret does not change the loop.
export async function tokensEqual(provided, expected) {
  const enc = new TextEncoder();
  const a = typeof provided === "string" ? provided : "";
  const b = typeof expected === "string" ? expected : "";
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const x = new Uint8Array(da);
  const y = new Uint8Array(db);
  let diff = x.length ^ y.length;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0 && b.length > 0;
}

export function originAllowed(request) {
  const origin = request.headers.get("Origin");
  if (!origin) return true; // non-browser hub client (aiohttp sends none)
  let page;
  try { page = new URL(request.url); } catch { return false; }
  return origin === `https://${page.host}`;
}

// Public /hub upgrade only. Returns a Response to send back, or null to accept.
export async function authorizeHub(request, expectedToken) {
  const url = new URL(request.url);
  if (url.pathname !== "/hub") return new Response("not found", { status: 404 });
  const upgrade = request.headers.get("Upgrade");
  if (request.method !== "GET" || !upgrade || upgrade.toLowerCase() !== "websocket")
    return new Response("not found", { status: 404 });
  if (!originAllowed(request)) return new Response("forbidden", { status: 403 });
  const ok = await tokensEqual(bearerToken(request.headers.get("Authorization")), expectedToken);
  if (!ok) return new Response("unauthorized", { status: 401 });
  return null;
}

export function plainSpeech(text) {
  let s = String(text ?? "").replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, " ");
  s = s.replace(/\s+/g, " ").trim();
  if (s.length > MAX_SPEECH_CHARS) s = s.slice(0, MAX_SPEECH_CHARS - 1).trimEnd() + "…";
  return s;
}

export function escapeSsml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

// Plain words inside <speak>, escaped, and well under the 8000-character cap
// even after the entities expand.
export function toSsml(text) {
  let s = escapeSsml(plainSpeech(text));
  const budget = 7000 - "<speak></speak>".length;
  if (s.length > budget) s = s.slice(0, budget - 1) + "…";
  return `<speak>${s}</speak>`;
}

export function hubStub(ns) {
  if (!ns) return null;
  if (typeof ns.getByName === "function") return ns.getByName(HUB_OBJECT_NAME);
  if (typeof ns.idFromName === "function" && typeof ns.get === "function")
    return ns.get(ns.idFromName(HUB_OBJECT_NAME));
  return null;
}

function autoPair() {
  const Pair = globalThis.WebSocketRequestResponsePair;
  if (typeof Pair === "function") return new Pair("ping", "pong");
  return { request: "ping", response: "pong" };
}

export class HubLink {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env || {};
    this.pending = new Map();
    // Text ping/pong is answered by the runtime, so a heartbeat does not wake the object.
    try { ctx?.setWebSocketAutoResponse?.(autoPair()); } catch { /* tests without the runtime */ }
  }

  freshSocket(now = Date.now()) {
    let sockets = [];
    try { sockets = this.ctx.getWebSockets("hub") || []; } catch { sockets = []; }
    const open = sockets.filter((ws) => ws && ws.readyState === OPEN);
    const ws = open[open.length - 1];
    if (!ws) return null;
    let last = 0;
    try {
      const auto = this.ctx.getWebSocketAutoResponseTimestamp?.(ws);
      if (auto && typeof auto.getTime === "function") last = Math.max(last, auto.getTime());
    } catch { /* no auto-response clock */ }
    try {
      const att = ws.deserializeAttachment?.() || {};
      last = Math.max(last, att.connectedAt || 0, att.lastApp || 0);
    } catch { /* attachment missing */ }
    if (!last || now - last > STALE_MS) return null;
    return ws;
  }

  _attach(server) {
    let existing = [];
    try { existing = this.ctx.getWebSockets("hub") || []; } catch { existing = []; }
    for (const ws of existing) {
      try { ws.close(4000, "replaced"); } catch { /* already closing */ }
    }
    this.ctx.acceptWebSocket(server, ["hub"]);
    try { server.serializeAttachment?.({ connectedAt: Date.now() }); } catch { /* ignore */ }
  }

  async fetch(request) {
    const url = new URL(request.url);
    const upgrade = request.headers.get("Upgrade");
    if (upgrade && upgrade.toLowerCase() === "websocket") {
      const denied = await authorizeHub(request, this.env.HUB_LINK_TOKEN);
      if (denied) return denied;
      return this._upgrade(request);
    }
    // Internal only: the worker calls this. The public route never forwards POST.
    if (request.method === "POST" && url.pathname === "/dispatch") return this._dispatch(request);
    if (request.method === "POST" && url.pathname === "/media") return this._media(request);
    if (request.method === "POST" && url.pathname === "/reqlog") return this._reqlogAdd(request);
    if (request.method === "GET" && url.pathname === "/reqlog") return this._reqlogRead();
    return new Response("not found", { status: 404 });
  }

  async _reqlogAdd(request) {
    let entry;
    try { entry = await request.json(); } catch { return new Response("bad", { status: 400 }); }
    if (!entry || typeof entry !== "object" || JSON.stringify(entry).length > 4096) return new Response("bad", { status: 400 });
    const { appendReqlog } = await import("./reqlog.js");
    return Response.json({ ok: true, count: await appendReqlog(this.ctx.storage, entry) });
  }

  async _reqlogRead() {
    const { mediaSummary } = await import("./reqlog.js");
    const requests = (await this.ctx.storage.get("reqlog")) || [];
    return Response.json({ requests, media: await mediaSummary(this.ctx.storage) });
  }

  // Media state (M2): resume offsets, last play per device, episode URLs.
  // Keys must start with "media:"; only the worker reaches this (the public /hub route never maps to /media).
  async _media(request) {
    let req;
    try { req = await request.json(); } catch { return new Response("bad", { status: 400 }); }
    const store = this.ctx?.storage;
    if (!store) return new Response("no storage", { status: 503 });
    const okKey = (k) => typeof k === "string" && k.startsWith("media:") && k.length <= 128;
    const okKeys = (ks) => Array.isArray(ks) && ks.length > 0 && ks.length <= 32 && ks.every(okKey);
    if (req?.op === "get" && okKeys(req.keys)) {
      const got = await store.get(req.keys);
      const values = {};
      for (const k of req.keys) values[k] = (got instanceof Map ? got.get(k) : got?.[k]) ?? null;
      return Response.json({ values });
    }
    if (req?.op === "put" && req.values && typeof req.values === "object" && okKeys(Object.keys(req.values))) {
      await store.put(req.values);
      return Response.json({ ok: true });
    }
    if (req?.op === "delete" && okKeys(req.keys)) {
      await store.delete(req.keys);
      return Response.json({ ok: true });
    }
    return new Response("bad", { status: 400 });
  }

  _upgrade(request) {
    const Pair = globalThis.WebSocketPair;
    if (typeof Pair !== "function") return new Response("websocket unavailable", { status: 500 });
    const pair = new Pair();
    const [client, server] = Object.values(pair);
    this._attach(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  async _dispatch(request) {
    let job;
    try { job = await request.json(); } catch { return Response.json({ offline: true }); }
    const id = job && job.id;
    if (!id) return Response.json({ offline: true });
    const ws = this.freshSocket();
    if (!ws) return Response.json({ offline: true });
    const payload = {
      id,
      text: job.text || job.question || "",
      intent: job.intent ?? null,
      deviceId: job.deviceId ?? null,
      sessionId: job.sessionId ?? null,
      locale: job.locale ?? null,
      deadline: job.deadline ?? null,
    };
    const waitMs = Math.min(
      DISPATCH_GRACE_MS,
      Math.max(50, (typeof payload.deadline === "number" ? payload.deadline - Date.now() : DISPATCH_GRACE_MS) + 500),
    );
    const body = await new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ timeout: true });
      }, waitMs);
      this.pending.set(id, {
        resolve: (msg) => {
          clearTimeout(timer);
          this.pending.delete(id);
          resolve(msg);
        },
      });
      try {
        ws.send(JSON.stringify(payload));
      } catch {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve({ offline: true });
      }
    });
    return Response.json(body);
  }

  async webSocketMessage(ws, message) {
    const text = typeof message === "string" ? message : new TextDecoder().decode(message);
    if (text === "ping" || text === "pong") return; // auto-response covers ping
    let msg;
    try { msg = JSON.parse(text); } catch { return; }
    if (!msg || typeof msg !== "object") return;
    try {
      const att = ws.deserializeAttachment?.() || {};
      ws.serializeAttachment?.({ ...att, lastApp: Date.now() });
    } catch { /* attachment is optional */ }
    const waiter = msg.id ? this.pending.get(msg.id) : null;
    if (!waiter) return;
    waiter.resolve({
      id: msg.id,
      speech: msg.speech ?? "",
      end_session: msg.end_session !== false,
      reprompt: msg.reprompt,
    });
  }

  async webSocketClose() {}

  async webSocketError() {}
}

// Worker entry for GET wss://<worker>/hub. Auth runs here, before the DO, and again on the DO.
export async function routeHub(request, env) {
  const denied = await authorizeHub(request, env?.HUB_LINK_TOKEN);
  if (denied) return denied;
  const stub = hubStub(env?.HUB_LINK);
  if (!stub) return new Response("unavailable", { status: 503 });
  return stub.fetch(request);
}
