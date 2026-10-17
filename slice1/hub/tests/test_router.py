"""Table test for router.route and the list store. No MLX, no network.

  python tests/test_router.py
"""
import json, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lists as lists_mod
import router

assert "mlx_whisper" not in sys.modules and "mlx_audio" not in sys.modules

def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL " + msg)

def main():
    root = Path(tempfile.mkdtemp())
    store = lists_mod.ListStore(root / "lists.json")

    # (phrase, recording_active, category, speech, extra)
    # extra: item / list / speaker / only, matched against the result
    rows = [
        ("add milk", False, "list.add", "Added milk to shopping.", {"item": "milk", "list": "shopping"}),
        ("please add oat milk to the shopping list", False, "list.add", "Added oat milk to shopping.",
         {"item": "oat milk", "list": "shopping"}),
        ("put eggs on my shopping list", False, "list.add", "Added eggs to shopping.",
         {"item": "eggs", "list": "shopping"}),
        ("add milk to the shopping list.", False, "list.add", "Added milk to shopping.",
         {"item": "milk", "list": "shopping"}),
        # C8: trailing politeness and punctuation, bare "the list" -> shopping
        ("add milk to the shopping list, please.", False, "list.add", "Added milk to shopping.",
         {"item": "milk", "list": "shopping"}),
        ("Add milk to the list", False, "list.add", "Added milk to shopping.",
         {"item": "milk", "list": "shopping"}),
        ("add milk to the list, please", False, "list.add", "Added milk to shopping.",
         {"item": "milk", "list": "shopping"}),
        # C7: to-do / to do / todo are one list
        ("add bread to the to-do list", False, "list.add", "Added bread to to-do.",
         {"item": "bread", "list": "to-do"}),
        ("add eggs to the to do list", False, "list.add", "Added eggs to to-do.",
         {"item": "eggs", "list": "to-do"}),
        ("add milk to the todo list", False, "list.add", "Added milk to to-do.",
         {"item": "milk", "list": "to-do"}),
        ("add butter to to-do", False, "list.add", "Added butter to to-do.",
         {"item": "butter", "list": "to-do"}),
        # C8: RECORD must not hijack an add
        ("add record for andrew", False, "list.add", "Added record for andrew to shopping.",
         {"item": "record for andrew", "list": "shopping"}),
        ("Add record for andrew.", False, "list.add", "Added record for andrew to shopping.",
         {"item": "record for andrew", "list": "shopping"}),
        ("add record for vic to the to-do list", False, "list.add", "Added record for vic to to-do.",
         {"item": "record for vic", "list": "to-do"}),
        ("add record for andrew, please", False, "list.add", "Added record for andrew to shopping.",
         {"item": "record for andrew", "list": "shopping"}),
        # record commands still win when they are the command
        ("record for andrew", False, "record.start", "Recording Womble for Andrew. Say it after each beep.",
         {"speaker": "andrew"}),
        ("please record for victoria", False, "record.start",
         "Recording Womble for Vic. Say it after each beep.", {"speaker": "vic"}),
        ("record for Vic", False, "record.start", "Recording Womble for Vic. Say it after each beep.",
         {"speaker": "vic"}),
        ("record other words for andrew", False, "record.start",
         "Recording other words for Andrew. Read each one from the screen after the beep.",
         {"speaker": "andrew", "only": "neg"}),
        ("record negatives for vic", False, "record.start",
         "Recording other words for Vic. Read each one from the screen after the beep.",
         {"speaker": "vic", "only": "neg"}),
        ("please record not womble for victoria", False, "record.start",
         "Recording other words for Vic. Read each one from the screen after the beep.",
         {"speaker": "vic", "only": "neg"}),
        ("stop", True, "record.stop", "Stopped recording.", {}),
        ("stop recording.", True, "record.stop", "Stopped recording.", {}),
        ("please stop", True, "record.stop", "Stopped recording.", {}),
        ("stop, please", True, "record.stop", "Stopped recording.", {}),
        ("stop", False, "fallback", "Sorry, I can only do lists for now.", {}),
        ("Find me a quick lentil curry recipe", False, "fallback", "Sorry, I can only do lists for now.", {}),
    ]
    for phrase, active, cat, speech, extra in rows:
        res = router.route(phrase, store, active)
        check(res["category"] == cat, f"{phrase!r} category {res['category']} != {cat}")
        check(res["speech"] == speech, f"{phrase!r} speech {res['speech']!r} != {speech!r}")
        if "speaker" in extra:
            check(res.get("speaker") == extra["speaker"], f"{phrase!r} speaker {res.get('speaker')!r}")
        if "only" in extra:
            check(res.get("only") == extra["only"], f"{phrase!r} only {res.get('only')!r}")
        if cat == "list.add":
            check(res["card"]["highlight"] == extra["item"], f"{phrase!r} highlight")
            check(res["card"]["title"] == extra["list"].title(), f"{phrase!r} title")
            check(extra["item"] in res["card"]["items"], f"{phrase!r} card items")
        elif cat == "record.start":
            who = extra["speaker"].title()
            title = f"Record {'other words' if extra.get('only') == 'neg' else 'Womble'} · {who}"
            check(res["card"] == {"type": "text", "title": title, "body": "Starting…"}, f"{phrase!r} card")
    print(f"PASS {len(rows)} route rows")

    # C8 show phrases, including the shopping default and "what is"
    show_rows = [
        ("what's on the list", "shopping"),
        ("show the list", "shopping"),
        ("show the list, please", "shopping"),
        ("What's on the shopping list", "shopping"),
        ("what is on the shopping list", "shopping"),
        ("what is on the to-do list", "to-do"),
        ("what's on my to-do list?", "to-do"),
        ("show the to do list", "to-do"),
        ("read the todo list", "to-do"),
    ]
    # counts after the adds above
    expect_n = {"shopping": None, "to-do": None}
    data = store.load()
    for name, rows_ in data.items():
        expect_n[name] = len(rows_)
    for phrase, lst in show_rows:
        res = router.route(phrase, store, False)
        n = expect_n[lst]
        check(res["category"] == "list.show", f"show {phrase!r} -> {res['category']}")
        check(res["speech"] == f"{n} items on {lst}.", f"show {phrase!r} speech {res['speech']!r}")
        check(res["card"]["title"] == lst.title() and res["card"]["items"] == store.show(lst),
              f"show {phrase!r} card")
    print("PASS show phrases (shopping default, what is, to-do spellings)")

    # one list on disk for the three to-do spellings; file shape unchanged
    raw = json.loads((root / "lists.json").read_text())
    check(set(raw) == {"shopping", "to-do"}, f"keys {set(raw)}")
    check("todo" not in raw and "to do" not in raw, "spellings collapsed")
    for name, rows_ in raw.items():
        for entry in rows_:
            check(set(entry) == {"item", "ts"} and isinstance(entry["item"], str) and isinstance(entry["ts"], float),
                  f"shape {entry}")
    check(not (root / "lists.json.tmp").exists(), "tmp file left behind")
    todo = [e["item"] for e in raw["to-do"]]
    check(todo == ["bread", "eggs", "milk", "butter", "record for vic"], f"to-do items {todo}")
    print("PASS lists.json shape, atomic replace, to-do normalised")

    # tolerant load: empty and corrupt files are renamed aside
    def _bad_load(name, text):
        d = root / name
        d.mkdir()
        store_ = lists_mod.ListStore(d / "lists.json")
        (d / "lists.json").write_text(text)
        check(store_.load() == {}, name)
        moved = list(d.glob("lists.corrupt-*.json"))
        check(len(moved) == 1 and not (d / "lists.json").exists(), f"{name} renamed {moved}")
        check(moved[0].read_text() == text, f"{name} bytes kept")
    _bad_load("empty", "   \n")
    _bad_load("corrupt", "{not json")
    print("PASS tolerant load (empty and corrupt)")

    # "read me the shopping list" stays the old capture (not a shopping default)
    res = router.route("read me the shopping list", store, False)
    check(res["category"] == "list.show" and "me the shopping" in res["speech"],
          f"unchanged read-me capture: {res}")
    print("PASS unchanged non-goals")
    print("test_router OK")

if __name__ == "__main__":
    main()
