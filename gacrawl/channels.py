"""Channel auto-detection and crawling.

For each source: find every way to get its data (official API, WordPress JSON,
RSS/Atom, sitemap, plain HTML, headless browser), time each, and use the
fastest working structured channel. The choice is cached in
state/channels.json, re-checked every `recheck_days`, or at once on failure.

Efficiency rules applied while crawling (all need a `CrawlContext` with a
frontier, i.e. the Store; without one everything is fetched, as in tests):
- documents (PDF, Word, CivicPlus DocumentCenter/AgendaCenter files) are
  fetched once; they don't change after publication
- HTML pages deeper than the listing pages are revisited at most every
  `page_revisit_days`; listing pages (depth 0-1) every run
- links that look like agendas, minutes, ordinances or council pages are
  crawled before everything else, so the page budget goes where the records are
- official websites get a coverage crawl (sitemap or HTML) on top of a news
  feed, so a feed never stands in for the whole site
- long structured backfills yield Checkpoints; the scheduler stores them as
  cursors so an interrupted first run resumes instead of starting over
"""
from __future__ import annotations

import datetime as dt
import json
from .registry import write_json_atomic
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from lxml import etree

from . import extract
from .blocked import Blocked
from .feeds import looks_like_feed, parse_feed
from .fetch import Disallowed, Fetcher
from .store import Record, norm_published

STRUCTURED = ("api", "wp_json", "feed")
PRIORITY = {"api": 0, "wp_json": 1, "feed": 2, "sitemap": 3, "html": 4, "browser": 5}
MIN_PAGE_CHARS = 200  # an HTML page with less extracted text than this is treated as empty (likely client-rendered)

DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|rtf|txt|csv)(?:$|[?#])", re.I)
DOC_PATH = re.compile(r"/(DocumentCenter/View|AgendaCenter/ViewFile|Archive\.aspx\?ADID|ArchiveCenter/ViewFile|"
                      r"GetMeetingFileStream|ViewFile\.aspx|MetaViewer\.php|View\.ashx)", re.I)
HOT = re.compile(r"agenda|minute|ordinance|resolution|council|commission|meeting|alderm|mayor|clerk|code-of|"
                 r"municipal-code|zoning|public-hearing|budget|election|result|qualif|candidate|runoff|referendum|splost|"
                 r"ballot|board-of-ed|school-board|appoint|press|news|transcript|video|AgendaCenter|DocumentCenter|Archive|"
                 r"campaign|disclosure|ccdr|contribution|ethics|financial-disclosure", re.I)
# "event" and "search" as words: "prevention", "research" and "eventide" are not calendars or search forms
COLD = re.compile(r"calendar|(?<![a-z])events?(?![a-z])|parks?-and-rec|facility|login|(?<![a-z])search|sitemap|privacy|accessib|employment|jobs|"
                  r"pay-|bill-pay|translate|facebook|twitter|instagram|youtube|mailto:|tel:|\.(jpg|jpeg|png|gif|svg|mp4|zip)$",
                  re.I)



# hosts a government's address can move to that are not its website
NOT_A_SITE = re.compile(r"(^|\.)(facebook|fb|instagram|twitter|x|youtube|youtu|linkedin|google|godaddy|sedo|parkingcrew|"
                        r"bodis|hugedomains|dan|afternic|wixsite|squarespace|archive)\.(com|org|be|me|net)$", re.I)


class ChannelDown(Exception):
    """The channel's own address (the feed, the sitemap, the posts API) did not answer 200 this time. Raised so the run
    is recorded as an error and the source's last good run stands: taken as "nothing new", the next run's cut-off moved
    past everything published in between, for good."""


@dataclass
class Checkpoint:
    """Yielded by a crawl when a backfill window is complete: resume from `value` next time."""
    stream: str
    value: str


# What is left of a page pass that did not fit one share of a run, kept as a cursor (the scheduler stores every
# Checkpoint). From Nov. 14 to 19, 2026 a hundred page sources were "partial" on every run: each share began again at
# the home page with an empty queue, read the same listing pages, and ran out of time before the pages below them
# (Duluth: "0 new / 41 seen", six shares a run). FRONTIER: the queue of an HTML or browser crawl. SITEMAP_LEFT: the
# addresses of a sitemap not yet read. An empty value means "no pass under way": it is written when a pass completes,
# because a cursor deleted on one runner is not deleted in the main database (shard.merge_delta only upserts).
FRONTIER, SITEMAP_LEFT, WP_PAGE = "frontier", "sitemap_left", "wp_page"
PASS_STREAMS = (FRONTIER, SITEMAP_LEFT, WP_PAGE)
PASS_MAX_URLS = 2500          # addresses kept in one cursor (about 200 KB); past it the deepest are dropped
SITEMAP_CHILDREN = 50         # child sitemaps of an index read in one pass
PASS_MAX_AGE_DAYS = 14        # a pass left unfinished this long starts over: its queue describes an older site
NON_200_IN_A_ROW = 5          # see _Streak
PASS_EXTRA_REQUESTS = 100     # requests a pass may make beyond max_pages (listing re-reads, pages that do not answer)
CHECKPOINT_PAGES = 10         # the cursor is written every so many pages read, and at least
CHECKPOINT_SECONDS = 45       # this often (a run killed outright loses no more than this)


def _pass_value(url: str, started: str, n: int, items: list, **extra) -> str:
    """The cursor's value: the pass's own address, when it began, the pages it has counted, and what is left. The same
    state gives the same text, so a share that got nowhere leaves the cursor as it was (scheduler.stall_after)."""
    cut = len(items) > PASS_MAX_URLS
    d = {"v": 1, "url": url, "start": started, "n": n, "q": items[:PASS_MAX_URLS], **({"cut": 1} if cut else {}), **extra}
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _pass_load(ctx: "CrawlContext | None", stream: str, url: str) -> dict | None:
    """The pass left unfinished by an earlier share, or None: nothing kept, a pass of another address (the source's
    website changed), one too old to carry on (or dated ahead of now), or a value that does not read. A value whose
    parts are not what this code writes (an entry that is not [address, depth, hot], a count that is not a number)
    is not carried on from: the pass starts over, where it used to end the source with an error on every share."""
    raw = ((ctx.cursors if ctx else None) or {}).get(stream)
    if not raw or not getattr(ctx, "resume", False):
        return None
    try:
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get("url") != url or not isinstance(d.get("q"), list) or not d["q"]:
            return None
        started = dt.datetime.fromisoformat(d["start"])
        if started.tzinfo is None:
            return None
        age = dt.datetime.now(dt.timezone.utc) - started
        if age > dt.timedelta(days=PASS_MAX_AGE_DAYS) or age < dt.timedelta(minutes=-5):
            return None
        d["n"] = int(d.get("n") or 0)
        d["a"] = int(d.get("a") or d["n"])
        if d["n"] < 0 or d["a"] < 0:
            return None
        q = []
        for e in d["q"][:PASS_MAX_URLS]:
            if stream == SITEMAP_LEFT:
                if not (isinstance(e, str) and e.startswith(("http://", "https://"))):
                    return None
                q.append(e)
            else:
                if not (isinstance(e, list) and len(e) == 3 and isinstance(e[0], str) and e[0].startswith(("http://", "https://"))
                        and isinstance(e[1], int) and not isinstance(e[1], bool) and 0 <= e[1] <= 50):
                    return None
                q.append([e[0], e[1], 1 if e[2] else 0])
        d["q"] = q
        hosts = d.get("hosts") or []
        d["hosts"] = [h for h in hosts if isinstance(h, str)] if isinstance(hosts, list) else []
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if getattr(ctx, "pass_started", None) is None or d["start"] < ctx.pass_started:
        ctx.pass_started = d["start"]       # the scheduler stamps the source's last good run with the pass's beginning
    return d


# ----------------------------------------------------------------------------- one site, several hosts
# Endings under which anyone can register a name. Besides the plain ones, the sub-domain platforms towns and schools
# rent a site from (town.govoffice.com is the town's; www.govoffice.com is the vendor's and somebody else's town is
# othertown.govoffice.com): treated as endings too, so two names under one platform are never "the same site".
_PLAIN_ENDINGS = ("com", "org", "net", "gov", "us", "info", "biz", "edu", "co", "ga.gov", "ga.us", "k12.ga.us")
_PLATFORM_ENDINGS = (
    "govoffice.com", "govoffice2.com", "govoffice3.com", "wixsite.com", "wordpress.com", "weebly.com", "squarespace.com",
    "godaddysites.com", "municipalimpact.com", "municipalone.com", "civicplus.com", "civicplus.pro", "revize.com",
    "egovlink.com", "municode.com", "municodeweb.com", "ecode360.com", "boarddocs.com", "legistar.com", "granicus.com",
    "civicclerk.com", "civicweb.net", "iqm2.com", "novusagenda.com", "schoolinsites.com", "schoolwires.net", "finalsite.com",
    "sharpschool.com", "schoolblocks.com", "edlio.com", "apptegy.net", "thrillshare.com", "google.com", "github.io",
    "azurewebsites.net", "cloudfront.net", "amazonaws.com", "blogspot.com", "wpengine.com", "wpenginepowered.com",
    "site123.me", "webs.com", "tripod.com", "myfreesites.net", "netlify.app", "herokuapp.com", "pages.dev", "web.app",
    "townweb.com", "catalis.com", "gov1.com", "hosted.civiclive.com", "specialdistrict.org")
_ENDINGS = tuple(sorted(_PLAIN_ENDINGS + _PLATFORM_ENDINGS, key=lambda e: -len(e)))


def registered(host: str) -> tuple[str, str]:
    """(name, ending) a host is registered as: ("cityofx", "com") for www.cityofx.com and agendas.cityofx.com,
    ("town", "govoffice.com") for town.govoffice.com. ("", "") when it cannot be told: an IP address, an ending not
    listed, or an ending on its own (govoffice.com is nobody's site but the vendor's)."""
    h = (host or "").lower().rstrip(".")
    if ":" in h or re.fullmatch(r"[\d.]+", h):
        return "", ""
    h = h.removeprefix("www.")
    if h in _PLATFORM_ENDINGS:
        return "", ""                           # the platform's own host
    for ending in _ENDINGS:
        if h.endswith("." + ending):
            name = h[: -len(ending) - 1].rsplit(".", 1)[-1]
            return (name, ending) if name and name != "www" else ("", "")
    return "", ""


def site_name(host: str) -> str:
    return registered(host)[0]


def same_site(src: dict, host: str, landed: str, redirected: bool = True) -> bool:
    """May the host an address moved to be read as this source's site? Only when it is
      - the same registered domain (www.cityofx.com, agendas.cityofx.com for cityofx.com); or
      - the same name under another plain ending (cityofx.com -> cityofx.gov, .org, .us), and only when the source's
        own configured address was answered with a redirect to it (`redirected`): the site's own word that it moved.
        Never between platform sub-domains (town.govoffice.com is not www.govoffice.com nor other.govoffice.com); or
      - a host the source's own configuration names (hosts, website, website_alt, endpoint, feeds: what the seed or a
        person recorded for it).
    Anything else is someone else's site: a code publisher (Municode, eCode360), a meeting portal, a link shortener,
    a parked or resold domain. Until this was an allow-list, a town address that redirected to one was crawled there
    and its pages stored under the town."""
    host, landed = host.lower().removeprefix("www."), (landed or "").lower().removeprefix("www.")
    if not landed or NOT_A_SITE.search(landed):
        return False
    if landed == host:
        return True
    (a, a_end), (b, b_end) = registered(host), registered(landed)
    if a and a == b and a_end == b_end:
        return True                             # one registered domain
    if (redirected and a and len(a) >= 4 and a == b and a_end in _PLAIN_ENDINGS and b_end in _PLAIN_ENDINGS):
        return True                             # the same name, moved to another ending
    listed = set()
    for key in ("hosts", "website", "website_alt", "endpoint", "feeds"):
        v = src.get(key)
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(item, str) and item:
                listed.add((urlsplit(item).netloc if "//" in item else item).lower().split("/")[0].removeprefix("www."))
    return landed in listed


class _Streak:
    """Pages in a row that did not answer. After NON_200_IN_A_ROW the pass stops where it is (its cursor stays): a host
    answering 403 or 500 to everything was asked for every address in the queue, 301 requests in one share. Pages
    that are gone (404, 410) are not counted: a stale sitemap or an old listing of dead links is worked through (the
    pass's own request ceiling bounds it), not an error. When the share had read nothing at all before it stopped, the
    host is remembered for the run (fetcher.quiet_hosts) and later shares of the run do not ask it again."""

    def __init__(self, fetcher=None, start: str = "", ctx: "CrawlContext | None" = None):
        self.n, self.last = 0, 0
        self.fetcher, self.host, self.ctx = fetcher, _bare_host(start), ctx
        quiet = getattr(fetcher, "quiet_hosts", None)
        if isinstance(quiet, dict) and self.host and self.host in quiet:
            raise ChannelDown(f"not asked again this run: {quiet[self.host]}")

    def note(self, status: int) -> None:
        if status == 200:
            self.n = 0
            return
        if status in (404, 410):
            return
        self.n, self.last = self.n + 1, status
        if self.n >= NON_200_IN_A_ROW:
            why = f"{self.n} pages in a row did not answer (the last: HTTP {self.last}); the pass waits where it is"
            quiet = getattr(self.fetcher, "quiet_hosts", None)
            if isinstance(quiet, dict) and self.host and self.ctx is not None and not self.ctx.stats.get("ok"):
                quiet[self.host] = why
            raise ChannelDown(why)


def _read_in_pass(ctx: "CrawlContext | None", sid: str, url: str, started: str) -> bool:
    """This source fetched the address since the pass began (in an earlier share: the queue in memory only knows this
    share's). Without it a link found again on a later page would be read a second time."""
    seen = getattr(ctx.frontier, "url_seen", None) if ctx and ctx.frontier else None
    row = seen(url) if seen else None
    return bool(row) and row.get("source_id") == sid and (row.get("fetched_at") or "") >= started


def _read_before(ctx: "CrawlContext", url: str) -> bool:
    """The frontier has this address as read (HTTP 200), at any time."""
    seen = getattr(ctx.frontier, "url_seen", None)
    row = seen(url) if seen else None
    return bool(row) and row.get("status") == 200


class _PassMarks:
    """Decides when a pass writes its cursor: every CHECKPOINT_PAGES pages or CHECKPOINT_SECONDS, when the share ends,
    and (empty) when the pass completes. `value` is a callable giving the cursor's text for the state as it stands."""

    def __init__(self, ctx: "CrawlContext | None", stream: str, value):
        self.on = bool(ctx and getattr(ctx, "resume", False))
        self.stream, self.value = stream, value
        self.had = bool(((ctx.cursors if ctx else None) or {}).get(stream))     # a cursor is stored already
        # the state the share begins in: a share that reads nothing writes nothing (and so moves no cursor, whatever
        # form an earlier version wrote the stored one in)
        self.last = value() if self.on else None
        self.pages, self.at = 0, time.monotonic()
        if self.on:
            ctx.left = self.now             # a caller that stops between two records asks for the cursor itself

    def page(self) -> None:
        self.pages += 1

    def due(self) -> bool:
        return self.on and (self.pages >= CHECKPOINT_PAGES or (self.pages and time.monotonic() - self.at > CHECKPOINT_SECONDS))

    def now(self) -> "Checkpoint | None":
        """The cursor, if the state changed since it was last written."""
        if not self.on:
            return None
        self.pages, self.at = 0, time.monotonic()
        v = self.value()
        if v == self.last:
            return None
        self.last = v
        self.had = True
        return Checkpoint(self.stream, v)

    def done(self) -> "Checkpoint | None":
        """The pass is complete: the stored cursor is emptied (nothing stored: nothing to say)."""
        if not self.on or not self.had or self.last == "":
            return None
        self.last = ""
        return Checkpoint(self.stream, "")


@dataclass
class CrawlContext:
    frontier: object | None = None          # Store (url_seen / fresh / mark_url); None = fetch everything
    cursors: dict = field(default_factory=dict)
    pages_ok: bool = True                   # inside the off-peak window: page crawling allowed
    leaf_ttl_days: float = 3650             # documents: effectively once
    page_revisit_days: float = 7            # HTML pages deeper than depth 1
    probes: list = field(default_factory=list)   # cached probes for this source (for the coverage crawl)
    stats: dict = field(default_factory=lambda: {"fetched": 0, "skipped_fresh": 0, "ok": 0})
    conn_errors: int = 0                    # dropped connections this source (5 in a row: the host is down)
    notes: list = field(default_factory=list)    # addresses passed over and why (the scheduler logs them)
    pass_started: str | None = None         # when the pass now being carried on began (an earlier share's start)
    left: object | None = None              # callable -> Checkpoint | None: the pass under way, as it stands (set by it)
    resume: bool = False                    # the caller stores Checkpoints as cursors (the scheduler): a page pass that
                                            # does not fit one share marks what is left of it and carries on from there


@dataclass
class Probe:
    channel: str
    ok: bool
    latency: float = 0.0
    items: int = 0
    endpoint: str = ""
    note: str = ""

    @property
    def per_item(self) -> float:
        return self.latency / max(1, self.items)



def not_future(published: str | None) -> str | None:
    """`published`, unless it is a day after today (tomorrow allowed, for time zones): a page, post or feed item was
    not published in the future, whatever its markup or its feed says (an event's day, a scheduled post)."""
    if not published or not isinstance(published, str):
        return published
    day = norm_published(published)
    if isinstance(day, str) and day[:4].isdigit() and day[:10] > (dt.date.today() + dt.timedelta(days=1)).isoformat():
        return None
    return published


def date_meta(di: dict) -> dict:
    """meta fields saying where a page's date came from (extract.page_date_info): date_from, and a date of its words
    that was set aside as not the page's own (too old or ahead of the fetch)."""
    return {**({"date_from": di["from"]} if di.get("from") else {}), **({"date_set_aside": di["set_aside"]} if di.get("set_aside") else {})}

def choose(probes: list[Probe]) -> Probe | None:
    """Fastest working structured channel; else best unstructured fallback by priority."""
    ok = [p for p in probes if p.ok and p.items > 0]
    structured = [p for p in ok if p.channel in STRUCTURED]
    if structured:
        return min(structured, key=lambda p: (p.per_item, PRIORITY[p.channel]))
    rest = [p for p in ok if p.channel not in STRUCTURED]
    return min(rest, key=lambda p: PRIORITY[p.channel]) if rest else None


class ChannelCache:
    def __init__(self, path: Path, recheck_days: int = 7):
        self.path = Path(path)
        self.recheck = dt.timedelta(days=recheck_days)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._lock = threading.Lock()

    def get(self, source_id: str) -> dict | None:
        hit = self.data.get(source_id)
        if not hit:
            return None
        when = dt.datetime.fromisoformat(hit["checked_at"])
        if dt.datetime.now(dt.timezone.utc) - when > self.recheck:
            return None
        return hit

    def put(self, source_id: str, chosen: Probe, probes: list[Probe], save: bool = True, url: str = ""):
        row = {
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "chosen": asdict(chosen),
            "probes": [asdict(p) for p in probes],
            **({"url": url} if url else {}),   # the source's address when probed: a changed website is probed again
        }
        # under the lock: save() walks the dict to write it, and a key added meanwhile by another worker ended that
        # worker's source ("error choosing channel: dictionary changed size during iteration", 10 sources, Nov. 8-9)
        with self._lock:
            self.data[source_id] = row
        if save:
            self.save()

    def invalidate(self, source_id: str):
        with self._lock:
            gone = self.data.pop(source_id, None) is not None
        if gone:
            self.save()

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{threading.get_ident()}.tmp")
            write_json_atomic(tmp, self.data, indent=1)
            tmp.replace(self.path)


# --------------------------------------------------------------------------- probing
def _timed(fn):
    t0 = time.monotonic()
    try:
        res = fn()
        return res, time.monotonic() - t0, None
    except Blocked:
        raise  # the whole host refused us; no point probing further
    except Disallowed as exc:
        return None, time.monotonic() - t0, f"robots.txt disallows {exc}"
    except Exception as exc:  # noqa: BLE001 — a failed probe is just a failed probe
        return None, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"[:200]


def fingerprint(html: str) -> set[str]:
    """Which CMS / platform a page comes from, so probing only tries endpoints that platform has."""
    h = html[:400000].lower()
    fp = set()
    if "/wp-content/" in h or "wp-json" in h or 'content="wordpress' in h:
        fp.add("wordpress")
    if "civicplus" in h or "/agendacenter" in h or "rssfeed.aspx" in h or "/documentcenter/" in h:
        fp.add("civicplus")
    if 'content="drupal' in h or "drupal-settings-json" in h or "/sites/default/files/" in h:
        fp.add("drupal")
    if "granicus" in h:
        fp.add("granicus")
    if "revize" in h:
        fp.add("revize")
    if h.count("<script") > 8 and ('id="root"' in h or "ng-app" in h or "__next_data__" in h or 'id="app"' in h):
        fp.add("spa")
    return fp


FINGERPRINT_FEEDS = {
    "wordpress": ["/feed/"],
    "drupal": ["/rss.xml"],
    "civicplus": ["/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml", "/RSSFeed.aspx?ModID=65&CID=All-0"],
}


def probe_source(fetcher: Fetcher, src: dict) -> list[Probe]:
    """Probe the channels this source can plausibly have. Typically 2-4 requests, not a dozen blind guesses:
    feeds come from the page's own <link rel=alternate> tags or its platform's known feed endpoints, the
    WordPress API is tried only on WordPress sites, sitemaps come from robots.txt."""
    base = src["url"].rstrip("/")
    probes: list[Probe] = []

    home_html, fp = "", set()
    res, lat, err = _timed(lambda: fetcher.get(src["url"]))
    if res is not None and res.status == 200 and "html" in res.content_type:
        home_html = res.text
        fp = fingerprint(home_html)
        _, md = extract.html_to_markdown(home_html, res.url)
        # link-heavy home pages with little prose are normal; only an app shell (script-built page) with
        # almost no text counts as unusable as plain HTML
        ok = bool(md) and not ("spa" in fp and len(md) < MIN_PAGE_CHARS)
        probes.append(Probe("html", ok, lat, 1 if ok else 0, src["url"], "" if ok else "no text without a browser"))
    else:
        probes.append(Probe("html", False, lat, 0, src["url"], err or f"HTTP {getattr(res, 'status', '?')}"))

    hints = extract.find_api_hints(home_html, src["url"]) if home_html else {}
    client = src.get("legistar") or hints.get("legistar_client")
    if not client and urlsplit(src["url"]).netloc.endswith(".legistar.com"):
        client = urlsplit(src["url"]).netloc.split(".")[0]
    if client:
        from .sources.legistar import Legistar

        n, lat, err = _timed(Legistar(fetcher, client, src.get("legistar_token")).probe)
        probes.append(Probe("api", bool(n), lat, n or 0, f"legistar:{client}", err or ""))

    cc_client = src.get("civicclerk")
    if not cc_client and ".portal.civicclerk.com" in src["url"]:
        cc_client = urlsplit(src["url"]).netloc.split(".")[0]
    if cc_client:
        from .sources.civicclerk import CivicClerk

        n, lat, err = _timed(CivicClerk(fetcher, cc_client).probe)
        probes.append(Probe("api", bool(n), lat, n or 0, f"civicclerk:{cc_client}", err or ""))

    bd_url = src.get("boarddocs") or (src["url"] if "go.boarddocs.com" in src["url"] else None)
    if bd_url:
        from .sources.boarddocs import BoardDocs

        n, lat, err = _timed(BoardDocs(fetcher, bd_url).probe)
        probes.append(Probe("api", bool(n), lat, n or 0, f"boarddocs:{bd_url}", err or ""))

    if "wordpress" in fp or hints.get("wp_json"):
        wp_posts = (hints.get("wp_json") or base + "/wp-json/").rstrip("/") + "/wp/v2/posts"
        res, lat, err = _timed(lambda: fetcher.get(wp_posts, params={"per_page": 10}))
        if res is not None and res.status == 200 and res.content_type.endswith("json"):
            try:
                n = len(res.json())
            except Exception:  # noqa: BLE001
                n = 0
            probes.append(Probe("wp_json", n > 0, lat, n, wp_posts))

    candidates = list(src.get("feeds") or [])
    if home_html:
        candidates += extract.find_feed_links(home_html, src["url"])
    for name in sorted(fp):
        candidates += [urljoin(base + "/", p.lstrip("/")) for p in FINGERPRINT_FEEDS.get(name, [])]
    best_feed = None
    for url in list(dict.fromkeys(candidates))[:6]:
        res, lat, err = _timed(lambda u=url: fetcher.get(u))
        if res is None or res.status != 200 or not looks_like_feed(res.content, res.content_type):
            continue
        n = len(parse_feed(res.content))
        p = Probe("feed", n > 0, lat, n, url)
        if n and (best_feed is None or p.per_item < best_feed.per_item):
            best_feed = p
        if not src.get("feeds") and best_feed:
            break  # auto-detected; one good feed is enough
    if best_feed:
        probes.append(best_feed)

    sitemaps = fetcher.robots.sitemaps(src["url"]) or [base + "/sitemap.xml"]
    res, lat, err = _timed(lambda: fetcher.get(sitemaps[0]))
    if res is not None and res.status == 200 and b"<loc" in _gunzip(res.content)[:200000]:
        n = len(_sitemap_entries(res.content))
        probes.append(Probe("sitemap", n > 0, lat, n, sitemaps[0]))

    thin = bool(home_html) and not any(p.ok for p in probes if p.channel in ("html", "feed", "sitemap", "wp_json", "api"))
    if src.get("render") == "browser" or (("spa" in fp or thin) and not any(p.ok for p in probes if p.channel == "html")):
        probes.append(Probe("browser", True, 0.0, 1, src["url"], "configured" if src.get("render") else "client-rendered"))
    return probes


# --------------------------------------------------------------------------- helpers
def _date10(value: str | None) -> str | None:
    """YYYY-MM-DD from an ISO date or an RFC 822 feed date ("Fri, 30 Oct 2026 ..."); None if unparseable."""
    if not value:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if m:
        return m.group(0)
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value.strip()).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _wp_load(ctx: CrawlContext, endpoint: str) -> tuple[int, str] | None:
    """(page, when the reading began) of a first reading of a posts API left unfinished, or None: nothing kept, a
    value that does not read, another address's, or a reading begun more than PASS_MAX_AGE_DAYS ago (or ahead of
    now): the list has moved on, it starts over."""
    raw = (ctx.cursors or {}).get(WP_PAGE)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get("url") != endpoint:
            return None
        page = d["page"]
        started = dt.datetime.fromisoformat(d["start"])
        if not isinstance(page, int) or isinstance(page, bool) or page < 1 or started.tzinfo is None:
            return None
        age = dt.datetime.now(dt.timezone.utc) - started
        if age > dt.timedelta(days=PASS_MAX_AGE_DAYS) or age < dt.timedelta(minutes=-5):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return page, d["start"]


def _iso_naive(value) -> bool:
    """A date and time without a zone, as WordPress writes its *_gmt fields ("2026-11-02T19:00:00")."""
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", value))


def _gunzip(content: bytes) -> bytes:
    """A sitemap sent as a .gz file (sitemap.xml.gz, as WordPress plugins and large sites write them): unpacked. It
    was parsed as it came and listed nothing."""
    if content and content[:2] == b"\x1f\x8b":
        import gzip
        import io

        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as fh:
                return fh.read(60_000_000)
        except (OSError, EOFError):
            return b""
    return content


def _sitemap_urls(content: bytes) -> list[str]:
    return [loc for loc, _ in _sitemap_entries(content)]


def _sitemap_entries(content: bytes) -> list[tuple[str, str | None]]:
    """(loc, lastmod) for each <url>/<sitemap> entry; lastmod is None when absent."""
    content = _gunzip(content)
    if not content or not content.strip():
        return []        # an empty sitemap (Gwinnett serves one) lists nothing; it is not an error
    try:
        root = etree.fromstring(content, etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []
    out = []
    for el in root.iter():
        if not (isinstance(el.tag, str) and el.tag.endswith("loc") and el.text):
            continue
        parent = el.getparent()
        lastmod = None
        if parent is not None:
            for sib in parent:
                if isinstance(sib.tag, str) and sib.tag.endswith("lastmod") and sib.text:
                    lastmod = sib.text.strip()
                    break
        out.append((el.text.strip(), lastmod))
    return out


def is_document_url(url: str) -> bool:
    return bool(DOC_EXT.search(url) or DOC_PATH.search(url))


def doc_type(url: str, title: str = "") -> str:
    s = f"{url} {title}".lower()
    for t, pat in (("finance_report", r"campaign[ _-]?(?:contribution|finance|disclosure)|contribution[ _-]?disclosure|\bccdr\b|"
                                      r"disclosure[ _-]?report|financial[ _-]?disclosure"),
                   ("minutes", r"minute"), ("agenda", r"agenda"), ("ordinance", r"ordinance"), ("resolution", r"resolution"),
                   ("results", r"result|canvass|returns|tally|summary report"), ("qualifying", r"qualif"),
                   ("sample_ballot", r"sample ballot|specimen ballot"), ("referendum", r"referendum|splost|question"),
                   ("budget", r"budget"), ("packet", r"packet"), ("transcript", r"transcript|caption")):
        if re.search(pat, s):
            return t
    return "document"


def _filter_urls(urls: list[str], src: dict) -> list[str]:
    inc = src.get("include", [])
    exc = src.get("exclude", [])
    out = []
    for u in urls:
        lu = u.lower()
        if inc and not any(s.lower() in lu for s in inc):
            continue
        if any(s.lower() in lu for s in exc):
            continue
        out.append(u)
    return out


def _skip_fresh(ctx: CrawlContext | None, url: str, depth: int | None = None) -> bool:
    """True if the frontier says this URL was fetched recently enough to skip."""
    if not ctx or not ctx.frontier:
        return False
    if is_document_url(url):
        ttl = ctx.leaf_ttl_days
    elif depth is not None and depth >= 2:
        ttl = ctx.page_revisit_days
    else:
        return False
    if ctx.frontier.fresh(url, ttl):
        ctx.stats["skipped_fresh"] += 1
        return True
    return False


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def http_modified(r, min_age_hours: float = 24.0) -> str | None:
    """The server's own Last-Modified for this answer as ISO 8601 UTC, or None: no header, one that does not read, a
    time ahead of now, or one within `min_age_hours` of now (a page built on request is stamped with the moment it
    was asked for, which says nothing and would change the record on every read)."""
    raw = (getattr(r, "headers", None) or {}).get("last-modified")
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(str(raw).strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    when = when.astimezone(dt.timezone.utc).replace(microsecond=0)
    now = dt.datetime.now(dt.timezone.utc)
    if when > now or now - when < dt.timedelta(hours=min_age_hours) or when.year < 1995:
        return None
    return when.isoformat()


def _modified_meta(r, min_age_hours: float = 24.0) -> dict:
    """meta.http_last_modified, when the server states it. Kept beside `published`, never as it: a file's upload time
    is not the date of the meeting it records (the minutes deriver dates motions by the document's `published`)."""
    when = http_modified(r, min_age_hours)
    return {"http_last_modified": when} if when else {}


def _stamp_modified(doc: Record, r) -> None:
    """A document's Last-Modified (files are stamped when they are posted; any age counts)."""
    doc.meta.update(_modified_meta(r, min_age_hours=0.0))


def _mark(ctx: CrawlContext | None, sid: str, r, asked: str = "") -> None:
    """The frontier's note that this address was fetched. `asked`: the address as it was requested, noted as well when
    the answer came from another one (http to https, a moved file): the frontier is asked about links as they are
    written, so a link that redirects was never "fresh" and was downloaded again on every run."""
    if ctx:
        ctx.stats["fetched"] += 1
        ctx.stats["ok"] = ctx.stats.get("ok", 0) + (1 if r.status == 200 else 0)
        ctx.stats["last_status"] = r.status
        if ctx.frontier:
            ctx.frontier.mark_url(r.url, r.status, sid, r.content_type, r.content if r.status == 200 else None)
            if asked and asked != r.url:
                ctx.frontier.mark_url(asked, r.status, sid, r.content_type)


def _deadline(fetcher) -> float | None:
    """The earlier of the run's and this source's deadlines (OCR stops there too)."""
    f = getattr(fetcher, "deadline", None)
    return f() if callable(f) else None


def _doc_record(r, sid: str, src: dict, title: str = "", stop_at: float | None = None) -> Record | None:
    """A fetched non-HTML document as a record (PDFs parsed; scanned pages OCR'd)."""
    if r.content_type == "application/pdf" or r.content[:5] == b"%PDF-":
        from .ingest import pdf_to_record

        kind = "ordinance" if src.get("kind") == "ordinance" else "document"
        try:
            rec = pdf_to_record(r.content, source_id=sid, kind=kind, url=r.url, title=title, stop_at=stop_at)
        except Exception as exc:  # noqa: BLE001 — one unreadable file must not end the source
            return Record(sid, kind, r.url, title=title or r.url.rsplit("/", 1)[-1], text="", channel="pdf",
                          meta={"opened": True, "doc_type": doc_type(r.url, title), "unreadable": f"{type(exc).__name__}: {exc}"[:200]})
        rec.meta["doc_type"] = doc_type(r.url, rec.title)
        rec.meta["opened"] = True
        return rec
    # Word (.docx) and plain-text files: clerks post minutes and agendas this way too. They were downloaded, no record
    # was made, and the frontier called them read for ten years.
    ctype, path = r.content_type, urlsplit(r.url).path.lower()
    kind = "ordinance" if src.get("kind") == "ordinance" else "document"
    name = title or r.url.rsplit("/", 1)[-1]
    if "wordprocessingml" in ctype or (path.endswith(".docx") and r.content[:2] == b"PK"):
        try:
            from .ordinance_files import _docx_lines, _lines_md

            text, md = _lines_md(_docx_lines(r.content))
        except Exception as exc:  # noqa: BLE001 — one unreadable file must not end the source
            return Record(sid, kind, r.url, title=name, text="", channel="docx",
                          meta={"opened": True, "doc_type": doc_type(r.url, title), "unreadable": f"{type(exc).__name__}: {exc}"[:200]})
        return Record(sid, kind, r.url, title=name, text=text, markdown=md, channel="docx",
                      meta={"opened": True, "doc_type": doc_type(r.url, title)}) if text.strip() else None
    if ctype == "text/plain" and path.endswith(".txt") and not path.endswith("robots.txt"):
        text = r.text.strip()
        if text and "\x00" not in text[:2000]:
            return Record(sid, kind, r.url, title=name, text=text, markdown=text, channel="text",
                          meta={"opened": True, "doc_type": doc_type(r.url, title)})
    return None


PAGE_EXTRACTORS: list = []   # callables(fetcher, sid, src, result, html) -> iterable of extra Records


def page_extras(fetcher: Fetcher, sid: str, src: dict, r, html: str):
    for fn in PAGE_EXTRACTORS:
        try:
            yield from fn(fetcher, sid, src, r, html)
        except (Disallowed, Blocked):
            continue
        except Exception:  # noqa: BLE001 — an extractor must not break the crawl
            continue


def _bare_host(url_or_host: str) -> str:
    return (urlsplit(url_or_host).netloc or url_or_host).lower().removeprefix("www.")


def _try_get(fetcher: Fetcher, url: str, ctx: CrawlContext | None, sid: str):
    """One page: None when it is too large or its server dropped the connection (that page is skipped and noted;
    the rest of the source goes on). Refusals (robots, bot protection) still stop the source, as before."""
    from .fetch import NET_ERRORS, TooLarge

    try:
        return fetcher.get(url)
    except Disallowed:
        return None
    except Blocked as exc:
        # The refusal is another host's (the address redirects to a site on the block list, or to one that answered
        # with a challenge): that is this one address, not the source. It is noted as asked for and passed over, and
        # the crawl goes on. Raised on, as it was, a town's "Code of Ordinances" link to a blocked code publisher
        # stopped the town's whole site, on every share. A refusal by the address's own host still stops the source.
        if _bare_host(getattr(exc, "host", "")) == _bare_host(url):
            raise
        if ctx is not None:
            ctx.notes.append(f"{url} leads to {exc.host}, which refuses the crawler ({exc.reason})"[:200])
            if ctx.frontier:
                ctx.frontier.mark_url(url, 403, sid, "leads to a refusing host")
        return None
    except TooLarge:
        if ctx and ctx.frontier:
            ctx.frontier.mark_url(url, 413, sid, "too large")
        return None
    except NET_ERRORS:
        if ctx is not None:
            ctx.conn_errors = getattr(ctx, "conn_errors", 0) + 1
            if ctx.conn_errors >= 5:
                raise                      # the host is down, not one bad page
        return None
    except RuntimeError as exc:
        if str(exc).startswith("gave up on"):
            return None
        raise


def _page_record(fetcher: Fetcher, sid: str, url: str, src: dict, ctx: CrawlContext | None = None,
                 depth: int | None = None) -> Record | None:
    if _skip_fresh(ctx, url, depth):
        return None
    r = _try_get(fetcher, url, ctx, sid)
    if r is None:
        return None
    if r.status != 200:
        _mark(ctx, sid, r, url)
        return None
    # the frontier's "read" mark only once the document is read: written first, a run that died while a PDF was being
    # parsed (33 shards did, Nov. 7-18, 2026) left the file marked read for ten years with no record of it
    doc = _doc_record(r, sid, src, stop_at=_deadline(fetcher))
    _mark(ctx, sid, r, url)
    if doc or "html" not in r.content_type:
        if doc:
            _stamp_modified(doc, r)
        return doc
    title, md = extract.html_to_markdown(r.text, r.url)
    if not md:
        return None
    di = extract.page_date_info(r.text, md)
    if di.get("date") and di["date"][:10] > (dt.date.today() + dt.timedelta(days=1)).isoformat():
        # a day after today is not when the page was published (an event's day in its markup): set aside
        di = {"date": None, "from": "", "set_aside": di["date"]}
    rec = Record(sid, src.get("kind", "page"), r.url, title=title, text=md, markdown=md, channel="html",
                 meta={"opened": True, **date_meta(di), **_modified_meta(r)}, published=di["date"])
    rec.extras = list(page_extras(fetcher, sid, src, r, r.text))  # transcripts etc. linked from this page
    return rec


def with_extras(rec):
    """Yield a record and anything an extractor found on its page."""
    if rec is None:
        return
    yield rec
    for x in getattr(rec, "extras", None) or []:
        yield x


def _legistar_api(lg, src, limits, since, lim, modified_since, ctx, sid):
    """The Legistar data service: matters, office records, meetings with votes."""
    yield from lg.matters(since, src.get("matter_types"), lim, modified_since=modified_since, cursors=ctx.cursors)
    try:
        yield from lg.officials()            # office records: who sits on which body, when (also the vote sweep's people)
    except RuntimeError:
        pass                                 # some deployments don't expose office records
    yield from lg.meetings(since, src.get("votes", True), lim, modified_since=modified_since, cursors=ctx.cursors,
                           lookback_days=src.get("meeting_lookback_days", limits.get("meeting_lookback_days", 45)),
                           store=ctx.frontier, source_id=sid, votes_mode=src.get("votes_mode", "person"))


# --------------------------------------------------------------------------- crawling
def crawl(fetcher: Fetcher, src: dict, chosen: dict, limits: dict, modified_since: str | None = None,
          ctx: CrawlContext | None = None) -> Iterator[Record | Checkpoint]:
    """Yield records (and Checkpoints) from the chosen channel.

    modified_since (the source's last successful run) turns a re-run into a
    delta: structured channels ask the server for only what changed, and feeds
    and sitemaps drop items older than it. None = full crawl (first run), which
    resumes from ctx.cursors when an earlier full crawl was interrupted.
    """
    ch, endpoint = chosen["channel"], chosen["endpoint"]
    sid = src["id"]
    since = src.get("since", limits.get("since", "2016-01-01"))
    max_pages = src.get("max_pages", limits.get("max_pages", 200))
    cutoff = _date10(modified_since)
    ctx = ctx or CrawlContext()

    if ch == "api" and endpoint.startswith("legistar:"):
        from .sources.legistar import Legistar

        lg = Legistar(fetcher, endpoint.split(":", 1)[1], src.get("legistar_token"))
        lim = src.get("max_items")
        try:
            yield from _legistar_api(lg, src, limits, since, lim, modified_since, ctx, sid)
        except RuntimeError as exc:
            # some cities' Legistar data service is switched on but not set up ("'Agenda Draft Status' ... is not setup
            # in settings"): read the same city's public Legistar pages instead (calendar, meeting details, agendas)
            if "not setup in settings" not in str(exc) and "HTTP 400" not in str(exc):
                raise
            if not ctx.pages_ok or not src.get("url"):
                return                     # page reading keeps to the off-peak window; the next night picks it up
            web = dict(src, url=src["url"], kind="meeting", depth=3,
                       max_pages=src.get("web_max_pages", 150),
                       include=["Calendar.aspx", "MeetingDetail.aspx", "View.ashx", "LegislationDetail.aspx", "Legislation.aspx",
                                "DepartmentDetail.aspx", "PersonDetail.aspx"],
                       exclude=["Feed.ashx", "Login", "Search", "?print", "Video.aspx"])
            yield from _html_crawl(fetcher, web, web["max_pages"], ctx)
        return

    if ch == "api" and endpoint.startswith("youtube:"):
        from .transcripts import youtube_records

        yield from youtube_records(fetcher, sid, src.get("jurisdiction", ""), endpoint.split(":", 1)[1], since,
                                   jurisdiction_name=src.get("jurisdiction_name", ""), basis=src.get("youtube_basis", ""),
                                   modified_since=modified_since, cursors=ctx.cursors if ctx else None)
        return

    if ch == "api" and endpoint.startswith("boarddocs:"):
        from .sources.boarddocs import BoardDocs

        yield from BoardDocs(fetcher, endpoint.split(":", 1)[1]).records(
            sid, src.get("jurisdiction", ""), since, modified_since=modified_since, store=ctx.frontier if ctx else None,
            max_items=int(src.get("max_items") or 120), max_docs=int(src.get("max_docs") or 40),
            lookback_days=src.get("meeting_lookback_days", limits.get("meeting_lookback_days", 45)), src=src)
        return

    if ch == "api" and endpoint.startswith("civicclerk:"):
        from .sources.civicclerk import CivicClerk

        yield from CivicClerk(fetcher, endpoint.split(":", 1)[1]).meetings(
            since, src.get("max_items"), modified_since=modified_since, cursors=ctx.cursors if ctx else None,
            lookback_days=int(src.get("meeting_lookback_days", limits.get("meeting_lookback_days", 45))),
            store=ctx.frontier if ctx else None)
        return

    if ch == "wp_json":
        # a first reading that does not fit one share carries on at the page of posts it had reached (WP_PAGE), and so
        # gets to the site's own pages (_coverage) at last: Butts County's elections site read the same 998 posts on
        # every share, Nov. 14-19, 2026
        # The cursor also keeps when the reading began: the scheduler stamps the source's last good run with that
        # time (ctx.pass_started), not with the last share's, so the next run asks for everything modified since the
        # first page was read. Stamped with the last share's start, a post published or edited after page 1 was read
        # and before the reading ended was never asked for again.
        page, first = 1, True
        marks = None
        if not modified_since and ctx.resume:
            wp_started = _utcnow()
            had = _wp_load(ctx, endpoint)
            if had:
                page, wp_started = had
                if ctx.pass_started is None or wp_started < ctx.pass_started:
                    ctx.pass_started = wp_started
            marks = _PassMarks(ctx, WP_PAGE, lambda: json.dumps({"page": page, "start": wp_started, "url": endpoint},
                                                                separators=(",", ":"), sort_keys=True))
        while page <= max_pages:
            params = {"per_page": 50, "page": page, "orderby": "modified", "order": "desc"}
            params["modified_after" if modified_since else "after"] = (modified_since or since + "T00:00:00")
            r = fetcher.get(endpoint, params=params, use_cache=not modified_since)
            if r.status != 200:
                if first and (page == 1 or r.status != 400):
                    raise ChannelDown(f"the posts API answered HTTP {r.status}")
                break               # past the last page (WordPress answers 400 there)
            first = False
            rows = r.json()
            if not rows:
                break
            for p in rows:
                _, md = extract.html_to_markdown(p.get("content", {}).get("rendered", ""), p.get("link", ""))
                yield Record(sid, "post" if src.get("kind") == "campaign" else src.get("kind", "page"), p.get("link", ""),
                             title=extract.soup_of(p.get("title", {}).get("rendered", "")).get_text(),
                             text=md, markdown=md, published=not_future(p.get("date")), channel="wp_json",
                             # the post's id under a name the record's key does not take (store.ID_FIELDS): the same
                             # address read by the coverage crawl is the same record, not a second one; a post with no
                             # address keeps the id in its key
                             meta={("wp_post_id" if p.get("link") else "wp_id"): p.get("id"), "opened": True,
                                   **({"modified": p["modified_gmt"] + "+00:00"} if _iso_naive(p.get("modified_gmt")) else {})})
            page += 1
            if marks:
                cp = marks.now()        # this page of posts is stored: the next share begins at the next one
                if cp:
                    yield cp
        yield from _coverage(fetcher, src, ctx, max_pages)
        if marks:
            cp = marks.done()
            if cp:
                yield cp
        return

    if ch == "feed":
        r = fetcher.get(endpoint, use_cache=not modified_since)
        if r.status != 200:
            raise ChannelDown(f"the feed answered HTTP {r.status}")
        for item in parse_feed(r.content):
            if cutoff and _date10(item.get("published")) and _date10(item["published"]) < cutoff:
                continue  # already had this on the last run
            if src.get("follow_feed_links", True) and item["link"]:
                if _skip_fresh(ctx, item["link"], depth=2):
                    continue
                page = _page_record(fetcher, sid, item["link"], src, ctx)
                if page:
                    page.channel = "feed"
                    # the feed's own date first: the page's is read from its text, where the first date is often
                    # one the story mentions (a release of Nov. 17 announcing a Dec. 15 hearing was dated Dec. 15)
                    if not_future(item["published"]):
                        page.published = norm_published(item["published"])
                        page.meta = {**{k: v for k, v in page.meta.items() if k != "date_set_aside"}, "date_from": "feed"}
                    # not "guid", which store.ID_FIELDS puts in the key: the coverage crawl reads the same page with
                    # none, and each run stored it twice (news/budget-hearing under "g-1" and under no id)
                    page.meta["feed_guid"] = item["id"]
                    yield from with_extras(page)
                    continue
            yield Record(sid, "feed_item", item["link"], title=item["title"], text=item["summary"],
                         published=not_future(item["published"]), channel="feed", meta={"feed": endpoint, "guid": item["id"]})
        yield from _coverage(fetcher, src, ctx, max_pages)
        return

    if ch == "sitemap":
        yield from _sitemap_crawl(fetcher, src, endpoint, ctx, max_pages, cutoff, use_cache=not modified_since)
        return

    if ch == "browser":
        yield from _browser_crawl(fetcher, src, max_pages, ctx)
        return

    yield from _html_crawl(fetcher, src, max_pages, ctx)


def _coverage(fetcher: Fetcher, src: dict, ctx: CrawlContext, max_pages: int) -> Iterator[Record]:
    """Official websites: a news feed or posts API covers news, not council pages and documents.
    Follow up with the sitemap (or an HTML crawl) — only inside the page-crawling window."""
    if not src.get("coverage") or not ctx.pages_ok:
        return
    sm = next((p for p in ctx.probes if p.get("channel") == "sitemap" and p.get("ok")), None)
    if sm:
        try:
            yield from _sitemap_crawl(fetcher, src, sm["endpoint"], ctx, max_pages, None, use_cache=True)
        except ChannelDown:
            return          # the feed or posts API was read; the site's own pages wait for the next run
    else:
        try:
            yield from _html_crawl(fetcher, src, max_pages, ctx)
        except ChannelDown:
            return          # as above: the feed was read; the pages (their cursor kept) wait for another time


def _sitemap_crawl(fetcher: Fetcher, src: dict, endpoint: str, ctx: CrawlContext, max_pages: int, cutoff: str | None,
                   use_cache: bool = True) -> Iterator[Record | Checkpoint]:
    sid = src["id"]
    store = ctx.frontier if ctx else None
    whole = bool(store is not None and (ctx.cursors or {}).get("sitemap_read"))
    # A pass that did not fit one share carries on from the addresses it had left (SITEMAP_LEFT) without reading the
    # sitemap and its children again: up to 51 requests before the first page, every share (Clayton County, Nov. 19,
    # 2026: "partial (sitemap delta): 0 new / 0 seen", share after share).
    left = _pass_load(ctx, SITEMAP_LEFT, endpoint)
    if left:
        started, n, urls, cut = left["start"], left["n"], list(left["q"]), bool(left.get("cut"))
    else:
        started, n, cut = _utcnow(), 0, False
        r = fetcher.get(endpoint, use_cache=use_cache)
        if r.status != 200:
            raise ChannelDown(f"the sitemap answered HTTP {r.status}")
        # "Unchanged since the last run" is a reason to skip a page only if the page has been read. A run reads at most
        # max_pages of the list (40 to 150), was marked complete, and every later run then dropped the pages it never
        # reached, their dates being older than the last run: Cobb County's site, 120 of 807 pages read and the other
        # 687 skipped for good; 240 sources are read this way. So the cut-off applies to pages the frontier has seen,
        # never to one it has not; and a child sitemap is skipped as unchanged only once a run has got through the whole
        # list without reaching max_pages (the "sitemap_read" mark).
        entries = _sitemap_entries(r.content)
        if entries and all(loc.endswith((".xml", ".xml.gz")) or "sitemap" in loc.lower() for loc, _ in entries[:5]):
            nested = []
            for loc, lastmod in entries[:SITEMAP_CHILDREN]:
                if whole and cutoff and lastmod and lastmod[:10] < cutoff:
                    continue  # whole child sitemap unchanged since last run
                try:
                    nested += _sitemap_entries(fetcher.get(loc, use_cache=use_cache).content)
                except (Disallowed, Blocked):
                    pass
            cut = len(entries) > SITEMAP_CHILDREN       # more children than are read: never "the whole list"
            entries = nested
        seen = getattr(store, "url_seen", None)
        entries = [(loc, lm) for loc, lm in entries if not (cutoff and lm and lm[:10] < cutoff and (seen is None or seen(loc)))]
        # relevant pages first, so max_pages is spent where the records are
        urls = sorted(_filter_urls([loc for loc, _ in entries], src), key=lambda u: (not HOT.search(u), bool(COLD.search(u))))
    i = 0
    streak = _Streak(fetcher, src.get("url") or endpoint, ctx)
    marks = _PassMarks(ctx, SITEMAP_LEFT, lambda: _pass_value(endpoint, started, n, urls[i:], **({"cut": 1} if cut else {})))
    while i < len(urls):
        if n >= max_pages:
            break               # more of the list is left for the next pass: no "whole list read" mark
        if marks.due():
            cp = marks.now()
            if cp:
                yield cp
        u = urls[i]
        if _skip_fresh(ctx, u, depth=2):
            i += 1
            continue
        before = (ctx.stats["fetched"], ctx.stats.get("ok", 0))
        try:
            rec = _page_record(fetcher, sid, u, src, ctx)
        except Exception:
            cp = marks.now()        # the share's time is up: this page and the rest are still to be read
            if cp:
                yield cp
            raise
        i += 1
        n += 1
        marks.page()
        if ctx.stats["fetched"] > before[0]:
            try:
                streak.note(200 if ctx.stats.get("ok", 0) > before[1] else int(ctx.stats.get("last_status") or 0))
            except ChannelDown:
                cp = marks.now()    # the cursor stays: the pass carries on from here another time
                if cp:
                    yield cp
                raise
        yield from with_extras(rec)
    cp = marks.done()
    if cp:
        yield cp
    if i >= len(urls) and not cut and store is not None and not whole:
        yield Checkpoint("sitemap_read", dt.date.today().isoformat())


ARTICLE_PAGE = re.compile(r'''property=["']og:type["'][^>]*content=["']article|content=["']article["'][^>]*property=["']og:type|'''
                          r'''<article[\s>]|"@type"\s*:\s*"(?:News|Blog)?(?:Article|Posting|Report)"|article:published_time''', re.I)


def stated_date(html: str, md: str, depth: int | None = None) -> tuple[str | None, str]:
    """(date, where from) for a page read by the page crawls, only when the page is an article or post and its markup
    states when it was published: a date meta tag or JSON-LD datePublished. Not a <time> element (a home page's
    "upcoming events" box dated the city's home page Dec. 12, a parade's day), not the first date among its words
    (on a council page, the next meeting's), not the source's own address (depth 0), and never a day after today."""
    if depth == 0 or not ARTICLE_PAGE.search(html[:300000]):
        return None, ""
    di = extract.page_date_info(html, md)
    d = di.get("date")
    if not d or di.get("from") not in ("meta", "jsonld"):
        return None, ""
    if d[:10] > (dt.date.today() + dt.timedelta(days=1)).isoformat():
        return None, ""
    return d, di["from"]


def _html_crawl(fetcher: Fetcher, src: dict, max_pages: int, ctx: CrawlContext | None = None) -> Iterator[Record | Checkpoint]:
    """Polite breadth-first crawl inside the site, hot links (agendas, minutes, ordinances...) first. A pass that does
    not fit one share of a run carries on where it stopped (ctx.resume: the FRONTIER cursor holds the queue), so every
    page is read once in a pass and the pass ends: at the last page, at max_pages counted over all its shares, or at
    max_pages + PASS_EXTRA_REQUESTS requests of any kind."""
    ctx = ctx or CrawlContext()
    sid = src["id"]
    start = src["url"]
    host = urlsplit(start).netloc.lower().removeprefix("www.")
    hosts = {host}                          # and the host the source's own address moves to, when it is the same site
    depth_max = src.get("depth", 3)
    hot_rx = src.get("hot") or HOT          # a source can name its own hot links (campaign sites: issues, about, endorsements)
    left = _pass_load(ctx, FRONTIER, start)
    if left:
        started, count, asked = left["start"], left["n"], left["a"]
        hosts |= {h for h in left["hosts"] if same_site(src, host, h)}      # checked again: the cursor is data
        mine = [e for e in left["q"] if urlsplit(e[0]).netloc.lower().removeprefix("www.") in hosts]
        hot = deque((u, d) for u, d, h in mine if h)
        cold = deque((u, d) for u, d, h in mine if not h)
    else:
        started, count, asked = _utcnow(), 0, 0
        hot, cold = deque([(start, 0)]), deque()
    queued = {u for u, _ in hot} | {u for u, _ in cold}
    streak = _Streak(fetcher, start, ctx)
    marks = _PassMarks(ctx, FRONTIER, lambda: _pass_value(
        start, started, count, [[u, d, 1] for u, d in hot] + [[u, d, 0] for u, d in cold], a=asked,
        **({"hosts": sorted(hosts - {host})} if len(hosts) > 1 else {})))
    while (hot or cold) and count < max_pages and asked < max_pages + PASS_EXTRA_REQUESTS:
        if marks.due():
            cp = marks.now()
            if cp:
                yield cp
        q = hot if hot else cold
        url, depth = q.popleft()
        if _skip_fresh(ctx, url, depth):
            continue
        # A listing page (the home page and the pages it links to) is read again on every pass, for its new links. Such
        # a re-read does not use up the pass's page count: a town's site is given 40 to 80 pages a pass, its home page
        # links to as many, and counted, the listing pages alone were the whole pass, every pass (the pages below them
        # were never reached). The count is of pages not read before (whenever that was: a small town's site is read
        # once a month). Every request, re-reads and pages that do not answer included, counts toward the pass's
        # ceiling of max_pages + PASS_EXTRA_REQUESTS.
        again = bool(depth <= 1 and ctx.frontier and _read_before(ctx, url))
        try:
            r = _try_get(fetcher, url, ctx, sid)
        except Exception:
            # the share's time is up (or the host refused, or went down): this page is still to be read
            q.appendleft((url, depth))
            cp = marks.now()
            if cp:
                yield cp
            raise
        marks.page()
        if r is None:
            continue
        asked += 1
        if r.status != 200:
            _mark(ctx, sid, r, url)
            try:
                streak.note(r.status)
            except ChannelDown:
                cp = marks.now()        # the cursor stays: the pass carries on from here another time
                if cp:
                    yield cp
                raise
            continue
        streak.note(200)
        landed = getattr(r, "final_url", "") or ""
        landed_host = urlsplit(landed).netloc.lower().removeprefix("www.") if landed else ""
        if landed_host and landed_host not in hosts:
            if depth == 0 and url == start and same_site(src, host, landed_host):
                # the source's own address answered from another host of the same name (cityofx.com moved to
                # cityofx.gov) or one its configuration lists: that host is the site. Its links used to be dropped
                # as "another site", and the crawl ended at the home page.
                hosts.add(landed_host)
            elif depth == 0 and url == start:
                # ... from a host that is not this source's (a code publisher, a portal, a parked domain): nothing of
                # it is read or stored under this source
                q.appendleft((url, depth))
                raise ChannelDown(f"the source's address now answers from {landed_host}, which is not listed for it "
                                  f"(same name under another ending, or hosts/website in its configuration); not read")
            else:
                _mark(ctx, sid, r, url)
                continue                    # a page of the site that sends the reader elsewhere: not this site's page
        if not again:
            count += 1
        # a page that answered from another host of the site: its links are read from where it is
        base = landed if landed_host and landed_host != urlsplit(r.url).netloc.lower().removeprefix("www.") else r.url
        doc = _doc_record(r, sid, src, stop_at=_deadline(fetcher))
        _mark(ctx, sid, r, url)   # after the document is read (see _page_record)
        if doc:
            _stamp_modified(doc, r)
            yield doc
            continue
        if "html" not in r.content_type:
            continue
        # the page's links are queued before its record is handed over: wherever the caller stops, the queue is whole
        if depth < depth_max:
            for link in extract.links(r.text, base):
                lh = urlsplit(link).netloc.lower().removeprefix("www.")
                if lh not in hosts or link in queued or not _filter_urls([link], {"exclude": src.get("exclude", [])}):
                    continue
                if COLD.search(link) and not is_document_url(link):
                    continue
                queued.add(link)
                if left and _read_in_pass(ctx, sid, link, started):
                    continue                # read in an earlier share of this pass
                (hot if hot_rx.search(link) or is_document_url(link) else cold).append((link, depth + 1))
        title, md = extract.html_to_markdown(r.text, r.url)
        if md and (not src.get("include") or _filter_urls([r.url], {"include": src["include"]})):
            stated, how = stated_date(r.text, md, depth)
            yield Record(sid, src.get("kind", "page"), r.url, title=title, text=md, markdown=md, channel="html",
                         meta={"opened": True, **({"date_from": how} if stated else {}), **_modified_meta(r)},
                         published=stated)
            yield from page_extras(fetcher, sid, src, r, r.text)
    cp = marks.done()
    if cp:
        yield cp


POLICY_CODE_RX = re.compile(r"\b([A-Z]{2,5}(?:\(\d+\))?(?:-[A-Z]{1,3})?)\b\s*[-:–—]\s*(.{4,120})")
POLICY_DATE_RX = re.compile(r"(?:adopted|effective|revised|last revised|approved)\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})", re.I)


def policy_meta(title: str, md: str) -> dict:
    """A board policy page's code (GSBA codes like BBA, GARH, JCDA) and adoption/revision date, when the page shows them."""
    out = {}
    m = POLICY_CODE_RX.search(title or "") or POLICY_CODE_RX.search(md[:600])
    if m:
        out["code"], out["policy_title"] = m.group(1), m.group(2).strip()
    d = POLICY_DATE_RX.search(md[:4000])
    if d:
        out["adopted"] = d.group(1)
    return out


BROWSER_LOCK = threading.Lock()   # one headless browser at a time: Playwright's sync API driven from several worker
                                  # threads at once crashed the whole run (segmentation fault, Nov 7)


def _browser_crawl(fetcher: Fetcher, src: dict, max_pages: int, ctx: CrawlContext | None = None) -> Iterator[Record]:
    """Headless Chromium for JS-rendered sites (code libraries, SPA city sites).
    Same robots/rate-limit/frontier rules as everything else. One browser crawl runs at a time (BROWSER_LOCK)."""
    with BROWSER_LOCK:
        yield from _browser_crawl_locked(fetcher, src, max_pages, ctx)


def _browser_crawl_locked(fetcher: Fetcher, src: dict, max_pages: int, ctx: CrawlContext | None = None) -> Iterator[Record]:
    from playwright.sync_api import sync_playwright

    from .fetch import launch_browser

    ctx = ctx or CrawlContext()
    sid = src["id"]
    start = src["url"]
    host = urlsplit(start).netloc
    selector = src.get("content_selector")
    link_pattern = src.get("follow")  # substring a link must contain to be followed
    # a pass that does not fit one share carries on from its queue (FRONTIER), as _html_crawl does: Haralson County
    # Schools' site was "partial (browser full): 0 new / 22 seen" on every share, Nov. 14-19, 2026
    base_host = host.lower().removeprefix("www.")
    # www.x and x are one site, and so is the host the source's own address lands on when it is the same site (as in
    # _html_crawl): the links of a site whose address is written without "www." were all dropped as another site's
    hosts = {base_host}
    left = _pass_load(ctx, FRONTIER, start)
    if left:
        started, count, asked = left["start"], left["n"], left["a"]
        hosts |= {h for h in left["hosts"] if same_site(src, base_host, h)}
        queue = deque((u, d) for u, d, _ in left["q"] if urlsplit(u).netloc.lower().removeprefix("www.") in hosts)
    else:
        started, count, asked = _utcnow(), 0, 0
        queue = deque([(start, 0)])
    seen = set()

    def value() -> str:
        items, had = [], set()
        for u, d in queue:
            if u not in had and u not in seen:
                had.add(u)
                items.append([u, d, 1])
        extra = sorted(hosts - {base_host})
        return _pass_value(start, started, count, items, a=asked, **({"hosts": extra} if extra else {}))

    marks = _PassMarks(ctx, FRONTIER, value)
    streak = _Streak(fetcher, start, ctx)
    stop: Exception | None = None           # why the pass ended early: raised once the browser is closed
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(user_agent=fetcher.user_agent)
        while queue and count < max_pages and asked < max_pages + PASS_EXTRA_REQUESTS:
            if marks.due():
                cp = marks.now()
                if cp:
                    yield cp
            url, depth = queue.popleft()
            if url in seen:
                continue
            if left and _read_in_pass(ctx, sid, url, started):
                seen.add(url)               # read in an earlier share of this pass
                continue
            held = fetcher.blocklist.is_blocked(url)
            if held:
                # the browser does not go through fetcher.get, where the block list is consulted: asked here
                queue.appendleft((url, depth))
                stop = Blocked(fetcher.host(url), held["reason"])
                break
            if not fetcher.allowed(url) or _skip_fresh(ctx, url, depth):
                seen.add(url)
                continue
            try:
                fetcher._check_time()                       # the browser does not go through fetcher.get: check here
                fetcher._turn(url, fetcher.robots.crawl_delay(url))
            except Exception:
                queue.appendleft((url, depth))              # the share's time is up: this page is still to be read
                cp = marks.now()
                if cp:
                    yield cp
                raise
            seen.add(url)
            marks.page()
            asked += 1
            t0 = time.monotonic()
            try:
                resp = page.goto(url, wait_until="load", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:  # noqa: BLE001 — a page that keeps a connection open never goes idle
                    page.wait_for_timeout(6000)
            except Exception:  # noqa: BLE001
                fetcher.limiter.record(url, 599, time.monotonic() - t0)
                continue
            status = resp.status if resp else 0
            fetcher.limiter.record(url, status, time.monotonic() - t0)
            ctx.stats["fetched"] += 1
            ctx.stats["ok"] = ctx.stats.get("ok", 0) + (1 if status == 200 else 0)
            from .blocked import looks_blocked

            why = looks_blocked(status, page.content().encode("utf-8", "ignore"))
            if why:
                # a challenge page: the site is left alone (never worked around), and the source is filed as refused,
                # not as read ("ok", as it used to end)
                at = str(getattr(page, "url", "") or url)
                at = at if at.startswith("http") else url
                fetcher.blocklist.add(at, why, sid)         # filed against the host that served it
                if _bare_host(at) not in hosts:
                    # ... which is not this site: the address led elsewhere. Passed over; the crawl goes on
                    ctx.notes.append(f"{url} leads to {_bare_host(at)}, which refuses the crawler ({why})"[:200])
                    continue
                queue.appendleft((url, depth))
                stop = Blocked(fetcher.host(at), why)
                break
            landed = urlsplit(str(getattr(page, "url", "") or "")).netloc.lower().removeprefix("www.")
            if landed and landed not in hosts:
                if url == start and depth == 0 and same_site(src, base_host, landed) and fetcher.allowed(page.url) \
                        and not fetcher.blocklist.is_blocked(page.url):
                    hosts.add(landed)       # the same site under another ending (see same_site)
                elif url == start and depth == 0:
                    queue.appendleft((url, depth))
                    stop = ChannelDown(f"the source's address now answers from {landed}, which is not listed for it; not read")
                    break
                else:
                    continue                # a page that sends the reader to someone else's site: not this site's page
            if ctx.frontier:
                ctx.frontier.mark_url(url, status, sid, "text/html", page.content().encode() if status == 200 else None)
            if status != 200:
                try:
                    streak.note(status)
                except ChannelDown as exc:
                    stop = exc
                    break
                continue
            streak.note(200)
            count += 1
            html = page.inner_html(selector) if selector and page.query_selector(selector) else page.content()
            title, md = extract.html_to_markdown(f"<html><head><title>{page.title()}</title></head><body>{html}</body></html>", url)
            for link in extract.links(page.content(), url):
                if urlsplit(link).netloc.lower().removeprefix("www.") in hosts and (not link_pattern or link_pattern in link) and link not in seen \
                        and not any(x in link for x in (src.get("exclude") or [])):
                    queue.append((link, depth + 1))
            if md:
                kind = next((k for sub, k in (src.get("kind_by_url") or []) if sub in url), src.get("kind", "page"))
                meta = {"opened": True}
                if kind == "policy":
                    meta.update(policy_meta(title, md))
                stated, how = stated_date(page.content(), md, depth)     # as _html_crawl: an article's own stated date
                if stated:
                    meta["date_from"] = how
                yield Record(sid, kind, url, title=title, text=md, markdown=md, channel="browser", meta=meta, published=stated)
        else:
            cp = marks.done()               # the pass ended at its last page (or its page count), not at a refusal
            if cp:
                yield cp
        browser.close()
    if stop is not None:
        cp = marks.now()                    # the cursor stays as it is: the pass carries on from here another time
        if cp:
            yield cp
        raise stop
