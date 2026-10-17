"""Womble wake-word sample recorder (round-2 microWakeWord training data).

Active mode: the hub presses the BOX-3's existing "Talk" template button over the native API
(button: on_press -> micro_wake_word.stop + voice_assistant.start), which starts a pipeline
without a wake word. The audio arrives through the normal subscribe_voice_assistant callbacks;
the hub routes them here while a recording run is active. Each round we capture one short
utterance, trim it, check it, save it, then end the pipeline with a TTS_END whose URL is a short
beep (or a spoken prompt when the variety changes); the device plays it and re-arms. Then the
next round starts.

Passive mode: hub.Session.finish() calls save_passive() for every real wake-word run (post-wake speech only).

Files: data/womble-samples/<speaker>/<ts>.wav             positives (16 kHz mono s16)
       data/womble-samples/<speaker>/negatives/<ts>.wav   similar-sounding non-wake phrases
       data/womble-samples/<speaker>/manifest.jsonl       one line per kept/rejected clip
       data/womble-samples/passive/<wake_word>/<ts>.wav   passive post-wake audio
"""
import asyncio, json, logging, math, os, re, time, wave
from datetime import datetime
from pathlib import Path
import numpy as np

log = logging.getLogger("recorder")
SR = 16000
SPEAKERS = ("andrew", "vic")
N_POS = int(os.environ.get("WOMBLE_REC_POS", "50"))
VARIETY = [  # (count, prompt shown + spoken when the block starts)
    (15, "normal voice"), (10, "quietly"), (10, "from across the room"), (10, "while walking away"), (5, "a bit faster")]
NEGATIVES = ["wobble", "humble", "tumble", "wombat", "rumble", "Wembley", "wimple", "warble", "mumble", "Womack",
             "what's the weather like", "where did I put the keys", "we'll be back later", "I'm going to the shop",
             "can you turn the music down"]
STOP_RE = re.compile(r"\b(stop|cancel|finish|enough)\b", re.I)

def _wav(p: Path, pcm: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)

def _ts():
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]

def analyse(pcm: bytes, kind: str):
    """Trim silence, reject silent/clipped. Returns (trimmed_bytes|None, info dict)."""
    x = np.frombuffer(pcm, np.int16).astype(np.float32)
    info = {"raw_s": round(len(x) / SR, 2)}
    if len(x) < SR // 4: return None, {**info, "reject": "too_short_capture"}
    hop = 320  # 20 ms frames
    fr = x[: len(x) // hop * hop].reshape(-1, hop)
    rms = np.sqrt(np.mean(fr ** 2, axis=1))
    floor = float(np.percentile(rms, 10)); peak = float(np.max(np.abs(x)))
    thr = max(floor * 3.0, 0.12 * float(np.percentile(rms, 98)), 60.0)
    info.update(floor=round(floor), peak=int(peak), rms98=round(float(np.percentile(rms, 98))))
    voiced = np.where(rms > thr)[0]
    if len(voiced) < 4 or float(np.percentile(rms, 98)) < max(floor * 4.0, 150.0):
        return None, {**info, "reject": "silent"}
    clip_frac = float(np.mean(np.abs(x) >= 32000))
    info["clip_frac"] = round(clip_frac, 5)
    if clip_frac > 0.001: return None, {**info, "reject": "clipped"}
    pad = int(0.15 * SR)
    a = max(0, voiced[0] * hop - pad); b = min(len(x), (voiced[-1] + 1) * hop + pad)
    speech_s = (voiced[-1] - voiced[0] + 1) * hop / SR
    info["speech_s"] = round(speech_s, 2)
    if kind == "pos":
        if speech_s > 1.6: return None, {**info, "reject": "too_long"}
        if speech_s < 0.2: return None, {**info, "reject": "too_short"}
        # aim for ~1.5 s clips: widen symmetrically with real room audio when the word is short
        want = int(1.5 * SR)
        if b - a < want:
            extra = want - (b - a); a = max(0, a - extra // 2); b = min(len(x), a + want); a = max(0, b - want)
    info["kept_s"] = round((b - a) / SR, 2)
    return x[a:b].astype(np.int16).tobytes(), info

class Recorder:
    def __init__(self, root: Path, disp, tts=None, transcribe=None, url_for=None, beep=None):
        self.root = root; self.disp = disp; self.tts = tts; self.transcribe = transcribe
        self.url_for = url_for or (lambda p: ""); self.beep = beep
        self.client = None; self.talk_key = None
        self.active = False; self.task = None; self.speaker = None; self.stop_reason = None
        self._run = None   # per-pipeline capture state
        self.passive = os.environ.get("WOMBLE_PASSIVE", "1") == "1"
        self.on_tts_wav = None   # optional hook for tests

    # ---------- device ----------
    async def attach(self, client):
        self.client = client; self.talk_key = None
        try:
            ents, _ = await client.list_entities_services()
            for e in ents:
                if type(e).__name__ == "ButtonInfo" and (getattr(e, "object_id", "") == "talk" or getattr(e, "name", "").lower() == "talk"):
                    self.talk_key = e.key
            log.info("recorder: Talk button key=%s", self.talk_key)
        except Exception as e:
            log.warning("recorder: entity list failed: %s", e)

    def detach(self):
        self.client = None; self.talk_key = None
        if self._run and not self._run["done"].done(): self._run["done"].set_result(None)

    # ---------- counts / UI ----------
    def counts(self):
        out = {}
        for s in SPEAKERS:
            d = self.root / s
            out[s] = {"pos": len(list(d.glob("*.wav"))), "neg": len(list((d / "negatives").glob("*.wav")))}
        p = self.root / "passive"
        out["passive"] = len(list(p.glob("*/*.wav"))) if p.exists() else 0
        return out

    async def ui(self, **kw):
        await self.disp.emit("record.state", {"active": self.active, "speaker": self.speaker, "counts": self.counts(), **kw})

    # ---------- control ----------
    async def start(self, speaker: str, delay_s: float = 0.0, only: str | None = None):
        speaker = speaker.lower()
        speaker = "vic" if speaker.startswith("vic") else speaker
        if speaker not in SPEAKERS: return False
        if self.active: await self.stop("restart")
        if self.client is None or self.talk_key is None:
            await self.disp.emit("error", {"message": "Can't record yet", "detail": "BOX-3 not connected or its Talk button is missing", "code": "rec.no_talk"}, ttl_s=10)
            return False
        self.active = True; self.speaker = speaker; self.stop_reason = None; self.only = only
        self.task = asyncio.create_task(self._loop(delay_s))
        return True

    async def stop(self, reason="stop"):
        if not self.active: return
        self.stop_reason = reason; self.active = False
        if self._run and not self._run["done"].done(): self._run["done"].set_result(None)
        if self.task and self.task is not asyncio.current_task():
            try: await asyncio.wait_for(self.task, 8)
            except Exception: self.task.cancel()

    async def on_tap(self, action: str):
        if action.startswith("record.start."): await self.start(action.rsplit(".", 1)[1])
        elif action in ("record.stop", "dismiss"): await self.stop("tap")

    # ---------- plan ----------
    def plan(self):
        steps = []
        n = 0
        for cnt, prompt in VARIETY:
            for i in range(cnt):
                if n >= N_POS: break
                n += 1; steps.append(("pos", n, N_POS, prompt, "Womble", i == 0))
        for i, ph in enumerate(NEGATIVES):
            steps.append(("neg", i + 1, len(NEGATIVES), "not the wake word", ph, i == 0))
        if getattr(self, "only", None): steps = [s for s in steps if s[0] == self.only]
        return steps

    # ---------- pipeline callbacks (hub.Session delegates here while active) ----------
    async def on_start(self, conversation_id, flags, audio_settings, wake_word_phrase):
        r = self._run
        if r is None or r["started"].done():
            log.info("recorder: unexpected pipeline start (wake=%s); ignoring audio", wake_word_phrase)
            self._ignored = True
            self.client.send_voice_assistant_event(EV().VOICE_ASSISTANT_RUN_START, None)
            return 0
        self._ignored = False
        r["started"].set_result(time.perf_counter())
        ev = EV(); self.client.send_voice_assistant_event(ev.VOICE_ASSISTANT_RUN_START, None)
        self.client.send_voice_assistant_event(ev.VOICE_ASSISTANT_STT_START, None)
        return 0

    async def on_audio(self, data: bytes):
        r = self._run
        if r is None or not r["started"].done() or r["done"].done(): 
            if getattr(self, "_ignored", False) and self.client:  # end stray runs right away
                self._ignored = False; asyncio.create_task(self._end_pipeline(None))
            return
        r["buf"] += data
        x = np.frombuffer(data, np.int16).astype(np.float32); rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        pos = len(r["buf"]) / 2 / SR
        if r["onset"] is None:
            r["floor"] = rms if r["floor"] is None else 0.9 * r["floor"] + 0.1 * rms
            if pos > 0.1 and rms > max(r["floor"] * 3.5, 120): r["onset"] = pos; r["last"] = pos
        elif rms > max(r["floor"] * 3.0, 100): r["last"] = pos
        end = pos >= r["max_s"] or (r["onset"] is None and pos >= r["wait_s"]) or \
              (r["onset"] is not None and pos - r["last"] >= 0.45 and pos - r["onset"] >= 0.25)
        if end: r["done"].set_result(None)

    async def on_stop(self, abort: bool):
        r = self._run
        if r and not r["done"].done(): r["done"].set_result(None)

    async def _end_pipeline(self, wav_path):
        ev = EV(); c = self.client
        if c is None: return
        try:
            c.send_voice_assistant_event(ev.VOICE_ASSISTANT_STT_VAD_END, None)
            c.send_voice_assistant_event(ev.VOICE_ASSISTANT_STT_END, {"text": ""})
            # mic and speaker share the BOX-3 I2S bus: let the mic fully stop before asking it to play,
            # otherwise the speaker loops on "Parent bus is busy" and the device never goes idle
            await asyncio.sleep(0.6)
            if wav_path is not None:
                c.send_voice_assistant_event(ev.VOICE_ASSISTANT_TTS_START, {"text": "."})
                c.send_voice_assistant_event(ev.VOICE_ASSISTANT_TTS_END, {"url": self.url_for(wav_path)})
            c.send_voice_assistant_event(ev.VOICE_ASSISTANT_RUN_END, None)
        except Exception as e:
            log.warning("recorder: end pipeline failed: %s", e)

    async def _say(self, text):
        if not self.tts: return self.beep, 0.3
        p = await asyncio.to_thread(self.tts, text)
        with wave.open(str(p)) as w: d = w.getnframes() / w.getframerate()
        return p, d

    # ---------- main loop ----------
    async def _loop(self, delay_s):
        sp = self.speaker; kept = {"pos": 0, "neg": 0}; fails = 0; rejects = 0
        try:
            if delay_s: await asyncio.sleep(delay_s)
            steps = self.plan(); i = 0
            next_audio = None   # (path, dur) played at the end of the previous pipeline
            while self.active and i < len(steps):
                kind, n, total, prompt, phrase, first = steps[i]
                say = (f'Say "{phrase}" now ({n}/{total})' if kind == "pos" else f'Say "{phrase}" ({n}/{total})')
                await self.ui(phase=kind, n=n, total=total, prompt=prompt, phrase=phrase, say=say, status="get ready")
                if next_audio: await asyncio.sleep(next_audio[1] + 0.35)  # let the beep/prompt finish playing
                elif first and kind == "pos": await asyncio.sleep(0.8)
                if not self.active: break
                wait_s = 3.0 if kind == "pos" else 4.0
                r = self._run = {"buf": bytearray(), "started": asyncio.get_running_loop().create_future(),
                                 "done": asyncio.get_running_loop().create_future(), "onset": None, "last": None,
                                 "floor": None, "wait_s": wait_s, "max_s": 2.6 if kind == "pos" else 4.5}
                self.client.button_command(self.talk_key)
                try: await asyncio.wait_for(asyncio.shield(r["started"]), 4.0)
                except asyncio.TimeoutError:
                    fails += 1; log.warning("recorder: Talk press gave no pipeline (%d)", fails)
                    await self.ui(phase=kind, n=n, total=total, prompt=prompt, phrase=phrase, say=say, status="BOX-3 didn't start, retrying")
                    if fails >= 3: self.stop_reason = "device"; break
                    next_audio = None; await asyncio.sleep(2.0); continue
                fails = 0
                await self.ui(phase=kind, n=n, total=total, prompt=prompt, phrase=phrase, say=say, status="listening")
                try: await asyncio.wait_for(asyncio.shield(r["done"]), wait_s + 3.0)
                except asyncio.TimeoutError: pass
                pcm = bytes(r["buf"]); self._run = None
                clip, info = analyse(pcm, kind)
                # what to play to close this pipeline: spoken prompt when the next step starts a new block, else beep
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                ok = clip is not None
                if not self.active: audio = None
                elif nxt is None: audio = None
                elif ok and nxt[5]:
                    audio = await self._say(("Now " + nxt[3] + ".") if nxt[0] == "pos" else "Now some words that are not Womble. Read each one from the screen.")
                elif not ok: audio = await self._say({"silent": "Didn't hear that. Again.", "clipped": "Too loud. Again."}.get(info["reject"], "Again."))
                else: audio = (self.beep, 0.35)
                await self._end_pipeline(audio[0] if audio else None)
                next_audio = audio
                text = ""
                if self.transcribe and len(pcm) > SR // 2:
                    try: text = (await asyncio.to_thread(self.transcribe, pcm)).strip()
                    except Exception as e: log.warning("recorder stt failed: %s", e)
                if text and STOP_RE.search(text) and not re.search(r"w\w*mb", text, re.I):
                    self.stop_reason = "voice"; self.active = False; break
                rec = {"ts": time.time(), "speaker": sp, "kind": kind, "n": n, "prompt": prompt, "phrase": phrase, "heard": text, **info}
                if ok:
                    d = self.root / sp / ("negatives" if kind == "neg" else "")
                    f = d / f"{_ts()}.wav"; _wav(f, clip); rec["file"] = str(f.relative_to(self.root)); kept[kind] += 1; i += 1; rejects = 0
                    await self.ui(phase=kind, n=n, total=total, prompt=prompt, phrase=phrase, say=say, status="saved", last=info)
                else:
                    rejects += 1
                    if rejects >= 4: rejects = 0; i += 1   # don't get stuck on one step
                    await self.ui(phase=kind, n=n, total=total, prompt=prompt, phrase=phrase, say=say, status="rejected: " + info["reject"].replace("_", " "), last=info)
                (self.root / sp).mkdir(parents=True, exist_ok=True)
                with (self.root / sp / "manifest.jsonl").open("a") as fh: fh.write(json.dumps(rec) + "\n")
                log.info("recorder %s", rec)
            if self.stop_reason is None: self.stop_reason = "complete"
        except asyncio.CancelledError:
            self.stop_reason = self.stop_reason or "cancelled"
        except Exception as e:
            log.exception("recorder loop failed"); self.stop_reason = f"error: {e}"
        finally:
            if self._run is not None:
                self._run = None; asyncio.create_task(self._end_pipeline(None))
            self.active = False
            log.info("recorder done speaker=%s kept=%s reason=%s", sp, kept, self.stop_reason)
            await self.disp.emit("record.done", {"speaker": sp, "kept": kept, "reason": self.stop_reason, "counts": self.counts()})

    # ---------- passive ----------
    def save_passive(self, pcm: bytes, wake_word: str | None, text: str = ""):
        if not self.passive or not wake_word or len(pcm) < SR // 2: return None
        slug = re.sub(r"[^a-z0-9]+", "_", wake_word.lower()).strip("_") or "unknown"
        f = self.root / "passive" / slug / f"{_ts()}.wav"; _wav(f, pcm)
        with (self.root / "passive" / "manifest.jsonl").open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "wake_word": wake_word, "file": str(f.relative_to(self.root)), "text": text, "s": round(len(pcm) / 2 / SR, 2)}) + "\n")
        return f

def EV():
    from aioesphomeapi import VoiceAssistantEventType
    return VoiceAssistantEventType

def make_beep(p: Path, sr=48000):
    t = np.arange(int(0.18 * sr)) / sr
    y = 0.35 * np.sin(2 * math.pi * 880 * t) * np.minimum(1, np.minimum(t, t[-1] - t) / 0.01)
    pcm = np.concatenate([(y * 32767).astype(np.int16), np.zeros(sr // 10, np.int16)])
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
    return p
