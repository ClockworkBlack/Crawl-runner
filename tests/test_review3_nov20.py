"""Third review of the Nov. 20, 2026 crawler-core changes: a host that served a few pages and then refuses everything
is still left alone (at a higher count), for every caller; a pass that gave up on a host is not sent back to it in
the same run; pages that are gone do not stop a pass; redirect hops within a host keep to the host's rate."""
import time

import pytest
import requests

from gacrawl import channels
from gacrawl.blocked import Blocked
from gacrawl.fetch import Fetcher
from gacrawl.polite import RateLimiter
from gacrawl.store import Store
from test_barriers import fetcher, page, serve

REJECTED = (403, "text/html", b"<html><body>" + b"<p>Request rejected.</p>" * 120 + b"</body></html>")
ROBOTS_OK = (200, "text/plain", b"User-agent: *\nDisallow:\n")


class Then(dict):
    """Serves `ok` requests (robots.txt apart), then answers everything with `after`."""

    def __init__(self, ok, after=REJECTED, home=None):
        super().__init__()
        self.ok, self.after, self.home = ok, after, home

    def get(self, key, default=None):
        if key == "/robots.txt":
            return ROBOTS_OK
        if self.ok > 0:
            self.ok -= 1
            return self.home if key == "/" and self.home else page("")
        return self.after


# ----------------------------------------------------------------------------- 1. counting
def test_a_host_that_served_and_then_refuses_everything_is_left_alone_after_twelve(tmp_path):
    srv, base, hits = serve(Then(1))
    try:
        f = fetcher(tmp_path)
        assert f.get(base + "/first").status == 200
        asked = 0
        with pytest.raises(Blocked):
            for i in range(300):                                        # a reader with a list of files, not a page pass
                asked += 1
                assert f.get(base + f"/docs/file{i}.pdf").status == 403
        assert asked == Fetcher.SERVED_REFUSALS_IN_A_ROW == 12
        assert f.blocklist.data[base.split("//")[1]]["ttl_days"] == 1.0
    finally:
        srv.shutdown()


def test_a_page_that_answers_between_refusals_starts_the_count_again(tmp_path):
    srv, base, hits = serve({"/robots.txt": ROBOTS_OK, **{f"/ok{i}": page("") for i in range(10)},
                             **{f"/staff/{i}-{k}": REJECTED for i in range(10) for k in range(11)}})
    srv2, base2, _ = serve(Then(0))
    try:
        f = fetcher(tmp_path)
        for i in range(10):                                             # 110 refusals, never twelve in a row
            assert f.get(base + f"/ok{i}").status == 200
            for k in range(11):
                assert f.get(base + f"/staff/{i}-{k}").status == 403
        assert not f.blocklist.data
        f2 = fetcher(tmp_path / "b")
        f2._served.add(base2)
        for i in range(11):
            assert f2.get(base2 + f"/x{i}").status == 403
        f2._answered(base2 + "/y")                                      # a 200 in between
        for i in range(11):
            assert f2.get(base2 + f"/z{i}").status == 403
        assert not f2.blocklist.data
    finally:
        srv.shutdown(); srv2.shutdown()


def test_a_data_services_refusals_are_counted_too_at_the_higher_count(tmp_path):
    srv, base, hits = serve(Then(0, after=(403, "application/json", b'{"error": "forbidden"}')))
    try:
        f = fetcher(tmp_path)
        asked = 0
        with pytest.raises(Blocked):
            for i in range(50):
                asked += 1
                assert f.get(base + f"/api/v1/things/{i}", headers={"Accept": "application/json"}).status == 403
        assert asked == 12
    finally:
        srv.shutdown()


def test_a_refused_front_door_blocks_a_served_host_for_a_day_and_an_unserved_one_for_the_lists_time(tmp_path):
    f = fetcher(tmp_path)
    assert f._host_refused("https://x.gov/", "HTTP 403 refused", 403) == Fetcher.BLOCK_DAYS == 0.0     # 0: the list's own 7 days
    f._served.add("https://y.gov")
    assert f._host_refused("https://y.gov/", "HTTP 403 refused", 403) == 1.0
    assert f._host_refused("https://web.archive.org/", "HTTP 403 refused", 403) == 0.0                 # the archive's front door too


def test_a_304_or_a_challenge_served_as_200_does_not_make_a_host_one_that_serves(tmp_path):
    etag = {"n": 0}

    class R(dict):
        def get(self, key, default=None):
            return ROBOTS_OK if key == "/robots.txt" else None
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nDisallow:\n"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path == "/cached":
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304); self.end_headers(); return
                body = b"<html><body>" + b"cached page " * 30 + b"</body></html>"
                self.send_response(200); self.send_header("ETag", '"v1"'); self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        warm = fetcher(tmp_path)
        assert warm.get(base + "/cached").status == 200 and base in warm._served
        f = fetcher(tmp_path)                                           # the next run: the same cache, answered with 304
        r = f.get(base + "/cached")
        assert r.not_modified and base not in f._served
        with pytest.raises(Blocked):
            for i in range(6):
                f.get(base + f"/p{i}")                                  # five in a row: it has served nothing this run
    finally:
        srv.shutdown()


def test_file_stores_and_the_archive_are_counted_at_twenty_and_three_sites_are_not_on_the_list(tmp_path):
    assert not {"scribd.com", "issuu.com", "box.com"} & set(Fetcher.SHARED_HOSTS)
    f = fetcher(tmp_path)
    for url in ("https://bucket.s3.amazonaws.com/a/b.pdf", "https://web.archive.org/web/2020/http://x.gov/a"):
        got = [f._host_refused(url, "HTTP 403 refused", 403, is_doc=True) for _ in range(20)]
        assert got[:19] == [None] * 19 and got[19] == 1.0, url
    assert [f._host_refused("https://www.scribd.com/doc/1", "HTTP 403 refused", 403) for _ in range(5)][-1] == 1.0


# ----------------------------------------------------------------------------- the page passes
def test_a_pass_that_gave_up_on_a_host_is_not_sent_back_to_it_in_the_same_run(tmp_path):
    home = page("".join(f'<a href="/p{i}">p</a> ' for i in range(120)))
    srv, base, hits = serve(Then(3, home=home))
    store = Store(tmp_path / "db")
    src = {"id": "s", "url": base + "/", "kind": "page", "depth": 3}
    try:
        f = fetcher(tmp_path)
        per_share = []
        for share in range(8):
            hits.clear()
            with pytest.raises(channels.ChannelDown):
                for it in channels._html_crawl(f, src, 80, channels.CrawlContext(frontier=store, cursors=store.cursors("s"), resume=True)):
                    if isinstance(it, channels.Checkpoint):
                        store.set_cursor("s", it.stream, it.value)
            per_share.append(len([h for h in hits if h != "/robots.txt"]))
        assert per_share == [8, 5, 0, 0, 0, 0, 0, 0]                    # it was 43 requests over the eight shares
        g = fetcher(tmp_path / "next")                                   # the next run asks again, and the count blocks it
        hits.clear()
        with pytest.raises(Blocked):
            list(channels._html_crawl(g, src, 80, channels.CrawlContext(frontier=store, cursors=store.cursors("s"), resume=True)))
        assert len([h for h in hits if h != "/robots.txt"]) == 5
    finally:
        srv.shutdown(); store.close()


def test_pages_that_are_gone_do_not_stop_a_pass(tmp_path):
    """A stale sitemap: most of its addresses answer 404 or 410. The pass works through them (and ends as it would)."""
    locs = "".join(f"<url><loc>http://HOST/old{i}</loc></url>" for i in range(30)) + "<url><loc>http://HOST/live</loc></url>"
    routes = {"/sitemap.xml": (200, "application/xml", f"<urlset>{locs}</urlset>".encode()), "/live": page(""),
              **{f"/old{i}": (410 if i % 2 else 404, "text/html", b"gone") for i in range(30)}}
    srv, base, hits = serve(routes)
    try:
        ctx = channels.CrawlContext(frontier=Store(tmp_path / "db"), resume=True)
        out = list(channels.crawl(fetcher(tmp_path), {"id": "s", "url": base + "/"}, {"channel": "sitemap", "endpoint": base + "/sitemap.xml"}, {}, ctx=ctx))
        assert [r.url for r in out if not isinstance(r, channels.Checkpoint)] == [base + "/live"]
        s = channels._Streak()
        for status in (404, 410, 404, 404, 410, 404, 404):
            s.note(status)
        with pytest.raises(channels.ChannelDown):
            for status in (500, 403, 404, 502, 403, 500):
                s.note(status)
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------- 2. redirect hops and the host's rate
def hop_site(robots: bytes):
    def chain(i):
        return ("redirect", f"/c{i + 1}") if i < 5 else ("redirect", "/end")
    return {"/robots.txt": (200, "text/plain", robots), "/end": page(""), "/a": ("redirect", "/a/"), "/a/": page(""),
            **{f"/c{i}": chain(i) for i in range(6)}}


def test_a_hop_within_a_host_waits_a_stated_crawl_delay(tmp_path):
    srv, base, hits = serve(hop_site(b"User-agent: *\nCrawl-delay: 9\n"))
    try:
        f = fetcher(tmp_path)
        waits = []
        real = f.limiter.wait
        f.limiter.wait = lambda url, delay=None, until=None: waits.append((url[len(base):], delay)) or real(url, 0, until=until)
        assert f.get(base + "/c0").status == 200
        assert [w for w in waits if w[0] != "/robots.txt"] == [(f"/c{i}", 9.0) for i in range(6)] + [("/end", 9.0)]
    finally:
        srv.shutdown()


def test_without_a_crawl_delay_each_hop_is_charged_to_the_hosts_spacing(tmp_path):
    srv, base, hits = serve(hop_site(b"User-agent: *\nDisallow:\n"))
    try:
        s = requests.Session(); s.trust_env = False
        clock = {"t": 1000.0}
        f = Fetcher("Test/1", tmp_path / "state", min_interval=3.0, session=s, sleep=lambda x: clock.__setitem__("t", clock["t"] + x))
        f.limiter._clock = lambda: clock["t"]
        f.get(base + "/robots.txt")
        t0 = clock["t"]
        assert f.get(base + "/c0").status == 200                        # seven requests: one waited for, six hops charged
        f.get(base + "/a/")                                             # the next page's turn comes after all seven
        assert clock["t"] - t0 >= 7 * 3.0
        lim = RateLimiter(2.0, sleep=lambda x: None, clock=lambda: 50.0)
        lim.charge("http://h/x"); lim.charge("http://h/y")
        assert lim._state("http://h").next_ok == 54.0
    finally:
        srv.shutdown()
