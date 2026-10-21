// Latest-episode RSS resolution for Frida (media plan §4). Stream-parses the
// feed, keeps the first MAX_ITEMS <item> blocks and cancels the body, so a
// multi-MB Daily Beast feed costs little CPU. Enclosure URLs are played exactly
// as published (S0b: Alexa follows the 6-hop tracking chain).

export const MAX_ITEMS = 30;
export const MAX_BYTES = 2 * 1024 * 1024;
export const FEED_TTL_S = 900;
const UA = "Womble/1.0 (Frida media)";

const decodeXml = (s) => String(s ?? "")
  .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, "\"").replace(/&apos;/g, "'")
  .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
  .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
  .replace(/&amp;/g, "&").trim();

function tag(block, name) {
  const m = new RegExp(`<${name}\\b[^>]*>([\\s\\S]*?)</${name}>`, "i").exec(block);
  return m ? decodeXml(m[1]) : "";
}

function attr(el, name) {
  const m = new RegExp(`\\b${name}\\s*=\\s*(["'])([\\s\\S]*?)\\1`, "i").exec(el);
  return m ? decodeXml(m[2]) : null;
}

export function parseItem(block) {
  const enc = /<enclosure\b[^>]*>/i.exec(block)?.[0] || "";
  const dur = tag(block, "itunes:duration");
  return {
    title: tag(block, "title"),
    guid: tag(block, "guid"),
    pubDate: tag(block, "pubDate"),
    episodeType: (tag(block, "itunes:episodeType") || "full").toLowerCase(),
    url: enc ? attr(enc, "url") : null,
    mime: enc ? attr(enc, "type") : null,
    durationS: parseDuration(dur),
  };
}

export function parseDuration(s) {
  if (!s) return null;
  if (/^\d+$/.test(s)) return Number(s);
  const parts = s.split(":").map(Number);
  if (parts.some((n) => !Number.isFinite(n))) return null;
  return parts.reduce((a, n) => a * 60 + n, 0);
}

// Pull complete <item>…</item> blocks out of a growing buffer.
export function takeItems(buf, out, limit = MAX_ITEMS) {
  let rest = buf;
  for (;;) {
    if (out.length >= limit) return "";
    const start = rest.search(/<item[\s>]/i);
    if (start < 0) return rest.slice(-16);       // keep a tail in case "<item" is split
    const end = rest.indexOf("</item>", start);
    if (end < 0) return rest.slice(start);
    out.push(parseItem(rest.slice(start, end + 7)));
    rest = rest.slice(end + 7);
  }
}

export async function readItems(body, limit = MAX_ITEMS) {
  const reader = body.getReader();
  const dec = new TextDecoder();
  const out = [];
  let buf = "";
  let bytes = 0;
  try {
    while (out.length < limit && bytes < MAX_BYTES) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      buf = takeItems(buf + dec.decode(value, { stream: true }), out, limit);
    }
  } finally {
    try { await reader.cancel(); } catch { /* already closed */ }
  }
  return out.slice(0, limit);
}

export async function fetchItems(feed, fetchImpl = globalThis.fetch) {
  const res = await fetchImpl(feed, { headers: { "user-agent": UA }, cf: { cacheTtl: FEED_TTL_S, cacheEverything: true } });
  if (!res.ok || !res.body) throw new Error(`feed ${res.status}`);
  return readItems(res.body);
}

const ts = (d) => { const t = Date.parse(d); return Number.isFinite(t) ? t : 0; };

const titleKey = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

// Playable episodes, newest first: HTTPS enclosure, not trailer/bonus,
// optional title prefix, duplicates (same guid/enclosure/title) collapsed.
export function episodes(items, method = {}) {
  const skip = new Set((method.skip_episode_types || ["trailer", "bonus"]).map((s) => s.toLowerCase()));
  const prefix = (method.filter?.title_prefix || "").toLowerCase();
  const seen = new Set();
  const keep = [];
  for (const it of items) {
    if (!it.url || !/^https:\/\//i.test(it.url)) continue;
    if (skip.has(it.episodeType)) continue;
    if (prefix && !it.title.toLowerCase().startsWith(prefix)) continue;
    const k1 = it.guid || it.url;
    const k2 = titleKey(it.title);
    if (seen.has(k1) || (k2 && seen.has(k2))) continue;
    seen.add(k1); if (k2) seen.add(k2);
    keep.push(it);
  }
  return keep.sort((a, b) => ts(b.pubDate) - ts(a.pubDate));
}

export async function epKey(item) {
  let basis = item.guid;
  if (!basis) { try { basis = new URL(item.url).pathname; } catch { basis = item.url || ""; } }
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(basis));
  return [...new Uint8Array(d)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}
