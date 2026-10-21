"""Voice Monkey routine triggers (media plan §5, M3).

The token is Keychain item womble-voicemonkey (read per call, never logged).
Trigger ids are per house: data/media_local.json "triggers" maps a seed
trigger_ref (lbc_kitchen, frida_white, am_preset_1) to the Voice Monkey device id.
Every call is counted in the plays table so the monthly quota is visible.
"""
import asyncio, json, logging, subprocess, time, urllib.request

log = logging.getLogger("hub.media")
KC_SERVICE = "womble-voicemonkey"
DEFAULT_API = "https://api-v3.voicemonkey.io/trigger"
TIMEOUT_S = 5.0
FREE_QUOTA = 200


def read_token():
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Womble/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(2048)


class VoiceMonkey:
    def __init__(self, cfg, lib=None, token=read_token, post=_post, quota=FREE_QUOTA):
        self.cfg, self.lib, self.token, self.post, self.quota = cfg, lib, token, post, quota

    def configured(self, ref) -> bool:
        return bool(ref and (self.cfg.get("triggers") or {}).get(ref))

    def month_count(self) -> int:
        if self.lib is None:
            return 0
        start = time.strftime("%Y-%m-01")
        with self.lib._lock:
            return self.lib.db.execute(
                "SELECT COUNT(*) FROM plays WHERE method LIKE 'vm:%' AND started_at >= ?", (start,)).fetchone()[0]

    async def fire(self, ref, entry_id=None, room=None, detail=None) -> bool:
        device = (self.cfg.get("triggers") or {}).get(ref)
        if not device:
            log.info("media native ref=%s not configured", ref)
            return False
        token = await asyncio.to_thread(self.token)
        if not token:
            log.warning("media native: Keychain %s missing", KC_SERVICE)
            return False
        url = self.cfg.get("vm_api") or DEFAULT_API
        ok, status = False, "error"
        try:
            code, _ = await asyncio.wait_for(
                asyncio.to_thread(self.post, url, {"token": token, "device": device}, TIMEOUT_S), TIMEOUT_S + 1)
            ok = 200 <= code < 300
            status = "ok" if ok else f"http {code}"
        except Exception as e:
            status = type(e).__name__
        if self.lib is not None:
            self.lib.log_play(entry_id, room, ref, f"vm:{ref}", "started" if ok else "failed", detail or status)
        n = self.month_count()
        log.info("media native ref=%s status=%s month_requests=%d", ref, status, n)
        return ok

    def quota_warning(self):
        n = self.month_count()
        return n >= 0.8 * self.quota, n
