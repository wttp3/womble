"""Fake-device test for recorder.py (no BOX-3, no ML). python tests/test_recorder.py"""
import asyncio, json, os, sys, tempfile, types, wave
from pathlib import Path
import numpy as np
os.environ["WOMBLE_REC_POS"] = "6"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# stub aioesphomeapi enum if not installed
try: import aioesphomeapi  # noqa
except ImportError:
    import enum; m = types.ModuleType("aioesphomeapi")
    m.VoiceAssistantEventType = enum.Enum("EV", "VOICE_ASSISTANT_RUN_START VOICE_ASSISTANT_STT_START VOICE_ASSISTANT_STT_VAD_END VOICE_ASSISTANT_STT_END VOICE_ASSISTANT_TTS_START VOICE_ASSISTANT_TTS_END VOICE_ASSISTANT_RUN_END")
    sys.modules["aioesphomeapi"] = m
import recorder as R
R.NEGATIVES = R.NEGATIVES[:3]

rng = np.random.default_rng(0)
def clip(kind):
    sil = lambda s: rng.normal(0, 40, int(16000 * s))
    if kind == "silent": a = sil(3.2)
    else:
        t = np.arange(int(16000 * 0.55)) / 16000
        v = (3000 if kind == "ok" else 40000) * np.sin(2 * np.pi * 180 * t) * np.hanning(len(t))
        a = np.concatenate([sil(0.4), v + sil(0.55), sil(1.0)])
    return np.clip(a, -32768, 32767).astype(np.int16).tobytes()

script = ["ok", "silent", "ok", "clipped", "ok", "ok", "ok", "ok", "ok", "ok", "ok"]
class Disp:
    def __init__(s): s.ev = []
    async def emit(s, e, d=None, **k): s.ev.append((e, d))
class Btn: pass
Btn.__name__ = "ButtonInfo"
class Client:
    def __init__(s): s.events = []; s.presses = 0
    async def list_entities_services(s):
        b = Btn(); b.object_id = "talk"; b.name = "Talk"; b.key = 42; return [b], []
    def send_voice_assistant_event(s, e, d): s.events.append((e.name, d))
    def button_command(s, key):
        assert key == 42; k = script[s.presses] if s.presses < len(script) else "ok"; s.presses += 1
        async def run():
            await asyncio.sleep(0.05); await rec.on_start("", 0, None, None)
            pcm = clip(k)
            for i in range(0, len(pcm), 1024):
                if rec._run is None or rec._run["done"].done(): break
                await rec.on_audio(pcm[i:i + 1024])
        asyncio.get_running_loop().create_task(run())

async def main():
    global rec
    root = Path(tempfile.mkdtemp()); disp = Disp(); c = Client()
    beep = R.make_beep(root / "beep.wav")
    rec = R.Recorder(root, disp, tts=None, transcribe=lambda pcm: "", beep=beep, url_for=lambda p: "http://x/" + p.name)
    await rec.attach(c); assert rec.talk_key == 42
    assert await rec.start("Andrew")
    await asyncio.wait_for(rec.task, 60)
    cnt = rec.counts(); print("counts", cnt)
    pos = sorted((root / "andrew").glob("*.wav")); neg = sorted((root / "andrew/negatives").glob("*.wav"))
    assert len(pos) == 6 and len(neg) == 3, (len(pos), len(neg))
    with wave.open(str(pos[0])) as w: assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2); print("pos clip s", w.getnframes() / 16000)
    man = [json.loads(l) for l in open(root / "andrew/manifest.jsonl")]
    rej = [m["reject"] for m in man if "reject" in m]; print("rejects", rej); assert rej == ["silent", "clipped"]
    done = [d for e, d in disp.ev if e == "record.done"][-1]; print("done", done); assert done["reason"] == "complete"
    says = [d["say"] for e, d in disp.ev if e == "record.state"]; print(says[0], "|", says[-1])
    assert c.events.count(("VOICE_ASSISTANT_RUN_END", None)) == c.presses
    # stop by voice
    rec.transcribe = lambda pcm: "Stop."; await rec.start("vic"); await asyncio.wait_for(rec.task, 20)
    assert rec.stop_reason == "voice" and rec.counts()["vic"]["pos"] == 0
    # stop by tap
    rec.transcribe = lambda pcm: ""; await rec.start("vic"); await asyncio.sleep(1.0); await rec.on_tap("dismiss")
    assert not rec.active and rec.stop_reason == "tap"; print("tap stop ok, vic", rec.counts()["vic"])
    # passive
    f = rec.save_passive(clip("ok"), "Womble", "add eggs"); assert f.exists() and "passive/womble" in str(f)
    assert rec.save_passive(clip("ok"), None) is None
    print("ALL PASS")
asyncio.run(main())
