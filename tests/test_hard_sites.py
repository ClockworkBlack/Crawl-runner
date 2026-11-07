"""Sites that used to stop a source: one oversized file or dropped connection is skipped, not fatal; documents get a
larger ceiling than pages; CivicClerk's relative file links resolve against its API; a Legistar city whose data
service is not set up is read from its public Legistar pages (at night only); a script-built page gets the browser."""
import http.server
import threading
import time

import pytest
import requests

from gacrawl import channels
from gacrawl.fetch import Fetcher, TooLarge
from gacrawl.sources.civicclerk import CivicClerk

BIG = b"%PDF-1.4 " + b"x" * 3000


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html", status=200):
        self.send_response(status); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        if p == "/":
            return self._send(b"<html><body><p>" + b"Council agendas and minutes. " * 20 + b"</p>"
                              b"<a href='/big.pdf'>packet</a> <a href='/page2'>minutes</a></body></html>")
        if p == "/big.pdf":
            return self._send(BIG, "application/pdf")
        if p == "/page2":
            return self._send(b"<html><body><p>" + b"Minutes of the regular meeting. " * 20 + b"</p></body></html>")
        if p == "/shell":
            return self._send(b"<html><head><script src='/app.js'></script></head><body><div id='root'></div></body></html>")
        self._send(b"", status=404)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def fetcher(tmp_path, **kw):
    s = requests.Session(); s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None, **kw)


def test_oversized_document_is_skipped_not_fatal(server, tmp_path):
    f = fetcher(tmp_path, max_bytes=2000, max_doc_bytes=2500)
    with pytest.raises(TooLarge):
        f.get(server + "/big.pdf")
    src = {"id": "x:site", "url": server + "/", "kind": "page", "depth": 2}
    recs = list(channels._html_crawl(f, src, 10, channels.CrawlContext()))
    assert {r.url for r in recs} >= {server + "/", server + "/page2"}
    f2 = fetcher(tmp_path, max_bytes=2000, max_doc_bytes=10_000)   # documents get the larger ceiling
    assert f2.get(server + "/big.pdf").status == 200


def test_civicclerk_relative_file_links(monkeypatch):
    seen = []

    class F:
        def get(self, url, **kw):
            seen.append(url)
            raise requests.ConnectionError("x")
    cc = CivicClerk(F(), "alpharettaga")
    assert cc._open_file({"url": "stream/ALPHARETTAGA/07b16baa.pdf"}, "Council", "2026-10-01", 1) is None
    assert seen == ["https://alpharettaga.api.civicclerk.com/v1/stream/ALPHARETTAGA/07b16baa.pdf"]


def test_legistar_not_set_up_falls_back_to_its_pages(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("Legistar canton/events: HTTP 400 ('Agenda Draft Status' ... is not setup in settings.)")
        yield
    monkeypatch.setattr(channels, "_legistar_api", broken)
    got = []
    monkeypatch.setattr(channels, "_html_crawl", lambda f, src, n, ctx: iter(got.append(src) or []))
    src = {"id": "canton:legistar", "url": "https://canton.legistar.com/", "kind": "council"}
    list(channels.crawl(None, src, {"channel": "api", "endpoint": "legistar:canton"}, {}, ctx=channels.CrawlContext(pages_ok=False)))
    assert got == []                                   # daytime: waits for the night window
    list(channels.crawl(None, src, {"channel": "api", "endpoint": "legistar:canton"}, {}, ctx=channels.CrawlContext(pages_ok=True)))
    assert got[0]["url"] == "https://canton.legistar.com/" and "MeetingDetail.aspx" in got[0]["include"]


def test_script_built_page_gets_the_browser(server, tmp_path):
    probes = channels.probe_source(fetcher(tmp_path), {"id": "c:site", "url": server + "/shell"})
    assert any(p.channel == "browser" for p in probes)


def test_imperva_challenge_shell_is_recognized():
    from gacrawl.blocked import looks_blocked

    shell = b'<html><head><script src="/_Incapsula_Resource?SWJIYLWA=719d"></script></head><body></body></html>'
    assert "Imperva" in looks_blocked(200, shell)
    assert looks_blocked(200, b"<html><body>" + b"Agenda " * 2000 + b"</body></html>") is None


def test_deep_403_does_not_block_the_host(tmp_path):
    """A members-only document answers 403 'Access denied': the page is skipped, the host stays open; the third
    such refusal on one host in a run, or a refusal at the front door, blocks it."""
    import http.server

    class H2(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(b"User-agent: *\nAllow: /\n")
            elif self.path.startswith("/private"):
                self.send_response(403); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(b"<html>Access denied</html>")
            else:
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(b"<html><body>fine</body></html>")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        f = fetcher(tmp_path)
        assert f.get(base + "/private/1.pdf").status == 403
        assert f.get(base + "/page").status == 200           # host still open
        assert f.get(base + "/private/2.pdf").status == 403
        from gacrawl.blocked import Blocked
        with pytest.raises(Blocked):
            f.get(base + "/private/3.pdf")                   # third refusal: the host is left alone
    finally:
        srv.shutdown()


def test_unreachable_host_fails_fast(tmp_path):
    """A host that refuses the connection is tried once; the next request to it fails at once (no retries, no waits)."""
    f = fetcher(tmp_path)
    f.robots.allowed = lambda url: True
    t0 = time.time()
    with pytest.raises(requests.ConnectionError):
        f.get("http://127.0.0.1:9/a")
    with pytest.raises(requests.ConnectionError) as ei:
        f.get("http://127.0.0.1:9/b")
    assert "earlier this run" in str(ei.value) and time.time() - t0 < 5


def test_statistics_csvs_get_the_document_ceiling(tmp_path):
    """A 25 MB+ statistics CSV (GOSA's certified personnel file) is a data file, not a page: it gets the documents'
    ceiling instead of ending the statistics step."""
    import http.server
    import threading

    big = b"SCHOOL_DSTRCT_CD,PERSONNEL_COUNT\n" + b"601,12\n" * 4000

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body, ct = b"User-agent: *\nAllow: /\n", "text/plain"
            else:
                body, ct = big, "text/csv"
            self.send_response(200); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        f = fetcher(tmp_path, max_bytes=2000, max_doc_bytes=100_000)
        r = f.get(f"http://127.0.0.1:{srv.server_address[1]}/2025/CERTIFIED_PERSONNEL_2024-25.csv")
        assert r.status == 200 and len(r.content) == len(big)
    finally:
        srv.shutdown()


def test_nces_feature_services_are_a_listed_data_api():
    from gacrawl import config

    f = config.make_fetcher(config.load())
    assert f._is_api_endpoint("https://services1.arcgis.com/Ua5sjt3LWTPigjyD/arcgis/rest/services/School_Characteristics_Current/FeatureServer/1/query?where=1")
    assert not f._is_api_endpoint("https://services1.arcgis.com/SomeoneElse/arcgis/rest/services/x/FeatureServer/0/query")


def test_slow_services_get_their_timeout_for_the_connection_too(monkeypatch, tmp_path):
    """GDELT's TLS handshake can take longer than the 10 s connect timeout (urllib3 then says 'Read timed out
    (read timeout=10)'): a host in host_timeouts gets that time for connecting and answering alike."""
    f = fetcher(tmp_path)
    f.host_timeouts = {"api.gdeltproject.org": 120.0}
    seen = []

    class R:
        status_code, headers, elapsed = 200, {"Content-Type": "application/json"}, None

        def iter_content(self, n):
            yield b"{}"

    monkeypatch.setattr(f.session, "get", lambda url, headers=None, timeout=None, stream=None: seen.append(timeout) or R())
    monkeypatch.setattr(f.robots, "allowed", lambda url: True)
    monkeypatch.setattr(f.robots, "crawl_delay", lambda url: None)
    f.get("https://api.gdeltproject.org/api/v2/doc/doc?query=x", use_cache=False)
    f.get("https://www.example.org/", use_cache=False)
    assert seen == [(120.0, 120.0), (f.connect_timeout, f.timeout)]
