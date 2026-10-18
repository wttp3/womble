"""Feed a `say` wav through the real Session path with a fake ESPHome client."""
import asyncio, os, subprocess, sys, wave
from pathlib import Path
import numpy as np
os.environ.setdefault("BOX3_NOISE_PSK", "unused"); os.environ.setdefault("HUB_HOST_IP", "127.0.0.1"); os.environ.setdefault("BOX3_HOST", "127.0.0.1")
sys.path.insert(0, str(Path(__file__).parent)); import hub

class FakeClient:
    def send_voice_assistant_event(self, ev, data): print("  event", ev.name, data or "")

def make(text, gain=0.25):
    subprocess.run(["say", "-o", "/tmp/t.aiff", text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", "/tmp/t.aiff", "/tmp/t.wav"], check=True)
    with wave.open("/tmp/t.wav") as w: sp = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) * gain
    rng = np.random.default_rng(1); noise = lambda s: rng.normal(0, 60, int(16000 * s))
    # wake-word tail gap, then speech with a mid-sentence pause, then room noise
    half = len(sp) // 2
    a = np.concatenate([noise(0.6), sp[:half], noise(0.4), sp[half:], noise(3.0)])
    return np.clip(a + rng.normal(0, 60, len(a)), -32768, 32767).astype(np.int16).tobytes()

async def main():
    hub.warmup()
    for text in ["add eggs to the shopping list", "what's on the shopping list"]:
        s = hub.Session(FakeClient()); await s.on_start("", 0, None, "okay nabu")
        pcm = make(text)
        for i in range(0, len(pcm), 1024):
            if not s.active: break
            await s.on_audio(pcm[i:i + 1024], None)
        print(open(hub.TIMING_LOG).read().splitlines()[-1])
    w = sorted(hub.TTS_DIR.glob("*.wav"), key=os.path.getmtime)[-1]
    with wave.open(str(w)) as f: print("tts", w.name, os.path.getsize(w), "bytes", f.getframerate(), "Hz", f.getnchannels(), "ch", round(f.getnframes() / f.getframerate(), 2), "s")
asyncio.run(main())
