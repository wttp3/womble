import sys, time, logging, numpy as np
sys.path.insert(0, "."); logging.basicConfig(level=logging.INFO)
from mlx_audio.tts.utils import load_model
import stt_xai
m = load_model("prince-canuma/Kokoro-82M")
a = np.concatenate([np.asarray(r.audio).reshape(-1) for r in m.generate(text="add bread to the shopping list", voice="af_heart", speed=1.0)])
a16 = np.interp(np.arange(0, len(a), 24000/16000), np.arange(len(a)), a)
pcm = (np.clip(a16, -1, 1) * 32767).astype(np.int16).tobytes()
key = stt_xai.get_key(); assert key, "no key"
for i in range(2):
    t0 = time.perf_counter(); txt = stt_xai.transcribe_xai(pcm, key)
    print(f"xai run{i+1}: {txt!r} latency={(time.perf_counter()-t0)*1000:.0f}ms audio={len(pcm)/32000:.2f}s")
