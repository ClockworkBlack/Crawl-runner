"""Faults an independent review found in the Nov. 20, 2026 crawler-core changes, each with its test:
robots.txt paths in one written form, robots.txt decoded from its bytes, rules matched without regular expressions,
a refusing host asked a handful of times and no more, redirects to other hosts, and the posts API's resumed reading."""
import datetime as dt
import json
import time

import pytest

from gacrawl import channels
from gacrawl.blocked import Blocked, looks_blocked
from gacrawl.fetch import Disallowed
from gacrawl.polite import ROBOTS_MAX_BYTES, Robots, decode_robots
from gacrawl.scheduler import Progress, run_sources, stuck
from gacrawl.shard import merge_delta, write_delta
from gacrawl.store import Record, Store, utcnow
from test_barriers import fetcher, page, serve
from test_page_pass_replay import Net, fake_browser

UA = "GACivicCrawler/0.3 (+https://github.com/ClockworkBlack/Crawl)"


def closed(rule, url, kind="Disallow"):
    return not Robots(f"User-agent: *\n{kind}: {rule}\n", UA).allowed(url)


# ----------------------------------------------------------------------------- 1. percent-encoding
@pytest.mark.parametrize("rule,url", [
    ("/foo/bar?baz=quz", "http://h/foo/bar?baz=quz"),                                  # RFC 9309 2.2.2, its four examples
    ("/foo/bar/ツ", "http://h/foo/bar/%E3%83%84"),
    ("/foo/bar/%E3%83%84", "http://h/foo/bar/%E3%83%84"),
    ("/foo/bar?baz=https://foo.bar", "http://h/foo/bar?baz=https%3A%2F%2Ffoo.bar"),
    ("/foo/bar/%E3%83%84", "http://h/foo/bar/ツ"), ("/foo/bar/%62%61%7A", "http://h/foo/bar/baz"),
    ("/my docs/", "http://h/my%20docs/x"), ("/my%20docs/", "http://h/my docs/x"),
    ("/café", "http://h/caf%C3%A9/x"), ("/caf%C3%A9", "http://h/café/x"), ("/caf%c3%a9", "http://h/caf%C3%A9"),
    ("/files(1)/", "http://h/files%281%29/x"), ("/a,b", "http://h/a%2Cb"), ("/a!b", "http://h/a%21b"), ("/a%21b", "http://h/a!b"),
    ("/s?q=a b", "http://h/s?q=a%20b"), ("/a%2Fb", "http://h/a/b"), ("/a%2fb", "http://h/a%2Fb"), ("/private/", "http://h/%70rivate/x"),
    ("/%7Ejoe/", "http://h/~joe/x"), ("/a%zz", "http://h/a%zz"),
])
def test_a_rule_and_an_address_written_differently_are_the_same_path(rule, url):
    assert closed(rule, url)


def test_what_a_rule_does_not_cover_stays_open():
    assert not closed("/my docs/", "http://h/mydocs/x") and not closed("/café", "http://h/cafe")
    assert not closed("/Private", "http://h/private")                   # paths are case-sensitive
    # an Allow is matched in the one written form only: "%2F" is not a path separator there
    r = Robots("User-agent: *\nDisallow: /a/\nAllow: /a%2Fb\n", UA)
    assert not r.allowed("http://h/a/b") and r.allowed("http://h/a%2Fb")


def test_a_rule_with_a_space_is_obeyed_through_the_fetcher(tmp_path):
    srv, base, hits = serve({"/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /Board Minutes/\nDisallow: /caf\xc3\xa9/\n"),
                             "/x": page("")})
    try:
        f = fetcher(tmp_path)
        assert f.get(base + "/x").status == 200
        for path in ("/Board Minutes/x", "/Board%20Minutes/x", "/café/x", "/caf%C3%A9/x"):
            with pytest.raises(Disallowed):
                f.get(base + path)
        assert hits == ["/robots.txt", "/x"]
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------- 2. decoding
def test_robots_txt_is_decoded_from_its_bytes(tmp_path):
    assert decode_robots(b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n").startswith("User-agent")
    assert decode_robots("User-agent: *\nDisallow: /\n".encode("utf-16")).startswith("User-agent")
    assert "/café/" in decode_robots("Disallow: /café/".encode())
    assert not Robots("ï»¿User-agent: *\nDisallow: /\n", UA).allowed("http://h/a")       # a mark mis-decoded upstream
    for ctype, body in (("text/plain", b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n"),               # no charset: requests says Latin-1
                        ("text/plain; charset=iso-8859-1", b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n"),
                        ("text/plain", "User-agent: *\nDisallow: /\n".encode("utf-16"))):
        srv, base, hits = serve({"/robots.txt": (200, ctype, body), "/x": page("")})
        try:
            with pytest.raises(Disallowed):
                fetcher(tmp_path / str(len(hits) + id(body) % 1000)).get(base + "/x")
            assert "/x" not in hits
        finally:
            srv.shutdown()


# ----------------------------------------------------------------------------- 3. matching and size
def test_many_wildcards_match_in_no_time_and_only_the_first_500_kib_are_read():
    evil = Robots("User-agent: *\nDisallow: /" + "*a" * 25 + "b\n", UA)
    t0 = time.monotonic()
    assert evil.allowed("http://h/" + "a" * 2000) and not evil.allowed("http://h/" + "a" * 2000 + "b")
    assert time.monotonic() - t0 < 0.5
    assert closed("/**********x", "http://h/abcx") and closed("/a*b*c$", "http://h/a1b2c") and not closed("/a*b*c$", "http://h/a1b2c3")
    assert closed("/*.pdf$", "http://h/d/e.pdf") and not closed("/*.pdf$", "http://h/d/e.pdf?x") and closed("/p*", "http://h/p")
    # stars do not make a rule longer: "Allow: /*********" does not outweigh "Disallow: /private/"
    assert not Robots("User-agent: *\nDisallow: /private/\nAllow: /*********\n", UA).allowed("http://h/private/x")
    big = "User-agent: *\n" + "Disallow: /filler\n" * (ROBOTS_MAX_BYTES // 18 + 10) + "Disallow: /late\n"
    r = Robots(big, UA)
    assert not r.allowed("http://h/filler") and r.allowed("http://h/late") and len(r.rules) < 30000


def test_who_a_group_is_for_and_what_a_crawl_delay_is():
    for name in ("GACivicCrawler/0.3", "GACivicCrawler*", "gaciviccrawler", "GACIVICCRAWLER"):
        assert not Robots(f"User-agent: {name}\nDisallow: /\n", UA).allowed("http://h/a"), name
    assert not Robots("User-agent: crawler\nDisallow: /\n", UA).allowed("http://h/a")      # part of our name: obeyed, to be safe
    assert Robots("User-agent: Googlebot\nDisallow: /\n", UA).allowed("http://h/a")
    delay = lambda t: Robots("User-agent: *\n" + t, UA).delay  # noqa: E731
    assert delay("Crawl-delay: 2\nCrawl-delay: 30\n") == 30.0 and delay("Crawl-delay: 10\n") == 10.0
    for bad in ("inf", "nan", "-5", "10s", ""):
        assert delay(f"Crawl-delay: {bad}\n") is None, bad


# ----------------------------------------------------------------------------- 4. a refusing host
class Everything(dict):
    """Every address answers the same."""

    def __init__(self, answer, robots=None):
        super().__init__()
        self.answer, self.robots = answer, robots

    def get(self, key, default=None):
        return self.robots if key == "/robots.txt" and self.robots else self.answer


BRANDED = b"<html><body>" + b"<p>Sorry, your request cannot be served.</p>" * 80 + b"</body></html>"
ROBOTS_OK = (200, "text/plain", b"User-agent: *\nAllow: /\n")


def resumed(base, n=300):
    q = [[base + f"/p{i}", 2, 1] for i in range(n)]
    return json.dumps({"v": 1, "url": base + "/", "start": utcnow(), "n": 5, "q": q})


@pytest.mark.parametrize("label,robots,answer,most", [
    ("robots.txt forbidden and so is the first page (unrecognised body)", None, (403, "text/html", BRANDED), 2),
    ("a site behind a login: 401 everywhere", None, (401, "text/html", b"Authorization Required"), 2),
    ("cloudflare's bare refusal", None, (403, "text/plain", b"error code: 1020"), 1),
    ("robots.txt served, every page 403 with a long branded body", ROBOTS_OK, (403, "text/html", BRANDED), 6),
    ("robots.txt served, every page 401", ROBOTS_OK, (401, "text/html", b"login"), 6),
])
def test_a_host_that_refuses_everything_is_asked_a_handful_of_times(tmp_path, label, robots, answer, most):
    srv, base, hits = serve(Everything(answer, robots))
    try:
        f = fetcher(tmp_path)
        src = {"id": "x:site", "url": base + "/", "kind": "page", "depth": 3}
        ctx = channels.CrawlContext(frontier=Store(tmp_path / "data"), cursors={"frontier": resumed(base)}, resume=True)
        with pytest.raises(Blocked):
            list(channels._html_crawl(f, src, 50, ctx))
        assert len(hits) <= most, (label, len(hits))
        assert f.blocklist.is_blocked(base + "/")
        n = len(hits)
        with pytest.raises(Blocked):
            f.get(base + "/again")                                      # for the rest of the run: not asked at all
        assert len(hits) == n
    finally:
        srv.shutdown()


def test_pages_that_do_not_answer_stop_the_pass_after_five_and_the_cursor_stays(tmp_path):
    srv, base, hits = serve(Everything((500, "text/html", b"error"), ROBOTS_OK))    # (pages that are gone, 404/410, do not count)
    try:
        f = fetcher(tmp_path)
        src = {"id": "x:site", "url": base + "/", "kind": "page", "depth": 3}
        ctx = channels.CrawlContext(frontier=Store(tmp_path / "data"), cursors={"frontier": resumed(base)}, resume=True)
        out = []
        with pytest.raises(channels.ChannelDown, match="5 pages in a row"):
            for item in channels._html_crawl(f, src, 50, ctx):
                out.append(item)
        assert [h for h in hits if h != "/robots.txt"] == [f"/p{i}" for i in range(5)]
        assert len(out) == 1 and out[0].stream == "frontier" and len(json.loads(out[0].value)["q"]) == 295    # not emptied
        assert ctx.stats["fetched"] == 5 and ctx.stats["ok"] == 0
    finally:
        srv.shutdown()


def test_a_share_whose_requests_were_all_refused_is_not_progress(tmp_path, monkeypatch):
    """The queue shrank (the cursor moved), but nothing answered: the stall rule counts the share."""
    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        ctx.stats["fetched"] += 3                                       # three addresses asked for, none answered 200
        yield channels.Checkpoint("frontier", f"moved-{time.monotonic()}")
        while True:
            time.sleep(0.02)
            f._check_time()
    monkeypatch.setattr(channels, "crawl", crawl)
    store, cache, prog = Store(tmp_path / "data"), channels.ChannelCache(tmp_path / "c.json"), Progress(tmp_path / "p.json")
    src = {"id": "x:site", "url": "https://x.gov/", "tier": "a", "channel": "html", "endpoint": "https://x.gov/"}
    f, calls = fetcher(tmp_path), []
    for _ in range(3):
        out = run_sources({"schedule": {"windows": []}}, [src], f, store, cache, prog, time.monotonic() + 600,
                          force=True, workers=1, slice_minutes=0.003, log=calls.append)
        assert prog.get("x:site")["moved"] is False and not any("carry on" in c for c in calls)    # and no spare share
    p = prog.get("x:site")
    assert p["stalled"] == 3 and stuck(p), p
    store.close()


def test_new_refusal_markers():
    assert looks_blocked(403, b"error code: 1020") and looks_blocked(403, b"<h1>Sorry, you have been blocked</h1>")
    assert looks_blocked(403, b"error code: 1006") and not looks_blocked(200, b"error code: 1020") and not looks_blocked(404, b"error code: 1020")


def test_a_data_services_403_is_not_counted_against_its_host(tmp_path):
    srv, base, hits = serve(Everything((403, "application/json", b'{"error": "over quota"}'), ROBOTS_OK))
    try:
        f = fetcher(tmp_path)
        for i in range(8):
            assert f.get(base + f"/api/v1/things/{i}", headers={"Accept": "application/json"}).status == 403
        assert not f.blocklist.data
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------- 5. the browser crawl (redirects: test_barriers.py)
def browser_run(tmp_path, monkeypatch, net, src, cursors=None):
    ctx = channels.CrawlContext(frontier=Store(tmp_path / "data"), cursors=cursors or {}, resume=True)
    with fake_browser(net, monkeypatch):
        return ctx, list(channels._browser_crawl_locked(net, src, 20, ctx))


def test_the_browser_crawl_asks_the_block_list_and_a_refusal_does_not_end_as_ok(tmp_path, monkeypatch):
    base = "http://site.test"
    site = {"/": f'<html><body><a href="{base}/a">a</a>' + "<p>Welcome to the district. </p>" * 20 + "</body></html>",
            "/a": "<html><title>Just a moment...</title><body><div class='cf-chl'>challenge-platform</div></body></html>"}
    net = Net(base, site, 50)
    added = []
    net.blocklist = type("BL", (), {"is_blocked": staticmethod(lambda url: None), "add": staticmethod(lambda *a, **k: added.append(a))})
    src = {"id": "sd-x:site", "url": base + "/", "kind": "page"}
    monkeypatch.setattr("gacrawl.blocked.looks_blocked", lambda status, body: "HTTP 403 bot-protection challenge" if b"cf-chl" in body else None)
    with pytest.raises(Blocked):
        browser_run(tmp_path, monkeypatch, net, src)
    assert added and net.hits == ["/", "/a"]
    # a host already on the block list is not opened in the browser at all
    net2 = Net(base, site, 50)
    net2.blocklist = type("BL", (), {"is_blocked": staticmethod(lambda url: {"reason": "HTTP 403 bot-protection challenge"}),
                                     "add": staticmethod(lambda *a, **k: None)})
    with pytest.raises(Blocked):
        browser_run(tmp_path / "b", monkeypatch, net2, src)
    assert net2.hits == []


# ----------------------------------------------------------------------------- 6. the posts API's resumed reading
def test_a_resumed_first_reading_of_posts_is_stamped_with_when_it_began(tmp_path, monkeypatch):
    began = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).replace(microsecond=0).isoformat()
    ep = "http://x/wp-json/wp/v2/posts"
    cur = json.dumps({"page": 3, "start": began, "url": ep})
    assert channels._wp_load(channels.CrawlContext(cursors={"wp_page": cur}), ep) == (3, began)
    for bad in ("3", json.dumps({"page": 3, "start": began, "url": "http://other/"}), json.dumps({"page": "3", "start": began, "url": ep}),
                json.dumps({"page": 3, "start": "2026-01-01T00:00:00+00:00", "url": ep}), json.dumps({"page": 3, "start": "2099-01-01T00:00:00+00:00", "url": ep}),
                json.dumps({"page": 3, "start": "2026-11-19T00:00:00", "url": ep}), "{{", json.dumps([3])):
        assert channels._wp_load(channels.CrawlContext(cursors={"wp_page": bad}), ep) is None, bad

    asked = []

    class R:
        status, content_type = 200, "application/json"

        def __init__(self, rows):
            self.rows, self.status = rows, 200 if rows else 400

        def json(self):
            return self.rows

    class F:
        def get(self, url, params=None, **kw):
            asked.append(params["page"])
            return R([{"id": 9, "link": "http://x/p9", "date": "2026-10-01T10:00:00", "title": {"rendered": "P"},
                       "content": {"rendered": "<p>Post nine.</p>"}}] if params["page"] == 3 else [])
    ctx = channels.CrawlContext(frontier=Store(tmp_path / "data"), cursors={"wp_page": cur}, resume=True)
    out = list(channels.crawl(F(), {"id": "s", "url": "http://x"}, {"channel": "wp_json", "endpoint": ep}, {}, ctx=ctx))
    assert asked == [3, 4] and ctx.pass_started == began
    marks = [o for o in out if isinstance(o, channels.Checkpoint)]
    assert json.loads(marks[0].value) == {"page": 4, "start": began, "url": ep} and marks[-1].value == ""

    # the scheduler stamps the source's last good run with that beginning, so the next run asks for everything
    # modified since page 1 was read
    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        ctx.pass_started = began
        yield Record(src["id"], "page", "http://x/p9", title="P", text="Post nine.")
    monkeypatch.setattr(channels, "crawl", crawl)
    store, cache, prog = Store(tmp_path / "d2"), channels.ChannelCache(tmp_path / "c.json"), Progress(tmp_path / "p.json")
    src = {"id": "co-butts:elections", "url": "http://x", "tier": "a", "channel": "wp_json", "endpoint": ep}
    run_sources({"schedule": {"windows": []}}, [src], fetcher(tmp_path), store, cache, prog, time.monotonic() + 60, force=True,
                workers=1, log=lambda *_: None)
    p = prog.get("co-butts:elections")
    assert p["status"] == "ok" and p["last_ok"] == began and p["last_run"] > began
    store.close()


# ----------------------------------------------------------------------------- lesser ones
def test_a_cursor_that_is_not_what_this_code_writes_starts_the_pass_over(tmp_path):
    srv, base, hits = serve({"/": page('<a href="/a">a</a>'), "/a": page("")})
    src = {"id": "x:site", "url": base + "/", "kind": "page", "depth": 3}
    now = utcnow()
    bad = {
        "not json": "{{{", "a list": "[1,2]",
        "q of strings": json.dumps({"url": src["url"], "start": now, "q": ["http://a/b"]}),
        "q of pairs": json.dumps({"url": src["url"], "start": now, "q": [[base + "/a", 1]]}),
        "count not a number": json.dumps({"url": src["url"], "start": now, "n": "x", "q": [[base + "/a", 1, 1]]}),
        "depth a string": json.dumps({"url": src["url"], "start": now, "n": 0, "q": [[base + "/a", "1", 1]]}),
        "start without a zone": json.dumps({"url": src["url"], "start": "2026-11-19T00:00:00", "n": 0, "q": [[base + "/a", 1, 1]]}),
        "start in the future": json.dumps({"url": src["url"], "start": "2099-01-01T00:00:00+00:00", "n": 0, "q": [[base + "/a", 1, 1]]}),
        "an address that is not one": json.dumps({"url": src["url"], "start": now, "n": 0, "q": [["javascript:x", 1, 1]]}),
    }
    try:
        for i, (label, cur) in enumerate(bad.items()):
            ctx = channels.CrawlContext(frontier=Store(tmp_path / f"d{i}"), cursors={"frontier": cur}, resume=True)
            assert channels._pass_load(ctx, "frontier", src["url"]) is None, label
            out = list(channels._html_crawl(fetcher(tmp_path / f"f{i}"), src, 50, ctx))
            assert [o.url for o in out if not isinstance(o, channels.Checkpoint)] == [base + "/", base + "/a"], label
        # another host's addresses in the queue, and a host the cursor names that is not this site's, are dropped
        cur = json.dumps({"url": src["url"], "start": now, "n": 0, "hosts": ["facebook.com", "evil.example"],
                          "q": [["http://127.0.0.1:1/evil", 1, 1], [base + "/a", 1, 1]]})
        ctx = channels.CrawlContext(frontier=Store(tmp_path / "dz"), cursors={"frontier": cur}, resume=True)
        out = list(channels._html_crawl(fetcher(tmp_path / "fz"), src, 50, ctx))
        assert [o.url for o in out if not isinstance(o, channels.Checkpoint)] == [base + "/a"]
    finally:
        srv.shutdown()


def test_full_empties_a_pass_cursor_instead_of_deleting_it(tmp_path, monkeypatch):
    seen = {}

    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        seen.update(ctx.cursors)
        yield Record(src["id"], "page", "http://x/a", title="A", text="a")
    monkeypatch.setattr(channels, "crawl", crawl)
    main, shard = Store(tmp_path / "main"), Store(tmp_path / "shard")
    old = channels._pass_value("https://x.gov/", "2026-11-19T00:00:00+00:00", 9, [["https://x.gov/a", 1, 1]])
    for s in (main, shard):
        s.db.execute("INSERT INTO cursors VALUES ('x:site', 'frontier', ?, '2026-11-19T00:00:00+00:00')", (old,))
        s.db.execute("INSERT INTO cursors VALUES ('x:site', 'events', '2019-01-01', '2026-11-19T00:00:00+00:00')")
    src = {"id": "x:site", "url": "https://x.gov/", "tier": "a", "channel": "html", "endpoint": "https://x.gov/"}
    run_sources({"schedule": {"windows": []}}, [src], fetcher(tmp_path), shard, channels.ChannelCache(tmp_path / "c.json"),
                Progress(tmp_path / "p.json"), time.monotonic() + 60, force=True, workers=1, full=True, log=lambda *_: None)
    assert seen == {"frontier": ""}                                     # the reading began from the start
    write_delta(shard, "2026-11-19T12:00:00+00:00", tmp_path / "d.db")
    merge_delta(main, tmp_path / "d.db")
    assert main.cursors("x:site")["frontier"] == ""                     # and the main database's old queue is gone too
    main.close(); shard.close()


def test_one_pass_never_makes_more_than_max_pages_plus_a_hundred_requests(tmp_path):
    """Listing re-reads and pages that do not answer count toward the ceiling (every third page here is gone, so no
    five in a row; every listing page was read before)."""
    routes = {"/": page("".join(f'<a href="/s{i}">s{i}</a> ' for i in range(400)))}
    for i in range(400):
        if i % 3:
            routes[f"/s{i}"] = page(f'<a href="/s{i}/doc">doc</a>')
    srv, base, hits = serve(routes)
    store = Store(tmp_path / "data")
    try:
        for i in range(400):
            store.mark_url(base + f"/s{i}", 200, "x:site", "text/html", b"x")
        store.mark_url(base + "/", 200, "x:site", "text/html", b"x")
        src = {"id": "x:site", "url": base + "/", "kind": "page", "depth": 3}
        shares = 0
        while shares < 5:
            shares += 1
            ctx = channels.CrawlContext(frontier=store, cursors=store.cursors("x:site"), resume=True)
            for item in channels._html_crawl(fetcher(tmp_path / f"f{shares}"), src, 10, ctx):
                if isinstance(item, channels.Checkpoint):
                    store.set_cursor("x:site", item.stream, item.value)
            if store.cursors("x:site").get("frontier", "") == "":
                break
        assert len([h for h in hits if h != "/robots.txt"]) <= 10 + channels.PASS_EXTRA_REQUESTS
    finally:
        srv.shutdown(); store.close()


ARTICLE = ('<html><head><meta property="og:type" content="article"><meta property="article:published_time" content="{d}">'
           "</head><body><article><p>" + "The council adopted the budget. " * 10 + "</p></article></body></html>")
HOME = ('<html><head><title>City of X</title></head><body><h1>Welcome</h1><div class="events"><a href="/e/1">Christmas Parade</a> '
        '<time datetime="2026-12-12T18:00">Dec 12</time></div><p>' + "The city council meets on the first Monday. " * 8 + "</p></body></html>")


def test_a_page_is_dated_by_its_markup_only_when_it_is_an_article_and_never_ahead_of_today():
    assert channels.stated_date(ARTICLE.format(d="2026-11-17T09:00:00-05:00"), "x", 2) == ("2026-11-17", "meta")
    assert channels.stated_date(ARTICLE.format(d="2026-11-17T09:00:00-05:00"), "x", 0) == (None, "")      # the source's own address
    ahead = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    assert channels.stated_date(ARTICLE.format(d=ahead), "x", 2) == (None, "")
    assert channels.stated_date(HOME, "x", 1) == (None, "")                                               # an events box's <time>
    assert channels.stated_date('<html><head><meta name="date" content="2026-11-01"></head><body><p>hello</p></body></html>', "x", 2) == (None, "")


def test_what_is_known_about_a_record_is_not_its_content(tmp_path):
    """meta.http_last_modified and meta.date_from are out of the hash: gaining or changing one leaves `updated`
    (changed_at) and the version alone, is stored all the same, and reaches the main database."""
    base = dict(title="Council", text="The council meets on Mondays.")
    plain = Record("x:site", "page", "http://x/c", meta={"opened": True}, **base)
    stamped = Record("x:site", "page", "http://x/c", meta={"opened": True, "http_last_modified": "2026-03-02T14:30:00+00:00", "date_from": "meta"}, **base)
    assert plain.content_hash == stamped.content_hash and stamped.legacy_hash != stamped.content_hash
    assert Record("x:site", "page", "http://x/c", meta={"opened": True, "k": 1}, **base).content_hash != plain.content_hash

    s = Store(tmp_path / "shard")
    s.add(plain)
    s.db.execute("UPDATE records SET first_seen='2026-11-01T00:00:00+00:00', changed_at='2026-11-01T00:00:00+00:00', fetched_at='2026-11-01T00:00:00+00:00'")
    assert s.add(stamped) is False                                      # not "new or changed"
    row = s.read("x:site", "page")[0]
    assert row["meta"]["http_last_modified"] and row["changed_at"] == "2026-11-01T00:00:00+00:00" and row["version"] == 1
    assert row["fetched_at"] > "2026-11-19"
    main = Store(tmp_path / "main")
    main.add(Record("x:site", "page", "http://x/c", meta={"opened": True}, **base))
    main.db.execute("UPDATE records SET first_seen='2026-11-01T00:00:00+00:00', changed_at='2026-11-01T00:00:00+00:00'")
    write_delta(s, "2026-11-19T00:00:00+00:00", tmp_path / "d.db")
    merge_delta(main, tmp_path / "d.db")
    got = main.read("x:site", "page")[0]
    assert got["meta"]["date_from"] == "meta" and got["changed_at"] == "2026-11-01T00:00:00+00:00" and got["version"] == 1

    # a row stored under the older hash (date_from inside it) is the same record: no new version when it is read again
    old = Record("x:site", "page", "http://x/n", meta={"opened": True, "date_from": "feed"}, **base)
    s.add(old)
    s.db.execute("UPDATE records SET hash=?, changed_at='2026-11-02T00:00:00+00:00' WHERE url='http://x/n'", (old.legacy_hash[:24],))
    assert s.add(Record("x:site", "page", "http://x/n", meta={"opened": True, "date_from": "feed"}, **base)) is False
    row = [r for r in s.read("x:site", "page") if r["url"] == "http://x/n"][0]
    assert row["version"] == 1 and row["changed_at"] == "2026-11-02T00:00:00+00:00" and row["hash"] == old.content_hash[:24]
    s.close(); main.close()
