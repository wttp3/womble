"""petit voice hub, slice 1.
BOX-3 (ESPHome voice_assistant over native API) -> mlx-whisper -> rule router
-> local JSON list -> TTS (Kokoro/mlx-audio, fallback macOS `say`) -> BOX-3 media player
+ WebSocket card to display page.  Secrets come from the environment only.

Env: BOX3_HOST (192.168.0.90), BOX3_NOISE_PSK (api encryption key), HUB_HOST_IP (petit LAN IP
the BOX-3 can reach), HUB_PORT (8080), WHISPER_MODEL, TTS_ENGINE (kokoro|say).
NOTE: the aioesphomeapi voice-assistant API names below match how Home Assistant's esphome
integration uses them; check them against the installed aioesphomeapi version (see README).
"""
import asyncio, json, logging, os, re, subprocess, tempfile, time, uuid, wave
from pathlib import Path

import numpy as np
from aiohttp import web
from aioesphomeapi import APIClient, VoiceAssistantEventType as EV
from recorder import Recorder, make_beep
from display import Display   # Womble #1 display: events, weather, taps (see DISPLAY.md)

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"; DATA.mkdir(exist_ok=True)
TTS_DIR = DATA / "tts"; TTS_DIR.mkdir(exist_ok=True)
LIST_FILE = DATA / "lists.json"
TIMING_LOG = DATA / "timing.jsonl"

BOX3_HOST = os.environ.get("BOX3_HOST", "192.168.0.90")
PSK = os.environ["BOX3_NOISE_PSK"]
HUB_IP = os.environ["HUB_HOST_IP"]
HUB_PORT = int(os.environ.get("HUB_PORT", "8080"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
TTS_ENGINE = os.environ.get("TTS_ENGINE", "kokoro")

log = logging.getLogger("hub")

# ---------- STT ----------
import mlx_whisper
def transcribe_whisper(pcm: bytes) -> str:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if 0 < peak < 0.5: audio = audio * min(0.5 / peak, 20.0)   # software gain for quiet mic
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER_MODEL, language=os.environ.get("WHISPER_LANG") or None,
                               condition_on_previous_text=False, temperature=0.0)
    return r.get("text", "").strip()

import stt_xai
def transcribe(pcm: bytes) -> str:
    """xAI STT first (unless STT_ENGINE=whisper or no key); whisper fallback per utterance."""
    t0 = time.perf_counter(); reason = None
    if os.environ.get("STT_ENGINE", "xai").lower() != "whisper":
        key = stt_xai.get_key()
        if key:
            try:
                text = stt_xai.transcribe_xai(pcm, key)
                log.info("stt engine=xai latency=%.0fms", (time.perf_counter() - t0) * 1000)
                return text
            except Exception as e:
                reason = type(e).__name__ if not isinstance(e, RuntimeError) else str(e)
                log.warning("xai stt failed (%s); falling back to whisper", reason)
    t1 = time.perf_counter(); text = transcribe_whisper(pcm)
    log.info("stt engine=whisper%s latency=%.0fms", " (fallback)" if reason else "", (time.perf_counter() - t1) * 1000)
    return text

def warmup():
    rng = np.random.default_rng(0)
    for secs in (2, 4):
        transcribe_whisper((rng.normal(0, 800, 16000 * secs)).astype(np.int16).tobytes())
    tts("Ready.")   # loads Kokoro + misaki/spaCy once (first call ~30 s cold)

# ---------- router ----------
ADD_RE = re.compile(r"^(?:please\s+)?(?:add|put)\s+(?P<item>.+?)(?:\s+(?:to|on)\s+(?:the\s+|my\s+)?(?P<list>[\w ]+?)(?:\s+list)?)?[.!]?$", re.I)
RECORD_RE = re.compile(r"\brecord\b.*?\bfor\s+(?P<who>andrew|vic\w*|victoria)\b", re.I)
SHOW_RE = re.compile(r"^(?:what'?s|show|read)\s+(?:on\s+)?(?:the\s+|my\s+)?(?P<list>[\w ]+?)\s+list", re.I)

def load_lists():
    if not LIST_FILE.exists(): return {}
    try:
        raw = LIST_FILE.read_text()
        if not raw.strip(): raise json.JSONDecodeError("empty file", raw, 0)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        bad = LIST_FILE.with_name(f"lists.corrupt-{int(time.time())}.json")
        try: os.replace(LIST_FILE, bad)
        except OSError: pass
        log.warning("lists.json unreadable (%s); moved to %s, starting empty", e.msg, bad.name)
        return {}

def save_lists(lists: dict):
    tmp = LIST_FILE.with_name(LIST_FILE.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(lists, indent=2)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, LIST_FILE)

def route(text: str) -> dict:
    t = text.strip()
    if m := RECORD_RE.search(t):
        who = "vic" if m["who"].lower().startswith("vic") else "andrew"
        if re.search(r"\b(other words|not womble|negatives?)\b", t, re.I):
            return {"category": "record.start", "speaker": who, "only": "neg", "speech": f"Recording other words for {who.title()}. Read each one from the screen after the beep.",
                    "card": {"type": "text", "title": f"Record other words · {who.title()}", "body": "Starting…"}}
        return {"category": "record.start", "speaker": who, "speech": f"Recording Womble for {who.title()}. Say it after each beep.",
                "card": {"type": "text", "title": f"Record Womble · {who.title()}", "body": "Starting…"}}
    if re.fullmatch(r"(?:please\s+)?stop(?: recording)?[.!]?", t, re.I) and REC.active:
        return {"category": "record.stop", "speech": "Stopped recording.", "card": {"type": "text", "title": "Recording stopped", "body": ""}}
    if m := ADD_RE.match(t):
        item, lst = m["item"].strip(), (m["list"] or "shopping").strip().lower()
        lists = load_lists(); lists.setdefault(lst, []).append({"item": item, "ts": time.time()})
        save_lists(lists)
        return {"category": "list.add", "speech": f"Added {item} to {lst}.",
                "card": {"type": "list", "title": lst.title(), "items": [i["item"] for i in lists[lst]], "highlight": item}}
    if m := SHOW_RE.match(t):
        lst = m["list"].strip().lower(); items = [i["item"] for i in load_lists().get(lst, [])]
        return {"category": "list.show", "speech": f"{len(items)} items on {lst}.",
                "card": {"type": "list", "title": lst.title(), "items": items}}
    # slice 1: everything else is just echoed; phase 2 dispatches to Grok bots
    return {"category": "fallback", "speech": "Sorry, I can only do lists for now.",
            "card": {"type": "text", "title": "Heard", "body": t}}

# ---------- TTS ----------
_kokoro = None
def tts(text: str) -> Path:
    out = TTS_DIR / f"{uuid.uuid4().hex}.wav"
    if TTS_ENGINE == "kokoro":
        try:
            global _kokoro
            from mlx_audio.tts.utils import load_model   # API: verify against installed mlx-audio
            _kokoro = _kokoro or load_model("prince-canuma/Kokoro-82M")
            chunks = [np.asarray(r.audio).reshape(-1) for r in _kokoro.generate(text=text, voice="af_heart", speed=1.0)]
            audio = np.concatenate(chunks); sr = 24000
            _write_wav(out, _resample(audio, sr, 48000), 48000); tts.last_engine = "kokoro"; return out
        except Exception as e:
            log.warning("kokoro failed (%s), falling back to say", e)
    tts.last_engine = "say"
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-o", str(aiff), text], check=True)
    import soundfile as sf
    audio, sr = sf.read(str(aiff), dtype="float32", always_2d=True)
    _write_wav(out, _resample(audio[:, 0], sr, 48000), 48000)
    aiff.unlink(missing_ok=True); return out

def _write_wav(p, audio, sr):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    if len(pcm) < sr // 10: raise ValueError(f"TTS produced only {len(pcm)} samples")
    pcm = np.concatenate([pcm, np.zeros(sr // 5, np.int16)])  # 200 ms tail so the end isn't clipped
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())

def _write_wav16(p, pcm: bytes):
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)

def _resample(audio, sr, target):
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sr == target or len(audio) == 0: return audio
    n = int(round(len(audio) * target / sr))
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)

# ---------- display ----------
disp = Display(lists_loader=load_lists)
SAMPLES = DATA / "womble-samples"; SAMPLES.mkdir(exist_ok=True)
REC = Recorder(SAMPLES, disp, tts=lambda t: tts(t), transcribe=lambda pcm: transcribe(pcm),
               url_for=lambda p: f"http://{HUB_IP}:{HUB_PORT}/tts/{p.name}", beep=make_beep(TTS_DIR / "rec-beep.wav"))
disp.tap_hooks.append(REC.on_tap); disp.snapshot_extra["record"] = REC.counts
async def push_card(card: dict):       # kept for slice-1 callers; translated to list.updated / reply events
    await disp.push_card(card)

# ---------- voice session ----------
class Session:
    def __init__(self, client: APIClient):
        self.c = client; self.buf = bytearray(); self.t = {}; self.active = False

    # ---- adaptive energy VAD (all in samples, independent of wall clock) ----
    SR = 16000
    MIN_SPEECH_S = 1.0        # keep recording at least this long after speech starts
    TRAIL_SIL_S = 0.65         # trailing silence that ends the utterance
    NO_SPEECH_S = 5.0         # give up if nobody speaks
    MAX_S = 10.0
    noise_floor = 30.0       # carried across sessions, updated from quiet chunks

    async def on_start(self, conversation_id, flags, audio_settings, wake_word_phrase):
        self.rec = REC.active
        if self.rec: return await REC.on_start(conversation_id, flags, audio_settings, wake_word_phrase)
        self.buf.clear(); self.active = True
        self.t = {"wake": time.perf_counter(), "wake_word": wake_word_phrase}
        self.speech_start = None; self.last_voice = None; self.peak = 0.0; self.sm = []; self.rms_log = []; self.chunk_sizes = set()
        log.info("run start wake=%s settings=%s", wake_word_phrase, audio_settings)
        asyncio.ensure_future(disp.emit("voice.listening", {"wake_word": wake_word_phrase, "source": "box3-kitchen"}))
        self.c.send_voice_assistant_event(EV.VOICE_ASSISTANT_RUN_START, None)
        self.c.send_voice_assistant_event(EV.VOICE_ASSISTANT_STT_START, None)
        return 0   # 0 = audio over the API connection

    def feed(self, data: bytes) -> bool:
        """Append audio; return True when the utterance has ended."""
        self.buf += data; self.chunk_sizes.add(len(data))
        n = len(self.buf) // 2; pos = n / self.SR
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        self.rms_log.append(round(rms))
        # speech onset: above 3x pre-speech floor (min 60). Silence after onset is RELATIVE to the
        # utterance's peak (smoothed): below max(3x floor, 15% of peak). Background speech/TV at
        # ~100-200 RMS after a ~1200 peak then counts as silence (tuned on 12:05 live capture).
        self.sm = getattr(self, "sm", [])[-2:] + [rms]
        srms = float(np.median(self.sm))                 # 3-chunk median, ignores single spikes
        onset_thr = max(Session.noise_floor * 3.0, 60.0)
        if self.speech_start is None:
            if srms > onset_thr and pos > 0.15:
                self.speech_start = pos; self.last_voice = pos; self.peak = srms
            else:
                Session.noise_floor = 0.95 * Session.noise_floor + 0.05 * rms   # learn floor before speech
        else:
            self.peak = max(self.peak, srms)
            sil_thr = max(Session.noise_floor * 3.0, 0.15 * self.peak)
            if srms > sil_thr: self.last_voice = pos
        if pos >= self.MAX_S: self.end_reason = "max"; return True
        if self.speech_start is None:
            if pos >= self.NO_SPEECH_S: self.end_reason = "no_speech"; return True
            return False
        if pos - self.speech_start >= self.MIN_SPEECH_S and pos - self.last_voice >= self.TRAIL_SIL_S:
            self.end_reason = "silence"; return True
        return False

    async def on_audio(self, data: bytes, data2: bytes | None = None):
        # aioesphomeapi 46.x: handle_audio(data, data2); data2 = optional second mic channel (unused).
        if getattr(self, "rec", False): await REC.on_audio(data); return
        if self.active and self.feed(data): await self.finish()

    async def on_stop(self, abort: bool = False):
        # handle_stop(True) on device stop/abort command, handle_stop(False) on audio end
        if getattr(self, "rec", False): await REC.on_stop(abort); return
        if self.active and not abort: self.end_reason = "device_stop"; await self.finish()
        self.active = False

    async def finish(self):
        if not self.active: return
        self.active = False; t = self.t; t["eos"] = time.perf_counter()
        c = self.c
        c.send_voice_assistant_event(EV.VOICE_ASSISTANT_STT_VAD_END, None)
        text = await asyncio.to_thread(transcribe, bytes(self.buf)); t["stt"] = time.perf_counter()
        c.send_voice_assistant_event(EV.VOICE_ASSISTANT_STT_END, {"text": text})
        if not text:
            await disp.emit("voice.cancel", {"reason": getattr(self, "end_reason", "no_speech")})
        else:
            await disp.emit("voice.heard", {"text": text})
        try: REC.save_passive(bytes(self.buf), t.get("wake_word"), text)   # passive wake-word capture (post-wake audio only)
        except Exception as e: log.warning("passive save failed: %s", e)
        res = route(text); t["route"] = time.perf_counter()
        c.send_voice_assistant_event(EV.VOICE_ASSISTANT_INTENT_START, None)
        c.send_voice_assistant_event(EV.VOICE_ASSISTANT_INTENT_END, {"conversation_id": ""})
        await push_card(res["card"]); t["display"] = time.perf_counter()
        c.send_voice_assistant_event(EV.VOICE_ASSISTANT_TTS_START, {"text": res["speech"]})
        wav = await asyncio.to_thread(tts, res["speech"]); t["tts"] = time.perf_counter()
        c.send_voice_assistant_event(EV.VOICE_ASSISTANT_TTS_END, {"url": f"http://{HUB_IP}:{HUB_PORT}/tts/{wav.name}"})
        c.send_voice_assistant_event(EV.VOICE_ASSISTANT_RUN_END, None)
        if res["category"] == "record.start":
            with wave.open(str(wav)) as wf: dur = wf.getnframes() / wf.getframerate()
            await REC.start(res["speaker"], delay_s=dur + 1.5, only=res.get("only"))
        elif res["category"] == "record.stop":
            await REC.stop("voice")
        w = t["wake"]
        r = self.rms_log
        rec = {"ts": time.time(), "text": text, "end_reason": getattr(self, "end_reason", "?"),
               "speech_start_s": self.speech_start, "speech_end_s": self.last_voice, "peak": round(self.peak), "noise_floor": round(Session.noise_floor),
               "rms_max": max(r) if r else 0, "rms_median": int(np.median(r)) if r else 0,
               "chunk_bytes": sorted(self.chunk_sizes), "tts_file_bytes": wav.stat().st_size, "category": res["category"], "audio_s": round(len(self.buf) / 32000, 2),
               **{f"{k}_ms": round((t[k] - w) * 1000) for k in ("eos", "stt", "route", "display", "tts")},
               "stt_only_ms": round((t["stt"] - t["eos"]) * 1000), "tts_only_ms": round((t["tts"] - t["display"]) * 1000)}
        with TIMING_LOG.open("a") as f: f.write(json.dumps(rec) + "\n")
        _write_wav16(DATA / "last_utterance.wav", bytes(self.buf))
        log.info("timing %s", rec)

# ---------- web ----------
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log.info("warming whisper…"); await asyncio.to_thread(warmup)
    app = web.Application()
    disp.add_routes(app)          # GET / (static/display.html), WS /ws, POST /api/event, GET /api/taps
    app.router.add_static("/tts/", TTS_DIR)
    app.router.add_get("/old", lambda r: web.FileResponse(BASE.parent / "display" / "index.html"))  # slice-1 page, rollback
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HUB_PORT).start()
    try:   # Alexa jobs from Cloudflare Queue (ALEXA.md §8); stays disabled without the Keychain token
        from alexa_pull import AlexaPuller
        alexa = AlexaPuller(disp, route, push_card); alexa.start()   # local ref keeps the task alive
    except Exception as e:
        log.warning("alexa pull not started: %s", e)

    # Simple explicit reconnect loop (ReconnectLogic without zeroconf backed off for minutes after a reboot)
    while True:
        client = APIClient(BOX3_HOST, 6053, None, noise_psk=PSK, client_info="petit-hub")
        lost = asyncio.Event()
        async def on_stop(expected_disconnect: bool):
            lost.set()
        try:
            await client.connect(on_stop=on_stop, login=True)
            s_ = Session(client)
            await REC.attach(client)
            client.subscribe_voice_assistant(handle_start=s_.on_start, handle_stop=s_.on_stop, handle_audio=s_.on_audio)
            log.info("connected to BOX-3, voice assistant subscribed")
            try:   # mirror device logs (wake detections, warnings) into hub.log
                from aioesphomeapi import LogLevel
                dlog = logging.getLogger("box3")
                def _on_log(msg):
                    line = msg.message.decode("utf-8", "replace") if isinstance(msg.message, bytes) else str(msg.message)
                    if any(k in line for k in ("WAKE", "micro_wake", "voice_assistant", "[W]", "[E]", "re-arming")):
                        dlog.info(line.strip())
                client.subscribe_logs(_on_log, log_level=LogLevel.LOG_LEVEL_DEBUG)
            except Exception as e:
                log.warning("device log subscribe failed: %s", e)
            await lost.wait()
            log.warning("BOX-3 disconnected; reconnecting")
            await REC.stop("disconnect"); REC.detach()
        except Exception as e:
            log.warning("connect failed (%s: %s); retry in 5 s", type(e).__name__, e)
        try: await client.disconnect()
        except Exception: pass
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
