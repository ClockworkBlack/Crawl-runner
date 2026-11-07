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
from .store import Record

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
COLD = re.compile(r"calendar|event|parks?-and-rec|facility|login|search|sitemap|privacy|accessib|employment|jobs|"
                  r"pay-|bill-pay|translate|facebook|twitter|instagram|youtube|mailto:|tel:|\.(jpg|jpeg|png|gif|svg|mp4|zip)$",
                  re.I)


@dataclass
class Checkpoint:
    """Yielded by a crawl when a backfill window is complete: resume from `value` next time."""
    stream: str
    value: str


@dataclass
class CrawlContext:
    frontier: object | None = None          # Store (url_seen / fresh / mark_url); None = fetch everything
    cursors: dict = field(default_factory=dict)
    pages_ok: bool = True                   # inside the off-peak window: page crawling allowed
    leaf_ttl_days: float = 3650             # documents: effectively once
    page_revisit_days: float = 7            # HTML pages deeper than depth 1
    probes: list = field(default_factory=list)   # cached probes for this source (for the coverage crawl)
    stats: dict = field(default_factory=lambda: {"fetched": 0, "skipped_fresh": 0})
    conn_errors: int = 0                    # dropped connections this source (5 in a row: the host is down)


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

    def put(self, source_id: str, chosen: Probe, probes: list[Probe], save: bool = True):
        self.data[source_id] = {  # dict assignment is atomic; the file write is locked
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "chosen": asdict(chosen),
            "probes": [asdict(p) for p in probes],
        }
        if save:
            self.save()

    def invalidate(self, source_id: str):
        if self.data.pop(source_id, None) is not None:
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
    if res is not None and res.status == 200 and b"<loc" in res.content[:200000]:
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


def _sitemap_urls(content: bytes) -> list[str]:
    return [loc for loc, _ in _sitemap_entries(content)]


def _sitemap_entries(content: bytes) -> list[tuple[str, str | None]]:
    """(loc, lastmod) for each <url>/<sitemap> entry; lastmod is None when absent."""
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


def _mark(ctx: CrawlContext | None, sid: str, r) -> None:
    if ctx:
        ctx.stats["fetched"] += 1
        if ctx.frontier:
            ctx.frontier.mark_url(r.url, r.status, sid, r.content_type, r.content if r.status == 200 else None)


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


def _try_get(fetcher: Fetcher, url: str, ctx: CrawlContext | None, sid: str):
    """One page: None when it is too large or its server dropped the connection (that page is skipped and noted;
    the rest of the source goes on). Refusals (robots, bot protection) still stop the source, as before."""
    import requests

    from .fetch import TooLarge

    try:
        return fetcher.get(url)
    except Disallowed:
        return None
    except TooLarge:
        if ctx and ctx.frontier:
            ctx.frontier.mark_url(url, 413, sid, "too large")
        return None
    except (requests.ConnectionError, requests.Timeout):
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
    _mark(ctx, sid, r)
    if r.status != 200:
        return None
    doc = _doc_record(r, sid, src, stop_at=_deadline(fetcher))
    if doc or "html" not in r.content_type:
        return doc
    title, md = extract.html_to_markdown(r.text, r.url)
    if not md:
        return None
    rec = Record(sid, src.get("kind", "page"), r.url, title=title, text=md, markdown=md, channel="html", meta={"opened": True},
                 published=extract.page_date(r.text, md))
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
        page = 1
        while page <= max_pages:
            params = {"per_page": 50, "page": page, "orderby": "modified", "order": "desc"}
            params["modified_after" if modified_since else "after"] = (modified_since or since + "T00:00:00")
            r = fetcher.get(endpoint, params=params, use_cache=not modified_since)
            if r.status != 200:
                break
            rows = r.json()
            if not rows:
                break
            for p in rows:
                _, md = extract.html_to_markdown(p.get("content", {}).get("rendered", ""), p.get("link", ""))
                yield Record(sid, "post" if src.get("kind") == "campaign" else src.get("kind", "page"), p.get("link", ""),
                             title=extract.soup_of(p.get("title", {}).get("rendered", "")).get_text(),
                             text=md, markdown=md, published=p.get("date"), channel="wp_json",
                             meta={"wp_id": p.get("id"), "opened": True})
            page += 1
        yield from _coverage(fetcher, src, ctx, max_pages)
        return

    if ch == "feed":
        r = fetcher.get(endpoint, use_cache=not modified_since)
        for item in parse_feed(r.content):
            if cutoff and _date10(item.get("published")) and _date10(item["published"]) < cutoff:
                continue  # already had this on the last run
            if src.get("follow_feed_links", True) and item["link"]:
                if _skip_fresh(ctx, item["link"], depth=2):
                    continue
                page = _page_record(fetcher, sid, item["link"], src, ctx)
                if page:
                    page.channel = "feed"
                    page.published = page.published or item["published"]
                    page.meta["guid"] = item["id"]
                    yield from with_extras(page)
                    continue
            yield Record(sid, "feed_item", item["link"], title=item["title"], text=item["summary"],
                         published=item["published"], channel="feed", meta={"feed": endpoint, "guid": item["id"]})
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
        yield from _sitemap_crawl(fetcher, src, sm["endpoint"], ctx, max_pages, None, use_cache=True)
    else:
        yield from _html_crawl(fetcher, src, max_pages, ctx)


def _sitemap_crawl(fetcher: Fetcher, src: dict, endpoint: str, ctx: CrawlContext, max_pages: int, cutoff: str | None,
                   use_cache: bool = True) -> Iterator[Record]:
    sid = src["id"]
    r = fetcher.get(endpoint, use_cache=use_cache)
    entries = _sitemap_entries(r.content)
    if entries and all(loc.endswith(".xml") or "sitemap" in loc.lower() for loc, _ in entries[:5]):
        nested = []
        for loc, lastmod in entries[:50]:
            if cutoff and lastmod and lastmod[:10] < cutoff:
                continue  # whole child sitemap unchanged since last run
            try:
                nested += _sitemap_entries(fetcher.get(loc, use_cache=use_cache).content)
            except (Disallowed, Blocked):
                pass
        entries = nested
    entries = [(loc, lm) for loc, lm in entries if not (cutoff and lm and lm[:10] < cutoff)]
    # relevant pages first, so max_pages is spent where the records are
    urls = sorted(_filter_urls([loc for loc, _ in entries], src), key=lambda u: (not HOT.search(u), bool(COLD.search(u))))
    n = 0
    for u in urls:
        if n >= max_pages:
            break
        if _skip_fresh(ctx, u, depth=2):
            continue
        rec = _page_record(fetcher, sid, u, src, ctx)
        n += 1
        yield from with_extras(rec)


def _html_crawl(fetcher: Fetcher, src: dict, max_pages: int, ctx: CrawlContext | None = None) -> Iterator[Record]:
    """Polite breadth-first crawl inside the site, hot links (agendas, minutes, ordinances...) first."""
    ctx = ctx or CrawlContext()
    start = src["url"]
    host = urlsplit(start).netloc.lower().removeprefix("www.")
    hot, cold = deque([(start, 0)]), deque()
    queued = {start}
    depth_max = src.get("depth", 3)
    hot_rx = src.get("hot") or HOT          # a source can name its own hot links (campaign sites: issues, about, endorsements)
    count = 0
    while (hot or cold) and count < max_pages:
        url, depth = (hot or cold).popleft()
        if _skip_fresh(ctx, url, depth):
            continue
        r = _try_get(fetcher, url, ctx, src["id"])
        if r is None:
            continue
        _mark(ctx, src["id"], r)
        if r.status != 200:
            continue
        count += 1
        doc = _doc_record(r, src["id"], src, stop_at=_deadline(fetcher))
        if doc:
            yield doc
            continue
        if "html" not in r.content_type:
            continue
        title, md = extract.html_to_markdown(r.text, r.url)
        if md and (not src.get("include") or _filter_urls([r.url], {"include": src["include"]})):
            yield Record(src["id"], src.get("kind", "page"), r.url, title=title, text=md, markdown=md, channel="html",
                         meta={"opened": True})
            yield from page_extras(fetcher, src["id"], src, r, r.text)
        if depth >= depth_max:
            continue
        for link in extract.links(r.text, r.url):
            lh = urlsplit(link).netloc.lower().removeprefix("www.")
            if lh != host or link in queued or not _filter_urls([link], {"exclude": src.get("exclude", [])}):
                continue
            if COLD.search(link) and not is_document_url(link):
                continue
            queued.add(link)
            (hot if hot_rx.search(link) or is_document_url(link) else cold).append((link, depth + 1))


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


def _browser_crawl(fetcher: Fetcher, src: dict, max_pages: int, ctx: CrawlContext | None = None) -> Iterator[Record]:
    """Headless Chromium for JS-rendered sites (code libraries, SPA city sites).
    Same robots/rate-limit/frontier rules as everything else."""
    from playwright.sync_api import sync_playwright

    from .fetch import launch_browser

    ctx = ctx or CrawlContext()
    start = src["url"]
    host = urlsplit(start).netloc
    selector = src.get("content_selector")
    link_pattern = src.get("follow")  # substring a link must contain to be followed
    seen, queue, count = set(), deque([(start, 0)]), 0
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(user_agent=fetcher.user_agent)
        while queue and count < max_pages:
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if not fetcher.allowed(url) or _skip_fresh(ctx, url, depth):
                continue
            fetcher._check_time()                       # the browser does not go through fetcher.get: check here
            fetcher._turn(url, fetcher.robots.crawl_delay(url))
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
            from .blocked import looks_blocked

            why = looks_blocked(status, page.content().encode("utf-8", "ignore"))
            if why:
                fetcher.blocklist.add(url, why, src["id"])
                break                      # a challenge page: leave the site alone (never worked around)
            if ctx.frontier:
                ctx.frontier.mark_url(url, status, src["id"], "text/html", page.content().encode() if status == 200 else None)
            if status != 200:
                continue
            count += 1
            html = page.inner_html(selector) if selector and page.query_selector(selector) else page.content()
            title, md = extract.html_to_markdown(f"<html><head><title>{page.title()}</title></head><body>{html}</body></html>", url)
            if md:
                kind = next((k for sub, k in (src.get("kind_by_url") or []) if sub in url), src.get("kind", "page"))
                meta = {"opened": True}
                if kind == "policy":
                    meta.update(policy_meta(title, md))
                yield Record(src["id"], kind, url, title=title, text=md, markdown=md, channel="browser", meta=meta)
            for link in extract.links(page.content(), url):
                if urlsplit(link).netloc == host and (not link_pattern or link_pattern in link) and link not in seen \
                        and not any(x in link for x in (src.get("exclude") or [])):
                    queue.append((link, depth + 1))
        browser.close()
