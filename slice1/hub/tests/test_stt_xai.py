"""Offline test for stt_xai with mocked HTTP (no network, no Keychain)."""
import sys, wave, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stt_xai

class Resp:
    def __init__(self, code, js): self.status_code, self._js = code, js
    def json(self): return self._js
class Sess:
    def __init__(self, resp): self.resp, self.calls = resp, []
    def post(self, url, **kw): self.calls.append((url, kw)); return self.resp

pcm = b"\x00\x01" * 16000
s = Sess(Resp(200, {"text": " add bread to the shopping list ", "language": "en", "duration": 1.0}))
assert stt_xai.transcribe_xai(pcm, "k", session=s) == "add bread to the shopping list"
url, kw = s.calls[0]
assert url == "https://api.x.ai/v1/stt" and kw["headers"]["Authorization"] == "Bearer k"
assert ("language", "en") in kw["data"] and ("keyterm", "Womble") in kw["data"] and kw["timeout"] == stt_xai.TIMEOUT
w = wave.open(io.BytesIO(kw["files"]["file"][1])); assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
try: stt_xai.transcribe_xai(pcm, "k", session=Sess(Resp(401, {}))); raise SystemExit("expected error")
except RuntimeError as e: assert str(e) == "http 401"

# C10: a missing key is not latched forever, and the value is never logged.
import logging
FAKE = "xai-test-key-value"
class Grab(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, record): self.lines.append(record.getMessage())
grab = Grab(); logging.getLogger("hub").addHandler(grab)
stt_xai._key = None; stt_xai._checked_at = None
calls = {"n": 0}
def miss(cmd, **kw):
    calls["n"] += 1
    class R: returncode = 1; stdout = ""
    return R()
real_run = stt_xai.subprocess.run
try:
    stt_xai.subprocess.run = miss
    assert stt_xai.get_key() is None and calls["n"] == 1
    assert stt_xai.get_key() is None and calls["n"] == 1   # inside the 5 min window
    stt_xai._checked_at -= stt_xai.KEY_RETRY_S + 1
    assert stt_xai.get_key() is None and calls["n"] == 2
    def hit(cmd, **kw):
        calls["n"] += 1
        class R: returncode = 0; stdout = FAKE + "\n"
        return R()
    stt_xai.subprocess.run = hit
    stt_xai._checked_at -= stt_xai.KEY_RETRY_S + 1
    assert stt_xai.get_key() == FAKE and calls["n"] == 3
    assert stt_xai.get_key() == FAKE and calls["n"] == 3   # cached, no further read
    assert not any(FAKE in line for line in grab.lines), grab.lines
finally:
    stt_xai.subprocess.run = real_run
    stt_xai._key = None; stt_xai._checked_at = None
    logging.getLogger("hub").removeHandler(grab)
print("PASS key recheck (no value logged)")
print("test_stt_xai OK")
