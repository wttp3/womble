"""frame/f1: PaintStats (push-to-paint with clock offset, rolling percentiles, tap feedback)
and the /ws paint_ack + clock.pong handling. No network. python tests/test_paint.py"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import display
from display import PaintStats, pct


def check(c, m):
    if not c: raise SystemExit(f"FAIL {m}")


ps = PaintStats(window=5)
ws = object()
# frame clock runs 10 s ahead of the hub; net one-way 5 ms
ps.on_pong(ws, t0=1000.0, t1=11005.0, t2=1010.0)
check(abs(ps.offset[ws][0] - 10000) < 0.01, ps.offset)
ps.on_send("a", "toast", 2000.0)
out = ps.on_ack(ws, {"id": "a", "t_recv": 12005.0, "t_paint": 12040.0}, now_ms=2045.0)
check(out["push_to_paint"] == 40 and out["render"] == 35 and out["net"] == 5 and out["rtt"] == 45, out)
check(ps.on_ack(ws, {"id": "a", "t_recv": 1, "t_paint": 2}, 3) is None, "ack counted once")
check(ps.on_ack(ws, {"id": "zz", "t_recv": 1, "t_paint": 2}, 3) is None, "unknown id")
ps.on_send("b", "toast", 3000.0)
check(ps.on_ack(ws, {"id": "b", "t_recv": "x"}, 3010) is None, "bad ack ignored")
# a noisier pong does not replace a cleaner one
ps.on_pong(ws, 5000.0, 15500.0, 5400.0)
check(abs(ps.offset[ws][0] - 10000) < 0.01, "kept clean offset")
# no offset: push_to_paint falls back to the hub round trip (upper bound)
ws2 = object(); ps.on_send("c", "idle", 100.0)
check(ps.on_ack(ws2, {"id": "c", "t_recv": 5, "t_paint": 25}, 180.0)["push_to_paint"] == 80, "fallback")
for v in (10, 20, 30, 40, 50, 60, 70):
    ps.on_send(str(v), "reply", 0.0); ps.on_ack(ws, {"id": str(v), "t_recv": 10000.0, "t_paint": 10000.0 + v}, v)
sm = ps.summary()
check(sm["push_to_paint"]["n"] == 5 and sm["push_to_paint"]["max"] == 70 and sm["push_to_paint"]["p50"] == 50, sm)
ps.on_tap_fb(18); ps.on_tap_fb("bad"); ps.on_tap_fb(-3)
check(ps.summary()["tap_fb"]["n"] == 1, "tap fb")
check("push_to_paint n=5" in ps.line(), ps.line())
check(pct([], 50) is None and pct([3, 1, 2], 95) == 3, "pct")
ps.reset(); check(ps.summary()["push_to_paint"]["n"] == 0, "reset")
print("PASS PaintStats")


async def ws_flow():
    from aiohttp import web
    from aiohttp.test_utils import TestServer, TestClient
    d = display.Display(lists_loader=lambda: {})
    async def nowx(): return None
    d.get_weather = nowx
    app = web.Application(); app.router.add_get("/ws", d.ws_handler); app.router.add_get("/api/paint", d.api_paint)
    async with TestClient(TestServer(app)) as c:
        ws = await c.ws_connect("/ws")
        snap = json.loads((await ws.receive()).data)
        check(snap["event"] == "snapshot" and "page_ver" in snap["data"], "snapshot + page_ver")
        ping = json.loads((await ws.receive()).data)
        check(ping["event"] == "clock.ping", ping)
        await ws.send_str(json.dumps({"event": "clock.pong", "t0": ping["data"]["t0"], "t1": time.time() * 1000}))
        await ws.send_str(json.dumps({"event": "paint_ack", "id": snap["id"], "t_recv": time.time() * 1000, "t_paint": time.time() * 1000 + 12}))
        await d.emit("toast", {"text": "hi"})
        m = json.loads((await ws.receive()).data)
        await ws.send_str(json.dumps({"event": "paint_ack", "id": m["id"], "t_recv": time.time() * 1000, "t_paint": time.time() * 1000 + 8}))
        await ws.send_str(json.dumps({"event": "tap", "action": "nothing.here", "fb_ms": 21}))
        await asyncio.sleep(0.1)
        r = await (await c.get("/api/paint")).json()
        check(r["push_to_paint"]["n"] == 2 and r["tap_fb"]["n"] == 1 and "toast" in r["by_event"], r)
        r = await (await c.get("/api/paint?reset=1")).json()
        r = await (await c.get("/api/paint")).json()
        check(r["push_to_paint"]["n"] == 0, "reset via api")
        await ws.close()
    print("PASS /ws paint_ack, clock.pong, tap fb_ms, /api/paint")

asyncio.run(ws_flow())
print("ALL PASS test_paint")
