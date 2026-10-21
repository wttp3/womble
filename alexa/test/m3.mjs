// M3/M4 worker tests: hub routing over HubLink, library snapshot, events to the hub,
// pending dispatch on LaunchRequest. No network.
import assert from "node:assert/strict";
import { handle } from "../src/app.js";
import { HubLink } from "../src/hub_link.js";

export function makeLink() {
  const mem = new Map();
  const storage = {
    async get(k) { return Array.isArray(k) ? new Map(k.map((x) => [x, structuredClone(mem.get(x))])) : structuredClone(mem.get(k)); },
    async put(k, v) { if (typeof k === "string") { mem.set(k, structuredClone(v)); return; } for (const [a, b] of Object.entries(k)) mem.set(a, structuredClone(b)); },
    async delete(k) { for (const x of Array.isArray(k) ? k : [k]) mem.delete(x); },
    async list({ prefix }) { return new Map([...mem].filter(([x]) => x.startsWith(prefix))); },
  };
  const sockets = [];
  const ctx = { storage, getWebSockets: () => sockets, acceptWebSocket(ws) { sockets.push(ws); } };
  const link = new HubLink(ctx, {});
  const sent = [];
  let answer = null;   // fn(job) -> reply message (or null = no reply)
  const ws = {
    readyState: 1, attachment: { connectedAt: Date.now() },
    serializeAttachment(a) { this.attachment = a; }, deserializeAttachment() { return this.attachment; },
    send(m) {
      const msg = JSON.parse(m); sent.push(msg);
      if (msg.id && answer) {
        const r = answer(msg);
        if (r) setTimeout(() => link.webSocketMessage(ws, JSON.stringify({ id: msg.id, ...r })), 5);
      }
    },
  };
  sockets.push(ws);
  const ns = { getByName: () => ({ fetch: (u, init) => link.fetch(new Request(u, init)) }) };
  return { link, ws, ns, mem, sent, setAnswer: (f) => { answer = f; } };
}

export async function runM3Tests(ok) {
  const SKILL = "amzn1.ask.skill.test-0000";
  const env = { ALEXA_SKILL_ID: SKILL, QUEUE: { send: async () => {} } };
  const pass = async () => {};
  const feed = `<rss><channel>${[3, 2, 1].map((i) => `<item><title>Ep ${i}</title><guid>g${i}</guid><pubDate>${new Date(Date.UTC(2026, 9, 10 + i)).toUTCString()}</pubDate><enclosure url="https://cdn.example/ep${i}.mp3" type="audio/mpeg"/></item>`).join("")}</channel></rss>`;
  const fetchFeed = async () => new Response(feed);
  const body = (request, dev = "amzn1.ask.device.KITCHEN", ap) => ({
    version: "1.0", session: { new: true, sessionId: "s", application: { applicationId: SKILL } },
    context: { System: { application: { applicationId: SKILL }, device: { deviceId: dev } }, ...(ap ? { AudioPlayer: ap } : {}) },
    request: { requestId: "r" + Math.random(), timestamp: new Date().toISOString(), locale: "en-GB", ...request },
  });
  const intent = (name, slots, dev) => body({ type: "IntentRequest", intent: { name, slots } }, dev);
  const said = (r) => (r.response.outputSpeech?.ssml || "").replace(/<\/?speak>/g, "").replace(/&apos;/g, "'");
  const play = (r) => r.response.directives?.[0];

  // --- hub says native: speech only, no AudioPlayer directive ---
  let L = makeLink();
  L.setAnswer((job) => job.media ? { speech: "Starting LBC on the kitchen speakers.", end_session: true, media: { route: "native", entry_id: "lbc", ok: true } } : null);
  const call = async (b, extra = {}) => (await (await handle(new Request("https://x/", { method: "POST", body: JSON.stringify(b) }), env, pass, { hub: L.ns, fetch: fetchFeed, ...extra })).json());
  let r = await call(intent("PlayMediaIntent", { media: { value: "LBC" } }));
  assert.equal(said(r), "Starting LBC on the kitchen speakers."); assert.equal(r.response.directives, undefined);
  const job = L.sent.find((m) => m.media);
  assert.deepEqual(job.media, { op: "play", entry_id: "lbc", name: "LBC", room: null, mode: "auto" });
  assert.ok(job.deviceId, "hub needs the device to pick the room");
  ok("M3: hub native route -> speech only, no double audio");

  // --- hub says frida: worker plays locally, then tells the hub ---
  L = makeLink();
  L.setAnswer((j) => j.media ? { speech: "", media: { route: "frida", entry_id: j.media.entry_id, mode: "latest" } } : null);
  r = await call(intent("PlayMediaIntent", { media: { value: "DSR Daily" }, room: { value: "kitchen" } }));
  assert.equal(play(r).audioItem.stream.url, "https://cdn.example/ep3.mp3");
  assert.equal(L.sent.find((m) => m.media).media.room, "kitchen");
  const ev = L.sent.find((m) => m.type === "media.event");
  assert.equal(ev.event, "play"); assert.equal(ev.entry_id, "dsr_daily"); assert.equal(ev.title, "Ep 3");
  assert.match(ev.device, /^[0-9a-f]{16}$/); assert.ok(!JSON.stringify(ev).includes("amzn1"));
  // Stopped event reaches the hub with the offset.
  await call(body({ type: "AudioPlayer.PlaybackStopped", token: play(r).audioItem.stream.token, offsetInMilliseconds: 61000 }));
  assert.equal(L.sent.filter((m) => m.type === "media.event").at(-1).event, "stopped");
  assert.equal(L.sent.filter((m) => m.type === "media.event").at(-1).offset_ms, 61000);
  ok("M3: hub frida route -> local AudioPlayer.Play; play/stopped events go to the hub");

  // --- hub says none (other room) ---
  L = makeLink();
  L.setAnswer((j) => j.media ? { speech: "I can't start that on the bedroom Echo yet.", media: { route: "none", ok: false } } : null);
  r = await call(intent("CaptureIntent", { query: { value: "play the dsr daily in the bedroom" } }));
  assert.equal(said(r), "I can't start that on the bedroom Echo yet.");
  assert.equal(L.sent.find((m) => m.media).media.room, "bedroom");
  ok("M3: old-model text 'play X in the bedroom' asks the hub with the room");

  // --- hub slow: local fallback after the media deadline ---
  L = makeLink();
  L.setAnswer(() => null);
  const t0 = Date.now();
  r = await call(intent("PlayMediaIntent", { media: { value: "LBC" } }), { hubMediaMs: 150 });
  assert.equal(play(r).type, "AudioPlayer.Play"); assert.ok(Date.now() - t0 < 2000);
  ok("M3: hub too slow -> worker plays locally");

  // --- library snapshot from the hub ---
  L = makeLink();
  L.setAnswer((j) => j.media ? { speech: "", media: { route: "frida", entry_id: j.media.entry_id } } : null);
  await L.link.webSocketMessage(L.ws, JSON.stringify({ type: "media.snapshot", entries: [
    { id: "rip", name: "The Rest Is Politics", aliases: ["rest is politics"], type: "podcast",
      methods: [{ kind: "podcast_rss_latest", target_kind: "any", feed: "https://feeds.example/rip" }] },
    { id: "lbc", name: "LBC", aliases: [], type: "radio", methods: [{ kind: "stream_url", target_kind: "echo", url: "https://media-ssl.musicradio.com/LBCLondon" }] },
  ] }));
  assert.ok(L.mem.get("media:snapshot").entries.length === 2);
  r = await call(intent("PlayMediaIntent", { media: { value: "the rest is politics" } }));
  assert.equal(play(r).audioItem.stream.url, "https://cdn.example/ep3.mp3");
  r = await call(intent("ListLibraryIntent"));
  assert.equal(said(r), "One radio station and one podcast, including LBC and The Rest Is Politics.");
  ok("M3: worker uses the hub's library snapshot (added entries play, summary follows)");

  // --- typed messages are validated ---
  await L.link.webSocketMessage(L.ws, JSON.stringify({ type: "media.pending", device: "nothex", cmd: { op: "play" } }));
  assert.ok(![...L.mem.keys()].some((k) => k.startsWith("media:pending:")));
  ok("M3: DO ignores malformed hub messages");
}
