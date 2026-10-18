// Local tests. The "bypass" here is a stub verifier passed to handle(); it is never in the deployed entrypoint.
import "reflect-metadata";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as x509 from "@peculiar/x509";
import worker, { HubLink } from "../src/index.js";
import { handle } from "../src/app.js";
import { verifyAlexaRequest, verifyChain, checkCertUrl } from "../src/verify.js";
import { authorizeHub, routeHub, tokensEqual, toSsml, FRAME_LINE, PROGRESSIVE_LINE, STALE_MS, HUB_OBJECT_NAME } from "../src/hub_link.js";

const SKILL = "amzn1.ask.skill.test-0000";
// placeholder check below
const sent = [];
const env = { ALEXA_SKILL_ID: SKILL, QUEUE: { send: async (m, o) => { assert.equal(o.contentType, "json"); sent.push(m); } } };
const passVerify = async () => {};
const realVerify = (h, raw) => verifyAlexaRequest(h, raw);
let n = 0; const ok = (name) => console.log(`PASS ${++n} ${name}`);

const body = (request, appId = SKILL) => ({
  version: "1.0",
  session: { new: true, sessionId: "s1", application: { applicationId: appId } },
  context: { System: { application: { applicationId: appId }, device: { deviceId: "amzn1.ask.device.TEST" } } },
  request: { requestId: "amzn1.echo-api.request.r" + Math.random(), timestamp: new Date().toISOString(), locale: "en-GB", ...request },
});
const post = (b, headers = {}) => new Request("https://x.workers.dev/", { method: "POST", body: JSON.stringify(b), headers });
const speech = async (r) => (await r.json()).response;
const said = (resp) => {
  const o = resp.outputSpeech;
  if (!o) return "";
  if (o.type === "PlainText") return o.text;
  return o.ssml.replace(/^<speak>/, "").replace(/<\/speak>$/, "")
    .replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", "\"").replaceAll("&apos;", "'").replaceAll("&amp;", "&");
};

// --- negative: real verifier ---
let r = await handle(post(body({ type: "LaunchRequest" })), env, realVerify);
assert.equal(r.status, 400); ok("unsigned request -> 400");
r = await handle(post(body({ type: "LaunchRequest" }), { SignatureCertChainUrl: "https://evil.example.com/echo.api/c.pem", "Signature-256": "AAAA" }), env, realVerify);
assert.equal(r.status, 400); ok("forged cert URL -> 400");
r = await handle(post(body({ type: "LaunchRequest" })), { ...env, ALEXA_SKILL_ID: "" }, passVerify);
assert.equal(r.status, 400); ok("skill id unset -> 400");
for (const [u, good] of [
  ["https://s3.amazonaws.com/echo.api/echo-api-cert.pem", true], ["https://s3.amazonaws.com:443/echo.api/echo-api-cert.pem", true],
  ["https://s3.amazonaws.com/echo.api/../echo.api/echo-api-cert.pem", true], ["https://S3.AMAZONAWS.COM/echo.api/x.pem", true],
  ["https://s3.amazonaws.com/EcHo.aPi/x.pem", false], ["http://s3.amazonaws.com/echo.api/x.pem", false],
  ["https://s3.amazonaws.com/invalid.path/x.pem", false], ["https://s3.amazonaws.com:563/echo.api/x.pem", false],
  ["https://s3.amazonaws.com/echo.api/../invalid.path/x.pem", false], ["https://s3.amazonaws.com.evil.com/echo.api/x.pem", false],
]) { let g = true; try { checkCertUrl(u); } catch { g = false; } assert.equal(g, good, u); }
ok("cert URL rules (Amazon's examples)");

// --- cert chain: Amazon's real (expired 2023) chain ---
const certs = readFileSync(new URL("./echo-api-cert-12.pem", import.meta.url), "utf8")
  .match(/-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g).map((p) => new x509.X509Certificate(p));
await verifyChain(certs, new Date("2023-06-01T00:00:00Z")); ok("real Amazon chain verifies at a date it was valid");
await assert.rejects(verifyChain(certs, new Date())); ok("same chain rejected today (leaf expired)");
await assert.rejects(verifyChain([certs[0]], new Date("2023-06-01T00:00:00Z"))); ok("leaf without intermediate rejected");
await assert.rejects(verifyChain(certs.slice(1), new Date("2023-06-01T00:00:00Z"))); ok("chain whose leaf lacks the SAN rejected");
// Self-signed impostor with the right SAN
const alg = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256", publicExponent: new Uint8Array([1, 0, 1]), modulusLength: 2048 };
const keys = await crypto.subtle.generateKey(alg, true, ["sign", "verify"]);
const fake = await x509.X509CertificateGenerator.createSelfSigned({ serialNumber: "01", name: "CN=echo-api.amazon.com",
  notBefore: new Date(Date.now() - 864e5), notAfter: new Date(Date.now() + 864e5), keys, signingAlgorithm: alg,
  extensions: [new x509.SubjectAlternativeNameExtension([{ type: "dns", value: "echo-api.amazon.com" }])] });
await assert.rejects(verifyChain([fake])); ok("self-signed impostor with correct SAN rejected");
// Full forged request signed by the impostor key, served from a mocked "s3" URL
const b = JSON.stringify(body({ type: "LaunchRequest" }));
const sig = Buffer.from(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", keys.privateKey, new TextEncoder().encode(b))).toString("base64");
const h = new Headers({ SignatureCertChainUrl: "https://s3.amazonaws.com/echo.api/forged.pem", "Signature-256": sig });
await assert.rejects(verifyAlexaRequest(h, b, async () => new Response(fake.toString("pem")))); ok("request signed by impostor cert rejected");

// --- positive path with stub verifier (test-only) ---
r = await handle(post(body({ type: "LaunchRequest" })), env, passVerify);
let s = await speech(r); assert.equal(said(s), "Hi, what do you need?"); assert.equal(s.shouldEndSession, false); ok("Launch -> 'Hi, what do you need?', session open");
r = await handle(post(body({ type: "IntentRequest", intent: { name: "AddIntent", slots: { query: { name: "query", value: "milk" } } } })), env, passVerify);
s = await speech(r); assert.equal(said(s), "On it."); assert.equal(sent.at(-1).text, "add milk");
assert.deepEqual(Object.keys(sent.at(-1)).slice(0, 5), ["v", "text", "deviceId", "ts", "requestId"]); ok("AddIntent -> queue {text:'add milk',deviceId,ts,requestId} + 'On it.'");
r = await handle(post(body({ type: "IntentRequest", intent: { name: "CaptureIntent", slots: { query: { value: "what time is sunset" } } } })), env, passVerify);
assert.equal(sent.at(-1).text, "what time is sunset"); ok("CaptureIntent passes raw text");
for (const [i, txt, end] of [["AMAZON.HelpIntent", null, false], ["AMAZON.StopIntent", "OK.", true], ["AMAZON.CancelIntent", "OK.", true]]) {
  s = await speech(await handle(post(body({ type: "IntentRequest", intent: { name: i } })), env, passVerify));
  assert.equal(s.shouldEndSession, end); if (txt) assert.equal(said(s), txt);
} ok("Help / Stop / Cancel");
r = await handle(post(body({ type: "SessionEndedRequest", reason: "USER_INITIATED" })), env, passVerify); assert.equal(r.status, 200); ok("SessionEnded -> 200");
r = await handle(post(body({ type: "LaunchRequest" }, "amzn1.ask.skill.other")), env, passVerify); assert.equal(r.status, 400); ok("wrong applicationId -> 400");
const old = body({ type: "LaunchRequest" }); old.request.timestamp = new Date(Date.now() - 151e3).toISOString();
r = await handle(post(old), env, passVerify); assert.equal(r.status, 400); ok("timestamp 151 s old -> 400");
r = await handle(post(body({ type: "IntentRequest", intent: { name: "AddIntent", slots: { query: { value: "eggs" } } } })),
  { ...env, QUEUE: { send: async () => { throw new Error("down"); } } }, passVerify);
assert.match(said(await speech(r)), /not reachable/); ok("queue failure -> 'not reachable'");

// --- slice 2: hub link, timers, session flags ---
assert.equal(await tokensEqual("same-token", "same-token"), true);
assert.equal(await tokensEqual("nope", "same-token"), false);
assert.equal(await tokensEqual("short", "much-longer-secret-value"), false);
assert.equal(await tokensEqual("", ""), false);
assert.equal(await tokensEqual("x", ""), false);
ok("hub token compare is exact and rejects empty");

const ssml = toSsml(`5 < 6 & "ok"`);
assert.match(ssml, /^<speak>.*<\/speak>$/);
assert.match(ssml, /5 &lt; 6 &amp; &quot;ok&quot;/);
assert.ok(toSsml("y".repeat(9000)).length < 8000, "speech cap");
ok("speech is plain SSML, escaped, under 8000 characters");

const TOKEN = "hub-link-test-token";
const forwarded = [];
const linkEnv = {
  HUB_LINK_TOKEN: TOKEN,
  HUB_LINK: {
    getByName(name) {
      assert.equal(name, HUB_OBJECT_NAME);
      return { fetch(req) { forwarded.push(req); return new Response("upgraded", { status: 200, headers: { "x-hub": "1" } }); } };
    },
  },
};
const hubReq = (headers) => new Request("https://x.workers.dev/hub", { headers });
let hr = await worker.fetch(hubReq({ Upgrade: "websocket", Authorization: `Bearer ${TOKEN}` }), linkEnv);
assert.equal(hr.status, 200); assert.equal(hr.headers.get("x-hub"), "1"); assert.equal(forwarded.length, 1);
hr = await worker.fetch(hubReq({ Upgrade: "websocket", Authorization: "Bearer wrong" }), linkEnv);
assert.equal(hr.status, 401); assert.equal(forwarded.length, 1);
hr = await worker.fetch(hubReq({ Upgrade: "websocket", Authorization: `Bearer ${TOKEN}`, Origin: "https://evil.example" }), linkEnv);
assert.equal(hr.status, 403); assert.equal(forwarded.length, 1);
hr = await worker.fetch(hubReq({ Authorization: `Bearer ${TOKEN}` }), linkEnv);
assert.equal(hr.status, 404);
hr = await worker.fetch(new Request("https://x.workers.dev/other", { headers: { Upgrade: "websocket" } }), linkEnv);
assert.equal(hr.status, 404);
hr = await routeHub(hubReq({ Upgrade: "websocket", Authorization: `Bearer ${TOKEN}`, Origin: "https://x.workers.dev" }), linkEnv);
assert.equal(hr.status, 200);
assert.equal(await authorizeHub(new Request("https://x.workers.dev/nope", { headers: { Upgrade: "websocket" } }), TOKEN) instanceof Response, true);
ok("DO route: /hub websocket only, bearer token, other origins rejected");

class FakeWS {
  constructor() { this.readyState = 1; this.sent = []; this.attachment = null; this.tags = []; }
  send(m) { this.sent.push(m); }
  close() { this.readyState = 3; }
  serializeAttachment(a) { this.attachment = a; }
  deserializeAttachment() { return this.attachment; }
}
class FakeCtx {
  constructor() { this.sockets = []; this.auto = null; this.autoAt = null; }
  acceptWebSocket(ws, tags) { ws.tags = tags || []; this.sockets.push(ws); }
  getWebSockets(tag) { return this.sockets.filter((ws) => !tag || ws.tags?.includes(tag)); }
  setWebSocketAutoResponse(pair) { this.auto = pair; }
  getWebSocketAutoResponseTimestamp(ws) { return ws.autoAt ?? this.autoAt; }
}
const ctx = new FakeCtx();
const link = new HubLink(ctx, { HUB_LINK_TOKEN: TOKEN });
const reqOf = (pair) => pair?.request ?? pair?.getRequest?.();
const resOf = (pair) => pair?.response ?? pair?.getResponse?.();
assert.equal(reqOf(ctx.auto), "ping"); assert.equal(resOf(ctx.auto), "pong");
ok("hibernation auto ping/pong is set");

let off = await link.fetch(new Request("https://hub.internal/dispatch", { method: "POST", body: JSON.stringify({ id: "j0", text: "hi" }) }));
assert.deepEqual(await off.json(), { offline: true });
const ws = new FakeWS();
link._attach(ws);
assert.equal(ws.tags.includes("hub"), true);
assert.ok(Math.abs(ws.attachment.connectedAt - Date.now()) < 2000);
const ws2 = new FakeWS();
link._attach(ws2);
assert.equal(ws.readyState, 3); assert.equal(link.freshSocket(), ws2);
ws.attachment = { connectedAt: Date.now() - STALE_MS - 1000 }; ws.readyState = 1; ws.autoAt = null;
ctx.autoAt = null;
ws2.attachment = { connectedAt: Date.now() - STALE_MS - 5000 };
assert.equal(link.freshSocket(), null);
ws2.autoAt = new Date();
assert.equal(link.freshSocket(), ws2);
ok("socket replaced, stale heartbeat is offline, fresh auto-response counts");

const ws3 = new FakeWS();
link._attach(ws3);
const jobBody = { id: "job-1", text: "a substitute for buttermilk", deviceId: "dev", sessionId: "sess", locale: "en-GB", deadline: Date.now() + 2000 };
const pending = link.fetch(new Request("https://hub.internal/dispatch", { method: "POST", body: JSON.stringify(jobBody) }));
await new Promise((r) => setTimeout(r, 0));
assert.equal(ws3.sent.length, 1);
const pushed = JSON.parse(ws3.sent[0]);
assert.deepEqual(pushed, jobBody);
await link.webSocketMessage(ws3, "ping");
await link.webSocketMessage(ws3, JSON.stringify({ id: "other", speech: "nope", end_session: true }));
assert.equal(link.pending.has("job-1"), true);
await link.webSocketMessage(ws3, JSON.stringify({ id: "job-1", speech: "Use yogurt.", end_session: false, reprompt: "Want the method?" }));
const matched = await (await pending).json();
assert.equal(matched.speech, "Use yogurt.");
assert.equal(matched.end_session, false);
assert.equal(matched.reprompt, "Want the method?");
ok("DO matches the answer to the job id");

function fakeHub({ delayMs = 0, connected = true, answer }) {
  const seen = [];
  const ns = {
    getByName() {
      return {
        async fetch(input, init) {
          const req = input instanceof Request ? input : new Request(input, init);
          const body = await req.json();
          seen.push(body);
          if (!connected) return Response.json({ offline: true });
          if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
          return Response.json(typeof answer === "function" ? answer(body) : answer);
        },
      };
    },
  };
  return { ns, seen };
}
const alexaBody = (request, extra = {}) => body({
  type: "IntentRequest",
  ...request,
});
const withApi = (b) => {
  b.context.System.apiEndpoint = "https://api.eu.amazonalexa.com";
  b.context.System.apiAccessToken = "atk-test";
  return b;
};
const qBefore = sent.length;

{
  const hub = fakeHub({ connected: false });
  const b = withApi(alexaBody({ intent: { name: "QuestionIntent", slots: { query: { value: "a substitute for buttermilk" } } } }));
  r = await handle(post(b), env, passVerify, { hub: hub.ns, progressiveMs: 500, cutoffMs: 800, fetch: async () => { throw new Error("progressive should not run"); } });
  s = await speech(r);
  assert.equal(said(s), "On it.");
  assert.equal(s.shouldEndSession, true);
  assert.equal(sent.at(-1).text, "a substitute for buttermilk");
  assert.equal(sent.at(-1).sessionId, "s1");
  assert.equal(hub.seen[0].text, "a substitute for buttermilk");
  assert.equal(hub.seen[0].sessionId, "s1");
  ok("hub offline -> enqueue + 'On it.'");
}

{
  const calls = [];
  const hub = fakeHub({
    delayMs: 0,
    answer: { speech: "Use yogurt or a splash of milk.", end_session: true },
  });
  const b = withApi(body({ type: "IntentRequest", intent: { name: "QuestionIntent", slots: { query: { value: "a substitute for buttermilk" } } } }));
  const before = sent.length;
  r = await handle(post(b), env, passVerify, {
    hub: hub.ns, progressiveMs: 500, cutoffMs: 800,
    fetch: async () => { calls.push("progressive"); return new Response(null, { status: 204 }); },
  });
  s = await speech(r);
  assert.equal(said(s), "Use yogurt or a splash of milk.");
  assert.equal(s.shouldEndSession, true);
  assert.equal(s.reprompt, undefined);
  assert.equal(sent.length, before);
  assert.equal(calls.length, 0);
  assert.equal(hub.seen[0].deviceId, "amzn1.ask.device.TEST");
  assert.equal(hub.seen[0].locale, "en-GB");
  assert.equal(typeof hub.seen[0].deadline, "number");
  assert.ok(hub.seen[0].id);
  ok("hub answer spoken, session ended, queue not used");
}

{
  const hub = fakeHub({
    delayMs: 0,
    answer: { speech: "Want the method too?", end_session: false, reprompt: "Want the method?" },
  });
  const b = body({ type: "IntentRequest", intent: { name: "QuestionIntent", slots: { query: { value: "and for scones" } } } });
  r = await handle(post(b), env, passVerify, { hub: hub.ns, progressiveMs: 500, cutoffMs: 800 });
  s = await speech(r);
  assert.equal(said(s), "Want the method too?");
  assert.equal(s.shouldEndSession, false);
  assert.equal(said(s.reprompt), "Want the method?");
  ok("end_session false keeps the mic open with the brain's reprompt");
}

{
  const hub = fakeHub({ delayMs: 0, answer: { speech: "Noted.", end_session: false } });
  const b = body({ type: "IntentRequest", intent: { name: "AMAZON.YesIntent" } });
  r = await handle(post(b), env, passVerify, { hub: hub.ns, progressiveMs: 400, cutoffMs: 800 });
  s = await speech(r);
  assert.equal(hub.seen[0].text, "yes");
  assert.equal(s.shouldEndSession, false);
  assert.equal(said(s.reprompt), "Anything else for the kitchen?");
  const no = body({ type: "IntentRequest", intent: { name: "AMAZON.NoIntent" } });
  const hubNo = fakeHub({ delayMs: 0, answer: { speech: "OK.", end_session: true } });
  r = await handle(post(no), env, passVerify, { hub: hubNo.ns, progressiveMs: 400, cutoffMs: 800 });
  assert.equal(hubNo.seen[0].text, "no");
  assert.equal((await speech(r)).shouldEndSession, true);
  ok("Yes/No go to the hub; a missing reprompt gets a short one");
}

{
  const calls = [];
  const hub = fakeHub({
    delayMs: 80,
    answer: { speech: "Yogurt works in scones.", end_session: true },
  });
  const b = withApi(body({ type: "IntentRequest", intent: { name: "QuestionIntent", slots: { query: { value: "for scones" } } } }));
  const before = sent.length;
  r = await handle(post(b), env, passVerify, {
    hub: hub.ns, progressiveMs: 20, cutoffMs: 200,
    fetch: async (url, opts) => {
      calls.push({ url, body: JSON.parse(opts.body), auth: opts.headers.authorization });
      return new Response(null, { status: 204 });
    },
  });
  s = await speech(r);
  assert.equal(said(s), "Yogurt works in scones.");
  assert.equal(sent.length, before);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.eu.amazonalexa.com/v1/directives");
  assert.equal(calls[0].auth, "Bearer atk-test");
  assert.equal(calls[0].body.directive.type, "VoicePlayer.Speak");
  assert.match(calls[0].body.directive.speech, new RegExp(`<speak>${PROGRESSIVE_LINE}</speak>`));
  assert.equal(calls[0].body.header.requestId, b.request.requestId);
  ok("progressive 'One moment…' at the first timer, then the real answer");
}

{
  const calls = [];
  const hub = fakeHub({
    delayMs: 250,
    answer: { speech: "too late", end_session: true },
  });
  const b = withApi(body({ type: "IntentRequest", intent: { name: "AddIntent", slots: { query: { value: "milk" } } } }));
  const before = sent.length;
  r = await handle(post(b), env, passVerify, {
    hub: hub.ns, progressiveMs: 20, cutoffMs: 60,
    fetch: async () => { calls.push(1); return new Response(null, { status: 204 }); },
  });
  s = await speech(r);
  assert.match(said(s), /I'll put it on the kitchen frame/);
  assert.equal(said(s), FRAME_LINE);
  assert.equal(s.shouldEndSession, true);
  assert.equal(calls.length, 1);
  assert.equal(sent.length, before + 1);
  assert.equal(sent.at(-1).text, "add milk");
  assert.equal(sent.at(-1).requestId, b.request.requestId);
  ok("cutoff speaks the frame line, enqueues, and has already said one moment");
}
assert.ok(sent.length >= qBefore);
console.log(`ALL ${n} PASSED`);
