"""Rules of the crawler's own that kept it from pages it may read (Nov. 20, 2026 review). The robots.txt and refusal
rules are in tests/test_robots_rfc9309.py; the page count of a pass in tests/test_page_pass_resume.py."""
import gzip
import http.server
import io
import threading
import zipfile

import pytest
import requests

from gacrawl import channels
from gacrawl.fetch import Disallowed, Fetcher
from gacrawl.store import Store

TEXT = "<p>" + "The commission approved the minutes of its last regular meeting. " * 6 + "</p>"


def docx(paragraphs):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def serve(routes):
    """routes: {path: (status, content_type, body) | ("redirect", location)}; HOST in a body or location is this server."""
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            hit = routes.get(self.path.split("?")[0])
            if hit is None:
                self.send_response(404); self.end_headers(); return
            if hit[0] == "redirect":
                self.send_response(301); self.send_header("Location", hit[1]); self.end_headers(); return
            status, ctype, body = hit
            body = body.replace(b"HOST", self.headers["Host"].encode())
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", hits


def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def page(body):
    return 200, "text/html; charset=utf-8", f"<html><title>t</title><body>{body}{TEXT}</body></html>".encode()


def test_a_site_whose_address_moved_to_a_host_of_its_own_is_read_there(tmp_path):
    """cityofx.com answers with a redirect to cityofx.gov: the links on the page are the new host's, and were all
    dropped as "another site" (the crawl ended at the home page, "1 seen", and was marked complete). The new host is
    the site only when it is the same name under another ending, or a host the source's configuration lists."""
    new, new_base, new_hits = serve({"/": page('<a href="/council">Council</a> <a href="http://elsewhere.test/x">other</a>'),
                                     "/council": page('<a href="/council/minutes">Minutes</a>'),
                                     "/council/minutes": page("")})
    old, old_base, _ = serve({"/": ("redirect", new_base + "/")})
    try:
        src = {"id": "x:site", "url": old_base + "/", "kind": "page", "depth": 3, "hosts": [new_base.split("//")[1]]}
        recs = [r for r in channels._html_crawl(fetcher(tmp_path), src, 50, channels.CrawlContext())]
        assert {r.url for r in recs} == {old_base + "/", new_base + "/council", new_base + "/council/minutes"}
        assert [h for h in new_hits if h != "/robots.txt"] == ["/", "/council", "/council/minutes"]
    finally:
        new.shutdown(); old.shutdown()


def test_an_address_that_moved_to_someone_elses_host_is_not_read_there(tmp_path):
    """A town's address that redirects to a code publisher, a meeting portal, a shortener or a parked domain: nothing
    of that host is read past the page the redirect led to, and nothing is stored under the town."""
    other, obase, ohits = serve({"/": page('<a href="/ga/othertown/codes">Other town</a>'), "/ga/othertown/codes": page("")})
    old, old_base, _ = serve({"/": ("redirect", obase + "/")})
    try:
        src = {"id": "smalltown:site", "url": old_base + "/", "kind": "page", "depth": 3}
        got = []
        with pytest.raises(channels.ChannelDown, match="not listed"):
            for r in channels._html_crawl(fetcher(tmp_path), src, 50, channels.CrawlContext()):
                got.append(r)
        assert got == [] and [h for h in ohits if h != "/robots.txt"] == ["/"]
    finally:
        other.shutdown(); old.shutdown()
    same = channels.same_site
    assert same({}, "cityofx.com", "www.cityofx.gov") and same({}, "cityofx.com", "agendas.cityofx.org")
    assert same({}, "haralson.k12.ga.us", "www.haralson.k12.ga.us") and same({"website": "https://newname.gov/x"}, "old.com", "newname.gov")
    for host in ("library.municode.com", "ecode360.com", "go.boarddocs.com", "bit.ly", "sedoparking.com", "civicplus.com",
                 "m.facebook.com", "cityofy.gov", "127.0.0.1:99"):
        assert not same({}, "cityofx.com", host), host


def test_every_hop_of_a_redirect_passes_the_block_list_robots_and_spacing(tmp_path):
    new, new_base, new_hits = serve({"/robots.txt": (200, "text/plain", b"User-agent: *\nCrawl-delay: 7\nDisallow: /members/\n"),
                                     "/open": page(""), "/members/list": page("")})
    old, old_base, old_hits = serve({"/a": ("redirect", new_base + "/open"), "/b": ("redirect", new_base + "/members/list"),
                                     "/loop": ("redirect", "/loop")})
    try:
        f = fetcher(tmp_path)
        waits = []
        real = f.limiter.wait
        f.limiter.wait = lambda url, delay=None, until=None: waits.append((url, delay)) or real(url, 0, until=until)
        r = f.get(old_base + "/a")
        assert r.status == 200 and r.url == old_base + "/a" and r.final_url == new_base + "/open"
        # the landing host's robots.txt was read before its page was asked for, and its Crawl-delay applied to that request
        assert new_hits == ["/robots.txt", "/open"] and (new_base + "/open", 7.0) in waits
        with pytest.raises(Disallowed):
            f.get(old_base + "/b")
        assert "/members/list" not in new_hits
        with pytest.raises(RuntimeError, match="gave up on .*circle"):
            f.get(old_base + "/loop")
        assert old_hits.count("/loop") == 1                              # a loop is seen at once, not walked
        # a landing host on the block list is not asked at all
        g = fetcher(tmp_path / "g")
        g.blocklist.add(new_base + "/", "HTTP 403 bot-protection challenge", "x")
        new_hits.clear()
        from gacrawl.blocked import Blocked
        with pytest.raises(Blocked):
            g.get(old_base + "/a")
        assert new_hits == []
    finally:
        new.shutdown(); old.shutdown()


def test_a_challenge_after_a_redirect_is_filed_against_the_host_that_served_it(tmp_path):
    wall, wbase, _ = serve({"/": (403, "text/html", b"<html><title>Just a moment...</title><div class='cf-chl'></div></html>")})
    old, old_base, _ = serve({"/x": ("redirect", wbase + "/")})
    try:
        from gacrawl.blocked import Blocked
        f = fetcher(tmp_path)
        with pytest.raises(Blocked):
            f.get(old_base + "/x")
        assert list(f.blocklist.data) == [wbase.split("//")[1]]
    finally:
        wall.shutdown(); old.shutdown()


def test_http_addresses_of_a_host_that_moved_to_https_are_asked_for_once():
    from gacrawl.fetch import Fetcher as F
    f = F.__new__(F)
    f._https = {"www.x.gov"}
    f.api_endpoints = []

    class BL:
        def is_blocked(self, url): return None

    class RB:
        def allowed(self, url): return True
    f.blocklist, f.robots = BL(), RB()
    assert f._upgraded("http://www.x.gov/a?b=1") == "https://www.x.gov/a?b=1" and f._upgraded("http://other.gov/a") == "http://other.gov/a"


def test_robots_of_the_host_an_address_moves_to_is_obeyed(tmp_path):
    new, new_base, new_hits = serve({"/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /members/\n"),
                                     "/members/list": page("")})
    old, old_base, _ = serve({"/list": ("redirect", new_base + "/members/list")})
    try:
        with pytest.raises(Disallowed):
            fetcher(tmp_path).get(old_base + "/list")
    finally:
        new.shutdown(); old.shutdown()


def test_word_and_text_files_become_records(tmp_path):
    """They were downloaded, gave no record, and the frontier called them read for ten years."""
    srv, base, _ = serve({
        "/": page('<a href="/files/minutes-2026-10-05.docx">Minutes</a> <a href="/files/agenda.txt">Agenda</a> <a href="/files/roll.xlsx">Roll</a>'),
        "/files/minutes-2026-10-05.docx": (200, "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                           docx(["Minutes of the regular meeting", "Motion to adopt the budget carried 5-0."])),
        "/files/agenda.txt": (200, "text/plain", b"AGENDA\n1. Call to order\n2. Approval of minutes\n"),
        "/files/roll.xlsx": (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04junk")})
    store = Store(tmp_path / "data")
    try:
        src = {"id": "x:site", "url": base + "/", "kind": "page", "depth": 2}
        recs = {r.url.rsplit("/", 1)[-1]: r for r in channels._html_crawl(fetcher(tmp_path), src, 50, channels.CrawlContext(frontier=store))}
        d = recs["minutes-2026-10-05.docx"]
        assert d.kind == "document" and d.channel == "docx" and d.meta["doc_type"] == "minutes" and "carried 5-0" in d.text
        assert recs["agenda.txt"].channel == "text" and "Call to order" in recs["agenda.txt"].text
        assert "roll.xlsx" not in recs                                  # no reader for spreadsheets (kept as it was)
    finally:
        srv.shutdown(); store.close()


def test_a_gzipped_sitemap_is_read(tmp_path):
    xml = b'<?xml version="1.0"?><urlset><url><loc>http://HOST/a</loc></url><url><loc>http://HOST/b</loc></url></urlset>'
    assert channels._sitemap_urls(gzip.compress(xml)) == ["http://HOST/a", "http://HOST/b"]
    assert channels._sitemap_urls(b"\x1f\x8bnot really gzip") == []
    index = b'<sitemapindex><sitemap><loc>http://HOST/pages.xml.gz</loc></sitemap></sitemapindex>'
    srv, base, hits = serve({"/sitemap.xml": (200, "application/xml", index),
                             "/pages.xml.gz": (200, "application/x-gzip", gzip.compress(xml.replace(b"HOST", b"H0ST"))),
                             "/a": page(""), "/b": page("")})
    try:
        import gacrawl.channels as ch
        f = fetcher(tmp_path)
        real = ch._sitemap_entries
        entries = real(f.get(base + "/pages.xml.gz").content)
        assert [loc for loc, _ in entries] == ["http://H0ST/a", "http://H0ST/b"]
    finally:
        srv.shutdown()


def test_cold_links_are_calendars_and_search_forms_not_words_that_contain_them():
    cold = channels.COLD.search
    assert cold("/calendar/2026/11") and cold("/events/") and cold("/Search?term=x") and cold("/event/123")
    assert not cold("/crime-prevention-ordinance") and not cold("/research-and-planning") and not cold("/departments/clerk")
