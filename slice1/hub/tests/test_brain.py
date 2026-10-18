"""Brain fall-through, offline line, print guard, memory, empty transcript, VAD tail.

No network and no Keychain. Run from anywhere:

  python tests/test_brain.py
"""
import asyncio, json, logging, os, sys, tempfile, time, types
from pathlib import Path

os.environ.setdefault("BOX3_NOISE_PSK", "unused")
os.environ.setdefault("HUB_HOST_IP", "127.0.0.1")
os.environ.setdefault("BOX3_HOST", "127.0.0.1")
os.environ.setdefault("TTS_ENGINE", "say")
os.environ.pop("XAI_BRAIN_API_KEY", None)
os.environ.pop("VAD_TRAIL_SIL_S", None)
os.environ.pop("BRAIN_WEB_SEARCH", None)
os.environ.pop("BRAIN_MEMORY_TURNS", None)
os.environ.pop("BRAIN_MODEL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
mw = types.ModuleType("mlx_whisper")
mw.transcribe = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mlx stub"))
sys.modules["mlx_whisper"] = mw

import numpy as np
import lists as lists_mod
import hub
import brain.core as brain_core
from brain.core import OFFLINE, Brain, BrainHTTPError, load_persona
from brain import key as brain_key
from brain import tools as brain_tools
from brain.core import requests as brain_requests

FAKE_KEY = "brain-key-do-not-log"


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL " + msg)


class Disp:
    def __init__(self):
        self.events = []

    async def emit(self, ev, data=None, **k):
        self.events.append((ev, data))


def say(text):
    async def transport(payload, key, timeout):
        yield {"type": "response.output_text.delta", "delta": text}
        yield {"type": "response.completed", "response": {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ]}}
    return transport


def brain(store, disp=None, transport=None, clock=None, timeout=2):
    return Brain(store, disp, transport=transport or say("Yes."), key_getter=lambda: FAKE_KEY,
                 clock=clock, timeout=timeout)


async def main():
    tmp = Path(tempfile.mkdtemp())
    store = lists_mod.ListStore(tmp / "lists.json")
    disp = Disp()

    os.environ.pop("BRAIN_TIMEOUT_S", None)
    check(Brain(store).timeout == 8.0, "default timeout")
    os.environ["BRAIN_TIMEOUT_S"] = "2.5"
    check(Brain(store).timeout == 2.5, "BRAIN_TIMEOUT_S")
    os.environ.pop("BRAIN_TIMEOUT_S", None)

    # --- print guard ---
    quiet = brain_tools.ToolContext("what's on the shopping list", "box3-kitchen", "box3-kitchen", store, disp)
    refused = await brain_tools.print_list({"list": "shopping"}, quiet)
    check(refused["error"] == brain_tools.REFUSED, "print without the word")
    finger = brain_tools.ToolContext("fingerprint the glass", "box3-kitchen", "box3-kitchen", store, disp)
    check((await brain_tools.print_text({"title": "x", "body": "y"}, finger))["error"] == brain_tools.REFUSED,
          "fingerprint is not print")
    asked = brain_tools.ToolContext("please print the shopping list", "box3-kitchen", "box3-kitchen", store, disp)
    stub = await brain_tools.print_list({"list": "shopping"}, asked)
    check(stub["error"] == brain_tools.NOT_AVAILABLE, "print asked but printer is not wired")
    check(not brain_tools.allows_print("printing is nice") and brain_tools.allows_print("Print that"),
          "word boundary")
    print("PASS print guard")

    # --- list and frame tools ---
    ctx = brain_tools.ToolContext("we need oats", "box3-kitchen", "box3-kitchen", store, disp)
    added = await brain_tools.list_add({"item": "oats", "list": "shopping"}, ctx)
    check(added["ok"] and added["items"] == ["oats"] and ctx.card["highlight"] == "oats", "list_add")
    read = await brain_tools.list_read({"list": "to-do"}, ctx)
    check(read["items"] == [] and read["list"] == "to-do", "list_read empty to-do")
    await brain_tools.frame_notify({"text": "kettle"}, ctx)
    check(disp.events[-1] == ("toast", {"text": "kettle", "kind": "info"}), "frame_notify")
    await brain_tools.frame_show({"title": "Note", "body": "Hello"}, ctx)
    check(any(ev == "reply" and d.get("body") == "Hello" for ev, d in disp.events), "frame_show")
    print("PASS list and frame tools")

    # --- router fall-through reaches the brain; lists and record do not ---
    payloads = []

    async def recording(payload, key, timeout):
        payloads.append(payload)
        check(key == FAKE_KEY, "transport saw a different key")
        check(FAKE_KEY not in json.dumps(payload), "key leaked into the body")
        async for ev in say("Soup is on.")(payload, key, timeout):
            yield ev

    hub.LISTS = store
    hub.BRAIN = brain(store, disp, transport=recording)
    res = await hub.route("what should we cook")
    check(res["category"] == "brain" and res["speech"] == "Soup is on.", res["speech"])
    check(res["speech"] != "Sorry, I can only do lists for now.", "old fallback line")
    check(len(payloads) == 1, "brain was not called")
    body = payloads[0]
    check(body["model"] == "grok-4.7" and body["store"] is False, "model settings")
    check(body["reasoning"] == {"effort": "low"} and body["stream"] is True, "reasoning and stream")
    check(body["instructions"] == load_persona(), "persona is not verbatim")
    names = [t.get("name") for t in body["tools"]]
    check(all(t.get("type") != "web_search" for t in body["tools"]), "general question offered web search")
    check("list_add" in names and "list_read" in names and "print_list" in names, names)
    check(all(t.get("type") != "web_search" for t in brain_tools.responses_tools(text="How do I boil an egg?")),
          "egg offered web search")
    check(all(t.get("type") != "web_search" for t in brain_tools.responses_tools(text="How do I slice a pomegranate?")),
          "pomegranate offered web search")
    check(brain_tools.responses_tools(text="what's the weather today")[0] == {"type": "web_search"}, "weather")
    check(brain_tools.responses_tools(text="who won today")[0] == {"type": "web_search"}, "who won")
    check(brain_tools.realtime_tools()[0] == {"type": "web_search"}, "realtime drops search")
    check(brain_tools.realtime_tools() == brain_tools.responses_tools(), "realtime schemas")
    os.environ["BRAIN_WEB_SEARCH"] = "0"
    try:
        off = brain_tools.responses_tools(text="what's the weather today")
        check(all(t.get("type") != "web_search" for t in off), "BRAIN_WEB_SEARCH=0")
    finally:
        os.environ.pop("BRAIN_WEB_SEARCH", None)
    before = len(payloads)
    for phrase, cat in (
        ("add milk", "list.add"),
        ("what's on the shopping list", "list.show"),
        ("record for andrew", "record.start"),
    ):
        got = await hub.route(phrase)
        check(got["category"] == cat, f"{phrase} -> {got['category']}")
    check(len(payloads) == before, "list or record called the brain")
    check((await hub.route("   "))["category"] == "empty", "blank route")
    check(len(payloads) == before, "blank called the brain")
    print("PASS router fall-through reaches the brain; lists and record do not")

    # --- offline, timeout, circuit breaker ---
    calls = {"n": 0}

    async def down(payload, key, timeout):
        calls["n"] += 1
        raise BrainHTTPError(503)
        yield {}

    clock = {"t": 1000.0}
    offline = Brain(store, disp, transport=down, key_getter=lambda: None, clock=lambda: clock["t"], timeout=2)
    hub.BRAIN = offline
    disp.events.clear()
    res = await hub.route("tell me a joke")
    check(res["speech"] == OFFLINE and res["category"] == "brain.offline", res["speech"])
    check(calls["n"] == 0, "missing key still called xAI")
    check(any(ev == "error" and d.get("code") == "brain" for ev, d in disp.events), disp.events)

    calls["n"] = 0
    clock["t"] = 1000.0
    flaky = Brain(store, disp, transport=down, key_getter=lambda: FAKE_KEY, clock=lambda: clock["t"], timeout=2)
    for i in range(3):
        res = await flaky.handle("again")
        check(res.speech == OFFLINE, f"failure {i}")
    check(calls["n"] == 3, str(calls["n"]))
    res = await flaky.handle("while open")
    check(res.speech == OFFLINE and calls["n"] == 3, "open circuit still called xAI")
    clock["t"] += 61
    res = await flaky.handle("after cooldown")
    check(calls["n"] == 4 and res.speech == OFFLINE, "breaker did not close")

    async def hang(payload, key, timeout):
        calls["n"] += 1
        await asyncio.sleep(5)
        yield {"type": "response.output_text.delta", "delta": "too late"}

    calls["n"] = 0
    slow = Brain(store, disp, transport=hang, key_getter=lambda: FAKE_KEY, timeout=0.4)
    for i in range(3):
        res = await slow.handle("slow")
        check(res.speech == OFFLINE, f"timeout {i}")
    check(calls["n"] == 3 and not slow.breaker.is_open() and slow.breaker.fails == 0,
          "timeouts counted toward the breaker")
    res = await slow.handle("after timeouts")
    check(calls["n"] == 4 and res.speech == OFFLINE, "breaker skipped xAI after timeouts")

    calls["n"] = 0

    async def mixed(payload, key, timeout):
        calls["n"] += 1
        if calls["n"] == 3:
            await asyncio.sleep(5)
            yield {}
            return
        raise BrainHTTPError(503)
        yield {}

    mixed_brain = Brain(store, disp, transport=mixed, key_getter=lambda: FAKE_KEY, timeout=0.4)
    await mixed_brain.handle("a")
    await mixed_brain.handle("b")
    await mixed_brain.handle("c")
    check(mixed_brain.breaker.fails == 2 and not mixed_brain.breaker.is_open(),
          "timeout reset hard failures")
    await mixed_brain.handle("d")
    check(mixed_brain.breaker.is_open() and calls["n"] == 4, "third http failure")

    async def req_timeout(payload, key, timeout):
        calls["n"] += 1
        raise brain_requests.exceptions.Timeout("read")
        yield {}

    calls["n"] = 0
    rt = Brain(store, disp, transport=req_timeout, key_getter=lambda: FAKE_KEY, timeout=2)
    for _ in range(3):
        res = await rt.handle("rt")
        check(res.speech == OFFLINE, "requests timeout")
    check(calls["n"] == 3 and not rt.breaker.is_open(), "requests timeout opened the breaker")
    print("PASS offline, timeout, circuit breaker")

    # --- first sentence is available before the stream ends ---
    seen = []

    async def two(payload, key, timeout):
        yield {"type": "response.reasoning_summary_text.delta", "delta": "THINKING SECRET"}
        yield {"type": "response.output_text.delta", "delta": "Hello. "}
        seen.append("after-first")
        yield {"type": "response.output_text.delta", "delta": "More soon."}
        yield {"type": "response.completed", "response": {}}

    streamed = Brain(store, disp, transport=two, key_getter=lambda: FAKE_KEY, timeout=2)
    reply = await streamed.handle("hi", on_sentence=lambda s: seen.append(("s", s)))
    check(seen[0] == ("s", "Hello.") and seen[1] == "after-first", seen)
    check(reply.speech == "Hello. More soon." and "THINKING" not in reply.speech, reply.speech)
    check(reply.llm_ttft_ms is not None and reply.llm_total_ms is not None, "timings")
    ask = Brain(store, disp, transport=say("Want tea?"), key_getter=lambda: FAKE_KEY, timeout=2)
    q = await ask.handle("drink")
    check(q.end_session is False and q.reprompt == "Anything else?", (q.end_session, q.reprompt))
    print("PASS streaming first sentence")

    # --- tool loop, reasoning echoed once, not kept in memory ---
    rounds = []

    async def tools(payload, key, timeout):
        rounds.append(payload)
        if len(rounds) == 1:
            yield {"type": "response.completed", "response": {"output": [
                {"type": "reasoning", "id": "rs", "encrypted_content": "cipher-blob"},
                {"type": "function_call", "call_id": "c1", "name": "list_add",
                 "arguments": json.dumps({"item": "bread", "list": "shopping"})},
            ]}}
        else:
            yield {"type": "response.output_text.delta", "delta": "Added bread."}
            yield {"type": "response.completed", "response": {}}

    tool_brain = Brain(store, disp, transport=tools, key_getter=lambda: FAKE_KEY, timeout=2)
    reply = await tool_brain.handle("remember bread", session="box3-kitchen")
    check(reply.speech == "Added bread." and reply.card["highlight"] == "bread", reply.speech)
    check("bread" in store.show("shopping"), store.show("shopping"))
    second = rounds[1]["input"]
    check(any(i.get("type") == "reasoning" and i.get("encrypted_content") == "cipher-blob" for i in second),
          "reasoning not passed back")
    check(any(i.get("type") == "function_call_output" and "bread" in i.get("output", "") for i in second),
          "tool output missing")
    check(all("cipher-blob" not in (i.get("content") or "") for i in tool_brain.memory.history("box3-kitchen")),
          "reasoning stored in memory")
    print("PASS tool loop")

    # --- memory: 6 turns, 5 minute expiry, separate session keys ---
    clock = {"t": 5000.0}
    mem_payloads = []

    async def mem_transport(payload, key, timeout):
        mem_payloads.append(payload["input"])
        yield {"type": "response.output_text.delta", "delta": "Yes."}
        yield {"type": "response.completed", "response": {}}

    mem = Brain(store, disp, transport=mem_transport, key_getter=lambda: FAKE_KEY,
                clock=lambda: clock["t"], timeout=2)
    await mem.handle("one", session="box3-kitchen")
    clock["t"] += 200
    await mem.handle("two", session="box3-kitchen")
    check(any(i.get("content") == "one" for i in mem_payloads[1]), "history dropped early")
    other = await mem.handle("side", session="alexa:Kitchen")
    check(other.speech == "Yes.", other.speech)
    check(not any(i.get("content") == "one" for i in mem_payloads[2]), "sessions shared memory")
    clock["t"] += 299
    await mem.handle("still", session="box3-kitchen")
    check(any(i.get("content") == "one" for i in mem_payloads[3]), "expired inside 5 minutes")
    clock["t"] += 301
    await mem.handle("later", session="box3-kitchen")
    check(not any(i.get("content") == "one" for i in mem_payloads[4]), "memory did not expire")
    check(mem_payloads[4][-1]["content"] == "later", mem_payloads[4])
    for n in range(8):
        await mem.handle(f"extra {n}", session="cap")
    check(len(mem.memory.history("cap")) == 12, len(mem.memory.history("cap")))
    print("PASS memory expiry")

    # --- real SSE parser, mocked HTTP ---
    class FakeResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self, decode_unicode=True):
            yield 'data: {"type":"response.output_text.delta","delta":"Hi."}'
            yield 'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"Hi."}]}]}}'
            yield "data: [DONE]"

    def fake_post(url, **kw):
        check(url == "https://api.x.ai/v1/responses", url)
        check(kw["headers"]["Authorization"] == "Bearer " + FAKE_KEY, "auth header")
        check(kw["json"]["model"] == "grok-4.7" and kw["json"]["store"] is False, "sse body")
        check(FAKE_KEY not in json.dumps(kw["json"]), "key in json")
        return FakeResp()

    real_post = brain_requests.post
    real_families = brain_core._families
    brain_core._families = lambda host, port=443: "ipv4=1,ipv6=0"
    brain_requests.post = fake_post
    try:
        live = Brain(store, disp, key_getter=lambda: FAKE_KEY, timeout=2)
        reply = await live.handle("ping")
        check(reply.speech == "Hi." and reply.category == "brain", reply.speech)
    finally:
        brain_requests.post = real_post
    print("PASS mocked responses stream")

    # Headers came back, one token, then the socket sat there. The timeout log
    # has to name that stage and keep the token time, without the key.
    def stall_post(url, **kw):
        class R:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_lines(self, decode_unicode=True):
                yield 'data: {"type":"response.output_text.delta","delta":"Hi. "}'
                time.sleep(5)
                if False:
                    yield ""

        return R()

    lines = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    blog = logging.getLogger("hub.brain")
    blog.addHandler(handler)
    brain_requests.post = stall_post
    os.environ["HTTPS_PROXY"] = "http://secret-user:secret-pass@proxy.example:8080"
    try:
        stalled = Brain(store, disp, key_getter=lambda: FAKE_KEY, timeout=0.5)
        reply = await stalled.handle("How do I slice a pomegranate?")
        blob = "\n".join(lines)
        check(reply.speech == OFFLINE and reply.llm_ttft_ms is not None, (reply.speech, reply.llm_ttft_ms, blob))
        check(not stalled.breaker.is_open() and stalled.breaker.fails == 0, "stall counted")
        check("stage=first-token" in blob and "http=200" in blob, blob)
        check("dns=ipv4=1,ipv6=0" in blob and "key=keychain" in blob, blob)
        check("proxy=http://proxy.example:8080" in blob, blob)
        check("loop_pulses=" in blob and "loop_gap_ms=" in blob, blob)
        check(FAKE_KEY not in blob and "secret-user" not in blob and "secret-pass" not in blob, "secret in the failure log")
    finally:
        os.environ.pop("HTTPS_PROXY", None)
        brain_requests.post = real_post
        brain_core._families = real_families
        blog.removeHandler(handler)
    print("PASS stall log names the stage")

    class Client:
        def __init__(self):
            self.ev = []

        def send_voice_assistant_event(self, ev, data):
            self.ev.append((ev.name, data))

    # --- BOX-3 finish() speaks the brain line and records llm timings ---
    hub.TIMING_LOG = tmp / "timing.jsonl"
    said = []

    def fake_tts(text):
        p = tmp / f"t{len(said)}.wav"
        p.write_bytes(b"wav")
        said.append(text)
        return p

    hub.tts = fake_tts
    hub.synthesize = lambda text: (np.ones(4800, np.float32) * 0.1, 48000)
    hub.BRAIN = brain(store, disp, transport=say("Soup is on."))
    hub.transcribe = lambda pcm: "what is soup"
    c = Client()
    s = hub.Session(c)
    await s.on_start("", 0, None, "womble")
    s.buf.extend(b"\x00\x01" * 40)
    await s.finish()
    start = [d for n, d in c.ev if n == "VOICE_ASSISTANT_TTS_START"]
    check(start and start[-1]["text"] == "Soup is on.", start)
    check("Sorry, I can only do lists for now." not in said, said)
    rec = json.loads(hub.TIMING_LOG.read_text().splitlines()[-1])
    check(rec["category"] == "brain" and "llm_ttft_ms" in rec and "llm_total_ms" in rec, rec)
    print("PASS finish() speaks the brain and logs llm timings")

    # --- empty transcript is silent ---
    said.clear()
    hub.transcribe = lambda pcm: ""
    events = []

    async def capture(ev, data=None, **k):
        events.append((ev, data))

    hub.disp.emit = capture

    c = Client()
    s = hub.Session(c)
    await s.on_start("", 0, None, "womble")
    s.buf.extend(b"\x00\x01" * 40)
    await s.finish()
    names = [n for n, _ in c.ev]
    check("VOICE_ASSISTANT_TTS_END" not in names and "VOICE_ASSISTANT_TTS_START" not in names, names)
    check("VOICE_ASSISTANT_RUN_END" in names, names)
    check(said == [], said)
    check(("voice.cancel", {"reason": "no_speech"}) in events or any(ev == "voice.cancel" for ev, _ in events), events)
    rec = json.loads(hub.TIMING_LOG.read_text().splitlines()[-1])
    check(rec["category"] == "empty" and rec["text"] == "", rec)
    print("PASS empty transcript is silent")

    # --- VAD tail (C1). A mid-sentence pause must not end the turn. ---
    def pcm(ms, amp):
        n = int(16000 * ms / 1000)
        return np.full(n, amp, np.int16).tobytes()

    def fresh():
        s = hub.Session(None)
        s.speech_start = None
        s.last_voice = None
        s.peak = 0.0
        s.sm = []
        s.rms_log = []
        s.chunk_sizes = set()
        hub.Session.noise_floor = 30.0
        return s

    def feed(s, raw):
        ended = False
        for i in range(0, len(raw), 1024):
            if s.feed(raw[i:i + 1024]):
                ended = True
                break
        return ended

    old = hub.Session.TRAIL_SIL_S
    try:
        check(old >= 1.0, f"default trail {old} is still the old 0.65 s cutoff")
        s = fresh()
        check(feed(s, pcm(1600, 4000)) is False, "speech ended early")
        check(feed(s, pcm(800, 0)) is False, "0.8 s pause cut the speaker off")
        check(feed(s, pcm(800, 0)) is True and s.end_reason == "silence", s.end_reason)
        hub.Session.TRAIL_SIL_S = 0.65
        s = fresh()
        check(feed(s, pcm(1600, 4000)) is False, "short-trail speech")
        check(feed(s, pcm(800, 0)) is True, "VAD_TRAIL_SIL_S=0.65 did not restore the old cutoff")
    finally:
        hub.Session.TRAIL_SIL_S = old
    print("PASS VAD tail")

    # --- keychain read, value never logged ---
    grab = logging.Handler()
    lines = []
    grab.emit = lambda record: lines.append(record.getMessage())
    logging.getLogger("hub.brain").addHandler(grab)
    os.environ["USER"] = "tester"
    seen = {"n": 0}

    def hit(cmd, **kw):
        seen["n"] += 1
        seen["cmd"] = cmd
        class R:
            returncode = 0
            stdout = FAKE_KEY + "\n"
        return R()

    real_run = brain_key.subprocess.run
    brain_key._key = None
    brain_key._checked_at = None
    try:
        brain_key.subprocess.run = hit
        got = brain_key.get_key()
        check(got == FAKE_KEY, "key mismatch")
        check(seen["cmd"][seen["cmd"].index("-s") + 1] == "womble-brain-xai", seen["cmd"])
        check(seen["cmd"][seen["cmd"].index("-a") + 1] == "tester", "account")
        check(not any(FAKE_KEY in line for line in lines), "key was logged")
        check(brain_key.get_key() == FAKE_KEY and seen["n"] == 1, "cache")
    finally:
        brain_key.subprocess.run = real_run
        brain_key._key = None
        brain_key._checked_at = None
        logging.getLogger("hub.brain").removeHandler(grab)
    print("PASS keychain read")
    print("test_brain OK")


if __name__ == "__main__":
    asyncio.run(main())
