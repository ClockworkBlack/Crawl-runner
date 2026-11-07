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
from .polite import RateLimiter, RobotsCache, SlotPastDeadline, host_of, parse_retry_after


class OutOfTime(Exception):
    """The run's time budget is spent: raised at the next request so a long source stops promptly."""


class TooLarge(ValueError):
    """A response over the size ceiling (25 MB for pages, more for documents): skipped, never half-read."""


class Disallowed(Exception):
    """robots.txt (or the site's response to it) says this URL is off-limits. `reason` says why: the host could
    not be reached, robots.txt answered 401/403 or 5xx, or its rules disallow the path (see RobotsCache.why)."""

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
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        # per-host read timeouts (seconds) for services that are slow to answer but do answer (GDELT's search)
        self.host_timeouts = {k.lower(): float(v) for k, v in (host_timeouts or {}).items()}
        self.max_bytes = max_bytes
        self.max_doc_bytes = max(max_bytes, max_doc_bytes)   # agenda packets and minutes books run large
        self.max_retries = max_retries
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
        if reason:
            self.blocklist.add(url, f"robots.txt: {reason}", self.source_id)
            raise Blocked(host_of(url), reason)
        return r.status_code, r.text

    def allowed(self, url: str) -> bool:
        return self.robots.allowed(url)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self._cache_dir / f"{key}.meta.json", self._cache_dir / f"{key}.body"

    MEMO_TTL = 900            # seconds: the same page asked for twice in one run (probe, then crawl; seed, then crawl)
    MEMO_MAX_BYTES = 2_000_000

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, use_cache: bool = True) -> FetchResult:
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
        res = self._get(full, headers, use_cache)
        if use_cache and res.status == 200 and len(res.content) <= self.MEMO_MAX_BYTES:
            with self._memo_lock:
                if len(self._memo) >= 2000:
                    for k in list(self._memo)[:500]:
                        self._memo.pop(k, None)
                self._memo[memo_key] = (time.monotonic(), res)
        return res

    stop_at: float | None = None   # monotonic time; set by the scheduler for the run's budget
    _local = threading.local()     # a per-thread earlier deadline: one source's share of the run (scheduler.run_sources)

    @property
    def thread_stop_at(self) -> float | None:
        return getattr(self._local, "stop_at", None)

    @thread_stop_at.setter
    def thread_stop_at(self, value: float | None) -> None:
        self._local.stop_at = value

    def _host_refused(self, url: str, reason: str) -> bool:
        """Is this refusal the whole host's doing? A challenge page (Cloudflare, Imperva...) or a refusal of the
        site's front door or robots.txt is; a plain 403 on one deep page (a members-only document, a mis-set
        folder) only counts, and the host is left alone after the third in a run."""
        if "challenge" in reason:
            return True
        path = urlsplit(url).path or "/"
        if path in ("/", "/robots.txt"):
            return True
        with self._refusal_lock:
            n = self._refusals[host_of(url)] = self._refusals.get(host_of(url), 0) + 1
        return n >= 3

    def deadline(self) -> float | None:
        """The earlier of the run's and this thread's deadlines."""
        ts = [t for t in (self.stop_at, getattr(self._local, "stop_at", None)) if t is not None]
        return min(ts) if ts else None

    max_wait: float | None = None   # research: a turn more than this many seconds away is not waited for (the step is skipped)

    def _turn(self, url: str, delay: float | None) -> None:
        """Wait for the host's next turn, unless it would come after the deadline (or, with max_wait, too far
        off: a strained shared host backs off up to 16x, and six workers queued behind it would sit idle)."""
        until = self.deadline()
        if self.max_wait is not None:
            cap = time.monotonic() + self.max_wait
            until = cap if until is None else min(until, cap)
        try:
            self.limiter.wait(url, delay, until=until)
        except SlotPastDeadline as exc:
            raise OutOfTime(str(exc)) from None

    def _check_time(self) -> None:
        now = time.monotonic()
        if self.stop_at is not None and now > self.stop_at:
            raise OutOfTime("run time budget used up")
        t = getattr(self._local, "stop_at", None)
        if t is not None and now > t:
            raise OutOfTime("this source's time slice used up")

    def _get(self, full: str, headers: dict | None, use_cache: bool) -> FetchResult:
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
            self._turn(full, delay)
            t0 = time.monotonic()
            try:
                # a slow service (host_timeouts) gets its time for the connection too: the TLS handshake is bounded by
                # the connect timeout, and urllib3 reports a slow handshake as "Read timed out (read timeout=10)"
                slow = self.host_timeouts.get(urlsplit(full).netloc.lower())
                tmo = (slow, slow) if slow else (self.connect_timeout, self.timeout)
                r = self.session.get(full, headers=req_headers, timeout=tmo, stream=True)
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
            except (requests.ConnectionError, requests.Timeout) as exc:
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
            self.limiter.record(full, r.status_code, answered, retry_after, kind="doc" if is_doc else "page")
            hdrs = {k.lower(): v for k, v in r.headers.items()}

            reason = looks_blocked(r.status_code, body)
            if reason and self._host_refused(full, reason):
                self.blocklist.add(full, reason, self.source_id)
                raise Blocked(host_of(full), reason)
            if r.status_code in (429, 503) and attempt < self.max_retries:
                self._sleep(min(600, retry_after or 2 ** attempt * 10))
                continue
            if r.status_code == 304 and cached:
                return FetchResult(full, 200, cached.get("headers", {}), body_p.read_bytes(), elapsed, not_modified=True)
            if r.status_code == 200 and use_cache and (hdrs.get("etag") or hdrs.get("last-modified")):
                meta_p.write_text(json.dumps({
                    "etag": hdrs.get("etag"),
                    "last_modified": hdrs.get("last-modified"),
                    "headers": {"content-type": hdrs.get("content-type", "")},
                }))
                body_p.write_bytes(body)
            return FetchResult(full, r.status_code, hdrs, body, elapsed)
        raise last_exc or RuntimeError(f"gave up on {full}")

    def post(self, url: str, data: dict | None = None, headers: dict | None = None, json_body=None) -> FetchResult:
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
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self.limiter.record(full, 599, time.monotonic() - t0)
                self._sleep(min(60, 2 ** attempt * 5))
                continue
            elapsed = time.monotonic() - t0
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            answered = r.elapsed.total_seconds() if getattr(r, "elapsed", None) is not None else elapsed
            self.limiter.record(full, r.status_code, answered, retry_after)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            reason = looks_blocked(r.status_code, body)
            if reason:
                self.blocklist.add(full, reason, self.source_id)
                raise Blocked(host_of(full), reason)
            if r.status_code in (429, 503) and attempt < self.max_retries:
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
