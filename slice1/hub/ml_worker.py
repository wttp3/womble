"""Local speech models (Kokoro TTS, whisper STT) in a child process of the hub.

MLX and torch rarely hand memory back to the OS inside one process, so the hub no longer
imports them. ml_client.py starts this file on demand (`python ml_worker.py tts|stt`), talks to
it over stdin/stdout and lets it exit after an idle timeout; the exit is what frees the memory.

Wire format, both directions: 4-byte header length, 4-byte payload length (big endian),
a JSON header, then raw bytes.
  {"op":"tts","text":...}                      -> {"ok":true,"sr":24000} + float32 mono samples
  {"op":"stt","model":...,"lang":...} + s16 PCM -> {"ok":true,"text":...}
  {"op":"warm","what":["tts","stt"],...}       -> {"ok":true}     (load, plus one short run)
  {"op":"ping"} / {"op":"unload"}              -> {"ok":true}
Every reply also carries ms, warm (what is loaded) and mem (MLX active/peak/cache and this
process's footprint, in MB).

Env: MLX_CACHE_MB (128) caps MLX's Metal buffer cache; MLX_MEMORY_LIMIT_MB (unset) passes a
limit to mx.set_memory_limit; TTS_CHUNK_CHARS (300) splits long replies at sentence ends so one
Kokoro run stays short. ML_WORKER_FAKE=1 swaps the models for small numpy stand-ins (tests only).
"""
import gc, json, logging, os, re, struct, sys, time

log = logging.getLogger("hub.ml")
KOKORO_REPO = "prince-canuma/Kokoro-82M"
KOKORO_VOICE = "af_heart"
KOKORO_SR = 24000
MB = 1024 * 1024
MAX_FRAME = 64 * MB


# ---------- framing ----------
def _read_exact(f, n):
    out = bytearray()
    while len(out) < n:
        chunk = f.read(n - len(out))
        if not chunk:
            return None
        out += chunk
    return bytes(out)


def read_frame(f):
    """(header dict, payload bytes), or None when the other side closed the pipe."""
    head = _read_exact(f, 8)
    if head is None:
        return None
    hlen, plen = struct.unpack(">II", head)
    if hlen > MB or plen > MAX_FRAME:
        raise ValueError("frame too large")
    raw = _read_exact(f, hlen)
    if raw is None:
        return None
    payload = _read_exact(f, plen) if plen else b""
    if payload is None:
        return None
    return json.loads(raw), payload


def write_frame(f, header, payload=b""):
    h = json.dumps(header).encode()
    f.write(struct.pack(">II", len(h), len(payload)) + h)
    if payload:
        f.write(payload)
    f.flush()


# ---------- text ----------
_SENT_END = re.compile(r"(?<=[.!?])\s+")


def split_text(text, limit):
    """Pack whole sentences into pieces of at most `limit` characters. Short text is returned as is."""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for s in _SENT_END.split(text):
        if cur and len(cur) + 1 + len(s) > limit:
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        parts.append(cur)
    return parts


# ---------- models ----------
class Engine:
    """Lazy Kokoro and whisper. Used by the worker process, and directly by the hub when ML_WORKER=0."""

    def __init__(self):
        self.fake = os.environ.get("ML_WORKER_FAKE", "0") == "1"
        self.chunk_chars = int(os.environ.get("TTS_CHUNK_CHARS", "300"))
        self.kokoro = None
        self.whisper = None      # model name once loaded
        self._mx = None
        self._mx_tried = False

    # ---- MLX knobs ----
    def mx(self):
        if not self._mx_tried:
            self._mx_tried = True
            if self.fake:
                return None
            try:
                import mlx.core as mx
                self._mx = mx
                self._mxcall("set_cache_limit", int(os.environ.get("MLX_CACHE_MB", "128")) * MB)
                limit = int(os.environ.get("MLX_MEMORY_LIMIT_MB", "0") or 0)
                if limit > 0:
                    self._mxcall("set_memory_limit", limit * MB)
            except Exception as e:
                log.warning("mlx setup failed (%s)", type(e).__name__)
        return self._mx

    def _mxcall(self, name, *args):
        """mx.<name>, or mx.metal.<name> on older MLX. None if neither exists."""
        mx = self._mx
        if mx is None:
            return None
        fn = getattr(mx, name, None) or getattr(getattr(mx, "metal", None), name, None)
        if fn is None:
            return None
        try:
            return fn(*args)
        except Exception:
            return None

    def release(self):
        """Drop cached Metal buffers so GPU memory doesn't grow between runs."""
        self._mxcall("clear_cache")

    def mem(self):
        out = {}
        for key, name in (("mlx_active", "get_active_memory"), ("mlx_peak", "get_peak_memory"),
                          ("mlx_cache", "get_cache_memory")):
            v = self._mxcall(name)
            if v is not None:
                out[key] = round(v / MB)
        try:
            import memlog
            f = memlog.footprint()
            if f is not None:
                out["proc"] = round(f / MB)
        except Exception:
            pass
        return out

    def loaded(self):
        return [n for n, v in (("tts", self.kokoro), ("stt", self.whisper)) if v]

    # ---- TTS ----
    def _load_kokoro(self):
        if self.kokoro is None:
            t0 = time.perf_counter()
            if self.fake:
                self.kokoro = bytearray(int(os.environ.get("ML_WORKER_FAKE_MB", "48")) * MB)
            else:
                self.mx()
                from mlx_audio.tts.utils import load_model   # API: verify against installed mlx-audio
                self.kokoro = load_model(KOKORO_REPO)
            log.info("kokoro loaded in %.1f s", time.perf_counter() - t0)
        return self.kokoro

    def tts(self, text):
        """Float32 mono samples at KOKORO_SR."""
        import numpy as np
        model = self._load_kokoro()
        chunks = []
        for part in split_text(text, self.chunk_chars):
            if self.fake:
                n = int(KOKORO_SR * max(0.2, 0.06 * len(part)))
                chunks.append((0.2 * np.sin(np.arange(n) * 2 * np.pi * 220 / KOKORO_SR)).astype(np.float32))
                continue
            for r in model.generate(text=part, voice=KOKORO_VOICE, speed=1.0):
                chunks.append(np.array(r.audio, dtype=np.float32).reshape(-1))
                del r
            self.release()
        return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)

    # ---- STT ----
    def stt(self, pcm, model, lang=None):
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if 0 < peak < 0.5:
            audio = audio * min(0.5 / peak, 20.0)   # software gain for quiet mic
        if self.fake:
            self.whisper = self.whisper or model or "fake"
            return f"fake transcript {len(pcm)}"
        self.mx()
        import mlx_whisper
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=model, language=lang or None,
                                   condition_on_previous_text=False, temperature=0.0)
        self.whisper = model
        self.release()
        return r.get("text", "").strip()

    def warm(self, what, model=None, lang=None):
        if what == "tts":
            if self.kokoro is None:
                self.tts("Ready.")
        elif what == "stt":
            if self.whisper is None:
                import numpy as np
                rng = np.random.default_rng(0)
                for secs in (2, 4):
                    self.stt(rng.normal(0, 800, 16000 * secs).astype(np.int16).tobytes(), model, lang)

    def unload(self):
        """In-process unload. Frees what MLX will give up; only a process exit returns all of it."""
        self.kokoro = None
        if self.whisper is not None and not self.fake:
            try:
                from mlx_whisper.transcribe import ModelHolder
                ModelHolder.model = None; ModelHolder.model_path = None
            except Exception:
                pass
        self.whisper = None
        gc.collect()
        self.release()

    # ---- one request ----
    def handle(self, op, hdr, payload=b""):
        t0 = time.perf_counter()
        self._mxcall("reset_peak_memory")
        out, data = {}, b""
        if op == "tts":
            audio = self.tts(hdr.get("text") or "")
            out["sr"] = KOKORO_SR
            data = audio.tobytes()
        elif op == "stt":
            out["text"] = self.stt(payload, hdr.get("model"), hdr.get("lang"))
        elif op == "warm":
            for what in hdr.get("what") or []:
                self.warm(what, hdr.get("model"), hdr.get("lang"))
        elif op == "unload":
            self.unload()
        elif op == "crash" and self.fake:
            os._exit(3)
        elif op == "sleep" and self.fake:
            time.sleep(float(hdr.get("s") or 0))
        elif op != "ping":
            raise ValueError(f"unknown op {op!r}")
        out.update(ok=True, ms=round((time.perf_counter() - t0) * 1000), warm=self.loaded(), mem=self.mem())
        return out, data


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "ml"
    # Keep the reply pipe private: anything a library prints goes to stderr (hub.log) instead.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    inp = sys.stdin.buffer
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s %(name)s[{name}] %(message)s")
    eng = Engine()
    while True:
        frame = read_frame(inp)
        if frame is None:      # hub closed the pipe (idle timeout, or the hub went away)
            break
        hdr, payload = frame
        op = hdr.get("op")
        if op == "exit":
            break
        try:
            reply, data = eng.handle(op, hdr, payload)
        except Exception as e:
            log.warning("%s failed (%s: %s)", op, type(e).__name__, e)
            reply, data = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "warm": eng.loaded()}, b""
        try:
            write_frame(out, reply, data)
        except OSError:
            break


if __name__ == "__main__":
    main()
