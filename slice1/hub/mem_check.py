"""Load/unload cycles with the real models, to check memory and cold-start time on the Mac.

    .venv/bin/python mem_check.py                 # 3 Kokoro cycles
    .venv/bin/python mem_check.py --stt           # plus one whisper cycle (loads ~1.6 GB for a moment)
    ML_WORKER_FAKE=1 python mem_check.py          # no models, anywhere (what tests/test_ml_worker.py uses)

No network beyond what the models' own loaders do, no secrets, does not start or touch the hub.
For each cycle it prints the cold load time, warm synthesis time, the worker's footprint after
loading and after a long reply, its lifetime peak, and that the process is gone afterwards.
"""
import argparse, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memlog, ml_client, ml_worker

SHORT = "It's twenty past four."
LONG = " ".join(["Here is a longer answer with several sentences, the kind a recipe step or a research summary produces."] * 8)


def mbs(n):
    return "n/a" if n is None else f"{n / memlog.MB:.0f} MB"


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--stt", action="store_true", help="also load whisper once")
    ap.add_argument("--idle", type=float, default=5.0, help="idle seconds before the worker exits")
    a = ap.parse_args()
    print(f"this process (stands in for the hub): {mbs(memlog.footprint())}")
    def offline(repo, needs=()):     # same rule as hub._hf_env: no Hugging Face calls when the model is cached
        on = "HF_HUB_OFFLINE" not in os.environ and os.environ.get("ML_HF_OFFLINE", "1") != "0" and ml_client.hf_cached(repo, needs)
        return {"HF_HUB_OFFLINE": "1"} if on else {}
    tts_env = offline(ml_worker.KOKORO_REPO, [f"voices/{ml_worker.KOKORO_VOICE}.*"])
    print(f"kokoro worker HF offline: {bool(tts_env)} | TTS_CHUNK_CHARS={os.environ.get('TTS_CHUNK_CHARS', '200')}")
    w = ml_client.make("tts", idle_s=a.idle, env=tts_env)
    for i in range(a.cycles):
        t0 = time.perf_counter(); w.request("warm", {"what": ["tts"]}); cold = time.perf_counter() - t0
        pid = w.pid(); loaded = memlog.footprint(pid)
        t0 = time.perf_counter(); hdr, raw = w.request("tts", {"text": SHORT}); short = time.perf_counter() - t0
        t0 = time.perf_counter(); hdr, raw = w.request("tts", {"text": LONG}); long_s = time.perf_counter() - t0
        after, peak = memlog.footprint(pid), memlog.lifetime_peak(pid)
        print(f"cycle {i + 1}: cold load {cold:.1f} s | short reply {short * 1000:.0f} ms | long reply "
              f"{long_s:.1f} s ({len(raw) / 4 / 24000:.0f} s audio) | worker loaded {mbs(loaded)}, after long "
              f"{mbs(after)}, peak {mbs(peak)} | mlx {hdr.get('mem')}")
        end = time.time() + a.idle + 10
        while time.time() < end and not (w.pid() is None and gone(pid)):
            time.sleep(0.2)
        print(f"         worker {'exited' if gone(pid) else 'STILL RUNNING'} after idle; "
              f"this process {mbs(memlog.footprint())}")
    if a.stt:
        model = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
        s = ml_client.make("stt", idle_s=0, env=offline(model))
        t0 = time.perf_counter(); hdr, _ = s.request("warm", {"what": ["stt"], "model": model, "lang": "en"})
        pid = s.pid()
        print(f"whisper: cold load + 2 runs {time.perf_counter() - t0:.1f} s | worker {mbs(memlog.footprint(pid))}, "
              f"peak {mbs(memlog.lifetime_peak(pid))} | mlx {hdr.get('mem')}")
        s.stop()
        print(f"         worker {'exited' if gone(pid) else 'STILL RUNNING'}; this process {mbs(memlog.footprint())}")
    ml_client.stop_all()


if __name__ == "__main__":
    main()
