"""Replay a fixed set of safe frame events and print push-to-paint stats.

  .venv/bin/python frame_bench.py [--rounds 3] [--gap 1.5]
Uses POST /api/event on 127.0.0.1 (localhost-only). Ends on idle with media
cleared. Resets /api/paint first, so the numbers cover only this run."""
import json, os, sys, time, urllib.request

PORT = int(os.environ.get("HUB_PORT", "8080"))
args = sys.argv[1:]
ROUNDS = int(args[args.index("--rounds") + 1]) if "--rounds" in args else 3
GAP = float(args[args.index("--gap") + 1]) if "--gap" in args else 1.5


def post(event, data):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/event", method="POST",
                                 data=json.dumps({"event": event, "data": data}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=5))


GRID = {"radio": [{"id": "r%d" % i, "name": "Test station %d" % i} for i in range(3)],
        "podcast": [{"id": "p%d" % i, "name": "Test podcast %d" % i} for i in range(7)], "music_preset": []}
NOW = {"title": "Test episode", "artist": "Test podcast", "player": "Frida on test", "state": "playing",
       "position_s": 120, "duration_s": 1800, "focus": True, "entry_id": "p1", "route": "frida"}
SEQ = [
    ("toast", {"text": "Frame timing test", "kind": "info"}),
    ("reply", {"title": "Timing test", "body": "This is a short reply used to time how fast the frame draws a new card."}),
    ("idle", {}),
    ("list.updated", {"list": "timing test", "action": "show", "items": ["eggs", "milk", "bread", "apples"], "highlight": []}),
    ("media.library", {"grid": GRID}),
    ("media.candidates", {"query": "test", "candidates": [{"n": 1, "name": "Test show", "source": "test", "ok": True},
                                                          {"n": 2, "name": "Other show", "source": "test", "ok": False, "why": "not HTTPS"}]}),
    ("media.state", NOW),
    ("media.state", {**NOW, "state": "paused", "focus": False, "position_s": 150}),
    ("toast", {"text": "Frame timing test 2", "kind": "info"}),
    ("media.state", {**NOW, "state": "idle", "focus": False}),
    ("idle", {}),
]

with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/paint?reset=1", timeout=5) as r:
    r.read()
for rnd in range(ROUNDS):
    for ev, data in SEQ:
        post(ev, data); time.sleep(GAP)
time.sleep(2)
os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "paintstats.py")])
