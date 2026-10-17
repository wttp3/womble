"""Offline smoke test: say -> wav -> mlx-whisper -> router -> Kokoro. Appends to data/smoke.jsonl."""
import json, os, subprocess, sys, time, wave
from pathlib import Path
import numpy as np
os.environ.setdefault("BOX3_NOISE_PSK", "unused"); os.environ.setdefault("HUB_HOST_IP", "127.0.0.1")
sys.path.insert(0, str(Path(__file__).parent))
t0 = time.perf_counter(); import hub; t_imp = time.perf_counter()
out = {}
phrases = ["Add oat milk to the shopping list", "What's on the shopping list", "Find me a quick lentil curry recipe"]
wav = Path("/tmp/smoke.wav")
def load(text):
    subprocess.run(["say", "-o", "/tmp/smoke.aiff", text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", "/tmp/smoke.aiff", str(wav)], check=True)
    with wave.open(str(wav)) as w: return w.readframes(w.getnframes())
t = time.perf_counter(); hub.warmup(); out["whisper_cold_load_s"] = round(time.perf_counter() - t, 2)
res = []
for p in phrases:
    pcm = load(p)
    a = time.perf_counter(); text = hub.transcribe(pcm); b = time.perf_counter()
    r = hub.route(text); c = time.perf_counter()
    f = hub.tts(r["speech"]); d = time.perf_counter()
    res.append({"said": p, "heard": text, "audio_s": round(len(pcm)/32000, 2), "category": r["category"],
                "stt_ms": round((b-a)*1000), "route_ms": round((c-b)*1000, 2), "tts_ms": round((d-c)*1000),
                "tts_file": f.name, "tts_engine_used": hub.tts.last_engine})
out.update(import_s=round(t_imp - t0, 2), runs=res)
print(json.dumps(out, indent=1))
with open(hub.DATA / "smoke.jsonl", "a") as fh: fh.write(json.dumps(out) + "\n")
