"""Offline tests for the Alexa live link. No Keychain, no Cloudflare.

  python tests/test_alexa_link.py
"""
import asyncio, json, logging, os, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alexa_link as al
import alexa_pull as ap
from aiohttp import WSMsgType, web

TOKEN = "test-hub-token"


class FakeDisp:
    def __init__(self):
        self.events = []

    async def emit(self, ev, data=None, corr=None, **k):
        self.events.append((ev, data))

    async def push_card(self, card):
        self.events.append(("card", card))


def route(text):
    if text.startswith("add "):
        return {"category": "list.add", "speech": f"Added {text[4:]} to shopping.",
                "card": {"type": "list", "title": "Shopping", "items": [text[4:]], "highlight": text[4:]}}
    return {"category": "fallback", "speech": "Sorry, I can only do lists for now.",
            "card": {"type": "text", "title": "Heard", "body": text}}


class Brain:
    def __init__(self):
        self.calls = []

    async def __call__(self, text, source, session):
        self.calls.append({"text": text, "source": source, "session": session})
        end = not str(text).rstrip().endswith("?")
        return type("Reply", (), {
            "speech": "Use yogurt.",
            "end_session": end,
            "reprompt": None if end else "Want the method?",
            "card": None,
            "category": "brain",
        })()


def link(tmp, brain=None):
    tmp.mkdir(parents=True, exist_ok=True)
    brain = brain or Brain()
    disp = FakeDisp()
    seen = ap.Seen(tmp / "seen.json")
    devices = ap.Devices(tmp / "dev.json")
    obj = al.AlexaLink(disp, route, brain, disp.push_card, devices=devices, seen=seen, recent={})
    return obj, disp, brain, seen


def job(**kw):
    base = {"id": "r1", "text": "a substitute for buttermilk", "deviceId": "dev-a",
            "sessionId": "sess-9", "locale": "en-GB", "deadline": 0}
    base.update(kw)
    return base


async def test_protocol_and_dedupe(tmp):
    obj, disp, brain, seen = link(tmp)
    reply = await obj.handle_job(job())
    assert reply == {"id": "r1", "speech": "Use yogurt.", "end_session": True}, reply
    assert brain.calls == [{
        "text": "a substitute for buttermilk", "source": "alexa:Echo 1", "session": "alexa:sess-9",
    }], brain.calls
    assert any(ev == "reply" and data.get("body") == "Use yogurt." and data.get("source") == "Echo 1"
               for ev, data in disp.events), disp.events
    assert "r1" in seen
    again = await obj.handle_job(job())
    assert again["speech"] == "Use yogurt." and len(brain.calls) == 1
    replies = [ev for ev, _ in disp.events if ev == "reply"]
    assert len(replies) == 1, disp.events
    print("PASS link answers through the brain, dedupes the same requestId")

    # Puller shares Seen, so the queued copy does not paint the frame again.
    pull = ap.AlexaPuller(disp, route, disp.push_card, devices=obj.devices, seen=seen)
    body = json.dumps({"text": "a substitute for buttermilk", "deviceId": "dev-a", "requestId": "r1"})
    acks, retries = await pull.handle_batch([{
        "body": body, "lease_id": "L1", "id": "L1", "attempts": 1, "timestamp_ms": time.time() * 1000,
    }])
    assert [a["lease_id"] for a in acks] == ["L1"] and not retries
    assert len([ev for ev, _ in disp.events if ev == "reply"]) == 1
    print("PASS AlexaPuller skips a requestId the link already claimed")

    listed = await obj.handle_job(job(id="r2", text="add milk", sessionId="sess-9"))
    assert listed["speech"] == "Added milk to shopping." and listed["end_session"] is True
    assert len(brain.calls) == 1, brain.calls
    assert disp.events[-1][0] == "card" and disp.events[-1][1]["highlight"] == "milk"
    print("PASS router hit does not call the brain and still updates the frame")

    devs = json.loads((tmp / "dev.json").read_text())
    devs["dev-a"].update(room="Kitchen", name="Kitchen Echo")
    (tmp / "dev.json").write_text(json.dumps(devs))
    asked = await obj.handle_job(job(id="r3", text="what about for scones?", sessionId=None))
    assert asked["end_session"] is False and asked["reprompt"] == "Want the method?", asked
    assert brain.calls[-1]["session"] == "alexa:Kitchen" and brain.calls[-1]["source"] == "alexa:Kitchen"
    print("PASS follow-up keeps the session open; missing sessionId falls back to alexa:<room>")

    os.environ["ALEXA_LINK_TEST_DELAY_S"] = "0.05"
    try:
        t0 = time.perf_counter()
        slow = await obj.handle_job(job(id="r4", text="for scones", sessionId="sess-9"))
        elapsed = time.perf_counter() - t0
    finally:
        os.environ.pop("ALEXA_LINK_TEST_DELAY_S", None)
    assert slow["speech"] == "Use yogurt." and elapsed >= 0.05, elapsed
    print("PASS ALEXA_LINK_TEST_DELAY_S holds the answer")


def test_url_and_token(tmp):
    assert al.worker_ws_url("https://worker.example/hub") == "wss://worker.example/hub"
    assert al.worker_ws_url("wss://worker.example") == "wss://worker.example/hub"
    assert al.worker_ws_url("https://user:secret@worker.example/hub") == ""
    assert al.worker_ws_url("https://worker.example/alexa") == ""
    assert al.worker_ws_url("") == ""
    print("PASS worker URL is wss /hub only, with no userinfo")

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = "from-keychain\n"
        return R()

    old = os.environ.get("HUB_LINK_TOKEN")
    user = os.environ.get("USER")
    os.environ.pop("HUB_LINK_TOKEN", None)
    os.environ["USER"] = "tester"
    real = al.subprocess.run
    al.subprocess.run = fake_run
    try:
        assert al.read_token() == "from-keychain"
        assert calls and calls[0][:4] == ["security", "find-generic-password", "-s", "womble-hub-link"]
        assert "-a" in calls[0] and "tester" in calls[0] and "-w" in calls[0]
        os.environ["HUB_LINK_TOKEN"] = "from-env"
        calls.clear()
        assert al.read_token() == "from-env" and calls == []
    finally:
        al.subprocess.run = real
        if old is None:
            os.environ.pop("HUB_LINK_TOKEN", None)
        else:
            os.environ["HUB_LINK_TOKEN"] = old
        if user is None:
            os.environ.pop("USER", None)
        else:
            os.environ["USER"] = user
    print("PASS token comes from the env or Keychain item womble-hub-link")


def test_enabled_flag():
    prev = os.environ.get("ALEXA_LINK")
    os.environ.pop("ALEXA_LINK", None)
    obj = al.AlexaLink(FakeDisp(), route, Brain(), FakeDisp().push_card)
    assert obj.start() is None and obj.task is None
    if prev is None:
        os.environ.pop("ALEXA_LINK", None)
    else:
        os.environ["ALEXA_LINK"] = prev
    print("PASS ALEXA_LINK defaults off")


async def test_reconnect(tmp):
    state = {"sockets": [], "inbox": [], "connected": asyncio.Event(), "answered": asyncio.Event()}

    async def handler(request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.Response(status=401, text="unauthorized")
        if request.headers.get("Origin"):
            return web.Response(status=403, text="origin")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state["sockets"].append(ws)
        state["connected"].set()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                state["inbox"].append(msg.data)
                if msg.data not in ("ping", "pong"):
                    state["answered"].set()
        return ws

    app = web.Application()
    app.router.add_get("/hub", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    prev = {k: os.environ.get(k) for k in ("ALEXA_LINK", "ALEXA_WORKER_URL", "HUB_LINK_TOKEN")}
    os.environ["ALEXA_LINK"] = "1"
    os.environ["ALEXA_WORKER_URL"] = f"ws://localhost:{port}/hub"
    os.environ["HUB_LINK_TOKEN"] = TOKEN
    real_backoff = al.backoff_s
    al.backoff_s = lambda attempt: 0.05
    logs = []
    handler_log = logging.Handler()
    handler_log.emit = lambda record: logs.append(record.getMessage())
    logging.getLogger("alexa.link").addHandler(handler_log)
    obj, disp, brain, seen = link(tmp / "re")
    obj.heartbeat_s = 0.05
    obj.jitter = False
    obj.token_retry = 0.05
    task = obj.start()
    try:
        assert task is not None
        await asyncio.wait_for(state["connected"].wait(), 2)
        await state["sockets"][-1].send_str(json.dumps(job(id="live-1", text="what's a substitute for buttermilk")))
        await asyncio.wait_for(state["answered"].wait(), 2)
        answers = [json.loads(m) for m in state["inbox"] if m not in ("ping", "pong")]
        assert answers and answers[0]["id"] == "live-1" and answers[0]["speech"] == "Use yogurt."
        assert answers[0]["end_session"] is True
        ping_deadline = time.perf_counter() + 1
        while time.perf_counter() < ping_deadline and "ping" not in state["inbox"]:
            await asyncio.sleep(0.02)
        assert "ping" in state["inbox"]
        print("PASS websocket protocol: job in, answer out, heartbeat ping")

        state["connected"].clear()
        state["answered"].clear()
        await state["sockets"][-1].close()
        await asyncio.wait_for(state["connected"].wait(), 2)
        assert len(state["sockets"]) >= 2
        await state["sockets"][-1].send_str(json.dumps(job(id="live-2", text="what about for scones?", sessionId="sess-9")))
        await asyncio.wait_for(state["answered"].wait(), 2)
        answers = [json.loads(m) for m in state["inbox"] if m.startswith("{")]
        assert any(a["id"] == "live-2" and a["end_session"] is False for a in answers), answers
        assert all(TOKEN not in line for line in logs)
        print("PASS reconnects after the socket drops, and the token is not logged")
    finally:
        task.cancel()
        with_cancel = asyncio.gather(task, return_exceptions=True)
        await with_cancel
        al.backoff_s = real_backoff
        logging.getLogger("alexa.link").removeHandler(handler_log)
        for key, val in prev.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        await runner.cleanup()


async def main():
    tmp = Path(tempfile.mkdtemp())
    test_url_and_token(tmp)
    test_enabled_flag()
    await test_protocol_and_dedupe(tmp)
    await test_reconnect(tmp / "sock")
    assert al.backoff_s(0) == 0.5 and al.backoff_s(10) == 30.0
    print("PASS reconnect backoff grows and caps")
    print("ALL PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
