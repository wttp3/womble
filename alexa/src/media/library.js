// Library snapshot for Frida, built from the same tracked seed the hub uses
// (slice1/hub/media/library.seed.json, public URLs only). M3 replaces this with
// the snapshot the hub pushes into the DO.
import seed from "../../../slice1/hub/media/library.seed.json" with { type: "json" };

export const TYPES = ["radio", "podcast", "music_preset"];
const STOP = new Set(["the", "podcast", "radio", "a", "show", "station"]);
export const FUZZY_MIN = 0.85;

export function normalise(s) {
  const toks = String(s || "").toLowerCase().replace(/&/g, " and ").replace(/['\u2019]/g, "").match(/[a-z0-9]+/g) || [];
  const kept = toks.filter((t) => !STOP.has(t));
  return (kept.length ? kept : toks).join(" ");
}

// Dice coefficient on character bigrams of the sorted token set: cheap, order-free.
function bigrams(s) {
  const out = new Map();
  for (let i = 0; i < s.length - 1; i++) { const g = s.slice(i, i + 2); out.set(g, (out.get(g) || 0) + 1); }
  return out;
}
export function similarity(a, b) {
  const sa = [...new Set(a.split(" "))].sort().join(" ");
  const sb = [...new Set(b.split(" "))].sort().join(" ");
  if (!sa || !sb) return 0;
  if (sa === sb) return 1;
  const A = bigrams(sa), B = bigrams(sb);
  let inter = 0, total = 0;
  for (const [g, n] of A) { inter += Math.min(n, B.get(g) || 0); total += n; }
  for (const n of B.values()) total += n;
  return total ? (2 * inter) / total : 0;
}

export class Library {
  constructor(entries = seed.entries) {
    this.all = entries.map((e) => ({ enabled: true, aliases: [], ...e }));
  }
  entries(type) { return this.all.filter((e) => e.enabled !== false && (!type || e.type === type)); }
  get(id) { return this.all.find((e) => e.id === id) || null; }

  match(name) {
    const want = String(name || "").toLowerCase().replace(/[.!?]+$/, "").trim();
    const n = normalise(name);
    if (!n) return null;
    const live = this.entries();
    for (const e of live) for (const x of [e.name, ...e.aliases]) if (x.toLowerCase() === want) return e;
    for (const e of live) for (const x of [e.name, ...e.aliases]) if (normalise(x) === n) return e;
    const scored = live.map((e) => [e, Math.max(...[e.name, ...e.aliases].map((x) => similarity(n, normalise(x))))])
      .sort((a, b) => b[1] - a[1]);
    if (!scored.length || scored[0][1] < FUZZY_MIN) return null;
    if (scored[1] && scored[1][1] === scored[0][1]) return null;
    return scored[0][0];
  }

  summary(type) {
    const es = this.entries(type);
    if (!es.length) return "The media library is empty.";
    const words = "zero one two three four five six seven eight nine ten eleven twelve".split(" ");
    const label = { radio: ["radio station", "radio stations"], podcast: ["podcast", "podcasts"], music_preset: ["music preset", "music presets"] };
    const parts = [];
    for (const t of TYPES) {
      const k = es.filter((e) => e.type === t).length;
      if (k) parts.push(`${words[k] ?? k} ${label[t][k === 1 ? 0 : 1]}`);
    }
    const head = join(parts);
    const ex = TYPES.flatMap((t) => es.filter((e) => e.type === t).map((e) => e.name)).slice(0, 4);
    return `${head[0].toUpperCase()}${head.slice(1)}, including ${join(ex)}.`;
  }
}

function join(xs) {
  return xs.length <= 1 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`;
}

export const LIBRARY = new Library();
