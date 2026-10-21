"""Latest-episode resolver for podcast_rss_latest entries (plan M1, section 4 step 5).

Parses at most the first 30 <item>s, skips trailers/bonus episodes and items
without an enclosure, and keeps the enclosure URL exactly as published (never
a resolved dcs-* URL). Results are cached for 15 minutes per feed.

Cross-posts (the Daily Beast posts the same episode in two feeds) are
collapsed: within a feed by ep_key/title, and across entries refresh_all()
marks the later copy with dup_of.
"""
import asyncio, hashlib, logging, re, time, urllib.request
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

log = logging.getLogger("hub.media")
CACHE_S = 900
MAX_ITEMS = 30
MAX_BYTES = 8 * 1024 * 1024
UA = "Womble/1.0 (home media hub)"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def ep_key(guid, enclosure) -> str:
    basis = guid or urlsplit(enclosure or "").path
    return hashlib.sha256((basis or "").encode()).hexdigest()[:16]


def _title_key(t):
    return " ".join(re.findall(r"[a-z0-9]+", (t or "").casefold()))


def _ts(pub):
    try:
        return parsedate_to_datetime(pub).timestamp()
    except (TypeError, ValueError, IndexError):
        return 0.0


def parse_duration(v) -> int:
    """itunes:duration as seconds ("2187", "36:27", "1:02:03"); 0 if unknown."""
    try:
        return sum(int(float(p)) * 60 ** i for i, p in enumerate(reversed((v or "").strip().split(":")))) if v and v.strip() else 0
    except ValueError:
        return 0


def parse_items(xml: bytes, limit=MAX_ITEMS) -> list:
    """First `limit` items as dicts. Stops parsing once enough items are read."""
    items = []
    parser = ET.XMLPullParser(events=("end",))
    step = 64 * 1024
    for i in range(0, len(xml), step):
        parser.feed(xml[i:i + step])
        for _, el in parser.read_events():
            if el.tag != "item":
                continue
            enc = el.find("enclosure")
            items.append({
                "title": (el.findtext("title") or "").strip(),
                "guid": (el.findtext("guid") or "").strip(),
                "pubDate": (el.findtext("pubDate") or "").strip(),
                "episode_type": (el.findtext(ITUNES + "episodeType") or "full").strip().lower(),
                "url": enc.get("url") if enc is not None else None,
                "mime": enc.get("type") if enc is not None else None,
                "duration_s": parse_duration(el.findtext(ITUNES + "duration")),
            })
            el.clear()
            if len(items) >= limit:
                return items
    return items


def pick_latest(items, method=None):
    """Newest playable item for a podcast_rss_latest method, or None."""
    method = method or {}
    skip = {s.lower() for s in method.get("skip_episode_types", ["trailer", "bonus"])}
    prefix = ((method.get("filter") or {}).get("title_prefix") or "").casefold()
    seen, keep = set(), []
    for it in items:
        if not it.get("url") or not it["url"].lower().startswith("https://"):
            continue
        if it.get("episode_type") in skip:
            continue
        if prefix and not it["title"].casefold().startswith(prefix):
            continue
        k = ep_key(it.get("guid"), it["url"])
        tk = _title_key(it["title"])
        if k in seen or (tk and tk in seen):
            continue
        seen.update({k, tk} - {""})
        keep.append(it)
    if not keep:
        return None
    best = max(keep, key=lambda it: _ts(it["pubDate"]))
    return {"ep_key": ep_key(best.get("guid"), best["url"]), "url": best["url"],
            "title": best["title"], "pubDate": best["pubDate"], "ts": _ts(best["pubDate"])}


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:   # follows redirects
        return r.read(MAX_BYTES)


class Resolver:
    def __init__(self, library, fetch=None, cache_s=CACHE_S, clock=time.monotonic):
        self.lib = library
        self.fetch = fetch or _http_get     # sync callable(url) -> bytes; tests pass fixtures
        self.cache_s = cache_s
        self.clock = clock
        self._cache = {}                    # feed -> (t, items)

    def _items(self, feed):
        hit = self._cache.get(feed)
        if hit and self.clock() - hit[0] < self.cache_s:
            return hit[1]
        items = parse_items(self.fetch(feed))
        self._cache[feed] = (self.clock(), items)
        return items

    def latest(self, entry):
        for m in entry.get("methods", []):
            if m.get("kind") == "podcast_rss_latest" and m.get("feed"):
                return pick_latest(self._items(m["feed"]), m)
        return None

    def refresh_all(self) -> dict:
        """{entry_id: latest} for every enabled podcast. A failing feed is logged and skipped."""
        out, owner = {}, {}
        for e in self.lib.entries("podcast"):
            try:
                got = self.latest(e)
            except Exception as ex:
                log.warning("media feed %s failed (%s)", e["id"], type(ex).__name__)
                continue
            if not got:
                continue
            tk = _title_key(got["title"])
            for k in (got["ep_key"], tk):
                if k and k in owner:
                    got["dup_of"] = owner[k]
                    break
            else:
                owner.update({got["ep_key"]: e["id"], tk: e["id"]})
            out[e["id"]] = got
        return out

    async def refresh_all_async(self) -> dict:
        return await asyncio.to_thread(self.refresh_all)
