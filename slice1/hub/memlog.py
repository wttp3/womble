"""Opt-in memory logging for the hub (MEM_LOG=1). Stdlib only, no ML imports.

  mem ev=<name> ...   one line per subsystem event (stt, tts, media.refresh, alexa.job, ws.snapshot, ...)
                      with the hub's footprint before -> after, the peak seen while the event ran, and the
                      same for each ML worker process that was alive during it.
  mem idle ...        one line every MEM_LOG_EVERY_S (default 300) with footprints, lifetime peaks and a
                      few container sizes, so slow growth shows up in hub.log.

On macOS the number is phys_footprint (what `footprint <pid>` and Activity Monitor show), read with
proc_pid_rusage. Elsewhere it is VmRSS from /proc. Peaks during an event come from a sampler thread
(MEM_LOG_SAMPLE_S, default 0.2 s) that only runs while an event is open.

With MEM_LOG unset every call here is a no-op.
"""
import ctypes, ctypes.util, logging, os, sys, threading, time
from contextlib import contextmanager

log = logging.getLogger("hub.mem")
ENABLED = os.environ.get("MEM_LOG", "0") == "1"
EVERY_S = float(os.environ.get("MEM_LOG_EVERY_S", "300"))
SAMPLE_S = max(0.02, float(os.environ.get("MEM_LOG_SAMPLE_S", "0.2")))
MB = 1024 * 1024

# struct rusage_info_v4 (sys/resource.h): uint8 ri_uuid[16], then uint64 fields.
_RUSAGE_INFO_V4 = 4
_F_PHYS_FOOTPRINT = 7
_F_LIFETIME_MAX = 28
_libproc = None


def _rusage(pid):
    """uint64 fields of rusage_info_v4 for pid (macOS), or None."""
    global _libproc
    if sys.platform != "darwin":
        return None
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
        buf = (ctypes.c_uint64 * 64)()          # 16-byte uuid = first two slots; the struct is 296 bytes
        if _libproc.proc_pid_rusage(int(pid), _RUSAGE_INFO_V4, ctypes.byref(buf)) != 0:
            return None
        return buf
    except Exception:
        return None


def _proc_status(pid, key):
    try:
        with open(f"/proc/{int(pid)}/status") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def footprint(pid=None):
    """Bytes of memory charged to pid (default: this process), or None if it can't be read."""
    pid = pid or os.getpid()
    r = _rusage(pid)
    if r is not None:
        return int(r[2 + _F_PHYS_FOOTPRINT])
    return _proc_status(pid, "VmRSS")


def lifetime_peak(pid=None):
    """Highest footprint the process has ever had, or None."""
    pid = pid or os.getpid()
    r = _rusage(pid)
    if r is not None:
        return int(r[2 + _F_LIFETIME_MAX])
    return _proc_status(pid, "VmHWM")


def mb(n):
    return "-" if n is None else "%.0f" % (n / MB)


# ---------- other processes and gauges ----------
_pids = {}      # name -> fn() -> pid or None (ML workers)
_gauges = {}    # name -> fn() -> number (container sizes worth watching)


def watch_pid(name, fn):
    _pids[name] = fn


def gauge(name, fn):
    _gauges[name] = fn


def _live_pids():
    out = {}
    for name, fn in list(_pids.items()):
        try:
            pid = fn()
        except Exception:
            pid = None
        if pid:
            out[name] = pid
    return out


# ---------- per-event peaks ----------
class _Event:
    def __init__(self, name):
        self.name = name
        self.t0 = time.perf_counter()
        self.before = footprint()
        self.peak = self.before or 0
        self.others = {}     # worker name -> [first, last, peak]
        self.notes = {}
        self.sample()

    def note(self, **kw):
        self.notes.update(kw)

    def sample(self):
        f = footprint()
        if f and f > self.peak:
            self.peak = f
        for name, pid in _live_pids().items():
            v = footprint(pid)
            if v is None:
                continue
            rec = self.others.setdefault(name, [v, v, v])
            rec[1] = v
            if v > rec[2]:
                rec[2] = v

    def line(self):
        after = footprint()
        parts = ["mem ev=%s ms=%d hub_mb=%s->%s peak=%s" % (
            self.name, (time.perf_counter() - self.t0) * 1000, mb(self.before), mb(after),
            mb(max(self.peak, after or 0)))]
        for name, (first, last, peak) in sorted(self.others.items()):
            parts.append("%s_mb=%s->%s peak=%s" % (name, mb(first), mb(last), mb(peak)))
        parts += ["%s=%s" % kv for kv in self.notes.items()]
        return " ".join(parts)


class _Null:
    def note(self, **kw):
        pass


_NULL = _Null()
_active = set()
_lock = threading.Lock()
_sampler = None


def _sample_loop():
    global _sampler
    while True:
        with _lock:
            evs = list(_active)
            if not evs:
                _sampler = None
                return
        for ev in evs:
            try:
                ev.sample()
            except Exception:
                pass
        time.sleep(SAMPLE_S)


@contextmanager
def event(name):
    """with memlog.event("tts") as ev: ...; ev.note(k=v) adds fields to the line."""
    global _sampler
    if not ENABLED:
        yield _NULL
        return
    ev = _Event(name)
    with _lock:
        _active.add(ev)
        if _sampler is None:
            _sampler = threading.Thread(target=_sample_loop, name="memlog", daemon=True)
            _sampler.start()
    try:
        yield ev
    finally:
        with _lock:
            _active.discard(ev)
        try:
            ev.sample()
            log.info(ev.line())
        except Exception as e:
            log.warning("mem log failed (%s)", type(e).__name__)


def snapshot_line():
    parts = ["mem idle hub_mb=%s hub_max_mb=%s" % (mb(footprint()), mb(lifetime_peak()))]
    live = _live_pids()
    for name in sorted(_pids):
        pid = live.get(name)
        parts.append("%s_mb=%s" % (name, mb(footprint(pid)) if pid else "off"))
    for name, fn in sorted(_gauges.items()):
        try:
            parts.append("%s=%s" % (name, fn()))
        except Exception:
            pass
    return " ".join(parts)


async def periodic():
    """Background task for hub.main(): one `mem idle` line every MEM_LOG_EVERY_S."""
    import asyncio
    if not ENABLED or EVERY_S <= 0:
        return
    log.info("memory logging on (every %.0f s, sample %.2f s)", EVERY_S, SAMPLE_S)
    while True:
        log.info(snapshot_line())
        await asyncio.sleep(EVERY_S)
