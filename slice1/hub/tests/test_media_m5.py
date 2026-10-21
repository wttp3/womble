"""Media M5: directory search + probe, add/confirm/remove, frame taps, transport debounce,
fast paths (before the list router's ADD_RE), display tap routing.

No network, no Keychain. python tests/test_media_m5.py
"""
import asyncio, hashlib, json, logging, os, sys, tempfile
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB))
logging.getLogger("hub.media").setLevel(logging.ERROR)
TMP = tempfile.mkdtemp(prefix="womble-m5-")
os.environ["MEDIA_DB"] = os.path.join(TMP, "media.sqlite")

import media.service as service
from media.directory import Directory
from media.library import Library
from media.native import VoiceMonkey
from media.service import MediaService, fast_path_m5

FEED = (b'<rss><channel><item><title>Ep 2</title><guid>g2</guid><pubDate>Mon, 19 Oct 2026 06:00:00 GMT</pubDate>'
        b'<enclosure url="https://cdn.example/ep2.mp3" type="audio/mpeg"/></item></channel></rss>')
HTTP_FEED = FEED.replace(b"https://cdn", b"http://cdn")


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL {msg}")


class Disp:
    def __init__(self): self.events = []
    async def emit(self, ev, data=None, **k): self.events.append((ev, data))


class Devs:
    def _load(self): return {"dev-white": {"room": "white alexa"}, "dev-blue": {"room": "blue kitchen"}}


class Link:
    def __init__(self): self.sent = []
    async def send_typed(self, msg): self.sent.append(msg); return True


CFG = {"triggers": {"lbc_kitchen": "T-LBC", "frida_white": "T-FW"}, "kitchen_pair": ["white alexa", "blue kitchen"],
       "dispatch": {"kitchen": {"echo": "white alexa", "trigger": "frida_white"}}, "box3_room": "kitchen"}


def directory(secret=lambda n: None):
    calls = []
    def get_json(url, headers=None):
        calls.append((url, headers))
        if "podcastindex" in url:
            return {"feeds": [{"title": "The Rest Is Politics", "author": "Goalhanger", "url": "https://feeds.example/trip"},
                              {"title": "TRIP Leading", "author": "Goalhanger", "url": "https://feeds.example/http"}]}
        if "itunes" in url:
            return {"results": [{"collectionName": "The Rest Is Politics", "artistName": "Goalhanger", "feedUrl": "https://feeds.example/trip"},
                                {"collectionName": "Plain HTTP Show", "artistName": "X", "feedUrl": "http://feeds.example/plain"}]}
        return [{"name": "Jazz FM", "url_resolved": "https://stream.example/jazz", "codec": "AAC", "bitrate": 128},
                {"name": "Old Jazz", "url_resolved": "http://stream.example/old", "codec": "MP3", "bitrate": 64}]
    def get_bytes(url):
        return HTTP_FEED if url.endswith("/http") else FEED
    def head(url, rng=False):
        return (206 if rng else 200), "audio/mpeg", url
    d = Directory(get_json=get_json, get_bytes=get_bytes, head=head, secret=secret, clock=lambda: 1700000000)
    d._rb_server = lambda: "rb.example"
    return d, calls


def make():
    lib = Library(os.path.join(TMP, f"m{os.urandom(3).hex()}.sqlite"))
    posts = []
    vm = VoiceMonkey(CFG, lib, token=lambda: "tok", post=lambda u, p, t: (posts.append(p) or (200, b"{}")))
    link = Link()
    svc = MediaService(lib, CFG, disp=Disp(), native=vm, devices=Devs(), link=link)
    svc.directory, calls = directory()
    return svc, posts, link, calls


async def main():
    # --- directory: iTunes fallback without keys, Podcast Index auth with keys, probe ---
    d, calls = directory()
    out = d.find("the rest is politics")
    check("itunes" in calls[0][0] and len(out) == 2, f"keyless -> iTunes {calls}")
    check(out[0]["ok"] and out[0]["latest"]["title"] == "Ep 2", out[0])
    check(not out[1]["ok"] and "HTTPS" in out[1]["why"], out[1])
    d, calls = directory(secret=lambda n: {"womble-podcastindex": "K" * 20, "womble-podcastindex-secret": "S" * 40}[n])
    out = d.find("the rest is politics")
    h = calls[0][1]
    check("podcastindex" in calls[0][0] and h["X-Auth-Key"] == "K" * 20 and h["X-Auth-Date"] == "1700000000", "PI headers")
    check(h["Authorization"] == hashlib.sha1(("K" * 20 + "S" * 40 + "1700000000").encode()).hexdigest(), "PI auth hash")
    check(out[0]["ok"] and not out[1]["ok"] and "no HTTPS episode" in out[1]["why"], out)
    out = d.find("jazz", "radio")
    check(out[0]["ok"] and not out[1]["ok"] and "rb.example" in calls[-1][0], out)
    e = Directory.to_entry(out[0], {"jazz_fm"})
    check(e["id"] == "jazz_fm_2" and e["methods"][0]["url"] == "https://stream.example/jazz", e)
    print("PASS directory search + probe")

    # --- add flow ---
    svc, posts, link, _ = make()
    r = await svc.add_search("the rest is politics", "podcast")
    check(r["ok"] and "first, The Rest Is Politics" in r["speech"] and "won't play" in r["speech"], r)
    ev = svc.disp.events[-1]
    check(ev[0] == "media.candidates" and ev[1]["candidates"][0]["ok"] and not ev[1]["candidates"][1]["ok"], ev)
    check(not any("feeds.example" in json.dumps(c) for c in ev[1]["candidates"]), "no feed URLs on the frame")
    check(fast_path_m5("yes, the first one", svc) == ("confirm", 1), "yes first")
    r = await svc.add_confirm(2)
    check(not r["ok"] and "didn't add" in r["speech"], r)
    n0 = len(svc.lib.entries())
    r = await svc.add_confirm(1)
    check(r["ok"] and len(svc.lib.entries()) == n0 + 1 and svc.lib.exact("the rest is politics"), r)
    check(link.sent[-1]["type"] == "media.snapshot" and any(x["name"] == "The Rest Is Politics" for x in link.sent[-1]["entries"]), "snapshot pushed")
    check(svc.disp.events[-1][0] == "media.library", "library card")
    check(svc.pending_add() is None and fast_path_m5("yes the first one", svc) is None, "pending cleared")
    r = await svc.add_search("the rest is politics")
    r = await svc.add_confirm(1)
    check(not r["ok"] and "already" in r["speech"], r)
    # expiry
    await svc.add_search("jazz", "radio")
    t0 = svc.clock; svc.clock = lambda: t0() + 301
    check(svc.pending_add() is None, "5 minute expiry"); svc.clock = t0
    print("PASS add_to_library: candidates, confirm, non-HTTPS refused, duplicate, expiry")

    # --- remove + frame taps ---
    eid = svc.lib.exact("the rest is politics")["id"]
    msg = await svc.on_tap(f"media.remove:{eid}")
    check("Removed" in msg and svc.lib.exact("the rest is politics") is None and svc.lib.get(eid) is not None, "soft delete")
    msg = await svc.on_tap("media.library")
    check(msg == "" and svc.disp.events[-1][0] == "media.library", "library tap")
    check(await svc.on_tap("weird.thing") is None, "unknown tap")
    msg = await svc.on_tap("media.play:dsr_daily")
    check(posts[-1] == {"token": "tok", "device": "T-FW"} and link.sent[-1]["cmd"] == {"op": "play", "entry_id": "dsr_daily", "mode": "auto"}, (posts, link.sent[-1]))
    check("Starting" in msg, msg)
    print("PASS remove (soft), library tap, frame play -> dispatch")

    # --- transport ---
    service.DEBOUNCE_S = 0.05
    await svc.on_event({"event": "play", "device": None, "entry_id": "dsr_daily", "ep_key": "a" * 16, "title": "Ep", "offset_ms": 0})
    st = svc.now.get("kitchen") or next(iter(svc.now.values()))
    svc.now["kitchen"] = {**st, "room": "kitchen"}
    n_posts = len(posts)
    for a in ("media.fwd", "media.fwd", "media.fwd", "media.back"):
        await svc.on_tap(a)
    await asyncio.sleep(0.15)
    seeks = [m for m in link.sent if m.get("type") == "media.pending" and m["cmd"]["op"] == "seek"]
    check(len(seeks) == 1 and seeks[0]["cmd"]["delta_ms"] == 60000 and len(posts) == n_posts + 1, (seeks, len(posts)))
    await svc.on_tap("media.toggle")
    check(link.sent[-1]["cmd"]["op"] == "pause" and svc.now["kitchen"]["state"] == "paused", link.sent[-1])
    await svc.on_tap("media.toggle")
    check(link.sent[-1]["cmd"]["op"] == "resume", link.sent[-1])
    await svc.on_tap("media.next")
    check(link.sent[-1]["cmd"]["op"] == "next", link.sent[-1])
    svc.now["kitchen"] = {**svc.now["kitchen"], "route": "native"}
    msg = await svc.on_tap("media.toggle")
    check("Alexa, stop" in msg, msg)
    svc.now.clear()
    check(await svc.on_tap("media.fwd") == "Nothing is playing.", "nothing playing")
    print("PASS transport: +30/-30 debounced into one seek, toggle pause/resume, next, native honest line")

    # --- quota warning at 80 % ---
    svc.native.month_count = lambda: 160
    await svc.on_tap("media.play:dsr_daily")
    check(any(e[0] == "toast" and "160 of 200" in e[1]["text"] for e in svc.disp.events), "quota toast")
    print("PASS quota warning")

    # --- fast paths beat the list router ---
    svc2, *_ = make()
    check(fast_path_m5("add the podcast the rest is politics", svc2) == ("add", "the rest is politics", "podcast"), "add podcast")
    check(fast_path_m5("add the radio station jazz fm", svc2) == ("add", "jazz fm", "radio"), "add radio")
    check(fast_path_m5("add the news agents to my library", svc2) == ("add", "the news agents", None), "add to library")
    check(fast_path_m5("add milk", svc2) is None and fast_path_m5("add eggs to shopping", svc2) is None, "lists untouched")
    check(fast_path_m5("yes the first one", svc2) is None, "yes without pending")
    check(fast_path_m5("show my library", svc2) == ("show",), "show")
    check(fast_path_m5("remove LBC from my library", svc2)[:2] == ("remove", "lbc"), "remove")
    print("PASS fast paths (before ADD_RE)")

    # --- display routes media taps to the handler and toasts its reply ---
    import display as dmod
    dd = dmod.Display.__new__(dmod.Display)
    dd.taps, dd.tap_hooks, got, ev = [], [], [], []
    async def h(a): got.append(a); return "Pausing."
    async def emit(e, data=None, **k): ev.append((e, data))
    dd.media_handler, dd.emit = h, emit
    await dd.on_tap("media.add:1"); await dd.on_tap("media.play:lbc")
    check(got == ["media.add:1", "media.play:lbc"] and ev[-1] == ("toast", {"text": "Pausing.", "kind": "info"}), (got, ev))
    print("PASS display tap routing")

    # --- brain tool ---
    import brain.tools as bt
    bt.MEDIA = svc2
    r = await bt.add_to_library({"query": "https://evil.example/feed"}, None)
    check(not r["ok"], "URL refused")
    r = await bt.add_to_library({"query": "the rest is politics"}, None)
    check(r["ok"] and "first" in r["say"], r)
    check("add_to_library" in bt.BY_NAME, "registered")
    print("PASS brain tool add_to_library")
    print("ALL PASS test_media_m5")


asyncio.run(main())
