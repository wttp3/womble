// Frida AudioPlayer (media plan §4, M2). Radio and podcasts play on the Echo
// that was spoken to. State (resume offsets, last play per device, episode
// URLs) lives in the HubLink DO under "media:" keys, so playback works with
// the hub offline.
//
// Spike S0 facts used here: offset start works, AAC is fine, the 6-hop
// enclosure chain plays as published, Alexa sends NO PlaybackStarted events,
// and PlaybackStopped carries the offset.
import { hubStub, toSsml } from "../hub_link.js";
import { LIBRARY } from "./library.js";
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
const PLAY_RE = /^(?:please |to |and )?(?:play|put on|start|listen to)(?: me)?(?: the)?\s+(?:(latest|newest|last|new)\s+)?(?:episode (?:of )?)?(.+?)(?:\s+(?:in|on) the \w+)?[.!?]?$/i;
const RESUME_RE = /^(?:please |to )?(?:resume|continue|carry on with)(?: the)?\s+(.+?)[.!?]?$/i;

export function mediaIntentFromText(text) {
  const t = String(text || "").replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, " ").trim();
  if (!t) return null;
  if (LIST_RE.test(t)) return { op: "list" };
  let m = RESUME_RE.exec(t);
  if (m) { const e = LIBRARY.match(m[1]); return e ? { op: "play", entry: e, mode: "resume" } : null; }
  m = PLAY_RE.exec(t);
  if (m) {
    const e = LIBRARY.match(m[2]);
    return e ? { op: "play", entry: e, mode: m[1] ? "latest" : "auto" } : null;
  }
  return null;
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
    await store.put({ [`media:last:${dev}`]: { token, offset_ms: 0, url, entry_id: entry.id } });
    console.log(`media play entry=${entry.id} kind=radio`);
    return playRes(url, token, 0, quiet ? null : `Playing ${entry.name}.`);
  }
  let eps;
  try { eps = await podcastEpisodes(entry, deps); } catch (e) {
    console.log(`media feed failed entry=${entry.id} err=${String(e?.message || e).slice(0, 40)}`);
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
    [`media:last:${dev}`]: { token, offset_ms: offset, url: ep.url, entry_id: entry.id },
  });
  console.log(`media play entry=${entry.id} kind=podcast ep=${key} offset_ms=${offset} index=${index}`);
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
  const entry = LIBRARY.get(cur.entryId);
  if (cur.key === LIVE) return entry ? playEntry(entry, ctx, { quiet: true }) : emptyRes();
  let url = cur.url;
  if (!url) url = (await ctx.store.get(`media:ep:${cur.key}`))[`media:ep:${cur.key}`]?.url;
  if (!url) return entry ? playEntry(entry, ctx, { quiet }) : sayRes("There's nothing to resume.");
  console.log(`media resume entry=${cur.entryId} ep=${cur.key} offset_ms=${cur.offset}`);
  return playRes(url, cur.token, cur.offset, null);
}

async function step(body, ctx, dir, quiet) {
  const cur = await current(body, ctx);
  const entry = cur && LIBRARY.get(cur.entryId);
  if (!entry) return quiet ? emptyRes() : sayRes("Nothing is playing.");
  if (cur.key === LIVE) {
    const radios = LIBRARY.entries("radio");
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
    return emptyRes();
  }
  if (!t) return emptyRes();
  console.log(`media event=${type} entry=${t.entryId} ep=${t.key} offset_ms=${off}`);
  const lastKey = `media:last:${ctx.dev}`;
  if (type === "AudioPlayer.PlaybackStopped" || type === "AudioPlayer.PlaybackStarted") {
    const prev = (await ctx.store.get(lastKey))[lastKey];
    const url = prev && prev.token === req.token ? prev.url : undefined;
    const vals = { [lastKey]: { token: req.token, offset_ms: t.key === LIVE ? 0 : off, url, entry_id: t.entryId } };
    if (t.key !== LIVE && off > 0) vals[`media:resume:${t.key}`] = { entry_id: t.entryId, offset_ms: off };
    await ctx.store.put(vals);
  } else if (type === "AudioPlayer.PlaybackFinished") {
    if (t.key !== LIVE) await ctx.store.del(`media:resume:${t.key}`);
    const prev = (await ctx.store.get(lastKey))[lastKey];
    await ctx.store.put({ [lastKey]: { token: req.token, offset_ms: 0, url: prev?.url, entry_id: t.entryId, finished: true } });
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
  if (!isEvent) {
    if (type !== "IntentRequest" && type !== "LaunchRequest") return null;
    const builtin = ["AMAZON.PauseIntent", "AMAZON.ResumeIntent", "AMAZON.NextIntent", "AMAZON.PreviousIntent",
      "AMAZON.StartOverIntent", ...UNSUPPORTED].includes(name);
    const stopLike = ["AMAZON.StopIntent", "AMAZON.CancelIntent"].includes(name) && ours(body);
    if (!builtin && !stopLike && !MEDIA_INTENTS.has(name)) {
      parsed = mediaIntentFromText(text);
      if (!parsed) return null;
    }
  }
  const ctx = { store: new MediaStore(deps.hub ?? env.HUB_LINK, deps.now), dev: await deviceKey(body), deps };

  if (type === "PlaybackController.PauseCommandIssued") return stopRes();
  if (type === "PlaybackController.PlayCommandIssued") return resume(body, ctx, true);
  if (type === "PlaybackController.NextCommandIssued") return step(body, ctx, +1, true);
  if (type === "PlaybackController.PreviousCommandIssued") return step(body, ctx, -1, true);
  if (isEvent) return onEvent(body, ctx);

  if (parsed?.op === "list" || name === "ListLibraryIntent") return sayRes(LIBRARY.summary());
  if (parsed?.op === "play") return playEntry(parsed.entry, ctx, { mode: parsed.mode });
  if (MEDIA_INTENTS.has(name)) {
    const slot = slotText(req.intent, "media");
    // "play the latest X" can land on PlayMediaIntent with "latest X" in the slot.
    const latestWord = /^(?:the )?(?:latest|newest|new)\s+(?:episode (?:of )?)?/i;
    const bare = slot.value.replace(latestWord, "");
    const entry = (slot.id && LIBRARY.get(slot.id)) || LIBRARY.match(slot.value) || LIBRARY.match(bare);
    if (!entry) return sayRes(slot.value ? `${slot.value} isn't in the library yet.` : "What should I play?");
    let mode = name === "PlayLatestMediaIntent" ? "latest" : name === "ResumeMediaIntent" ? "resume" : "auto";
    if (mode === "auto" && bare !== slot.value) mode = "latest";
    return playEntry(entry, ctx, { mode });
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
