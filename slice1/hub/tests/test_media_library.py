"""Media M1: library seed, name matching, list_library tool, RSS resolver.

No network, no Keychain, no hub. Run from anywhere:

  python tests/test_media_library.py
"""
import asyncio, json, logging, os, subprocess, sys, tempfile, time
logging.getLogger("hub.media").setLevel(logging.ERROR)
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB))
FIX = Path(__file__).resolve().parent / "fixtures"
TMP = tempfile.mkdtemp(prefix="womble-media-")
os.environ["MEDIA_DB"] = os.path.join(TMP, "media.sqlite")

from media.library import Library, SEED_PATH, normalise, token_set_ratio
from media import local as media_local
from media.resolver import Resolver, parse_items, pick_latest, ep_key


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL {msg}")


class FakeDisplay:
    def __init__(self): self.events = []
    async def emit(self, event, data=None, **k): self.events.append((event, data))


def test_seed():
    lib = Library(os.path.join(TMP, "seed.sqlite"))
    n = lib.count()
    check(n == len(json.loads(SEED_PATH.read_text())["entries"]) == 12, f"seed count {n}")
    lib.db.execute("UPDATE entries SET enabled=0 WHERE id='db_obsessed'"); lib.db.commit()
    before = [dict(r) for r in lib.db.execute("SELECT * FROM entries ORDER BY id")]
    lib.import_seed(); lib.import_seed()
    after = [dict(r) for r in lib.db.execute("SELECT * FROM entries ORDER BY id")]
    check(before == after, "seed import is not idempotent")
    check(not lib.get("db_obsessed")["enabled"], "re-seed re-enabled a disabled entry")
    lib2 = Library(os.path.join(TMP, "seed.sqlite"))   # reopen, seeds again
    check(lib2.count() == 12, "reopen duplicated rows")
    print("PASS seed idempotent")


def test_match():
    lib = Library(os.path.join(TMP, "match.sqlite"))
    m = lambda s: (lib.match(s) or (None,))[0]
    check(m("deep state radio")["id"] == "dsr_network", "deep state radio")
    check(m("LBC")["id"] == "lbc", "LBC")
    check(m("lbc")["id"] == "lbc" and m("L B C")["id"] == "lbc", "lbc variants")
    check(m("the world service")["id"] == "bbc_ws", "world service")
    check(m("DSR Daily")["id"] == "dsr_daily", "dsr daily")
    check(m("daily beast podcast")["id"] == "db_podcast", "daily beast")
    check(m("inside trumps head")["id"] == "db_inside_trumps_head", "trump's head")
    check(m("siliconsciousness podcast")["id"] == "dsr_siliconsciousness", "strip podcast")
    check(m("the royalists")["id"] == "db_royalist", "fuzzy plural")
    check(lib.match("Radio Nacional de España") is None, "unknown matched")
    check(lib.match("jazz fm") is None, "jazz fm matched")
    check(lib.match("") is None, "empty matched")
    check(lib.match("kitchen music") is None, "disabled preset matched")
    check(normalise("The DSR Podcast!") == "dsr", normalise("The DSR Podcast!"))
    check(token_set_ratio("a b", "b a") == 1.0, "token set")
    print("PASS match")


def test_summary_and_tool():
    lib = Library(os.path.join(TMP, "sum.sqlite"))
    s = lib.summary()
    check(s.startswith("Two radio stations and nine podcasts, including"), s)
    check(s.count(".") == 1 and len(s) < 160, "summary is one short sentence")
    check(lib.summary("radio").startswith("Two radio stations"), lib.summary("radio"))
    check("music" not in s, "disabled preset in summary")

    import brain.tools as bt
    bt._LIBRARY = None
    disp = FakeDisplay()
    ctx = bt.ToolContext("what's in my library", "box3-kitchen", "box3-kitchen", None, disp)
    res = asyncio.run(bt.list_library({}, ctx))
    check(res["ok"] and res["count"] == 11 and res["summary"] == s, res)
    check(ctx.card["type"] == "text" and "Podcasts:" in ctx.card["body"] and "Radio:" in ctx.card["body"], ctx.card)
    ev, data = disp.events[-1]
    check(ev == "media.library" and len(data["entries"]["podcast"]) == 9, "media.library event")
    check("https://" not in json.dumps(data) and "https://" not in json.dumps(res), "URLs leaked to frame/brain")
    res = asyncio.run(bt.list_library({"type": "podcasts"}, ctx))
    check(res["count"] == 9 and "Radio:" not in ctx.card["body"], "type filter")
    names = [t["name"] for t in bt.responses_tools(include_web_search=False)]
    check("list_library" in names, names)
    check(bt.BY_NAME["list_library"].scope == "read", "scope")
    print("PASS summary and list_library tool")


def test_resolver():
    feeds = {"https://feeds.megaphone.fm/QCD5580850161": (FIX / "feed_db_podcast.xml").read_bytes(),
             "https://feeds.megaphone.fm/QCD8589611489": (FIX / "feed_db_inside.xml").read_bytes()}
    calls = []

    def fetch(url):
        calls.append(url)
        if url not in feeds:
            raise OSError("no network in tests")
        return feeds[url]

    items = parse_items(feeds["https://feeds.megaphone.fm/QCD5580850161"])
    check(len(items) == 6 and items[0]["episode_type"] == "trailer", items)
    check(len(parse_items(feeds["https://feeds.megaphone.fm/QCD5580850161"], limit=2)) == 2, "early stop")
    got = pick_latest(items)
    check(got["title"] == "Inside the Head: Episode 12" and got["url"] == "https://example.invalid/a/x12.mp3",
          f"newest non-trailer: {got}")
    check(got["ep_key"] == ep_key("xpost-12", None) and len(got["ep_key"]) == 16, "ep_key")
    check(pick_latest(items, {"filter": {"title_prefix": "older"}})["title"] == "Older full episode", "prefix")
    check(pick_latest([{"title": "x", "url": "http://a/b.mp3", "guid": "", "pubDate": "", "episode_type": "full"}]) is None,
          "http enclosure accepted")

    lib = Library(os.path.join(TMP, "res.sqlite"))
    now = [0.0]
    r = Resolver(lib, fetch=fetch, clock=lambda: now[0])
    out = r.refresh_all()
    check(set(out) == {"db_podcast", "db_inside_trumps_head"}, out.keys())   # other feeds fail offline, skipped
    a, b = out["db_inside_trumps_head"], out["db_podcast"]
    check(a["title"] == b["title"] == "Inside the Head: Episode 12", "cross-post newest")
    check(("dup_of" in a) != ("dup_of" in b), "cross-post not deduped")
    n = len(calls)
    r.refresh_all(); check(len(calls) == n + 7, "cache: only failed feeds refetched")   # 7 offline feeds retried
    now[0] = 901; r.refresh_all(); check(len(calls) == n + 7 + 9, "cache expiry")
    print("PASS resolver (fixtures, no network)")


def test_local():
    p = Path(TMP) / "media_local.json"
    cfg = media_local.load(p)
    check(cfg == {"triggers": {}, "targets": []}, "missing file")
    p.write_text(json.dumps({"triggers": {"lbc_kitchen": "T1"},
                             "targets": [{"room": "kitchen", "name": "BOX-3", "target_kind": "box3", "device_ref": "box3-kitchen"}]}))
    cfg = media_local.load(p)
    check(media_local.trigger(cfg, "lbc_kitchen") == "T1" and media_local.trigger(cfg, "nope") is None, "trigger")
    lib = Library(os.path.join(TMP, "loc.sqlite"))
    check(media_local.sync_targets(lib, cfg) == 1 and media_local.sync_targets(lib, cfg) == 1, "targets sync")
    p.write_text("{not json"); check(media_local.load(p)["triggers"] == {}, "bad json")
    print("PASS media_local loader")


def test_repo_clean():
    """No device ids, trigger ids or home paths in tracked media files."""
    root = HUB.parents[1]
    files = ["slice1/hub/media", "slice1/hub/tests/fixtures"]
    pat = r"amzn1\.|/Users/|voicemonkey\.io/trigger\?|[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|([0-9a-f]{2}:){5}"
    out = subprocess.run(["grep", "-rEnI", pat, *files], cwd=root, capture_output=True, text=True)
    check(out.returncode == 1, "identifier-like strings found:\n" + out.stdout[:400])
    print("PASS no identifiers in media files")


if __name__ == "__main__":
    test_seed(); test_match(); test_summary_and_tool(); test_resolver(); test_local(); test_repo_clean()
    print("ALL PASS test_media_library")
