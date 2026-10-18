// Alexa skill logic, independent of signature verification (so it can be tested locally).
import {
  CUTOFF_MS, DEFAULT_REPROMPT, FRAME_LINE, PROGRESSIVE_LINE, PROGRESSIVE_MS,
  hubStub, plainSpeech, toSsml,
} from "./hub_link.js";

const MAX_SKEW_S = 150;
const CARRIER_INTENTS = { AddIntent: "add", FindIntent: "find", SaveIntent: "save", RememberIntent: "remember" };

const say = (text, endSession = true, reprompt) => {
  const response = { outputSpeech: { type: "SSML", ssml: toSsml(text) }, shouldEndSession: endSession };
  if (!endSession && reprompt) response.reprompt = { outputSpeech: { type: "SSML", ssml: toSsml(reprompt) } };
  return { version: "1.0", response };
};
const silent = () => ({ version: "1.0", response: {} });
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json;charset=UTF-8" } });
const bad = (why) => new Response("bad request", { status: 400, headers: { "x-reason": why } });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function sayBrain(answer) {
  const end = !answer || answer.end_session !== false;
  const speech = plainSpeech(answer?.speech);
  const text = speech || (end ? "OK." : "What do you need?");
  if (end) return say(text, true);
  const reprompt = plainSpeech(answer?.reprompt) || DEFAULT_REPROMPT;
  return say(text, false, reprompt);
}

function directivesUrl(apiEndpoint) {
  if (!apiEndpoint || typeof apiEndpoint !== "string") return null;
  let u;
  try { u = new URL(apiEndpoint); } catch { return null; }
  if (u.protocol !== "https:") return null;
  const host = u.hostname.toLowerCase();
  if (host !== "api.amazonalexa.com" && !host.endsWith(".amazonalexa.com")) return null;
  return `${u.origin}/v1/directives`;
}

async function sendProgressive(body, fetchImpl) {
  const sys = body.context?.System || {};
  const url = directivesUrl(sys.apiEndpoint);
  const token = sys.apiAccessToken;
  const requestId = body.request?.requestId;
  if (!url || !token || !requestId) return false;
  try {
    const res = await fetchImpl(url, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        header: { requestId },
        directive: { type: "VoicePlayer.Speak", speech: toSsml(PROGRESSIVE_LINE) },
      }),
    });
    return res.status === 204 || res.ok;
  } catch {
    return false;
  }
}

function squash(text) {
  return String(text ?? "").replace(/\s+/g, " ").trim();
}

// Apostrophe counts as a boundary so "what" matches the start of "what's …".
function phraseAtStart(text, phrase) {
  const fold = (s) => s.toLowerCase().replace(/[\u2018\u2019\u201B\u2032]/g, "'");
  const t = fold(text);
  const p = fold(phrase);
  if (!p || t.length < p.length || !t.startsWith(p)) return false;
  if (t.length === p.length) return true;
  return /[\s,?.!;:'"]/.test(t[p.length]);
}

// AMAZON.SearchQuery cannot share an utterance with another slot, so each
// carrier is its own intent. The worker puts that carrier back in front of
// the query. A query that already begins with it is left unchanged.
export const ASK_CARRIERS = {
  AskWhatIntent: "what",
  AskWhatsIntent: "what's",
  AskWhatIsIntent: "what is",
  AskWhatAboutIntent: "what about",
  AskHowIntent: "how",
  AskHowDoIIntent: "how do I",
  AskHowDoYouIntent: "how do you",
  AskHowLongIntent: "how long",
  AskHowMuchIntent: "how much",
  AskHowManyIntent: "how many",
  AskWhyIntent: "why",
  AskWhenIntent: "when",
  AskWhoIntent: "who",
  AskIsIntent: "is",
  AskAreIntent: "are",
  AskCanIntent: "can",
  AskCouldIntent: "could",
  AskShouldIntent: "should",
  ResearchIntent: "research",
  TellMeAboutIntent: "tell me about",
  TellMeIntent: "tell me",
  AskAndIntent: "and",
};

export function joinQuestion(ask, query) {
  const a = squash(ask);
  const q = squash(query);
  if (!q) return a;
  if (!a || phraseAtStart(q, a)) return q;
  return `${a} ${q}`;
}

function intentText(name, intent) {
  if (name === "AMAZON.YesIntent") return "yes";
  if (name === "AMAZON.NoIntent") return "no";
  if (!(name in ASK_CARRIERS) && name !== "CaptureIntent" && !(name in CARRIER_INTENTS)) return null;
  const q = (intent?.slots?.query?.value || "").trim();
  if (!q) return "";
  if (name in ASK_CARRIERS) return joinQuestion(ASK_CARRIERS[name], q);
  if (CARRIER_INTENTS[name]) return `${CARRIER_INTENTS[name]} ${q}`;
  return q;
}

async function enqueue(env, job) {
  try {
    await env.QUEUE.send(job, { contentType: "json" });
    return true;
  } catch {
    return false;
  }
}

function unreachable() {
  return json(say("Womble's not reachable, try again."));
}

// Hub offline: today's path. Hub connected but past the cutoff: frame line, and
// the queue still gets the job so AlexaPuller can show it. The hub dedupes on requestId.
async function speakJob(body, req, env, name, text, deps) {
  const fetchImpl = deps.fetch || globalThis.fetch;
  const progressiveMs = deps.progressiveMs ?? PROGRESSIVE_MS;
  const cutoffMs = deps.cutoffMs ?? CUTOFF_MS;
  const deviceId = body.context?.System?.device?.deviceId || null;
  const sessionId = body.session?.sessionId || null;
  const queueJob = {
    v: 1, text, deviceId, ts: req.timestamp, requestId: req.requestId,
    intent: name, locale: req.locale, source: "alexa", sessionId,
  };
  const hubJob = {
    id: req.requestId,
    text,
    deviceId,
    sessionId,
    locale: req.locale || null,
    deadline: Date.now() + cutoffMs,
  };
  const stub = hubStub(deps.hub ?? env.HUB_LINK);
  const started = Date.now();
  if (!stub) {
    return (await enqueue(env, queueJob)) ? json(say("On it.")) : unreachable();
  }

  const dispatchP = stub.fetch("https://hub.internal/dispatch", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(hubJob),
  }).then(async (r) => {
    try { return await r.json(); } catch { return { offline: true }; }
  }).catch(() => ({ offline: true }));

  let done = false;
  const timer = setTimeout(() => {
    if (done) return;
    sendProgressive(body, fetchImpl).catch(() => {});
  }, progressiveMs);

  let outcome;
  try {
    outcome = await Promise.race([
      dispatchP.then((answer) => ({ kind: "hub", answer })),
      sleep(cutoffMs).then(() => ({ kind: "cutoff" })),
    ]);
  } finally {
    done = true;
    clearTimeout(timer);
  }

  const answer = outcome.kind === "hub" ? outcome.answer : null;
  // Duration only. Spoken text is never logged.
  const why = answer?.offline ? "offline" : answer?.timeout ? "timeout" : outcome.kind;
  console.log(`alexa hub wait_ms=${Date.now() - started} outcome=${why}`);
  if (answer && answer.offline) {
    return (await enqueue(env, queueJob)) ? json(say("On it.")) : unreachable();
  }
  if (answer && !answer.timeout && Object.prototype.hasOwnProperty.call(answer, "speech"))
    return json(sayBrain(answer));

  // Cutoff, or the hub accepted the job and did not answer in time.
  // Enqueue so the frame still updates if the live reply never lands.
  if (!(await enqueue(env, queueJob))) return unreachable();
  return json(say(FRAME_LINE));
}

// verify(headers, raw) must throw on failure.
// deps is test-only (hub stub, clock overrides, fetch). The deployed entry does not pass it.
export async function handle(request, env, verify, deps = {}) {
  const url = new URL(request.url);
  if (request.method !== "POST" || (url.pathname !== "/" && url.pathname !== "/alexa"))
    return new Response("not found", { status: 404 });
  // Placeholder "unset" (or anything not a skill id) means: not configured yet -> 400.
  if (!/^amzn1\.ask\.skill\./.test(env.ALEXA_SKILL_ID || "")) return bad("skill id not configured");
  const raw = await request.text();
  try { await verify(request.headers, raw); } catch { return bad("signature"); }
  let body;
  try { body = JSON.parse(raw); } catch { return bad("json"); }
  const req = body.request || {};
  const ts = Date.parse(req.timestamp);
  if (!Number.isFinite(ts) || Math.abs(Date.now() - ts) > MAX_SKEW_S * 1000) return bad("timestamp");
  const appId = body.session?.application?.applicationId ?? body.context?.System?.application?.applicationId;
  if (appId !== env.ALEXA_SKILL_ID) return bad("application id");

  switch (req.type) {
    case "LaunchRequest":
      return json(say("Hi, what do you need?", false, "What do you need? Start with add, find or save."));
    case "SessionEndedRequest":
      return json(silent());
    case "IntentRequest":
      break;
    default:
      return json(silent());
  }
  const name = req.intent?.name;
  if (name === "AMAZON.HelpIntent")
    return json(say("Say what you need, starting with add, find or save. For example, add milk.", false, "What do you need?"));
  if (name === "AMAZON.StopIntent" || name === "AMAZON.CancelIntent" || name === "AMAZON.NavigateHomeIntent")
    return json(say("OK."));
  if (name === "AMAZON.FallbackIntent")
    return json(say("Sorry, say it starting with add, find or save.", false, "What do you need?"));
  const text = intentText(name, req.intent);
  if (text === null)
    return json(say("Sorry, say it starting with add, find or save.", false, "What do you need?"));
  if (!text) return json(say("Sorry, I didn't catch that. What do you need?", false, "What do you need?"));
  return speakJob(body, req, env, name, text, deps);
}
