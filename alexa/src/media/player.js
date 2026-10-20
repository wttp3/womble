// Frida AudioPlayer (media plan §4, M2). Radio and podcasts play on the Echo
// that was spoken to. State (resume offsets, last play per device, episode
// URLs) lives in the HubLink DO under "media:" keys, so playback works with
// the hub offline.
//
// Spike S0 facts used here: offset start works, AAC is fine, the 6-hop
// enclosure chain plays as published, Alexa sends NO PlaybackStarted events,
// and PlaybackStopped carries the offset.
import { hubStub, toSsml } from "../hub_link.js";
import { LIBRARY, Library } from "./library.js";
import { epKey, episodes, fetchItems } from "./rss.js";

export const RESUME_MIN_MS = 30_000;
export const RESUME_MAX_FRACTION = 0.95;
export const KEEP_MS = 30 * 24 * 3600 * 1000;
export const LIVE = "live";
const TOKEN_RE = /^w1\|([a-z0-9_]{1,64})\|([0-9a-f]{16}|live)$/;

export const makeToken = (entryId, key) => `w1|${entryId}|${key}`;
export function parseToken(token) {
  const m = TOKEN_RE.exec(String(token || ""));
  return m ? { entryId: m[1], key: m[2] } : null;
}

// ---------- responses ----------
const speech = (text) => ({ type: "SSML", ssml: toSsml(text) });
export function playDirective(url, token, offsetMs = 0) {
  return {
    type: "AudioPlayer.Play",
    playBehavior: "REPLACE_ALL",
    audioItem: { stream: { url, token, offsetInMilliseconds: Math.max(0, Math.floor(offsetMs || 0)) } },
  };
}
const playRes = (url, token, offsetMs, text) => {
  const response = { directives: [playDirective(url, token, offsetMs)], shouldEndSession: true };
  if (text) response.outputSpeech = speech(text);
  return { version: "1.0", response };
};
const stopRes = () => ({ version: "1.0", response: { directives: [{ type: "AudioPlayer.Stop" }], shouldEndSession: true } });
const sayRes = (text) => ({ version: "1.0", response: { outputSpeech: speech(text), shouldEndSession: true } });
// AudioPlayer / PlaybackController events: no speech, no shouldEndSession.
const emptyRes = () => ({ version: "1.0", response: {} });

// ---------- DO storage ----------
export class MediaStore {
  constructor(ns, now = () => Date.now()) { this.stub = hubStub(ns); this.now = now; }
  async _call(payload) {
    if (!this.stub) return null;
    try {
      const r = await this.stub.fetch("https://hub.internal/media", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
      });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }
  async get(...keys) {
    const r = await this._call({ op: "get", keys });
    const out = {};
    for (const k of keys) {
      const v = r?.values?.[k];
      out[k] = v && (!v.at || this.now() - v.at < KEEP_MS) ? v : null;
    }
    return out;
  }
  async put(values) {
    const stamped = {};
    for (const [k, v] of Object.entries(values)) stamped[k] = { ...v, at: this.now() };
    await this._call({ op: "put", values: stamped });
  }
  async del(...keys) { await this._call({ op: "delete", keys }); }
}

export async function deviceKey(body) {
  const id = body?.context?.System?.device?.deviceId || "";
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(id));
  return [...new Uint8Array(d)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------- text matching (works with the old model too) ----------
const LIST_RE = /^(?:what(?:'s| is)|whats) (?:in )?(?:my |the |your )?(?:media )?library\??$|^what (?:can|do) (?:you|i|frida) (?:play|have)\??$|^(?:list|read) (?:my |the )?(?:media )?library$/i;
const PLAY_RE = /^(?:please |to |and )?(?:play|put on|start|listen to)(?: me)?(?: the)?\s+(?:(latest|newest|last|new)\s+)?(?:episode (?:of )?)?(.+?)(?:\s+(?:in|on) the (\w+(?: room)?))?[.!?]?$/i;
const RESUME_RE = /^(?:please |to )?(?:resume|continue|carry on with)(?: the)?\s+(.+?)[.!?]?$/i;

const cleanText = (text) => String(text || "").replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, " ").trim();

// Cheap gate: could this text be a media request at all? (no DO read)
export function looksLikeMedia(text) {
  const t = cleanText(text);
  return !!t && (LIST_RE.test(t) || RESUME_RE.test(t) || PLAY_RE.test(t));
}

export function mediaIntentFromText(text, lib = LIBRARY) {
  const t = cleanText(text);
  if (!t) return null;
  if (LIST_RE.test(t)) return { op: "list" };
  let m = RESUME_RE.exec(t);
  if (m) { const e = lib.match(m[1]); return e ? { op: "play", entry: e, mode: "resume", room: null } : null; }
  m = PLAY_RE.exec(t);
  if (m) {
    const e = lib.match(m[2]);
    return e ? { op: "play", entry: e, mode: m[1] ? "latest" : "auto", room: m[3] || null } : null;
  }
  return null;
}

// Library: the hub's snapshot when it has pushed one (M3), else the seed.
async function loadLibrary(store) {
  const snap = (await store.get("media:snapshot"))["media:snapshot"];
  return Array.isArray(snap?.entries) && snap.entries.length ? new Library(snap.entries) : LIBRARY;
}

// Worker -> hub event for the frame (best effort, never blocks a reply for long).
function notify(ctx, msg) {
  const stub = ctx.store.stub;
  if (!stub) return;
  const p = stub.fetch("https://hub.internal/notify", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ type: "media.event", device: ctx.dev, ...msg }),
  }).catch(() => {});
  if (ctx.deps.waitUntil) ctx.deps.waitUntil(p);
  else ctx.pending.push(p);
}

export const HUB_MEDIA_MS = 3000;

// Ask the hub how to play (M3). null = hub offline or too slow: play locally.
async function askHub(body, ctx, env, media) {
  const stub = ctx.store.stub;
  if (!stub) return null;
  const req = body.request || {};
  try {
    const r = await stub.fetch("https://hub.internal/dispatch", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        id: req.requestId, text: "", intent: req.intent?.name || req.type, deviceId: body.context?.System?.device?.deviceId || null,
        sessionId: body.session?.sessionId || null, locale: req.locale || null,
        deadline: Date.now() + (ctx.deps.hubMediaMs ?? HUB_MEDIA_MS), media,
      }),
    });
    const a = await r.json();
    if (!a || a.offline || a.timeout || !a.media) return null;
    return a;
  } catch { return null; }
}

async function routedPlay(body, ctx, env, entry, mode, room) {
  const a = await askHub(body, ctx, env, { op: "play", entry_id: entry.id, name: entry.name, room: room || null, mode });
  if (ctx.deps.trace) ctx.deps.trace.forwarded = a ? `hub-media:${a.media.route}` : undefined;
  if (!a) return playEntry(entry, ctx, { mode });
  if (a.media.route === "frida") {
    const e = (a.media.entry_id && ctx.lib.get(a.media.entry_id)) || entry;
    return playEntry(e, ctx, { mode: a.media.mode || mode });
  }
  if (ctx.deps.trace) ctx.deps.trace.entry = entry.id;
  return sayRes(a.speech || "OK.");
}

function slotText(intent, name) {
  const s = intent?.slots?.[name];
  const r = s?.resolutions?.resolutionsPerAuthority?.find((a) => a?.status?.code === "ER_SUCCESS_MATCH");
  const id = r?.values?.[0]?.value?.id;
  return { id: id || null, value: String(s?.value || "").trim() };
}

// ---------- playback ----------
const minutes = (ms) => Math.round(ms / 60000);
const clip = (s, n = 90) => (s.length <= n ? s : `${s.slice(0, n - 1).trimEnd()}…`);

function liveUrl(entry) {
  const ms = entry.methods || [];
  const m = ms.find((x) => x.kind === "stream_url" && (x.target_kind === "echo" || x.target_kind === "any") && /^https:\/\//i.test(x.url || ""));
  return m ? m.url : null;
}

async function podcastEpisodes(entry, deps) {
  const m = (entry.methods || []).find((x) => x.kind === "podcast_rss_latest" && x.feed);
  if (!m) return [];
  return episodes(await fetchItems(m.feed, deps.fetch || globalThis.fetch), m);
}

function usableOffset(saved, durationS) {
  const off = saved?.offset_ms || 0;
  if (off < RESUME_MIN_MS) return 0;
  if (durationS && off > durationS * 1000 * RESUME_MAX_FRACTION) return 0;
  return off;
}

// mode: auto (resume if there is an offset) | latest (from the start) | resume | index
async function playEntry(entry, ctx, { mode = "auto", index = 0, quiet = false } = {}) {
  const { store, dev, deps } = ctx;
  if (entry.type === "music_preset") return sayRes(`${entry.name} plays on the kitchen speakers, which isn't set up yet.`);
  if (entry.type === "radio") {
    const url = liveUrl(entry);
    if (!url) return sayRes(`I can't play ${entry.name} on this Echo.`);
    const token = makeToken(entry.id, LIVE);
    await store.put({ [`media:last:${dev}`]: { token, offset_ms: 0, url, entry_id: entry.id, playing: true } });
    console.log(`media play entry=${entry.id} kind=radio`);
    notify(ctx, { event: "play", entry_id: entry.id, title: entry.name, offset_ms: 0, duration_s: 0 });
    return playRes(url, token, 0, quiet ? null : `Playing ${entry.name}.`);
  }
  let eps;
  try { eps = await podcastEpisodes(entry, deps); } catch (e) {
    console.log(`media feed failed entry=${entry.id} err=${String(e?.message || e).slice(0, 40)}`);
    if (deps.trace) { deps.trace.error = `feed: ${String(e?.message || e).slice(0, 60)}`; deps.trace.entry = entry.id; }
    return sayRes(`I couldn't get ${entry.name} right now.`);
  }
  const ep = eps[Math.max(0, index)];
  if (!ep) return sayRes(index > 0 ? `That's the oldest ${entry.name} episode I have.` : `I couldn't find an episode of ${entry.name}.`);
  const key = await epKey(ep);
  const token = makeToken(entry.id, key);
  let offset = 0;
  if (mode !== "latest") {
    const saved = (await store.get(`media:resume:${key}`))[`media:resume:${key}`];
    offset = usableOffset(saved, ep.durationS);
  }
  await store.put({
    [`media:ep:${key}`]: { entry_id: entry.id, url: ep.url, title: ep.title, pubDate: ep.pubDate, duration_s: ep.durationS },
    [`media:last:${dev}`]: { token, offset_ms: offset, url: ep.url, entry_id: entry.id, playing: true },
  });
  console.log(`media play entry=${entry.id} kind=podcast ep=${key} offset_ms=${offset} index=${index}`);
  notify(ctx, { event: "play", entry_id: entry.id, ep_key: key, title: ep.title, offset_ms: offset, duration_s: ep.durationS || 0 });
  let text = null;
  if (!quiet) {
    text = offset ? `Resuming ${entry.name} from ${minutes(offset)} minute${minutes(offset) === 1 ? "" : "s"}.`
      : `Playing ${entry.name}: ${clip(ep.title)}.`;
  }
  return playRes(ep.url, token, offset, text);
}

// The token playing (or last played) on this device, with its offset.
async function current(body, ctx) {
  const ap = body?.context?.AudioPlayer || {};
  const t = parseToken(ap.token);
  if (t) return { ...t, token: ap.token, offset: ap.offsetInMilliseconds || 0, activity: ap.playerActivity };
  const last = (await ctx.store.get(`media:last:${ctx.dev}`))[`media:last:${ctx.dev}`];
  const lt = parseToken(last?.token);
  return lt ? { ...lt, token: last.token, offset: last.offset_ms || 0, url: last.url } : null;
}

async function resume(body, ctx, quiet) {
  const cur = await current(body, ctx);
  if (!cur) return quiet ? emptyRes() : sayRes("There's nothing to resume.");
  const entry = ctx.lib.get(cur.entryId);
  if (cur.key === LIVE) return entry ? playEntry(entry, ctx, { quiet: true }) : emptyRes();
  let url = cur.url;
  if (!url) url = (await ctx.store.get(`media:ep:${cur.key}`))[`media:ep:${cur.key}`]?.url;
  if (!url) return entry ? playEntry(entry, ctx, { quiet }) : sayRes("There's nothing to resume.");
  console.log(`media resume entry=${cur.entryId} ep=${cur.key} offset_ms=${cur.offset}`);
  const epRec = (await ctx.store.get(`media:ep:${cur.key}`))[`media:ep:${cur.key}`];
  notify(ctx, { event: "play", entry_id: cur.entryId, ep_key: cur.key, title: epRec?.title || entry?.name, offset_ms: cur.offset, duration_s: epRec?.duration_s || 0 });
  await ctx.store.put({ [`media:last:${ctx.dev}`]: { token: cur.token, offset_ms: cur.offset, url, entry_id: cur.entryId, playing: true } });
  return playRes(url, cur.token, cur.offset, null);
}

// ---------- M4: commands left by the hub before it woke this Echo ----------
// The hub stores media:pending:<device hash> then fires a routine that opens
// Frida; the LaunchRequest that follows consumes it once (TTL set by the hub).
export async function takePending(ctx) {
  const k = `media:pending:${ctx.dev}`;
  const p = (await ctx.store.get(k))[k];
  if (!p || !p.cmd) return null;
  await ctx.store.del(k);
  const now = ctx.store.now();
  if (p.until && now > p.until) return null;
  return p.cmd;
}

// Where the stream is now: Alexa gives no PlaybackStarted, so estimate from
// the last known offset plus wall time while playing.
async function estimate(ctx) {
  const k = `media:last:${ctx.dev}`;
  const last = (await ctx.store.get(k))[k];
  const t = parseToken(last?.token);
  if (!t) return null;
  const run = last.playing && last.at ? Math.max(0, ctx.store.now() - last.at) : 0;
  return { ...t, token: last.token, url: last.url, offset: (last.offset_ms || 0) + run, playing: !!last.playing };
}

async function runPending(cmd, body, ctx) {
  console.log(`media pending op=${String(cmd.op).slice(0, 12)}`);
  if (ctx.deps.trace) ctx.deps.trace.pending = String(cmd.op).slice(0, 12);
  if (cmd.op === "play") {
    const entry = ctx.lib.get(cmd.entry_id);
    if (!entry) return sayRes("That isn't in the library any more.");
    return playEntry(entry, ctx, { mode: cmd.mode || "auto", quiet: true });
  }
  const cur = await estimate(ctx);
  if (!cur) return sayRes("Nothing is playing.");
  if (cmd.op === "pause" || cmd.op === "stop") {
    // Untested live: opening the skill already took focus; Stop ends the stream.
    await ctx.store.put({ [`media:last:${ctx.dev}`]: { token: cur.token, offset_ms: cur.key === LIVE ? 0 : cur.offset, url: cur.url, entry_id: cur.entryId, playing: false } });
    if (cur.key !== LIVE) await ctx.store.put({ [`media:resume:${cur.key}`]: { entry_id: cur.entryId, offset_ms: cur.offset } });
    notify(ctx, { event: "stopped", entry_id: cur.entryId, ep_key: cur.key === LIVE ? null : cur.key, offset_ms: cur.offset });
    return stopRes();
  }
  if (cmd.op === "resume") return resume(body, ctx, true);
  if (cmd.op === "next" || cmd.op === "prev") return step(body, ctx, cmd.op === "next" ? +1 : -1, true);
  if (cmd.op === "seek" && cur.key !== LIVE) {
    const url = cur.url || (await ctx.store.get(`media:ep:${cur.key}`))[`media:ep:${cur.key}`]?.url;
    if (!url) return sayRes("Nothing is playing.");
    const off = Math.max(0, cur.offset + (Number(cmd.delta_ms) || 0));
    await ctx.store.put({ [`media:last:${ctx.dev}`]: { token: cur.token, offset_ms: off, url, entry_id: cur.entryId, playing: true } });
    notify(ctx, { event: "play", entry_id: cur.entryId, ep_key: cur.key, offset_ms: off });
    return playRes(url, cur.token, off, null);
  }
  return sayRes("I can't do that with this stream.");
}

async function step(body, ctx, dir, quiet) {
  const cur = await current(body, ctx);
  const entry = cur && ctx.lib.get(cur.entryId);
  if (!entry) return quiet ? emptyRes() : sayRes("Nothing is playing.");
  if (cur.key === LIVE) {
    const radios = ctx.lib.entries("radio");
    const i = radios.findIndex((e) => e.id === entry.id);
    const next = radios[(i + dir + radios.length) % radios.length];
    return playEntry(next, ctx, { quiet });
  }
  let eps;
  try { eps = await podcastEpisodes(entry, ctx.deps); } catch { return quiet ? emptyRes() : sayRes(`I couldn't get ${entry.name} right now.`); }
  const keys = await Promise.all(eps.map(epKey));
  const i = keys.indexOf(cur.key);
  const j = (i < 0 ? 0 : i) + dir;   // next = older (+1), previous = newer (-1)
  if (j < 0) return quiet ? emptyRes() : sayRes(`That's the newest ${entry.name} episode.`);
  if (j >= eps.length) return quiet ? emptyRes() : sayRes(`That's the oldest ${entry.name} episode I have.`);
  return playEntry(entry, ctx, { mode: "auto", index: j, quiet });
}

async function startOver(body, ctx) {
  const cur = await current(body, ctx);
  if (!cur) return sayRes("Nothing is playing.");
  if (cur.key === LIVE) return resume(body, ctx, true);
  const url = cur.url || (await ctx.store.get(`media:ep:${cur.key}`))[`media:ep:${cur.key}`]?.url;
  if (!url) return sayRes("Nothing is playing.");
  return playRes(url, cur.token, 0, null);
}

async function onEvent(body, ctx) {
  const req = body.request || {};
  const t = parseToken(req.token);
  const off = Number.isFinite(req.offsetInMilliseconds) ? req.offsetInMilliseconds : 0;
  const type = req.type;
  if (type === "AudioPlayer.PlaybackFailed" || type === "System.ExceptionEncountered") {
    const err = req.error || {};
    console.log(`media event=${type} err=${String(err.type || "-").slice(0, 60)} msg=${String(err.message || "-").replace(/https?:\/\/\S+/g, "<url>").slice(0, 120)}`);
    const ft = parseToken(req.token);
    if (ft) notify(ctx, { event: "failed", entry_id: ft.entryId, ep_key: ft.key === LIVE ? null : ft.key, error: String(err.type || "").slice(0, 60) });
    return emptyRes();
  }
  if (!t) return emptyRes();
  console.log(`media event=${type} entry=${t.entryId} ep=${t.key} offset_ms=${off}`);
  const lastKey = `media:last:${ctx.dev}`;
  if (type === "AudioPlayer.PlaybackStopped") notify(ctx, { event: "stopped", entry_id: t.entryId, ep_key: t.key === LIVE ? null : t.key, offset_ms: off });
  if (type === "AudioPlayer.PlaybackFinished") notify(ctx, { event: "finished", entry_id: t.entryId, ep_key: t.key === LIVE ? null : t.key, offset_ms: 0 });
  if (type === "AudioPlayer.PlaybackStopped" || type === "AudioPlayer.PlaybackStarted") {
    const prev = (await ctx.store.get(lastKey))[lastKey];
    const url = prev && prev.token === req.token ? prev.url : undefined;
    const vals = { [lastKey]: { token: req.token, offset_ms: t.key === LIVE ? 0 : off, url, entry_id: t.entryId, playing: type === "AudioPlayer.PlaybackStarted" } };
    if (t.key !== LIVE && off > 0) vals[`media:resume:${t.key}`] = { entry_id: t.entryId, offset_ms: off };
    await ctx.store.put(vals);
  } else if (type === "AudioPlayer.PlaybackFinished") {
    if (t.key !== LIVE) await ctx.store.del(`media:resume:${t.key}`);
    const prev = (await ctx.store.get(lastKey))[lastKey];
    await ctx.store.put({ [lastKey]: { token: req.token, offset_ms: 0, url: prev?.url, entry_id: t.entryId, finished: true, playing: false } });
  }
  // NearlyFinished: enqueue nothing, so a podcast stops at the end (Q5 default).
  return emptyRes();
}

const UNSUPPORTED = new Set(["AMAZON.LoopOnIntent", "AMAZON.LoopOffIntent", "AMAZON.ShuffleOnIntent",
  "AMAZON.ShuffleOffIntent", "AMAZON.RepeatIntent"]);
export const MEDIA_INTENTS = new Set(["PlayMediaIntent", "PlayLatestMediaIntent", "ResumeMediaIntent", "ListLibraryIntent"]);

const ours = (body) => !!parseToken(body?.context?.AudioPlayer?.token);

// Returns a response object, or null when this is not a media request.
// `text` is the utterance the normal path would use (null if none).
export async function handleMedia(body, env, deps = {}, text = null) {
  const req = body?.request || {};
  const type = String(req.type || "");
  const isEvent = type.startsWith("AudioPlayer.") || type.startsWith("PlaybackController.") || type === "System.ExceptionEncountered";
  const name = req.intent?.name;
  let parsed = null;
  // A routine-opened launch has no transcript; a spoken "ask frida …" does.
  if (type === "LaunchRequest" && !text) {
    const pctx = { store: new MediaStore(deps.hub ?? env.HUB_LINK, deps.now), dev: await deviceKey(body), deps, pending: [] };
    const cmd = pctx.dev ? await takePending(pctx) : null;
    if (cmd) {
      pctx.lib = await loadLibrary(pctx.store);
      const res = await runPending(cmd, body, pctx);
      if (pctx.pending.length) await Promise.all(pctx.pending);
      return res;
    }
  }
  if (!isEvent) {
    if (type !== "IntentRequest" && type !== "LaunchRequest") return null;
    const builtin = ["AMAZON.PauseIntent", "AMAZON.ResumeIntent", "AMAZON.NextIntent", "AMAZON.PreviousIntent",
      "AMAZON.StartOverIntent", ...UNSUPPORTED].includes(name);
    const stopLike = ["AMAZON.StopIntent", "AMAZON.CancelIntent"].includes(name) && ours(body);
    if (!builtin && !stopLike && !MEDIA_INTENTS.has(name) && !looksLikeMedia(text)) return null;
  }
  const ctx = { store: new MediaStore(deps.hub ?? env.HUB_LINK, deps.now), dev: await deviceKey(body), deps, pending: [] };
  ctx.lib = await loadLibrary(ctx.store);
  const name0 = name;
  if (!isEvent && !["AMAZON.PauseIntent", "AMAZON.ResumeIntent", "AMAZON.NextIntent", "AMAZON.PreviousIntent",
    "AMAZON.StartOverIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent", ...UNSUPPORTED].includes(name0) && !MEDIA_INTENTS.has(name0)) {
    parsed = mediaIntentFromText(text, ctx.lib);
    if (!parsed) return null;
  }
  const res = await mediaReply(body, env, ctx, req, type, isEvent, name, parsed);
  if (ctx.pending.length) await Promise.all(ctx.pending);
  return res;
}

async function mediaReply(body, env, ctx, req, type, isEvent, name, parsed) {

  if (type === "PlaybackController.PauseCommandIssued") return stopRes();
  if (type === "PlaybackController.PlayCommandIssued") return resume(body, ctx, true);
  if (type === "PlaybackController.NextCommandIssued") return step(body, ctx, +1, true);
  if (type === "PlaybackController.PreviousCommandIssued") return step(body, ctx, -1, true);
  if (isEvent) return onEvent(body, ctx);

  if (parsed?.op === "list" || name === "ListLibraryIntent") return sayRes(ctx.lib.summary());
  if (parsed?.op === "play") return routedPlay(body, ctx, env, parsed.entry, parsed.mode, parsed.room);
  if (MEDIA_INTENTS.has(name)) {
    const slot = slotText(req.intent, "media");
    // "play the latest X" can land on PlayMediaIntent with "latest X" in the slot.
    const latestWord = /^(?:the )?(?:latest|newest|new)\s+(?:episode (?:of )?)?/i;
    const bare = slot.value.replace(latestWord, "");
    const entry = (slot.id && ctx.lib.get(slot.id)) || ctx.lib.match(slot.value) || ctx.lib.match(bare);
    if (!entry) return sayRes(slot.value ? `${slot.value} isn't in the library yet.` : "What should I play?");
    let mode = name === "PlayLatestMediaIntent" ? "latest" : name === "ResumeMediaIntent" ? "resume" : "auto";
    if (mode === "auto" && bare !== slot.value) mode = "latest";
    const room = slotText(req.intent, "room").value || null;
    return routedPlay(body, ctx, env, entry, mode, room);
  }
  switch (name) {
    case "AMAZON.PauseIntent":
    case "AMAZON.StopIntent":
    case "AMAZON.CancelIntent":
      return stopRes();
    case "AMAZON.ResumeIntent": return resume(body, ctx, false);
    case "AMAZON.NextIntent": return step(body, ctx, +1, false);
    case "AMAZON.PreviousIntent": return step(body, ctx, -1, false);
    case "AMAZON.StartOverIntent": return startOver(body, ctx);
    default: return sayRes("I can't do that with this stream.");
  }
}
