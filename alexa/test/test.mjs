// Local tests. The "bypass" here is a stub verifier passed to handle(); it is never in the deployed entrypoint.
import "reflect-metadata";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as x509 from "@peculiar/x509";
import { handle } from "../src/app.js";
import { verifyAlexaRequest, verifyChain, checkCertUrl } from "../src/verify.js";

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
let s = await speech(r); assert.equal(s.outputSpeech.text, "Hi, what do you need?"); assert.equal(s.shouldEndSession, false); ok("Launch -> 'Hi, what do you need?', session open");
r = await handle(post(body({ type: "IntentRequest", intent: { name: "AddIntent", slots: { query: { name: "query", value: "milk" } } } })), env, passVerify);
s = await speech(r); assert.equal(s.outputSpeech.text, "On it."); assert.equal(sent.at(-1).text, "add milk");
assert.deepEqual(Object.keys(sent.at(-1)).slice(0, 5), ["v", "text", "deviceId", "ts", "requestId"]); ok("AddIntent -> queue {text:'add milk',deviceId,ts,requestId} + 'On it.'");
r = await handle(post(body({ type: "IntentRequest", intent: { name: "CaptureIntent", slots: { query: { value: "what time is sunset" } } } })), env, passVerify);
assert.equal(sent.at(-1).text, "what time is sunset"); ok("CaptureIntent passes raw text");
for (const [i, txt, end] of [["AMAZON.HelpIntent", null, false], ["AMAZON.StopIntent", "OK.", true], ["AMAZON.CancelIntent", "OK.", true]]) {
  s = await speech(await handle(post(body({ type: "IntentRequest", intent: { name: i } })), env, passVerify));
  assert.equal(s.shouldEndSession, end); if (txt) assert.equal(s.outputSpeech.text, txt);
} ok("Help / Stop / Cancel");
r = await handle(post(body({ type: "SessionEndedRequest", reason: "USER_INITIATED" })), env, passVerify); assert.equal(r.status, 200); ok("SessionEnded -> 200");
r = await handle(post(body({ type: "LaunchRequest" }, "amzn1.ask.skill.other")), env, passVerify); assert.equal(r.status, 400); ok("wrong applicationId -> 400");
const old = body({ type: "LaunchRequest" }); old.request.timestamp = new Date(Date.now() - 151e3).toISOString();
r = await handle(post(old), env, passVerify); assert.equal(r.status, 400); ok("timestamp 151 s old -> 400");
r = await handle(post(body({ type: "IntentRequest", intent: { name: "AddIntent", slots: { query: { value: "eggs" } } } })),
  { ...env, QUEUE: { send: async () => { throw new Error("down"); } } }, passVerify);
assert.match((await speech(r)).outputSpeech.text, /not reachable/); ok("queue failure -> 'not reachable'");
console.log(`ALL ${n} PASSED`);
