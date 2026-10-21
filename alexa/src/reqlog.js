// Request log: ring of the last REQLOG_MAX verified Alexa requests, kept in
// the HubLink DO. Read with GET /admin/requests and the hub bearer
// (HUB_LINK_TOKEN). Entries hold a device hash, never raw ids, tokens or URLs
// (stream host only).
import { bearerToken, hubStub, tokensEqual } from "./hub_link.js";
import { parseToken } from "./media/player.js";

export const REQLOG_MAX = 50;
const clip = (v, n = 120) => (v == null || v === "" ? null
  : String(v).replace(/https?:\/\/\S+/gi, "<url>").replace(/amzn1\.[\w.-]+/gi, "<id>").replace(/\s+/g, " ").trim().slice(0, n) || null);

async function hash8(s) {
  if (!s) return null;
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(s)));
  return [...new Uint8Array(d)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function slotValues(intent) {
  const slots = intent?.slots;
  if (!slots || typeof slots !== "object") return null;
  const out = {};
  const list = Array.isArray(slots) ? slots.map((x) => [x?.name, x]) : Object.entries(slots).map(([k, x]) => [x?.name || k, x]);
  for (const [name, s] of list) {
    if (!name || !s) continue;
    const v = s.value ?? s.slotValue?.value;
    const er = s.resolutions?.resolutionsPerAuthority?.find((a) => a?.status?.code === "ER_SUCCESS_MATCH")?.values?.[0]?.value?.id;
    if (v != null || er) out[clip(name, 32)] = er ? `${clip(v, 80)} => ${clip(er, 40)}` : clip(v, 80);
  }
  return Object.keys(out).length ? out : null;
}

function host(url) {
  try { return new URL(url).host; } catch { return null; }
}

// body: parsed Alexa request; out: the JSON sent back; trace: filled by the handler.
export async function buildEntry(body, out, trace = {}) {
  const req = body?.request || {};
  const resp = out?.response || {};
  const d = resp.directives?.[0];
  let reply = "empty";
  let streamHost = null;
  let entry = null;
  let offset = null;
  if (d?.type === "AudioPlayer.Play") {
    reply = "AudioPlayer.Play";
    streamHost = host(d.audioItem?.stream?.url);
    entry = parseToken(d.audioItem?.stream?.token)?.entryId || null;
    offset = d.audioItem?.stream?.offsetInMilliseconds ?? null;
  } else if (d?.type) reply = d.type;
  else if (trace.forwarded) reply = `hub:${trace.forwarded}`;
  else if (resp.outputSpeech) reply = "speech";
  const evTok = parseToken(req.token ?? body?.context?.AudioPlayer?.token);
  const err = req.error && typeof req.error === "object" ? req.error : null;
  const isEvent = /^(AudioPlayer|PlaybackController)\./.test(req.type || "");
  return {
    at: new Date().toISOString(),
    type: clip(req.type, 60),
    intent: clip(req.intent?.name, 60),
    slots: slotValues(req.intent),
    text: clip(trace.text, 120),
    entry: entry || (isEvent ? evTok?.entryId : null) || trace.entry || null,
    reply,
    speech: clip((resp.outputSpeech?.ssml || "").replace(/<[^>]+>/g, ""), 100),
    stream_host: streamHost,
    play_offset_ms: offset,
    event_offset_ms: isEvent ? (req.offsetInMilliseconds ?? null) : null,
    player: clip(body?.context?.AudioPlayer?.playerActivity, 20),
    error: err ? `${clip(err.type, 60)}: ${clip(err.message, 120)}` : clip(trace.error, 160),
    reason: clip(req.reason, 40),
    device: await hash8(body?.context?.System?.device?.deviceId),
  };
}

export async function recordEntry(ns, entry) {
  const stub = hubStub(ns);
  if (!stub) return;
  try {
    await stub.fetch("https://hub.internal/reqlog", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(entry),
    });
  } catch { /* logging must not break a reply */ }
}

// DO side.
export async function appendReqlog(storage, entry) {
  const ring = (await storage.get("reqlog")) || [];
  ring.push(entry);
  while (ring.length > REQLOG_MAX) ring.shift();
  await storage.put("reqlog", ring);
  return ring.length;
}

export async function mediaSummary(storage) {
  let all;
  try { all = await storage.list({ prefix: "media:" }); } catch { return null; }
  const rows = all instanceof Map ? [...all] : Object.entries(all || {});
  const out = { episodes: 0, resume: [], last: [] };
  for (const [k, v] of rows) {
    const [, kind, id] = k.split(":");
    if (kind === "ep") out.episodes++;
    else if (kind === "resume") out.resume.push({ entry: v?.entry_id ?? null, offset_ms: v?.offset_ms ?? null, at: v?.at ? new Date(v.at).toISOString() : null });
    else if (kind === "last") out.last.push({ device: id, entry: v?.entry_id ?? parseToken(v?.token)?.entryId ?? null,
      offset_ms: v?.offset_ms ?? null, finished: !!v?.finished, at: v?.at ? new Date(v.at).toISOString() : null });
  }
  return out;
}

// Worker route: GET /admin/requests with Authorization: Bearer <HUB_LINK_TOKEN>.
export async function routeRequests(request, env) {
  if (request.method !== "GET") return new Response("not found", { status: 404 });
  if (!env?.HUB_LINK_TOKEN) return new Response("not found", { status: 404 });
  const ok = await tokensEqual(bearerToken(request.headers.get("Authorization")), env.HUB_LINK_TOKEN);
  if (!ok) return new Response("unauthorized", { status: 401 });
  const stub = hubStub(env?.HUB_LINK);
  if (!stub) return new Response("unavailable", { status: 503 });
  const r = await stub.fetch("https://hub.internal/reqlog");
  return new Response(r.body, { status: r.status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}
