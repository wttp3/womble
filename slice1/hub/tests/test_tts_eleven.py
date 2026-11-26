"""TTS_ENGINE=elevenlabs against a fake local HTTP server. Offline, no real key, no models.

Covers: request shape and PCM -> 48 kHz WAV, 402 -> Kokoro fallback + cooldown, 401 cooldown,
429 retried once, 429 twice -> fallback, 5xx, timeout, connection refused, normalization refused,
no key, no Kokoro prewarm at the wake word, key kept out of the worker env.
Kokoro here is the ML_WORKER_FAKE stand-in. Run: .venv/bin/python tests/test_tts_eleven.py
"""
import asyncio, json, logging, os, socket, sys, tempfile, threading, time, wave
from pathlib import Path

FAKE_KEY = "test-key-not-real"
os.environ.update({
    "TTS_ENGINE": "elevenlabs", "ELEVENLABS_API_KEY": FAKE_KEY, "ML_WORKER_FAKE": "1", "ML_WORKER": "1",
    "STT_ENGINE": "xai", "TTS_IDLE_UNLOAD_S": "0", "ELEVENLABS_TIMEOUT_S": "1.0",
    "ELEVENLABS_CONNECT_TIMEOUT_S": "0.5", "ELEVENLABS_COOLDOWN_S": "600",
})
for k in ("ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL", "ELEVENLABS_TEXT_NORMALIZATION"):
    os.environ.pop(k, None)
os.environ.setdefault("BOX3_NOISE_PSK", "unused")
os.environ.setdefault("HUB_HOST_IP", "127.0.0.1")
os.environ.setdefault("BOX3_HOST", "127.0.0.1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from aiohttp import web


def check(cond, msg):
    if not cond: raise SystemExit("FAIL " + msg)


# ---------- fake ElevenLabs ----------
SR = 24000
PCM = (0.3 * 32767 * np.sin(np.arange(SR) * 2 * np.pi * 440 / SR)).astype("<i2").tobytes()   # 1 s
seen = []           # every request the server got
script = []         # responses to give, in order; empty = 200 with PCM


async def handle(request):
    body = await request.json()
    seen.append({"path": request.path, "query": dict(request.query), "key": request.headers.get("xi-api-key"),
                 "body": body})
    step = script.pop(0) if script else {"status": 200}
    if step.get("sleep"):
        await asyncio.sleep(step["sleep"])
    if step.get("status", 200) != 200:
        return web.json_response({"detail": {"status": step.get("code", ""), "message": "SECRET-LOOKING MESSAGE"}},
                                 status=step["status"])
    resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
    await resp.prepare(request)
    for i in range(0, len(PCM), 5001):        # odd-sized chunks, like a real stream
        await resp.write(PCM[i:i + 5001])
    await resp.write_eof()
    return resp


def start_server():
    ready = threading.Event(); box = {}
    def run():
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        app = web.Application(); app.router.add_post("/v1/text-to-speech/{voice}/stream", handle)
        runner = web.AppRunner(app, access_log=None); loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0); loop.run_until_complete(site.start())
        box["port"] = site._server.sockets[0].getsockname()[1]; ready.set()
        loop.run_forever()
    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    return box["port"]


class LogCatch(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, rec): self.lines.append(rec.getMessage())


def main():
    port = start_server()
    os.environ["ELEVENLABS_BASE_URL"] = f"http://127.0.0.1:{port}"
    import hub, ml_client, tts_eleven
    catch = LogCatch(); logging.getLogger("hub").addHandler(catch); logging.getLogger("hub").setLevel(logging.INFO)
    tmp = Path(tempfile.mkdtemp()); hub.TTS_DIR = tmp
    said = []
    real_run = hub.subprocess.run
    def no_say(cmd, **kw):
        said.append(cmd[0]); raise FileNotFoundError("say")
    hub.subprocess.run = no_say

    def fresh():
        seen.clear(); script.clear(); catch.lines.clear(); tts_eleven.reset(); hub.ML_TTS.stop()

    # ---- success: request shape, PCM -> 48 kHz mono 16-bit WAV with the 200 ms tail ----
    fresh()
    wav = hub.tts("It's twenty past four.")
    check(hub.tts.last_engine == "elevenlabs", "engine used")
    r = seen[0]
    check(r["path"] == "/v1/text-to-speech/rhS7yjXTU4uIlRxXhNW7/stream", f"default voice in path: {r['path']}")
    check(r["query"] == {"output_format": "pcm_24000"}, f"query {r['query']}")
    check(r["key"] == FAKE_KEY, "key header")
    check(r["body"] == {"text": "It's twenty past four.", "model_id": "eleven_flash_v2_5",
                        "apply_text_normalization": "on"}, f"body {r['body']}")
    with wave.open(str(wav)) as w:
        check((w.getframerate(), w.getnchannels(), w.getsampwidth()) == (48000, 1, 2), "wav format")
        check(w.getnframes() == 48000 + 48000 // 5, f"1 s of audio + 200 ms tail ({w.getnframes()})")
        x = np.frombuffer(w.readframes(48000), "<i2")
    check(0.25 * 32767 < np.abs(x).max() < 0.35 * 32767, "level preserved")
    check(hub.ML_TTS.pid() is None, "Kokoro was not started")
    check(not any(FAKE_KEY in l for l in catch.lines), "key not logged")

    os.environ["ELEVENLABS_VOICE_ID"] = "voiceX"; os.environ["ELEVENLABS_MODEL"] = "modelY"
    hub.synthesize("Hi there.")
    check(seen[-1]["path"].endswith("/voiceX/stream") and seen[-1]["body"]["model_id"] == "modelY", "env voice and model")
    del os.environ["ELEVENLABS_VOICE_ID"], os.environ["ELEVENLABS_MODEL"]

    # ---- first-sentence join path: two sentences, one WAV ----
    fresh()
    async def joined():
        spoken = {"first": None, "rest": [], "text": []}
        hub.begin_sentence_audio(spoken, "First sentence."); hub.begin_sentence_audio(spoken, "Second one.")
        return await hub._speak("First sentence. Second one.", spoken)
    wav = asyncio.run(joined())
    with wave.open(str(wav)) as w:
        check(w.getnframes() == 2 * 48000 + 48000 // 5, "two streamed pieces joined")
    check([s["body"]["text"] for s in seen] == ["First sentence.", "Second one."], "one request per piece")

    # ---- 402: Kokoro speaks, reason logged as status + code only, then a cooldown ----
    fresh(); script.append({"status": 402, "code": "insufficient_credits"})
    audio, sr = hub.synthesize("No credit left.")
    check(hub.tts.last_engine == "kokoro" and len(audio) > 4800, "402 falls back to Kokoro")
    line = next(l for l in catch.lines if "elevenlabs tts failed" in l)
    check("http 402 insufficient_credits" in line and "SECRET" not in line, f"reason logged: {line}")
    check(tts_eleven.cooling(), "cooldown started")
    n = len(seen); hub.synthesize("Still no credit.")
    check(len(seen) == n and hub.tts.last_engine == "kokoro", "no request during the cooldown")
    tts_eleven._cooldown_until = time.time() - 1
    hub.synthesize("Topped up.")
    check(hub.tts.last_engine == "elevenlabs" and len(seen) == n + 1, "ElevenLabs again after the cooldown")

    fresh(); script.append({"status": 401, "code": "quota_exceeded"})
    hub.synthesize("x y z.")
    check(hub.tts.last_engine == "kokoro" and tts_eleven.cooling(), "401 also cools down")

    # ---- 429: one short retry ----
    fresh(); script.append({"status": 429, "code": "too_many_concurrent_requests"})
    hub.synthesize("Busy once.")
    check(hub.tts.last_engine == "elevenlabs" and len(seen) == 2, "429 retried once, then spoken by ElevenLabs")
    fresh(); script.extend([{"status": 429, "code": "rate_limit"}, {"status": 429, "code": "rate_limit"}])
    hub.synthesize("Busy twice.")
    check(hub.tts.last_engine == "kokoro" and len(seen) == 2 and not tts_eleven.cooling(), "429 twice -> Kokoro, no cooldown")
    check(any("http 429 rate_limit" in l for l in catch.lines), "429 reason logged")

    # ---- 5xx, timeout, network ----
    fresh(); script.append({"status": 503})
    hub.synthesize("Server down.")
    check(hub.tts.last_engine == "kokoro" and len(seen) == 1 and not tts_eleven.cooling(), "5xx -> Kokoro")
    check(any("(http 503)" in l for l in catch.lines), "5xx reason logged")

    fresh(); script.append({"sleep": 5})
    t0 = time.perf_counter(); hub.synthesize("Too slow.")
    took = time.perf_counter() - t0
    check(hub.tts.last_engine == "kokoro" and any("(timeout)" in l for l in catch.lines), "timeout -> Kokoro")
    check(took < 4, f"timeout respected ({took:.1f}s)")
    hub.synthesize("Fine again.")
    check(hub.tts.last_engine == "elevenlabs", "next reply uses ElevenLabs again")

    fresh()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
    os.environ["ELEVENLABS_BASE_URL"] = f"http://127.0.0.1:{dead}"
    hub.synthesize("Nobody home.")
    check(hub.tts.last_engine == "kokoro" and any("(network " in l for l in catch.lines), "network error -> Kokoro")
    os.environ["ELEVENLABS_BASE_URL"] = f"http://127.0.0.1:{port}"

    # ---- a plan that refuses text normalization: retried without it, and remembered ----
    fresh(); script.append({"status": 400, "code": "unsupported_text_normalization"})
    hub.synthesize("Normalise me.")
    check(hub.tts.last_engine == "elevenlabs" and "apply_text_normalization" not in seen[1]["body"], "retried without normalization")
    hub.synthesize("And again.")
    check("apply_text_normalization" not in seen[2]["body"] and len(seen) == 3, "remembered")

    # ---- Kokoro failing too -> say ----
    fresh(); script.append({"status": 500}); said.clear()
    real_req = hub.ML_TTS.request
    hub.ML_TTS.request = lambda *a, **k: (_ for _ in ()).throw(ml_client.WorkerError("boom"))
    try: hub.synthesize("Everything is down.")
    except FileNotFoundError: pass
    finally: hub.ML_TTS.request = real_req
    check(said == ["say"], "elevenlabs -> kokoro -> say")

    # ---- no key: straight to Kokoro, no request ----
    fresh(); os.environ.pop("ELEVENLABS_API_KEY")
    tts_eleven.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))})
    hub.synthesize("No key.")
    check(hub.tts.last_engine == "kokoro" and not seen, "no key -> Kokoro without a request")
    os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY

    # ---- wake word and warm-up leave Kokoro alone; the key stays out of the worker ----
    fresh()
    class Client:
        def send_voice_assistant_event(self, ev, data): pass
    async def wake():
        async def emit(*a, **k): pass
        hub.disp.emit = emit
        await hub.Session(Client()).on_start("", 0, None, "womble")
    asyncio.run(wake()); time.sleep(0.3)
    check(hub.ML_TTS.pid() is None and not hub.ML_TTS._warming, "wake word does not prewarm Kokoro")
    hub.warmup()
    check(hub.ML_TTS.pid() is None and not seen, "warm-up starts nothing for elevenlabs")
    hdr, _ = hub.ML_TTS.request("hasenv", {"name": "ELEVENLABS_API_KEY"})
    check(hdr["has"] is False, "key withheld from the worker env")
    check(hub.ML_TTS.request("hasenv", {"name": "ML_WORKER_FAKE"})[0]["has"] is True, "other env still passed")

    hub.subprocess.run = real_run
    ml_client.stop_all()
    print("test_tts_eleven OK")


if __name__ == "__main__":
    main()
