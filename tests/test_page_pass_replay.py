"""The 18 sources that were "partial" on every run from Nov. 14 to 19, 2026, replayed with their own numbers.

For each: the pages one share read (state/progress.json `fetched`, Nov. 19 16:15 UTC run; the browser source's records
seen), the pages below that were already stored and skipped as read (`skipped`), and the source's page count a pass
(max_pages, config). The site modelled from them: listing pages (the home page and what it links to) a few more than
one share reads, which is why no share got past them; below them the pages already stored, and as many again that no
share had ever reached. The old crawl (no cursor) reads the same listing pages on every share; the new one completes,
reading no page twice in a pass."""
import contextlib
import math
import sys
import types
from collections import Counter

import pytest

from gacrawl import channels
from gacrawl.fetch import FetchResult, OutOfTime
from gacrawl.store import Store

# id, channel, pages read in one share, pages skipped as already read, max_pages
SOURCES = [
    ("duluth:site", "html", 76, 6, 120), ("valdosta:site", "feed", 23, 753, 120), ("rome:site", "feed", 28, 49, 120),
    ("south-fulton:site", "feed", 32, 115, 120), ("south-fulton:agendas", "html", 57, 624, 150),
    ("waycross:site", "feed", 22, 15, 120), ("waycross:agendas", "html", 59, 157, 150), ("winder:site", "html", 84, 2, 120),
    ("co-floyd:site", "html", 83, 2, 120), ("co-colquitt:site", "html", 87, 3, 120), ("co-greene:agendas", "html", 44, 116, 150),
    ("co-barrow:site", "feed", 6, 91, 120), ("co-barrow:agendas", "html", 7, 90, 150), ("brookhaven:site", "feed", 21, 70, 120),
    ("dalton:agendas", "html", 8, 17, 150), ("co-emanuel:site", "feed", 34, 619, 120), ("co-emanuel:agendas", "html", 41, 595, 150),
    ("sd-haralson:site", "browser", 22, 0, 80),
]
BODY = "<p>" + "The board met and approved the minutes of its last meeting. " * 6 + "</p>"


def model(base: str, share: int, stored: int):
    """-> ({path: html}, [paths below the listing pages that are already stored])."""
    listing = share + 5                                    # more than a share reads
    below = stored + share                                 # stored already, and as many never reached
    per = math.ceil(below / listing)
    site, deep = {}, []
    menu = "".join(f'<a href="{base}/meetings-{i}">Meetings {i}</a> ' for i in range(listing))
    site["/"] = f"<html><title>Home</title><body>{menu}{BODY}</body></html>"
    n = 0
    for i in range(listing):
        links = ""
        for k in range(per):
            if n >= below:
                break
            links += f'<a href="{base}/meetings-{i}/minutes-{k}">Minutes {k}</a> '
            site[f"/meetings-{i}/minutes-{k}"] = f"<html><title>Minutes {i}.{k}</title><body>{BODY} {i} {k}</body></html>"
            deep.append(f"/meetings-{i}/minutes-{k}")
            n += 1
        site[f"/meetings-{i}"] = f"<html><title>Meetings {i}</title><body>{menu}{links}{BODY} {i}</body></html>"
    return site, deep[:stored]


class Net:
    """The site as a fetcher: `share` page requests, then the share's time is up."""
    user_agent = "Test/1"

    def __init__(self, base, site, share, feed=None):
        self.base, self.site, self.share, self.left, self.hits, self.feed = base, site, share, share, [], feed

    def get(self, url, **kw):
        if url == self.base + "/feed.xml":
            return FetchResult(url, 200, {"content-type": "application/rss+xml"}, self.feed, 0.0)
        if self.left <= 0:
            raise OutOfTime("this source's time slice used up")
        self.left -= 1
        path = url[len(self.base):] or "/"
        self.hits.append(path)
        if path not in self.site:
            return FetchResult(url, 404, {"content-type": "text/html"}, b"", 0.0)
        return FetchResult(url, 200, {"content-type": "text/html; charset=utf-8"}, self.site[path].encode(), 0.0)

    # the browser crawl's own checks
    def allowed(self, url):
        return True

    def _check_time(self):
        if self.left <= 0:
            raise OutOfTime("this source's time slice used up")

    def _turn(self, url, delay):
        self.left -= 1

    class robots:
        @staticmethod
        def crawl_delay(url):
            return None

    class limiter:
        @staticmethod
        def record(*a, **k):
            pass

    class blocklist:
        @staticmethod
        def add(*a, **k):
            pass

        @staticmethod
        def is_blocked(url):
            return None

    def host(self, url):
        return self.base


@contextlib.contextmanager
def fake_browser(net: Net, monkeypatch):
    """Playwright's place taken by the modelled site (the browser crawl's own code runs)."""
    class Resp:
        def __init__(self, status):
            self.status = status

    class Page:
        html = ""

        def goto(self, url, **kw):
            path = url[len(net.base):] or "/"
            net.hits.append(path)
            self.html = net.site.get(path, "")
            return Resp(200 if path in net.site else 404)

        def wait_for_load_state(self, *a, **k): pass
        def wait_for_timeout(self, *a, **k): pass
        def content(self): return self.html
        def title(self): return ""
        def query_selector(self, sel): return None

    class Browser:
        def new_page(self, **kw): return Page()
        def close(self): pass

    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: PW()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    import gacrawl.fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "launch_browser", lambda p: Browser())
    yield


def shares_until_done(net, store, src, chosen, resume, max_shares):
    """-> the number of shares the pass took, or None if it was not over after max_shares."""
    for share in range(1, max_shares + 1):
        net.left = net.share
        ctx = channels.CrawlContext(frontier=store, cursors=store.cursors(src["id"]) if resume else {}, resume=resume)
        try:
            for item in channels.crawl(net, src, chosen, {}, ctx=ctx):
                if isinstance(item, channels.Checkpoint):
                    store.set_cursor(src["id"], item.stream, item.value)
                else:
                    store.add(item)
        except OutOfTime:
            continue
        return share
    return None


@pytest.mark.parametrize("sid,channel,share,stored,max_pages", SOURCES, ids=[s[0] for s in SOURCES])
def test_the_source_completes_where_it_used_to_reread_its_listing_pages(tmp_path, monkeypatch, sid, channel, share, stored, max_pages):
    base = "http://site.test"
    site, known = model(base, share, stored)
    feed = (f'<?xml version="1.0"?><rss version="2.0"><channel><title>News</title><item><title>News</title><link>{base}'
            f"/meetings-0</link><guid>1</guid><pubDate>Mon, 02 Nov 2026 19:00:00 GMT</pubDate></item></channel></rss>").encode()
    src = {"id": sid, "url": base + "/", "kind": "page", "depth": 3, "max_pages": max_pages, "coverage": True}
    chosen = {"channel": channel, "endpoint": base + ("/feed.xml" if channel == "feed" else "/")}
    listing = {"/"} | {p for p in site if p.count("/") == 1 and p != "/"}

    results = {}
    for mode in ("old", "new"):
        store = Store(tmp_path / mode)
        for p in known:                                    # what earlier runs had stored: skipped as read
            store.mark_url(base + p, 200, sid, "text/html", b"x")
        net = Net(base, site, share, feed)
        ctxm = fake_browser(net, monkeypatch) if channel == "browser" else contextlib.nullcontext()
        with ctxm:
            results[mode] = (shares_until_done(net, store, src, chosen, resume=(mode == "new"), max_shares=(60 if mode == "new" else 8)), Counter(net.hits))
        store.close()

    old_shares, old_hits = results["old"]
    assert old_shares is None                               # a run's 8 shares and still "partial"
    assert set(old_hits) <= listing and old_hits["/"] == 8    # the same listing pages every share, nothing below them

    new_shares, new_hits = results["new"]
    assert new_shares is not None, new_hits.most_common(2)
    if channel == "feed":
        # the one page the feed links to is read as the feed's item, and once more as a listing page (for its links)
        assert new_hits.pop("/meetings-0") == 2
        new_hits["/meetings-0"] = 1
    assert max(new_hits.values()) == 1, (new_shares, new_hits.most_common(2))
    unread = set(site) - {p for p in known}                 # every page not already stored
    want = min(len(unread), max_pages)                      # ... up to the pass's page count
    assert len(new_hits) >= want and set(new_hits) <= unread
    assert new_shares <= math.ceil(len(new_hits) / share) + 1
