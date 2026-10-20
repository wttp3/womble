"""Print the frame's push-to-paint / tap-feedback stats from the running hub.

  .venv/bin/python paintstats.py            # p50/p95/max (ms), per event too
  .venv/bin/python paintstats.py --reset    # print, then clear the rolling window
Talks to 127.0.0.1 only (GET /api/paint is localhost-only without DISPLAY_TOKEN)."""
import json, os, sys, urllib.request

port = int(os.environ.get("HUB_PORT", "8080"))
url = f"http://127.0.0.1:{port}/api/paint" + ("?reset=1" if "--reset" in sys.argv else "")
req = urllib.request.Request(url)
if os.environ.get("DISPLAY_TOKEN"):
    req.add_header("Authorization", "Bearer " + os.environ["DISPLAY_TOKEN"])
sm = json.load(urllib.request.urlopen(req, timeout=5))
if "--json" in sys.argv:
    print(json.dumps(sm, indent=1)); sys.exit()
fmt = lambda v: "-" if v is None else "%.0f" % v
print("%-16s %5s %7s %7s %7s" % ("metric (ms)", "n", "p50", "p95", "max"))
for k in ("push_to_paint", "render", "rtt", "tap_fb"):
    v = sm[k]; print("%-16s %5d %7s %7s %7s" % (k, v["n"], fmt(v["p50"]), fmt(v["p95"]), fmt(v["max"])))
print("push_to_paint by event:")
for k, v in sorted(sm["by_event"].items()):
    print("  %-14s %5d %7s %7s %7s" % (k, v["n"], fmt(v["p50"]), fmt(v["p95"]), fmt(v["max"])))
