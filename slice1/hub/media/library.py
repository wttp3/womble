"""Media library store (plan M1, section 2).

SQLite at data/media.sqlite (gitignored), seeded from the tracked
media/library.seed.json, which holds public publisher URLs only. Per-house
values (Voice Monkey trigger ids, Echo device ids) stay in data/media_local.json.

Stdlib only: sqlite3 for the store, difflib for the fuzzy match.
"""
import json, os, re, sqlite3, threading, time
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED_PATH = HERE / "library.seed.json"
DEFAULT_DB = HERE.parent / "data" / "media.sqlite"
TYPES = ("radio", "podcast", "music_preset")
FUZZY_MIN = 0.85

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
  type TEXT NOT NULL, methods TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
  legal_note TEXT, added_by TEXT, verified_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS targets(
  room TEXT NOT NULL, name TEXT NOT NULL, target_kind TEXT NOT NULL, device_ref TEXT,
  PRIMARY KEY(room, name));
CREATE TABLE IF NOT EXISTS plays(
  id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT, room TEXT, target TEXT, method TEXT,
  started_at TEXT, ended_at TEXT, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS resume_mirror(
  ep_key TEXT PRIMARY KEY, entry_id TEXT, offset_ms INTEGER, updated_at TEXT);
"""

_STOP = {"the", "podcast", "radio", "a", "show", "station"}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def normalise(s: str) -> str:
    s = (s or "").casefold().replace("&", " and ").replace("'", "").replace("\u2019", "")
    toks = re.findall(r"[a-z0-9]+", s)
    kept = [t for t in toks if t not in _STOP]
    return " ".join(kept or toks)


def token_set_ratio(a: str, b: str) -> float:
    """rapidfuzz-style token_set_ratio on 0..1, built on difflib."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    inter = " ".join(sorted(ta & tb))
    ra = " ".join(sorted(ta - tb)); rb = " ".join(sorted(tb - ta))
    c1 = (inter + " " + ra).strip(); c2 = (inter + " " + rb).strip()
    r = lambda x, y: SequenceMatcher(None, x, y).ratio()
    return max(r(inter, c1) if inter else 0.0, r(inter, c2) if inter else 0.0, r(c1, c2))


class Entry(dict):
    """An entries row with aliases/methods decoded."""

    @property
    def names(self):
        return [self["name"], *self["aliases"]]


class Library:
    def __init__(self, path=None, seed=SEED_PATH):
        self.path = str(path or os.environ.get("MEDIA_DB") or DEFAULT_DB)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock, self.db:
            self.db.executescript(SCHEMA)
        if seed:
            self.import_seed(seed)

    # ---------- seed ----------
    def import_seed(self, seed=SEED_PATH) -> int:
        """Upsert seed entries. Idempotent: re-running changes nothing.

        Seed-owned fields (name, aliases, type, methods, legal_note) follow the
        file; enabled, created_at and verified_at keep their stored values.
        """
        data = json.loads(Path(seed).read_text()) if not isinstance(seed, dict) else seed
        n = 0
        with self._lock, self.db:
            for e in data.get("entries", []):
                if e.get("type") not in TYPES:
                    raise ValueError(f"bad type for {e.get('id')}")
                self.db.execute(
                    "INSERT INTO entries(id,name,aliases,type,methods,enabled,legal_note,added_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,'seed',?)"
                    " ON CONFLICT(id) DO UPDATE SET name=excluded.name, aliases=excluded.aliases,"
                    " type=excluded.type, methods=excluded.methods, legal_note=excluded.legal_note",
                    (e["id"], e["name"], json.dumps(e.get("aliases", [])), e["type"],
                     json.dumps(e.get("methods", [])), 1 if e.get("enabled", True) else 0,
                     e.get("legal_note"), _now()))
                n += 1
        return n

    # ---------- reads ----------
    def _row(self, r) -> Entry:
        d = Entry(dict(r))
        d["aliases"] = json.loads(d["aliases"] or "[]")
        d["methods"] = json.loads(d["methods"] or "[]")
        d["enabled"] = bool(d["enabled"])
        return d

    def entries(self, type=None, include_disabled=False) -> list:
        q = "SELECT * FROM entries"
        cond, args = [], []
        if not include_disabled:
            cond.append("enabled=1")
        if type:
            cond.append("type=?"); args.append(type)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY type, name", args).fetchall()
        return [self._row(r) for r in rows]

    def get(self, entry_id):
        with self._lock:
            r = self.db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return self._row(r) if r else None

    def count(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    # ---------- matching ----------
    def exact(self, name):
        """Exact name or alias (case- and punctuation-insensitive). Fast path."""
        want = (name or "").casefold().strip(" .!?")
        nwant = normalise(name)
        for e in self.entries():
            for n in e.names:
                if n.casefold() == want:
                    return e
        for e in self.entries():
            for n in e.names:
                if normalise(n) == nwant and nwant:
                    return e
        return None

    def candidates(self, name, n=3) -> list:
        """[(entry, score)] best first, by normalised token-set ratio."""
        q = normalise(name)
        if not q:
            return []
        scored = []
        for e in self.entries():
            best = max(token_set_ratio(q, normalise(x)) for x in e.names)
            scored.append((e, best))
        scored.sort(key=lambda t: -t[1])
        return scored[:n]

    def match(self, name):
        """(entry, score) or None. Exact first, then fuzzy >= 0.85 with a clear winner."""
        e = self.exact(name)
        if e:
            return e, 1.0
        cands = self.candidates(name, 2)
        if not cands or cands[0][1] < FUZZY_MIN:
            return None
        if len(cands) > 1 and cands[1][1] >= FUZZY_MIN and cands[1][1] == cands[0][1]:
            return None   # a tie: let the brain choose between them
        return cands[0]

    # ---------- writes ----------
    def log_play(self, entry_id, room, target, method, status, detail=None) -> int:
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT INTO plays(entry_id,room,target,method,started_at,status,detail) VALUES(?,?,?,?,?,?,?)",
                (entry_id, room, target, method, _now(), status, (detail or "")[:200]))
            return cur.lastrowid

    def add_entry(self, e, added_by="voice") -> str:
        """Insert a confirmed directory entry (M5). Returns its id."""
        if e.get("type") not in TYPES:
            raise ValueError("bad type")
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO entries(id,name,aliases,type,methods,enabled,added_by,created_at,verified_at)"
                " VALUES(?,?,?,?,?,1,?,?,?)",
                (e["id"], e["name"], json.dumps(e.get("aliases", [])), e["type"], json.dumps(e.get("methods", [])),
                 added_by, _now(), _now()))
        return e["id"]

    def set_enabled(self, entry_id, on: bool) -> bool:
        """Soft delete / restore. True when the row exists."""
        with self._lock, self.db:
            cur = self.db.execute("UPDATE entries SET enabled=? WHERE id=?", (1 if on else 0, entry_id))
        return cur.rowcount > 0

    def ids(self) -> set:
        with self._lock:
            return {r[0] for r in self.db.execute("SELECT id FROM entries").fetchall()}

    def mirror_resume(self, ep_key, entry_id, offset_ms):
        with self._lock, self.db:
            if offset_ms:
                self.db.execute("INSERT INTO resume_mirror(ep_key,entry_id,offset_ms,updated_at) VALUES(?,?,?,?)"
                                " ON CONFLICT(ep_key) DO UPDATE SET offset_ms=excluded.offset_ms, updated_at=excluded.updated_at",
                                (ep_key, entry_id, int(offset_ms), _now()))
            else:
                self.db.execute("DELETE FROM resume_mirror WHERE ep_key=?", (ep_key,))

    def snapshot(self) -> list:
        """Enabled entries with their methods, for the worker (DO) snapshot."""
        return [{"id": e["id"], "name": e["name"], "aliases": e["aliases"], "type": e["type"],
                 "methods": e["methods"]} for e in self.entries()]

    # ---------- summary ----------
    def summary(self, type=None) -> str:
        es = self.entries(type)
        if not es:
            return "The media library is empty." if not type else f"There are no {_plural(type, 2)} in the library."
        by = {t: [e for e in es if e["type"] == t] for t in TYPES}
        parts = [f"{_num(len(v))} {_plural(t, len(v))}" for t, v in by.items() if v]
        head = _join(parts)
        examples = [e["name"] for t in TYPES for e in by[t]][:4]
        return f"{head[0].upper()}{head[1:]}, including {_join(examples)}."

    def grid(self) -> dict:
        """Frame payload for media.library (no URLs, no per-house ids)."""
        out = {t: [] for t in TYPES}
        for e in self.entries():
            out[e["type"]].append({"id": e["id"], "name": e["name"], "legal_note": e.get("legal_note"),
                                   "verified_at": e.get("verified_at")})
        return out


_LABEL = {"radio": ("radio station", "radio stations"), "podcast": ("podcast", "podcasts"),
          "music_preset": ("music preset", "music presets")}
_WORDS = "zero one two three four five six seven eight nine ten eleven twelve".split()


def _plural(t, n):
    s, p = _LABEL.get(t, (t, t + "s"))
    return s if n == 1 else p


def _num(n):
    return _WORDS[n] if n < len(_WORDS) else str(n)


def _join(xs):
    xs = list(xs)
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + " and " + xs[-1]
