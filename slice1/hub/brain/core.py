"""Brain turn: Responses API, tool loop, memory, timeout, circuit breaker.

Transport-neutral. handle() returns a Reply and does not know about the BOX-3.
"""
import asyncio, contextvars, json, logging, os, re, socket, threading, time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.exceptions import Timeout as RequestsTimeout

from brain.key import get_key
from brain.tools import BY_NAME, ToolContext, responses_tools

log = logging.getLogger("hub.brain")
URL = "https://api.x.ai/v1/responses"
_API_HOST = "api.x.ai"
_trace_var = contextvars.ContextVar("brain_trace", default=None)
_announced = False
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


def iter_sse(resp, trace=None):
    for raw in resp.iter_lines(decode_unicode=True):
        if trace is not None:
            trace.mark("first-byte")
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


def _proxy_label() -> str:
    """Proxy host only. Userinfo in the URL is never logged."""
    seen = []
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            parsed = urlparse(raw if "://" in raw else "http://" + raw)
            if not parsed.hostname:
                host = "set"
            else:
                host = f"{parsed.scheme}://{parsed.hostname}"
                if parsed.port:
                    host += f":{parsed.port}"
        except Exception:
            host = "set"
        if host not in seen:
            seen.append(host)
    return ",".join(seen) if seen else "off"


def _families(host, port=443) -> str:
    """Address-family counts for host. No addresses are logged."""
    infos = socket.getaddrinfo(host, port, socket.SOCK_STREAM)
    v4 = sum(1 for item in infos if item[0] == socket.AF_INET)
    v6 = sum(1 for item in infos if item[0] == socket.AF_INET6)
    return f"ipv4={v4},ipv6={v6}"


def _install_ipv4_first():
    """Try IPv4 before IPv6 for urllib3 only.

    urllib3 walks getaddrinfo order and gives each address the full connect
    timeout. A blackholed IPv6 route (common when a shell has a proxy and a
    LaunchAgent does not) can spend the whole turn before the first header.
    The TLS hostname stays the URL host; only the attempt order changes.
    """
    import urllib3.util.connection as cn
    if getattr(cn, "_womble_ipv4_first", False):
        return

    def create_connection(address, timeout=cn._DEFAULT_TIMEOUT, source_address=None, socket_options=None):
        host, port = address
        if isinstance(host, str) and host.startswith("["):
            host = host.strip("[]")
        try:
            host.encode("idna")
        except UnicodeError:
            raise cn.LocationParseError(f"'{host}', label empty or too long") from None
        family = cn.allowed_gai_family()
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        infos.sort(key=lambda item: item[0] != socket.AF_INET)
        err = None
        for af, socktype, proto, _canon, sa in infos:
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                cn._set_socket_options(sock, socket_options)
                if timeout is not cn._DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                return sock
            except OSError as exc:
                err = exc
                if sock is not None:
                    sock.close()
        if err is not None:
            raise err
        raise OSError("getaddrinfo returns an empty list")

    cn.create_connection = create_connection
    cn._womble_ipv4_first = True


class TurnTrace:
    """Stages updated from the request thread, so a cancelled turn can still say where it stuck.

    The asyncio timeout used to log llm_ttft_ms=- even after tokens had arrived:
    that number lived only inside the task wait_for had just cancelled.
    """

    def __init__(self):
        self.stage = "start"
        self.http_status = None
        self.worker_ttft_ms = None
        self.loop_ttft_ms = None
        self.dns = None
        self.proxy = _proxy_label()
        self.key_source = None
        self.pulses = 0
        self.max_gap_ms = 0
        self._prev = time.perf_counter()
        self.t0 = self._prev
        self._lock = threading.Lock()

    def mark(self, stage, http_status=None):
        with self._lock:
            if stage == "first-byte" and self.stage != "status":
                return
            now = int((time.perf_counter() - self.t0) * 1000)
            if stage == "first-token" and self.worker_ttft_ms is None:
                self.worker_ttft_ms = now
            self.stage = stage
            if http_status is not None:
                self.http_status = http_status
            self.elapsed_ms = now

    def note_loop_ttft(self, ms):
        with self._lock:
            if self.loop_ttft_ms is None:
                self.loop_ttft_ms = ms

    def pulse(self):
        now = time.perf_counter()
        with self._lock:
            gap = int((now - self._prev) * 1000)
            self._prev = now
            self.pulses += 1
            if gap > self.max_gap_ms:
                self.max_gap_ms = gap

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "stage": self.stage,
                "http": "-" if self.http_status is None else str(self.http_status),
                "worker_ttft_ms": self.worker_ttft_ms,
                "loop_ttft_ms": self.loop_ttft_ms,
                "dns": self.dns or "-",
                "proxy": self.proxy,
                "key": self.key_source or "-",
                "loop_pulses": self.pulses,
                "loop_gap_ms": self.max_gap_ms,
            }


def _fmt_ms(value):
    return "-" if value is None else str(value)


def _announce(trace: TurnTrace):
    """Once per process: the environment a LaunchAgent has and a shell may not."""
    global _announced
    if _announced:
        return
    _announced = True
    log.info("brain transport proxy=%s key=%s", trace.proxy, trace.key_source)


def _timeout_exc(exc) -> bool:
    """True for our budget, asyncio.wait_for, and a requests connect/read timeout."""
    return isinstance(exc, (TimeoutError, RequestsTimeout))


def iter_sse_marked(resp, trace):
    """iter_sse, but the request thread records first byte and first text token."""
    for ev in iter_sse(resp, trace):
        if trace is not None and ev.get("type") == "response.output_text.delta" and (ev.get("delta") or ""):
            trace.mark("first-token")
        yield ev


async def default_transport(payload: dict, key: str, timeout: float):
    """Stream SSE events from the Responses API. The key stays in the header only.

    Events are handed to the loop with call_soon_threadsafe. Stage marks are
    written on the request thread, because a timeout cancels the consumer
    while this thread may still be blocked in DNS, connect, or iter_lines.
    """
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    trace = _trace_var.get()
    _install_ipv4_first()

    def worker():
        try:
            if trace is not None:
                trace.mark("dns")
                try:
                    trace.dns = _families(_API_HOST)
                except Exception:
                    trace.mark("dns-error")
                    raise
                trace.mark("connect")
            with requests.post(
                URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                stream=True,
                timeout=(min(3.0, timeout), timeout),
            ) as resp:
                if trace is not None:
                    trace.mark("status", http_status=resp.status_code)
                if resp.status_code != 200:
                    loop.call_soon_threadsafe(queue.put_nowait, ("status", resp.status_code))
                    return
                for ev in iter_sse_marked(resp, trace):
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
        self.timeout = float(timeout if timeout is not None else os.environ.get("BRAIN_TIMEOUT_S", "8"))
        self.persona = persona if persona is not None else load_persona()
        self.model = os.environ.get("BRAIN_MODEL", "grok-4.7")
        self.max_output_tokens = int(os.environ.get("BRAIN_MAX_OUTPUT_TOKENS", "200"))

    def _payload(self, items, text) -> dict:
        return {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "store": False,
            "max_output_tokens": self.max_output_tokens,
            "stream": True,
            "instructions": self.persona,
            "input": items,
            "tools": responses_tools(text=text),
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

    async def _pulse(self, trace: TurnTrace):
        while True:
            trace.pulse()
            await asyncio.sleep(0.25)

    def _failure_line(self, exc) -> str:
        trace = _trace_var.get()
        if exc is None:
            why = "no-key"
        else:
            detail = str(exc).strip()
            why = type(exc).__name__ if not detail else f"{type(exc).__name__}: {detail}"
        if trace is None:
            return why
        snap = trace.snapshot()
        ttft = snap["loop_ttft_ms"] if snap["loop_ttft_ms"] is not None else snap["worker_ttft_ms"]
        return (
            f"{why} stage={snap['stage']} http={snap['http']} "
            f"ttft_ms={_fmt_ms(ttft)} worker_ttft_ms={_fmt_ms(snap['worker_ttft_ms'])} "
            f"dns={snap['dns']} proxy={snap['proxy']} key={snap['key']} "
            f"loop_pulses={snap['loop_pulses']} loop_gap_ms={snap['loop_gap_ms']}"
        )

    async def _fail(self, t0, ttft, exc, *, count_breaker=True) -> Reply:
        # A timeout speaks the offline line and stops there. Counting it would
        # open the circuit after a few slow turns and then fail the next ones
        # without calling xAI at all.
        opened = self.breaker.failure() if count_breaker else False
        log.warning("brain failed (%s)", self._failure_line(exc))
        if opened:
            log.warning("brain circuit open for %.0fs", self.breaker.cooldown)
        await self._emit_down()
        trace = _trace_var.get()
        if ttft is None and trace is not None:
            snap = trace.snapshot()
            ttft = snap["loop_ttft_ms"] if snap["loop_ttft_ms"] is not None else snap["worker_ttft_ms"]
        return self._down(t0, ttft)

    async def handle(self, text, source="box3-kitchen", session=None, on_sentence=None) -> Reply:
        """Answer one utterance. on_sentence(text) is sync and may be called mid-stream."""
        session = session or source
        t0 = time.perf_counter()
        if self.breaker.is_open():
            await self._emit_down()
            return self._down(t0, None)
        trace = TurnTrace()
        trace.key_source = "env" if os.environ.get("XAI_BRAIN_API_KEY", "").strip() else "keychain"
        _announce(trace)
        token = _trace_var.set(trace)
        pulse = asyncio.create_task(self._pulse(trace))
        try:
            try:
                key = await asyncio.to_thread(self.key_getter)
            except Exception as e:
                return await self._fail(t0, None, e)
            if not key:
                return await self._fail(t0, None, None)
            try:
                reply = await asyncio.wait_for(
                    self._handle(text, source, session, key, t0, on_sentence, trace),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError as e:
                return await self._fail(t0, None, e, count_breaker=False)
            except Exception as e:
                return await self._fail(t0, None, e, count_breaker=not _timeout_exc(e))
            return reply
        finally:
            pulse.cancel()
            try:
                await pulse
            except asyncio.CancelledError:
                pass
            _trace_var.reset(token)

    async def _handle(self, text, source, session, key, t0, on_sentence, trace) -> Reply:
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
                    trace.note_loop_ttft(ttft)
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
            response, seen_calls, streamed = await self._round(
                items, key, min(self.timeout, remaining), push, text)
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

    async def _round(self, items, key, timeout, push, text):
        response = None
        seen = []
        streamed = []
        async for ev in self.transport(self._payload(items, text), key, timeout):
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
