"""Replay a captured utterance through Session.feed() and show where it would end."""
import os, sys, wave
from pathlib import Path
os.environ.setdefault("BOX3_NOISE_PSK", "x"); os.environ.setdefault("HUB_HOST_IP", "127.0.0.1"); os.environ.setdefault("BOX3_HOST", "127.0.0.1")
sys.path.insert(0, str(Path(__file__).parent))
import importlib.util
spec = importlib.util.spec_from_file_location("hubmod", Path(__file__).parent / "hub.py")
src = (Path(__file__).parent / "hub.py").read_text().replace("import mlx_whisper", "mlx_whisper = None")
mod = type(sys)("hubmod"); mod.__file__ = spec.origin; exec(compile(src, spec.origin, "exec"), mod.__dict__)
f = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "data/last_utterance.wav")
w = wave.open(f); pcm = w.readframes(w.getnframes())
s = mod.Session(None); s.buf = bytearray(); s.speech_start = None; s.last_voice = None; s.peak = 0.0; s.sm = []; s.rms_log = []; s.chunk_sizes = set()
mod.Session.noise_floor = 30.0
for i in range(0, len(pcm), 1024):
    if s.feed(pcm[i:i + 1024]):
        end = len(s.buf) / 32000
        print(f"end_reason={s.end_reason} speech_start={s.speech_start:.2f}s speech_end={s.last_voice:.2f}s end={end:.2f}s eos_after_speech={end - s.last_voice:.2f}s peak={s.peak:.0f}")
        break
else: print("never ended within file")
