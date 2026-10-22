"""Chat (CHAT-SPEC v2.7): reply gains id, display and key_fact; old fields stay. python tests/test_reply_fields.py"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from display import Display, key_fact, reply_display, first_sentence


def check(c, m):
    if not c: raise SystemExit(f"FAIL {m}")


check(first_sentence("17° and light rain. Stops at 18:00.") == "17° and light rain.", "first sentence")
check(key_fact("17° and light rain. Stops at 18:00.") == "17° and light rain", "key fact = short first sentence")
long1 = "Allow twenty minutes per five hundred grams plus twenty more minutes for a medium bird. Rest it."
check(key_fact(long1) is None, "no key fact when the first sentence is > 12 words")
check(key_fact("") is None, "no key fact for empty body")
print("PASS key_fact heuristic")

check(reply_display("Done.") == "speak", "<= 12 words -> speak")
check(reply_display(" ".join(["w"] * 20)) == "short", "13-25 words -> short")
check(reply_display(" ".join(["w"] * 26)) == "long", "> 25 words -> long")
check(reply_display("Done.", ["a", "b"]) == "list", "items -> list")
print("PASS display rules")


class FakeWS:
    def __init__(self): self.out = []
    async def send_str(self, s): self.out.append(json.loads(s))


async def main():
    d = Display(lists_loader=lambda: {}); ws = FakeWS(); d.clients.add(ws)
    body = "It is 17 degrees. " + " ".join(["more"] * 70)
    await d.emit("reply", {"title": "Weather", "body": body})
    r = ws.out[-1]["data"]
    check(r["title"] == "Weather" and r["body"] == body and r["short"].endswith("…"), "old fields kept (title, body, 60-word short)")
    check(len(r["id"]) == 32 and r["display"] == "long" and r["key_fact"] == "It is 17 degrees", "new fields id/display/key_fact")
    await d.emit("reply", {"title": "x", "body": "Yes.", "display": "short", "key_fact": "Yes", "id": "abc"})
    r = ws.out[-1]["data"]
    check(r["id"] == "abc" and r["display"] == "short" and r["key_fact"] == "Yes", "caller-supplied fields win")
    print("PASS emit adds reply fields")
    # voice.stop with no handler is a no-op; recipe.repeat re-sends the current step
    await d.on_tap("voice.stop")
    called = []
    async def stop(): called.append(1)
    d.voice_stop_handler = stop; await d.on_tap("voice.stop"); check(called == [1], "voice.stop calls the hook")
    await d.emit("recipe", {"title": "T", "steps": ["one", "two"]}); await d.on_tap("recipe.next"); n = len(ws.out)
    await d.on_tap("recipe.repeat"); m = ws.out[-1]
    check(len(ws.out) == n + 1 and m["event"] == "recipe.step" and m["data"]["index"] == 0, "recipe.repeat re-sends step")
    print("PASS voice.stop hook, recipe.repeat")

asyncio.run(main())
print("ALL PASS test_reply_fields")
