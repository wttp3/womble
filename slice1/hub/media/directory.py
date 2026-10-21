"""Directory search + probe for add_to_library (plan M5, section 3).

Podcasts: Podcast Index /search/byterm (Keychain womble-podcastindex = key,
womble-podcastindex-secret = secret), falling back to the keyless iTunes
Search API. Radio: radio-browser.info /json/stations/search.

Each candidate is probed before it is offered:
  podcast: the feed parses, the latest enclosure is HTTPS, and a range GET
           answers 206/200 with an audio type;
  radio:   the stream URL is HTTPS and answers 200 with an audio type.
Only candidates with probe ok can be added. Nothing is written here.

Stdlib only (urllib in a thread). Secrets are read inside the process and
never logged.
"""
import asyncio, hashlib, json, logging, re, socket, subprocess, time, urllib.parse, urllib.request

from .resolver import UA, parse_items, pick_latest

log = logging.getLogger("hub.media")
TIMEOUT = 6
FEED_BYTES = 2 * 1024 * 1024
PI_API = "https://api.podcastindex.org/api/1.0/search/byterm"
ITUNES_API = "https://itunes.apple.com/search"
RB_FALLBACK = "de1.api.radio-browser.info"
AUDIO_RE = re.compile(r"^(audio/|application/(octet-stream|ogg|x-mpegurl|vnd\.apple\.mpegurl))", re.I)


def keychain(name):
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", name, "-w"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _open(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, method=method)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def http_json(url, headers=None):
    with _open(url, headers) as r:
        return json.loads(r.read(2 * 1024 * 1024))


def http_bytes(url):
    with _open(url) as r:
        return r.read(FEED_BYTES)


def http_head(url, rng=False):
    """(status, content-type, final url) without reading the body."""
    with _open(url, {"Range": "bytes=0-1"} if rng else None) as r:
        return r.status, r.headers.get("Content-Type", ""), r.geturl()


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.casefold()).strip("_")[:40] or "item"


class Directory:
    def __init__(self, get_json=http_json, get_bytes=http_bytes, head=http_head, secret=keychain, clock=time.time):
        self.get_json, self.get_bytes, self.head, self.secret, self.clock = get_json, get_bytes, head, secret, clock

    # ---------- search ----------
    def _podcastindex(self, q, n):
        key, sec = self.secret("womble-podcastindex"), self.secret("womble-podcastindex-secret")
        if not key or not sec:
            return None
        now = str(int(self.clock()))
        auth = hashlib.sha1((key + sec + now).encode()).hexdigest()
        d = self.get_json(f"{PI_API}?{urllib.parse.urlencode({'q': q, 'max': n})}",
                          {"X-Auth-Key": key, "X-Auth-Date": now, "Authorization": auth})
        return [{"type": "podcast", "name": (f.get("title") or "").strip(), "author": (f.get("author") or "").strip(),
                 "feed": f.get("url") or "", "source": "Podcast Index"} for f in (d.get("feeds") or [])[:n]]

    def _itunes(self, q, n):
        d = self.get_json(f"{ITUNES_API}?{urllib.parse.urlencode({'media': 'podcast', 'term': q, 'limit': n})}")
        return [{"type": "podcast", "name": (r.get("collectionName") or "").strip(), "author": (r.get("artistName") or "").strip(),
                 "feed": r.get("feedUrl") or "", "source": "Apple Podcasts"} for r in (d.get("results") or [])[:n]]

    def _rb_server(self):
        try:
            ips = {a[4][0] for a in socket.getaddrinfo("all.api.radio-browser.info", 443, proto=socket.IPPROTO_TCP)}
            names = sorted({socket.gethostbyaddr(ip)[0] for ip in ips})
            names = [x for x in names if x.endswith(".api.radio-browser.info")]
            if names:
                return names[0]
        except Exception:
            pass
        return RB_FALLBACK

    def _radio(self, q, n):
        qs = urllib.parse.urlencode({"name": q, "limit": n, "hidebroken": "true", "order": "votes", "reverse": "true"})
        d = self.get_json(f"https://{self._rb_server()}/json/stations/search?{qs}")
        return [{"type": "radio", "name": (s.get("name") or "").strip(), "url": s.get("url_resolved") or s.get("url") or "",
                 "codec": (s.get("codec") or "").lower(), "bitrate": s.get("bitrate") or 0,
                 "country": s.get("countrycode") or "", "source": "radio-browser"} for s in (d or [])[:n]]

    def search(self, query, type=None, n=3):
        if type == "radio":
            return self._radio(query, n)
        try:
            r = self._podcastindex(query, n)
        except Exception as e:
            log.warning("podcastindex search failed (%s)", e.__class__.__name__)
            r = None
        if r is None:
            r = self._itunes(query, n)
        return r

    # ---------- probe ----------
    def probe(self, c):
        """Adds c['ok'], c['why'] and, for podcasts, c['latest'] {title, pubDate}."""
        try:
            if c["type"] == "podcast":
                if not c.get("feed", "").lower().startswith("https://"):
                    return {**c, "ok": False, "why": "feed isn't HTTPS"}
                latest = pick_latest(parse_items(self.get_bytes(c["feed"])))
                if not latest:
                    return {**c, "ok": False, "why": "no HTTPS episode"}
                st, ct, _ = self.head(latest["url"], rng=True)
                ok = st in (200, 206) and bool(AUDIO_RE.match(ct or ""))
                return {**c, "ok": ok, "why": "" if ok else f"episode answered {st} {ct or '?'}",
                        "latest": {"title": latest["title"], "pubDate": latest["pubDate"]}}
            if not c.get("url", "").lower().startswith("https://"):
                return {**c, "ok": False, "why": "stream isn't HTTPS"}
            st, ct, _ = self.head(c["url"])
            ok = st == 200 and bool(AUDIO_RE.match(ct or ""))
            return {**c, "ok": ok, "why": "" if ok else f"stream answered {st} {ct or '?'}"}
        except Exception as e:
            return {**c, "ok": False, "why": f"probe failed ({e.__class__.__name__})"}

    def _top(self, query, cands, n):
        """Exact name first, then HTTPS, keeping directory order otherwise."""
        q = re.sub(r"^the\s+", "", query.casefold().strip())
        def rank(c):
            nm = re.sub(r"^the\s+", "", c["name"].casefold())
            u = c.get("feed") or c.get("url") or ""
            return (nm != q, not u.lower().startswith("https://"))
        out, seen = [], set()
        for c in sorted((c for c in cands if c.get("name")), key=rank):
            k = c["name"].casefold()
            if k not in seen:
                seen.add(k); out.append(c)
        return out[:n]

    def find(self, query, type=None, n=3):
        return [self.probe(c) for c in self._top(query, self.search(query, type, n + 5), n)]

    async def find_async(self, query, type=None, n=3):
        cands = self._top(query, await asyncio.to_thread(self.search, query, type, n + 5), n)
        return list(await asyncio.gather(*(asyncio.to_thread(self.probe, c) for c in cands)))

    @staticmethod
    def to_entry(c, existing_ids=()):
        """Library entry for a probed candidate (public URLs only)."""
        base = _slug(c["name"]); eid = base; i = 2
        while eid in existing_ids:
            eid = f"{base}_{i}"; i += 1
        if c["type"] == "podcast":
            methods = [{"kind": "podcast_rss_latest", "target_kind": "any", "feed": c["feed"],
                        "skip_episode_types": ["trailer", "bonus"]}]
        else:
            methods = [{"kind": "stream_url", "target_kind": "any", "url": c["url"], "format": c.get("codec") or None}]
        return {"id": eid, "name": c["name"], "aliases": [], "type": c["type"], "methods": methods}
