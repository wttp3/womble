"""Media M3/M4: hub routing table, Voice Monkey client, worker events, Alexa link media jobs.

No network, no Keychain. python tests/test_media_routing.py
"""
import asyncio, json, logging, os, sys, tempfile
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB))
logging.getLogger("hub.media").setLevel(logging.ERROR)
TMP = tempfile.mkdtemp(prefix="womble-route-")
os.environ["MEDIA_DB"] = os.path.join(TMP, "media.sqlite")

from media.library import Library
from media.native import VoiceMonkey
from media.service import MediaService, device_hash, fast_path


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL {msg}")


class Disp:
    def __init__(self): self.events = []
    async def emit(self, ev, data=None, **k): self.events.append((ev, data))


class Devs:
    def __init__(self):
        self.map = {"dev-white": {"room": "white alexa"}, "dev-blue": {"room": "blue kitchen"},
                    "dev-black": {"room": "black alexa"}}
    def _load(self): return self.map
    def lookup(self, d): return (self.map.get(d) or {"room": "Echo"}), False


class Link:
    def __init__(self, up=True): self.up, self.sent = up, []
    async def send_typed(self, msg):
        self.sent.append(msg); return self.up


CFG = {"triggers": {"lbc_kitchen": "T-LBC", "frida_white": "T-FW"}, "kitchen_pair": ["white alexa", "blue kitchen"],
       "dispatch": {"kitchen": {"echo": "white alexa", "trigger": "frida_white"}}}


def make(cfg=CFG, post_status=200, link=None, token="tok"):
    lib = Library(os.path.join(TMP, f"r{os.urandom(3).hex()}.sqlite"))
    posts = []
    def post(url, payload, timeout):
        posts.append((url, payload)); return post_status, b"{}"
    vm = VoiceMonkey(cfg, lib, token=lambda: token, post=post)
    svc = MediaService(lib, cfg, disp=Disp(), native=vm, devices=Devs(), link=link if link is not None else Link())
    return svc, posts


async def main():
    # --- §5 table ---
    svc, posts = make()
    r = await svc.play("LBC", source="alexa", echo="white alexa")
    check(r["route"] == "native" and posts[-1][1] == {"token": "tok", "device": "T-LBC"}, r)
    check("tok" not in posts[-1][0], "token in the URL")
    check(svc.disp.events[-1][0] == "media.state" and svc.disp.events[-1][1]["player"] == "via Alexa routine", "frame")
    r = await svc.play("LBC", source="alexa", echo="black alexa")
    check(r["route"] == "frida" and r["entry_id"] == "lbc", f"single Echo LBC {r}")
    r = await svc.play("DSR Daily", source="alexa", echo="blue kitchen")
    check(r["route"] == "frida", f"kitchen podcast via Frida (S0c) {r}")
    r = await svc.play("deep state radio", source="alexa", echo="black alexa", mode="latest")
    check(r["route"] == "frida" and r["entry_id"] == "dsr_network" and r["mode"] == "latest", r)
    r = await svc.play("DSR Daily", room="bedroom", source="alexa", echo="black alexa")
    check(r["route"] == "none" and "bedroom Echo" in r["speech"], r)
    r = await svc.play("jazz fm", source="box3")
    check(r["route"] == "none" and "isn't in the library" in r["speech"], r)
    r = await svc.play("kitchen music", source="box3")
    check(r["route"] == "none", "disabled preset (Q1)")
    print("PASS routing table (alexa sources)")

    n = len(posts)
    r = await svc.play("LBC", source="box3")
    check(r["route"] == "native" and len(posts) == n + 1, "BOX-3 LBC native")
    r = await svc.play("DSR Daily", source="box3")
    check(r["route"] == "dispatch" and posts[-1][1]["device"] == "T-FW", f"BOX-3 podcast dispatch {r}")
    pend = svc.link.sent[-1]
    check(pend["type"] == "media.pending" and pend["device"] == device_hash("dev-white")
          and pend["cmd"] == {"op": "play", "entry_id": "dsr_daily", "mode": "auto"}, pend)
    check(r["speech"] == "Starting DSR Daily on the kitchen Echo.", r["speech"])
    print("PASS routing table (BOX-3: native radio, podcast dispatch to white)")

    # --- not configured / failures ---
    svc2, posts2 = make(cfg={"kitchen_pair": ["white alexa", "blue kitchen"]})
    r = await svc2.play("LBC", source="alexa", echo="white alexa")
    check(r["route"] == "frida", "no trigger -> Frida on the Echo spoken to")
    r = await svc2.play("DSR Daily", source="box3")
    check(r["route"] == "none" and "from an Echo" in r["speech"] and not posts2, r)
    svc3, _ = make(post_status=500)
    r = await svc3.play("LBC", source="box3")
    check(r["speech"] == "I couldn't start the kitchen speakers.", r)
    svc4, posts4 = make(token=None)
    r = await svc4.play("LBC", source="box3")
    check(r["route"] == "none" and not posts4, "no Keychain token")
    svc5, posts5 = make(link=Link(up=False))
    r = await svc5.play("DSR Daily", source="box3")
    check(r["route"] == "none" and "link is down" in r["speech"] and not posts5, "dispatch needs the link before firing")
    check(svc.native.month_count() >= 3, "plays counted")
    print("PASS not configured, Voice Monkey failure, missing token, link down")

    # --- events from the worker ---
    h = device_hash("dev-white")
    await svc.on_event({"type": "media.event", "event": "play", "entry_id": "dsr_daily", "ep_key": "a" * 16,
                        "title": "Ep 3", "offset_ms": 0, "duration_s": 1800, "device": h})
    st = svc.now["kitchen"]
    check(st["state"] == "playing" and st["title"] == "Ep 3" and st["player"] == "Frida on white alexa", st)
    await svc.on_event({"event": "stopped", "entry_id": "dsr_daily", "ep_key": "a" * 16, "offset_ms": 90000, "device": h})
    check(svc.now["kitchen"]["state"] == "paused" and svc.now["kitchen"]["position_s"] == 90, svc.now["kitchen"])
    row = svc.lib.db.execute("SELECT offset_ms FROM resume_mirror WHERE ep_key=?", ("a" * 16,)).fetchone()
    check(row and row[0] == 90000, "resume mirror")
    await svc.on_event({"event": "finished", "entry_id": "dsr_daily", "ep_key": "a" * 16, "device": h})
    check(svc.lib.db.execute("SELECT COUNT(*) FROM resume_mirror").fetchone()[0] == 0, "finished clears mirror")
    print("PASS worker events -> frame now playing + resume mirror")

    # --- stop ---
    svc.now["kitchen"] = {"route": "native", "title": "LBC", "state": "playing"}
    r = await svc.stop(source="box3")
    check(not r["ok"] and "Alexa, stop" in r["speech"] and svc.now["kitchen"]["state"] == "idle", r)
    svc.now["kitchen"] = {"route": "frida", "title": "Ep", "state": "playing"}
    r = await svc.stop(source="box3")
    check(r["ok"] and svc.link.sent[-1]["cmd"] == {"op": "pause"}, "stop via dispatcher pause")
    print("PASS stop (honest line for native, pause via dispatcher for Frida)")

    # --- fast path ---
    lib = svc.lib
    check(fast_path("play LBC", lib) == ("play", "lbc", None, "auto"), fast_path("play LBC", lib))
    check(fast_path("Play the latest DSR Daily.", lib) == ("play", "dsr_daily", None, "latest"), "latest")
    check(fast_path("put on world service in the kitchen", lib) == ("play", "bbc_ws", "kitchen", "auto"), "room")
    check(fast_path("stop the radio", lib) == ("stop",) and fast_path("stop", lib) is None, "stop")
    check(fast_path("play some jazz", lib) is None, "unknown falls to the brain")
    print("PASS fast path")

    # --- snapshot ---
    svc.link.sent.clear()
    check(await svc.push_snapshot({"dsr_daily": {"ep_key": "b" * 16}}), "snapshot sent")
    snap = svc.link.sent[-1]
    check(snap["type"] == "media.snapshot" and len(snap["entries"]) == 11 and "latest" in snap, "snapshot shape")
    check(not any(k in json.dumps(snap) for k in ("T-LBC", "T-FW", "dev-white")), "per-house ids in the snapshot")
    print("PASS snapshot carries no per-house ids")

    # --- Alexa link media job + typed message ---
    import alexa_link
    link = alexa_link.AlexaLink(Disp(), lambda t: {"category": "fallback"}, None, None, devices=Devs())
    link.media = svc
    rep = await link.handle_job({"id": "j1", "deviceId": "dev-black", "media": {"op": "play", "entry_id": "lbc", "mode": "auto"}})
    check(rep["id"] == "j1" and rep["media"]["route"] == "frida" and rep["end_session"], rep)
    rep = await link.handle_job({"id": "j2", "deviceId": "dev-blue", "media": {"op": "play", "entry_id": "lbc"}})
    check(rep["media"]["route"] == "native" and rep["speech"].startswith("Starting LBC"), rep)
    await link._on_typed({"type": "media.event", "event": "play", "entry_id": "lbc", "device": device_hash("dev-black")})
    check(svc.now["black alexa"]["player"] == "Frida on black alexa", svc.now.keys())

    class WS:
        closed = False
        def __init__(self): self.out = []
        async def send_str(self, s): self.out.append(s)
    ws = WS()
    await link._on_text(ws, json.dumps({"type": "media.event", "event": "stopped", "entry_id": "lbc", "device": "x"}))
    check(ws.out == [], "typed message got a reply")
    link.ws = ws
    check(await link.send_typed({"type": "x"}) and json.loads(ws.out[-1]) == {"type": "x"}, "send_typed")
    print("PASS alexa link media jobs and typed messages")

    # --- brain tools ---
    import brain.tools as bt
    bt.MEDIA = svc
    ctx = bt.ToolContext("play lbc", "box3-kitchen", "box3-kitchen", None, Disp())
    res = await bt.play_media({"name": "https://evil.example/x.mp3"}, ctx)
    check(res["error"] == bt.REFUSED, "raw URL refused")
    res = await bt.play_media({"name": "LBC"}, ctx)
    check(res["route"] == "native" and res["say"].startswith("Starting LBC"), res)
    names = [t["name"] for t in bt.responses_tools(include_web_search=False)]
    check("play_media" in names and "stop_media" in names, names)
    print("PASS brain tools play_media / stop_media")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASS test_media_routing")
