"""Second review of the Nov. 20, 2026 crawler-core changes: refusals are counted only against a host that serves
nothing, a link into someone else's refusing host is one address and not the source, and the redirect follower reads
and sends what `requests` did."""
import datetime as dt
import http.server
import threading

import pytest

from gacrawl import channels
from gacrawl.blocked import Blocked, BlockList
from gacrawl.polite import Robots
from gacrawl.scheduler import Progress, due
from gacrawl.store import Store
from test_barriers import fetcher, page, serve

UA = "GACivicCrawler/0.3 (+https://github.com/ClockworkBlack/Crawl)"
F403 = (403, "text/html", b"<h1>Forbidden</h1>")
S3 = (403, "application/xml", b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>")
U401 = (401, "text/html", b"<h1>Authorization Required</h1>")
ROBOTS_OK = (200, "text/plain", b"User-agent: *\nDisallow:\n")


def site(robots, links, extra):
    routes = {"/": page("".join(f'<a href="/p{i}">p{i}</a> ' for i in range(12)) + links)}
    routes.update({f"/p{i}": page("") for i in range(12)})
    routes.update(extra)
    if robots:
        routes["/robots.txt"] = robots
    return serve(routes)


def crawl(tmp_path, base, n=80):
    f = fetcher(tmp_path)
    ctx = channels.CrawlContext(frontier=Store(tmp_path / "db"), resume=True)
    return f, ctx, channels._html_crawl(f, {"id": "s", "url": base + "/", "kind": "page", "depth": 3}, n, ctx)


# ----------------------------------------------------------------------------- A. refusals
@pytest.mark.parametrize("label,robots,links,extra", [
    ("robots.txt forbidden, a healthy site, one staff-only link", F403, '<a href="/zstaff/handbook">s</a>', {"/zstaff/handbook": F403}),
    ("robots.txt served, four protected documents", ROBOTS_OK, "".join(f'<a href="/intranet/m{i}.pdf">m</a>' for i in range(4)),
     {f"/intranet/m{i}.pdf": U401 for i in range(4)}),
    ("no robots.txt, four files gone from a file store", None, "".join(f'<a href="/files/a{i}.pdf">m</a>' for i in range(4)),
     {f"/files/a{i}.pdf": S3 for i in range(4)}),
])
def test_refused_pages_of_a_site_that_serves_its_pages_do_not_block_it(tmp_path, label, robots, links, extra):
    srv, base, hits = site(robots, links, extra)
    try:
        f, ctx, it = crawl(tmp_path, base)
        recs = [r for r in it if not isinstance(r, channels.Checkpoint)]
        assert len(recs) == 13 and not f.blocklist.data, label          # every page read, the host not on the block list
    finally:
        srv.shutdown()


def test_five_protected_documents_in_a_row_stop_the_pass_not_the_host(tmp_path):
    links = "".join(f'<a href="/intranet/minutes{i}.pdf">m</a>' for i in range(5))
    srv, base, hits = site(ROBOTS_OK, links, {f"/intranet/minutes{i}.pdf": U401 for i in range(5)})
    try:
        f, ctx, it = crawl(tmp_path, base)
        with pytest.raises(channels.ChannelDown):                       # the pass waits (its cursor kept) ...
            list(it)
        assert not f.blocklist.data and f.get(base + "/p3").status == 200     # ... the host is not blocked
    finally:
        srv.shutdown()


def test_a_file_host_with_a_forbidden_robots_txt_is_not_blocked_by_a_dead_link(tmp_path):
    srv, base, hits = serve({"/robots.txt": S3, "/docs/a.pdf": (200, "text/plain", b"fine"), "/docs/gone.pdf": S3, "/docs/gone2": S3})
    try:
        f = fetcher(tmp_path)
        assert f.get(base + "/docs/a.pdf").status == 200
        assert f.get(base + "/docs/gone.pdf").status == 403 and f.get(base + "/docs/gone2").status == 403
        assert not f.blocklist.data
        # asked first, before anything was served: a missing file (a document's address) is still not the host's refusal
        g = fetcher(tmp_path / "g")
        assert g.get(base + "/docs/gone.pdf").status == 403 and g.get(base + "/docs/a.pdf").status == 200 and not g.blocklist.data
    finally:
        srv.shutdown()


def test_a_host_that_serves_nothing_is_blocked_for_a_day_and_a_front_door_or_challenge_for_seven(tmp_path):
    srv, base, hits = serve({"/robots.txt": F403, "/staff/a": F403})
    try:
        f = fetcher(tmp_path)
        with pytest.raises(Blocked):
            f.get(base + "/staff/a")                                    # robots.txt refused, the first page too, nothing served
        rec = f.blocklist.data[base.split("//")[1]]
        assert rec["ttl_days"] == 1.0 and len(hits) == 2
    finally:
        srv.shutdown()
    bl = BlockList(tmp_path / "b.json", ttl_days=7)
    bl.add("https://counted.example/x", "HTTP 403 refused", ttl_days=1)
    bl.add("https://front.example/", "HTTP 403 access denied")
    bl.add("https://wall.example/x", "HTTP 403 bot-protection challenge")
    two_days_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
    for rec in bl.data.values():
        rec["last_seen"] = two_days_ago
    assert bl.is_blocked("https://counted.example/y") is None           # tried again after a day
    assert bl.is_blocked("https://front.example/y") and bl.is_blocked("https://wall.example/y")
    bl.add("https://counted.example/", "HTTP 403 bot-protection challenge")       # a challenge met later: the full time again
    assert "ttl_days" not in bl.data["counted.example"]
    # the scheduler looks at a counted block again after a day, at a challenge after the source's cadence
    now = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)
    prog = Progress(tmp_path / "p.json")
    day = (now - dt.timedelta(days=2)).isoformat()
    prog.update("a:site", status="blocked: https://a.gov: HTTP 403 refused", last_run=day, last_ok=day)
    prog.update("b:site", status="blocked: https://b.gov: HTTP 403 bot-protection challenge", last_run=day, last_ok=day)
    srcs = [{"id": i, "url": "https://x/", "tier": "c"} for i in ("a:site", "b:site")]
    assert [s["id"] for s in due(srcs, prog, now=now)] == []            # (inside their cadence: last_ok two days ago)
    prog.update("a:site", last_ok="2026-09-01T00:00:00+00:00")
    prog.update("b:site", last_ok="2026-09-01T00:00:00+00:00")
    assert [s["id"] for s in due(srcs, prog, now=now)] == ["a:site"]


def test_refusals_of_a_shared_host_do_not_block_it_at_the_usual_count(tmp_path):
    """File stores and the archive, which many sources' links point into: counted at a higher bar
    (tests/test_review3_nov20.py); a challenge is one there too."""
    f = fetcher(tmp_path)
    for url in ("https://web.archive.org/web/2020/http://x.gov/a", "https://bucket.s3.amazonaws.com/a/b.pdf",
                "https://static.wixstatic.com/ugd/x.pdf", "https://d111.cloudfront.net/f.pdf"):
        for _ in range(8):
            assert f._host_refused(url, "HTTP 403 refused", 403) is None, url
    assert f._host_refused("https://web.archive.org/x", "HTTP 403 bot-protection challenge", 403) is not None


# ----------------------------------------------------------------------------- B. a link into a refusing host
def test_a_link_that_leads_to_a_blocked_host_is_passed_over_and_the_site_is_read(tmp_path):
    other, B, other_hits = serve({"/library": page("x")})
    town, A, town_hits = serve({"/": page('<a href="/ordinances">Code of Ordinances</a> ' + "".join(f'<a href="/dept{i}">d</a> ' for i in range(8))),
                                "/ordinances": ("redirect", B + "/library"), **{f"/dept{i}": page("") for i in range(8)}})
    store = Store(tmp_path / "db")
    src = {"id": "town:site", "url": A + "/", "kind": "page", "depth": 3}
    try:
        f = fetcher(tmp_path)
        f.blocklist.add(B + "/", "HTTP 403 bot-protection challenge", "muni")
        ctx = channels.CrawlContext(frontier=store, cursors={}, resume=True)
        got = [r.url[len(A):] for r in channels._html_crawl(f, src, 50, ctx) if not isinstance(r, channels.Checkpoint)]
        assert got == ["/"] + [f"/dept{i}" for i in range(8)]           # it stopped at "/" with Blocked, every share
        assert other_hits == [] and town_hits.count("/ordinances") == 1
        assert len(ctx.notes) == 1 and "/ordinances leads to" in ctx.notes[0] and store.url_seen(A + "/ordinances")["status"] == 403
    finally:
        other.shutdown(); town.shutdown(); store.close()


def test_a_challenge_met_behind_a_link_is_the_other_hosts_and_the_site_goes_on(tmp_path):
    wall, W, _ = serve({"/x": (403, "text/html", b"<html><title>Just a moment...</title><div class='cf-chl'></div></html>")})
    town, A, _ = serve({"/": page('<a href="/code">Code</a> <a href="/council">Council</a>'), "/code": ("redirect", W + "/x"), "/council": page("")})
    try:
        f = fetcher(tmp_path)
        ctx = channels.CrawlContext(frontier=Store(tmp_path / "db"), resume=True)
        got = [r.url[len(A):] for r in channels._html_crawl(f, {"id": "t:site", "url": A + "/", "kind": "page", "depth": 3}, 50, ctx)]
        assert got == ["/", "/council"] and list(f.blocklist.data) == [W.split("//")[1]]
        # the sitemap and feed readers' page fetch too
        assert channels._page_record(f, "t:site", A + "/code", {"id": "t:site"}, ctx) is None
    finally:
        wall.shutdown(); town.shutdown()


def test_the_sources_own_host_refusing_still_stops_it(tmp_path):
    srv, base, _ = serve({"/": page('<a href="/a">a</a>'), "/a": (403, "text/html", b"<html><title>Just a moment...</title><div class='cf-chl'></div></html>")})
    try:
        f, ctx, it = crawl(tmp_path, base)
        with pytest.raises(Blocked):
            list(it)
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------- C. the redirect follower
class H(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    hits: list = []

    def do_GET(self):
        H.hits.append((self.headers.get("Host"), self.path, self.headers.get("Authorization"), self.headers.get("X-Key"), self.headers.get("Cookie")))
        hit = H.routes.get(self.path.split("?")[0])
        if hit is None:
            self.send_response(404); self.end_headers(); return
        if hit[0] == "redirect":
            self.send_response(hit[2] if len(hit) > 2 else 301)
            loc = hit[1]
            self._headers_buffer.append(b"Location: " + (loc if isinstance(loc, bytes) else loc.encode()) + b"\r\n")
            self.end_headers(); return
        status, ctype, body = hit
        self.send_response(status); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def two_hosts():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    H.hits = []
    yield f"http://127.0.0.1:{port}", f"http://localhost:{port}"
    srv.shutdown()


def test_redirects_are_followed_as_requests_followed_them(tmp_path, two_hosts):
    A, B = two_hosts
    chain = {f"/chain{i}": ("redirect", f"/chain{i + 1}") for i in range(1, 10)}
    H.routes = {"/loop1": ("redirect", "/loop2"), "/loop2": ("redirect", "/loop1"), "/rel": ("redirect", "sub/page?x=1#frag"),
                "/sub/page": page("rel ok"), "/utf": ("redirect", "/café minutes.pdf".encode("utf-8")),
                "/caf%C3%A9%20minutes.pdf": (200, "text/plain", b"utf8 target ok"), "/x-host": ("redirect", B + "/landed"),
                "/landed": page("landed"), "/r307": ("redirect", "/sub/page", 307), **chain, "/chain10": ("redirect", "/sub/page"),
                "/long1": ("redirect", "/long2"), **{f"/long{i}": ("redirect", f"/long{i + 1}") for i in range(2, 14)},
                "/js": ("redirect", "javascript:void(0)")}
    f = fetcher(tmp_path)
    waits = []
    real = f.limiter.wait
    f.limiter.wait = lambda url, delay=None, until=None: waits.append(url) or real(url, delay, until=until)

    r = f.get(A + "/utf")                                               # the Location's bytes are UTF-8
    assert r.status == 200 and r.content == b"utf8 target ok"
    assert f.get(A + "/rel").final_url == A + "/sub/page?x=1" and f.get(A + "/r307").status == 200
    waits.clear()
    assert f.get(A + "/chain1").status == 200                           # ten hops are followed ...
    assert waits == [A + "/chain1"]                                     # ... and within one host only the first request waits its turn
    n = len(H.hits)
    with pytest.raises(RuntimeError, match="gave up on"):               # a loop: an error the callers skip, seen at once
        f.get(A + "/loop1")
    assert len(H.hits) - n == 2
    with pytest.raises(RuntimeError, match="gave up on"):               # more hops than that: the same
        f.get(A + "/long1")
    assert channels._try_get(f, A + "/loop1", None, "s") is None
    assert f.get(A + "/js").status == 301                               # not an address: the redirect is the answer

    # a hop to another host waits that host's turn, and does not carry the first host's credentials
    waits.clear(); H.hits.clear()
    r = f.get(A + "/x-host", headers={"Authorization": "Bearer SECRET", "Cookie": "sid=1", "X-Key": "k"})
    assert r.status == 200 and r.final_url == B + "/landed" and B + "/landed" in waits
    first = [h for h in H.hits if h[1] == "/x-host"][0]
    landed = [h for h in H.hits if h[1] == "/landed"][0]
    assert first[2] == "Bearer SECRET" and first[4] == "sid=1"
    assert landed[2] is None and landed[4] is None and landed[3] == "k"


# ----------------------------------------------------------------------------- also
def test_same_site_is_the_registered_domain_or_a_move_the_site_itself_announced():
    same = channels.same_site
    assert same({}, "cityofx.com", "www.cityofx.com") and same({}, "cityofx.com", "agendas.cityofx.com")
    assert same({}, "cityofx.com", "cityofx.gov") and same({}, "cityofx.com", "www.cityofx.org") and same({}, "cityofx.com", "cityofx.us")
    assert not same({}, "cityofx.com", "cityofx.gov", redirected=False)             # another ending only by the site's own redirect
    assert same({}, "cityofx.com", "docs.cityofx.com", redirected=False)             # one registered domain either way
    # a platform's sub-domains are separate sites, and the platform's own host is nobody's town
    assert not same({}, "town.govoffice.com", "www.govoffice.com") and not same({}, "town.govoffice.com", "othertown.govoffice.com")
    assert not same({}, "town.govoffice.com", "govoffice.com") and not same({}, "town.com", "town.govoffice.com")
    assert not same({}, "smalltown.wixsite.com", "www.wixsite.com") and not same({}, "x.municipalimpact.com", "y.municipalimpact.com")
    assert not same({}, "cityofx.com", "library.municode.com") and not same({}, "cityofx.com", "cityofx.municode.com")
    assert same({}, "town.govoffice.com", "www.town.govoffice.com")
    assert channels.registered("www.haralson.k12.ga.us") == ("haralson", "k12.ga.us") and channels.registered("govoffice.com") == ("", "")
    assert channels.registered("127.0.0.1:80") == ("", "") and channels.registered("10.0.0.1") == ("", "")
    assert same({"hosts": ["newname.gov"]}, "old.com", "newname.gov") and same({"website": "https://www.newname.gov/x"}, "old.com", "newname.gov")


def test_an_encoded_star_in_a_rule_is_a_star_in_the_address_not_a_wildcard():
    r = Robots("User-agent: *\nDisallow: /a%2Ab\n", UA)
    assert r.allowed("http://h/aXb") and not r.allowed("http://h/a*b") and not r.allowed("http://h/a%2Ab/c")
    assert not Robots("User-agent: *\nDisallow: /a*b\n", UA).allowed("http://h/aXb")
    d = Robots("User-agent: *\nDisallow: /p%24\n", UA)                  # nor is an encoded dollar the end of the address
    assert not d.allowed("http://h/p$x") and d.allowed("http://h/p")


def test_no_page_post_or_feed_item_is_stored_with_a_date_ahead_of_today(tmp_path, monkeypatch):
    ahead = (dt.date.today() + dt.timedelta(days=40)).isoformat()
    assert channels.not_future(ahead) is None and channels.not_future(ahead + "T10:00:00") is None
    assert channels.not_future("Sat, 12 Dec 2099 18:00:00 GMT") is None
    assert channels.not_future("2026-11-17") == "2026-11-17" and channels.not_future(None) is None
    assert channels.not_future(dt.date.today().isoformat()) and channels.not_future((dt.date.today() + dt.timedelta(days=1)).isoformat())
    # whatever extract.page_date_info says
    monkeypatch.setattr(channels.extract, "page_date_info", lambda *a, **k: {"date": ahead, "from": "meta", "set_aside": None})
    art = ('<html><head><meta property="og:type" content="article"></head><body><article><p>' + "The council adopted it. " * 12
           + "</p></article></body></html>").encode()
    feed = (f'<?xml version="1.0"?><rss version="2.0"><channel><title>N</title><item><title>Later</title><link>http://HOST/n/1</link>'
            f"<guid>1</guid><pubDate>{ahead}T10:00:00Z</pubDate><description>d</description></item></channel></rss>").encode()
    srv, base, _ = serve({"/": page('<a href="/n/1">n</a>'), "/n/1": (200, "text/html", art), "/feed.xml": (200, "application/rss+xml", feed)})
    try:
        src = {"id": "x:site", "url": base + "/", "kind": "page", "depth": 2}
        ctx = channels.CrawlContext(frontier=Store(tmp_path / "db"))
        recs = list(channels._html_crawl(fetcher(tmp_path / "a"), src, 10, ctx))
        recs.append(channels._page_record(fetcher(tmp_path / "b"), "x:site", base + "/n/1", src))
        recs += [r for r in channels.crawl(fetcher(tmp_path / "c"), {**src, "follow_feed_links": False}, {"channel": "feed", "endpoint": base + "/feed.xml"}, {})]
        recs += [r for r in channels.crawl(fetcher(tmp_path / "d"), src, {"channel": "feed", "endpoint": base + "/feed.xml"}, {})]
        assert len(recs) >= 5 and all(r.published is None for r in recs), [(r.url, r.published) for r in recs]
    finally:
        srv.shutdown()
