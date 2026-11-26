"""ElevenLabs streaming TTS (TTS_ENGINE=elevenlabs). aiohttp only; no ML imports.

POST {base}/v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000 with model_id and
apply_text_normalization. The reply is raw 16-bit 24 kHz mono PCM; synth() returns it as bytes
and hub.synthesize() resamples it like Kokoro's output.

Key: macOS Keychain generic password, service womble-elevenlabs, account $USER, read the same
way brain/key.py reads womble-brain-xai. Cached in memory, never logged. ELEVENLABS_API_KEY is
an override for tests.

Failures raise ElevenError whose text is the status code and ElevenLabs' error code only (never
the response body or the key). 401/402/403/404 start a cooldown (ELEVENLABS_COOLDOWN_S, 600)
during which available() is False, so a dead key or an empty balance isn't retried on every
reply. 429 is retried once after a short pause.

Env: ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL (eleven_flash_v2_5), ELEVENLABS_TEXT_NORMALIZATION (on),
ELEVENLABS_CONNECT_TIMEOUT_S (3), ELEVENLABS_TIMEOUT_S (15), ELEVENLABS_COOLDOWN_S (600),
ELEVENLABS_BASE_URL (tests).
"""
import asyncio, atexit, logging, os, re, subprocess, threading, time

import aiohttp

log = logging.getLogger("hub.tts")
KC_SERVICE = "womble-elevenlabs"
KEY_RETRY_S = 5 * 60
SAMPLE_RATE = 24000
OUTPUT_FORMAT = "pcm_24000"
DEFAULT_VOICE = "rhS7yjXTU4uIlRxXhNW7"     # Charlotte (public library voice id)
DEFAULT_MODEL = "eleven_flash_v2_5"
RETRY_429_S = 0.4
MAX_PCM_BYTES = 16 * 1024 * 1024           # ~5.5 minutes of audio; a reply is never that long
COOLDOWN_STATUSES = (401, 402, 403, 404)
_CODE = re.compile(r"[^a-z0-9_.-]")

_key = None
_checked_at = None
_cooldown_until = 0.0
_normalization_ok = True                   # cleared if the API rejects apply_text_normalization for this model/plan


class ElevenError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


def _f(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def base_url() -> str:
    return (os.environ.get("ELEVENLABS_BASE_URL") or "https://api.elevenlabs.io").rstrip("/")


def account() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def get_key():
    """Return the key, or None. Env override wins and is not cached.
    A missing Keychain item is re-read every KEY_RETRY_S. The value is never logged."""
    global _key, _checked_at
    env = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if env:
        return env
    if _key:
        return _key
    now = time.time()
    if _checked_at is not None and now - _checked_at < KEY_RETRY_S:
        return None
    _checked_at = now
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KC_SERVICE, "-a", account(), "-w"],
            capture_output=True, text=True, timeout=5,
        )
        _key = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        _key = None
    if _key:
        log.info("elevenlabs tts enabled")
    else:
        log.warning("elevenlabs tts disabled (no Keychain item %s); will recheck in 5 min", KC_SERVICE)
    return _key


def cooling(now: float | None = None) -> bool:
    return (now or time.time()) < _cooldown_until


def available() -> bool:
    """False while cooling down after an auth/credit error, or when there is no key."""
    return not cooling() and bool(get_key())


def _start_cooldown(status):
    global _cooldown_until
    secs = _f("ELEVENLABS_COOLDOWN_S", 600)
    _cooldown_until = time.time() + secs
    log.warning("elevenlabs off for %.0f s after http %s", secs, status)


def reset():
    """Forget the cooldown and cached key (tests)."""
    global _cooldown_until, _key, _checked_at, _normalization_ok
    _cooldown_until = 0.0; _key = None; _checked_at = None; _normalization_ok = True


def _error_code(body) -> str:
    """ElevenLabs' short error code from an error body ({"detail": {"status": ...}}). Never the message."""
    code = ""
    if isinstance(body, dict):
        d = body.get("detail")
        if isinstance(d, dict):
            code = d.get("status") or d.get("code") or ""
        if not code:
            code = body.get("status") or body.get("code") or ""
    return _CODE.sub("", str(code).lower())[:40]


# ---------- one background event loop, so the HTTPS connection is reused between replies ----------
_loop = None
_loop_lock = threading.Lock()
_session = None


def _get_loop():
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="elevenlabs", daemon=True).start()
        return _loop


async def _get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def _post(key, text, voice, model, normalize):
    """(status, pcm bytes or b"", error code)."""
    timeout = aiohttp.ClientTimeout(total=_f("ELEVENLABS_TIMEOUT_S", 15),
                                    connect=_f("ELEVENLABS_CONNECT_TIMEOUT_S", 3),
                                    sock_connect=_f("ELEVENLABS_CONNECT_TIMEOUT_S", 3))
    body = {"text": text, "model_id": model}
    if normalize:
        body["apply_text_normalization"] = normalize
    s = await _get_session()
    async with s.post(f"{base_url()}/v1/text-to-speech/{voice}/stream", params={"output_format": OUTPUT_FORMAT},
                      json=body, headers={"xi-api-key": key, "Accept": "application/octet-stream"},
                      timeout=timeout) as r:
        if r.status != 200:
            try:
                err = _error_code(await r.json(content_type=None))
            except Exception:
                err = ""
            return r.status, b"", err
        buf = bytearray()
        async for chunk in r.content.iter_chunked(32 * 1024):
            buf += chunk
            if len(buf) > MAX_PCM_BYTES:
                break
        return 200, bytes(buf[:len(buf) // 2 * 2]), ""


async def _synth(key, text):
    global _normalization_ok
    voice = os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE
    model = os.environ.get("ELEVENLABS_MODEL") or DEFAULT_MODEL
    norm = os.environ.get("ELEVENLABS_TEXT_NORMALIZATION", "on").strip().lower()
    normalize = norm if _normalization_ok and norm in ("on", "off") else None
    retried_429 = False
    while True:
        status, pcm, err = await _post(key, text, voice, model, normalize)
        if status == 200:
            return pcm
        if status == 429 and not retried_429:
            retried_429 = True
            await asyncio.sleep(RETRY_429_S)
            continue
        if status in (400, 422) and normalize == "on":
            # Some model/plan pairs refuse apply_text_normalization=on. Send without it from now on.
            log.warning("elevenlabs refused text normalization (http %s %s); retrying without it", status, err or "-")
            _normalization_ok = False; normalize = None
            continue
        raise ElevenError(f"http {status}" + (f" {err}" if err else ""), status)


@atexit.register
def close():
    loop, s = _loop, _session
    if loop is None or loop.is_closed() or s is None or s.closed:
        return
    try:
        asyncio.run_coroutine_threadsafe(s.close(), loop).result(timeout=2)
    except Exception:
        pass


def synth(text: str) -> bytes:
    """Blocking. Raw s16le mono PCM at SAMPLE_RATE, or ElevenError."""
    if cooling():
        raise ElevenError("cooldown")
    key = get_key()
    if not key:
        raise ElevenError("no key")
    fut = asyncio.run_coroutine_threadsafe(_synth(key, text), _get_loop())
    try:
        pcm = fut.result(timeout=_f("ELEVENLABS_TIMEOUT_S", 15) * 2 + 5)
    except ElevenError as e:
        if e.status in COOLDOWN_STATUSES:
            _start_cooldown(e.status)
        raise
    except (asyncio.TimeoutError, TimeoutError):
        fut.cancel()
        raise ElevenError("timeout") from None
    except aiohttp.ClientError as e:
        raise ElevenError(f"network {type(e).__name__}") from None
    except Exception as e:
        raise ElevenError(f"error {type(e).__name__}") from None
    if len(pcm) < SAMPLE_RATE // 10 * 2:
        raise ElevenError("empty audio")
    return pcm
