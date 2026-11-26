"""On-demand model workers: load, idle exit, reload, crash and timeout recovery, hub wiring.

Offline and model-free: ML_WORKER_FAKE=1 swaps Kokoro/whisper for numpy stand-ins that hold
some ballast, so the process lifecycle and the memory hand-back are real even though the
models are not. Run: .venv/bin/python tests/test_ml_worker.py
(mem_check.py does the same cycles with the real models on the Mac.)
"""
import asyncio, io, os, sys, tempfile, threading, time
from pathlib import Path

os.environ["ML_WORKER_FAKE"] = "1"
os.environ["ML_WORKER_FAKE_MB"] = "48"
os.environ["ML_WORKER"] = "1"
os.environ["TTS_ENGINE"] = "kokoro"
os.environ["STT_ENGINE"] = "xai"
os.environ["TTS_IDLE_UNLOAD_S"] = "0.7"
os.environ["TTS_KEEP_S"] = "3600"
os.environ.setdefault("BOX3_NOISE_PSK", "unused")
os.environ.setdefault("HUB_HOST_IP", "127.0.0.1")
os.environ.setdefault("BOX3_HOST", "127.0.0.1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import memlog, ml_client
from ml_worker import Engine, read_frame, split_text, write_frame

MB = 1024 * 1024


def check(cond, msg):
    if not cond: raise SystemExit("FAIL " + msg)


def gone(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn(): return True
        time.sleep(0.05)
    return fn()


def test_framing_and_split():
    b = io.BytesIO()
    write_frame(b, {"op": "tts", "text": "hé"}, b"\x00\x01")
    write_frame(b, {"op": "ping"})
    b.seek(0)
    check(read_frame(b) == ({"op": "tts", "text": "hé"}, b"\x00\x01"), "frame 1")
    check(read_frame(b) == ({"op": "ping"}, b""), "frame 2")
    check(read_frame(b) is None, "eof")
    check(split_text("Short one. Two.", 300) == ["Short one. Two."], "short text is one piece")
    long = " ".join(f"Sentence number {i} is here." for i in range(40))
    parts = split_text(long, 120)
    check(len(parts) > 5 and all(len(p) <= 120 for p in parts), "pieces within the limit")
    check(" ".join(parts) == long, "nothing lost when split")
    check(split_text(long, 0) == [long], "0 disables splitting")


def test_cycles():
    """Load -> speak -> idle exit, several times. The worker's memory goes back each time."""
    w = ml_client.make("tts", idle_s=0.5)
    check(isinstance(w, ml_client.MLWorker) and w.pid() is None, "nothing runs before first use")
    base = memlog.footprint()
    fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    peaks = []
    for i in range(5):
        hdr, raw = w.request("tts", {"text": "Hello there. This is cycle %d." % i})
        audio = np.frombuffer(raw, np.float32)
        check(hdr["sr"] == 24000 and len(audio) > 2400 and hdr["warm"] == ["tts"], "audio back")
        pid = w.pid()
        check(pid and not gone(pid), "worker alive after a request")
        peaks.append(memlog.footprint(pid) or 0)
        hdr2, _ = w.request("tts", {"text": "Again."})
        check(w.pid() == pid, "same worker reused inside the idle window")
        check(wait_for(lambda: w.pid() is None and gone(pid)), "worker exits when idle")
    check(w.starts == 5, f"one start per cycle ({w.starts})")
    if all(peaks):
        check(min(peaks) > 40 * MB, f"worker held the model ({[p // MB for p in peaks]} MB)")
        grew = (memlog.footprint() - base) / MB
        check(grew < 8, f"hub did not grow over the cycles (+{grew:.1f} MB)")
        print("  worker footprint per cycle MB:", [p // MB for p in peaks], "hub growth MB: %.1f" % grew)
    if fds is not None:
        check(len(os.listdir("/proc/self/fd")) <= fds, "no leaked pipes")
    check(wait_for(lambda: not [t for t in threading.enumerate() if isinstance(t, threading.Timer)], 2),
          "no idle timers left")


def test_prewarm_and_touch():
    w = ml_client.make("tts", idle_s=0.6)
    t0 = time.perf_counter(); w.prewarm("tts"); took = time.perf_counter() - t0
    check(took < 0.5, f"prewarm returns at once ({took:.2f}s)")
    hdr, _ = w.request("tts", {"text": "Queued behind the warm-up."})     # waits for the load, no second process
    check(w.starts == 1 and "tts" in w._warm, "request reused the prewarmed worker")
    pid = w.pid()
    time.sleep(0.4); w.prewarm("tts")         # already warm: only restarts the idle countdown
    time.sleep(0.4)
    check(w.pid() == pid, "a wake word keeps a warm worker alive")
    check(wait_for(lambda: w.pid() is None), "then it idles out")
    keep = ml_client.make("tts", idle_s=0)
    keep.request("ping"); pid = keep.pid(); time.sleep(0.3)
    check(keep.pid() == pid, "idle_s=0 keeps the worker")
    keep.stop(); check(gone(pid), "stop() ends it")


def test_failures():
    w = ml_client.make("tts", idle_s=0, timeout=10)
    w.request("ping"); pid = w.pid()
    os.kill(pid, 9); wait_for(lambda: not w.alive())
    hdr, _ = w.request("tts", {"text": "Back again."})
    check(hdr["ok"] and w.pid() != pid, "a killed idle worker is replaced on the next request")
    try:
        w.request("crash"); check(False, "crash should raise")
    except ml_client.WorkerError:
        pass
    check(w.request("ping")[0]["ok"], "usable after a crash")
    try:
        w.request("nope"); check(False, "bad op should raise")
    except ml_client.WorkerError as e:
        check("unknown op" in str(e), "worker error text comes back")
    check(w.alive(), "an error reply does not kill the worker")
    t0 = time.perf_counter()
    try:
        w.request("sleep", {"s": 30}, timeout=0.5); check(False, "timeout should raise")
    except ml_client.WorkerError as e:
        check("timed out" in str(e), "timeout reported")
    check(time.perf_counter() - t0 < 5, "a hung run is cut off")
    check(w.request("ping")[0]["ok"], "usable after a timeout")
    w.stop()


def test_inprocess():
    os.environ["ML_WORKER"] = "0"
    try:
        w = ml_client.make("tts", idle_s=0.4)
        check(isinstance(w, ml_client.InProcess) and not w.alive(), "lazy in-process")
        for _ in range(3):
            hdr, raw = w.request("tts", {"text": "In process."})
            check(len(raw) > 0 and w.alive(), "loaded on use")
            check(wait_for(lambda: not w.alive()), "unloaded when idle")
    finally:
        os.environ["ML_WORKER"] = "1"
    e = Engine()
    e.tts("x"); e.stt(b"\0\0" * 16000, "m"); check(e.loaded() == ["tts", "stt"], "engine loaded")
    e.unload(); check(e.loaded() == [], "engine unloaded")


def test_memlog():
    check(isinstance(memlog.footprint(), int) and memlog.footprint() > MB, "own footprint readable")
    with memlog.event("off") as ev: ev.note(a=1)      # MEM_LOG unset: no-op
    memlog.ENABLED = True
    lines = []
    old = memlog.log.info; memlog.log.info = lambda m, *a: lines.append(m % a if a else m)
    try:
        w = ml_client.make("tts", idle_s=0)
        memlog.watch_pid("tts", w.pid)
        with memlog.event("tts") as ev:
            hold = bytearray(30 * MB)          # a spike that is gone again before the event ends
            time.sleep(0.3); del hold
            w.request("tts", {"text": "Measured."}); ev.note(chars=9)
        w.stop()
        memlog.gauge("answers", lambda: 3)
        lines.append(memlog.snapshot_line())
    finally:
        memlog.log.info = old; memlog.ENABLED = False; memlog._pids.clear(); memlog._gauges.clear()
    ev_line = lines[0]
    check(ev_line.startswith("mem ev=tts ") and "tts_mb=" in ev_line and "chars=9" in ev_line, ev_line)
    f = dict(kv.split("=", 1) for kv in ev_line.split()[1:])
    before, after = (float(x) for x in f["hub_mb"].split("->"))
    check(float(f["peak"]) >= before + 20, f"peak caught the spike: {ev_line}")
    check("mem idle hub_mb=" in lines[1] and "answers=3" in lines[1], lines[1])
    print(" ", ev_line); print(" ", lines[1])


def test_hub_wiring():
    """hub.py speaks and transcribes through the workers and imports no ML library itself."""
    import hub, stt_xai
    for mod in ("mlx", "mlx_whisper", "mlx_audio", "torch", "spacy", "numba"):
        check(mod not in sys.modules, f"hub imported {mod}")
    print("  hub footprint after import: %d MB" % (memlog.footprint() // MB))
    tmp = Path(tempfile.mkdtemp()); hub.TTS_DIR = tmp
    check(hub.ML_TTS.pid() is None and hub.ML_STT.pid() is None, "no model process at idle")

    audio, sr = hub.synthesize("Hello from the worker.")
    check(sr == 48000 and hub.tts.last_engine == "kokoro" and len(audio) > 4800, "kokoro path via worker")
    wav = hub.tts("A spoken line.")
    check(wav.exists() and wav.stat().st_size > 1000, "wav written")
    pid = hub.ML_TTS.pid()
    check(wait_for(lambda: hub.ML_TTS.pid() is None and gone(pid)), "tts worker exits after TTS_IDLE_UNLOAD_S")

    # wake word: loading starts while the user is still talking
    class Client:
        def send_voice_assistant_event(self, ev, data): pass
    async def wake():
        async def emit(*a, **k): pass
        hub.disp.emit = emit
        s = hub.Session(Client()); await s.on_start("", 0, None, "womble")
    asyncio.run(wake())
    check(wait_for(lambda: "tts" in hub.ML_TTS._warm), "wake word prewarms Kokoro")
    check(hub.ML_STT.pid() is None, "whisper is not loaded while xAI is the main STT")
    hub.ML_TTS.stop()

    # whisper as fallback: its process is gone as soon as the transcript is back
    stt_xai.get_key = lambda: None
    text = hub.transcribe(b"\0\0" * 16000)
    check(text.startswith("fake transcript") and hub.ML_STT.pid() is None, "fallback whisper process exits at once")
    os.environ["STT_ENGINE"] = "whisper"
    try:
        hub.transcribe(b"\0\0" * 16000)
        check(hub.ML_STT.pid() is not None, "main-STT whisper stays for the idle window")
    finally:
        os.environ["STT_ENGINE"] = "xai"; hub.ML_STT.stop()

    # Kokoro failing still falls back to `say`
    calls = []
    real = hub.ML_TTS.request
    hub.ML_TTS.request = lambda *a, **k: (_ for _ in ()).throw(ml_client.WorkerError("boom"))
    def fake_say(cmd, **kw):
        calls.append(cmd[0]); raise FileNotFoundError("say")
    real_run = hub.subprocess.run; hub.subprocess.run = fake_say
    try:
        hub.synthesize("x")
    except FileNotFoundError:
        pass
    finally:
        hub.ML_TTS.request = real; hub.subprocess.run = real_run
    check(calls == ["say"], "say fallback reached")

    # old reply audio is pruned, other files are left alone
    old = tmp / ("a" * 32 + ".wav"); old.write_bytes(b"x"); os.utime(old, (time.time() - 7200,) * 2)
    beep = tmp / "rec-beep.wav"; beep.write_bytes(b"x"); os.utime(beep, (time.time() - 7200,) * 2)
    hub._tts_pruned_at = 0
    check(hub._prune_tts() == 1 and not old.exists() and beep.exists() and wav.exists(), "tts prune")


def test_bounded_containers():
    from display import Display, TAPS_KEEP
    from brain.core import Memory
    d = Display()
    async def taps():
        async def emit(*a, **k): pass
        d.emit = emit
        for i in range(TAPS_KEEP + 50): await d.on_tap("dismiss")
    logging_off = memlog.logging.getLogger("display"); lvl = logging_off.level; logging_off.setLevel(50)
    asyncio.run(taps()); logging_off.setLevel(lvl)
    check(len(d.taps) == TAPS_KEEP, "taps bounded")
    now = [0.0]
    m = Memory(ttl_s=300, clock=lambda: now[0])
    for i in range(100):
        now[0] += 400; m.add(f"alexa:session-{i}", "user", "hi")
    check(len(m._store) == 1, f"expired sessions pruned ({len(m._store)})")


if __name__ == "__main__":
    for t in (test_framing_and_split, test_cycles, test_prewarm_and_touch, test_failures, test_inprocess,
              test_memlog, test_hub_wiring, test_bounded_containers):
        print(t.__name__); t()
    ml_client.stop_all()
    print("test_ml_worker OK")
