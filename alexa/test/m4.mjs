// M4 worker tests: the hub leaves a command (media.pending), fires a routine,
// and the LaunchRequest that follows consumes it once. No network.
import assert from "node:assert/strict";
import { handle } from "../src/app.js";
import { deviceKey } from "../src/media/player.js";
import { makeLink } from "./m3.mjs";

export async function runM4Tests(ok) {
  const SKILL = "amzn1.ask.skill.test-0000";
  const env = { ALEXA_SKILL_ID: SKILL, QUEUE: { send: async () => {} } };
  const pass = async () => {};
  const feed = `<rss><channel>${[3, 2, 1].map((i) => `<item><title>Ep ${i}</title><guid>g${i}</guid><pubDate>${new Date(Date.UTC(2026, 9, 10 + i)).toUTCString()}</pubDate><enclosure url="https://cdn.example/ep${i}.mp3" type="audio/mpeg"/></item>`).join("")}</channel></rss>`;
  const fetchFeed = async () => new Response(feed);
  const DEV = "amzn1.ask.device.KITCHEN";
  const body = (request, ap) => ({
    version: "1.0", session: { new: true, sessionId: "s", application: { applicationId: SKILL } },
    context: { System: { application: { applicationId: SKILL }, device: { deviceId: DEV } }, ...(ap ? { AudioPlayer: ap } : {}) },
    request: { requestId: "r" + Math.random(), timestamp: new Date().toISOString(), locale: "en-GB", ...request },
  });
  const said = (r) => (r.response.outputSpeech?.ssml || "").replace(/<\/?speak>/g, "").replace(/&apos;/g, "'");
  const dir = (r) => r.response.directives?.[0];
  const hash = await deviceKey(body({}));
  let clock = Date.now();
  const L = makeLink();
  const call = async (b) => (await (await handle(new Request("https://x/", { method: "POST", body: JSON.stringify(b) }), env, pass, { hub: L.ns, fetch: fetchFeed, now: () => clock })).json());
  const pend = async (cmd, ttl_s = 60) => L.link.webSocketMessage(L.ws, JSON.stringify({ type: "media.pending", device: hash, cmd, ttl_s }));

  // play: launch plays quietly, consumed once
  await pend({ op: "play", entry_id: "dsr_daily", mode: "latest" });
  assert.ok(L.mem.has(`media:pending:${hash}`));
  let r = await call(body({ type: "LaunchRequest" }));
  assert.equal(dir(r).type, "AudioPlayer.Play"); assert.equal(dir(r).audioItem.stream.url, "https://cdn.example/ep3.mp3");
  assert.equal(r.response.outputSpeech, undefined, "quiet start");
  assert.ok(!L.mem.has(`media:pending:${hash}`));
  r = await call(body({ type: "LaunchRequest" }));
  assert.equal(dir(r), undefined); assert.match(said(r), /what do you need/i);
  ok("M4: pending play consumed once by LaunchRequest; second launch greets");

  // seek: estimate = last offset + wall time while playing
  const tok = (await call(body({ type: "IntentRequest", intent: { name: "ResumeMediaIntent", slots: { media: { value: "DSR Daily" } } } }))).response.directives[0].audioItem.stream.token;
  await call(body({ type: "AudioPlayer.PlaybackStopped", token: tok, offsetInMilliseconds: 100000 }));
  await call(body({ type: "IntentRequest", intent: { name: "AMAZON.ResumeIntent" } }));   // playing from 100 s
  clock += 20000;
  await pend({ op: "seek", delta_ms: 30000 });
  r = await call(body({ type: "LaunchRequest" }));
  assert.equal(dir(r).type, "AudioPlayer.Play");
  assert.ok(Math.abs(dir(r).audioItem.stream.offsetInMilliseconds - 150000) < 1500, String(dir(r).audioItem.stream.offsetInMilliseconds));
  await pend({ op: "seek", delta_ms: -1000000 });
  r = await call(body({ type: "LaunchRequest" }));
  assert.equal(dir(r).audioItem.stream.offsetInMilliseconds, 0);
  ok("M4/M5: pending seek +-30 s from the estimated offset, clamped at 0");

  // pause via launch: AudioPlayer.Stop, resume point saved, hub told
  clock += 40000;
  await pend({ op: "pause" });
  r = await call(body({ type: "LaunchRequest" }));
  assert.equal(dir(r).type, "AudioPlayer.Stop");
  const ev = L.sent.filter((m) => m.type === "media.event").at(-1);
  assert.equal(ev.event, "stopped"); assert.ok(ev.offset_ms >= 39000);
  await pend({ op: "resume" });
  r = await call(body({ type: "LaunchRequest" }));
  assert.equal(dir(r).type, "AudioPlayer.Play"); assert.ok(dir(r).audioItem.stream.offsetInMilliseconds >= 39000);
  ok("M4/M5: pending pause -> AudioPlayer.Stop (estimate saved), pending resume -> Play from there");

  // expired / bad
  await pend({ op: "play", entry_id: "dsr_daily" }, 5);
  clock += 6000;
  r = await call(body({ type: "LaunchRequest" }));
  assert.equal(dir(r), undefined);
  await L.link.webSocketMessage(L.ws, JSON.stringify({ type: "media.pending", device: "not-a-hash", cmd: { op: "play" } }));
  assert.ok(![...L.mem.keys()].some((k) => k.includes("not-a-hash")));
  await pend({ op: "play", entry_id: "nope" }, 120);   // DO uses the real clock, test clock is ahead
  r = await call(body({ type: "LaunchRequest" }));
  assert.match(said(r), /isn't in the library/);
  ok("M4: expired pending ignored; bad device rejected; unknown entry answered");
}
