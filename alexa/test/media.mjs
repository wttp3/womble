// M2 media tests: AudioPlayer directives, tokens, RSS early stop, resume, next/previous.
// No network: feeds are generated in memory. Imported by test.mjs.
import assert from "node:assert/strict";
import { handle } from "../src/app.js";
import { HubLink } from "../src/hub_link.js";
import { LIBRARY } from "../src/media/library.js";
import { episodes, readItems, parseDuration } from "../src/media/rss.js";
import { makeToken, parseToken, mediaIntentFromText } from "../src/media/player.js";
import { REQLOG_MAX, routeRequests } from "../src/reqlog.js";

export async function runMediaTests(ok) {
  const SKILL = "amzn1.ask.skill.test-0000";
  const env = { ALEXA_SKILL_ID: SKILL, QUEUE: { send: async () => { throw new Error("media must not enqueue"); } } };
  const pass = async () => {};

  // Real HubLink DO with in-memory storage.
  const mem = new Map();
  const storage = {
    async get(keys) { return Array.isArray(keys) ? new Map(keys.map((k) => [k, structuredClone(mem.get(k))])) : structuredClone(mem.get(keys)); },
    async put(obj, val) { if (typeof obj === "string") { mem.set(obj, structuredClone(val)); return; } for (const [k, v] of Object.entries(obj)) mem.set(k, structuredClone(v)); },
    async delete(keys) { for (const k of keys) mem.delete(k); },
  };
  const link = new HubLink({ storage, getWebSockets: () => [] }, {});
  const hubCalls = [];
  const hub = { getByName: () => ({ fetch: (u, init) => { const r = new Request(u, init); hubCalls.push(new URL(r.url).pathname); return link.fetch(r); } }) };

  // Generated feed: newest is a trailer, then a bonus, then full episodes.
  const day = (d) => new Date(Date.UTC(2026, 9, d, 9)).toUTCString();
  const item = (i, d, type = "full", url = `https://pdst.example/redirect/traffic.example/ep${i}.mp3?x=1&amp;y=2`) =>
    `<item><title>Episode ${i} &amp; more</title><guid isPermaLink="false">g-${i}</guid><pubDate>${day(d)}</pubDate>` +
    `<itunes:episodeType>${type}</itunes:episodeType><itunes:duration>${i === 9 ? "00:40:00" : "3600"}</itunes:duration>` +
    `<enclosure url="${url}" length="1" type="audio/mpeg"/><description>${"x".repeat(200)}</description></item>`;
  const feedXml = (n = 12) => `<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>F</title>` +
    item(99, 20, "trailer") + item(98, 19, "bonus") + item(97, 19, "full", "http://insecure.example/x.mp3") +
    Array.from({ length: n }, (_, k) => item(10 - k, 18 - k)).join("") + `</channel></rss>`;
  const streamOf = (text, chunk = 16384) => {
    const bytes = new TextEncoder().encode(text);
    let i = 0, cancelled = false;
    const s = new ReadableStream({
      pull(c) { if (i >= bytes.length) return c.close(); c.enqueue(bytes.slice(i, i += chunk)); },
      cancel() { cancelled = true; },
    });
    return { s, read: () => i, cancelled: () => cancelled, total: bytes.length };
  };
  const feeds = [];
  const fetchFeed = async (url) => { feeds.push(url); return new Response(streamOf(feedXml()).s); };
  const deps = { hub, fetch: fetchFeed };

  const body = (request, ap) => ({
    version: "1.0",
    session: { new: true, sessionId: "s1", application: { applicationId: SKILL } },
    context: { System: { application: { applicationId: SKILL }, device: { deviceId: "amzn1.ask.device.MEDIA" } }, ...(ap ? { AudioPlayer: ap } : {}) },
    request: { requestId: "r" + Math.random(), timestamp: new Date().toISOString(), locale: "en-GB", ...request },
  });
  const call = async (b) => (await (await handle(new Request("https://x/", { method: "POST", body: JSON.stringify(b) }), env, pass, deps)).json());
  const intent = (name, slots) => body({ type: "IntentRequest", intent: { name, slots } });
  const said = (r) => (r.response.outputSpeech?.ssml || "").replace(/<\/?speak>/g, "").replace(/&apos;/g, "'").replace(/&amp;/g, "&");
  const play = (r) => r.response.directives?.[0];

  // --- tokens and library ---
  assert.equal(makeToken("dsr_daily", "0123456789abcdef"), "w1|dsr_daily|0123456789abcdef");
  assert.deepEqual(parseToken("w1|lbc|live"), { entryId: "lbc", key: "live" });
  assert.equal(parseToken("spike-1-123"), null); assert.equal(parseToken("w1|x|nothex"), null);
  assert.equal(LIBRARY.match("deep state radio").id, "dsr_network"); assert.equal(LIBRARY.match("LBC").id, "lbc");
  assert.equal(LIBRARY.match("radio nacional"), null);
  assert.equal(mediaIntentFromText("play the latest Daily Beast").mode, "latest");
  assert.equal(mediaIntentFromText("play the latest Daily Beast").entry.id, "db_podcast");
  assert.equal(mediaIntentFromText("resume DSR Daily").mode, "resume");
  assert.equal(mediaIntentFromText("what's in my library").op, "list");
  assert.equal(mediaIntentFromText("play some jazz"), null);
  assert.equal(mediaIntentFromText("add milk"), null);
  ok("media: token round-trip, library match, text intents");

  // --- RSS early stop on a ~6 MB feed ---
  const big = feedXml(30) + `<!--${"y".repeat(6 * 1024 * 1024)}-->`;
  const st = streamOf(big);
  const t0 = process.cpuUsage();
  const items = await readItems(st.s);
  const cpu = process.cpuUsage(t0); const cpuMs = (cpu.user + cpu.system) / 1000;
  assert.equal(items.length, 30); assert.ok(st.cancelled(), "stream not cancelled");
  assert.ok(st.read() < 1024 * 1024, `read ${st.read()} of ${st.total}`);
  assert.ok(cpuMs < 50, `parse cpu ${cpuMs} ms`);
  const eps = episodes(items);
  assert.equal(eps[0].title, "Episode 10 & more"); assert.ok(eps.every((e) => e.url.startsWith("https://")));
  assert.equal(eps[0].url, "https://pdst.example/redirect/traffic.example/ep10.mp3?x=1&y=2");
  assert.equal(parseDuration("01:02:03"), 3723);
  ok(`media: early-stop parser on a 6 MB feed (${cpuMs.toFixed(1)} ms CPU), skips trailer/bonus/http`);

  // --- play latest podcast ---
  let r = await call(intent("PlayMediaIntent", { media: { name: "media", value: "the daily beast" } }));
  let d = play(r);
  assert.equal(d.type, "AudioPlayer.Play"); assert.equal(d.playBehavior, "REPLACE_ALL");
  assert.equal(d.audioItem.stream.url, "https://pdst.example/redirect/traffic.example/ep10.mp3?x=1&y=2");
  assert.equal(d.audioItem.stream.offsetInMilliseconds, 0);
  const tok = d.audioItem.stream.token;
  assert.match(tok, /^w1\|db_podcast\|[0-9a-f]{16}$/); assert.ok(tok.length < 1024);
  assert.equal(r.response.shouldEndSession, true); assert.match(said(r), /^Playing The Daily Beast Podcast: Episode 10/);
  assert.equal(feeds.at(-1), "https://feeds.megaphone.fm/QCD5580850161");
  ok("media: PlayMediaIntent -> AudioPlayer.Play REPLACE_ALL, enclosure as published, w1 token");

  // --- events: Stopped stores offset (no Started events from Alexa) ---
  r = await call(body({ type: "AudioPlayer.PlaybackStopped", token: tok, offsetInMilliseconds: 1_380_000 }));
  assert.deepEqual(r.response, {});
  const epk = parseToken(tok).key;
  assert.equal(mem.get(`media:resume:${epk}`).offset_ms, 1_380_000);
  r = await call(body({ type: "AudioPlayer.PlaybackNearlyFinished", token: tok, offsetInMilliseconds: 1 }));
  assert.deepEqual(r.response, {});
  r = await call(body({ type: "AudioPlayer.PlaybackFailed", token: tok, error: { type: "MEDIA_ERROR_UNKNOWN", message: "https://secret.example/x" } }));
  assert.deepEqual(r.response, {});
  ok("media: playback events answer with no speech; Stopped stores the resume offset");

  // --- AMAZON.ResumeIntent from context, and from the DO with no context ---
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.ResumeIntent" } },
    { token: tok, offsetInMilliseconds: 1_380_000, playerActivity: "STOPPED" }));
  d = play(r);
  assert.equal(d.audioItem.stream.token, tok); assert.equal(d.audioItem.stream.offsetInMilliseconds, 1_380_000);
  assert.equal(d.audioItem.stream.url, "https://pdst.example/redirect/traffic.example/ep10.mp3?x=1&y=2");
  assert.equal(r.response.outputSpeech, undefined);
  r = await call(intent("AMAZON.ResumeIntent"));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 1_380_000);
  r = await call(body({ type: "PlaybackController.PlayCommandIssued" }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 1_380_000); assert.equal(r.response.outputSpeech, undefined);
  ok("media: Resume (intent and button) re-issues Play at the stored offset");

  // --- "play X" again resumes; "play the latest X" restarts ---
  r = await call(intent("PlayMediaIntent", { media: { value: "Daily Beast" } }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 1_380_000); assert.equal(said(r), "Resuming The Daily Beast Podcast from 23 minutes.");
  r = await call(intent("PlayLatestMediaIntent", { media: { value: "Daily Beast" } }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 0);
  r = await call(intent("PlayMediaIntent", { media: { value: "latest daily beast" } }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 0);
  r = await call(intent("PlayMediaIntent", { media: { value: "x", resolutions: { resolutionsPerAuthority: [{ status: { code: "ER_SUCCESS_MATCH" }, values: [{ value: { id: "db_podcast" } }] }] } } }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 1_380_000);
  // Old console model: the same request arrives as CaptureIntent / Fallback text.
  r = await call(intent("CaptureIntent", { query: { value: "play the latest daily beast" } }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 0);
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.FallbackIntent" }, inputTranscript: "resume the daily beast" }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 1_380_000);
  ok("media: play resumes over 30 s, play the latest restarts, old-model text works");

  // --- Pause / Stop ---
  for (const n of ["AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"]) {
    r = await call(body({ type: "IntentRequest", intent: { name: n } }, { token: tok, offsetInMilliseconds: 5, playerActivity: "PLAYING" }));
    assert.deepEqual(r.response.directives, [{ type: "AudioPlayer.Stop" }]);
  }
  r = await call(body({ type: "PlaybackController.PauseCommandIssued" }));
  assert.deepEqual(r.response.directives, [{ type: "AudioPlayer.Stop" }]);
  // Stop with nothing of ours playing keeps the old answer.
  r = await call(intent("AMAZON.StopIntent"));
  assert.equal(said(r), "OK.");
  ok("media: Pause/Stop/Cancel -> AudioPlayer.Stop; plain Stop unchanged");

  // --- Next (older) / Previous (newer) ---
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.NextIntent" } }, { token: tok, offsetInMilliseconds: 0 }));
  d = play(r); assert.match(d.audioItem.stream.url, /ep9\.mp3/); assert.match(said(r), /Episode 9/);
  const tok9 = d.audioItem.stream.token;
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.PreviousIntent" } }, { token: tok9, offsetInMilliseconds: 0 }));
  assert.match(play(r).audioItem.stream.url, /ep10\.mp3/);
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.PreviousIntent" } }, { token: tok, offsetInMilliseconds: 0 }));
  assert.equal(said(r), "That's the newest The Daily Beast Podcast episode.");
  // Ep 9 is 40 min; an offset past 95 % does not resume.
  await call(body({ type: "AudioPlayer.PlaybackStopped", token: tok9, offsetInMilliseconds: 2_390_000 }));
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.NextIntent" } }, { token: tok, offsetInMilliseconds: 0 }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 0);
  // Finished clears the resume offset.
  await call(body({ type: "AudioPlayer.PlaybackFinished", token: tok, offsetInMilliseconds: 0 }));
  assert.equal(mem.has(`media:resume:${epk}`), false);
  r = await call(intent("PlayMediaIntent", { media: { value: "daily beast" } }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 0);
  ok("media: Next = older episode, Previous = newer, 95 % rule, Finished clears resume");

  // --- radio ---
  r = await call(intent("PlayMediaIntent", { media: { value: "LBC" } }));
  d = play(r); assert.equal(d.audioItem.stream.url, "https://media-ssl.musicradio.com/LBCLondon");
  assert.equal(d.audioItem.stream.token, "w1|lbc|live"); assert.equal(said(r), "Playing LBC.");
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.ResumeIntent" } }, { token: "w1|lbc|live", offsetInMilliseconds: 600000 }));
  assert.equal(play(r).audioItem.stream.offsetInMilliseconds, 0);
  r = await call(body({ type: "IntentRequest", intent: { name: "AMAZON.NextIntent" } }, { token: "w1|lbc|live", offsetInMilliseconds: 0 }));
  assert.equal(play(r).audioItem.stream.token, "w1|bbc_ws|live");
  r = await call(intent("CaptureIntent", { query: { value: "play world service" } }));
  assert.match(play(r).audioItem.stream.url, /\.m3u8$/);
  ok("media: radio plays live (LBC AAC first), resume restarts live, next cycles stations");

  // --- library, unknown names, unsupported ---
  r = await call(intent("ListLibraryIntent"));
  assert.match(said(r), /^Two radio stations and nine podcasts, including/);
  r = await call(intent("AskWhatsIntent", { query: { value: "in my library" } }));
  assert.match(said(r), /^Two radio stations/);
  r = await call(intent("PlayMediaIntent", { media: { value: "jazz fm" } }));
  assert.equal(said(r), "jazz fm isn't in the library yet."); assert.equal(r.response.directives, undefined);
  r = await call(intent("AMAZON.ShuffleOnIntent"));
  assert.equal(said(r), "I can't do that with this stream.");
  r = await call(intent("PlayMediaIntent", { media: { value: "kitchen music" } }));
  assert.equal(said(r), "kitchen music isn't in the library yet.");   // preset disabled until Q1
  ok("media: library summary (new and old model), unknown name, unsupported built-ins");

  // --- DO guards, no identifiers stored ---
  let bad = await link.fetch(new Request("https://hub.internal/media", { method: "POST", body: JSON.stringify({ op: "put", values: { "x": 1 } }) }));
  assert.equal(bad.status, 400);
  bad = await link.fetch(new Request("https://hub.internal/media", { method: "POST", body: JSON.stringify({ op: "drop" }) }));
  assert.equal(bad.status, 400);
  assert.ok(![...mem.keys()].some((k) => k.includes("amzn1")), "raw device id in a key");
  assert.ok(!JSON.stringify([...mem.values()]).includes("amzn1"), "raw device id in a value");
  assert.ok(hubCalls.every((p) => ["/media", "/reqlog", "/dispatch", "/notify"].includes(p)), hubCalls.join());
  ok("media: DO accepts only media: keys; device ids are hashed");

  // --- hub offline: still plays (no DO) ---
  const r2 = await (await handle(new Request("https://x/", { method: "POST", body: JSON.stringify(intent("PlayMediaIntent", { media: { value: "DSR Daily" } })) }),
    env, pass, { fetch: fetchFeed })).json();
  assert.equal(play(r2).type, "AudioPlayer.Play"); assert.equal(feeds.at(-1), "https://feeds.megaphone.fm/TRGM2505689189");
  const fail = await (await handle(new Request("https://x/", { method: "POST", body: JSON.stringify(intent("PlayMediaIntent", { media: { value: "DSR Daily" } })) }),
    env, pass, { fetch: async () => new Response("nope", { status: 503 }) })).json();
  assert.equal(said(fail), "I couldn't get DSR Daily right now.");
  ok("media: plays without DO storage; a failing feed gets a short apology");

  // --- request log ring (read through the bearer route) ---
  storage.list = async ({ prefix }) => new Map([...mem].filter(([k]) => k.startsWith(prefix)));
  const TOKEN = "test-hub-token-0123456789";
  const renv = { HUB_LINK_TOKEN: TOKEN, HUB_LINK: hub };
  const get = (auth, method = "GET") => routeRequests(new Request("https://x/admin/requests", { method, headers: auth ? { Authorization: auth } : {} }), renv);
  assert.equal((await get(null)).status, 401);
  assert.equal((await get("Bearer wrong")).status, 401);
  assert.equal((await get(`Bearer ${TOKEN}`, "POST")).status, 404);
  assert.equal((await routeRequests(new Request("https://x/admin/requests", { headers: { Authorization: "Bearer " } }), { HUB_LINK: hub })).status, 404);
  for (let i = 0; i < 20; i++) await call(intent("ListLibraryIntent"));
  await (await handle(new Request("https://x/", { method: "POST", body: JSON.stringify(intent("PlayMediaIntent", { media: { value: "DSR Daily" } })) }),
    env, pass, { hub, fetch: async () => new Response("nope", { status: 503 }) })).json();
  const logRes = await get(`Bearer ${TOKEN}`);
  assert.equal(logRes.status, 200);
  const log = await logRes.json();
  assert.equal(log.requests.length, REQLOG_MAX);
  const raw = JSON.stringify(log);
  assert.ok(!/amzn1\.|https?:\/\/|w1\|/.test(raw), "identifier, URL or token in the log");
  assert.ok(log.requests.every((e) => /^[0-9a-f]{16}$/.test(e.device) && /Z$/.test(e.at)));
  const last = log.requests.at(-1);
  assert.equal(last.intent, "PlayMediaIntent"); assert.equal(last.error, "feed: feed 503"); assert.equal(last.entry, "dsr_daily");
  assert.equal(last.slots.media, "DSR Daily"); assert.equal(last.reply, "speech");
  const all = log.requests;
  const plays = all.filter((e) => e.reply === "AudioPlayer.Play");
  assert.ok(plays.some((e) => e.stream_host === "media-ssl.musicradio.com" && e.entry === "lbc"));
  assert.ok(plays.some((e) => e.stream_host && e.stream_host.endsWith(".akamaized.net") && e.entry === "bbc_ws"));
  assert.ok(log.requests.some((e) => e.intent === "CaptureIntent" && e.text === "play world service"));
  assert.ok(log.requests.filter((e) => e.intent === "ListLibraryIntent").length >= 20);
  assert.ok(log.media.episodes >= 2 && log.media.last.length >= 1 && log.media.last.every((l) => /^[0-9a-f]{16}$/.test(l.device)));
  ok("request log: last 50 kept, bearer-only read, no ids/tokens/URLs, media summary");

  // Forwarded questions are logged as hub replies.
  const qhub = { getByName: () => ({ fetch: async (u, init) => {
    const r = new Request(u, init); const p = new URL(r.url).pathname;
    if (p === "/reqlog") { qlog.push(await r.json()); return Response.json({ ok: true }); }
    return Response.json({ speech: "Salt.", end_session: true });
  } }) };
  const qlog = [];
  await handle(new Request("https://x/", { method: "POST", body: JSON.stringify(intent("AskHowIntent", { query: { value: "to season soup" } })) }), env, pass, { hub: qhub });
  assert.equal(qlog.length, 1); assert.equal(qlog[0].reply, "hub:hub"); assert.equal(qlog[0].text, "how to season soup");
  assert.equal(qlog[0].speech, "Salt.");
  const bad1 = await handle(new Request("https://x/", { method: "POST", body: "{}" }), env, async () => { throw new Error("sig"); }, { hub: qhub });
  assert.equal(bad1.status, 400); assert.equal(qlog.length, 1);
  ok("request log: forwarded question logged as hub reply; unverified requests are not logged");
}
