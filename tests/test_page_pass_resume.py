"""A page pass larger than one share of a run carries on where it stopped and completes: every page read once.

Nov. 14-19, 2026: 18 city and county sites (Duluth, Valdosta, Rome, ...) and a hundred other page sources were
"partial" on every run. Each share began at the home page with an empty queue, read the same listing pages and ran out
of time. The pass now keeps its queue as a cursor (channels.FRONTIER / SITEMAP_LEFT / WP_PAGE)."""
import http.server
import json
import threading
import time
from collections import Counter

import pytest
import requests

from gacrawl import channels
from gacrawl.fetch import Fetcher, OutOfTime
from gacrawl.scheduler import Progress, run_sources, stuck
from gacrawl.shard import merge_delta, write_delta
from gacrawl.store import Store, utcnow

TEXT = "<p>" + "The council met and adopted the budget for the coming year. " * 8 + "</p>"


def make_site(sections: int, per: int, deep: int = 0) -> dict[str, bytes]:
    """A home page linking to `sections` listing pages, each linking to `per` pages; `deep` pages under each of those.
    Every page links back to the home page and to its neighbours, as a site's menu does."""
    site = {}
    menu = "".join(f'<a href="/agenda-{s}">Agendas {s}</a> ' for s in range(sections))
    site["/"] = f"<html><title>Home</title><body>{menu}{TEXT}</body></html>".encode()
    for s in range(sections):
        links = "".join(f'<a href="/agenda-{s}/item-{i}">Item {i}</a> ' for i in range(per))
        site[f"/agenda-{s}"] = f"<html><title>Agendas {s}</title><body>{menu}{links}{TEXT} section {s}</body></html>".encode()
        for i in range(per):
            below = "".join(f'<a href="/agenda-{s}/item-{i}/part-{k}">Part {k}</a> ' for k in range(deep))
            site[f"/agenda-{s}/item-{i}"] = (f"<html><title>Item {s}.{i}</title><body>{menu}{below}"
                                             f'<a href="/agenda-{s}/item-{(i + 1) % per}">next</a>{TEXT} item {s} {i}</body></html>').encode()
            for k in range(deep):
                site[f"/agenda-{s}/item-{i}/part-{k}"] = f"<html><title>Part {s}.{i}.{k}</title><body>{menu}{TEXT} part {s} {i} {k}</body></html>".encode()
    return site


class Server:
    def __init__(self, site: dict[str, bytes], extra=None):
        self.site, self.hits, self.extra = site, [], extra or {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                path = self.path.split("?")[0]
                body, ctype = outer.site.get(path), "text/html; charset=utf-8"
                if path in outer.extra:
                    ctype, body = outer.extra[path]
                    body = body.replace(b"HOST", self.headers["Host"].encode())
                if body is None:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def pages(self) -> Counter:
        return Counter(h for h in self.hits if h != "/robots.txt")

    def close(self):
        self.srv.shutdown()


class Budget:
    """A fetcher that allows `per_share` page requests and then says the share's time is up."""

    def __init__(self, fetcher, per_share):
        self.f, self.per_share, self.left = fetcher, per_share, per_share

    def __getattr__(self, name):
        return getattr(self.f, name)

    def get(self, url, **kw):
        if self.left <= 0:
            raise OutOfTime("this source's time slice used up")
        self.left -= 1
        return self.f.get(url, **kw)


def new_fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def run_pass(fetcher, store, src, chosen, per_share, max_shares=200, limits=None, modified_since=None):
    """Shares of `per_share` requests until the crawl completes, the way the scheduler runs them. -> (shares, records)"""
    f = Budget(fetcher, per_share)
    records, shares = [], 0
    while shares < max_shares:
        shares += 1
        f.left = per_share
        ctx = channels.CrawlContext(frontier=store, cursors=store.cursors(src["id"]), resume=True,
                                    probes=src.get("_probes") or [])
        try:
            for item in channels.crawl(f, src, chosen, limits or {}, modified_since=modified_since, ctx=ctx):
                if isinstance(item, channels.Checkpoint):
                    store.set_cursor(src["id"], item.stream, item.value)
                else:
                    records.append(item)
                    store.add(item)
        except OutOfTime:
            continue
        return shares, records
    raise AssertionError(f"the pass did not complete in {max_shares} shares")


def test_a_site_larger_than_one_share_is_read_once_over_several_shares(tmp_path):
    site = make_site(sections=6, per=9, deep=2)          # 1 + 6 + 54 + 108 = 169 pages
    srv = Server(site)
    store = Store(tmp_path / "data")
    src = {"id": "duluth:site", "url": srv.base + "/", "kind": "page", "depth": 3}
    try:
        shares, recs = run_pass(new_fetcher(tmp_path), store, src, {"channel": "html", "endpoint": src["url"]}, per_share=20)
        assert shares >= 8                                 # it did not fit one share, nor five
        hits = srv.pages()
        assert set(hits) == set(site) and max(hits.values()) == 1, hits.most_common(3)    # every page, exactly once
        assert Counter(r.url for r in recs).most_common(1)[0][1] == 1 and len(recs) == len(site)
        assert store.cursors("duluth:site")[channels.FRONTIER] == ""          # the pass is over: nothing to resume
    finally:
        srv.close(); store.close()


def test_without_the_cursor_the_same_site_never_finished(tmp_path):
    """The old behaviour, for the record (resume off): every share begins at the home page again, and a site whose
    listing pages alone fill a share never gets below them. With the cursor the same site completes."""
    site = make_site(sections=25, per=3)                  # 26 listing pages, 20 requests a share
    srv = Server(site)
    store = Store(tmp_path / "data")
    src = {"id": "duluth:site", "url": srv.base + "/", "kind": "page", "depth": 3}
    try:
        f = Budget(new_fetcher(tmp_path), 20)
        for _ in range(12):
            f.left = 20
            f.f._memo.clear()
            with pytest.raises(OutOfTime):
                list(channels.crawl(f, src, {"channel": "html", "endpoint": src["url"]}, {}, ctx=channels.CrawlContext(frontier=store)))
        assert set(srv.pages()) == {"/"} | {f"/agenda-{i}" for i in range(19)} and srv.pages()["/"] == 12
        srv.hits.clear()
        store2 = Store(tmp_path / "data2")
        shares, _ = run_pass(new_fetcher(tmp_path / "b"), store2, src, {"channel": "html", "endpoint": src["url"]}, per_share=20)
        assert shares == 6 and set(srv.pages()) == set(site) and max(srv.pages().values()) == 1
        store2.close()
    finally:
        srv.close(); store.close()


def test_the_page_count_of_a_pass_runs_over_its_shares_and_listing_rereads_are_free(tmp_path):
    """max_pages is the pass's, not the share's; and on the next pass the listing pages (read again for new links) do
    not use it up: a town's 40 pages a pass went on its home page's 40 links, every pass."""
    site = make_site(sections=10, per=6)                  # 1 + 10 + 60
    srv = Server(site)
    store = Store(tmp_path / "data")
    src = {"id": "t:site", "url": srv.base + "/", "kind": "page", "depth": 3, "max_pages": 30}
    chosen = {"channel": "html", "endpoint": src["url"]}
    try:
        f = new_fetcher(tmp_path)
        shares, recs = run_pass(f, store, src, chosen, per_share=7)
        assert shares >= 4 and len(srv.pages()) == 30 and max(srv.pages().values()) == 1     # 30 pages, over 5 shares
        first = set(srv.pages())
        srv.hits.clear(); f._memo.clear()
        run_pass(f, store, src, chosen, per_share=1000)    # the next pass: 11 listing pages again, and 30 new ones
        second = srv.pages()
        assert len(set(second) - first) == 30 and {"/", "/agenda-0", "/agenda-9"} <= set(second)
        srv.hits.clear(); f._memo.clear()
        run_pass(f, store, src, chosen, per_share=1000)    # the third reaches the last of the 71 pages
        assert first | set(second) | set(srv.pages()) == set(site)
    finally:
        srv.close(); store.close()


def test_a_share_that_reads_nothing_leaves_the_cursor_as_it_was(tmp_path):
    srv = Server(make_site(4, 4))
    store = Store(tmp_path / "data")
    src = {"id": "t:site", "url": srv.base + "/", "kind": "page", "depth": 3}
    try:
        f = Budget(new_fetcher(tmp_path), 6)
        ctx = channels.CrawlContext(frontier=store, cursors={}, resume=True)
        out = []
        with pytest.raises(OutOfTime):
            for item in channels._html_crawl(f, src, 200, ctx):
                out.append(item)
        cps = [i for i in out if isinstance(i, channels.Checkpoint)]
        assert len(cps) == 1 and json.loads(cps[0].value)["n"] == 6 and len(json.loads(cps[0].value)["q"]) >= 1
        f.left = 0                                          # the host's turn never comes
        ctx2 = channels.CrawlContext(frontier=store, cursors={channels.FRONTIER: cps[0].value}, resume=True)
        out2 = []
        with pytest.raises(OutOfTime):
            for item in channels._html_crawl(f, src, 200, ctx2):
                out2.append(item)
        assert out2 == []                                   # no Checkpoint: nothing moved (the stall rule sees that)
        # a fresh pass that read nothing writes no cursor either
        out3 = []
        with pytest.raises(OutOfTime):
            for item in channels._html_crawl(f, src, 200, channels.CrawlContext(frontier=store, cursors={}, resume=True)):
                out3.append(item)
        assert out3 == []
    finally:
        srv.close(); store.close()


def test_a_cursor_of_another_address_or_an_old_pass_starts_over(tmp_path):
    ctx = channels.CrawlContext(resume=True, cursors={channels.FRONTIER: channels._pass_value("http://old/", utcnow(), 3, [["http://old/a", 1, 1]])})
    assert channels._pass_load(ctx, channels.FRONTIER, "http://new/") is None
    assert channels._pass_load(ctx, channels.FRONTIER, "http://old/")["n"] == 3
    ctx.cursors[channels.FRONTIER] = channels._pass_value("http://old/", "2026-01-01T00:00:00+00:00", 3, [["http://old/a", 1, 1]])
    assert channels._pass_load(ctx, channels.FRONTIER, "http://old/") is None
    ctx.cursors[channels.FRONTIER] = "not json"
    assert channels._pass_load(ctx, channels.FRONTIER, "http://old/") is None
    big = channels._pass_value("http://old/", utcnow(), 0, [[f"http://old/{i}", 2, 0] for i in range(channels.PASS_MAX_URLS + 500)])
    assert len(json.loads(big)["q"]) == channels.PASS_MAX_URLS and json.loads(big)["cut"] == 1 and len(big) < 400_000


def test_a_sitemap_pass_resumes_without_reading_the_sitemap_again(tmp_path):
    site = make_site(sections=5, per=10)                  # 56 pages
    locs = "".join(f"<url><loc>http://HOST{p}</loc></url>" for p in sorted(site))
    extra = {"/sitemap.xml": ("application/xml", f'<?xml version="1.0"?><urlset>{locs}</urlset>'.encode())}
    srv = Server(site, extra)
    store = Store(tmp_path / "data")
    src = {"id": "valdosta:site", "url": srv.base + "/", "kind": "page"}
    try:
        shares, recs = run_pass(new_fetcher(tmp_path), store, src, {"channel": "sitemap", "endpoint": srv.base + "/sitemap.xml"}, per_share=9)
        hits = srv.pages()
        assert shares >= 6 and hits.pop("/sitemap.xml") == 1                     # the list was read once, not every share
        assert set(hits) == set(site) and max(hits.values()) == 1
        cur = store.cursors("valdosta:site")
        assert cur[channels.SITEMAP_LEFT] == "" and cur.get("sitemap_read")       # whole list read: marked as before
    finally:
        srv.close(); store.close()


def test_a_feed_site_gets_to_its_own_pages(tmp_path):
    """Rome, South Fulton, Waycross, Brookhaven: a news feed, then the site's own pages (_coverage). The feed is read
    on every share; the page pass behind it carries on."""
    site = make_site(sections=4, per=8)                   # 37 pages
    feed = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>News</title><item><title>Budget hearing</title>'
            b"<link>http://HOST/agenda-0/item-1</link><guid>g1</guid><pubDate>Mon, 02 Nov 2026 19:00:00 GMT</pubDate>"
            b"<description>Hearing.</description></item></channel></rss>")
    srv = Server(site, {"/feed.xml": ("application/rss+xml", feed)})
    store = Store(tmp_path / "data")
    src = {"id": "rome:site", "url": srv.base + "/", "kind": "page", "depth": 3, "coverage": True}
    try:
        f = new_fetcher(tmp_path)
        f.get = (lambda real: lambda url, **kw: real(url, **{**kw, "use_cache": False} if url.endswith("feed.xml") else kw))(f.get)
        shares, recs = run_pass(f, store, src, {"channel": "feed", "endpoint": srv.base + "/feed.xml"}, per_share=8)
        hits = srv.pages()
        assert shares >= 5 and hits.pop("/feed.xml") == shares
        assert set(hits) == set(site) and max(hits.values()) == 1
        dated = [r for r in recs if r.url.endswith("/agenda-0/item-1")]
        assert len(dated) == 1 and dated[0].published.startswith("2026-11-02")    # the feed's own date
    finally:
        srv.close(); store.close()


def test_a_first_reading_of_a_posts_api_carries_on_at_its_page(tmp_path):
    posts = [{"id": i, "link": f"http://x/p{i}", "date": "2026-10-01T10:00:00", "modified_gmt": "2026-10-02T10:00:00",
              "title": {"rendered": f"Post {i}"}, "content": {"rendered": f"<p>Post number {i} of the city.</p>"}} for i in range(230)]
    asked = []

    class R:
        def __init__(self, status, rows):
            self.status, self._rows, self.content_type = status, rows, "application/json"

        def json(self):
            return self._rows

    class F:
        left = 2

        def get(self, url, params=None, **kw):
            if self.left <= 0:
                raise OutOfTime("this source's time slice used up")
            self.left -= 1
            asked.append(params["page"])
            rows = posts[(params["page"] - 1) * 50: params["page"] * 50]
            return R(200 if rows else 400, rows)
    store = Store(tmp_path / "data")
    src = {"id": "co-butts:elections", "url": "http://x"}
    f = F()
    seen = []
    for share in range(10):
        f.left = 2
        ctx = channels.CrawlContext(frontier=store, cursors=store.cursors(src["id"]), resume=True)
        try:
            for item in channels.crawl(f, src, {"channel": "wp_json", "endpoint": "http://x/wp-json/wp/v2/posts"}, {}, ctx=ctx):
                if isinstance(item, channels.Checkpoint):
                    store.set_cursor(src["id"], item.stream, item.value)
                else:
                    seen.append(item.url)
        except OutOfTime:
            continue
        break
    assert asked == [1, 2, 3, 4, 5, 6] and len(seen) == len(set(seen)) == 230      # no page of posts asked for twice
    assert store.cursors(src["id"])[channels.WP_PAGE] == ""
    store.close()


# ----------------------------------------------------------------------------- through the scheduler
def slow_site_fetcher(tmp_path, delay):
    f = new_fetcher(tmp_path)
    real = f._get

    def slow(full, headers, use_cache, kind=None):
        time.sleep(delay)
        return real(full, headers, use_cache, kind)
    f._get = slow
    return f


def test_the_scheduler_carries_a_pass_over_shares_and_runs_to_ok(tmp_path):
    """Through run_sources: the share ends on its time slice, the cursor is stored, the spare shares of the same run
    and the next run carry on, the source completes, and it was never counted as stuck although it stored nothing new
    after its first run."""
    site = make_site(sections=5, per=7)                   # 41 pages: Duluth's "41 seen"
    srv = Server(site)
    store, cache, prog = Store(tmp_path / "data"), channels.ChannelCache(tmp_path / "c.json"), Progress(tmp_path / "p.json")
    src = {"id": "duluth:site", "url": srv.base + "/", "tier": "a", "channel": "html", "endpoint": srv.base + "/", "depth": 3}
    cfg = {"schedule": {"windows": []}}
    try:
        f = slow_site_fetcher(tmp_path, 0.03)
        lines = []
        run_sources(cfg, [src], f, store, cache, prog, time.monotonic() + 120, force=True, workers=1,
                    slice_minutes=0.25 / 60, log=lines.append)                    # a quarter second a share: about 8 pages
        p = prog.get("duluth:site")
        runs = 1
        while p["status"] != "ok" and runs < 12:
            assert p["status"].startswith("partial") and not stuck(p) and p.get("moved"), p
            runs += 1
            run_sources(cfg, [src], f, store, cache, prog, time.monotonic() + 120, force=True, workers=1,
                        slice_minutes=0.25 / 60, log=lines.append)
            p = prog.get("duluth:site")
        assert p["status"] == "ok" and not p.get("stalled"), (p, lines[-3:])
        hits = srv.pages()
        assert set(hits) == set(site) and max(hits.values()) == 1, hits.most_common(3)
        assert store.total() == len(site)
        assert store.cursors("duluth:site") == {channels.FRONTIER: ""}             # kept, emptied (see the next test)
    finally:
        srv.close(); store.close()


def test_a_second_reading_of_stored_pages_is_progress_not_a_stall(tmp_path):
    """Every page is stored already and unchanged ("0 new"): the cursor still moves each share, so the source keeps
    its shares and completes. This is the case the stall rule backed off (0 new / 41 seen, share after share)."""
    site = make_site(sections=5, per=7)
    srv = Server(site)
    store, cache, prog = Store(tmp_path / "data"), channels.ChannelCache(tmp_path / "c.json"), Progress(tmp_path / "p.json")
    src = {"id": "duluth:site", "url": srv.base + "/", "tier": "a", "channel": "html", "endpoint": srv.base + "/", "depth": 3}
    cfg = {"schedule": {"windows": []}, "frontier": {"page_revisit_days": 0}}      # every page is read again
    try:
        run_sources(cfg, [src], new_fetcher(tmp_path), store, cache, prog, time.monotonic() + 120, force=True, workers=1,
                    log=lambda *_: None)
        assert prog.get("duluth:site")["status"] == "ok" and store.total() == len(site)
        srv.hits.clear()
        f = slow_site_fetcher(tmp_path, 0.03)
        lines = []
        out = run_sources(cfg, [src], f, store, cache, prog, time.monotonic() + 900, force=True, workers=1, full=True,
                          slice_minutes=0.25 / 60, log=lines.append)
        p = prog.get("duluth:site")
        assert p["status"] == "ok" and p["new"] == 0, (p, lines)                   # completed inside the run's 8 passes
        assert any("carry on" in ln for ln in lines) and not any("stuck" in ln for ln in lines)
        assert max(srv.pages().values()) == 1 and set(srv.pages()) == set(site)
    finally:
        srv.close(); store.close()


def test_a_pass_that_cannot_advance_still_backs_off(tmp_path, monkeypatch):
    """A cursor is stored, but no share reads a page (the host's turn never comes inside the share): the cursor does
    not move, and after STALL_SHARES such shares the source is stuck, as before."""
    from gacrawl.scheduler import STALL_SHARES

    store, cache, prog = Store(tmp_path / "data"), channels.ChannelCache(tmp_path / "c.json"), Progress(tmp_path / "p.json")
    src = {"id": "x:site", "url": "http://127.0.0.1:9/", "tier": "a", "channel": "html", "endpoint": "http://127.0.0.1:9/"}
    store.set_cursor("x:site", channels.FRONTIER, channels._pass_value(src["url"], utcnow(), 5, [[src["url"] + "a", 1, 1]]))
    f = new_fetcher(tmp_path)

    def never(url, **kw):
        while True:
            time.sleep(0.02)
            f._check_time()
    monkeypatch.setattr(f, "get", never)
    lines = []
    for _ in range(3):
        run_sources({"schedule": {"windows": []}}, [src], f, store, cache, prog, time.monotonic() + 60, force=True,
                    workers=1, slice_minutes=0.002, log=lines.append)
    p = prog.get("x:site")
    assert p["stalled"] >= STALL_SHARES and stuck(p) and not p.get("moved"), p
    store.close()


def test_the_emptied_cursor_reaches_the_main_database(tmp_path):
    """A runner's delta carries rows, not deletions: a cursor deleted on the runner stayed in the main database, and
    the next pass would have begun in the middle of the last one's queue. The emptied cursor is a row."""
    main, shard = Store(tmp_path / "main"), Store(tmp_path / "shard")
    old = channels._pass_value("http://x/", "2026-11-18T00:00:00+00:00", 9, [["http://x/a", 1, 1]])
    for s in (main, shard):
        s.db.execute("INSERT INTO cursors VALUES ('x:site', ?, ?, '2026-11-18T00:00:00+00:00')", (channels.FRONTIER, old))
    shard.set_cursor("x:site", channels.FRONTIER, "")
    shard.clear_cursors("x:site", keep=("full", "sitemap_read") + channels.PASS_STREAMS)
    write_delta(shard, "2026-11-19T00:00:00+00:00", tmp_path / "d.db")
    merge_delta(main, tmp_path / "d.db")
    assert main.cursors("x:site") == {channels.FRONTIER: ""}
    main.close(); shard.close()
