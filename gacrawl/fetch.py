"""HTTP fetcher: every request passes robots.txt, the rate limiter, and a
conditional-request cache (ETag / Last-Modified) so re-runs only download what
changed."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .blocked import Blocked, BlockList, looks_blocked
from .polite import RateLimiter, RobotsCache, SlotPastDeadline, decode_robots, host_of, parse_retry_after


# A connection that fails: refused, timed out, or dropped while the body was being read. requests raises the last as
# ChunkedEncodingError (or ContentDecodingError), which is neither a ConnectionError nor a Timeout, so it used to end
# the whole source instead of being tried again ("co-dekalb:legistar: error (api): ChunkedEncodingError: Connection
# broken: BrokenPipeError", Nov. 11, 14, 15, 16 and 18, 2026).
NET_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
              requests.exceptions.ContentDecodingError)


class OutOfTime(Exception):
    """The run's time budget is spent: raised at the next request so a long source stops promptly."""


class TooLarge(ValueError):
    """A response over the size ceiling (25 MB for pages, more for documents): skipped, never half-read."""


class Disallowed(Exception):
    """robots.txt (or the site's response to it) says this URL is off-limits. `reason` says why: the host could
    not be reached, robots.txt answered 429 or 5xx, or its rules disallow the path (see RobotsCache.why)."""

    def __init__(self, url: str, reason: str = ""):
        super().__init__(f"{url} ({reason})" if reason else url)
        self.url, self.reason = url, reason


__all__ = ["Fetcher", "FetchResult", "Disallowed", "Blocked"]


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict
    content: bytes
    elapsed: float
    not_modified: bool = False
    final_url: str = ""          # where the answer came from when the address redirected (else empty)

    @property
    def text(self) -> str:
        enc = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip() or enc
        return self.content.decode(enc, errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json(self):
        return json.loads(self.content)


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        state_dir: Path,
        min_interval: float = 2.0,
        timeout: float = 30.0,
        max_bytes: int = 25_000_000,
        max_doc_bytes: int = 150_000_000,
        max_retries: int = 3,
        session: requests.Session | None = None,
        sleep=time.sleep,
        blocked_ttl_days: int = 7,
        pool_maxsize: int = 32,
        host_intervals: dict | None = None,
        api_endpoints: list[str] | None = None,
        host_timeouts: dict | None = None,
        after_answer: list | None = None,
        host_retries: dict | None = None,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        # per-host read timeouts (seconds) for services that are slow to answer but do answer (GDELT's search)
        self.host_timeouts = {k.lower(): float(v) for k, v in (host_timeouts or {}).items()}
        self.max_bytes = max_bytes
        self.max_doc_bytes = max(max_bytes, max_doc_bytes)   # agenda packets and minutes books run large
        self.max_retries = max_retries
        # per-host retries after a 429/503: the Internet Archive blocks an address for an hour when it keeps asking after
        # a 429 for over a minute, so web.archive.org gets none (its 429 comes back to the caller, which stops asking)
        self.host_retries = {k.lower(): int(v) for k, v in (host_retries or {}).items()}
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.set_pool(pool_maxsize)
        self.limiter = RateLimiter(min_interval, sleep=sleep, host_intervals=host_intervals, after_answer=after_answer)
        self.robots = RobotsCache(self._raw_get_text, user_agent)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = self.state_dir / "http_cache"
        self._cache_dir.mkdir(exist_ok=True)
        self.blocklist = BlockList(self.state_dir / "blocked.json", blocked_ttl_days)
        self._refusals: dict[str, int] = {}
        self._refusal_lock = threading.Lock()
        self._dead: dict[str, str] = {}            # host -> why it could not be reached (this run only)
        self.quiet_hosts: dict[str, str] = {}      # host -> why a page pass gave up on it this run (channels._Streak)
        self._served: set[str] = set()             # hosts that have answered a page with 200 this run (_host_refused)
        self._https: set[str] = set()              # hosts whose http addresses redirect to https (see _upgraded)
        self.connect_timeout = min(10.0, timeout)
        self._memo: dict = {}                 # (url, headers) -> (monotonic time, FetchResult), see get()
        self._memo_lock = threading.Lock()
        self._tls = threading.local()  # per-worker source id, so block records name the right source
        # Documented public APIs whose operators invite programmatic use (with an identifying User-Agent and a
        # modest rate) even though robots.txt, which is written for crawlers indexing pages, disallows the path.
        # Spacing, back-off and the block list still apply. Configured explicitly in settings (http.api_endpoints).
        self.api_endpoints = [e.lower().rstrip("/") for e in (api_endpoints or [])]

    def _is_api_endpoint(self, url: str) -> bool:
        u = re.sub(r"^https?://", "", url.lower())
        return any(u == e or u.startswith(e + "?") or u.startswith(e + "/") for e in self.api_endpoints)

    def latency_kind(self, url: str, headers: dict | None = None, is_doc: bool = False, kind: str | None = None) -> str:
        """Which answers a response's time is compared with (polite.RateLimiter.record): documents with documents,
        pages with pages, and each API endpoint with itself. One host's endpoints answer at very different speeds
        (a token in 0.2 s, a 100-line search in 5 s, a 10,000-file listing in 20 s); compared with each other, every
        normal answer of the slow endpoint looked like a strained server and the host's spacing climbed to its cap."""
        if kind:
            return kind
        if is_doc:
            return "doc"
        path = urlsplit(url).path or "/"
        accept = str((headers or {}).get("Accept") or (headers or {}).get("accept") or "").lower()
        if "json" in accept or "/api/" in path.lower() or self._is_api_endpoint(url):
            tpl = re.sub(r"[A-Za-z]*\d[\w-]*", "#", path.rstrip("/"))     # ids and numbers: one endpoint, many records
            return "api:" + "/".join(tpl.split("/")[:7])
        return "page"

    @property
    def source_id(self) -> str | None:
        return getattr(self._tls, "source_id", None)

    @source_id.setter
    def source_id(self, value: str | None) -> None:
        self._tls.source_id = value

    def set_pool(self, pool_maxsize: int) -> None:
        """Size the connection pool to the number of concurrent workers.

        urllib3's default pool_maxsize is 10, so with more worker threads the
        extra ones discard and re-open connections every request ("Connection
        pool is full, discarding connection"). Sizing the pool to the worker
        count keeps one keep-alive connection per host hot, which is most of
        the per-request latency on a many-host crawl. A small connect/read
        Retry smooths transient network blips without touching our own 429/503
        and bot-protection handling (status_forcelist is empty on purpose).
        """
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        self.pool_maxsize = max(10, int(pool_maxsize))
        # one connection retry inside the pool; the loop in _get decides anything beyond that (a host that does not
        # resolve or refuses the connection is not asked again this run)
        retry = Retry(total=1, connect=1, read=0, status=0, redirect=0, backoff_factor=0.5,
                      status_forcelist=[], respect_retry_after_header=False)
        adapter = HTTPAdapter(pool_connections=self.pool_maxsize, pool_maxsize=self.pool_maxsize,
                              max_retries=retry, pool_block=False)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # robots.txt itself is fetched directly (still rate limited)
    def _raw_get_text(self, url: str) -> tuple[int, str]:
        self.limiter.wait(url)
        t0 = time.monotonic()
        r = self.session.get(url, timeout=self.timeout)
        self.limiter.record(url, r.status_code, r.elapsed.total_seconds() if getattr(r, "elapsed", None) is not None
                            else time.monotonic() - t0)
        reason = looks_blocked(r.status_code, r.content)
        if reason and "challenge" in reason:
            # a bot-protection challenge in place of robots.txt: the site refuses the crawler (never worked around)
            self.blocklist.add(url, f"robots.txt: {reason}", self.source_id)
            raise Blocked(host_of(url), reason)
        # A plain 401/403 for this one file is not that: RFC 9309 calls the file "unavailable" and the site open
        # (RobotsCache). Many servers forbid /robots.txt and serve their pages; until Nov. 20, 2026 the short
        # "403 Forbidden" page put the whole host on the block list (57 hosts in state/blocked.json, 15 sources).
        # If the site does refuse the crawler, its front page says so on the next request and the host is blocked then.
        # decoded from its bytes as UTF-8 (polite.decode_robots), not by the charset the server names or leaves out
        return r.status_code, decode_robots(r.content)

    def allowed(self, url: str) -> bool:
        return self.robots.allowed(url)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self._cache_dir / f"{key}.meta.json", self._cache_dir / f"{key}.body"

    MEMO_TTL = 900            # seconds: the same page asked for twice in one run (probe, then crawl; seed, then crawl)
    MEMO_MAX_BYTES = 2_000_000

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, use_cache: bool = True,
            kind: str | None = None) -> FetchResult:
        full = requests.Request("GET", url, params=params).prepare().url
        blocked = self.blocklist.is_blocked(full)
        if blocked:
            raise Blocked(host_of(full), blocked["reason"])
        if not self._is_api_endpoint(full) and not self.robots.allowed(full):
            raise Disallowed(full, self.robots.why(full))
        memo_key = (full, tuple(sorted((headers or {}).items())))
        if use_cache:
            hit = self._memo.get(memo_key)
            if hit and time.monotonic() - hit[0] < self.MEMO_TTL:
                return dataclasses.replace(hit[1], not_modified=True, elapsed=0.0)   # same content as earlier this run
        res = self._follow(full, headers, use_cache, kind)
        if use_cache and res.status == 200 and len(res.content) <= self.MEMO_MAX_BYTES:
            with self._memo_lock:
                if len(self._memo) >= 2000:
                    for k in list(self._memo)[:500]:
                        self._memo.pop(k, None)
                self._memo[memo_key] = (time.monotonic(), res)
        return res

    MAX_HOPS = 10

    def _follow(self, full: str, headers: dict | None, use_cache: bool, kind: str | None) -> FetchResult:
        """The answer to `full`, following redirects one hop at a time. Each hop to another host is a request of its
        own: the block list, robots.txt, that host's spacing and Crawl-delay apply before it is asked. (`requests`
        used to follow redirects itself: a page on another host was requested before that host's robots.txt had been
        read, whatever its block-list entry said.) A hop within one host (a trailing slash, http to https, the
        Wayback Machine's hop to a capture's own timestamp) passes the block list and robots.txt too; it waits the
        host's Crawl-delay when one is stated and is otherwise asked at once and charged to the host's spacing. The result keeps the address asked for
        as `url`; `final_url` is where the answer came from. A loop, or more than MAX_HOPS hops, is an error
        ("gave up on ...": a page the callers skip), not a string of requests."""
        from urllib.parse import urldefrag, urljoin

        at = self._upgraded(full)
        res = self._get(at, headers, use_cache, kind)
        seen = {at}
        while res.status in (301, 302, 303, 307, 308) and res.headers.get("location"):
            # the header's bytes are UTF-8 (http.client hands them over as Latin-1): read as `requests` reads them, or
            # a redirect to "/café minutes.pdf" asks for an address that is not there
            loc = res.headers["location"].strip()
            try:
                loc = loc.encode("latin-1").decode("utf-8", "replace")
            except UnicodeEncodeError:
                pass
            nxt = urldefrag(urljoin(at, loc))[0]
            if not nxt.startswith(("http://", "https://")):
                break
            if nxt in seen or len(seen) > self.MAX_HOPS:
                raise RuntimeError(f"gave up on {full}: its redirects " + ("go round in a circle" if nxt in seen else f"do not end ({self.MAX_HOPS} followed)"))
            seen.add(nxt)
            blocked = self.blocklist.is_blocked(nxt)
            if blocked:
                raise Blocked(host_of(nxt), blocked["reason"])
            if not self._is_api_endpoint(nxt) and not self.robots.allowed(nxt):
                raise Disallowed(nxt, self.robots.why(nxt))
            a, b = urlsplit(at), urlsplit(nxt)
            same_host = a.netloc.lower() == b.netloc.lower()
            if a.scheme == "http" and b.scheme == "https" and same_host:
                self._https.add(a.netloc.lower())       # the host's http addresses are asked for as https from now on
            if not same_host and headers:
                # what was sent for the first host is not the next one's to see (bearer tokens, cookies)
                headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie", "proxy-authorization")}
            at = nxt
            # A hop within the host is the same page under its other address and is asked without waiting for a turn,
            # but not for free: when the host states a Crawl-delay, that delay is waited before every request, hops
            # included; otherwise the hop is charged to the host's spacing (its next turn moves one interval on), so
            # the rate over a string of redirects is what it is over pages.
            wait = not same_host or bool(self.robots.crawl_delay(at))
            if not wait:
                self.limiter.charge(at)
            res = self._get(at, headers, use_cache, kind, spaced=wait)
        if at != full:
            res = dataclasses.replace(res, url=full, final_url=at)
        return res

    def _upgraded(self, url: str) -> str:
        """http://host/... as https://host/... once the host has answered an http address with a redirect to the same
        address under https: one request a page instead of two."""
        if url.startswith("http://") and urlsplit(url).netloc.lower() in self._https:
            https = "https://" + url[len("http://"):]
            if not self.blocklist.is_blocked(https) and (self._is_api_endpoint(https) or self.robots.allowed(https)):
                return https
        return url

    stop_at: float | None = None   # monotonic time; set by the scheduler for the run's budget
    _local = threading.local()     # a per-thread earlier deadline: one source's share of the run (scheduler.run_sources)

    @property
    def thread_stop_at(self) -> float | None:
        return getattr(self._local, "stop_at", None)

    @thread_stop_at.setter
    def thread_stop_at(self, value: float | None) -> None:
        self._local.stop_at = value

    def _host_refused(self, url: str, reason: str, status: int = 0, is_doc: bool = False, api: bool = False) -> float | None:
        """Is this refusal the whole host's doing? -> the days the host is left alone for (0: the block list's own
        time, 7 days), None when it is not.

          - A challenge page (Cloudflare, Imperva...), wherever it is met: the block list's time.
          - A refusal of the host's front door: the block list's time, or COUNTED_BLOCK_DAYS (1) when the host has
            served a page this run (it may be a passing ban).
          - The first 401/403 on a page of a host whose robots.txt answered 401/403 too, while the host has served
            nothing this run (that file's refusal alone leaves the site open, RFC 9309; with a page refused as well
            and nothing served, the host is refusing the crawler: a site behind a login costs two requests a run): 1 day.
          - 401/403 answers one after another with no 200 between them, for every caller (pages, files, data
            services): 1 day after REFUSALS_IN_A_ROW (5) from a host that has served nothing this run,
            SERVED_REFUSALS_IN_A_ROW (12) from one that has (or a data service), SHARED_REFUSALS_IN_A_ROW (20) from a
            file store or archive many sources' links point into (SHARED_HOSTS).
        A 401/403 among pages that answer is that page's own (a staff-only folder, a members' document, a file gone
        from a file store): any 200 from the host starts the count again. Counted without that, one staff-only link
        blocked a healthy site for a week; not counted at all once a host had served a page, a host that served three
        pages and then refused everything (a rate ban) was asked for all 300 files of a list (Nov. 20, 2026)."""
        if "challenge" in reason:
            return self.BLOCK_DAYS
        host = host_of(url)
        netloc = urlsplit(url).netloc.lower()
        served = host in self._served
        shared = any(netloc == h or netloc.endswith("." + h) for h in self.SHARED_HOSTS)
        path = urlsplit(url).path or "/"
        if path in ("/", "/robots.txt"):
            return self.COUNTED_BLOCK_DAYS if served else self.BLOCK_DAYS
        if status in (401, 403) and not (served or shared or is_doc or api) and self.robots.unavailable.get(host) in (401, 403):
            return self.COUNTED_BLOCK_DAYS
        with self._refusal_lock:
            n = self._refusals[host] = self._refusals.get(host, 0) + 1
        bar = (self.SHARED_REFUSALS_IN_A_ROW if shared else self.SERVED_REFUSALS_IN_A_ROW if (served or api)
               else self.REFUSALS_IN_A_ROW)
        return self.COUNTED_BLOCK_DAYS if n >= bar else None

    # It was three refusals in a whole run, however many pages answered between them: three forbidden links on a site
    # of hundreds of pages (a staff-only folder, a calendar's JSON address, a mistyped mail link) put
    # www.clarkstonga.gov, www.waverlyhallga.gov, www.woodstockga.gov and www.habershamschools.com on the block list
    # for a week each (state/blocked.json, Nov. 2026).
    REFUSALS_IN_A_ROW = 5
    SERVED_REFUSALS_IN_A_ROW = 12
    SHARED_REFUSALS_IN_A_ROW = 20
    BLOCK_DAYS = 0.0                # 0: the block list's own time (blocked_ttl_days)
    COUNTED_BLOCK_DAYS = 1.0        # a block worked out by counting plain refusals is tried again the next day
    # archives and file stores that many sites' links point into: their refusals are mostly of single files
    SHARED_HOSTS = ("archive.org", "amazonaws.com", "cloudfront.net", "wixstatic.com", "googleusercontent.com",
                    "googleapis.com", "windows.net", "azureedge.net", "akamaihd.net", "dropboxusercontent.com")

    def _answered(self, url: str) -> None:
        """This host answered 200 with a real page (not a challenge shell; a 304 does not come here): it serves the
        crawler, and its run of refusals, if any, is over."""
        host = host_of(url)
        if host not in self._served or self._refusals.get(host):
            with self._refusal_lock:
                self._served.add(host)
                self._refusals[host] = 0

    def deadline(self) -> float | None:
        """The earlier of the run's and this thread's deadlines."""
        ts = [t for t in (self.stop_at, getattr(self._local, "stop_at", None)) if t is not None]
        return min(ts) if ts else None

    max_wait: float | None = None   # research: a turn more than this many seconds away is not waited for (the step is skipped)

    def _turn(self, url: str, delay: float | None) -> None:
        """Wait for the host's next turn, unless it would come after the deadline (or, with max_wait, too far
        off: a strained shared host backs off up to 16x, and six workers queued behind it would sit idle)."""
        until = self.deadline()
        # a thread can set its own (research's news lane: None, waiting for its one host is all it does)
        max_wait = getattr(getattr(self, "_local", None), "max_wait", self.max_wait)
        if max_wait is not None:
            cap = time.monotonic() + max_wait
            until = cap if until is None else min(until, cap)
        try:
            self.limiter.wait(url, delay, until=until)
        except SlotPastDeadline as exc:
            own = getattr(getattr(self, "_local", None), "stop_at", None)
            which = ("this source's time slice" if own is not None and (self.stop_at is None or own < self.stop_at)
                     else "the run's time budget")
            raise OutOfTime(f"{exc} ({which})") from None

    def _check_time(self) -> None:
        now = time.monotonic()
        if self.stop_at is not None and now > self.stop_at:
            raise OutOfTime("run time budget used up")
        t = getattr(self._local, "stop_at", None)
        if t is not None and now > t:
            raise OutOfTime("this source's time slice used up")

    def _get(self, full: str, headers: dict | None, use_cache: bool, kind: str | None = None, spaced: bool = True) -> FetchResult:
        self._check_time()
        meta_p, body_p = self._cache_paths(full)
        req_headers = dict(headers or {})
        cached = None
        if use_cache and meta_p.exists() and body_p.exists():
            cached = json.loads(meta_p.read_text())
            if cached.get("etag"):
                req_headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                req_headers["If-Modified-Since"] = cached["last_modified"]

        delay = self.robots.crawl_delay(full)
        last_exc = None
        dead = self._dead.get(host_of(full))
        if dead:
            raise requests.ConnectionError(f"{host_of(full)} could not be reached earlier this run ({dead})")
        for attempt in range(self.max_retries + 1):
            if spaced or attempt:
                self._turn(full, delay)     # (a redirect's hop within one host is asked at once: see _follow)
            t0 = time.monotonic()
            try:
                # a slow service (host_timeouts) gets its time for the connection too: the TLS handshake is bounded by
                # the connect timeout, and urllib3 reports a slow handshake as "Read timed out (read timeout=10)"
                slow = self.host_timeouts.get(urlsplit(full).netloc.lower())
                tmo = (slow, slow) if slow else (self.connect_timeout, self.timeout)
                r = self.session.get(full, headers=req_headers, timeout=tmo, stream=True, allow_redirects=False)
                ctype = (r.headers.get("Content-Type") or "").lower()
                # documents and data files (agenda packets, minutes books, statistics CSVs) get the larger ceiling
                is_doc = ("pdf" in ctype or "msword" in ctype or "officedocument" in ctype or "octet-stream" in ctype
                          or "text/csv" in ctype or "zip" in ctype or "ms-excel" in ctype
                          or bool(re.search(r"\.(pdf|docx?|xlsx?|xls|csv|zip)(?:$|[?#])", full, re.I)))
                limit = self.max_doc_bytes if is_doc else self.max_bytes
                chunks, size = [], 0
                for chunk in r.iter_content(256 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        r.close()
                        raise TooLarge(f"response over {limit} bytes: {full}")
                body = b"".join(chunks)
            except NET_ERRORS as exc:
                last_exc = exc
                self.limiter.record(full, 599, time.monotonic() - t0)
                if _unreachable(exc):
                    # no such host, or nothing listening: asking again (or asking for the next page) cannot help
                    self._dead[host_of(full)] = f"{type(exc).__name__}"[:60]
                    break
                if attempt >= 1:
                    break                          # a timeout or a reset gets one more try, not four
                self._sleep(3)
                continue
            elapsed = time.monotonic() - t0
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            # the server's answer time (headers), not the download: a big PDF is not a strained server
            answered = r.elapsed.total_seconds() if getattr(r, "elapsed", None) is not None else elapsed
            self.limiter.record(full, r.status_code, answered, retry_after, kind=self.latency_kind(full, headers, is_doc, kind))
            hdrs = {k.lower(): v for k, v in r.headers.items()}

            reason = looks_blocked(r.status_code, body)
            api = self.latency_kind(full, headers, is_doc, kind) not in ("page", "doc")
            if not reason and r.status_code in (401, 403):
                # any 401 or 403 is a refusal of that page, whatever its body says ("error code: 1020", a long branded
                # page, a login prompt): counted, so a host that answers so to everything is left alone after a few
                reason = f"HTTP {r.status_code} refused"
            days = self._host_refused(full, reason, r.status_code, is_doc, api) if reason else None
            if days is not None:
                self.blocklist.add(full, reason, self.source_id, ttl_days=days or None)
                raise Blocked(host_of(full), reason)
            if r.status_code in (429, 503) and attempt < self.host_retries.get(urlsplit(full).netloc.lower(), self.max_retries):
                self._sleep(min(600, retry_after or 2 ** attempt * 10))
                continue
            if r.status_code == 200 and not reason:
                self._answered(full)
            if r.status_code == 304 and cached:
                # the cached answer's own Last-Modified goes with it (channels.http_modified reads it)
                kept = {**cached.get("headers", {}), **({"last-modified": cached["last_modified"]} if cached.get("last_modified") else {})}
                return FetchResult(full, 200, kept, body_p.read_bytes(), elapsed, not_modified=True)
            if r.status_code == 200 and use_cache and (hdrs.get("etag") or hdrs.get("last-modified")):
                meta_p.write_text(json.dumps({
                    "etag": hdrs.get("etag"),
                    "last_modified": hdrs.get("last-modified"),
                    "headers": {"content-type": hdrs.get("content-type", "")},
                }))
                body_p.write_bytes(body)
            return FetchResult(full, r.status_code, hdrs, body, elapsed)
        raise last_exc or RuntimeError(f"gave up on {full}")

    def post(self, url: str, data: dict | None = None, headers: dict | None = None, json_body=None,
             kind: str | None = None) -> FetchResult:
        """A POST (form `data`, or JSON `json_body`) with the same politeness as get() (robots, rate limit, block
        list, back-off); never cached. Used only for read-only endpoints that a site's own pages load this way
        (BoardDocs meeting lists, the General Assembly's legislation search)."""
        full = requests.Request("POST", url).prepare().url
        blocked = self.blocklist.is_blocked(full)
        if blocked:
            raise Blocked(host_of(full), blocked["reason"])
        if not self._is_api_endpoint(full) and not self.robots.allowed(full):
            raise Disallowed(full, self.robots.why(full))
        self._check_time()
        delay = self.robots.crawl_delay(full)
        last_exc = None
        for attempt in range(self.max_retries + 1):
            self._turn(full, delay)
            t0 = time.monotonic()
            try:
                if json_body is not None:
                    r = self.session.post(full, json=json_body, headers=dict(headers or {}), timeout=self.timeout)
                else:
                    r = self.session.post(full, data=data or {}, headers=dict(headers or {}), timeout=self.timeout)
                body = r.content
                if len(body) > self.max_bytes:
                    raise TooLarge(f"response over {self.max_bytes} bytes: {full}")
            except NET_ERRORS as exc:
                last_exc = exc
                self.limiter.record(full, 599, time.monotonic() - t0)
                self._sleep(min(60, 2 ** attempt * 5))
                continue
            elapsed = time.monotonic() - t0
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            answered = r.elapsed.total_seconds() if getattr(r, "elapsed", None) is not None else elapsed
            # a JSON body is an API call: its own endpoint's baseline (latency_kind)
            self.limiter.record(full, r.status_code, answered, retry_after,
                                kind=self.latency_kind(full, {"Accept": "application/json"} if json_body is not None else headers,
                                                       False, kind))
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            reason = looks_blocked(r.status_code, body)
            if reason:
                self.blocklist.add(full, reason, self.source_id)
                raise Blocked(host_of(full), reason)
            if r.status_code in (429, 503) and attempt < self.host_retries.get(urlsplit(full).netloc.lower(), self.max_retries):
                self._sleep(min(600, retry_after or 2 ** attempt * 10))
                continue
            return FetchResult(full, r.status_code, hdrs, body, elapsed)
        raise last_exc or RuntimeError(f"gave up on {full}")

    def host(self, url: str) -> str:
        return host_of(url)


def _unreachable(exc: Exception) -> bool:
    """A failure that will not change by asking again: the name does not resolve, or the port refuses."""
    text = f"{type(exc).__name__}: {exc}"
    return bool(re.search(r"NameResolutionError|Name or service not known|nodename nor servname|getaddrinfo failed|"
                          r"Connection refused|ECONNREFUSED|No route to host|Network is unreachable|certificate verify failed|"
                          r"SSLError", text))


def launch_browser(playwright):
    """Chromium for script-rendered pages. GACRAWL_BROWSER_CHANNEL=chrome uses the machine's installed Chrome (GitHub's
    runners ship one), which saves downloading Playwright's browser on every run; falls back to the bundled build."""
    import os

    channel = os.environ.get("GACRAWL_BROWSER_CHANNEL", "").strip()
    if channel:
        try:
            return playwright.chromium.launch(channel=channel)
        except Exception:  # noqa: BLE001 — that channel isn't installed here
            pass
    return playwright.chromium.launch()
