"""xAI grok-voice-transcribe-2.0 batch STT (REST POST /v1/stt).
Key comes from macOS Keychain (service womble-xai, account decoy) at runtime; never logged.
A missing or unreadable key is re-read every 5 minutes instead of staying off until restart."""
import io, logging, os, subprocess, time, wave
import requests

log = logging.getLogger("hub")
URL = "https://api.x.ai/v1/stt"
KC_SERVICE, KC_ACCOUNT = "womble-xai", "decoy"
KEYTERMS = ["milk", "bread", "biscuits", "eggs", "Womble", "Vic", "Andrew", "shopping", "list", "Frida"]
TIMEOUT = float(os.environ.get("XAI_STT_TIMEOUT", "5"))
KEY_RETRY_S = 5 * 60
_key = None
_checked_at = None   # time.time() of the last failed read; None = never tried

def get_key():
    """Return the key, or None. Cached once found. While missing, re-check every KEY_RETRY_S.
    The value is never logged."""
    global _key, _checked_at
    if _key:
        return _key
    now = time.time()
    if _checked_at is not None and now - _checked_at < KEY_RETRY_S:
        return None
    _checked_at = now
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w"],
                           capture_output=True, text=True, timeout=5)
        _key = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        _key = None
    if _key:
        log.info("xai stt enabled")
    else:
        log.warning("xai stt disabled (no Keychain item womble-xai); will recheck in 5 min")
    return _key

def pcm_to_wav(pcm: bytes, rate=16000) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    return b.getvalue()

def transcribe_xai(pcm: bytes, key: str, session=requests) -> str:
    data = [("language", "en")] + [("keyterm", k) for k in KEYTERMS]
    r = session.post(URL, headers={"Authorization": f"Bearer {key}"}, data=data,
                     files={"file": ("utt.wav", pcm_to_wav(pcm), "audio/wav")}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"http {r.status_code}")
    return (r.json().get("text") or "").strip()
