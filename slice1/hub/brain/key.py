"""xAI key for the brain. Separate from speech-to-text (womble-xai).

Keychain generic password: service womble-brain-xai, account $USER.
An optional XAI_BRAIN_API_KEY env override is for the latency script.
The value is never logged.
"""
import logging, os, subprocess, time

log = logging.getLogger("hub.brain")
KC_SERVICE = "womble-brain-xai"
KEY_RETRY_S = 5 * 60
_key = None
_checked_at = None


def account() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def get_key():
    """Return the brain key, or None. Env override wins and is not cached.
    A missing Keychain item is re-read every KEY_RETRY_S. The value is never logged."""
    global _key, _checked_at
    env = os.environ.get("XAI_BRAIN_API_KEY", "").strip()
    if env:
        return env
    if _key:
        return _key
    now = time.time()
    if _checked_at is not None and now - _checked_at < KEY_RETRY_S:
        return None
    _checked_at = now
    who = account()
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KC_SERVICE, "-a", who, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        _key = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        _key = None
    if _key:
        log.info("xai brain enabled")
    else:
        log.warning("xai brain disabled (no Keychain item %s); will recheck in 5 min", KC_SERVICE)
    return _key
