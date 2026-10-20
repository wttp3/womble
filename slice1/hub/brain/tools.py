"""One tool registry for the brain.

Responses and realtime voice both take the flat xAI shape
{"type":"function","name","description","parameters"}. Handlers stay here so
the print guard and the list/frame behaviour apply to both APIs.

Slice 1: lists, frame, a guarded print stub, and server-side web_search.
Media M1: list_library (read-only spoken summary; the grid goes to the frame).
Timers and the research-bot hand-off are later slices and are not registered.
"""
import os, re

from router import _list_name

_PRINT_RE = re.compile(r"\bprint\b", re.I)
REFUSED = "refused"
NOT_AVAILABLE = "not_available"


def allows_print(text: str) -> bool:
    """True only when the user's own sentence contains the word print."""
    return bool(_PRINT_RE.search(text or ""))


class ToolContext:
    def __init__(self, text, source, session, store, display):
        self.text = text or ""
        self.source = source
        self.session = session
        self.store = store
        self.display = display
        self.card = None


async def list_add(args, ctx: ToolContext) -> dict:
    item = str((args or {}).get("item") or "").strip()
    if not item:
        return {"ok": False, "error": "missing item"}
    lst = _list_name((args or {}).get("list"))
    items = ctx.store.add(item, lst)
    ctx.card = {"type": "list", "title": lst.title(), "items": items, "highlight": item}
    return {"ok": True, "list": lst, "items": items}


async def list_read(args, ctx: ToolContext) -> dict:
    lst = _list_name((args or {}).get("list"))
    items = ctx.store.show(lst)
    ctx.card = {"type": "list", "title": lst.title(), "items": items}
    return {"ok": True, "list": lst, "items": items, "count": len(items)}


async def frame_show(args, ctx: ToolContext) -> dict:
    title = str((args or {}).get("title") or "").strip() or "Womble"
    body = str((args or {}).get("body") or "").strip()
    ctx.card = {"type": "text", "title": title, "body": body}
    if ctx.display is not None:
        await ctx.display.emit("reply", {"title": title, "body": body, "source": ctx.source})
    return {"ok": True}


async def frame_notify(args, ctx: ToolContext) -> dict:
    text = str((args or {}).get("text") or "").strip()
    if ctx.display is not None:
        await ctx.display.emit("toast", {"text": text, "kind": "info"})
    return {"ok": True, "text": text}


async def _print_guard(ctx: ToolContext) -> dict:
    """Slice 4 wires the printer. Until then the call is refused, or stubbed."""
    if not allows_print(ctx.text):
        return {"ok": False, "error": REFUSED,
                "reason": "printing only happens when the user says print"}
    return {"ok": False, "error": NOT_AVAILABLE,
            "reason": "the printer is not connected yet"}


async def print_list(args, ctx: ToolContext) -> dict:
    return await _print_guard(ctx)


async def print_text(args, ctx: ToolContext) -> dict:
    return await _print_guard(ctx)


_LIBRARY = None


def media_library():
    """Lazy shared Library (data/media.sqlite, or MEDIA_DB). Seeds idempotently on first use."""
    global _LIBRARY
    if _LIBRARY is None:
        from media.library import Library
        _LIBRARY = Library()
    return _LIBRARY


async def list_library(args, ctx: ToolContext) -> dict:
    kind = str((args or {}).get("type") or "").strip().lower() or None
    if kind in ("radios", "stations", "radio stations"): kind = "radio"
    if kind in ("podcasts",): kind = "podcast"
    if kind in ("music", "presets", "playlists"): kind = "music_preset"
    from media.library import TYPES
    if kind and kind not in TYPES:
        kind = None
    lib = media_library()
    summary = lib.summary(kind)
    names = [e["name"] for e in lib.entries(kind)]
    grid = lib.grid()
    lines = []
    for t, label in (("radio", "Radio"), ("podcast", "Podcasts"), ("music_preset", "Music")):
        if grid[t] and (kind is None or kind == t):
            lines.append(label + ": " + ", ".join(x["name"] for x in grid[t]))
    ctx.card = {"type": "text", "title": "Media library", "body": "\n".join(lines) or summary}
    if ctx.display is not None:
        await ctx.display.emit("media.library", {"entries": grid, "type": kind})
    return {"ok": True, "summary": summary, "names": names, "count": len(names),
            "say": "Speak the summary in one sentence."}


MEDIA = None   # media.service.MediaService, set by the hub at startup (M3)


def _media_source(ctx):
    src = str(ctx.source or "")
    if src.startswith("alexa:"):
        return "alexa", src.split(":", 1)[1]
    return "box3", None


async def play_media(args, ctx: ToolContext) -> dict:
    if MEDIA is None:
        return {"ok": False, "error": NOT_AVAILABLE}
    name = str((args or {}).get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing name"}
    if re.match(r"^https?://", name, re.I):
        return {"ok": False, "error": REFUSED, "reason": "only library entries can be played"}
    episode = str((args or {}).get("episode") or "").lower()
    mode = {"latest": "latest", "resume": "resume"}.get(episode, "auto")
    source, echo = _media_source(ctx)
    res = await MEDIA.play(name=name, room=(args or {}).get("room"), source=source, echo=echo, mode=mode)
    if res.get("route") == "frida" and source == "alexa":
        entry = MEDIA.lib.get(res["entry_id"])
        res = {**res, "speech": f"Say, play {entry['name'] if entry else name}."}
    return {"ok": bool(res.get("ok")), "route": res.get("route"), "say": res.get("speech")}


async def stop_media(args, ctx: ToolContext) -> dict:
    if MEDIA is None:
        return {"ok": False, "error": NOT_AVAILABLE}
    source, echo = _media_source(ctx)
    res = await MEDIA.stop(room=(args or {}).get("room"), source=source, echo=echo)
    return {"ok": bool(res.get("ok")), "say": res.get("speech")}


class Tool:
    def __init__(self, name, description, parameters, handler, scope):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler
        self.scope = scope

    def schema(self) -> dict:
        return {"type": "function", "name": self.name, "description": self.description,
                "parameters": self.parameters}


_LIST = {
    "type": "string",
    "description": "List name. Omit it, or pass shopping, for the shopping list. Use to-do for tasks.",
}

TOOLS = [
    Tool(
        "list_add",
        "Add one item to a household list. Use this when someone wants something remembered or bought.",
        {"type": "object", "properties": {
            "item": {"type": "string", "description": "The item, without the list name."},
            "list": _LIST,
        }, "required": ["item"]},
        list_add, "house",
    ),
    Tool(
        "list_read",
        "Read the items on a household list.",
        {"type": "object", "properties": {"list": _LIST}, "required": []},
        list_read, "read",
    ),
    Tool(
        "frame_show",
        "Show a short note on the kitchen frame.",
        {"type": "object", "properties": {
            "title": {"type": "string", "description": "A few words."},
            "body": {"type": "string", "description": "One or two short sentences."},
        }, "required": ["title", "body"]},
        frame_show, "house",
    ),
    Tool(
        "frame_notify",
        "Show a small toast on the kitchen frame.",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "A short line."},
        }, "required": ["text"]},
        frame_notify, "house",
    ),
    Tool(
        "print_list",
        "Print a list on the kitchen printer. Only when the person's own words ask to print. "
        "The hub refuses this call otherwise.",
        {"type": "object", "properties": {"list": _LIST}, "required": []},
        print_list, "house",
    ),
    Tool(
        "print_text",
        "Print a short note on the kitchen printer. Only when the person's own words ask to print. "
        "The hub refuses this call otherwise.",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["title", "body"]},
        print_text, "house",
    ),
    Tool(
        "list_library",
        "List what Womble can play: radio stations, podcasts and music presets in the media library. "
        "Use for 'what's in my library' or 'what can you play'. Answer with the one-sentence summary.",
        {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["radio", "podcast", "music_preset"],
                     "description": "Optional: only this kind."},
        }, "required": []},
        list_library, "read",
    ),
    Tool(
        "play_media",
        "Play a radio station, podcast or music preset from the media library. Only library names; "
        "never a URL. Say the returned 'say' line.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "Library name as spoken, e.g. LBC or the DSR Daily."},
            "room": {"type": "string", "description": "Optional room, e.g. kitchen."},
            "episode": {"type": "string", "enum": ["latest", "resume"], "description": "Optional."},
        }, "required": ["name"]},
        play_media, "house",
    ),
    Tool(
        "stop_media",
        "Stop or pause what Womble started playing. Say the returned 'say' line.",
        {"type": "object", "properties": {"room": {"type": "string"}}, "required": []},
        stop_media, "house",
    ),
]

BY_NAME = {t.name: t for t in TOOLS}


# Live facts only. "How do I boil an egg?" does not match; offering web_search
# there makes the model hunt before the first spoken token.
_LIVE_FACT = re.compile(
    r"\b("
    r"today|tonight|tomorrow|yesterday|"
    r"this (?:week|weekend|morning|evening)|"
    r"latest|current(?:ly)?|right now|"
    r"news|weather|forecast|scores?|prices?|"
    r"who won|what happened"
    r")\b",
    re.I,
)


def wants_live_fact(text: str) -> bool:
    return bool(_LIVE_FACT.search(text or ""))


def _web_search_enabled(include_web_search):
    if include_web_search is not None:
        return include_web_search
    return os.environ.get("BRAIN_WEB_SEARCH", "1") != "0"


def responses_tools(include_web_search=None, text=None) -> list:
    """Tools for POST /v1/responses. web_search runs on xAI's side.

    With no utterance, the full list is returned (realtime session, and
    BRAIN_WEB_SEARCH still turns it off). Pass text and search is attached
    only when the sentence looks like a live fact.
    """
    enabled = _web_search_enabled(include_web_search)
    if enabled and text is not None and include_web_search is None:
        enabled = wants_live_fact(text)
    tools = [{"type": "web_search"}] if enabled else []
    tools.extend(t.schema() for t in TOOLS)
    return tools


def realtime_tools(include_web_search=None) -> list:
    """Full flat schemas for a long-lived realtime session, when that lands."""
    return responses_tools(include_web_search)
