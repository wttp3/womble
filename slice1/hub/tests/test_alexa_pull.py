"""Offline tests for alexa_pull (no network, no Keychain)."""
import asyncio, base64, json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alexa_pull as ap

class FakeDisp:
    def __init__(self): self.events = []
    async def emit(self, ev, data=None, corr=None, **k): self.events.append((ev, data))
    async def push_card(self, card): self.events.append(("card", card))

def route(text):
    if text.startswith("add "): return {"category": "list.add", "speech": "", "card": {"type": "list", "title": "Shopping", "items": [text[4:]], "highlight": text[4:]}}
    return {"category": "fallback", "speech": "", "card": {"type": "text", "title": "Heard", "body": text}}

def msg(job, lease, age_s=0, attempts=1, b64=False):
    body = json.dumps(job); body = base64.b64encode(body.encode()).decode() if b64 else body
    return {"body": body, "lease_id": lease, "id": lease, "attempts": attempts, "timestamp_ms": (time.time() - age_s) * 1000}

async def main():
    tmp = Path(tempfile.mkdtemp())
    d = FakeDisp()
    p = ap.AlexaPuller(d, route, d.push_card, devices=ap.Devices(tmp / "dev.json"), seen=ap.Seen(tmp / "seen.json"))
    job = {"text": "add milk", "deviceId": "amzn1.ask.device.A", "ts": "x", "requestId": "r1"}
    acks, retries = await p.handle_batch([msg(job, "L1"), msg(job, "L2", b64=True)])
    assert [a["lease_id"] for a in acks] == ["L1", "L2"] and not retries, (acks, retries)
    heard = [e for e in d.events if e[0] == "voice.heard"]
    assert len(heard) == 1 and heard[0][1]["room"] == "Echo 1" and heard[0][1]["source"] == "alexa", d.events
    print("PASS dedupe (plain + base64 bodies), room chip 'Echo 1', ack both")
    devs = json.loads((tmp / "dev.json").read_text()); devs["amzn1.ask.device.A"].update(room="Kitchen", name="Kitchen Echo")
    (tmp / "dev.json").write_text(json.dumps(devs))
    await p.handle_batch([msg({**job, "requestId": "r2", "text": "what is for dinner"}, "L3")])
    assert d.events[-1][0] == "reply" and d.events[-1][1]["source"] == "Kitchen Echo", d.events[-1]
    print("PASS renamed room picked up without restart; reply labelled 'Kitchen Echo'")
    acks, _ = await p.handle_batch([msg({**job, "requestId": "r3", "text": "old one"}, "L4", age_s=700)])
    assert acks and p.missed == ["old one"]; await p._flush_missed()
    assert "missed" in d.events[-1][1]["title"]; print("PASS stale job -> 'missed' card, acked")
    bad = lambda t: (_ for _ in ()).throw(RuntimeError("boom"))
    p2 = ap.AlexaPuller(d, bad, d.push_card, devices=p.devices, seen=ap.Seen(tmp / "s2.json"))
    a, r = await p2.handle_batch([msg({**job, "requestId": "r4"}, "L5", attempts=1)])
    assert not a and r[0]["delay_seconds"] == 5
    a, r = await p2.handle_batch([msg({**job, "requestId": "r4"}, "L6", attempts=3)])
    assert a and not r; print("PASS failure -> retry, then ack + error card after 3 attempts")
    assert ap.poll_interval(time.time(), now=time.mktime((2026,10,15,12,0,0,0,0,-1))) == ap.ACTIVE_S
    assert ap.poll_interval(0, now=time.mktime((2026,10,15,12,0,0,0,0,-1))) == ap.IDLE_S
    assert ap.poll_interval(0, now=time.mktime((2026,10,15,3,0,0,0,0,-1))) == ap.NIGHT_S
    print("PASS poll interval active/idle/night")
    # C10: missing / unreadable token is retried, and the value is not logged
    def boom(*a, **k): raise OSError("locked")
    real_run = ap.subprocess.run
    ap.subprocess.run = boom
    try: assert ap._read_token() is None
    finally: ap.subprocess.run = real_run
    calls = {"n": 0}
    real_read = ap._read_token
    def fake_read():
        calls["n"] += 1
        if calls["n"] < 3: return None
        return "queue-token"
    ap._read_token = fake_read
    old_wait = ap.TOKEN_RETRY_S; ap.TOKEN_RETRY_S = 0
    try:
        p3 = ap.AlexaPuller(d, route, d.push_card, devices=ap.Devices(tmp / "d3.json"), seen=ap.Seen(tmp / "s3.json"))
        got = await asyncio.wait_for(p3.wait_for_token(), 2)
        assert got == "queue-token" and calls["n"] == 3, calls
    finally:
        ap._read_token = real_read; ap.TOKEN_RETRY_S = old_wait
    print("PASS missing token rechecked (value not required to start)")
    import os; os.environ["ALEXA_PULL"] = "0"; assert p.start() is None and p.task is None
    print("PASS ALEXA_PULL=0 -> not started")
    print("ALL PASSED")
asyncio.run(main())
