"""Origin allow-list, bind hosts, and supervise(). No MLX, no browser."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web, ClientError
from aiohttp.test_utils import TestClient, TestServer
import display as D

def check(cond, msg):
    if not cond: raise SystemExit("FAIL " + msg)

async def main():
    check(D.http_hosts("192.168.0.70") == ["192.168.0.70", "127.0.0.1"], "lan + loopback")
    check(D.http_hosts("127.0.0.1") == ["127.0.0.1"], "loopback only once")
    check("0.0.0.0" not in D.http_hosts("192.168.0.70"), "not all interfaces")
    origins = D.allowed_ws_origins("192.168.0.70", 8080)
    check(origins == {"", "http://192.168.0.70:8080", "http://127.0.0.1:8080", "http://localhost:8080"}, str(origins))

    disp = D.Display()
    disp.allowed_origins = origins
    disp.weather = {"temp": 21, "code": 1, "max": 22, "min": 14}
    disp.weather_ts = __import__("time").time()
    disp.boot_note = "Warming up…"
    app = web.Application()
    app.router.add_get("/ws", disp.ws_handler)
    async with TestClient(TestServer(app)) as client:
        try:
            await client.ws_connect("/ws", headers={"Origin": "http://evil.example"})
            raise SystemExit("FAIL evil origin was accepted")
        except ClientError as e:
            check(getattr(e, "status", None) == 403 or "403" in str(e), f"evil origin: {e!r}")
        print("PASS rejected foreign Origin")
        for origin in ("", "http://192.168.0.70:8080", "http://127.0.0.1:8080", "http://localhost:8080"):
            ws = await client.ws_connect("/ws", headers={"Origin": origin})
            snap = json.loads(await ws.receive_str())
            note = json.loads(await ws.receive_str())
            check(snap["event"] == "snapshot", f"snapshot from {origin!r}")
            check(note["event"] == "toast" and note["data"]["text"] == "Warming up…", f"boot note {origin!r} {note}")
            await ws.close()
        print("PASS empty origin and the three hub origins")

    disp2 = D.Display()
    disp2.weather = {"temp": 21, "code": 1, "max": 22, "min": 14}
    disp2.weather_ts = __import__("time").time()
    app2 = web.Application()
    disp2.add_routes(app2)
    async with TestClient(TestServer(app2)) as client:
        page = await client.get("/")
        html = await page.text()
        check(page.status == 200 and "WOMBLE" in html and "/static/womble-mark.webp" in html, "dashboard mark")
        check('rel="icon"' in html and "apple-touch-icon" in html, "icon links")
        for path in ("/static/womble-mark.webp", "/favicon.png", "/favicon.ico", "/apple-touch-icon.png"):
            resp = await client.get(path)
            body = await resp.read()
            ctype = resp.headers.get("Content-Type", "")
            check(resp.status == 200 and ctype.startswith("image/"), f"{path} {resp.status} {ctype}")
            check(200 < len(body) < 80000, f"{path} is {len(body)} bytes")
        print("PASS logo, favicon and apple-touch icon")
    task = app2.get("weather_task")
    if task is not None:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

    n = {"n": 0}
    alerts = []
    async def flaky():
        n["n"] += 1
        if n["n"] < 3: raise RuntimeError("died")
    async def alert(name, exc):
        alerts.append((name, type(exc).__name__))
    await D.supervise("demo", flaky, on_give_up=alert, fails_before_alert=2, first_delay=0.01)
    check(n["n"] == 3, f"restarts {n}")
    check(alerts == [("demo", "RuntimeError")], f"alerts {alerts}")
    print("PASS supervise restarts then stops on a clean return")
    print("test_origin OK")

asyncio.run(main())
