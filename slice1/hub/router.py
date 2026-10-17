"""Rule router for list and record commands. Sync. No ML imports.

route(text, store, recording_active) keeps the historical reply phrases.
List names 'to-do', 'to do' and 'todo' are one list, stored as 'to-do'.
"""
import re

# Hyphen is allowed so "to-do" is a list name, not part of the item.
ADD_RE = re.compile(
    r"^(?:please\s+)?(?:add|put)\s+(?P<item>.+?)"
    r"(?:\s+(?:to|on)\s+(?:the\s+|my\s+)?(?P<list>[\w -]+?)(?:\s+list)?)?[.!]?$",
    re.I,
)
RECORD_RE = re.compile(r"\brecord\b.*?\bfor\s+(?P<who>andrew|vic\w*|victoria)\b", re.I)
SHOW_RE = re.compile(
    r"^(?:what(?:'s| is)|show|read)\s+(?:on\s+)?(?:the\s+|my\s+)?(?P<list>[\w -]+?)\s+list",
    re.I,
)
_STOP_RE = re.compile(r"(?:please\s+)?stop(?: recording)?[.!]?", re.I)
_PLEASE_RE = re.compile(r"(?:[,;]\s*|\s+)please[.!?]*$", re.I)
_STOP_LISTS = {"", "list", "the", "my"}


def _prepare(text: str) -> str:
    """Drop a trailing ', please' (or ' please') and trailing punctuation."""
    t = text.strip()
    t = _PLEASE_RE.sub("", t).strip()
    return t.rstrip(" \t.,!?;:")


def _list_name(raw: str | None) -> str:
    if raw is None:
        return "shopping"
    name = re.sub(r"\s+", " ", raw.strip().lower())
    if name in _STOP_LISTS:
        return "shopping"
    if name == "todo" or re.fullmatch(r"to[\s-]?do", name):
        return "to-do"
    return name


def route(text: str, store, recording_active: bool = False) -> dict:
    t = _prepare(text)
    # Add wins over RECORD so "add record for andrew" is a list item, not a session.
    if m := ADD_RE.match(t):
        item, lst = m["item"].strip(), _list_name(m["list"])
        items = store.add(item, lst)
        return {"category": "list.add", "speech": f"Added {item} to {lst}.",
                "card": {"type": "list", "title": lst.title(), "items": items, "highlight": item}}
    if m := RECORD_RE.search(t):
        who = "vic" if m["who"].lower().startswith("vic") else "andrew"
        if re.search(r"\b(other words|not womble|negatives?)\b", t, re.I):
            return {"category": "record.start", "speaker": who, "only": "neg",
                    "speech": f"Recording other words for {who.title()}. Read each one from the screen after the beep.",
                    "card": {"type": "text", "title": f"Record other words · {who.title()}", "body": "Starting…"}}
        return {"category": "record.start", "speaker": who,
                "speech": f"Recording Womble for {who.title()}. Say it after each beep.",
                "card": {"type": "text", "title": f"Record Womble · {who.title()}", "body": "Starting…"}}
    if _STOP_RE.fullmatch(t) and recording_active:
        return {"category": "record.stop", "speech": "Stopped recording.",
                "card": {"type": "text", "title": "Recording stopped", "body": ""}}
    if m := SHOW_RE.match(t):
        lst = _list_name(m["list"])
        items = store.show(lst)
        return {"category": "list.show", "speech": f"{len(items)} items on {lst}.",
                "card": {"type": "list", "title": lst.title(), "items": items}}
    return {"category": "fallback", "speech": "Sorry, I can only do lists for now.",
            "card": {"type": "text", "title": "Heard", "body": t}}
