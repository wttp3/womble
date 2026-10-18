"""Session.finish error path and abort cancel. MLX is stubbed; no network."""
import asyncio, json, os, sys, tempfile, types
from pathlib import Path

os.environ.setdefault("BOX3_NOISE_PSK", "unused")
os.environ.setdefault("HUB_HOST_IP", "127.0.0.1")
os.environ.setdefault("BOX3_HOST", "127.0.0.1")
os.environ.setdefault("TTS_ENGINE", "say")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
mw = types.ModuleType("mlx_whisper")
mw.transcribe = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mlx stub"))
sys.modules["mlx_whisper"] = mw
import hub

def check(cond, msg):
    if not cond: raise SystemExit("FAIL " + msg)

class Client:
    def __init__(self): self.ev = []
    def send_voice_assistant_event(self, ev, data): self.ev.append((ev.name, data))

async def main():
    tmp = Path(tempfile.mkdtemp())
    hub.LISTS.path = tmp / "lists.json"
    hub.TIMING_LOG = tmp / "timing.jsonl"
    events = []
    async def capture(ev, data=None, **k): events.append((ev, data))
    hub.disp.emit = capture
    said = []
    def fake_tts(text):
        p = tmp / f"t{len(said)}.wav"; p.write_bytes(b"wav"); said.append(text); return p
    hub.tts = fake_tts

    # C3: device abort while listening tells the frame to leave "Listening…"
    events.clear()
    c = Client(); s = hub.Session(c)
    await s.on_start("", 0, None, "womble")
    await s.on_stop(True)
    check(not s.active, "still active after abort")
    check(("voice.cancel", {"reason": "abort"}) in events, f"cancel {events}")
    check(not hub.TIMING_LOG.exists(), "abort wrote a timing line")
    print("PASS abort emits voice.cancel")

    # success path still speaks the routed line and writes a timing line with no error key
    events.clear(); said.clear()
    hub.transcribe = lambda pcm: "add milk"
    c = Client(); s = hub.Session(c)
    await s.on_start("", 0, None, "womble")
    s.buf.extend(b"\x00\x01" * 800)
    await s.finish()
    names = [n for n, _ in c.ev]
    check("VOICE_ASSISTANT_ERROR" not in names, f"error on success {names}")
    check(names.count("VOICE_ASSISTANT_RUN_END") == 1, names)
    check(said == ["Added milk to shopping."], said)
    rec = json.loads(hub.TIMING_LOG.read_text().splitlines()[-1])
    check(rec["category"] == "list.add" and "error" not in rec, rec)
    check("stt_only_ms" in rec and "tts_only_ms" in rec, rec)
    print("PASS success path unchanged")

    # C4: a failure still ends the pipeline, shows an error, speaks the sorry line, logs timing
    events.clear(); said.clear()
    def boom(pcm): raise RuntimeError("stt-down")
    hub.transcribe = boom
    c = Client(); s = hub.Session(c)
    await s.on_start("", 0, None, "womble")
    s.end_reason = "silence"
    await s.finish()
    names = [n for n, _ in c.ev]
    check("VOICE_ASSISTANT_ERROR" in names and "VOICE_ASSISTANT_RUN_END" in names, names)
    err = [d for n, d in c.ev if n == "VOICE_ASSISTANT_ERROR"][-1]
    check(err["message"] == hub.SORRY and "stt-down" not in json.dumps(err), err)
    check(said == [hub.SORRY], said)
    shown = [d for ev, d in events if ev == "error"]
    check(shown and shown[-1]["code"] == "finish" and shown[-1]["message"] == hub.SORRY, shown)
    rec = json.loads(hub.TIMING_LOG.read_text().splitlines()[-1])
    check(rec.get("error") == "RuntimeError" and rec["category"] == "error", rec)
    print("PASS finish error path")
    print("test_session_errors OK")

asyncio.run(main())
