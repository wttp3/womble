"""Media routing on the hub (media plan §5, M3/M4).

The hub decides HOW an entry plays; the worker still builds the AudioPlayer
directive (it holds the resume offsets). Routes:
  frida     - play on the Echo that was spoken to (worker plays it locally)
  native    - Voice Monkey routine on the kitchen pair (radio / music presets)
  dispatch  - another Echo opens Frida through its Voice Monkey trigger and
              plays a pending command (M4, S0a passed)
  none      - honest "can't do that yet" line

Per-house values (Voice Monkey ids, which Echos form the kitchen pair, the
dispatcher Echo) come from data/media_local.json. Echo names are the room
names in data/alexa_devices.json; device ids never leave that file, the
worker only ever sees a 16-hex hash.
"""
import asyncio, hashlib, logging, re, time

log = logging.getLogger("hub.media")
KITCHEN = "kitchen"
PENDING_TTL_S = 60
ADD_TTL_S = 300       # one pending add per room, 5 minutes
SEEK_STEP_MS = 30000
DEBOUNCE_S = 1.5      # rapid +30/-30 taps collapse into one seek
ORDINALS = {"first": 1, "1st": 1, "one": 1, "1": 1, "second": 2, "2nd": 2, "two": 2, "2": 2,
            "third": 3, "3rd": 3, "three": 3, "3": 3}


def device_hash(device_id: str) -> str:
    return hashlib.sha256((device_id or "").encode()).hexdigest()[:16]


def norm_room(room) -> str:
    r = re.sub(r"\s+", " ", str(room or "").strip().lower())
    r = re.sub(r"^(?:the|my)\s+", "", r)
    return r


class MediaService:
    def __init__(self, lib, cfg, disp=None, native=None, devices=None, link=None, resolver=None, clock=time.time):
        self.lib, self.cfg, self.disp, self.native = lib, cfg or {}, disp, native
        self.devices, self.link, self.resolver, self.clock = devices, link, resolver, clock
        self.now = {}          # room -> media.state dict shown on the frame
        self.directory = None  # media.directory.Directory (M5)
        self.adds = {}         # room -> {"query", "cands", "at"}
        self._seek = {"delta": 0, "task": None}

    # ---------- rooms and Echos ----------
    def _devmap(self) -> dict:
        if self.devices is None:
            return {}
        try:
            self.devices.map = self.devices._load()
        except Exception:
            pass
        return getattr(self.devices, "map", {}) or {}

    def echo_for_hash(self, h):
        for did, e in self._devmap().items():
            if device_hash(did) == h:
                return e.get("room") or e.get("name")
        return None

    def hash_for_echo(self, echo):
        want = norm_room(echo)
        for did, e in self._devmap().items():
            if norm_room(e.get("room")) == want or norm_room(e.get("name")) == want:
                return device_hash(did)
        return None

    def kitchen_pair(self):
        return [norm_room(x) for x in self.cfg.get("kitchen_pair") or []]

    def room_of_echo(self, echo) -> str:
        e = norm_room(echo)
        return KITCHEN if e in self.kitchen_pair() else e

    def box3_room(self) -> str:
        return norm_room(self.cfg.get("box3_room") or KITCHEN)

    def dispatcher(self, room):
        d = (self.cfg.get("dispatch") or {}).get(norm_room(room))
        if not d or not d.get("echo") or not (self.native and self.native.configured(d.get("trigger"))):
            return None
        return d

    # ---------- frame ----------
    async def emit_state(self, room, state):
        self.now[room] = state
        if self.disp is not None:
            await self.disp.emit("media.state", state)

    # ---------- play ----------
    def resolve(self, name=None, entry_id=None):
        if entry_id:
            e = self.lib.get(entry_id)
            if e and e["enabled"]:
                return e
        if name:
            m = self.lib.match(name)
            if m:
                return m[0]
        return None

    def _native_method(self, entry):
        for m in entry.get("methods", []):
            if m.get("kind") == "native_routine" and self.native and self.native.configured(m.get("trigger_ref")):
                return m
        return None

    async def play(self, name=None, entry_id=None, room=None, source="box3", echo=None, mode="auto") -> dict:
        entry = self.resolve(name, entry_id)
        if not entry:
            return {"route": "none", "ok": False,
                    "speech": f"{name} isn't in the library yet." if name else "What should I play?"}
        src_room = self.room_of_echo(echo) if source == "alexa" else self.box3_room()
        target = norm_room(room) or src_room
        here = source == "alexa" and target == src_room
        nm = self._native_method(entry) if target == KITCHEN else None

        if entry["type"] == "music_preset":
            if nm:
                return await self._native(entry, nm, target)
            return {"route": "none", "ok": False, "speech": f"{entry['name']} isn't set up on the kitchen speakers yet."}
        if entry["type"] == "radio" and nm:
            return await self._native(entry, nm, target)
        if here:
            self.lib.log_play(entry["id"], target, echo, "frida", "requested", mode)
            return {"route": "frida", "ok": True, "entry_id": entry["id"], "mode": mode, "speech": ""}
        d = self.dispatcher(target)
        if d:
            return await self.dispatch(target, {"op": "play", "entry_id": entry["id"], "mode": mode},
                                       label=entry["name"])
        if source == "alexa":
            return {"route": "none", "ok": False, "speech": f"I can't start that on the {target} Echo yet."}
        return {"route": "none", "ok": False,
                "speech": f"I can only start {entry['name']} from an Echo for now. "
                          f"Say, Alexa, ask Frida to play {entry['name']}."}

    async def _native(self, entry, method, room):
        ok = await self.native.fire(method["trigger_ref"], entry["id"], room)
        if not ok:
            return {"route": "none", "ok": False, "speech": "I couldn't start the kitchen speakers."}
        await self.emit_state(room, {"title": entry["name"], "artist": "kitchen speakers", "player": "via Alexa routine",
                                     "state": "playing", "position_s": 0, "duration_s": 0, "focus": True,
                                     "entry_id": entry["id"], "room": room, "route": "native"})
        await self._quota_toast()
        return {"route": "native", "ok": True, "entry_id": entry["id"],
                "speech": f"Starting {entry['name']} on the kitchen speakers."}

    # ---------- dispatch (M4) ----------
    async def dispatch(self, room, cmd, label=None) -> dict:
        d = self.dispatcher(room)
        h = self.hash_for_echo(d["echo"]) if d else None
        if not d or not h:
            return {"route": "none", "ok": False, "speech": f"I can't reach the {room} Echo yet."}
        if self.link is None or not await self.link.send_typed({"type": "media.pending", "device": h, "cmd": cmd,
                                                                "ttl_s": PENDING_TTL_S}):
            return {"route": "none", "ok": False, "speech": "The Alexa link is down, so I can't start that."}
        ok = await self.native.fire(d["trigger"], cmd.get("entry_id"), room, detail=cmd.get("op"))
        if not ok:
            return {"route": "none", "ok": False, "speech": f"I couldn't wake the {room} Echo."}
        await self._quota_toast()
        if cmd.get("op") == "play":
            self.lib.log_play(cmd.get("entry_id"), room, d["echo"], "dispatch", "requested", cmd.get("mode"))
        what = label or "it"
        return {"route": "dispatch", "ok": True, "entry_id": cmd.get("entry_id"),
                "speech": f"Starting {what} on the {room} Echo." if cmd.get("op") == "play" else "OK."}

    # ---------- stop ----------
    async def stop(self, room=None, source="box3", echo=None) -> dict:
        target = norm_room(room) or (self.room_of_echo(echo) if source == "alexa" else self.box3_room())
        st = self.now.get(target)
        if st and st.get("route") in ("frida", "dispatch") and self.dispatcher(target):
            res = await self.dispatch(target, {"op": "pause"})
            if res["ok"]:
                await self.emit_state(target, {**st, "state": "paused", "focus": False})
                return {"ok": True, "speech": "Pausing."}
        if st:
            await self.emit_state(target, {**st, "state": "idle", "focus": False})
        where = "the kitchen Echo" if target == KITCHEN else f"the {target} Echo"
        return {"ok": False, "speech": f"Say, Alexa, stop, to {where}."}

    # ---------- events from the worker ----------
    async def on_event(self, msg: dict):
        ev = msg.get("event")
        echo = self.echo_for_hash(msg.get("device")) or "Echo"
        room = self.room_of_echo(echo)
        entry = self.lib.get(msg.get("entry_id")) if msg.get("entry_id") else None
        name = entry["name"] if entry else (msg.get("entry_id") or "")
        off = int(msg.get("offset_ms") or 0)
        if msg.get("ep_key") and ev in ("stopped", "finished"):
            self.lib.mirror_resume(msg["ep_key"], msg.get("entry_id"), off if ev == "stopped" else 0)
        log.info("media event=%s entry=%s room=%s offset_ms=%d", ev, msg.get("entry_id"), room, off)
        prev = self.now.get(room) or {}
        if ev == "play":
            await self.emit_state(room, {"title": msg.get("title") or name, "artist": name,
                                         "player": f"Frida on {echo}", "state": "playing",
                                         "position_s": off / 1000, "duration_s": msg.get("duration_s") or 0,
                                         "focus": True, "entry_id": msg.get("entry_id"), "ep_key": msg.get("ep_key"),
                                         "room": room, "echo": echo, "route": "frida", "started": self.clock()})
        elif ev == "stopped":
            await self.emit_state(room, {**prev, "state": "paused", "position_s": off / 1000, "focus": False,
                                         "entry_id": msg.get("entry_id") or prev.get("entry_id")})
        elif ev in ("finished", "failed"):
            await self.emit_state(room, {**prev, "state": "idle", "focus": False})
            if ev == "failed" and self.disp is not None:
                await self.disp.emit("toast", {"text": f"{name or 'Audio'} couldn't play", "kind": "info"})

    # ---------- snapshot to the worker ----------
    async def push_snapshot(self, latest=None):
        if self.link is None:
            return False
        msg = {"type": "media.snapshot", "entries": self.lib.snapshot()}
        if latest is not None:
            msg["latest"] = latest
        return await self.link.send_typed(msg)

    async def refresh_loop(self, every_s=900):
        """15-minute latest-episode precompute, pushed with the library snapshot."""
        while True:
            latest = None
            if self.resolver is not None:
                try:
                    latest = await self.resolver.refresh_all_async()
                except Exception as e:
                    log.warning("media refresh failed (%s)", type(e).__name__)
            await self.push_snapshot(latest)
            await asyncio.sleep(every_s)


# ---------- fast path ----------
PLAY_RE = re.compile(r"^(?:please\s+)?(?:play|put on)\s+(?:the\s+)?(?:(latest|newest)\s+)?(?P<name>.+?)"
                     r"(?:\s+(?:in|on)\s+the\s+(?P<room>[\w ]+?))?[.!?]?$", re.I)
STOP_RE = re.compile(r"^(?:please\s+)?(?:stop|pause)(?:\s+the)?(?:\s+(?:music|radio|podcast|playing))?[.!?]?$", re.I)


def fast_path(text, lib):
    """('play', entry_id, room, mode) for an exact library name, ('stop',) for stop, else None."""
    t = (text or "").strip()
    if STOP_RE.match(t) and re.search(r"music|radio|podcast|playing", t, re.I):
        return ("stop",)
    m = PLAY_RE.match(t)
    if m:
        e = lib.exact(m["name"])
        if e:
            return ("play", e["id"], m["room"], "latest" if m.group(1) else "auto")
    return None


# ---------- M5: library card, add_to_library, frame transport ----------
async def _toast(self, text):
    if self.disp is not None and text:
        await self.disp.emit("toast", {"text": text, "kind": "info"})


async def _quota_toast(self):
    full, n = self.native.quota_warning() if self.native else (False, 0)
    if full:
        await self._toast(f"Voice Monkey: {n} of 200 requests used this month")


async def show_library(self):
    if self.disp is not None:
        await self.disp.emit("media.library", {"grid": self.lib.grid(), "ts": self.clock()})
    return {"ok": True, "speech": self.lib.summary()}


async def add_search(self, query, type=None, room=None):
    room = norm_room(room) or self.box3_room()
    query = (query or "").strip(" .!?")
    if not query:
        return {"ok": False, "speech": "What should I add?"}
    if self.directory is None:
        return {"ok": False, "speech": "I can't search the directories right now."}
    try:
        cands = (await self.directory.find_async(query, type))[:3]
    except Exception as e:
        log.warning("media add search failed (%s)", e.__class__.__name__)
        return {"ok": False, "speech": f"I couldn't search for {query} right now."}
    if not cands:
        self.adds.pop(room, None)
        return {"ok": False, "speech": f"I couldn't find {query}."}
    self.adds[room] = {"query": query, "cands": cands, "at": self.clock()}
    if self.disp is not None:
        await self.disp.emit("media.candidates", {"query": query, "room": room, "candidates": [
            {"n": i + 1, "name": c["name"], "type": c["type"], "source": c.get("source"),
             "detail": c.get("author") or " ".join(x for x in [c.get("codec", "").upper(), f"{c['bitrate']} kbps" if c.get("bitrate") else ""] if x),
             "latest": (c.get("latest") or {}).get("title"), "ok": bool(c.get("ok")), "why": c.get("why") or ""}
            for i, c in enumerate(cands)]})
    words = ["first", "second", "third"]
    listed = "; ".join(f"{words[i]}, {c['name']}" + ("" if c.get("ok") else " (won't play)") for i, c in enumerate(cands))
    return {"ok": True, "speech": f"I found {listed}. Say yes, the first one, or tap Add."}


def pending_add(self, room=None):
    room = norm_room(room) or self.box3_room()
    p = self.adds.get(room)
    if p and self.clock() - p["at"] > ADD_TTL_S:
        self.adds.pop(room, None); p = None
    return p


async def add_confirm(self, n, room=None):
    room = norm_room(room) or self.box3_room()
    p = self.pending_add(room)
    if not p:
        return {"ok": False, "speech": "There's nothing waiting to be added."}
    if not 1 <= int(n) <= len(p["cands"]):
        return {"ok": False, "speech": f"There are only {len(p['cands'])} to choose from."}
    c = p["cands"][int(n) - 1]
    if not c.get("ok"):
        return {"ok": False, "speech": f"{c['name']} didn't play in my test, so I didn't add it."}
    if self.lib.exact(c["name"]):
        self.adds.pop(room, None)
        return {"ok": False, "speech": f"{c['name']} is already in the library."}
    from .directory import Directory
    e = Directory.to_entry(c, self.lib.ids())
    self.lib.add_entry(e)
    self.adds.pop(room, None)
    log.info("media added entry=%s type=%s", e["id"], e["type"])
    await self.push_snapshot()
    await self.show_library()
    return {"ok": True, "entry_id": e["id"], "speech": f"Added {c['name']}. Say, play {c['name']}."}


async def remove_entry(self, entry_id):
    e = self.lib.get(entry_id)
    if not e or not self.lib.set_enabled(entry_id, False):
        return {"ok": False, "speech": "That isn't in the library."}
    log.info("media removed entry=%s", entry_id)
    await self.push_snapshot()
    await self.show_library()
    return {"ok": True, "speech": f"Removed {e['name']}."}


async def transport(self, op, room=None):
    """Frame transport for Frida audio on the dispatcher Echo (plan section 12)."""
    room = norm_room(room) or self.box3_room()
    st = self.now.get(room)
    if not st or not st.get("entry_id"):
        return {"ok": False, "speech": "Nothing is playing."}
    if st.get("route") == "native":
        return {"ok": False, "speech": "Say, Alexa, stop, to the kitchen Echo."}
    if not self.dispatcher(room):
        return {"ok": False, "speech": f"I can't reach the {room} Echo yet."}
    if op in ("fwd", "back"):
        self._seek["delta"] += SEEK_STEP_MS if op == "fwd" else -SEEK_STEP_MS
        pos = max(0.0, (st.get("position_s") or 0) + (self.clock() - (st.get("started") or self.clock()) if st.get("state") == "playing" else 0))
        await self.emit_state(room, {**st, "position_s": max(0.0, pos + (SEEK_STEP_MS / 1000 if op == "fwd" else -SEEK_STEP_MS / 1000)),
                                     "started": self.clock(), "focus": False})
        if self._seek["task"] is None:
            self._seek["task"] = asyncio.create_task(self._flush_seek(room))
        return {"ok": True, "speech": ""}
    if op == "toggle":
        op = "pause" if st.get("state") == "playing" else "resume"
    if op == "pause":
        res = await self.dispatch(room, {"op": "pause"})
        if res["ok"]:
            await self.emit_state(room, {**st, "state": "paused", "focus": False})
        return res
    if op in ("resume", "next", "prev"):
        return await self.dispatch(room, {"op": op, "entry_id": st.get("entry_id")})
    return {"ok": False, "speech": "I can't do that."}


async def _flush_seek(self, room):
    try:
        await asyncio.sleep(DEBOUNCE_S)
        delta, self._seek["delta"] = self._seek["delta"], 0
        if delta:
            res = await self.dispatch(room, {"op": "seek", "delta_ms": delta})
            if not res["ok"]:
                await self._toast(res["speech"])
    finally:
        self._seek["task"] = None


async def on_tap(self, action):
    """Frame taps (display.media_handler). Returns a toast line or ''."""
    a = str(action or "")
    if a == "media.library":
        res = await self.show_library(); return ""
    if a.startswith("media.play:"):
        res = await self.play(entry_id=a.split(":", 1)[1][:64], source="frame")
    elif a.startswith("media.add:"):
        n = a.split(":", 1)[1]
        res = await self.add_confirm(int(n)) if n.isdigit() else {"speech": ""}
    elif a.startswith("media.remove:"):
        res = await self.remove_entry(a.split(":", 1)[1][:64])
    elif a in ("media.fwd", "media.back", "media.toggle", "media.pause", "media.resume", "media.next", "media.prev"):
        res = await self.transport(a.split(".", 1)[1])
    else:
        return None
    return res.get("speech") or ""


for _f in (_toast, _quota_toast, show_library, add_search, pending_add, add_confirm, remove_entry,
           transport, _flush_seek, on_tap):
    setattr(MediaService, _f.__name__, _f)


# ---------- M5 fast paths ----------
ADD_RE = re.compile(r"^(?:please\s+)?add\s+(?:the\s+|a\s+)?(?P<kind>podcast|radio station|station|radio)\s+(?:called\s+)?(?P<q>.+?)"
                    r"(?:\s+to\s+(?:my|the)\s+(?:media\s+)?library)?[.!?]?$", re.I)
ADD_LIB_RE = re.compile(r"^(?:please\s+)?add\s+(?P<q>.+?)\s+to\s+(?:my|the)\s+(?:media\s+)?library[.!?]?$", re.I)
YES_RE = re.compile(r"^(?:(?:yes|yeah|yep|ok|okay)[,.]?\s*)?(?:add\s+)?(?:the\s+)?(?:number\s+)?"
                    r"(?P<n>first|second|third|1st|2nd|3rd|one|two|three|[123])(?:\s+one)?[.!?]?$", re.I)
SHOW_RE = re.compile(r"^(?:show|open)(?:\s+me)?\s+(?:my\s+|the\s+)?(?:media\s+)?library[.!?]?$", re.I)
REMOVE_RE = re.compile(r"^(?:please\s+)?(?:remove|delete)\s+(?:the\s+)?(?P<q>.+?)\s+from\s+(?:my|the)\s+(?:media\s+)?library[.!?]?$", re.I)


def fast_path_m5(text, svc):
    """('add', query, type) | ('confirm', n) | ('show',) | ('remove', entry_id|None, name) | None."""
    t = (text or "").strip()
    m = ADD_RE.match(t)
    if m:
        return ("add", m["q"], "podcast" if m["kind"].lower() == "podcast" else "radio")
    m = ADD_LIB_RE.match(t)
    if m:
        return ("add", m["q"], None)
    if svc.pending_add() is not None:
        m = YES_RE.match(t)
        if m:
            return ("confirm", ORDINALS[m["n"].lower()])
    if SHOW_RE.match(t):
        return ("show",)
    m = REMOVE_RE.match(t)
    if m:
        hit = svc.lib.match(m["q"])
        return ("remove", hit[0]["id"] if hit else None, m["q"])
    return None
