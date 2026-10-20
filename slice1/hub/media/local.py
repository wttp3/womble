"""Per-house media config: data/media_local.json (gitignored).

Shape: {"triggers": {"lbc_kitchen": "<vm-trigger-id>", ...},
        "targets": [{"room": "kitchen", "name": "kitchen pair", "target_kind": "stereo_pair",
                     "device_ref": "<white-device-id>,<blue-device-id>"}]}
Nothing from this file is logged or sent to the brain.
"""
import json, logging
from pathlib import Path

log = logging.getLogger("hub.media")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "media_local.json"
EMPTY = {"triggers": {}, "targets": []}


def load(path=DEFAULT_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {k: type(v)() for k, v in EMPTY.items()}
    try:
        d = json.loads(p.read_text())
    except (ValueError, OSError) as e:
        log.warning("media_local.json unreadable (%s); using empty config", type(e).__name__)
        return {k: type(v)() for k, v in EMPTY.items()}
    return {"triggers": dict(d.get("triggers") or {}), "targets": list(d.get("targets") or [])}


def trigger(cfg: dict, ref: str):
    """Resolve a seed trigger_ref to the private id, or None when not configured."""
    return (cfg.get("triggers") or {}).get(ref)


def sync_targets(lib, cfg: dict) -> int:
    """Copy configured targets into the library's targets table (replace all)."""
    rows = [(t["room"], t["name"], t["target_kind"], t.get("device_ref")) for t in cfg.get("targets") or []
            if t.get("room") and t.get("name") and t.get("target_kind")]
    with lib._lock, lib.db:
        lib.db.execute("DELETE FROM targets")
        lib.db.executemany("INSERT INTO targets(room,name,target_kind,device_ref) VALUES(?,?,?,?)", rows)
    return len(rows)
