"""The Wayback Machine (web.archive.org) for pages that are gone or are now someone else's.

A campaign site whose domain lapsed and was bought for a casino or parked for sale, or that answers 404 / no such
host, is read from its last capture that was still the candidate's (the newest capture of the home page that names
them; when the newest is someone else's, a binary search over the captures finds where the domain changed hands). Its
pages are read as captured on or before that day, at most `site_max_pages`, the issue, about and endorsement pages
first. A news article that answers 404 or 410 is read from its newest capture.

Never for a page that answers but is closed: a paywall, a login or a refusal (401, 402, 403, a challenge page) is not
gone, and its text is not fetched from an archive.

Polite by the Internet Archive's limits: one request every 4 seconds (http.host_intervals; its capture index allows
about 24 a minute), no retry after a 429 (http.host_retries: 0), and the first 429, 5xx, refusal or robots.txt
disallow switches the archive off for the rest of the run (an address that keeps asking for over a minute after a 429
is blocked for an hour). The capture index (CDX) is a documented public API (http.api_endpoints); the archived pages
are pages, read only where web.archive.org's robots.txt allows.
"""
from __future__ import annotations

import re
import threading
from urllib.parse import urlsplit

from . import extract
from .blocked import Blocked
from .fetch import Disallowed, OutOfTime

CDX = "https://web.archive.org/cdx/search/cdx"
RAW = "https://web.archive.org/web/{ts}id_/{url}"   # the page as captured, without the archive's banner and links
SHOW = "https://web.archive.org/web/{ts}/{url}"     # the copy people follow from the site
SKIP_PAGE = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|json|xml|txt|pdf|zip|mp[34]|mov)(?:$|\?)|[?&](?:replytocom|share|"
                       r"p|page_id|s|amp)=|/(?:feed|wp-json|wp-admin|wp-login|xmlrpc|cart|checkout|my-account)\b", re.I)


def day(ts: str) -> str:
    """'20240517093012' -> '2024-05-17'."""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def host_of(url: str) -> str:
    return urlsplit(url if "//" in url else "http://" + url).netloc.lower().split(":")[0].removeprefix("www.")


class Wayback:
    """One per run: the archive's state (switched off after a 429 or a refusal) and the run's lookup budget."""

    def __init__(self, fetcher, log=print, sites_per_run: int = 15, articles_per_run: int = 25, max_reads: int = 8):
        self.f = fetcher
        self.log = log
        self.off = ""                       # why the archive is not asked again this run
        self.sites_left = int(sites_per_run)
        self.articles_left = int(articles_per_run)
        self.max_reads = int(max_reads)     # home-page captures read to find the last one that was theirs
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ requests
    def _switch_off(self, why: str) -> None:
        with self._lock:
            if not self.off:
                self.off = why
                self.log(f"  archive: off for this run ({why})")

    def _get(self, url: str, params=None):
        """The answer, or None (and the archive switched off) on a 429, a 5xx, a refusal or a robots.txt disallow."""
        if self.off:
            return None
        try:
            r = self.f.get(url, params=params)
        except OutOfTime:
            raise
        except (Disallowed, Blocked) as exc:
            self._switch_off(f"{type(exc).__name__}: {exc}"[:160])
            return None
        except Exception as exc:  # noqa: BLE001  a dropped connection or a timeout: this answer only
            self.log(f"  archive: {type(exc).__name__} for {url[:120]}")
            return None
        if r.status == 429 or r.status >= 500:
            self._switch_off(f"HTTP {r.status} from {host_of(url)}")
            return None
        return r

    def take(self, what: str) -> bool:
        """Spend one of the run's site or article lookups."""
        with self._lock:
            if self.off:
                return False
            left = "sites_left" if what == "site" else "articles_left"
            if getattr(self, left) <= 0:
                return False
            setattr(self, left, getattr(self, left) - 1)
            return True

    # ------------------------------------------------------------------ the capture index
    def captures(self, url: str, *, prefix: bool = False, until: str = "", newest: int = 40, limit: int = 3000) -> list[dict]:
        """Captures of a page (oldest first; one per distinct content, the newest `newest`), or with prefix of every
        page under it up to `until` (a 14-digit timestamp): [{"urlkey", "timestamp", "original"}]. Only answers that
        were HTML pages with status 200."""
        params = [("url", url.rstrip("*") + ("*" if prefix else "")), ("output", "json"), ("fl", "urlkey,timestamp,original"),
                  ("filter", "statuscode:200"), ("filter", "mimetype:text/html"), ("collapse", "digest")]
        if until:
            params.append(("to", until))
        params.append(("limit", str(limit) if prefix else f"-{int(newest)}"))
        r = self._get(CDX, params)
        if r is None or r.status != 200:
            return []
        try:
            rows = r.json()
        except ValueError:
            return []
        if not rows or not isinstance(rows, list):
            return []
        head = rows[0]
        out = [dict(zip(head, row)) for row in rows[1:] if len(row) == len(head)]
        return sorted(out, key=lambda c: c.get("timestamp", ""))

    def page(self, ts: str, url: str) -> tuple[str, str] | None:
        """(title, markdown) of a page as captured, or None."""
        r = self._get(RAW.format(ts=ts, url=url))
        if r is None or r.status != 200 or "html" not in r.content_type:
            return None
        title, md = extract.html_to_markdown(r.text, url)
        return (title or "", md) if md else None

    # ------------------------------------------------------------------ campaign sites
    def last_good(self, url: str, good) -> dict | None:
        """The newest capture of `url` whose page good(title, markdown) accepts: the newest itself when it passes,
        else a binary search over the older captures for where the page stopped passing (a domain changes hands once),
        reading at most `max_reads` captures. {"timestamp", "original", "title", "md"} or None."""
        caps = self.captures(url)
        if not caps:
            return None
        reads, seen = [0], {}

        def ok(i: int):
            if i not in seen:
                if reads[0] >= self.max_reads or self.off:
                    return None
                reads[0] += 1
                p = self.page(caps[i]["timestamp"], caps[i]["original"])
                seen[i] = p if (p and good(*p)) else None
            return seen[i]

        found = None
        if ok(len(caps) - 1):
            found = len(caps) - 1
        else:
            lo, hi = 0, len(caps) - 2
            while lo <= hi and reads[0] < self.max_reads and not self.off:
                mid = (lo + hi + 1) // 2
                if ok(mid):
                    found, lo = mid, mid + 1
                else:
                    hi = mid - 1
        if found is None:
            return None
        title, md = seen[found]
        return {**caps[found], "title": title, "md": md}

    def site_pages(self, home: dict, *, max_pages: int, hot: re.Pattern | None = None, exclude: list[str] | None = None) -> list[dict]:
        """The site's pages as captured on or before the home page's capture (the latest capture of each, at most
        `max_pages`): the home page, then pages whose address looks like issues, about or endorsements, then the
        shortest addresses."""
        host = host_of(home["original"])
        caps = self.captures(host + "/", prefix=True, until=home["timestamp"])
        latest: dict[str, dict] = {}
        for c in caps:
            k = c.get("urlkey") or c["original"]
            if c["timestamp"] <= home["timestamp"] and (k not in latest or c["timestamp"] > latest[k]["timestamp"]):
                latest[k] = c
        pages = []
        for c in latest.values():
            u = c["original"]
            if host_of(u) != host or SKIP_PAGE.search(u) or any(x in u.lower() for x in (exclude or [])):
                continue
            pages.append(c)

        def rank(c):
            path = urlsplit(c["original"]).path.rstrip("/")
            return (path != "", not (hot and hot.search(path)), len(path), path)
        pages.sort(key=rank)
        home_key = urlsplit(home["original"]).path.rstrip("/")
        pages = [c for c in pages if urlsplit(c["original"]).path.rstrip("/") != home_key]
        return [home] + pages[: max(0, max_pages - 1)]

    # ------------------------------------------------------------------ news
    def article(self, url: str) -> dict | None:
        """The newest capture of an article that is gone: {"url", "archived", "archived_url", "title", "text"}."""
        if not self.take("article"):
            return None
        caps = self.captures(url, newest=1)
        if not caps:
            return None
        c = caps[-1]
        p = self.page(c["timestamp"], c["original"])
        if not p:
            return None
        return {"url": url, "title": p[0], "text": p[1], "archived": day(c["timestamp"]),
                "archived_url": SHOW.format(ts=c["timestamp"], url=c["original"])}
