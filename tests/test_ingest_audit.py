"""Ingestion faults found by the Nov. 18, 2026 audit, each run through the real scheduler against a local site:
a failed answer is not "nothing new"; a sitemap's unread pages are not "unchanged"; a link that redirects is fresh
once read; a date, place or evidence given later is stored; a PDF marked read with no record is read again; a host
that could not be reached is not a refusal; a dropped connection is tried again."""
import datetime as dt
import http.server
import json
import threading
import time
from email.utils import format_datetime

import pytest
import requests

from gacrawl import channels, scheduler
from gacrawl.fetch import NET_ERRORS, Fetcher
from gacrawl.shard import merge_delta, write_delta
from gacrawl.store import Record, Store


class Site:
    def __init__(self):
        self.routes = {"/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n")}
        self.hits = []
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                site.hits.append(self.path)
                r = site.routes.get(self.path) or site.routes.get(self.path.split("?")[0])
                if callable(r):
                    r = r(self.path)
                if not r:
                    self.send_response(404); self.end_headers(); return
                st, ct, body = r
                if st in (301, 302):
                    self.send_response(st); self.send_header("Location", body.decode()); self.send_header("Content-Length", "0"); self.end_headers(); return
                body = body.replace(b"HOST", self.headers["Host"].encode())
                self.send_response(st); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture
def site():
    s = Site()
    yield s
    s.srv.shutdown()


@pytest.fixture
def env(tmp_path):
    s = requests.Session()
    s.trust_env = False
    f = Fetcher("TestCrawler/0.1", tmp_path / "state", min_interval=0, session=s, sleep=lambda x: None)
    st = Store(tmp_path / "data")
    cache = channels.ChannelCache(tmp_path / "state" / "channels.json")
    prog = scheduler.Progress(tmp_path / "state" / "progress.json")

    def run(src, **kw):
        f._memo.clear()
        return scheduler.run_sources({}, [src], f, st, cache, prog, time.monotonic() + 300, workers=1, log=lambda *a: None, **kw)

    yield type("E", (), {"f": f, "store": st, "prog": prog, "run": staticmethod(run), "tmp": tmp_path})
    st.close()


def page(path):
    return (200, "text/html", f"<html><head><title>{path}</title></head><body><main><h1>Council {path}</h1><p>{'Minutes text. ' * 40}</p></main></body></html>".encode())


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>http://HOST/n/{i}</link><guid>{i}</guid><pubDate>{p}</pubDate><description>{t} text</description></item>"
                   for i, t, p in items)
    return (200, "application/rss+xml", f'<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>{body}</channel></rss>'.encode())


def test_a_feed_that_fails_one_night_is_an_error_and_what_it_missed_is_read_next_time(site, env):
    now = dt.datetime.now(dt.timezone.utc)
    old, mid = format_datetime(now - dt.timedelta(days=10)), format_datetime(now - dt.timedelta(days=2))
    src = {"id": "town:site", "url": site.base + "/", "channel": "feed", "endpoint": site.base + "/news.rss", "follow_feed_links": False, "tier": "a"}
    env.f.max_retries = 0
    site.routes["/news.rss"] = rss([(1, "Old item", old)])
    env.run(src)
    first_ok = (now - dt.timedelta(days=3)).isoformat(timespec="seconds")
    env.prog.data["town:site"]["last_ok"] = first_ok
    site.routes["/news.rss"] = (503, "text/html", b"<html>Service Unavailable</html>")
    out = env.run(src, force=True)
    assert out["town:site"].startswith("error") and "HTTP 503" in out["town:site"]
    assert env.prog.data["town:site"]["last_ok"] == first_ok                   # the last good run stands
    site.routes["/news.rss"] = rss([(2, "Budget vote", mid), (1, "Old item", old)])
    env.run(src, force=True)
    assert {r["title"] for r in env.store.query("source_id='town:site'")} == {"Old item", "Budget vote"}


def test_sitemap_pages_a_run_did_not_reach_are_read_by_the_next_ones(site, env):
    n = 30
    urls = "".join(f"<url><loc>http://HOST/council/page{i}</loc><lastmod>2026-06-01</lastmod></url>" for i in range(n))
    site.routes["/sitemap.xml"] = (200, "application/xml", f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode())
    for i in range(n):
        site.routes[f"/council/page{i}"] = page
    src = {"id": "town:site", "url": site.base + "/", "channel": "sitemap", "endpoint": site.base + "/sitemap.xml", "max_pages": 10, "tier": "a"}
    env.run(src)
    assert env.store.total() == 10 and "sitemap_read" not in env.store.cursors("town:site")
    for _ in range(3):
        env.run(src, force=True)
    assert env.store.total() == n                                               # was 10 of 30, for good
    assert env.store.cursors("town:site").get("sitemap_read")
    # with the whole list read, an unchanged list costs no page; a page added later is still read
    before = len(site.hits)
    env.run(src, force=True)
    assert not [h for h in site.hits[before:] if h.startswith("/council/")]
    site.routes["/council/new"] = page
    site.routes["/sitemap.xml"] = (200, "application/xml", ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls +
                                                           "<url><loc>http://HOST/council/new</loc><lastmod>2026-01-01</lastmod></url></urlset>").encode())
    env.run(src, force=True)
    assert env.store.total() == n + 1


def test_a_link_that_redirects_is_fresh_once_read(site, env):
    site.routes["/old/minutes.pdf"] = (302, "", b"/files/minutes.pdf")
    site.routes["/files/minutes.pdf"] = (200, "text/html", page("/files/minutes.pdf")[2])
    ctx = channels.CrawlContext(frontier=env.store)
    rec = channels._page_record(env.f, "town:site", site.base + "/old/minutes.pdf", {"id": "town:site"}, ctx)
    assert rec is not None and env.store.fresh(site.base + "/old/minutes.pdf", 3650)       # the link as it is written
    assert channels._skip_fresh(ctx, site.base + "/old/minutes.pdf") is True


def test_a_date_place_or_evidence_given_later_is_stored(env, tmp_path):
    a = Record("town:site", "vote", "https://x.gov/min.pdf", title="Approve the budget", text="Approve the budget", meta={"n": 1}, key="k1")
    assert env.store.add(a) is True
    b = Record("town:site", "vote", "https://x.gov/min.pdf", title="Approve the budget", text="Approve the budget", meta={"n": 1}, key="k1",
               published="2026-05-11", jurisdiction="town", evidence="primary")
    assert env.store.add(b) is True                                              # same hash; was dropped as unchanged
    row = env.store.query("key='k1'")[0]
    assert (row["published"][:10], row["jurisdiction"], row["evidence"]) == ("2026-05-11", "town", "primary")
    assert env.store.add(b) is False
    # sent again without the date: what is stored stays
    assert env.store.add(a) is False and env.store.query("key='k1'")[0]["published"][:10] == "2026-05-11"
    # and a merge carries it: the main database holds the record undated, the shard's delta holds it dated
    main = Store(tmp_path / "main")
    main.add(a)
    main.flush()
    env.store.flush()
    out = tmp_path / "d.db"
    write_delta(env.store, "2000-01-01", out)
    merge_delta(main, out)
    assert main.query("key='k1'")[0]["published"][:10] == "2026-05-11"
    main.close()


def test_a_pdf_marked_read_with_no_record_is_read_again(env):
    env.store.mark_url("https://x.gov/lost.pdf", 200, "town:site", "application/pdf", b"%PDF-1.4 x")
    env.store.mark_url("https://x.gov/kept.pdf", 200, "town:site", "application/pdf", b"%PDF-1.4 y")
    env.store.mark_url("https://x.gov/page", 200, "town:site", "text/html", b"<html>")
    env.store.add(Record("town:site", "document", "https://x.gov/kept.pdf", title="Minutes", text="Minutes"))
    env.store.flush()
    assert env.store.fresh("https://x.gov/lost.pdf", 3650)
    assert env.store.forget_unrecorded_documents("2026-11-07") == 1
    assert not env.store.fresh("https://x.gov/lost.pdf", 3650) and env.store.fresh("https://x.gov/kept.pdf", 3650) and env.store.fresh("https://x.gov/page", 7)


def test_a_host_that_could_not_be_reached_is_an_error_not_a_refusal(env, monkeypatch):
    from gacrawl.fetch import Disallowed

    src = {"id": "town:site", "url": "http://127.0.0.1:9/", "channel": "html", "endpoint": "http://127.0.0.1:9/", "tier": "c"}

    def crawl(*a, **k):
        raise Disallowed("http://127.0.0.1:9/feed/ (unreachable (ConnectTimeout: timed out))")
        yield

    monkeypatch.setattr(channels, "crawl", crawl)
    out = env.run(src, force=True)
    assert out["town:site"].startswith("error") and "unreachable" in out["town:site"], out
    # a refusal is still a refusal
    def refused(*a, **k):
        raise Disallowed("http://127.0.0.1:9/ (robots.txt disallows this path for our user agent)")
        yield

    monkeypatch.setattr(channels, "crawl", refused)
    assert env.run(src, force=True)["town:site"].startswith("stopped: robots.txt disallows")


def test_a_connection_dropped_while_the_body_is_read_is_a_network_error():
    assert issubclass(requests.exceptions.ChunkedEncodingError, NET_ERRORS)
    assert issubclass(requests.exceptions.ConnectTimeout, NET_ERRORS) and not issubclass(ValueError, NET_ERRORS)


def test_the_channel_cache_is_changed_under_its_lock(tmp_path):
    cache = channels.ChannelCache(tmp_path / "channels.json")
    held = []
    real = cache._lock

    class Watch:
        def __enter__(self):
            real.acquire(); held.append(1)

        def __exit__(self, *a):
            held.pop(); real.release()

    cache._lock = Watch()

    class D(dict):
        def __setitem__(self, k, v):
            assert held, "put() changed the cache outside its lock"
            super().__setitem__(k, v)

        def pop(self, *a):
            assert held, "invalidate() changed the cache outside its lock"
            return super().pop(*a)

    cache.data = D()
    cache.put("a:site", channels.Probe("html", True, "u"), [], save=True)
    cache.invalidate("a:site")
    assert "a:site" not in cache.data


def test_boarddocs_stops_at_its_time_limit_instead_of_storing_empty_meetings(tmp_path):
    from gacrawl.fetch import OutOfTime
    from gacrawl.sources.boarddocs import BoardDocs

    class R:
        def __init__(self, text, ct="text/html", status=200, url=""):
            self.status, self.text, self.content_type, self.url, self.content = status, text, ct, url, text.encode()

    class Fake:
        """A fetcher at its deadline: every request after `budget` raises OutOfTime."""
        def __init__(self, budget):
            self.calls, self.budget = 0, budget

        def _tick(self):
            self.calls += 1
            if self.calls > self.budget:
                raise OutOfTime("this source's time slice used up")

        def deadline(self):
            return None

        def get(self, url, **kw):
            self._tick()
            if url.endswith("/Public"):
                return R('<div class="committee" id="AAAAAA1">Board of Education</div>')
            return R("<html><body><h1>Item</h1><p>" + "Item text. " * 30 + "</p></body></html>", url=url)

        def post(self, url, data=None, **kw):
            self._tick()
            if "BD-GetMeetingsList" in url:
                return R(json.dumps([{"unique": f"MEET{y}AAA", "name": "Regular Meeting", "numberdate": f"{y}0115"} for y in range(2018, 2026)]), "application/json")
            return R('<h2>Action Items</h2><a id="ITEMAAA1">1. Approve the budget</a><a id="ITEMAAA2">2. Adopt millage rate</a>')

    store = Store(tmp_path / "bd")
    bd = BoardDocs(Fake(budget=6), "https://go.boarddocs.com/ga/test/Board.nsf/Public")
    with pytest.raises(OutOfTime):                       # the scheduler records "partial", not "ok"
        for rec in bd.records("sd:boarddocs", "sd", store=store):
            store.add(rec)
    assert all(r["meta"]["items"] for r in store.read("sd:boarddocs", "meeting"))        # no empty shells
    # a shell an older run left is read again, outside the lookback too
    shell = Record("sd:boarddocs", "meeting", "https://go.boarddocs.com/ga/test/Board.nsf/goto?open&id=MEET2018AAA", title="x",
                   key="sd:boarddocs|meeting|boarddocs|MEET2018AAA", meta={"items": 0})
    store.add(shell)
    bd2 = BoardDocs(Fake(budget=10 ** 6), "https://go.boarddocs.com/ga/test/Board.nsf/Public")
    for rec in bd2.records("sd:boarddocs", "sd", store=store, modified_since="2026-11-18T00:00:00+00:00"):
        store.add(rec)
    assert store.get_meta("sd:boarddocs|meeting|boarddocs|MEET2018AAA")["items"] == 2
    assert len(store.read("sd:boarddocs", "meeting")) == 8
    store.close()
