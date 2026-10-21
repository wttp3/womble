"""Media M6: hub-side episode progress and end (Alexa sends no AudioPlayer events).

No network, no Keychain. python tests/test_media_m6.py
"""
import asyncio, logging, os, sys, tempfile
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB))
logging.getLogger("hub.media").setLevel(logging.ERROR)
TMP = tempfile.mkdtemp(prefix="womble-m6-")
os.environ["MEDIA_DB"] = os.path.join(TMP, "media.sqlite")

import media.service as service
from media.library import Library
from media.resolver import Resolver, ep_key, parse_duration
from media.service import MediaService

FEED = (b'<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
        b'<item><title>Ep 2</title><guid>g2</guid><itunes:duration>36:27</itunes:duration>'
        b'<enclosure url="https://cdn.example/ep2.mp3" type="audio/mpeg"/></item>'
        b'<item><title>Ep 1</title><guid>g1</guid><enclosure url="https://cdn.example/ep1.mp3" type="audio/mpeg"/></item>'
        b'</channel></rss>')


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL {msg}")


class Disp:
    def __init__(self): self.events = []
    async def emit(self, ev, data=None, **k): self.events.append((ev, dict(data or {})))


class Link:
    def __init__(self): self.sent = []
    async def send_typed(self, msg): self.sent.append(msg); return True


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


CFG = {"kitchen_pair": ["white alexa"], "box3_room": "kitchen"}


def make():
    lib = Library(os.path.join(TMP, f"m{os.urandom(3).hex()}.sqlite"))
    clk, link = Clock(), Link()
    svc = MediaService(lib, CFG, disp=Disp(), link=link, clock=clk,
                       resolver=Resolver(lib, fetch=lambda url: FEED))
    return svc, clk, link


async def main():
    check(parse_duration("2187") == 2187 and parse_duration("36:27") == 2187 and parse_duration("1:00:00") == 3600
          and parse_duration("") == 0 and parse_duration("x") == 0, "parse_duration")

    # --- progress saves, pause freezes, end marks finished ---
    svc, clk, link = make()
    ep = "a" * 16
    await svc.on_event({"event": "play", "entry_id": "dsr_daily", "ep_key": ep, "device": "d" * 16,
                        "offset_ms": 0, "duration_s": 120, "title": "Ep"})
    room = next(iter(svc.now))
    clk.t += 31; await svc.progress_tick()
    p = [m for m in link.sent if m["type"] == "media.progress"]
    check(len(p) == 1 and p[0]["offset_ms"] == 31000 and not p[0]["finished"] and p[0]["device"] == "d" * 16, p)
    clk.t += 10; await svc.progress_tick()
    check(len(link.sent) == 1, "no save before 30 s")
    # +30 seek from the frame moves the position (transport sets position/started)
    st = svc.now[room]
    svc.now[room] = {**st, "position_s": svc.position(st) + 30, "started": clk.t}
    check(abs(svc.position(svc.now[room]) - 71) < 0.01, svc.position(svc.now[room]))
    # pause: position frozen
    svc.now[room] = {**svc.now[room], "state": "paused", "position_s": svc.position(svc.now[room]), "started": clk.t}
    clk.t += 500; await svc.progress_tick()
    check(svc.now[room]["state"] == "paused" and len(link.sent) == 1, "paused episode never finishes")
    # resume: the worker notifies play at the saved offset
    await svc.on_event({"event": "play", "entry_id": "dsr_daily", "ep_key": ep, "device": "d" * 16,
                        "offset_ms": 71000, "duration_s": 120})
    clk.t += 60; await svc.progress_tick()
    check(svc.now[room]["state"] == "playing", "not finished inside grace")
    clk.t += 10; await svc.progress_tick()
    st = svc.now[room]
    check(st["state"] == "idle" and not st["focus"], st)
    fin = link.sent[-1]
    check(fin["finished"] is True and fin["ep_key"] == ep and fin["offset_ms"] == 0, fin)
    ev, data = svc.disp.events[-1]
    check(ev == "media.state" and data["state"] == "idle", (ev, data))
    n = len(link.sent); clk.t += 100; await svc.progress_tick()
    check(len(link.sent) == n, "finished once")
    check(not svc.lib.db.execute("SELECT 1 FROM resume_mirror WHERE ep_key=?", (ep,)).fetchone(), "mirror cleared")

    # --- radio is exempt ---
    svc, clk, link = make()
    await svc.on_event({"event": "play", "entry_id": "lbc", "device": "d" * 16, "offset_ms": 0, "duration_s": 0})
    clk.t += 10000; await svc.progress_tick()
    check(link.sent == [] and next(iter(svc.now.values()))["state"] == "playing", "radio exempt")
    check(svc._ticker is None, "no ticker for radio")

    # --- duration from the feed when the worker sent none ---
    svc, clk, link = make()
    k2 = ep_key("g2", "https://cdn.example/ep2.mp3")
    check(svc.episode_duration("dsr_daily", k2) == 2187, "feed itunes:duration")
    service.ffprobe_duration = lambda url: 1804.5
    check(svc.episode_duration("dsr_daily", ep_key("g1", "https://cdn.example/ep1.mp3")) == 1804.5, "ffprobe fallback")
    await svc.on_event({"event": "play", "entry_id": "dsr_daily", "ep_key": k2, "device": "d" * 16, "offset_ms": 0})
    for _ in range(50):
        await asyncio.sleep(0.01)
        if next(iter(svc.now.values())).get("duration_s"):
            break
    check(next(iter(svc.now.values()))["duration_s"] == 2187, svc.now)
    svc._ticker.cancel()
    print("ALL M6 PASSED")


asyncio.run(main())
