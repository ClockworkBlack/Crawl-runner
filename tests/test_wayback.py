"""The Wayback Machine for campaign sites that are gone or now someone else's, and for articles that are gone: the last
capture that was still theirs, its pages, the archive switched off at the first 429, and never for a closed page."""
import json
from urllib.parse import parse_qsl, urlsplit

from gacrawl.candidates import CAMPAIGN_HOT, Researcher
from gacrawl.fetch import FetchResult, Fetcher
from gacrawl.stances import Extractor, page_is_theirs, taken_over
from gacrawl.wayback import CDX, RAW, Wayback

CAND = {"id": "jane-smith", "name": "Jane Smith", "aliases": []}
THEIRS = "<html><head><title>Jane Smith for Commission</title></head><body><h1>Jane Smith</h1><p>I am running for " \
         "county commission to fix our roads and keep taxes low for every family in the district.</p></body></html>"
CASINO = "<html><head><title>Situs Slot Gacor</title></head><body><p>Situs slot gacor terpercaya, daftar judi online " \
         "casino deposit pulsa, bandar togel jackpot hari ini.</p></body></html>"


def res(url, status=200, body="", ctype="text/html; charset=utf-8"):
    return FetchResult(url, status, {"content-type": ctype}, body.encode(), 0.1)


class FakeArchive:
    """web.archive.org: captures {page address: [(timestamp, html)]}; counts requests; can answer 429 from the nth."""

    def __init__(self, captures, live=None, fail_from=None):
        self.caps, self.live, self.fail_from, self.calls = captures, live or {}, fail_from, []

    def get(self, url, params=None, **kw):
        self.calls.append(url)
        if self.fail_from is not None and len(self.calls) >= self.fail_from and "archive.org" in url:
            return res(url, 429)
        if url == CDX:
            q = dict(parse_qsl("&".join(f"{k}={v}" for k, v in params if k != "filter"), keep_blank_values=True))
            want, until = q["url"], q.get("to", "99999999999999")
            rows = []
            for page, caps in self.caps.items():
                hit = page.startswith(want[:-1]) if want.endswith("*") else page.rstrip("/") == want.split("//")[-1].rstrip("/")
                if hit:
                    rows += [[page.split("/", 1)[0] + ")/" + page.split("/", 1)[1], ts, "https://" + page] for ts, _ in caps if ts <= until]
            rows.sort(key=lambda r: r[1])
            lim = int(q["limit"])
            rows = rows[lim:] if lim < 0 else rows[:lim]
            return res(url, 200, json.dumps([["urlkey", "timestamp", "original"]] + rows), "application/json")
        if url.startswith("https://web.archive.org/web/"):
            ts, page = url.split("/web/", 1)[1].split("id_/", 1)
            for t, html in self.caps.get(page.split("//", 1)[1], []):
                if t == ts:
                    return res(url, 200, html)
            return res(url, 404)
        return self.live.get(url) or res(url, 404)


def test_taken_over_and_theirs():
    ex = Extractor(CAND)
    assert taken_over(ex, "Situs slot gacor terpercaya, daftar judi online") == "a gambling or spam page"
    assert taken_over(ex, "This domain is for sale! Make an offer today.") == "a parked or for-sale domain"
    assert taken_over(ex, "") == ""                                  # a script-built page: not taken for someone else's
    assert taken_over(ex, "Sign up for a volunteer time slot") == ""   # one spam word on a real page is not enough
    assert taken_over(ex, "Jane Smith: casino revenue should fund schools") == ""   # names her: hers
    assert page_is_theirs(ex, "Our plan for roads", "janesmithforga.com")
    assert not page_is_theirs(ex, "Situs slot gacor", "janesmithforga.com")


def test_last_good_capture_before_the_domain_changed_hands():
    caps = [(f"2024{m:02d}01000000", THEIRS.replace("roads", f"roads {m}")) for m in range(1, 8)] + \
           [(f"2025{m:02d}01000000", CASINO.replace("hari", f"hari {m}")) for m in range(1, 10)]
    fa = FakeArchive({"janesmithforga.com/": caps})
    ex = Extractor(CAND)
    wb = Wayback(fa, log=lambda *a: None)
    got = wb.last_good("https://janesmithforga.com/", lambda t, md: page_is_theirs(ex, t + md, "janesmithforga.com")
                       and not taken_over(ex, t + md))
    assert got and got["timestamp"] == "20240701000000"             # the last capture that was hers
    assert len([u for u in fa.calls if "/web/" in u]) <= 8           # a binary search, not every capture


def test_site_pages_issue_pages_first_and_nothing_after_the_change():
    fa = FakeArchive({"janesmithforga.com/": [("20240701000000", THEIRS)],
                      "janesmithforga.com/issues": [("20240601000000", THEIRS)],
                      "janesmithforga.com/about": [("20240501000000", THEIRS), ("20250301000000", CASINO)],
                      "janesmithforga.com/donate": [("20240601000000", THEIRS)],
                      "janesmithforga.com/2024/05/blog-post-with-a-long-name": [("20240601000000", THEIRS)],
                      "janesmithforga.com/style.css": [("20240601000000", "x")]})
    wb = Wayback(fa, log=lambda *a: None)
    home = {"timestamp": "20240701000000", "original": "https://janesmithforga.com/", "title": "", "md": ""}
    pages = wb.site_pages(home, max_pages=4, hot=CAMPAIGN_HOT, exclude=["/donate"])
    assert pages[0] is home
    assert [p["original"].split(".com")[1] for p in pages[1:]] == ["/about", "/issues", "/2024/05/blog-post-with-a-long-name"]
    assert pages[1]["timestamp"] == "20240501000000"                  # the copy from before the casino, not after


def test_first_429_switches_the_archive_off():
    fa = FakeArchive({"janesmithforga.com/": [("20240101000000", THEIRS)]}, fail_from=1)
    logs = []
    wb = Wayback(fa, log=logs.append)
    assert wb.captures("https://janesmithforga.com/") == [] and wb.off == "HTTP 429 from web.archive.org"
    n = len(fa.calls)
    assert wb.page("20240101000000", "https://janesmithforga.com/") is None and len(fa.calls) == n   # not asked again
    assert not wb.take("site") and len(logs) == 1


def test_fetcher_does_not_retry_a_429_from_the_archive(tmp_path):
    class Resp:
        status_code, headers, elapsed = 429, {"Retry-After": "30"}, None

        def iter_content(self, n):
            yield b""

        def close(self):
            pass

    class Session:
        headers, urls = {}, []

        def get(self, url, *a, **k):
            Session.urls.append(url)
            return Resp()

        def mount(self, *a, **k):
            pass
    slept = []
    f = Fetcher("t", tmp_path, min_interval=0, session=Session(), sleep=slept.append,
                host_retries={"web.archive.org": 0}, api_endpoints=["web.archive.org/cdx/search/cdx"])
    r = f.get(CDX, params=[("url", "x.com/")])
    asked = [u for u in Session.urls if not u.endswith("/robots.txt")]
    assert r.status == 429 and len(asked) == 1 and not slept         # handed back at once: no wait, no second ask


class Store:
    def __init__(self):
        self.kv, self.marked = {}, []

    def kv_get(self, k):
        return self.kv.get(k)

    def kv_set(self, k, v):
        self.kv[k] = v

    def mark_url(self, *a):
        self.marked.append(a)


def researcher(fetcher):
    r = object.__new__(Researcher)
    r.f, r.store, r.log, r.root = fetcher, Store(), (lambda *a: None), None
    r.opts, r._opened = {"site_max_pages": 5}, {}
    r.wayback = Wayback(fetcher, log=lambda *a: None)
    return r


def test_a_casino_domain_is_read_from_its_archived_copy():
    site = "https://janesmithforga.com/"
    fa = FakeArchive({"janesmithforga.com/": [("20240701000000", THEIRS), ("20250901000000", CASINO)],
                      "janesmithforga.com/issues": [("20240601000000", THEIRS.replace("roads", "roads and parks"))]},
                     live={site: res(site, 200, CASINO)})
    r = researcher(fa)
    assert r._taken_over(CAND, site) == "a gambling or spam page"
    got = []
    assert r._archived_site(CAND, "cand:jane-smith:site0", site, got.append, "now a gambling or spam page") == 2
    assert [x.url for x in got] == ["https://web.archive.org/web/20240701000000/https://janesmithforga.com/",
                                    "https://web.archive.org/web/20240601000000/https://janesmithforga.com/issues"]
    assert got[0].meta["original"] == site and got[0].meta["archived"] == "2024-07-01" and got[0].kind == "statement"
    assert got[0].evidence == "primary" and got[0].published == "2024-07-01"
    n = len(fa.calls)
    assert r._archived_site(CAND, "cand:jane-smith:site0", site, got.append, "gone") == 0 and len(fa.calls) == n   # read once


def test_a_live_site_of_theirs_is_not_replaced():
    site = "https://janesmithforga.com/"
    r = researcher(FakeArchive({}, live={site: res(site, 200, THEIRS)}))
    assert r._taken_over(CAND, site) == ""
    r2 = researcher(FakeArchive({}, live={site: res(site, 200, "<html><body><div id=app></div></body></html>")}))
    assert r2._taken_over(CAND, site) == ""                           # a script-built page with no text: left alone


def test_gone_articles_come_from_the_archive_closed_ones_never():
    gone, paywalled = "https://www.ajc.com/politics/gone-story/", "https://www.ajc.com/politics/paid-story/"
    fa = FakeArchive({"www.ajc.com/politics/gone-story/": [("20251001000000", "<html><title>Story</title><body><p>Jane Smith "
                                                           "spoke at the meeting about the county budget and roads.</p></body></html>")],
                      "www.ajc.com/politics/paid-story/": [("20251001000000", THEIRS)]},
                     live={gone: res(gone, 404), paywalled: res(paywalled, 403)})
    r = researcher(fa)
    page = r._open_article(gone)
    assert page["url"] == gone and page["archived"] == "2025-10-01" and "county budget" in page["text"]
    assert page["archived_url"].startswith("https://web.archive.org/web/20251001000000/")
    assert r._open_article(paywalled) is None
    assert not any("paid-story" in u for u in fa.calls if "archive.org" in u)
