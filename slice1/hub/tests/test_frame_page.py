"""frame/f1: static checks on static/display.html (no browser).

Screens are pre-built sections updated in place; every hub event has a handler;
the page stays within Chrome 106 / ES2017 and the timing hooks are present.
python tests/test_frame_page.py"""
import re, sys
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "static" / "display.html").read_text()
JS = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def check(c, m):
    if not c: raise SystemExit(f"FAIL {m}")


SCREENS = ["idle", "recmenu", "record", "listening", "thinking", "reply", "list", "research", "recipe", "step",
           "playing", "error", "library", "cands"]
for s in SCREENS:
    check(f'id="s-{s}"' in HTML, f"section for {s}")
    check(re.search(r"\b" + s + r":function", JS), f"updater for {s}")
print("PASS every screen is a pre-built section with an in-place updater")

EVENTS = ["snapshot", "idle", "weather", "voice.listening", "voice.heard", "voice.cancel", "reply", "list.updated",
          "research.added", "recipe", "recipe.step", "media.library", "media.candidates", "media.state", "home.done",
          "record.state", "record.done", "toast", "error"]
for e in EVENTS:
    check(f"case '{e}':" in JS, f"handler for {e}")
check("d.grid||d.entries" in JS, "library card accepts list_library's {entries} shape")
print("PASS event handlers (incl. the library-list fix)")

check(not re.search(r"\$\('main'\)\.innerHTML", JS), "#main is never rebuilt")
check(".innerHTML=h" not in JS, "no whole-screen innerHTML string")
check("transform='scaleX(" in JS and "setInterval(function(){if(cur.s==='playing'" in JS, "progress bar on a local timer, transform only")
check("contain:layout paint" in HTML, "cards use contain")
check(not re.search(r"@keyframes[^}]*\b(width|height|top|left)\b", HTML), "animations use opacity/transform only")
check(not re.search(r"transition:[^;]*\b(width|height|top|left|background)\b", HTML), "transitions use opacity/transform only")
check(not re.search(r"backdrop-filter|filter:\s*blur|@font-face|fonts\.googleapis", HTML), "no blur, no webfonts")
print("PASS do/don't list (no #main rebuild, contain, transform/opacity, local progress timer, system fonts)")

# Chrome 106 ceiling: no syntax/APIs from later versions.
for pat, why in [(r"\.at\(", "Array.at is fine (92) but keep ES2017 style"), (r"\?\?=|\|\|=|&&=", "logical assignment"),
                 (r"structuredClone|\.findLast\(|\.toSorted\(|\.hasOwn\(", "post-106 APIs"), (r":has\(", "CSS :has (105 ok but avoid)"),
                 (r"\bclass\s+\w+\s*\{", "no classes (ES2017 house style)"), (r"=>", "no arrow functions (house style)")]:
    check(not re.search(pat, JS if "CSS" not in why else HTML), why)
print("PASS Chrome 106 / ES2017 style")

check(re.search(r"[\u2600-\u27bf\U0001F300-\U0001FAFF]", re.sub(r"<script>.*?</script>", "", HTML, flags=re.S).replace("\u2713", "").replace("\u2715", "")) is None,
      "no emoji glyphs in markup (inline SVG instead)")
check("paint_ack" in JS and "clock.pong" in JS and "fb_ms" in JS, "timing hooks")
check("function optimistic(" in JS and "optimistic(b.getAttribute('data-a'))" in JS, "optimistic transport on pointerdown")
check("checkVer(d.page_ver)" in JS, "reload on new page version")
print("PASS emoji-free markup, timing hooks, optimistic transport, self-reload")
print("ALL PASS test_frame_page")
