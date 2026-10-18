"""Womble #1 — Alexa jobs from the Cloudflare Queue (see ../../ALEXA.md §8).

Outbound only: short-polls the HTTP pull consumer of queue `womble-alexa`, runs each job through the hub's
route(), shows it on the frame with a room chip, and acks. Does nothing if ALEXA_PULL=0. A missing or
unreadable Keychain item (service=womble-cf-queue account=decoy) is re-read every 5 minutes.

hub.py wiring:   AlexaPuller(disp, route, push_card) supervised from main(), before model warm-up.
                 start() is the same loop without the supervisor (ALEXA_PULL=0 returns None).
Standalone check:  .venv/bin/python alexa_pull.py --status | --once
"""
import asyncio, base64, collections, inspect, json, logging, os, random, subprocess, time
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger("alexa")
# Cloudflare account + queue ids come from the environment (local.env, untracked); no built-in default.
ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID") or os.environ.get("ALEXA_CF_ACCOUNT", "")
QUEUE_ID = os.environ.get("CF_QUEUE_ID") or os.environ.get("ALEXA_QUEUE_ID", "")   # queue womble-alexa
KC_SERVICE, KC_ACCOUNT = "womble-cf-queue", os.environ.get("ALEXA_KC_ACCOUNT", "decoy")
API = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/queues/{QUEUE_ID}/messages"
DATA = Path(__file__).resolve().parent / "data"
DEVICES = DATA / "alexa_devices.json"      # deviceId -> {"room": "Kitchen", "name": "Kitchen Echo"}  (rename here)
XDEV_DEDUPE_S = 10                          # same text from any Echo within this window = one request
SEEN = DATA / "alexa_seen.json"            # last 500 requestIds (at-least-once dedupe, survives restarts)
STALE_S = 10 * 60                          # older jobs are listed as "missed", not executed
MAX_ATTEMPTS = 3
ACTIVE_S, IDLE_S = float(os.environ.get("ALEXA_POLL_S", "2")), float(os.environ.get("ALEXA_IDLE_POLL_S", "5"))
NIGHT_S = float(os.environ.get("ALEXA_NIGHT_POLL_S", "15"))
IDLE_AFTER_S = 10 * 60
TOKEN_RETRY_S = 5 * 60

def keychain_item_exists() -> bool:
    """True if the Keychain item exists. Does NOT read the secret (no -w)."""
    r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT],
                       capture_output=True)
    return r.returncode == 0

def _read_token() -> str | None:
    """Queue token, or None if the item is missing or unreadable. The value is never logged."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None

def decode_body(body) -> dict:
    """json content type: docs say base64, the API currently returns a JSON string; accept both."""
    if isinstance(body, dict): return body
    try: return json.loads(body)
    except (ValueError, TypeError): return json.loads(base64.b64decode(body))

def _call_route(route, text, **kw):
    """Call route with the kwargs it accepts. A coroutine result is awaited by the caller."""
    try:
        params = inspect.signature(route).parameters
    except (TypeError, ValueError):
        params = {}
    accepted = {k: v for k, v in kw.items() if k in params}
    return route(text, **accepted)

class Devices:
    def __init__(self, path=DEVICES): self.path = path; self.map = self._load()
    def _load(self):
        try: return json.loads(self.path.read_text())
        except Exception: return {}
    def lookup(self, device_id: str | None) -> tuple[dict, bool]:
        """Returns (entry, is_new). Unknown Echos are recorded as 'Echo N' so Andrew can rename the room."""
        self.map = self._load()                       # pick up hand edits without a restart
        if not device_id: return {"room": "Echo", "name": "Echo"}, False
        if device_id in self.map: return self.map[device_id], False
        n = len(self.map) + 1
        entry = {"room": f"Echo {n}", "name": f"Echo {n}", "first_seen": time.strftime("%Y-%m-%d %H:%M")}
        self.map[device_id] = entry
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.map, indent=2))
        return entry, True

class Seen:
    def __init__(self, path=SEEN, cap=500):
        self.path, self.cap = path, cap
        try: ids = json.loads(path.read_text())
        except Exception: ids = []
        self.d = collections.OrderedDict((i, 1) for i in ids[-cap:])
    def __contains__(self, k): return k in self.d
    def add(self, k):
        self.d[k] = 1
        while len(self.d) > self.cap: self.d.popitem(last=False)
        try: self.path.write_text(json.dumps(list(self.d)))
        except Exception as e: log.warning("seen save failed: %s", e)

    def discard(self, k):
        if k not in self.d:
            return
        self.d.pop(k, None)
        try: self.path.write_text(json.dumps(list(self.d)))
        except Exception as e: log.warning("seen save failed: %s", e)

def session_key(session_id, room) -> str:
    """Brain memory key. The live session id wins; otherwise the Echo's room."""
    if session_id:
        return f"alexa:{session_id}"
    return f"alexa:{room}"


def claim_recent(recent: dict, text: str, now: float | None = None) -> bool:
    """True if this wording should be handled. Same words inside the window are one request."""
    key = " ".join((text or "").lower().strip(" .!?").split())
    if not key:
        return False
    now = now or time.time()
    for k, t in list(recent.items()):
        if now - t >= XDEV_DEDUPE_S:
            recent.pop(k, None)
    if key in recent:
        log.info("alexa job skipped: same words within %ss (likely two Echos heard it)", XDEV_DEDUPE_S)
        return False
    recent[key] = now
    return True


async def show_alexa_result(disp, push_card, job, res, dev, new, room):
    """Frame card with the room chip, the same events AlexaPuller already emits."""
    text = (job.get("text") or "").strip()
    chip = {"source": "alexa", "room": room, "device": dev.get("name", room)}
    rid = job.get("requestId") or job.get("id")
    if new:
        await disp.emit("toast", {"text": f"New Echo ({room}): rename it in alexa_devices.json", "kind": "info"})
    await disp.emit("voice.heard", {"text": text, **chip}, corr=rid)
    card = dict(res["card"])
    if card.get("type") == "text":   # reply cards show the source label; list cards toast as usual
        await disp.emit("reply", {"title": card.get("title", ""), "body": card.get("body", ""),
                                   "source": dev.get("name", room), **{k: v for k, v in chip.items() if k != "source"}},
                        corr=rid)
    else:
        await push_card(card)
    log.info("alexa job done: room=%s category=%s", room, res.get("category"))
    return room


def poll_interval(last_msg_ts: float, now: float | None = None) -> float:
    now = now or time.time()
    hour = time.localtime(now).tm_hour
    if hour >= 23 or hour < 7: return NIGHT_S          # overnight (Madrid time on petit)
    return ACTIVE_S if now - last_msg_ts < IDLE_AFTER_S else IDLE_S

class AlexaPuller:
    def __init__(self, disp, route, push_card, devices: Devices | None = None, seen: Seen | None = None,
                 recent: dict | None = None):
        self.disp, self.route, self.push_card = disp, route, push_card
        self.devices, self.seen = devices or Devices(), seen or Seen()
        self._recent = recent if recent is not None else {}
        self.task = None; self.state = "disabled"; self.last_msg = 0.0; self.missed: list[str] = []

    def enabled(self) -> bool:
        return os.environ.get("ALEXA_PULL", "1") != "0" and keychain_item_exists()

    def start(self):
        if os.environ.get("ALEXA_PULL", "1") == "0":
            log.info("alexa pull disabled (ALEXA_PULL=0)"); return None
        self.task = asyncio.create_task(self.run()); return self.task

    async def wait_for_token(self) -> str:
        """Block until the Keychain token can be read. Recheck every TOKEN_RETRY_S while it is missing."""
        while True:
            token = await asyncio.to_thread(_read_token)
            if token:
                return token
            self.state = "disabled"
            log.info("alexa pull disabled (no Keychain item %s); rechecking in 5 min", KC_SERVICE)
            await asyncio.sleep(TOKEN_RETRY_S)

    async def _post(self, s, token, path, payload):
        async with s.post(f"{API}/{path}", json=payload, headers={"Authorization": f"Bearer {token}"}) as r:
            body = await r.json(content_type=None)
            return r.status, body

    async def run(self):
        if not (ACCOUNT_ID and QUEUE_ID):   # clean return: supervise() leaves it stopped
            self.state = "disabled"
            log.error("alexa pull disabled: CF_ACCOUNT_ID / CF_QUEUE_ID not set (see local.env.example)"); return
        token = await self.wait_for_token()
        self.state = "running"; backoff = 0.0
        log.info("alexa pull running (queue womble-alexa)")
        async with ClientSession(timeout=ClientTimeout(total=15)) as s:
            while True:
                try:
                    st, body = await self._post(s, token, "pull", {"batch_size": 10, "visibility_timeout_ms": 60000})
                    if st in (401, 403):
                        self.state = "auth_error"
                        log.error("alexa pull: token rejected (%s); stopping until restart", st)
                        await self.disp.emit("toast", {"text": "Alexa link: token problem", "kind": "error"})
                        return
                    if st == 429 or st >= 500: raise RuntimeError(f"HTTP {st}")
                    msgs = (body.get("result") or {}).get("messages") or []
                    backoff = 0.0
                    if msgs:
                        self.last_msg = time.time()
                        acks, retries = await self.handle_batch(msgs)
                        if acks or retries:
                            await self._post(s, token, "ack", {"acks": acks, "retries": retries})
                    if self.missed: await self._flush_missed()
                    await asyncio.sleep(poll_interval(self.last_msg))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    backoff = min(60.0, max(2.0, backoff * 2))
                    self.state = f"backoff {backoff:.0f}s"
                    log.warning("alexa pull error (%s: %s); retry in %.0f s", type(e).__name__, e, backoff)
                    await asyncio.sleep(backoff + random.random())
                    self.state = "running"

    async def handle_batch(self, msgs):
        acks, retries = [], []
        for m in msgs:
            lease = {"lease_id": m["lease_id"]}
            try:
                job = decode_body(m["body"])
                rid = job.get("requestId") or m.get("id")
                if rid in self.seen: acks.append(lease); continue                  # duplicate delivery
                age = time.time() - m.get("timestamp_ms", time.time() * 1000) / 1000
                if age > STALE_S:
                    self.missed.append(job.get("text", "")); self.seen.add(rid); acks.append(lease); continue
                await self.handle_job(job)
                self.seen.add(rid); acks.append(lease)
            except Exception as e:
                log.warning("alexa job failed (attempt %s): %s", m.get("attempts"), e)
                if (m.get("attempts") or 1) >= MAX_ATTEMPTS:
                    await self.disp.emit("error", {"message": "Couldn't do an Alexa request", "code": "alexa"})
                    acks.append(lease)
                else:
                    retries.append({**lease, "delay_seconds": 5})
        return acks, retries

    async def handle_job(self, job: dict):
        text = (job.get("text") or "").strip()
        if not text: return
        if not claim_recent(self._recent, text):
            return
        dev, new = self.devices.lookup(job.get("deviceId"))
        room = dev.get("room", "Echo")
        res = _call_route(self.route, text, source=f"alexa:{room}",
                          session=session_key(job.get("sessionId"), room))
        if asyncio.iscoroutine(res):
            res = await res
        await show_alexa_result(self.disp, self.push_card, job, res, dev, new, room)

    async def _flush_missed(self):
        n = len(self.missed); body = "\n".join(f"• {t}" for t in self.missed[:10])
        self.missed = []
        await self.disp.emit("reply", {"title": f"{n} missed Alexa request{'s' if n != 1 else ''}", "body": body,
                                       "source": "Alexa"})

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--status", action="store_true"); a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(json.dumps({"keychain_item": keychain_item_exists(), "ALEXA_PULL": os.environ.get("ALEXA_PULL", "1"),
                      "queue_id": QUEUE_ID, "devices": str(DEVICES)}))
