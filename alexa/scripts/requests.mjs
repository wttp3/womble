#!/usr/bin/env node
// Print the worker's request log (last 50 Alexa requests) and the media state.
//   node alexa/scripts/requests.mjs [--json] [--last N]
// Worker URL: ALEXA_WORKER_URL from ~/box3-voice/local.env (untracked).
// Bearer: Keychain item womble-hub-link, read here and never printed.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const args = process.argv.slice(2);
const asJson = args.includes("--json");
const lastN = Number(args[args.indexOf("--last") + 1]) || 50;

function workerUrl() {
  let v = process.env.ALEXA_WORKER_URL;
  if (!v) {
    try {
      const env = readFileSync(new URL("../../local.env", import.meta.url), "utf8");
      v = /^\s*ALEXA_WORKER_URL\s*=\s*["']?([^"'\s#]+)/m.exec(env)?.[1];
    } catch { /* no local.env */ }
  }
  if (!v) { console.error("ALEXA_WORKER_URL is not set (local.env)"); process.exit(2); }
  const u = new URL(v.replace(/^wss:/, "https:").replace(/^ws:/, "http:"));
  return `${u.origin}/admin/requests`;
}

function token() {
  try {
    return execFileSync("security", ["find-generic-password", "-s", "womble-hub-link", "-w"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch { console.error("Keychain item womble-hub-link not found"); process.exit(2); }
}

const res = await fetch(workerUrl(), { headers: { Authorization: `Bearer ${token()}` } });
if (!res.ok) { console.error(`request log: HTTP ${res.status}`); process.exit(1); }
const data = await res.json();
if (asJson) { console.log(JSON.stringify(data, null, 2)); process.exit(0); }

const fmt = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Madrid", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const when = (iso) => (iso ? fmt.format(new Date(iso)).replace(",", "") : "-");
const mins = (ms) => (ms == null ? "" : `${(ms / 60000).toFixed(1)}m`);
const short = (s, n) => { s = s == null ? "" : String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const rows = (data.requests || []).slice(-lastN);
console.log(`Request log: ${rows.length} of ${(data.requests || []).length} (times Europe/Madrid)\n`);
const cols = [["time", 14], ["dev", 4], ["request", 30], ["said", 34], ["entry", 14], ["reply", 18], ["host / offset", 26], ["answer / error", 50]];
console.log(cols.map(([h, w]) => h.padEnd(w)).join(" "));
for (const e of rows) {
  const req = e.intent ? `${e.type === "IntentRequest" ? "" : e.type + " "}${e.intent}` : e.type;
  const slots = e.slots ? Object.entries(e.slots).map(([k, v]) => `${k}=${v}`).join(" ") : "";
  const said = e.text || slots;
  const where = [e.stream_host, e.play_offset_ms ? `@${mins(e.play_offset_ms)}` : "", e.event_offset_ms != null ? `ev ${mins(e.event_offset_ms)}` : ""].filter(Boolean).join(" ");
  const tail = e.error ? `ERR ${e.error}` : (e.speech || e.reason || "");
  console.log([when(e.at), (e.device || "").slice(0, 4), short(req, 30), short(said, 34), short(e.entry, 14),
    short(e.reply, 18), short(where, 26), short(tail, 50)].map((v, i) => String(v ?? "").padEnd(cols[i][1])).join(" "));
}
const m = data.media;
if (m) {
  console.log(`\nMedia state: ${m.episodes} episode(s) known`);
  for (const r of m.resume || []) console.log(`  resume  ${short(r.entry, 22).padEnd(22)} at ${mins(r.offset_ms).padStart(7)}  (${when(r.at)})`);
  for (const l of m.last || []) console.log(`  last    dev ${String(l.device).slice(0, 4)} ${short(l.entry, 18).padEnd(18)} ${mins(l.offset_ms).padStart(7)}${l.finished ? " finished" : ""}  (${when(l.at)})`);
}
