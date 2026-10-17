"""Womble #1 display service (see ../../DISPLAY.md §6).
Pure aiohttp, no ML imports, so it can be tested on its own:  python display.py --port 8099
hub.py mounts it with  disp = Display(...); disp.add_routes(app)

Env: DISPLAY_NAME ("Womble #1"), WEATHER_LAT/WEATHER_LON (Madrid default),
     DISPLAY_PRINTER (0/1, feature flag), DISPLAY_TOKEN (optional bearer for POST /api/event;
     without it /api/event only accepts requests from 127.0.0.1)."""
import asyncio, json, logging, os, time, uuid
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout

log = logging.getLogger("display")
STATIC = Path(__file__).resolve().parent / "static"


def http_hosts(hub_ip: str) -> list[str]:
    """LAN address, plus loopback so localhost /api/event still reaches the hub."""
    hosts = [hub_ip]
    if hub_ip not in ("127.0.0.1", "::1", "localhost"):
        hosts.append("127.0.0.1")
    return hosts


def allowed_ws_origins(hub_ip: str, port: int) -> set[str]:
    """Empty Origin (non-browser clients) or the page served on the hub / loopback."""
    return {
        "",
        f"http://{hub_ip}:{port}",
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }


async def supervise(name, coro_factory, on_give_up=None, fails_before_alert=3, first_delay=2.0):
    """Restart a long-lived task with backoff if it raises. A clean return is left stopped."""
    delay = first_delay
    fails = 0
    while True:
        started = time.monotonic()
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if time.monotonic() - started > 30:
                delay, fails = first_delay, 0
            fails += 1
            log.warning("%s failed (%s: %s); restarting in %.0f s", name, type(e).__name__, e, delay)
            if on_give_up and fails >= fails_before_alert:
                try:
                    await on_give_up(name, e)
                except Exception as ee:
                    log.warning("%s alert failed (%s)", name, type(ee).__name__)
                fails = 0
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)
        else:
            log.warning("%s stopped", name)
            return
WEATHER_TTL = 15 * 60
SHORT_WORDS = 60
FOREGROUND = {"reply", "list.updated", "research.added", "recipe", "recipe.step", "error"}

def shorten(text: str, n: int = SHORT_WORDS) -> str:
    w = text.split()
    return text if len(w) <= n else " ".join(w[:n]).rstrip(",.;:") + " …"

class Display:
    def __init__(self, lists_loader=None, name=None):
        self.clients: set[web.WebSocketResponse] = set()
        self.name = name or os.environ.get("DISPLAY_NAME", "Womble #1")
        self.lat = float(os.environ.get("WEATHER_LAT", "40.4168"))
        self.lon = float(os.environ.get("WEATHER_LON", "-3.7038"))
        self.printer = os.environ.get("DISPLAY_PRINTER", "0") == "1"
        self.token = os.environ.get("DISPLAY_TOKEN")
        self.lists_loader = lists_loader or (lambda: {})
        self.weather = None; self.weather_ts = 0.0
        self.recipe = None; self.step = None; self.media = None
        self.last_reply = None            # full text kept for "print that"
        self.media_handler = None         # async fn(action) -> str|None ; set by hub when MA plugin exists
        self.taps: list[dict] = []
        self.tap_hooks: list = []          # async fn(action) called for every tap (recorder)
        self.snapshot_extra: dict = {}     # key -> fn() merged into snapshot data
        self.allowed_origins = None        # None = no Origin check (standalone). set of strings otherwise
        self.boot_note = None              # toast sent to each socket that connects while set

    # ---------- outbound ----------
    async def emit(self, event: str, data: dict | None = None, corr=None, ttl_s=None, priority="normal"):
        data = dict(data or {})
        if event == "reply":
            body = data.get("body", ""); self.last_reply = body
            data.setdefault("short", shorten(body))
        if event == "recipe": self.recipe = data; self.step = None
        if event == "recipe.step": self.step = data.get("index")
        if event == "media.state": self.media = data
        if event == "idle": self.recipe = None; self.step = None   # dismiss / "stop cooking" ends the recipe
        msg = {"v": 1, "event": event, "id": uuid.uuid4().hex, "corr": corr, "ts": time.time(),
               "ttl_s": ttl_s, "priority": priority, "data": data}
        await self._send_all(msg)

    async def push_card(self, card: dict):
        """Legacy slice-1 shapes {type:list|text}; the page also accepts them raw, but translate here."""
        if card.get("type") == "list":
            hl = card.get("highlight")
            await self.emit("list.updated", {"list": (card.get("title") or "").lower(), "action": "add" if hl else "show",
                                             "items": card.get("items", []), "highlight": [hl] if hl else []})
        else:
            await self.emit("reply", {"title": card.get("title", ""), "body": card.get("body", "")})

    async def _send_all(self, msg):
        s = json.dumps(msg)
        for ws in list(self.clients):
            try: await ws.send_str(s)
            except Exception: self.clients.discard(ws)

    async def snapshot(self) -> dict:
        lists = self.lists_loader()
        return {"v": 1, "event": "snapshot", "id": uuid.uuid4().hex, "corr": None, "ts": time.time(), "ttl_s": 0,
                "priority": "normal", "data": {
                    "device_name": self.name, "features": {"printer": self.printer},
                    "lists": {k: [i["item"] if isinstance(i, dict) else i for i in v] for k, v in lists.items()},
                    "weather": await self.get_weather(), "media": self.media, "recipe": self.recipe, "step": self.step,
                    **{k: f() for k, f in self.snapshot_extra.items()}}}

    # ---------- weather (Open-Meteo, no key, cached 15 min) ----------
    async def get_weather(self):
        if self.weather and time.time() - self.weather_ts < WEATHER_TTL: return self.weather
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m,weather_code"
               "&daily=temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=1") % (self.lat, self.lon)
        try:
            async with ClientSession(timeout=ClientTimeout(total=6)) as s, s.get(url) as r:
                j = await r.json()
            self.weather = {"temp": round(j["current"]["temperature_2m"]), "code": j["current"]["weather_code"],
                            "max": round(j["daily"]["temperature_2m_max"][0]), "min": round(j["daily"]["temperature_2m_min"][0])}
            self.weather_ts = time.time()
        except Exception as e:
            log.warning("weather fetch failed: %s", e)   # keep stale value (or None)
            self.weather_ts = time.time() - WEATHER_TTL + 120   # retry in 2 min, not every connect
        return self.weather

    async def weather_loop(self):
        while True:
            old = self.weather; w = await self.get_weather()
            if w and w != old: await self.emit("weather", w)
            await asyncio.sleep(WEATHER_TTL)

    # ---------- inbound taps from the page ----------
    async def on_tap(self, action: str):
        self.taps.append({"action": action, "ts": time.time()}); log.info("tap %s", action)
        for h in self.tap_hooks:
            try: await h(action)
            except Exception as e: log.warning("tap hook failed: %s", e)
        if action.startswith("record."): return
        if action == "dismiss":
            await self.emit("idle")
        elif action in ("recipe.next", "recipe.prev") and self.recipe:
            steps = self.recipe.get("steps", [])
            i = 0 if self.step is None else self.step + (1 if action == "recipe.next" else -1)
            i = max(0, min(i, len(steps) - 1))
            await self.emit("recipe.step", {"index": i, "total": len(steps), "text": steps[i]})
        elif action in ("media.toggle", "media.next", "media.prev"):
            msg = await self.media_handler(action) if self.media_handler else None
            if msg is None and not self.media_handler:
                await self.emit("toast", {"text": "Music control isn't connected yet", "kind": "info"})
        else:
            log.warning("unknown tap action %r", action)

    # ---------- http ----------
    async def ws_handler(self, request):
        origin = request.headers.get("Origin", "")
        if self.allowed_origins is not None and origin not in self.allowed_origins:
            log.info("ws rejected origin %r", origin)
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=20); await ws.prepare(request); self.clients.add(ws)
        await ws.send_str(json.dumps(await self.snapshot()))
        if self.boot_note:
            note = {"v": 1, "event": "toast", "id": uuid.uuid4().hex, "corr": None, "ts": time.time(),
                    "ttl_s": None, "priority": "normal", "data": {"text": self.boot_note, "kind": "info"}}
            await ws.send_str(json.dumps(note))
        async for m in ws:
            try:
                j = json.loads(m.data)
                if j.get("event") == "tap": await self.on_tap(str(j.get("action", "")))
            except Exception as e:
                log.warning("bad client msg: %s", e)
        self.clients.discard(ws); return ws

    async def api_event(self, request):
        """Local injection (tests, bot callbacks via router later). Body: {"event":..., "data":{...}} or legacy card."""
        if self.token:
            if request.headers.get("Authorization") != f"Bearer {self.token}": raise web.HTTPUnauthorized()
        elif request.remote not in ("127.0.0.1", "::1"): raise web.HTTPForbidden()
        j = await request.json()
        if "event" in j: await self.emit(j["event"], j.get("data"), j.get("corr"), j.get("ttl_s"), j.get("priority", "normal"))
        else: await self.push_card(j)
        return web.json_response({"ok": True, "clients": len(self.clients)})

    async def api_taps(self, request):
        return web.json_response(self.taps)

    def add_routes(self, app: web.Application):
        app.router.add_get("/", lambda r: web.FileResponse(STATIC / "display.html"))
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_post("/api/event", self.api_event)
        app.router.add_get("/api/taps", self.api_taps)
        async def start_bg(app):
            async def _alert(name, exc):
                await self.emit("error", {"message": "Weather updates stopped", "code": "weather",
                                          "detail": type(exc).__name__})
            app["weather_task"] = asyncio.create_task(supervise("weather", self.weather_loop, on_give_up=_alert))
        app.on_startup.append(start_bg)

if __name__ == "__main__":   # standalone test server, no BOX-3 / ML needed
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8099); ap.add_argument("--lists")
    a = ap.parse_args(); logging.basicConfig(level=logging.INFO)
    loader = (lambda: json.loads(Path(a.lists).read_text())) if a.lists else None
    app = web.Application(); Display(loader).add_routes(app); web.run_app(app, port=a.port)
