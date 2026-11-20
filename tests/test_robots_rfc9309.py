"""robots.txt read the way RFC 9309 says (gacrawl/polite.py Robots), and what is and is not a refusal."""
import datetime as dt

import pytest
import requests

from gacrawl.blocked import Blocked, BlockList, not_a_refusal
from gacrawl.fetch import Fetcher
from gacrawl.polite import Robots, RobotsCache
from gacrawl.scheduler import Progress, due

UA = "GACivicCrawler/0.3 (+https://github.com/ClockworkBlack/Crawl)"


def ok(text, path, ua=UA):
    return Robots(text, ua).allowed("https://x.gov" + path)


def test_the_longest_rule_decides_and_allow_wins_a_tie():
    closed_but_agendas = "User-agent: *\nDisallow: /\nAllow: /agendas/\n"
    assert ok(closed_but_agendas, "/agendas/2026.pdf") and not ok(closed_but_agendas, "/") and not ok(closed_but_agendas, "/staff")
    open_but_private = "User-agent: *\nAllow: /\nDisallow: /private/\n"        # the first rule used to win: /private/ was read
    assert ok(open_but_private, "/council") and not ok(open_but_private, "/private/x")
    assert ok("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page")


def test_wildcards_and_end_anchors_are_obeyed():
    t = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /*?print=\nDisallow: /search*results\nAllow: /docs/*.pdf$\n"
    assert not ok(t, "/files/a.pdf") and ok(t, "/files/a.pdf?download=1") and ok(t, "/files/a.pdfx")
    assert not ok(t, "/news/item?print=1") and ok(t, "/news/item")
    assert not ok(t, "/search/all/results/2") and ok(t, "/docs/minutes.pdf")


def test_blank_lines_do_not_end_a_group_and_a_bom_or_comment_is_not_a_rule():
    t = "﻿User-agent: *\n\n# keep out\nDisallow: /wp-admin/   # the admin pages\n\nDisallow: /index.php\n"
    assert not ok(t, "/wp-admin/x") and not ok(t, "/index.php/2026/notice") and ok(t, "/council")


def test_our_own_group_replaces_the_general_one_and_only_our_name_is_ours():
    t = ("User-agent: *\nDisallow: /\n\nUser-agent: GACivicCrawler\nDisallow: /staff/\n\n"
         "User-agent: crawler\nDisallow: /\n\nUser-agent: GACivicCrawlerBot\nDisallow: /\n")
    assert ok(t, "/council") and not ok(t, "/staff/list")              # ours, not `*`, not "crawler", not a longer name
    assert not ok(t, "/council", ua="OtherBot/1")
    both = "User-agent: gaciviccrawler\nDisallow: /a\n\nUser-agent: GACIVICCRAWLER\nDisallow: /b\n"
    assert not ok(both, "/a") and not ok(both, "/b") and ok(both, "/c")  # two groups for one name are one group
    shared = "User-agent: Googlebot\nUser-agent: GACivicCrawler\nDisallow: /x\nCrawl-delay: 7\n"
    r = Robots(shared, UA)
    assert not r.allowed("https://x.gov/x") and r.delay == 7.0


def test_empty_disallow_no_rules_and_robots_txt_itself():
    assert ok("User-agent: *\nDisallow:\n", "/anything") and ok("", "/anything")
    assert ok("User-agent: *\nDisallow: /\n", "/robots.txt")
    assert ok("<html><body><h1>Page not found</h1><p>Disallowed? No: this is a web page.</p></body></html>", "/council")
    assert not ok("User-agent: *\nDisallow: /a%2fb\n", "/a%2Fb") and not ok("User-agent: *\nDisallow: /caf%C3%A9\n", "/caf%c3%a9")


def test_crawl_delay_and_sitemaps():
    rc = RobotsCache(lambda url: (200, "Sitemap: https://x.gov/sitemap.xml\nUser-agent: *\nCrawl-delay: 30\nDisallow: /p\n"), UA,
                     warn=lambda *_: None)
    assert rc.crawl_delay("https://x.gov/a") == 30.0 and rc.sitemaps("https://x.gov/") == ["https://x.gov/sitemap.xml"]
    big = RobotsCache(lambda url: (200, "User-agent: *\nCrawl-delay: 600\n"), UA, warn=lambda *_: None)
    assert big.crawl_delay("https://x.gov/a") == 600.0                  # obeyed as written


def test_a_4xx_robots_is_no_robots_and_a_429_or_5xx_is_a_short_hold():
    for status in (401, 403, 404, 410):
        rc = RobotsCache(lambda url, s=status: (s, "<h1>Forbidden</h1>"), UA, warn=lambda *_: None)
        assert rc.allowed("https://x.gov/council") and not rc.failures, status
    for status in (429, 500, 503):
        rc = RobotsCache(lambda url, s=status: (s, ""), UA, warn=lambda *_: None)
        assert not rc.allowed("https://x.gov/council") and "unreachable" in rc.why("https://x.gov/council"), status


def test_a_dropped_connection_on_robots_is_tried_once_more():
    calls = []

    def get(url):
        calls.append(url)
        if len(calls) == 1:
            raise requests.ConnectionError("Connection aborted: RemoteDisconnected")
        return 200, "User-agent: *\nDisallow: /private\n"
    rc = RobotsCache(get, UA, warn=lambda *_: None)
    assert rc.allowed("https://x.gov/council") and len(calls) == 2

    gone = []

    def dead(url):
        gone.append(url)
        raise requests.ConnectionError("NameResolutionError: Failed to resolve 'x.gov'")
    rc2 = RobotsCache(dead, UA, warn=lambda *_: None)
    assert not rc2.allowed("https://x.gov/") and len(gone) == 1         # no such host: not asked twice


# ----------------------------------------------------------------------------- refusals
class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {"Content-Type": "text/html"}
        self.text = body.decode("utf-8", "replace")
        self.elapsed = dt.timedelta(seconds=0.01)

    def iter_content(self, n):
        yield self.content

    def close(self):
        pass


class Session:
    """Answers by path: {path: (status, body)}; anything else 200 with a page."""
    headers: dict = {}

    def __init__(self, routes):
        self.routes, self.asked, self.headers = routes, [], {}

    def mount(self, *a, **k):
        pass

    def get(self, url, **kw):
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        self.asked.append(path)
        status, body = self.routes.get(path, (200, b"<html><body><p>" + b"Council minutes. " * 30 + b"</p></body></html>"))
        return Resp(status, body)


def fetcher(tmp_path, routes):
    return Fetcher(UA, tmp_path / "state", min_interval=0, session=Session(routes), sleep=lambda _: None)


FORBIDDEN = b"<html><head><title>403 Forbidden</title></head><body><h1>Forbidden</h1></body></html>"
CHALLENGE = b"<html><head><title>Just a moment...</title></head><body><div class='cf-chl'>challenge-platform</div></body></html>"


def test_a_forbidden_robots_txt_alone_does_not_close_the_site(tmp_path):
    f = fetcher(tmp_path, {"/robots.txt": (403, FORBIDDEN)})
    assert f.get("https://www.atlantaga.gov/council").status == 200
    assert not f.blocklist.data


def test_a_site_that_refuses_its_front_page_too_is_blocked_at_once(tmp_path):
    f = fetcher(tmp_path, {"/robots.txt": (403, FORBIDDEN), "/": (403, FORBIDDEN)})
    with pytest.raises(Blocked):
        f.get("https://www.atlantaga.gov/")
    assert "www.atlantaga.gov" in f.blocklist.data
    with pytest.raises(Blocked):
        f.get("https://www.atlantaga.gov/council")                      # and stays so: not asked again
    assert f.session.asked == ["/robots.txt", "/"]


def test_a_challenge_in_place_of_robots_txt_is_a_refusal(tmp_path):
    f = fetcher(tmp_path, {"/robots.txt": (403, CHALLENGE)})
    with pytest.raises(Blocked):
        f.get("https://www.acworth-ga.gov/council")
    assert f.session.asked == ["/robots.txt"] and "challenge" in f.blocklist.data["www.acworth-ga.gov"]["reason"]


def test_forbidden_deep_pages_among_pages_that_answer_do_not_block_the_host(tmp_path):
    routes = {f"/staff/{i}": (403, FORBIDDEN) for i in range(6)}
    f = fetcher(tmp_path, routes)
    for i in range(6):
        assert f.get(f"https://www.clarkstonga.gov/staff/{i}").status == 403
        assert f.get(f"https://www.clarkstonga.gov/page/{i}").status == 200      # the site answers: not a refusal of us
    assert not f.blocklist.data
    g = fetcher(tmp_path / "g", routes)
    with pytest.raises(Blocked):
        for i in range(6):
            g.get(f"https://www.clarkstonga.gov/staff/{i}")              # nothing but refusals: the fifth in a row
    assert len([p for p in g.session.asked if p.startswith("/staff")]) == Fetcher.REFUSALS_IN_A_ROW


def test_blocks_filed_for_a_forbidden_robots_txt_are_not_honoured_any_more(tmp_path):
    assert not_a_refusal("robots.txt: HTTP 403 access denied") and not not_a_refusal("robots.txt: HTTP 403 bot-protection challenge")
    assert not not_a_refusal("HTTP 403 access denied")
    bl = BlockList(tmp_path / "blocked.json")
    bl.add("https://www.atlantaga.gov/robots.txt", "robots.txt: HTTP 403 access denied")
    bl.add("https://www.acworth-ga.gov/", "HTTP 403 bot-protection challenge")
    assert bl.is_blocked("https://www.atlantaga.gov/x") is None and bl.is_blocked("https://www.acworth-ga.gov/x")

    now = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)
    prog = Progress(tmp_path / "p.json")
    day = (now - dt.timedelta(days=8)).isoformat()   # past an error's wait (a quarter of 30 days), inside a refusal's
    prog.update("atlanta:site", status="blocked: https://www.atlantaga.gov: robots.txt: HTTP 403 access denied", last_run=day)
    prog.update("acworth:site", status="blocked: https://www.acworth-ga.gov: HTTP 403 bot-protection challenge", last_run=day)
    prog.update("pavo:site", status="stopped: robots.txt disallows https://cityofpavo.com/ (unreachable (ConnectionError: x))", last_run=day)
    prog.update("lyons:granicus", status="stopped: robots.txt disallows https://lyonsga.granicus.com/ (robots.txt disallows this path for our user agent)", last_run=day)
    srcs = [{"id": i, "url": "https://x/", "tier": "c"} for i in ("atlanta:site", "acworth:site", "pavo:site", "lyons:granicus")]
    assert sorted(s["id"] for s in due(srcs, prog, now=now)) == ["atlanta:site", "pavo:site"]     # the real refusals wait
