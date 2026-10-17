// Alexa skill logic, independent of signature verification (so it can be tested locally).
const MAX_SKEW_S = 150;
const CARRIER_INTENTS = { AddIntent: "add", FindIntent: "find", SaveIntent: "save", RememberIntent: "remember" };

const say = (text, endSession = true, reprompt) => {
  const response = { outputSpeech: { type: "PlainText", text }, shouldEndSession: endSession };
  if (reprompt) response.reprompt = { outputSpeech: { type: "PlainText", text: reprompt } };
  return { version: "1.0", response };
};
const silent = () => ({ version: "1.0", response: {} });
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json;charset=UTF-8" } });
const bad = (why) => new Response("bad request", { status: 400, headers: { "x-reason": why } });

// verify(headers, raw) must throw on failure.
export async function handle(request, env, verify) {
  const url = new URL(request.url);
  if (request.method !== "POST" || (url.pathname !== "/" && url.pathname !== "/alexa"))
    return new Response("not found", { status: 404 });
  // Placeholder "unset" (or anything not a skill id) means: not configured yet -> 400.
  if (!/^amzn1\.ask\.skill\./.test(env.ALEXA_SKILL_ID || "")) return bad("skill id not configured");
  const raw = await request.text();
  try { await verify(request.headers, raw); } catch (e) { return bad("signature"); }
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
  if (name === "CaptureIntent" || name in CARRIER_INTENTS) {
    const q = (req.intent.slots?.query?.value || "").trim();
    if (!q) return json(say("Sorry, I didn't catch that. What do you need?", false, "What do you need?"));
    const text = CARRIER_INTENTS[name] ? `${CARRIER_INTENTS[name]} ${q}` : q;
    const job = {
      v: 1, text, deviceId: body.context?.System?.device?.deviceId || null,
      ts: req.timestamp, requestId: req.requestId, intent: name, locale: req.locale, source: "alexa",
    };
    try {
      await env.QUEUE.send(job, { contentType: "json" });
    } catch (e) {
      return json(say("Womble's not reachable, try again."));
    }
    return json(say("On it."));
  }
  return json(say("Sorry, say it starting with add, find or save.", false, "What do you need?"));
}
