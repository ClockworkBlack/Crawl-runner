"""`gacrawl seed`: find where each jurisdiction's records live.

Runs wherever the crawler has network access (GitHub Actions, your machine).
For every county and municipality in the registry it fills state/discovered.json with:

  website        official site. Sources, in order: Wikidata (one query, keyed by
                 FIPS), the GMA city directory page, the Wikipedia infobox.
  gma            GMA directory URL, council meeting schedule, elected officials,
                 regional commission (cities only)
  portals        meeting portal / code library / official social links found on
                 the website (Legistar, CivicPlus AgendaCenter, Granicus,
                 CivicClerk, BoardDocs, eCode360, American Legal, Municode,
                 Facebook, YouTube)
  feeds          RSS/Atom feeds the website advertises

Every request goes through the normal polite fetcher (robots.txt, spacing,
honest User-Agent). Wikimedia asks exactly that of automated clients.

A directory's listing is a candidate, not the answer: the website a jurisdiction is known by is the first
candidate that answers (hand override, then Wikidata, the GMA entry, Wikipedia, the official page Electo
opened), and a dead `http://` listing is also tried as `https://` with and without `www.`. A host that refuses
the crawler (bot protection) is never worked around; the next candidate is tried and the refusal is recorded
with its reason so `gacrawl seed --report` can list what needs a hand check or an open records request.
"""
from __future__ import annotations

import datetime as dt
import json
from .registry import write_json_atomic
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from . import extract
from .blocked import Blocked
from .fetch import Disallowed, Fetcher
from .registry import find_portals

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKI_API = "https://en.wikipedia.org/w/api.php"
GMA_BASE = "https://www.gacities.com/gma-cities-districts/"
SEED_VERSION = 3        # bump when discovery changes: records from an older seed are due at once, like the readers'
GMA_PARSER = 2          # bump when parse_gma changes: the GMA entry is re-read even inside its 180-day cadence
RETRY_DAYS = 7          # a record with no website or a site error is re-checked this often (hosts come back, overrides get added)
MAX_CANDIDATES = 5      # sites tried per jurisdiction in one pass (directory listings plus scheme/www variants)
NON_OFFICIAL = ("gacities.com", "cloudinary.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
                "youtube.com", "linkedin.com", "wikipedia.org", "google.com", "mailto:", "tel:")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


ARCHIVED = re.compile(r"^https?://(?:web\.)?archive\.org/web/\d+[a-z_]*/(https?://.+)$", re.I)


def _clean_site(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    m = ARCHIVED.match(url)
    if m:
        url = m.group(1)    # an archived copy of the official site (Wikipedia's DeKalb schools entry): the site it archived
    if not re.match(r"https?://", url):
        url = "https://" + url
    s = urlsplit(url)
    if not s.netloc or any(b in url.lower() for b in NON_OFFICIAL):
        return None
    return f"{s.scheme}://{s.netloc}{s.path or '/'}"


# ----------------------------------------------------------------------------- wikidata
def wikidata_websites(fetcher: Fetcher) -> dict[str, str]:
    """{fips: official website} for every Georgia place (P774) and county (P882) with one on Wikidata."""
    query = """SELECT ?fips ?web WHERE {
      { ?item wdt:P774 ?fips . FILTER(STRSTARTS(?fips,"13")) }
      UNION
      { ?item wdt:P882 ?fips . FILTER(STRSTARTS(?fips,"13")) }
      ?item wdt:P856 ?web .
    }"""
    r = fetcher.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                    headers={"Accept": "application/sparql-results+json"}, use_cache=False)
    if r.status != 200:
        raise RuntimeError(f"wikidata HTTP {r.status}")
    out = {}
    for row in r.json()["results"]["bindings"]:
        fips, web = row["fips"]["value"], row["web"]["value"]
        if fips not in out:  # first listed website wins
            out[fips] = web
    return out


# ----------------------------------------------------------------------------- GMA directory
def gma_url(j: dict) -> str | None:
    if j["kind"] == "county" or not j.get("fips") or len(j["fips"]) != 7:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", j["name"].lower().replace("'", "")).strip("-")
    return f"{GMA_BASE}{slug}/{j['fips'][2:]}"


def parse_gma(html: str, base_url: str) -> dict:
    soup = extract.soup_of(html)
    text = soup.get_text("\n", strip=True)
    out: dict = {"gma_url": base_url}
    # The city's site is the link whose visible text is its own address ("www.abbevillega.org"); sponsor
    # and ad links on the same page have other text, so they're never taken for the city's website.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http") or any(b in href.lower() for b in NON_OFFICIAL):
            continue
        label = re.sub(r"^https?://", "", a.get_text(" ", strip=True).lower()).removeprefix("www.").rstrip("/")
        host = urlsplit(href).netloc.lower().removeprefix("www.")
        if label and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(/\S*)?", label) and (label == host or label.split("/")[0] == host):
            out["website"] = _clean_site(href)
            break
    m = re.search(r"Council meets:\s*(.+)", text)
    if m:
        out["council_meets"] = m.group(1).strip()
    m = re.search(r"Regional Commission\n(.+)", text)
    if m:
        out["regional_commission"] = m.group(1).strip()
    m = re.search(r"Year of Incorporation\n(\d{4})", text)
    if m:
        out["incorporated"] = int(m.group(1))
    m = re.search(r"\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b", text)
    if m:
        out["phone"] = m.group(0).strip()
    # the city hall address: a street line followed by "Town,GA31001" (the directory runs the city, state and
    # ZIP together); kept for open records requests that must go by post
    m = re.search(r"^(\d+[^\n]{3,80})\n([A-Z][^\n]*?),\s*GA\s*(\d{5}(?:-\d{4})?)$", text, re.M)
    if m:
        out["address"] = f"{m.group(1).strip()}, {m.group(2).strip()}, GA {m.group(3)}"
    officials = parse_gma_officials(text)
    if officials:
        out["officials"] = officials
    out["gma_parser"] = GMA_PARSER
    return out


OFFICIAL_TITLE = re.compile(r"^(Mayor|Council|Commission|Alderm|Vice|President|Post|Ward|District|Member|Chair)", re.I)


def parse_gma_officials(text: str) -> list[dict]:
    """Elected officials from the directory's text: a title line, then the name.

    The live page puts the first and last name in separate elements (so they come as separate lines), and a
    title can run over two lines ("Councilmember" / "Post 2"). The old parser paired lines two at a time, so it
    kept first names only and dropped every other official (Atlanta: 10 of 19 council members, "Andre" for the
    mayor). Now a title line opens an official and the lines up to the next title are the name."""
    sect = text.split("Elected Officials", 1)[-1].split("Appointed Officials", 1)[0]
    lines = [l.strip() for l in sect.splitlines() if l.strip()]
    officials: list[dict] = []
    cur: dict | None = None
    for line in lines:
        if OFFICIAL_TITLE.match(line):
            if cur and not cur["name"]:
                cur["title"] += " " + line          # a title over two lines
                continue
            cur = {"title": line, "name": []}
            officials.append(cur)
        elif cur is not None and len(cur["name"]) < 3:
            cur["name"].append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", line))
    out = []
    for o in officials:
        name = " ".join(o["name"]).strip()
        if name:
            out.append({"title": o["title"], "name": name})
    return out


# ----------------------------------------------------------------------------- wikipedia infobox
def wikipedia_website(fetcher: Fetcher, wiki_url: str) -> str | None:
    title = wiki_url.rsplit("/wiki/", 1)[-1]
    r = fetcher.get(WIKI_API, params={"action": "parse", "page": title, "prop": "wikitext", "format": "json",
                                      "redirects": 1}, use_cache=False)
    if r.status != 200:
        return None
    wt = r.json().get("parse", {}).get("wikitext", {}).get("*", "")
    m = re.search(r"\|\s*website\s*=\s*(.+)", wt)
    if not m:
        return None
    val = m.group(1)
    m2 = re.search(r"\{\{\s*URL\s*\|\s*([^|}]+)", val, re.I) or re.search(r"\[?(https?://[^\s\]|}]+)", val) \
        or re.search(r"([a-z0-9.\-]+\.(?:gov|org|com|us|net)[^\s|}\]]*)", val, re.I)
    return _clean_site(m2.group(1)) if m2 else None


# ----------------------------------------------------------------------------- website portals
def error_kind(exc_or_status, reason: str = "") -> str:
    """One word for what went wrong with a site, for the records and the gap report:
    unreachable (no such host, nothing listening), blocked (bot protection: never worked around), robots
    (robots.txt refused or disallows), ssl, timeout, http (a non-200 answer), other."""
    if isinstance(exc_or_status, Blocked):
        return "blocked"
    if isinstance(exc_or_status, Disallowed):
        why = (getattr(exc_or_status, "reason", "") or str(exc_or_status)).lower()
        return "unreachable" if "unreachable" in why else "robots"
    if isinstance(exc_or_status, int):
        return "http"
    text = f"{type(exc_or_status).__name__}: {exc_or_status}"
    if "SSLError" in text or "certificate" in text.lower():
        return "ssl"
    if re.search(r"NameResolutionError|Name or service not known|getaddrinfo failed|Connection refused|ECONNREFUSED|"
                 r"No route to host|Network is unreachable|could not be reached", text):
        return "unreachable"
    if "Timeout" in text or "timed out" in text.lower():
        return "timeout"
    return "other"


def inspect_site(fetcher: Fetcher, website: str) -> dict:
    out: dict = {}
    try:
        r = fetcher.get(website)
    except (Disallowed, Blocked) as exc:
        return {"site_error": f"{type(exc).__name__}: {exc}"[:240], "site_error_kind": error_kind(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"site_error": f"{type(exc).__name__}: {exc}"[:240], "site_error_kind": error_kind(exc)}
    if r.status != 200 or "html" not in r.content_type:
        return {"site_error": f"HTTP {r.status} {r.content_type}".strip(), "site_error_kind": "http", "site_status": r.status}
    html = r.text
    out["website"] = _clean_site(r.url) or website  # follow redirects to the canonical host
    out["home_title"], md = extract.html_to_markdown(html, r.url)
    out["home_title"] = (out["home_title"] or "")[:200]
    site_name = re.search(r"""<meta[^>]+property=["']og:site_name["'][^>]+content=["']([^"']{2,120})""", html, re.I)
    out["home_name"] = site_name.group(1) if site_name else ""
    out["home_text"] = (md or "")[:3000]
    out["portals"] = find_portals(html, r.url)
    out["feeds"] = extract.find_feed_links(html, r.url)[:5]
    out["hints"] = extract.find_api_hints(html, r.url)
    # CivicPlus sites: the AgendaCenter usually isn't linked from the home page by that name
    if "agenda_center" not in out["portals"] and ("civicplus" in html.lower() or "/AgendaCenter" in html):
        probe = urljoin(r.url, "/AgendaCenter")
        try:
            pr = fetcher.get(probe)
            if pr.status == 200 and "AgendaCenter" in pr.text:
                out["portals"]["agenda_center"] = probe
                out["feeds"] += [f for f in extract.find_feed_links(pr.text, pr.url) if f not in out["feeds"]][:5]
        except (Disallowed, Blocked):
            pass
    # meeting portals (Legistar, Granicus, CivicClerk, BoardDocs, iQM2...) are usually linked from an
    # "Agendas & Minutes" / "City Council" / "Board of Education" page rather than the home page
    known = {v.rstrip("/").lower() for v in out["portals"].values()}
    pages = [u for u in meeting_pages(html, r.url) if u.rstrip("/").lower() not in known][:MEETING_PAGES]
    for i, page_url in enumerate(pages):
        if i >= 2 and {"legistar", "granicus", "civicclerk", "boarddocs", "iqm2", "escribe", "novusagenda"} & set(out["portals"]):
            break  # the first two council/agendas pages are always read; more only while no structured portal is known
        try:
            pr = fetcher.get(page_url)
        except (Disallowed, Blocked):
            break
        except Exception:  # noqa: BLE001
            continue
        if pr.status == 200 and "html" in pr.content_type:
            for k, v in find_portals(pr.text, pr.url).items():
                out["portals"].setdefault(k, v)
            out["feeds"] += [f for f in extract.find_feed_links(pr.text, pr.url) if f not in out["feeds"]][:3]
            if "meeting_page" not in out:
                out["meeting_page"] = pr.url
    el = find_elections_site(html, r.url)
    if el:
        out["portals"]["elections"] = el
    try:
        rc = find_records_contact(fetcher, html, r.url)
        if rc:
            out["records"] = rc
    except (Disallowed, Blocked):
        pass
    return out


MEETING_PAGES = 4
MEETING_LINK = re.compile(r"\b(agendas?|minutes|meetings?|city\s+council|county\s+commission|board\s+of\s+commissioners|"
                          r"board\s+of\s+education|school\s+board|board\s+meetings?|council\s+meetings?|public\s+meetings?)\b", re.I)


def meeting_pages(home_html: str, home_url: str) -> list[str]:
    """Internal links from the home page that lead to agendas/minutes/council pages, best first."""
    soup = extract.soup_of(home_html)
    host = urlsplit(home_url).netloc.lower().removeprefix("www.")
    scored = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(home_url, a["href"])
        if urlsplit(href).netloc.lower().removeprefix("www.") != host or href.split("#")[0] == home_url.split("#")[0]:
            continue
        hay = f"{text} {urlsplit(href).path}"
        m = MEETING_LINK.search(hay)
        if not m:
            continue
        score = 3 if re.search(r"agenda|minute", hay, re.I) else 2 if re.search(r"council|commission|board", hay, re.I) else 1
        scored.append((-score, len(href), href.split("#")[0]))
    seen, out = set(), []
    for _, _, href in sorted(scored):
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


ELECTIONS_LINK = re.compile(r"\b(elections?|voter\s+registration|board\s+of\s+elections|registrar|elections?\s*(&|and)\s*"
                            r"(voter\s+)?registration|vote\b)", re.I)


def find_elections_site(home_html: str, home_url: str) -> str | None:
    """The elections office page or site linked from the official home page (county boards often have their own
    domain). Only links the page itself carries; official-looking hosts only."""
    soup = extract.soup_of(home_html)
    host = urlsplit(home_url).netloc.lower().removeprefix("www.")
    best = None
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = urljoin(home_url, a["href"]).split("#")[0]
        if not ELECTIONS_LINK.search(f"{label} {a['href']}") or href.lower().startswith("mailto:"):
            continue
        h = urlsplit(href).netloc.lower().removeprefix("www.")
        if h != host and not re.search(r"\.(gov|us|org)$", h):
            continue
        if re.search(r"sos\.ga\.gov|mvp\.sos|facebook|twitter|instagram|youtube", href, re.I):
            continue  # the state's voter page / social links, not the county's office
        score = (2 if re.search(r"elections?", label, re.I) else 1) + (1 if h != host else 0)
        if not best or score > best[0]:
            best = (score, href)
    return best[1] if best else None


RECORDS_LINK = re.compile(r"open[\s-]*records|public[\s-]*records|records[\s-]*request|right[\s-]*to[\s-]*know|"
                          r"\bfoia\b|\bora\b|city[\s-]*clerk|county[\s-]*clerk|clerk[\s-]*of[\s-]*(council|the|commission)|"
                          r"clerk['’]?s[\s-]*office", re.I)
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RECORDS_PORTALS = re.compile(r"https?://[^\s\"'<>]*(govqa\.us|nextrequest\.com|justfoia\.com|mycusthelp\.com|"
                             r"civicplus\.help|openrecords\.)[^\s\"'<>]*", re.I)


def _official_email(addr: str, site: str) -> bool:
    """An address on the jurisdiction's own domain, or any .gov / .ga.us / .us government domain."""
    dom = addr.rsplit("@", 1)[-1].lower()
    host = urlsplit(site).netloc.lower().removeprefix("www.")
    base = ".".join(host.split(".")[-2:])
    return dom == host or dom.endswith("." + base) or dom == base or dom.endswith((".gov", ".ga.us", ".us"))


def find_records_contact(fetcher: Fetcher, home_html: str, home_url: str) -> dict | None:
    """Follow the site's own open-records / clerk link and take the request email or portal it publishes.
    Only addresses printed on an official page (and on an official domain) are kept; nothing is guessed."""
    soup = extract.soup_of(home_html)
    host = urlsplit(home_url).netloc.lower().removeprefix("www.")
    cands = []
    for a in soup.find_all("a", href=True):
        label = f"{a.get_text(' ', strip=True)} {a['href']}"
        if RECORDS_LINK.search(label):
            u = urljoin(home_url, a["href"]).split("#")[0]
            if urlsplit(u).netloc.lower().removeprefix("www.") == host or RECORDS_PORTALS.search(u):
                score = 2 if re.search(r"records", label, re.I) else 1
                cands.append((score, u))
    for _, u in sorted(dict.fromkeys(cands), key=lambda x: -x[0])[:2]:
        if RECORDS_PORTALS.search(u):
            return {"portal": u, "page": home_url}
        r = fetcher.get(u)
        if r.status != 200 or "html" not in r.content_type:
            continue
        page = r.text
        mails = [m.group(0).rstrip(".") for m in EMAIL_RX.finditer(page)]
        mails += [a["href"][7:].split("?")[0] for a in extract.soup_of(page).find_all("a", href=True)
                  if a["href"].lower().startswith("mailto:")]
        mails = [m for m in dict.fromkeys(x.strip().lower() for x in mails) if _official_email(m, home_url)]
        records_first = sorted(mails, key=lambda m: (not re.search(r"record|foia|ora|clerk", m), len(m)))
        portal = RECORDS_PORTALS.search(page)
        if records_first or portal:
            return {"emails": records_first[:3], "portal": portal.group(0) if portal else None, "page": r.url}
    return None


# ----------------------------------------------------------------------------- driver
def _stale(rec: dict | None, max_age_days: float | None) -> bool:
    if not rec:
        return True
    if max_age_days is None:
        return False
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(rec.get("checked_at", ""))
    except ValueError:
        return True
    return age.total_seconds() > max_age_days * 86400


def hand_website(j: dict) -> str | None:
    """The website a person set in config/overrides.yaml, or None. registry.jurisdictions merges the seed's own
    earlier finding into j["website"] too, so that field alone cannot tell an override from a finding."""
    return j.get("website") if "website" in (j.get("overrides") or []) else None


def due(rec: dict | None, j: dict, max_age_days: float | None, refresh: bool = False) -> bool:
    """Is this jurisdiction's record due for another look? Always when there is none, with --refresh, when the
    seed has changed since (SEED_VERSION), or when a hand override names a site the record has not tried.
    Otherwise by age: a record with a working website after max_age_days (None: never); one with no website or a
    site error after RETRY_DAYS at most (hosts come back, listings get fixed, overrides get added)."""
    if refresh or not rec or rec.get("seed_version") != SEED_VERSION or override_pending(rec, j):
        return True
    failed = not rec.get("website") or bool(rec.get("site_error"))
    if failed:
        return _stale(rec, RETRY_DAYS if max_age_days is None else min(max_age_days, RETRY_DAYS))
    return _stale(rec, max_age_days)


def override_pending(rec: dict, j: dict) -> bool:
    """A hand-set website the record has not tried yet (set since the last seed run)."""
    hand = _clean_site(hand_website(j))
    return bool(hand) and hand != rec.get("website") and hand not in {c.get("url") for c in rec.get("website_candidates", [])}


def _site_key(url: str) -> str:
    """One spelling of a site: scheme, host and path (the https and www forms of a dead listing are meant to be
    tried in turn, so they are different keys; a blocked host's other spelling is refused by the block list at
    once, without a request)."""
    s = urlsplit(url.lower())
    return f"{s.scheme}://{s.netloc}{s.path.rstrip('/')}"


def _host_key(url: str) -> str:
    return urlsplit(url.lower()).netloc.removeprefix("www.")


def variants(url: str, kind: str) -> list[str]:
    """Other spellings of a dead listing worth one try each: directories keep `http://` and `www.` forms of
    sites that moved to `https://` or dropped `www.`, and Wikidata sometimes lists a deep page that is gone
    while the site itself is not. Nothing for a host that refused the crawler (blocked, robots): the refusal
    stands, and a different spelling of the same host is the same host."""
    s = urlsplit(url)
    if kind not in ("unreachable", "http", "ssl", "timeout"):
        return []
    host = s.netloc
    other = host[4:] if host.lower().startswith("www.") else "www." + host
    out = []
    if kind in ("http",) and s.path not in ("", "/"):
        out.append(f"{s.scheme}://{host}/")                    # the site's root instead of a gone deep page
    if kind in ("unreachable", "timeout"):
        if s.scheme == "http":
            out.append(f"https://{host}/")
        out.append(f"https://{other}/")
        out.append(f"http://{other}/")
    if kind == "ssl" and s.scheme == "https":
        out.append(f"https://{other}/")                        # the certificate is often for the other name only
    return out


def resolve_site(fetcher: Fetcher, candidates: list[tuple[str, str]], lazy=None, log=None, accept=None) -> dict:
    """Try the candidate sites in order until one answers; `lazy` (a callable giving more (url, source) pairs) is
    asked only when every listed candidate failed. Returns the record fields: website, website_source, and the
    inspection (portals, feeds...) of the site that answered, or the first candidate's error when none did;
    website_candidates lists everything tried with its error; website_alt is the next distinct site, so the
    registry can fall back to it when the chosen host later refuses the crawler.
    `accept(url, inspection)` -> None, or why the site that answered is not this jurisdiction's (a newspaper, another
    district's school): that candidate is passed over like one that failed."""
    tried: list[dict] = []
    seen: set[str] = set()
    queue = [(_clean_site(u), src) for u, src in candidates]
    queue = [(u, src) for u, src in queue if u]
    first_error: dict | None = None
    lazy_done = lazy is None

    def take():
        while queue:
            u, src = queue.pop(0)
            key = _site_key(u)
            if key in seen or u in {t["url"] for t in tried}:
                continue
            return u, src
        return None

    while len(tried) < MAX_CANDIDATES:
        nxt = take()
        if nxt is None and not lazy_done:
            lazy_done = True
            for u, src in lazy() or []:
                cu = _clean_site(u)
                if cu:
                    queue.append((cu, src))
            continue
        if nxt is None:
            break
        url, source = nxt
        seen.add(_site_key(url))
        ins = inspect_site(fetcher, url)
        if not ins.get("site_error") and accept is not None:
            why = accept(ins.get("website") or url, ins)
            if why:
                ins = {"site_error": why, "site_error_kind": "not_this_place"}
        if ins.get("site_error"):
            tried.append({"url": url, "source": source, "error": ins["site_error"], "kind": ins.get("site_error_kind")})
            if first_error is None:
                # a site that answered but is not this place's (a newspaper, another district's school) is never kept
                # as its website, not even as the failed one: the source would crawl it
                first_error = ({"website_rejected": url, "website_source": source, **ins}
                               if ins.get("site_error_kind") == "not_this_place" else {"website": url, "website_source": source, **ins})
            for v in variants(url, ins.get("site_error_kind", "")):
                if _site_key(v) not in seen and v not in {t["url"] for t in tried}:
                    queue.insert(0, (v, f"{source} ({'https' if v.startswith('https') else 'http'} form)"))
            if log:
                log(f"  {url} ({source}): {ins.get('site_error_kind')}: {ins['site_error'][:100]}")
            continue
        tried.append({"url": url, "source": source, "error": None})
        ins.pop("home_text", None)          # read by accept(); not kept in discovered.json
        ins.pop("home_name", None)
        out = {"website": ins.get("website") or url, "website_source": source, **ins, "website_candidates": tried}
        alt = next((u for u, _ in queue if _host_key(u) != _host_key(out["website"])), None)
        if alt:
            out["website_alt"] = alt
        return out
    out = dict(first_error or {})
    out["website_candidates"] = tried
    alt = next((t["url"] for t in tried[1:] if _host_key(t["url"]) != _host_key(out.get("website", ""))), None)
    if alt:
        out["website_alt"] = alt
    return out


def seed(fetcher: Fetcher, jurisdictions: list[dict], state_path: Path, workers: int = 6, only: str | None = None,
         refresh: bool = False, log=print, max_age_days: float | None = None, deadline: float | None = None) -> dict:
    """Discover websites/portals/feeds. Re-checks entries that are due (see due(): age, failures weekly, a changed
    seed, a new override; all with refresh); stops starting new jurisdictions once `deadline` (time.monotonic)
    passes; resumable."""
    import time as _time

    state_path.parent.mkdir(parents=True, exist_ok=True)
    discovered: dict = json.loads(state_path.read_text()) if state_path.exists() else {}
    lock = threading.Lock()

    def save():
        with lock:
            tmp = state_path.with_suffix(".tmp")
            write_json_atomic(tmp, discovered, indent=1, ensure_ascii=False)
            tmp.replace(state_path)

    todo = [j for j in jurisdictions if (not only or j["id"] == only)
            and due(discovered.get(j["id"]), j, max_age_days, refresh)]
    # biggest first: if time runs out, the jurisdictions most people live in are done
    todo.sort(key=lambda j: -(j.get("population") or 0))
    log(f"seeding {len(todo)} of {len(jurisdictions)} jurisdictions")

    wd: dict[str, str] = {}
    try:
        wd = wikidata_websites(fetcher)
        log(f"wikidata: {len(wd)} websites")
    except Exception as exc:  # noqa: BLE001
        log(f"wikidata unavailable ({exc}); falling back to GMA + Wikipedia")

    GMA_KEYS = ("gma_url", "council_meets", "regional_commission", "incorporated", "officials", "phone", "address",
                "gma_error", "gma_parser", "gma_website")

    def one(j: dict):
        if deadline is not None and _time.monotonic() > deadline:
            return j["id"], None
        rec: dict = {"checked_at": _now(), "seed_version": SEED_VERSION, "portals": {}, "feeds": []}
        cands: list[tuple[str, str]] = []
        if hand_website(j):
            cands.append((j["website"], "override"))
        if wd.get(j.get("fips")):
            cands.append((wd[j["fips"]], "wikidata"))
        old = discovered.get(j["id"]) or {}
        gurl = gma_url(j)
        # the GMA directory entry (meeting day, officials, website) changes rarely: re-read it twice a year,
        # unless the parser has changed since it was read
        if gurl and not refresh and old.get("gma_url") and old.get("gma_parser") == GMA_PARSER and not _stale(old, 180):
            rec.update({k: old[k] for k in GMA_KEYS if k in old})
            gurl = None
        if gurl:
            try:
                r = fetcher.get(gurl)
                if r.status == 200:
                    g = parse_gma(r.text, gurl)
                    if g.get("website"):
                        rec["gma_website"] = g["website"]
                    rec.update({k: v for k, v in g.items() if k != "website"})
                else:
                    rec["gma_error"] = f"HTTP {r.status}"
            except (Disallowed, Blocked) as exc:
                rec["gma_error"] = str(exc)[:200]
            except Exception as exc:  # noqa: BLE001
                rec["gma_error"] = f"{type(exc).__name__}: {exc}"[:200]
        if rec.get("gma_website"):
            cands.append((rec["gma_website"], "gma"))
        if j.get("website_hint"):
            cands.append((j["website_hint"], "electo (official page Electo opened)"))

        def more():
            # Wikipedia's infobox: asked only when the directories gave nothing that answers
            if not j.get("wikipedia"):
                return []
            try:
                w = wikipedia_website(fetcher, j["wikipedia"])
            except Exception as exc:  # noqa: BLE001
                rec["wikipedia_error"] = f"{type(exc).__name__}: {exc}"[:200]
                return []
            return [(w, "wikipedia")] if w else []

        res = resolve_site(fetcher, cands, lazy=more)
        rec.update(res)
        if not rec.get("website"):
            rec["website_candidates"] = res.get("website_candidates", [])
            rec["none_listed"] = {"wikidata": j.get("fips") in wd, "gma": bool(rec.get("gma_url")),
                                  "wikipedia": bool(j.get("wikipedia"))}
        with lock:
            discovered[j["id"]] = rec
        return j["id"], rec

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for jid, rec in pool.map(one, todo):
            if rec is None:
                continue  # time budget used up; picked up next run
            done += 1
            site = rec.get("website", "-")
            portals = ",".join(rec.get("portals", {}).keys()) or "-"
            err = f" {rec.get('site_error_kind')}: {rec['site_error'][:80]}" if rec.get("site_error") else ""
            log(f"[{done}/{len(todo)}] {jid}: {site} portals={portals}{err}")
            if done % 25 == 0:
                save()
    save()
    return discovered


# ----------------------------------------------------------------------------- school districts
def wikidata_school_districts(fetcher: Fetcher) -> list[dict]:
    """Wikidata items in Georgia whose English label mentions schools, with their official websites:
    [{label, web, county}] (located in a Georgia county directly, or in a city within one)."""
    query = """SELECT ?item ?label ?web ?countyLabel WHERE {
      ?county wdt:P882 ?fips . FILTER(STRSTARTS(?fips,"13"))
      { ?item wdt:P131 ?county } UNION { ?item wdt:P131 ?city . ?city wdt:P131 ?county }
      ?item wdt:P856 ?web ; rdfs:label ?label . FILTER(LANG(?label)="en")
      FILTER(CONTAINS(LCASE(?label),"school"))
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }"""
    r = fetcher.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                    headers={"Accept": "application/sparql-results+json"}, use_cache=False)
    if r.status != 200:
        raise RuntimeError(f"wikidata HTTP {r.status}")
    out = []
    for row in r.json()["results"]["bindings"]:
        out.append({"label": row["label"]["value"], "web": row["web"]["value"],
                    "county": row.get("countyLabel", {}).get("value", "").removesuffix(" County")})
    return out


def wikipedia_search_website(fetcher: Fetcher, names: list[str]) -> tuple[str | None, str | None]:
    """Search Wikipedia for a district page by its names; (website, page url) from the first page whose
    title names the district."""
    for name in names[:3]:
        try:
            r = fetcher.get(WIKI_API, params={"action": "query", "list": "search", "srsearch": f"{name} Georgia",
                                              "srlimit": 3, "format": "json"}, use_cache=False)
        except Exception:  # noqa: BLE001
            return None, None
        if r.status != 200:
            continue
        for hit in r.json().get("query", {}).get("search", []):
            title = hit.get("title", "")
            place = name.split(" County")[0] + (" County" if " County" in name else "")
            if DISTRICT_TITLE.search(title) and not ONE_SCHOOL.search(title) and _alnum_s(place) in _alnum_s(title):
                url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
                try:
                    web = wikipedia_website(fetcher, url)
                except Exception:  # noqa: BLE001
                    web = None
                if web:
                    return web, url
    return None, None


def _alnum_s(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


DISTRICT_SEED_VERSION = 1   # bump when district discovery changes: every district is looked at again
# a Wikipedia page about a whole district, not one of its schools ("Columbia High School (Decatur, Georgia)" gave
# Columbia County's district the DeKalb high school's site)
DISTRICT_TITLE = re.compile(r"\b(school district|school system|county schools|city schools|public schools|board of education|"
                            r"schools)\b", re.I)
ONE_SCHOOL = re.compile(r"\b(high|middle|elementary|primary|charter|magnet)\s+school\b|\bacademy\b", re.I)
NEWS_DOMAIN = re.compile(r"(news|times|herald|journal|gazette|tribune|ledger|chronicle|observer|messenger|courier|"
                         r"sentinel|telegraph|enterprise)$", re.I)
NEWS_SITES = {"ajc", "patch", "wsbtv", "11alive", "fox5atlanta", "wtoc", "wjcl", "wrdw", "wtvm", "walb", "wmaz", "gpb", "wabe"}


def district_site_ok(d: dict, url: str, ins: dict) -> str | None:
    """None when the site that answered is plausibly this district's own; else why not. The home page must name the
    district (its name or an alias in the title, the site name or the opening text, or its county or city with a
    district word); a site archive, a newspaper's or a TV station's site, or another district's k12.ga.us domain is
    never it."""
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if "archive.org" in host:
        return f"{host} is an archive, not the district's site"
    labels = host.split(".")
    domain = labels[-2] if len(labels) >= 2 else host
    if NEWS_DOMAIN.search(domain) or domain in NEWS_SITES:
        return f"{host} is a news outlet's site, not the district's"
    place = _alnum_s(d.get("city") or d.get("county") or d["name"].split()[0])
    m = re.search(r"([a-z0-9-]+)\.k12\.ga\.us$", host)
    if m and place and place not in _alnum_s(m.group(1)) and _alnum_s(m.group(1)) not in place:
        # columbiahs.dekalb.k12.ga.us: a school in DeKalb's domain, not Columbia County's district
        return f"{host} is under another district's domain"
    hay = _alnum_s(" ".join([ins.get("home_title") or "", ins.get("home_name") or "", (ins.get("home_text") or "")[:1500]]))
    if any(_alnum_s(n) in hay for n in [d["name"]] + list(d.get("aliases") or []) if n):
        return None
    if place and place in hay and re.search(r"schools|schooldistrict|schoolsystem|boardofeducation", hay):
        return None
    return f"the home page does not name {d['name']}"


def seed_school_districts(fetcher: Fetcher, districts: list[dict], state_path: Path, workers: int = 6,
                          only: str | None = None, refresh: bool = False, log=print, max_age_days: float | None = None,
                          deadline: float | None = None) -> dict:
    """Websites and board portals (BoardDocs etc.) for school districts, into the same discovered.json."""
    import time as _time

    state_path.parent.mkdir(parents=True, exist_ok=True)
    discovered: dict = json.loads(state_path.read_text()) if state_path.exists() else {}
    lock = threading.Lock()

    def save():
        with lock:
            tmp = state_path.with_suffix(".tmp")
            write_json_atomic(tmp, discovered, indent=1, ensure_ascii=False)
            tmp.replace(state_path)

    todo = [d for d in districts if (not only or d["id"] == only) and
            (due(discovered.get(d["id"]), d, max_age_days, refresh)
             or (discovered.get(d["id"]) or {}).get("district_seed_version") != DISTRICT_SEED_VERSION)]
    todo.sort(key=lambda d: -(d.get("population") or 0))
    log(f"seeding {len(todo)} of {len(districts)} school districts")
    if not todo:
        return discovered
    wd: list[dict] = []
    try:
        wd = wikidata_school_districts(fetcher)
        log(f"wikidata: {len(wd)} school items")
    except Exception as exc:  # noqa: BLE001
        log(f"wikidata unavailable ({exc}); falling back to Wikipedia search")

    def match(d: dict) -> str | None:
        names = [d["name"]] + list(d.get("aliases") or [])
        keys = {_alnum_s(n) for n in names}
        for row in wd:
            if _alnum_s(row["label"]) in keys and (not d.get("county") or not row["county"] or _alnum_s(row["county"]) == _alnum_s(d["county"])):
                return row["web"]
        # "X County School District" vs "X County Schools": same county, label mentions school and nothing else
        for row in wd:
            if row["county"] and _alnum_s(row["county"]) == _alnum_s(d.get("county", "")) and not d.get("city") \
                    and re.fullmatch(r"[a-z]+countyschool(s|district|system)?", _alnum_s(row["label"])):
                return row["web"]
        return None

    def one(d: dict):
        if deadline is not None and _time.monotonic() > deadline:
            return d["id"], None
        rec: dict = {"checked_at": _now(), "seed_version": SEED_VERSION, "district_seed_version": DISTRICT_SEED_VERSION,
                     "portals": {}, "feeds": [], "kind": "school_district"}
        cands: list[tuple[str, str]] = []
        if hand_website(d):
            cands.append((d["website"], "override"))
        m = match(d)
        if m:
            cands.append((m, "wikidata"))

        def more():
            web, page = wikipedia_search_website(fetcher, [d["name"]] + list(d.get("aliases") or []))
            if web:
                rec["wikipedia"] = page
                return [(web, "wikipedia")]
            return []

        res = resolve_site(fetcher, cands, lazy=more, accept=lambda url, ins: district_site_ok(d, url, ins))
        rec.update(res)
        if not rec.get("website"):
            rec["website_candidates"] = res.get("website_candidates", [])
            rec["none_listed"] = {"wikidata": bool(m), "wikipedia": "wikipedia" not in rec}
        with lock:
            discovered[d["id"]] = rec
        return d["id"], rec

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for did, rec in pool.map(one, todo):
            if rec is None:
                continue
            done += 1
            log(f"[{done}/{len(todo)}] {did}: {rec.get('website', '-')} portals={','.join(rec.get('portals', {}).keys()) or '-'}")
            if done % 25 == 0:
                save()
    save()
    return discovered


# ----------------------------------------------------------------------------- the gap report
def gap_report(discovered: dict, jurisdictions: list[dict]) -> tuple[str, dict]:
    """data/seed_gaps.md: every jurisdiction the seed could not give a working website, by cause, biggest
    first, each with what is still known (meeting day, phone, portals Electo or the directory gave) and a
    ready stanza for config/overrides.yaml. Returns (markdown, counts).

    Three kinds:
      refuses the crawler   bot protection answered the home page or robots.txt. The crawler never works
                            around that; the records come from portals on other hosts, the GMA entry, YouTube
                            or an open records request (the clerk's address is on the GMA entry).
      listed site cannot    every listing (and its https/www spellings) was dead, refused robots or
      be reached            answered an error. Usually the site moved: open the directory's link in a browser,
                            find the live address, add the override.
      no site listed        Wikidata, the GMA directory and Wikipedia list none. Small towns often have none;
                            where one exists it needs a hand check and an override.
    """
    by_id = {j["id"]: j for j in jurisdictions}
    blocked, unreachable, none_listed, other = [], [], [], []
    working = pending = 0
    for jid, rec in discovered.items():
        j = by_id.get(jid)
        if not j:
            continue
        if rec.get("website") and not rec.get("site_error"):
            working += 1
            continue
        if override_pending(rec, j):
            pending += 1          # a hand override set since the last seed run: the next run settles it
            continue
        kind = rec.get("site_error_kind") or _kind_from_text(rec.get("site_error")) or ("none_listed" if not rec.get("website") else "other")
        if kind == "blocked":
            blocked.append((j, rec))
        elif kind in ("unreachable", "robots", "ssl", "timeout", "http"):
            unreachable.append((j, rec))
        elif not rec.get("website"):
            none_listed.append((j, rec))
        else:
            other.append((j, rec))
    for lst in (blocked, unreachable, none_listed, other):
        lst.sort(key=lambda x: -(x[0].get("population") or 0))

    def known(rec: dict) -> str:
        bits = []
        if rec.get("council_meets"):
            bits.append(f"meets {rec['council_meets']}")
        if rec.get("phone"):
            bits.append(rec["phone"])
        if rec.get("address"):
            bits.append(rec["address"])
        p = {k: v for k, v in (rec.get("portals") or {}).items() if v}
        if p:
            bits.append("portals: " + ", ".join(f"{k} {v}" for k, v in sorted(p.items())))
        if rec.get("records", {}).get("emails"):
            bits.append("records: " + ", ".join(rec["records"]["emails"]))
        return "; ".join(bits)

    def tried(rec: dict) -> str:
        c = rec.get("website_candidates") or []
        if not c:
            return rec.get("site_error", "") or ""
        return "; ".join(f"{t['url']} ({t['source']}): {t.get('kind') or ''} {t.get('error') or ''}".strip() for t in c)

    def stanza(j: dict, rec: dict, hint: str) -> str:
        return (f"  {j['id']}:\n    website: {hint}\n    notes: >-\n      {_now()[:10]}: checked by hand; "
                f"(what the site offers: agendas, minutes, video, code)\n")

    lines = [f"# Seed gaps ({_now()[:10]})", "",
             f"{working} jurisdictions have a working website; {pending} have a hand override set since the last "
             f"seed run (settled by the next one). Below: {len(blocked)} refuse the crawler, "
             f"{len(unreachable)} have a listed site that cannot be reached, {len(none_listed)} have no site listed "
             f"anywhere the seed looks, {len(other)} other errors. Each entry ends with a stanza for "
             "`config/overrides.yaml` (jurisdictions:); fill in `website:` after checking the site by hand, "
             "or add `enabled: false` with a note when there is truly nothing to crawl.", "",
             "## Refuses the crawler (bot protection; never worked around)", "",
             "The home page or robots.txt answered with a bot-protection challenge or an access-denied page. "
             "The crawler leaves these hosts alone (unauthorized access, and the project's addresses would be "
             "banned wherever the same protection runs). What still works: portals on other hosts (Legistar, "
             "Granicus, CivicClerk, eCode360, YouTube) found through Electo or hand-checked here; the GMA "
             "directory entry (meeting day, officials, phone, address); and an open records request to the "
             "clerk (`gacrawl openrecords`; the address and phone below are the GMA entry's). Nearly all of these "
             "run on CivicPlus or Granicus govAccess, whose firewalls refuse data-center address ranges rather than "
             "this crawler: a self-hosted runner on an ordinary connection (`bash scripts/selfhosted.sh`, README "
             "'Refused from GitHub's runners') reaches them with the same rules. If the site offers an RSS feed "
             "or an API on another host, put it in `portals:`.", ""]
    for j, rec in blocked:
        lines += [f"### {j['name']} (`{j['id']}`, pop. {j.get('population') or '?'})",
                  f"- site: {rec.get('website')} — {rec.get('site_error', '')}",
                  f"- known: {known(rec) or 'nothing beyond the listing'}",
                  "```yaml", stanza(j, rec, f"{rec.get('website')}   # refuses the crawler; add portals: on other hosts, or enabled: false"), "```", ""]
    lines += ["## Listed site cannot be reached", "",
              "Every listing (Wikidata, GMA, Wikipedia, Electo) and its https/www spellings was dead, refused "
              "robots.txt, or answered an error. Usually the site moved to a new address the directories do not "
              "have yet: open the listing in a browser, follow it to the live site, and put that in `website:`.", ""]
    for j, rec in unreachable:
        lines += [f"### {j['name']} (`{j['id']}`, pop. {j.get('population') or '?'})",
                  f"- tried: {tried(rec)}",
                  f"- known: {known(rec) or 'nothing beyond the listing'}",
                  "```yaml", stanza(j, rec, "https://   # the live address"), "```", ""]
    lines += ["## No site listed anywhere the seed looks", "",
              "Wikidata (by FIPS), the GMA city directory entry and the Wikipedia infobox list no website. Many "
              "towns this size have none; those that do need a hand check. A `website:` override is picked up "
              "by the next seed run. `none_listed` says which directories had an entry at all.", ""]
    for j, rec in none_listed:
        nl = rec.get("none_listed") or {"gma": bool(rec.get("gma_url")), "wikipedia": bool(j.get("wikipedia"))}
        where = ", ".join(k for k, v in nl.items() if v) or "no directory entry"
        lines += [f"### {j['name']} (`{j['id']}`, pop. {j.get('population') or '?'}{', ' + j['county'] + ' County' if j.get('county') else ''})",
                  f"- directories with an entry: {where}",
                  f"- known: {known(rec) or 'nothing'}",
                  "```yaml", stanza(j, rec, "https://   # or enabled: false"), "```", ""]
    if other:
        lines += ["## Other errors", ""]
        for j, rec in other:
            lines += [f"- {j['name']} (`{j['id']}`): {rec.get('site_error', '')}"]
        lines.append("")
    summary = {"blocked": len(blocked), "unreachable": len(unreachable), "none_listed": len(none_listed),
               "other_errors": len(other), "working": working, "pending_next_seed": pending}
    return "\n".join(lines), summary


def _kind_from_text(err: str | None) -> str | None:
    """The kind of an older record's site_error, which carried no kind of its own."""
    if not err:
        return None
    if err.startswith("Blocked"):
        return "blocked"
    if err.startswith("Disallowed"):
        return "unreachable" if "unreachable" in err else "robots"
    if err.startswith("SSLError") or "certificate" in err:
        return "ssl"
    if err.startswith("HTTP"):
        return "http"
    if "Timeout" in err or "timed out" in err:
        return "timeout"
    if re.search(r"NameResolution|ConnectionError|could not be reached", err):
        return "unreachable"
    return None
