"""Hub side of ml_worker.py: start a model process on demand, stop it when idle.

    tts = ml_client.make("tts", idle_s=300)
    tts.prewarm("tts")                        # non-blocking; call at the wake word
    hdr, audio = tts.request("tts", {"text": "Hello."})

One request runs at a time per worker. After `idle_s` seconds without a request the process
exits and the OS gets all of its memory back; the next request (or prewarm) starts a new one.
idle_s <= 0 keeps it running. A worker that died is restarted on the next request.

ML_WORKER=0 runs the same Engine inside the hub process instead (the old layout). Idle
unload then only drops the model references and clears the MLX cache.
"""
import atexit, logging, os, subprocess, sys, threading, time
from pathlib import Path

from ml_worker import Engine, read_frame, write_frame

log = logging.getLogger("hub.ml")
WORKER = Path(__file__).with_name("ml_worker.py")
# The worker only needs model settings. Don't hand it the hub's credentials.
_KEEP_FROM_WORKER = ("BOX3_NOISE_PSK", "HUB_LINK_TOKEN", "DISPLAY_TOKEN", "XAI_BRAIN_API_KEY", "ELEVENLABS_API_KEY")
_all = []


class WorkerError(RuntimeError):
    pass


def hf_cache_dir() -> Path:
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            return Path(os.environ[var])
    home = os.environ.get("HF_HOME")
    return (Path(home) if home else Path.home() / ".cache" / "huggingface") / "hub"


def hf_cached(repo: str, needs=()) -> bool:
    """True if a snapshot of `repo` is in the local Hugging Face cache and holds a match for each glob in `needs`."""
    if not repo or repo.count("/") != 1 or repo.startswith(("/", ".", "~")):
        return False
    try:
        snaps = [d for d in (hf_cache_dir() / ("models--" + repo.replace("/", "--")) / "snapshots").iterdir() if d.is_dir()]
        return any(any(d.iterdir()) and all(any(d.glob(g)) for g in needs) for d in snaps)
    except OSError:
        return False


class _Base:
    def __init__(self, name, idle_s=300.0, timeout=120.0):
        self.name, self.idle_s, self.timeout = name, float(idle_s), float(timeout)
        self._lock = threading.Lock()      # one request at a time
        self._idle_timer = None
        self._last = time.monotonic()
        self._warm = set()
        self._warming = False
        self.starts = 0
        _all.append(self)

    # subclasses
    def alive(self) -> bool: raise NotImplementedError
    def pid(self): return None
    def _call(self, op, header, payload, timeout): raise NotImplementedError
    def _release(self, reason): raise NotImplementedError

    def request(self, op, header=None, payload=b"", timeout=None):
        """Blocking. Returns (reply header, payload bytes) or raises WorkerError."""
        with self._lock:
            self._cancel_idle()
            try:
                hdr, data = self._call(op, dict(header or {}), payload, timeout or self.timeout)
                self._warm = set(hdr.get("warm") or [])
                return hdr, data
            finally:
                self._last = time.monotonic()
                self._arm_idle()

    def prewarm(self, *what, **header):
        """Start loading in the background. Returns at once; a request made meanwhile queues behind it."""
        if self._warming:
            return
        if self.alive() and set(what) <= self._warm:
            self.touch()
            return
        self._warming = True
        def run():
            try:
                hdr, _ = self.request("warm", {"what": list(what), **header})
                log.info("ml %s warm in %s ms", self.name, hdr.get("ms"))
            except Exception as e:
                log.warning("ml %s warm failed (%s)", self.name, e)
            finally:
                self._warming = False
        threading.Thread(target=run, name=f"ml-{self.name}-warm", daemon=True).start()

    def touch(self):
        """Restart the idle countdown (a wake word arrived; a request is about to follow)."""
        if self._lock.acquire(blocking=False):
            try:
                self._cancel_idle()
                self._last = time.monotonic()
                self._arm_idle()
            finally:
                self._lock.release()

    def stop(self, reason="stop"):
        with self._lock:
            self._cancel_idle()
            if self.alive():
                self._release(reason)

    # ---- idle ----
    def _cancel_idle(self):
        t, self._idle_timer = self._idle_timer, None
        if t is not None:
            t.cancel()

    def _arm_idle(self):
        if self.idle_s > 0 and self.alive():
            t = threading.Timer(self.idle_s, self._idle_exit)
            t.daemon = True
            t.start()
            self._idle_timer = t

    def _idle_exit(self):
        if not self._lock.acquire(blocking=False):
            return                      # busy; the running request re-arms the timer
        try:
            if self.alive() and time.monotonic() - self._last >= self.idle_s - 0.05:
                self._release("idle")
        finally:
            self._lock.release()


class MLWorker(_Base):
    """Models in a child process."""

    def __init__(self, name, idle_s=300.0, timeout=120.0, env=None):
        super().__init__(name, idle_s, timeout)
        self.env = env or {}          # dict, or a callable returning one (evaluated at each spawn)
        self.spawn_env = {}           # what the running worker was started with
        self._proc = None
        self._born = 0.0

    def alive(self):
        p = self._proc
        return p is not None and p.poll() is None

    def pid(self):
        return self._proc.pid if self.alive() else None

    def _spawn(self):
        env = {k: v for k, v in os.environ.items() if k not in _KEEP_FROM_WORKER}
        self.spawn_env = dict(self.env() if callable(self.env) else self.env)
        env.update(self.spawn_env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._proc = subprocess.Popen([sys.executable, str(WORKER), self.name],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self._born = time.monotonic()
        self._warm = set()
        self.starts += 1
        log.info("ml %s worker started pid=%d", self.name, self._proc.pid)

    def _reap(self):
        p, self._proc = self._proc, None
        if p is None:
            return None
        for f in (p.stdin, p.stdout):
            try: f.close()
            except Exception: pass
        try:
            return p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            return p.wait()

    @staticmethod
    def _kill(p, fired):
        fired.append(True)
        try: p.kill()
        except Exception: pass

    def _call(self, op, header, payload, timeout):
        for attempt in (0, 1):
            fresh = not self.alive()
            if fresh:
                self._reap()
                self._spawn()
            p = self._proc
            fired = []
            dog = threading.Timer(timeout, self._kill, args=(p, fired))   # a hung model run can't block the hub for ever
            dog.daemon = True
            dog.start()
            try:
                write_frame(p.stdin, {"op": op, **header}, payload)
                got = read_frame(p.stdout)
            except (OSError, ValueError):
                got = None
            finally:
                dog.cancel()
            if got is not None:
                hdr, data = got
                if not hdr.get("ok"):
                    self._warm = set(hdr.get("warm") or [])
                    raise WorkerError(hdr.get("error") or f"{self.name} worker error")
                return hdr, data
            rc = self._reap()
            log.warning("ml %s worker %s during %s (rc=%s)", self.name, "timed out" if fired else "died", op, rc)
            if fresh or attempt or fired:   # retry once, and only when an older worker had gone away
                raise WorkerError(f"{self.name} worker {'timed out' if fired else 'died'} during {op} (rc={rc})")
        raise WorkerError(f"{self.name} worker unavailable")

    def _release(self, reason):
        age = time.monotonic() - self._born
        rc = self._reap()       # closing stdin is the exit signal; killed if it lingers
        self._warm = set()
        log.info("ml %s worker exited (%s) after %.0f s rc=%s", self.name, reason, age, rc)


class InProcess(_Base):
    """ML_WORKER=0: the models live in the hub process, loaded on first use."""

    def __init__(self, name, idle_s=300.0, timeout=120.0, env=None):
        super().__init__(name, idle_s, timeout)
        self.engine = Engine()

    def alive(self):
        return bool(self.engine.loaded())

    def _call(self, op, header, payload, timeout):
        try:
            return self.engine.handle(op, header, payload)
        except Exception as e:
            raise WorkerError(f"{type(e).__name__}: {e}") from e

    def _release(self, reason):
        self.engine.unload()
        self._warm = set()
        log.info("ml %s unloaded in-process (%s)", self.name, reason)


def make(name, idle_s=300.0, timeout=120.0, env=None):
    cls = InProcess if os.environ.get("ML_WORKER", "1") == "0" else MLWorker
    return cls(name, idle_s=idle_s, timeout=timeout, env=env)


@atexit.register
def stop_all():
    for w in list(_all):
        try:
            if w._lock.acquire(timeout=1):
                try:
                    w._cancel_idle()
                    if w.alive(): w._release("hub exit")
                finally:
                    w._lock.release()
            elif isinstance(w, MLWorker) and w._proc is not None:
                w._proc.kill()
        except Exception:
            pass
