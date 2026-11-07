"""The scheduler's time handling: each source's share covers its own probe, running out of time is not a failure,
spare time goes to sources their share stopped, and a site does not re-read a portal another source owns."""
import datetime as dt
import time

import pytest
import requests

from gacrawl import channels
from gacrawl.channels import Probe
from gacrawl.fetch import Fetcher
from gacrawl.scheduler import Progress, due, owned_endpoints, ran_out_of_time, run_sources
from gacrawl.store import Record, Store


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def _env(tmp_path):
    return Store(tmp_path / "data"), channels.ChannelCache(tmp_path / "c.json"), Progress(tmp_path / "p.json")


def test_probe_runs_in_its_own_share_not_the_previous_sources(tmp_path, fetcher, monkeypatch):
    """One worker, two sources: the first uses up its share; the second's probe used to inherit that spent share and
    'fail' with OutOfTime (185 sources in one run)."""
    def probe(f, src):
        f._check_time()                       # what any real probe request does first
        return [Probe("html", True, 0.1, 1, src["url"])]

    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        if src["id"] == "a:site":
            while True:                           # a big archive: reads until its share is used up
                time.sleep(0.05)
                f._check_time()
                yield Record(src["id"], "page", f"{src['url']}p{time.monotonic()}", title="p")
        yield Record(src["id"], "page", src["url"] + "home", title="home")

    monkeypatch.setattr(channels, "probe_source", probe)
    monkeypatch.setattr(channels, "crawl", crawl)
    store, cache, prog = _env(tmp_path)
    srcs = [{"id": "a:site", "url": "https://a.gov/", "tier": "a"}, {"id": "b:site", "url": "https://b.gov/", "tier": "a"}]
    out = run_sources({"schedule": {"windows": []}}, srcs, fetcher, store, cache, prog, time.monotonic() + 20,
                      force=True, workers=1, slice_minutes=0.01, log=lambda *_: None)
    assert out["a:site"].startswith("partial") and out["a:site"].endswith("(time slice)")
    assert prog.get("b:site")["status"] == "ok", out["b:site"]
    assert fetcher.thread_stop_at is None                 # nothing left behind for the thread's next source
    store.close()


def test_spare_time_goes_to_sources_their_share_stopped(tmp_path, fetcher, monkeypatch):
    calls = {"n": 0}

    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        calls["n"] += 1
        if calls["n"] == 1:
            for i in range(3):
                yield Record(src["id"], "page", f"https://a.gov/{i}", title=str(i))
            while True:
                time.sleep(0.05)
                f._check_time()
        yield Record(src["id"], "page", "https://a.gov/last", title="last")

    monkeypatch.setattr(channels, "crawl", crawl)
    store, cache, prog = _env(tmp_path)
    src = {"id": "a:site", "url": "https://a.gov/", "tier": "a", "channel": "html", "endpoint": "https://a.gov/"}
    lines = []
    out = run_sources({"schedule": {"windows": []}}, [src], fetcher, store, cache, prog, time.monotonic() + 600,
                      force=True, workers=1, slice_minutes=0.01, log=lines.append)
    assert calls["n"] == 2 and prog.get("a:site")["status"] == "ok", out
    assert any(ln.startswith("pass 2:") for ln in lines)
    store.close()


def test_running_out_of_time_is_not_backed_off(tmp_path):
    prog = Progress(tmp_path / "p.json")
    now = dt.datetime.now(dt.timezone.utc)
    recent = (now - dt.timedelta(minutes=20)).isoformat()
    prog.update("sd-x:site", last_run=recent,
                status="error choosing channel: no working channel: html: OutOfTime: https://x.k12.ga.us: next turn at +6s is past the deadline")
    prog.update("dead:site", last_run=recent, status="error choosing channel: no working channel: html: HTTP 404")
    srcs = [{"id": "sd-x:site", "url": "https://x.k12.ga.us/", "tier": "a"},
            {"id": "dead:site", "url": "https://dead.gov/", "tier": "a"}]
    ids = [s["id"] for s in due(srcs, prog, now=now)]
    assert ids == ["sd-x:site"]                  # timed out: due again (and first, as a resume); a real error waits
    assert ran_out_of_time("partial (html full): 0 new / 2 seen (time slice)")
    assert not ran_out_of_time("error (html): HTTPError: 500")


def test_site_leaves_a_portal_to_the_source_that_owns_it(tmp_path, fetcher, monkeypatch):
    seen = {}

    def probe(f, src):
        return [Probe("api", True, 0.1, 5, "legistar:dekalbcountyga"), Probe("html", True, 1.0, 1, src["url"])]

    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        seen[src["id"]] = chosen["endpoint"]
        yield Record(src["id"], "page", src["url"], title="x")

    monkeypatch.setattr(channels, "probe_source", probe)
    monkeypatch.setattr(channels, "crawl", crawl)
    store, cache, prog = _env(tmp_path)
    leg = {"id": "co-dekalb:legistar", "url": "https://dekalbcountyga.legistar.com/", "channel": "api",
           "endpoint": "legistar:dekalbcountyga", "tier": "a"}
    site = {"id": "co-dekalb:site", "url": "https://www.dekalbcountyga.gov/", "tier": "a"}
    assert owned_endpoints([leg, site]) == {"legistar:dekalbcountyga": "co-dekalb:legistar"}
    # chosen before the rule existed: the cached choice is dropped and the site probed again
    cache.put("co-dekalb:site", Probe("api", True, 0.1, 5, "legistar:dekalbcountyga"), [])
    run_sources({"schedule": {"windows": []}}, [site], fetcher, store, cache, prog, time.monotonic() + 30, force=True,
                workers=1, owned=owned_endpoints([leg, site]), log=lambda *_: None)
    assert seen["co-dekalb:site"] == "https://www.dekalbcountyga.gov/"
    store.close()


def test_purge_by_channel_keeps_the_sites_own_pages(tmp_path):
    store = Store(tmp_path / "data")
    store.add(Record("co-dekalb:site", "agenda_item", "https://dekalbcountyga.legistar.com/M1", title="a", channel="api"))
    store.add(Record("co-dekalb:site", "page", "https://www.dekalbcountyga.gov/x", title="p", channel="html"))
    store.add(Record("cand:x", "mention", "https://dekalbcountyga.legistar.com/M1", title="m", meta={"source": "co-dekalb:site"}))
    store.add(Record("cand:x", "mention", "https://www.dekalbcountyga.gov/x", title="m2", meta={"source": "co-dekalb:site"}))
    store.flush()
    assert store.purge_source("co-dekalb:site", channel="api") == {"citing_mentions": 1, "records": 1}
    left = sorted((r["source_id"], r["url"]) for r in store.db.execute("SELECT source_id, url FROM records"))
    assert left == [("cand:x", "https://www.dekalbcountyga.gov/x"), ("co-dekalb:site", "https://www.dekalbcountyga.gov/x")]
    store.close()


def test_district_seed_passes_over_sites_that_are_not_the_district(monkeypatch):
    from gacrawl import seed

    pages = {"https://www.columbiahs.dekalb.k12.ga.us/": "Columbia High School",
             "https://www.forsythnews.com/": "Forsyth County News",
             "https://www.ccboe.net/": "Columbia County School District / Homepage"}

    def inspect(f, url):
        return {"website": url, "home_title": pages.get(url, ""), "home_text": "", "portals": {}, "feeds": []}

    monkeypatch.setattr(seed, "inspect_site", inspect)
    col = {"id": "sd-columbia", "name": "Columbia County School District", "county": "Columbia",
           "aliases": ["Columbia County Schools"]}
    ok = lambda url, ins: seed.district_site_ok(col, url, ins)   # noqa: E731
    got = seed.resolve_site(None, [("https://www.columbiahs.dekalb.k12.ga.us/", "wikipedia"),
                                   ("https://www.ccboe.net/", "wikidata")], accept=ok)
    assert got["website"] == "https://www.ccboe.net/" and "home_text" not in got
    fo = {"id": "sd-forsyth", "name": "Forsyth County School District", "county": "Forsyth", "aliases": []}
    got = seed.resolve_site(None, [("https://www.forsythnews.com/", "wikipedia")],
                            accept=lambda url, ins: seed.district_site_ok(fo, url, ins))
    assert not got.get("website") and got["website_rejected"] == "https://www.forsythnews.com/"
    assert seed._clean_site("https://web.archive.org/web/20220208092027/https://www.dekalbschoolsga.org/") == \
        "https://www.dekalbschoolsga.org/"


def test_police_news_is_place_qualified_and_georgia_only():
    from gacrawl.police import department_name, news_phrase
    from gacrawl.policing_news import _mentions_department, in_georgia

    dek = {"id": "co-dekalb", "name": "DeKalb County", "kind": "county"}
    d = {"found": True, "kind": "police", "name": "Police Department", "link_label": "Police Department"}
    assert department_name(dek, d)[0] == "DeKalb County Police Department"
    assert news_phrase(dek, d) == "DeKalb County police"
    colorado = "The Lakewood Police Department identified their fallen officer... Police Department officials said"
    assert not _mentions_department(colorado, {**d, "name": "DeKalb County Police Department"}, "DeKalb County", "DeKalb County police")
    story = "DeKalb County police said the man was shot on Candler Road."
    assert _mentions_department(story, {**d, "name": "DeKalb County Police Department"}, "DeKalb County", "DeKalb County police")
    assert not in_georgia("Police identify fallen Colorado officer killed in shootout", story, True, "DeKalb County", "DeKalb")
    assert in_georgia("Man shot on Candler Road", story, True, "DeKalb County", "DeKalb")
    assert not in_georgia("Columbus police arrest suspect", "Columbus police said... Franklin County, Ohio", False, "Columbus", "Muscogee")
    bibb = {"id": "macon-bibb", "name": "Macon", "kind": "consolidated", "counties": ["Bibb"]}
    assert department_name(bibb, {"kind": "sheriff", "name": "Notice of Change to Mail Services - Effective July 15, 2025",
                                  "link_label": "Sheriff’s Office"})[0] == "Bibb County Sheriff's Office"


def test_coverage_flags_zero_and_shrinking_features(tmp_path):
    import json

    from gacrawl import coverage

    (tmp_path / "state").mkdir()
    store = Store(tmp_path / "data")
    store.add(Record("ga:legis", "bill", "https://legis.ga.gov/legislation/1", title="HB 1"))
    for i in range(3):
        store.add(Record("co-x:legistar", "agenda_item", f"https://x/{i}", title=str(i), jurisdiction="co-x"))
    store.flush()
    (tmp_path / "state" / "coverage.json").write_text(json.dumps({"as_of": "2026-11-07T00:00:00+00:00",
                                                                  "counts": {"agenda_items": 10}}))
    out = coverage.run(tmp_path, store)
    assert "roll_calls" in out["zero"] and "agenda_items" in out["shrank"] and "bills" not in out["zero"]
    md = (tmp_path / "data" / "coverage.md").read_text()
    assert "General Assembly roll calls" in md and "shrank from 10 to 3" in md
    assert json.loads((tmp_path / "state" / "coverage.json").read_text())["counts"]["agenda_items"] == 3
    store.close()


def test_police_news_uses_outlet_apis_when_gdelt_refuses(tmp_path, monkeypatch):
    import json

    from gacrawl import policing_news as pnm

    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "state" / "wp_outlets.json").write_text(json.dumps({"daltoncitizen.com": {"api": "https://www.daltoncitizen.com/wp-json/"}}))
    (tmp_path / "config" / "outlets.yaml").write_text("outlets: [daltoncitizen.com]\nstatewide: []\ncounty_outlets: {whitfield: [daltoncitizen.com]}\n")

    class R:
        def __init__(self, status, body):
            self.status, self.content, self.url, self.content_type = status, body, "u", "application/json"
            self.text = body.decode()

        def json(self):
            return json.loads(self.content)

    post = [{"link": "https://www.daltoncitizen.com/news/man-charged", "date": "2026-11-01T10:00:00",
             "title": {"rendered": "Man charged after shooting"},
             "content": {"rendered": "<p>Dalton police said the man was arrested and charged after a shooting on Walnut Avenue in Dalton, Georgia.</p>"}}]

    class F:
        def get(self, url, params=None, use_cache=True):
            if "gdeltproject" in url:
                return R(429, b"Please limit requests to one every 5 seconds")
            return R(200, json.dumps(post).encode())

        def _sleep(self, s):
            pass

    store = Store(tmp_path / "data")
    monkeypatch.setattr("gacrawl.candidates.load_outlets", lambda root: ({"daltoncitizen.com"}, set()))
    pn = pnm.PoliceNews(F(), store, tmp_path, log=lambda *_: None)
    j = {"id": "dalton", "name": "Dalton", "kind": "city", "counties": ["Whitfield"]}
    n = pn.department("dalton", {"found": True, "kind": "police", "name": "Police Department"}, "Dalton", j)
    assert n == 1 and not pn.gdelt_ok
    rec = next(store.iter_query("kind='police_incident'"))
    assert rec["jurisdiction"] == "dalton" and rec["meta"]["department"] == "Dalton Police Department"
    store.close()


def test_a_changed_website_is_probed_again(tmp_path, fetcher, monkeypatch):
    seen = {}

    def probe(f, src):
        return [Probe("html", True, 1.0, 1, src["url"])]

    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        seen[src["id"]] = chosen["endpoint"]
        yield Record(src["id"], "page", src["url"], title="x")

    monkeypatch.setattr(channels, "probe_source", probe)
    monkeypatch.setattr(channels, "crawl", crawl)
    store, cache, prog = _env(tmp_path)
    # chosen when the seed had the archive copy; the seed now gives the district's own site
    cache.put("sd-dekalb:site", Probe("html", True, 1.0, 1, "https://web.archive.org/web/2022/https://www.dekalbschoolsga.org/"), [])
    src = {"id": "sd-dekalb:site", "url": "https://www.dekalbschoolsga.org/", "tier": "a"}
    run_sources({"schedule": {"windows": []}}, [src], fetcher, store, cache, prog, time.monotonic() + 30, force=True,
                workers=1, log=lambda *_: None)
    assert seen["sd-dekalb:site"] == "https://www.dekalbschoolsga.org/"
    assert cache.get("sd-dekalb:site")["url"] == "https://www.dekalbschoolsga.org/"
    store.close()
