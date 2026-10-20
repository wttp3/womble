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
        full, n = self.native.quota_warning()
        if full and self.disp is not None:
            await self.disp.emit("toast", {"text": f"Voice Monkey: {n} requests this month", "kind": "info"})
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
