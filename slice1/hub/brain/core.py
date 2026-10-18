"""Brain turn: Responses API, tool loop, memory, timeout, circuit breaker.

Transport-neutral. handle() returns a Reply and does not know about the BOX-3.
"""
import asyncio, json, logging, os, re, threading, time
from dataclasses import dataclass
from pathlib import Path

import requests

from brain.key import get_key
from brain.tools import BY_NAME, ToolContext, responses_tools

log = logging.getLogger("hub.brain")
URL = "https://api.x.ai/v1/responses"
OFFLINE = "Connection unavailable."
PERSONA_PATH = Path(__file__).with_name("persona.md")
_SENT = re.compile(r"(.+?[.!?])(?:\s+|$)")
MAX_TOOL_ROUNDS = 3


def load_persona() -> str:
    return PERSONA_PATH.read_text(encoding="utf-8")


def pop_sentences(buf: str):
    """Split completed sentences off the front of buf. The tail stays unsplit."""
    ready = []
    while m := _SENT.match(buf):
        sentence = m.group(1).strip()
        if sentence:
            ready.append(sentence)
        buf = buf[m.end():]
    return ready, buf


class BrainError(Exception):
    pass


class BrainHTTPError(BrainError):
    def __init__(self, status: int):
        super().__init__(f"http {status}")
        self.status = status


@dataclass
class Reply:
    speech: str
    end_session: bool = True
    reprompt: str | None = None
    card: dict | None = None
    category: str = "brain"
    llm_ttft_ms: int | None = None
    llm_total_ms: int | None = None


class Memory:
    """Last N turns per session key, dropped after ttl seconds of silence."""

    def __init__(self, turns=None, ttl_s=None, clock=None):
        self.turns = int(turns if turns is not None else os.environ.get("BRAIN_MEMORY_TURNS", "6"))
        self.ttl = float(ttl_s if ttl_s is not None else os.environ.get("BRAIN_MEMORY_TTL_S", "300"))
        self.clock = clock or time.time
        self._store = {}

    def _live(self, key):
        rec = self._store.get(key)
        if not rec:
            return None
        if self.clock() - rec["ts"] > self.ttl:
            self._store.pop(key, None)
            return None
        return rec

    def history(self, key) -> list:
        rec = self._live(key)
        return list(rec["items"]) if rec else []

    def add(self, key, role, content):
        now = self.clock()
        rec = self._live(key)
        if rec is None:
            rec = {"ts": now, "items": []}
            self._store[key] = rec
        rec["items"].append({"role": role, "content": content})
        cap = max(2, self.turns * 2)
        if len(rec["items"]) > cap:
            rec["items"] = rec["items"][-cap:]
        rec["ts"] = now


class Breaker:
    """Opens after `limit` failures, then stays open for `cooldown` seconds."""

    def __init__(self, limit=None, cooldown=None, clock=None):
        self.limit = int(limit if limit is not None else os.environ.get("BRAIN_BREAKER_FAILS", "3"))
        self.cooldown = float(cooldown if cooldown is not None else os.environ.get("BRAIN_BREAKER_COOLDOWN_S", "60"))
        self.clock = clock or time.time
        self.fails = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        return self.clock() < self.open_until

    def failure(self) -> bool:
        """Record a failure. True when this failure opens the circuit."""
        self.fails += 1
        if self.fails >= self.limit:
            self.open_until = self.clock() + self.cooldown
            self.fails = 0
            return True
        return False

    def success(self):
        self.fails = 0
        self.open_until = 0.0


def iter_sse(resp):
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data = line[5:].strip()
        elif line.startswith("{"):
            data = line.strip()
        else:
            continue
        if not data:
            continue
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            log.warning("brain stream skipped a non-json line")


async def default_transport(payload: dict, key: str, timeout: float):
    """Stream SSE events from the Responses API. The key stays in the header only."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def worker():
        try:
            with requests.post(
                URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                stream=True,
                timeout=(min(3.0, timeout), timeout),
            ) as resp:
                if resp.status_code != 200:
                    loop.call_soon_threadsafe(queue.put_nowait, ("status", resp.status_code))
                    return
                for ev in iter_sse(resp):
                    loop.call_soon_threadsafe(queue.put_nowait, ("ev", ev))
        except Exception as exc:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ("err", exc))
            except RuntimeError:
                return
        finally:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ("end", None))
            except RuntimeError:
                pass

    threading.Thread(target=worker, name="brain-sse", daemon=True).start()
    while True:
        kind, val = await queue.get()
        if kind == "end":
            return
        if kind == "status":
            raise BrainHTTPError(val)
        if kind == "err":
            raise val
        yield val


def _message_text(response) -> str:
    if not response:
        return ""
    parts = []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        content = item.get("content") or []
        if isinstance(content, str):
            parts.append(content)
            continue
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                parts.append(c.get("text") or "")
    return "".join(parts).strip()


def _function_calls(response, seen) -> list:
    if response:
        found = [i for i in response.get("output") or [] if i.get("type") == "function_call"]
        if found:
            return found
    return seen


class Brain:
    def __init__(self, store, display=None, *, transport=None, key_getter=None, clock=None,
                 memory=None, breaker=None, timeout=None, persona=None):
        self.store = store
        self.display = display
        self.transport = transport or default_transport
        self.key_getter = key_getter or get_key
        self.clock = clock or time.time
        self.memory = memory or Memory(clock=self.clock)
        self.breaker = breaker or Breaker(clock=self.clock)
        self.timeout = float(timeout if timeout is not None else os.environ.get("BRAIN_TIMEOUT_S", "4"))
        self.persona = persona if persona is not None else load_persona()
        self.model = os.environ.get("BRAIN_MODEL", "grok-4.7")
        self.max_output_tokens = int(os.environ.get("BRAIN_MAX_OUTPUT_TOKENS", "200"))

    def _payload(self, items) -> dict:
        return {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "store": False,
            "max_output_tokens": self.max_output_tokens,
            "stream": True,
            "instructions": self.persona,
            "input": items,
            "tools": responses_tools(),
        }

    async def _emit_down(self):
        if self.display is None:
            return
        try:
            await self.display.emit("error", {"message": OFFLINE, "code": "brain"})
        except Exception as e:
            log.warning("brain error event failed (%s)", type(e).__name__)

    def _down(self, t0, ttft) -> Reply:
        total = int((time.perf_counter() - t0) * 1000)
        log.info("brain llm_ttft_ms=%s llm_total_ms=%s offline", "-" if ttft is None else ttft, total)
        return Reply(speech=OFFLINE, end_session=True, category="brain.offline",
                     card={"type": "text", "title": "Womble", "body": OFFLINE},
                     llm_ttft_ms=ttft, llm_total_ms=total)

    async def _fail(self, t0, ttft, exc) -> Reply:
        opened = self.breaker.failure()
        if opened:
            log.warning("brain circuit open for %.0fs", self.breaker.cooldown)
        else:
            log.warning("brain failed (%s)", type(exc).__name__ if exc is not None else "no-key")
        await self._emit_down()
        return self._down(t0, ttft)

    async def handle(self, text, source="box3-kitchen", session=None, on_sentence=None) -> Reply:
        """Answer one utterance. on_sentence(text) is sync and may be called mid-stream."""
        session = session or source
        t0 = time.perf_counter()
        ttft = None
        if self.breaker.is_open():
            await self._emit_down()
            return self._down(t0, None)
        try:
            key = self.key_getter()
        except Exception as e:
            return await self._fail(t0, None, e)
        if not key:
            return await self._fail(t0, None, None)
        try:
            reply = await asyncio.wait_for(
                self._handle(text, source, session, key, t0, on_sentence),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return await self._fail(t0, ttft, TimeoutError())
        except Exception as e:
            return await self._fail(t0, ttft, e)
        return reply

    async def _handle(self, text, source, session, key, t0, on_sentence) -> Reply:
        ctx = ToolContext(text, source, session, self.store, self.display)
        items = list(self.memory.history(session))
        items.append({"role": "user", "content": text})
        pending = ""
        parts = []
        ttft = None

        def push(delta: str, flush: bool = False):
            nonlocal pending, ttft
            if delta:
                if ttft is None:
                    ttft = int((time.perf_counter() - t0) * 1000)
                pending += delta
                ready, pending = pop_sentences(pending)
                for sentence in ready:
                    parts.append(sentence)
                    if on_sentence:
                        on_sentence(sentence)
            if flush:
                tail = pending.strip()
                pending = ""
                if tail:
                    parts.append(tail)
                    if on_sentence:
                        on_sentence(tail)

        deadline = time.perf_counter() + self.timeout
        for _ in range(MAX_TOOL_ROUNDS + 1):
            remaining = deadline - time.perf_counter()
            if remaining <= 0.2:
                raise TimeoutError("brain budget")
            response, seen_calls, streamed = await self._round(items, key, min(self.timeout, remaining), push)
            calls = _function_calls(response, seen_calls)
            if streamed and not parts and not pending:
                push(streamed)
            if calls:
                await self._append_tool_round(items, response, calls, ctx)
                continue
            if not pending and not parts:
                extra = _message_text(response)
                if extra and extra != "".join(parts):
                    push(extra)
            push("", flush=True)
            speech = " ".join(parts).strip()
            if not speech:
                speech = "Sorry, try that once more."
            self.breaker.success()
            self.memory.add(session, "user", text)
            self.memory.add(session, "assistant", speech)
            total = int((time.perf_counter() - t0) * 1000)
            log.info("brain llm_ttft_ms=%s llm_total_ms=%s", "-" if ttft is None else ttft, total)
            end_session = not speech.rstrip().endswith("?")
            return Reply(
                speech=speech,
                end_session=end_session,
                reprompt=None if end_session else "Anything else?",
                card=ctx.card,
                category="brain",
                llm_ttft_ms=ttft,
                llm_total_ms=total,
            )
        push("", flush=True)
        speech = " ".join(parts).strip() or "Sorry, try that once more."
        self.breaker.success()
        self.memory.add(session, "user", text)
        self.memory.add(session, "assistant", speech)
        total = int((time.perf_counter() - t0) * 1000)
        log.info("brain llm_ttft_ms=%s llm_total_ms=%s", "-" if ttft is None else ttft, total)
        end_session = not speech.rstrip().endswith("?")
        return Reply(speech=speech, end_session=end_session,
                     reprompt=None if end_session else "Anything else?",
                     card=ctx.card, category="brain", llm_ttft_ms=ttft, llm_total_ms=total)

    async def _round(self, items, key, timeout, push):
        response = None
        seen = []
        streamed = []
        async for ev in self.transport(self._payload(items), key, timeout):
            typ = ev.get("type")
            if typ == "response.output_text.delta":
                delta = ev.get("delta") or ""
                streamed.append(delta)
                push(delta)
            elif typ == "response.output_item.done":
                item = ev.get("item") or {}
                if item.get("type") == "function_call":
                    seen.append(item)
            elif typ in ("response.completed", "response.complete"):
                response = ev.get("response") or {}
            elif typ in ("response.failed", "error"):
                raise BrainError(typ)
        return response, seen, "".join(streamed)

    async def _append_tool_round(self, items, response, calls, ctx):
        # Pass reasoning and server-side tool items back unchanged so the next
        # round keeps them. They are not written into conversation memory.
        output = [i for i in ((response or {}).get("output") or []) if i.get("type") != "message"]
        handled = False
        for item in output:
            items.append(item)
            if item.get("type") == "function_call":
                handled = True
                items.append(await self._tool_output(item, ctx))
        if handled:
            return
        for call in calls:
            items.append({
                "type": "function_call",
                "call_id": call.get("call_id") or call.get("id") or "",
                "name": call.get("name") or "",
                "arguments": call.get("arguments") if isinstance(call.get("arguments"), str) else json.dumps(call.get("arguments") or {}),
            })
            items.append(await self._tool_output(call, ctx))

    async def _tool_output(self, call, ctx):
        arguments = call.get("arguments") if call.get("arguments") is not None else "{}"
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        return {"type": "function_call_output",
                "call_id": call.get("call_id") or call.get("id") or "",
                "output": await _run_tool(call.get("name") or "", arguments, ctx)}


async def _run_tool(name, arguments, ctx) -> str:
    tool = BY_NAME.get(name)
    if tool is None:
        return json.dumps({"ok": False, "error": "unknown tool"})
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments")
    except (ValueError, TypeError):
        return json.dumps({"ok": False, "error": "bad arguments"})
    try:
        result = await tool.handler(args, ctx)
    except Exception as e:
        log.warning("tool %s failed (%s)", name, type(e).__name__)
        result = {"ok": False, "error": type(e).__name__}
    return json.dumps(result)
