"""Outbound WebSocket from the hub to the womble-alexa Durable Object.

The worker pushes a job; this module answers on the same socket. No inbound port.
Enabled only when ALEXA_LINK=1. The worker URL is ALEXA_WORKER_URL in local.env.
The bearer token is HUB_LINK_TOKEN in the environment, or Keychain item
womble-hub-link (account $USER), read the same way as the brain key.

ALEXA_LINK_TEST_DELAY_S delays each answer so the worker's progressive response
and 6.5 s cutoff can be exercised. It is off unless set.
"""
import asyncio, contextlib, inspect, json, logging, os, random, re, subprocess, time
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType

from alexa_pull import (
    Devices, Seen, claim_recent, session_key, show_alexa_result,
)

log = logging.getLogger("alexa.link")
KC_SERVICE = "womble-hub-link"
TOKEN_RETRY_S = 5 * 60
HEARTBEAT_S = 15
MAX_SPEECH = 6000
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def account() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def read_token() -> str | None:
    """Hub-link bearer token. Env wins. The value is never logged."""
    env = os.environ.get("HUB_LINK_TOKEN", "").strip()
    if env:
        return env
    who = account()
    if not who:
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KC_SERVICE, "-a", who, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def worker_ws_url(raw: str | None) -> str:
    """wss://<worker>/hub from an https or wss URL. Rejects userinfo and other paths."""
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "wss://" + text
    parts = urlsplit(text)
    if parts.username or parts.password or parts.query or parts.fragment:
        return ""
    scheme = {"https": "wss", "wss": "wss", "http": "ws", "ws": "ws"}.get(parts.scheme)
    if not scheme or not parts.hostname:
        return ""
    path = (parts.path or "").rstrip("/")
    if path not in ("", "/hub"):
        return ""
    return urlunsplit((scheme, parts.netloc, "/hub", "", ""))


def backoff_s(attempt: int) -> float:
    return min(30.0, 0.5 * (2 ** max(0, attempt)))


def test_delay_s() -> float:
    raw = os.environ.get("ALEXA_LINK_TEST_DELAY_S", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def plain_speech(text) -> str:
    s = _CTRL.sub(" ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > MAX_SPEECH:
        s = s[: MAX_SPEECH - 1].rstrip() + "…"
    return s


# Whole utterance only. "is there no buttermilk" is still a question.
_DISMISS_RE = re.compile(
    r"^(?:no|nope|no,?\s+thanks|that(?:'|’)s all|thats all)[.!?]?$",
    re.IGNORECASE,
)
_END_INTENTS = frozenset({"AMAZON.NoIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"})


def is_dismissal(text: str) -> bool:
    return bool(_DISMISS_RE.match(re.sub(r"\s+", " ", str(text or "")).strip()))


def session_goodbye(text: str, intent) -> bool:
    return (intent or "") in _END_INTENTS or is_dismissal(text)


def build_reply(job: dict, res: dict) -> dict:
    speech = plain_speech(res.get("speech") or "") or "OK."
    end = bool(res.get("end_session", True))
    out = {"id": job.get("id") or job.get("requestId"), "speech": speech, "end_session": end}
    if not end:
        out["reprompt"] = plain_speech(res.get("reprompt") or "") or "Anything else?"
    return out


class AlexaLink:
    def __init__(self, disp, route, brain, push_card, devices: Devices | None = None,
                 seen: Seen | None = None, recent: dict | None = None):
        self.disp, self.route, self.brain, self.push_card = disp, route, brain, push_card
        self.devices = devices or Devices()
        self.seen = seen or Seen()
        self.recent = recent if recent is not None else {}
        self.task = None
        self.state = "disabled"
        self.heartbeat_s = HEARTBEAT_S
        self.token_retry = TOKEN_RETRY_S
        self.jitter = True
        self._answers = {}
        self.ws = None
        self.media = None          # media.service.MediaService, set by the hub (M3)

    def enabled(self) -> bool:
        return os.environ.get("ALEXA_LINK", "0") == "1"

    def start(self):
        if not self.enabled():
            log.info("alexa link disabled (ALEXA_LINK!=1)")
            return None
        self.task = asyncio.create_task(self.run())
        return self.task

    async def _answer(self, text, room, session):
        res = self.route(text)
        if inspect.iscoroutine(res):
            res = await res
        if not isinstance(res, dict):
            res = {}
        if res.get("category") != "fallback":
            out = dict(res)
            out.setdefault("end_session", True)
            return out
        reply = self.brain(text, source=f"alexa:{room}", session=session)
        if inspect.iscoroutine(reply):
            reply = await reply
        if hasattr(reply, "speech"):
            card = getattr(reply, "card", None) or {"type": "text", "title": "Womble", "body": reply.speech}
            # A finished brain answer used to close the mic, so "what about for scones"
            # had to invoke Frida again. Hold it open. A question keeps its own reprompt.
            # List hits return above and still end the session.
            end = bool(getattr(reply, "end_session", True))
            reprompt = getattr(reply, "reprompt", None)
            if end or not reprompt:
                reprompt = "Anything else?"
            return {
                "category": getattr(reply, "category", "brain"),
                "speech": reply.speech,
                "card": card,
                "end_session": False,
                "reprompt": reprompt,
            }
        return reply

    def _log_job(self, job: dict, text: str, session: str, reply: dict) -> None:
        speech = plain_speech(reply.get("speech"))[:80]
        # Question text is logged, same as a BOX-3 transcript. No tokens or URLs.
        log.info(
            "alexa job intent=%s text=%s session=%s speech=%s end_session=%s",
            job.get("intent") or "-",
            text,
            session,
            speech,
            bool(reply.get("end_session", True)),
        )

    async def send_typed(self, msg: dict) -> bool:
        """Hub -> DO message without a job id (media snapshot, pending dispatch)."""
        ws = self.ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_str(json.dumps(msg))
            return True
        except Exception as e:
            log.warning("alexa link send failed (%s)", type(e).__name__)
            return False

    async def _media_job(self, job: dict) -> dict:
        """Play request routed by the hub (M3). The worker builds the directive."""
        m = job.get("media") or {}
        dev, _ = self.devices.lookup(job.get("deviceId"))
        echo = dev.get("room", "Echo")
        if self.media is None:
            return {"id": job.get("id"), "speech": "", "end_session": True, "media": {"route": "frida",
                    "entry_id": m.get("entry_id"), "mode": m.get("mode") or "auto"}}
        op = m.get("op") or "play"
        if op == "stop":
            res = await self.media.stop(room=m.get("room"), source="alexa", echo=echo)
            res = {**res, "route": "none"}
        else:
            res = await self.media.play(name=m.get("name"), entry_id=m.get("entry_id"), room=m.get("room"),
                                        source="alexa", echo=echo, mode=m.get("mode") or "auto")
        log.info("alexa media job op=%s entry=%s room=%s echo=%s route=%s", op, res.get("entry_id") or "-",
                 m.get("room") or "-", echo, res.get("route"))
        return {"id": job.get("id"), "speech": plain_speech(res.get("speech") or ""), "end_session": True,
                "media": {k: res.get(k) for k in ("route", "entry_id", "mode", "ok") if res.get(k) is not None}}

    async def handle_job(self, job: dict) -> dict:
        rid = job.get("id") or job.get("requestId")
        if job.get("media"):
            return await self._media_job(job)
        text = (job.get("text") or job.get("question") or "").strip()
        dev, new = self.devices.lookup(job.get("deviceId"))
        room = dev.get("room", "Echo")
        session = session_key(job.get("sessionId"), room)
        if rid and rid in self._answers:
            cached = dict(self._answers[rid])
            cached["id"] = rid
            self._log_job(job, text, session, cached)
            return cached
        if rid:
            if rid in self.seen:
                reply = {"id": rid, "speech": "OK.", "end_session": True}
                self._log_job(job, text, session, reply)
                return reply
            self.seen.add(rid)
        duplicate = bool(text) and not claim_recent(self.recent, text)
        recv = time.perf_counter()
        delay = test_delay_s()
        try:
            if delay:
                await asyncio.sleep(delay)
            started = time.perf_counter()
            if not text:
                res = {"category": "empty", "speech": "Sorry, I didn't catch that.", "end_session": False,
                       "reprompt": "What do you need?", "card": {"type": "text", "title": "", "body": ""}}
            elif duplicate:
                res = {"category": "deduped", "speech": "OK.", "end_session": True,
                       "card": {"type": "text", "title": "", "body": ""}}
            elif session_goodbye(text, job.get("intent")):
                res = {"category": "dismiss", "speech": "Okay.", "end_session": True,
                       "card": {"type": "text", "title": "", "body": "Okay."}}
            else:
                res = await self._answer(text, room, session)
            model_ms = int((time.perf_counter() - started) * 1000)
            if not duplicate:
                await show_alexa_result(self.disp, self.push_card, job, res, dev, new, room)
        except Exception:
            if rid:
                self.seen.discard(rid)
            raise
        reply = build_reply(job, res)
        if rid:
            self._answers[rid] = reply
        hop_ms = max(0, int((time.perf_counter() - recv) * 1000 - model_ms - int(delay * 1000)))
        log.info("alexa link hop_ms=%d model_ms=%d", hop_ms, model_ms)
        self._log_job(job, text, session, reply)
        return reply

    async def _on_text(self, ws, data: str):
        if data in ("ping", "pong"):
            return
        try:
            job = json.loads(data)
        except (ValueError, TypeError):
            return
        if isinstance(job, dict) and job.get("type") and not job.get("id"):
            await self._on_typed(job)
            return
        if not isinstance(job, dict) or not (job.get("id") or job.get("requestId")):
            return
        try:
            reply = await self.handle_job(job)
        except Exception as e:
            log.warning("alexa link job failed (%s)", type(e).__name__)
            reply = {"id": job.get("id") or job.get("requestId"), "speech": "Connection unavailable.",
                     "end_session": True}
            self._log_job(job, (job.get("text") or job.get("question") or "").strip(),
                          session_key(job.get("sessionId"), "Echo"), reply)
        await ws.send_str(json.dumps(reply))

    async def _on_typed(self, msg: dict):
        if msg.get("type") == "media.event" and self.media is not None:
            try:
                await self.media.on_event(msg)
            except Exception as e:
                log.warning("media event failed (%s)", type(e).__name__)

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(self.heartbeat_s)
            if ws.closed:
                return
            await ws.send_str("ping")

    async def _session(self, url: str, token: str):
        timeout = ClientTimeout(total=None, sock_connect=20)
        ssl = url.startswith("wss://")
        async with ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                url,
                headers={"Authorization": f"Bearer {token}"},
                heartbeat=20,
                origin=None,
                ssl=ssl,
            ) as ws:
                self.state = "connected"
                self.ws = ws
                log.info("alexa link connected")
                pinger = asyncio.create_task(self._ping(ws))
                if self.media is not None:
                    asyncio.create_task(self.media.push_snapshot())
                try:
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            await self._on_text(ws, msg.data)
                        elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                            break
                finally:
                    self.ws = None
                    pinger.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pinger
        raise ConnectionError("closed")

    async def run(self):
        if not self.enabled():
            log.info("alexa link disabled (ALEXA_LINK!=1)")
            return
        attempt = 0
        while True:
            url = worker_ws_url(os.environ.get("ALEXA_WORKER_URL", ""))
            token = await asyncio.to_thread(read_token)
            if not url or not token:
                self.state = "disabled"
                log.info("alexa link waiting (worker URL or hub token missing)")
                await asyncio.sleep(self.token_retry)
                continue
            try:
                await self._session(url, token)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state = "reconnect"
                delay = backoff_s(attempt)
                if self.jitter:
                    delay += random.random() * 0.25
                attempt += 1
                log.warning("alexa link down (%s); reconnect in %.1f s", type(e).__name__, delay)
                await asyncio.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps({
        "ALEXA_LINK": os.environ.get("ALEXA_LINK", "0"),
        "worker_url_set": bool(worker_ws_url(os.environ.get("ALEXA_WORKER_URL", ""))),
        "token_set": bool(read_token()),
    }))
