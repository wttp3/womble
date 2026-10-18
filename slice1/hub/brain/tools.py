"""One tool registry for the brain.

Responses and realtime voice both take the flat xAI shape
{"type":"function","name","description","parameters"}. Handlers stay here so
the print guard and the list/frame behaviour apply to both APIs.

Slice 1: lists, frame, a guarded print stub, and server-side web_search.
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
]

BY_NAME = {t.name: t for t in TOOLS}


def _web_search_enabled(include_web_search):
    if include_web_search is not None:
        return include_web_search
    return os.environ.get("BRAIN_WEB_SEARCH", "1") != "0"


def responses_tools(include_web_search=None) -> list:
    """Tools for POST /v1/responses. web_search runs on xAI's side."""
    tools = [{"type": "web_search"}] if _web_search_enabled(include_web_search) else []
    tools.extend(t.schema() for t in TOOLS)
    return tools


def realtime_tools(include_web_search=None) -> list:
    """Same flat schemas for the realtime voice API, when that lands."""
    return responses_tools(include_web_search)
