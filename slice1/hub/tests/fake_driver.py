"""Fake event driver + headless screenshots for the Womble #1 display. Runs display.py standalone (no BOX-3, no ML).
Usage: python tests/fake_driver.py [outdir]     (needs aiohttp + playwright + a Chromium/Chrome)"""
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
HUB = Path(__file__).resolve().parents[1]; OUT = Path(sys.argv[1] if len(sys.argv) > 1 else HUB / "tests" / "shots"); OUT.mkdir(parents=True, exist_ok=True)
PORT = 8099; URL = f"http://127.0.0.1:{PORT}"
lists = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"shopping": [{"item": i, "ts": 0} for i in ["Eggs", "Lemons", "Basmati rice", "Butter", "Coriander", "Greek yoghurt", "Tinned tomatoes"]],
           "to-do": [{"item": "Call plumber", "ts": 0}]}, lists); lists.close()
srv = subprocess.Popen([sys.executable, str(HUB / "display.py"), "--port", str(PORT), "--lists", lists.name], cwd=HUB,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
results = []
def check(name, ok, info=""): results.append((name, bool(ok))); print(("PASS " if ok else "FAIL ") + name, info)
def post(ev, data=None, **kw):
    body = json.dumps({"event": ev, "data": data or {}, **kw} if ev else data).encode()
    return json.loads(urllib.request.urlopen(urllib.request.Request(URL + "/api/event", body, {"Content-Type": "application/json"})).read())
for _ in range(50):
    try: urllib.request.urlopen(URL + "/"); break
    except Exception: time.sleep(0.2)
CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")
try:
  with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    def shot(n): pg.wait_for_timeout(350); pg.screenshot(path=str(OUT / f"{n}.png"))
    def txt(): return pg.inner_text("body")
    pg.goto(URL + "/?hour=14&min=43"); pg.wait_for_timeout(2500)   # snapshot incl. live Open-Meteo fetch
    check("snapshot idle shows brand + shopping", "Womble #1" in txt() and "Basmati rice" in txt())
    check("weather rendered", "max" in txt(), "(Open-Meteo reachable)" if "max" in txt() else "(weather unavailable)")
    shot("01-idle")
    post("voice.listening", {"wake_word": "womble"}); shot("02-listening"); check("listening", "Listening…" in txt())
    post("voice.heard", {"text": "Find a quick lentil curry recipe", "bot": "search"}); shot("03-thinking"); check("thinking", "lentil curry" in txt())
    long = ("Fresh eggs keep about three to five weeks in the fridge from the day you buy them. " * 8).strip()
    post("reply", {"title": "How long do eggs keep?", "body": long, "source": "Grok chat bot"}); shot("04-reply-short")
    check("reply shortened >60 words + print hint", "…" in txt() and "print that" in txt())
    post("list.updated", {"list": "shopping", "action": "add", "items": ["Eggs", "Lemons", "Basmati rice", "Butter", "Coriander", "Greek yoghurt", "Tinned tomatoes", "Oat milk"], "highlight": ["Oat milk"]})
    shot("05-list"); check("list highlight", pg.locator("li.hl").inner_text() == "Oat milk")
    post(None, {"type": "list", "title": "To-do", "items": ["Call plumber", "Book MOT"], "highlight": "Book MOT"}); shot("06-legacy-list")
    check("legacy slice-1 list card", "Book MOT" in txt() and pg.locator("li.hl").inner_text() == "Book MOT")
    post(None, {"type": "text", "title": "Heard", "body": "turn the oven on"}); pg.wait_for_timeout(300); check("legacy slice-1 text card", "turn the oven on" in txt())
    post("research.added", {"title": "Induction hobs", "note": "under €600 with a bridge zone", "count": 4}); shot("07-research")
    post("recipe", {"title": "Lentil curry", "minutes": 35, "serves": 4,
         "ingredients": ["250 g red lentils", "1 onion, diced", "3 garlic cloves", "1 tbsp ginger", "2 tsp curry powder", "400 ml coconut milk", "400 g tinned tomatoes", "Spinach, lime, coriander"],
         "steps": ["Soften the onion in oil, 5 min.", "Add garlic, ginger and curry powder; 1 min.", "Add the lentils, tomatoes, coconut milk and 300 ml water. Bring to a simmer.", "Simmer 20 min, stirring now and then.", "Stir in spinach and lime juice; top with coriander."]})
    shot("08-recipe")
    pg.click("[data-a='recipe.next']"); pg.wait_for_timeout(400); check("tap recipe.next -> step 1", "Step 1 of 5" in txt())
    pg.click("[data-a='recipe.next']"); pg.click("[data-a='recipe.next']"); pg.wait_for_timeout(400); check("tap x2 -> step 3", "Step 3 of 5" in txt())
    post("recipe.step", {"index": 3, "total": 5, "text": "Simmer 20 min, stirring now and then.", "timer_s": 1200}); shot("09-step-timer")
    check("step timer shown", "20:00" in txt() or "19:5" in txt())
    pg.reload(); pg.wait_for_timeout(1500); check("reload restores step mode (recipe+step in snapshot)", "Step 4 of 5" in txt())
    post("list.updated", {"list": "shopping", "action": "add", "items": ["Eggs", "Limes"], "highlight": ["Limes"]}); pg.wait_for_timeout(300)
    check("lower-priority event during step -> toast, stays in step", "Step 4 of 5" in txt() and "Added Limes" in txt()); shot("10-step-toast")
    post("media.state", {"state": "playing", "title": "Clair de Lune", "artist": "Claude Debussy", "album": "Suite bergamasque", "position_s": 134, "duration_s": 303, "player": "Kitchen speaker", "focus": True})
    pg.wait_for_timeout(300); check("media focus during step -> stays in step", "Step 4 of 5" in txt())
    pg.click("#x"); pg.wait_for_timeout(400); check("dismiss -> idle with now-playing strip", "Clair de Lune" in txt() and "Shopping" in txt()); shot("11-idle-playing")
    post("media.state", {"state": "playing", "title": "Clair de Lune", "artist": "Claude Debussy", "album": "Suite bergamasque", "position_s": 134, "duration_s": 303, "player": "Kitchen speaker", "focus": True})
    shot("12-now-playing"); pg.click("[data-a='media.toggle'].pri"); pg.wait_for_timeout(400)
    check("media tap -> hub toast (MA not wired)", "isn't connected" in txt())
    post("error", {"code": "bot_timeout", "message": "Couldn’t reach the search bot", "detail": "Your request is saved and will retry.", "retry_in_s": 60}); shot("13-error")
    post("home.done", {"text": "Kitchen lights off"}); pg.wait_for_timeout(300); check("home.done toast", "Kitchen lights off" in txt())
    taps = json.loads(urllib.request.urlopen(URL + "/api/taps").read())
    check("hub received taps", [t["action"] for t in taps][:4] == ["recipe.next"] * 3 + ["dismiss"], [t["action"] for t in taps])
    pg2 = b.new_page(viewport={"width": 1280, "height": 800}); pg2.goto(URL + "/?hour=23&min=30"); pg2.wait_for_timeout(1500)
    check("night dim on at 23:30", pg2.locator("#dim.on").count() == 1); pg2.screenshot(path=str(OUT / "14-night-dim.png"))
    pg2.close()
    srv.terminate(); srv.wait(); pg.wait_for_timeout(2500); check("offline indicator", "Offline" in txt()); shot("15-offline")
    check("no JS errors", not errs, errs)
    b.close()
finally:
    if srv.poll() is None: srv.terminate()
    os.unlink(lists.name)
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} passed" + (f"; FAILED: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
