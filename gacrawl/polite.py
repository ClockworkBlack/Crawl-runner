"""Politeness layer: robots.txt, per-host adaptive rate limiting, off-peak windows.

Everything the crawler fetches goes through these checks. A site that says no
(robots.txt Disallow, a bot-protection challenge, 429/503 with Retry-After) gets
respected, not worked around.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def host_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


# --------------------------------------------------------------------------- robots
def product_token(user_agent: str) -> str:
    """The name a robots.txt group addresses us by: "GACivicCrawler/0.3 (+https://...)" -> "gaciviccrawler"."""
    m = re.match(r"[A-Za-z_-]+", (user_agent or "").strip())
    return m.group(0).lower() if m else ""


ROBOTS_MAX_BYTES = 500 * 1024      # RFC 9309 2.5: at least 500 KiB must be parsed; the rest of a larger file is not read
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_KEPT_ENCODED = frozenset("/?#*$%")     # an encoded one of these is not the character itself ("%2F" is not a path separator)
_HEX = frozenset("0123456789abcdefABCDEF")


def _canon(path: str) -> str:
    """One written form for a path, in a rule or in an address (RFC 9309 2.2.2): UTF-8, every octet outside the
    unreserved set percent-encoded with upper-case hex, so "/my docs/", "/my%20docs/", "/café" and "/caf%C3%A9",
    "(" and "%28" are the same text. "/", "?", "*" and "$" keep their meaning when written raw, and stay encoded when
    they were written encoded."""
    b = path.encode("utf-8", "surrogatepass")
    out, i, n = [], 0, len(b)
    while i < n:
        c = b[i]
        if c == 0x25 and i + 2 < n and chr(b[i + 1]) in _HEX and chr(b[i + 2]) in _HEX:
            c = int(b[i + 1:i + 3], 16)
            i += 3
            if chr(c) in _KEPT_ENCODED:
                out.append("%%%02X" % c)
                continue
            out.append(chr(c) if chr(c) in _UNRESERVED else "%%%02X" % c)
            continue
        i += 1
        ch = chr(c)
        out.append(ch if ch in _UNRESERVED or ch in "/?*$" else "%%%02X" % c)
    return "".join(out)


def _plain(path: str) -> str:
    """The path with every percent-encoding undone (octets as characters): the second form a Disallow is tried in, so
    a rule "/a%2Fb" also covers "/a/b" and nothing written one way slips past a rule written the other."""
    from urllib.parse import unquote_to_bytes

    return unquote_to_bytes(path.encode("utf-8", "surrogatepass")).decode("latin-1")


class _Pattern:
    """A rule's path: `*` is any run of characters, a final `$` the end of the address (RFC 9309 2.2.3). Matched
    piece by piece (the text between the stars, each found after the last), never as a regular expression:
    "/*a*a*a…b" as one took minutes on a 60-character address."""

    __slots__ = ("parts", "end", "length")

    def __init__(self, text: str, form=None):
        """`form`: applied to each piece between the stars after the rule is cut at them, so only a `*` and a final
        `$` written as such in the rule are a wildcard and an anchor ("%2A" undone is a star in the address, not one)."""
        text = re.sub(r"\*+", "*", text)
        self.end = text.endswith("$")
        if self.end:
            text = text[:-1]
        self.parts = [form(p) if form else p for p in text.split("*")]
        self.length = sum(len(p) for p in self.parts) + len(self.parts) - 1 + (1 if self.end else 0)

    def match(self, path: str) -> bool:
        parts = self.parts
        if not path.startswith(parts[0]):
            return False
        if len(parts) == 1:
            return not self.end or path == parts[0]
        pos = len(parts[0])
        for mid in parts[1:-1]:
            at = path.find(mid, pos)
            if at < 0:
                return False
            pos = at + len(mid)
        last = parts[-1]
        if self.end:
            return len(path) - len(last) >= pos and path.endswith(last)
        return path.find(last, pos) >= 0


def decode_robots(content: bytes) -> str:
    """A robots.txt as text. The file is UTF-8 (RFC 9309 2.3); it is decoded here from its bytes, whatever charset
    the server names or omits: without one, `requests` reads text/plain as ISO-8859-1, a UTF-8 byte-order mark
    became three letters in front of the first "User-agent", and that group's rules applied to nobody. UTF-16
    files (a mark at the start) are read as such. Only the first ROBOTS_MAX_BYTES are read."""
    content = bytes(content or b"")[:ROBOTS_MAX_BYTES]
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return content.decode("utf-16", "replace")
    if content[:3] == b"\xef\xbb\xbf":
        content = content[3:]
    return content.decode("utf-8", "replace")


class Robots:
    """One robots.txt, read the way RFC 9309 says. Python's urllib.robotparser, used until Nov. 20, 2026, differs from
    it in ways that matter both ways:
      - it applied the first rule that matched, not the longest: "Disallow: /" followed by "Allow: /agendas/" closed
        the agendas too; and "Allow: /" followed by "Disallow: /private/" opened the private pages;
      - it knew no wildcards: "Disallow: /*.pdf$" or "Disallow: /*?print=" stopped nothing;
      - a blank line ended a group, so the rules after "User-agent: *" and an empty line applied to nobody.
    Here: a group is ours when its name, cut to its product token ("GACivicCrawler/0.3" and "GACivicCrawler*" are
    "gaciviccrawler"), is our token; several such groups are one. Without one, a group whose name is a part of ours
    ("crawler", as the old parser took it: obeyed, to be on the safe side); without that, the `*` groups. Among the
    rules that match an address the longest pattern decides, Allow on a tie; no match means allowed. Rules and
    addresses are compared in one written form (_canon); a Disallow also applies when it matches with every
    percent-encoding undone (_plain). /robots.txt itself is always allowed."""

    def __init__(self, text: str, user_agent: str):
        self.token = product_token(user_agent)
        self.sitemaps: list[str] = []
        groups: dict[str, dict] = {}            # agent name -> {"rules": [(allow, path)], "delay": float | None}
        agents: list[str] = []
        in_rules = False
        text = (text or "")[:ROBOTS_MAX_BYTES].replace("\x00", "")
        for mark in ("﻿", "ï»¿", "ÿþ", "þÿ"):     # a byte-order mark, also one mis-decoded
            if text.startswith(mark):
                text = text[len(mark):]
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue                        # blank lines and anything that is not "name: value" are skipped
            key, value = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "sitemap":
                if value:
                    self.sitemaps.append(value)
            elif key == "user-agent":
                if in_rules:
                    agents, in_rules = [], False        # a user-agent line after rules opens a new group
                name = "*" if value.strip() == "*" else (product_token(value) or value.lower())
                agents.append(name)
                groups.setdefault(name, {"rules": [], "delay": None})
            elif key in ("allow", "disallow"):
                in_rules = True
                if value:                               # "Disallow:" with nothing after it disallows nothing
                    for a in agents:
                        groups[a]["rules"].append((key == "allow", value))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    d = float(value)
                except ValueError:
                    continue
                if d != d or d in (float("inf"), float("-inf")) or d < 0:
                    continue                            # not a number of seconds
                for a in agents:                        # several in one group: the largest (the most careful reading)
                    if groups[a]["delay"] is None or d > groups[a]["delay"]:
                        groups[a]["delay"] = d
        mine = [groups[self.token]] if self.token and self.token in groups else []
        if not mine and self.token:
            mine = [g for name, g in groups.items() if name != "*" and len(name) >= 4 and name in self.token]
        if not mine:
            mine = [groups["*"]] if "*" in groups else []
        self.rules = []
        for g in mine:
            for allow, path in g["rules"]:
                self.rules.append((allow, _Pattern(path, _canon), None if allow else _Pattern(path, _plain)))
        delays = [g["delay"] for g in mine if g["delay"] is not None]
        self.delay = max(delays) if delays else None

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        raw = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        path, plain = _canon(raw), _plain(raw)
        if path == "/robots.txt":
            return True
        best_len, best_allow = -1, True
        for allow, pat, loose in self.rules:
            if pat.match(path) or (loose is not None and loose.match(plain)):
                if pat.length > best_len or (pat.length == best_len and allow):
                    best_len, best_allow = pat.length, allow
        return best_allow


class RobotsCache:
    """Caches one parsed robots.txt per host.

    Rules follow RFC 9309 (2.3.1): an answer in the 400s means there is no robots.txt to obey (everything allowed;
    until Nov. 20, 2026 a 401 or 403 was read as "disallow everything", and 57 hosts went on the block list for the
    plain "403 Forbidden" their server gives for that one file); a 429, a 5xx or a network error means it could not
    be read, and everything is disallowed, but only for `error_ttl` seconds (a dropped connection on one robots.txt
    fetch used to black out the host for the whole run: the General Assembly's API went "disallowed" for a night
    that way), then asked for again. A bot-protection challenge in place of robots.txt is a refusal of the crawler
    and never reaches here (Fetcher._raw_get_text raises Blocked).
    """

    def __init__(self, get, user_agent: str, ttl: int = 86400, error_ttl: int = 120, warn=print):
        self._get = get  # callable(url) -> (status:int, text:str)
        self.user_agent = user_agent
        self.ttl = ttl
        self.error_ttl = error_ttl
        self.warn = warn
        self._cache: dict[str, tuple[float, Robots | None, bool]] = {}
        self._ttl: dict[str, float] = {}
        self.failures: dict[str, int] = {}     # host -> last status when robots.txt could not be read
        self.reasons: dict[str, str] = {}      # host -> why everything is disallowed (see why())
        self.unavailable: dict[str, int] = {}  # host -> the 4xx its robots.txt answered (no rules: everything allowed)
        self._lock = threading.Lock()

    def _load(self, host: str):
        with self._lock:
            hit = self._cache.get(host)
            if hit and time.time() - hit[0] < self._ttl.get(host, self.ttl):
                return hit
        from .blocked import Blocked

        parser: Robots | None = None
        deny_all = False
        failure = ""
        status, text = 599, ""
        for attempt in (1, 2):
            try:
                status, text = self._get(host + "/robots.txt")
                failure = ""
                break
            except Blocked:
                raise
            except Exception as exc:  # noqa: BLE001
                status, text = 599, ""
                failure = f"{type(exc).__name__}: {exc}"
                # one more try for a connection that dropped or timed out: a single lost packet on this one file
                # ended the source's turn (and, for a source never read, its next six hours to a week). Not for a
                # host that does not exist or refuses the connection.
                if attempt == 2 or re.search(r"NameResolution|Name or service not known|getaddrinfo|Connection refused|"
                                             r"No route to host|Network is unreachable|SSLError|certificate", failure):
                    break
        ttl = self.ttl
        if status == 429 or status >= 500:
            deny_all = True
            if status == 599:
                # the host could not be reached at all (no such name, nothing listening, a dropped connection):
                # said so, rather than "disallowed", so a dead directory listing is told apart from a refusal
                self.reasons[host] = f"unreachable ({failure[:120]})" if failure else "unreachable"
            else:
                self.reasons[host] = f"unreachable (robots.txt HTTP {status}; treated as disallowed for {self.error_ttl} s, then asked again)"
            ttl = self.error_ttl         # a transient failure: ask again soon
            self.failures[host] = status
            self.warn(f"robots.txt of {host}: HTTP {status}; treated as disallowed for {ttl} s, then asked again")
        elif 400 <= status < 500:
            parser = Robots("", self.user_agent)  # no robots.txt (RFC 9309: "unavailable") -> allow all
            self.reasons.pop(host, None)
            if status != 404:
                self.unavailable[host] = status
        else:
            # an HTML page in place of the file (a "not found" page sent with 200, a login page) has no rules in it
            parser = Robots(text, self.user_agent)
            self.reasons.pop(host, None)
        entry = (time.time(), parser, deny_all)
        with self._lock:
            self._cache[host] = entry
            self._ttl[host] = ttl
        return entry

    def allowed(self, url: str) -> bool:
        _, parser, deny_all = self._load(host_of(url))
        if deny_all or parser is None:
            return False
        return parser.allowed(url)

    def why(self, url: str) -> str:
        """Why this URL is off-limits, for people reading the logs and the seed's records: the host could not be
        reached, its robots.txt failed (429, 5xx), or its rules disallow this path."""
        host = host_of(url)
        _, parser, deny_all = self._load(host)
        if deny_all or parser is None:
            return self.reasons.get(host, "robots.txt could not be read")
        return "robots.txt disallows this path for our user agent"

    def crawl_delay(self, url: str) -> float | None:
        _, parser, _ = self._load(host_of(url))
        if parser is None or parser.delay is None:
            return None
        return max(0.0, float(parser.delay))        # obeyed as written, whatever its size

    def sitemaps(self, url: str) -> list[str]:
        _, parser, _ = self._load(host_of(url))
        if parser is None:
            return []
        return list(parser.sitemaps)


# --------------------------------------------------------------------------- rate limiting
@dataclass
class _HostState:
    next_ok: float = 0.0
    multiplier: float = 1.0
    baseline_latency: float | None = None          # pages and API answers
    baselines: dict = field(default_factory=dict)  # per kind of response ("doc": PDFs and office files)
    lock: threading.Lock = field(default_factory=threading.Lock)


class SlotPastDeadline(Exception):
    """The host's next free turn comes after the caller's deadline."""


class RateLimiter:
    """Per-host spacing that slows down when a site looks strained.

    - base interval = max(configured min_interval, robots Crawl-delay)
    - 429/503 or latency > 3x baseline -> multiplier doubles (cap 16x)
    - each healthy response decays the multiplier back toward 1x
    """

    def __init__(self, min_interval: float = 2.0, max_multiplier: float = 16.0, sleep=time.sleep, clock=time.monotonic,
                 host_intervals: dict | None = None, after_answer: list | None = None):
        self.min_interval = min_interval
        # hosts whose spacing counts from the end of the previous answer, not its start (GDELT: "one request every
        # 5 seconds", and a search can take a minute to answer)
        self.after_answer = {h.lower() for h in (after_answer or [])}
        # per-host floors: APIs built for machine traffic can go faster; some services ask for slower
        self.host_intervals = {k.lower(): float(v) for k, v in (host_intervals or {}).items()}
        self.max_multiplier = max_multiplier
        self._hosts: dict[str, _HostState] = {}
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock

    def _state(self, host: str) -> _HostState:
        with self._lock:
            return self._hosts.setdefault(host, _HostState())

    def base_interval(self, host: str) -> float:
        netloc = host.split("://", 1)[-1].lower()
        for h, v in self.host_intervals.items():
            if netloc == h or netloc.endswith("." + h):
                return v
        return self.min_interval

    daytime_factor: float = 1.0     # > 1 while a big site is read by day (scheduler.daytime): gentler than at night

    def interval(self, host: str, crawl_delay: float | None = None) -> float:
        st = self._state(host)
        base = max(self.base_interval(host), crawl_delay or 0.0)
        if self.daytime_factor != 1.0 and host not in self.host_intervals:
            base *= self.daytime_factor
        return base * st.multiplier

    def wait(self, url: str, crawl_delay: float | None = None, until: float | None = None) -> float:
        """Block until this host may be hit again. Safe across threads: each
        host hands out slots in order, so workers on the same host serialize.
        With `until` (a monotonic deadline), a turn that would only come after it is not taken: SlotPastDeadline
        is raised at once instead of sleeping through the deadline."""
        host = host_of(url)
        st = self._state(host)
        with st.lock:
            now = self._clock()
            slot = max(now, st.next_ok)
            if until is not None and slot > until:
                raise SlotPastDeadline(f"{host}: next turn at +{slot - now:.0f}s is past the deadline")
            st.next_ok = slot + self.interval(host, crawl_delay)
        delay = max(0.0, slot - self._clock())
        if delay:
            self._sleep(delay)
        return delay

    def charge(self, url: str, crawl_delay: float | None = None) -> None:
        """Count a request that was made without waiting for a turn (a redirect's hop within the host): the host's
        next turn moves one interval on, so the average rate holds."""
        host = host_of(url)
        st = self._state(host)
        with st.lock:
            st.next_ok = max(self._clock(), st.next_ok) + self.interval(host, crawl_delay)

    def record(self, url: str, status: int, latency: float, retry_after: float | None = None, kind: str = "page") -> None:
        """Note a response. `latency` should be the time to the server's answer (headers), not the download: a
        multi-megabyte PDF downloads slower than a JSON answer without the server being under strain. Each kind of
        response ("page", "doc") keeps its own baseline, so documents are compared with documents; a response more
        than 3x its kind's baseline, a 429 or a 503 doubles the host's spacing (up to max_multiplier), anything else
        relaxes it."""
        st = self._state(host_of(url))
        refused = status in (429, 503)
        strained = refused
        base = st.baseline_latency if kind == "page" else st.baselines.get(kind)
        if base is None and not strained and status < 400:
            base = latency
        elif base and latency > 3 * base:
            strained = True
        if strained:
            st.multiplier = min(self.max_multiplier, st.multiplier * 2)
            if not refused and base and status < 400:
                # slow, not refused: the baseline still learns (slowly). A first answer that happened to be quick (a
                # small lookup) otherwise made every normal answer after it look strained, and the host's spacing
                # climbed to the 16x cap and stayed there (Nov 9: EFile and GovInfo at 48 s a request)
                base = 0.9 * base + 0.1 * latency
        else:
            st.multiplier = max(1.0, st.multiplier * 0.8)
            if base and status < 400:
                base = 0.9 * base + 0.1 * latency
        if kind == "page":
            st.baseline_latency = base
        elif base is not None:
            st.baselines[kind] = base
        if urlsplit(url).netloc.lower() in self.after_answer:
            st.next_ok = max(st.next_ok, self._clock() + self.interval(host_of(url)))
        if retry_after:
            st.next_ok = max(st.next_ok, self._clock() + retry_after)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        return max(0.0, (when - dt.datetime.now(when.tzinfo)).total_seconds())
    except Exception:
        return None


# --------------------------------------------------------------------------- off-peak windows
@dataclass
class Window:
    days: list[str]
    start: dt.time
    end: dt.time  # may be earlier than start -> wraps past midnight

    def contains(self, local: dt.datetime) -> bool:
        t = local.time()
        today = DAYS[local.weekday()]
        yesterday = DAYS[(local.weekday() - 1) % 7]
        if self.start <= self.end:
            return today in self.days and self.start <= t < self.end
        # wrapping window: evening part belongs to the start day,
        # early-morning part belongs to the previous day's window
        return (today in self.days and t >= self.start) or (yesterday in self.days and t < self.end)


@dataclass
class Schedule:
    """'Good times' to crawl: when government sites see the least human traffic."""

    tz: ZoneInfo
    windows: list[Window] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: dict) -> "Schedule":
        tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
        wins = []
        for w in cfg.get("windows", []):
            days = [d.lower()[:3] for d in w.get("days", DAYS)]
            wins.append(Window(days, dt.time.fromisoformat(w["start"]), dt.time.fromisoformat(w["end"])))
        return cls(tz, wins)

    def is_good_time(self, now: dt.datetime | None = None) -> bool:
        if not self.windows:
            return True
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(self.tz)
        return any(w.contains(local) for w in self.windows)

    def next_good_time(self, now: dt.datetime | None = None) -> dt.datetime:
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(self.tz)
        probe = local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
        for _ in range(7 * 24 * 4):  # 15-minute steps, one week
            if self.is_good_time(probe):
                return probe
            probe += dt.timedelta(minutes=15)
        return probe
