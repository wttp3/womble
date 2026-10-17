"""Shopping / to-do list store. No ML imports.

File shape (unchanged): {"<list>": [{"item": str, "ts": epoch_float}]}.
Writes are atomic (temp file, flush, fsync, os.replace). A bad or empty file
is renamed aside and the store starts empty.
"""
import json, logging, os, time
from pathlib import Path

log = logging.getLogger("hub")
DEFAULT_FILE = Path(__file__).resolve().parent / "data" / "lists.json"


class ListStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_FILE

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text()
            if not raw.strip():
                raise json.JSONDecodeError("empty file", raw, 0)
            return json.loads(raw)
        except json.JSONDecodeError as e:
            bad = self.path.with_name(f"lists.corrupt-{int(time.time())}.json")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            log.warning("lists.json unreadable (%s); moved to %s, starting empty", e.msg, bad.name)
            return {}

    def _save(self, lists: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(lists, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def add(self, item: str, lst: str) -> list:
        lists = self.load()
        lists.setdefault(lst, []).append({"item": item, "ts": time.time()})
        self._save(lists)
        return [i["item"] for i in lists[lst]]

    def show(self, lst: str) -> list:
        return [i["item"] for i in self.load().get(lst, [])]
