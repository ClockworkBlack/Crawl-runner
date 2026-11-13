"""Policing contacts and media tip lines, read from official pages only (Electo's Policing tab).

police     For each seeded jurisdiction (county or city with a website), follow the government's own link to its
           police department, sheriff's office or public-safety department (same site, or another host the home
           page itself links to that names the place), then up to four of that section's own pages (chief /
           command staff, contact, precincts / zones / stations, internal affairs / complaints, records).
           A county keeps its sheriff's office and its county police separately (Cobb, Gwinnett, DeKalb, Chatham...
           have both); a city keeps its police (or marshal). When the home page itself links to no department (a
           menu built by scripts, as on cobbcounty.gov), the site's own department index pages and the sitemaps its
           robots.txt names are read for one. What is kept, each field with the URL it was read on: the
           department's name, the chief or sheriff (name and title as printed), phone numbers with the label printed
           beside them, email addresses on the government's domain, the street address, precincts with their
           addresses and phones, and the complaint and records pages. Nothing is guessed: no pattern-built URLs or
           emails, no inferred chiefs.
roster     Every Georgia law enforcement agency the FBI's Uniform Crime Reporting program lists (the Crime Data
           Explorer API: name, type, counties, ORI), once a week -> state/police_roster.json. It names the
           departments whose sites refuse the crawler or link nowhere useful (Atlanta, Marietta, Cherokee...), every
           county's sheriff's office, and the other agencies in each county (campus police, school-system police,
           marshals, the State Patrol post).
media      For each outlet in config/outlets.yaml, the outlet's own contact / tips page and the news-tip addresses
           printed there (on the outlet's domain only).
records    The open-records contacts the seed found (state/discovered.json `records`), plus config/private/openrecords.yaml
           approvals, in one file for Electo's records-request drafter.

State: state/police.json (one entry per jurisdiction: its first department, any other under `also`), state/media.json
(checked_at per entry; a department re-read after 30 days, a place where none was found after 7, every place when
POLICE_VERSION changes, or with --refresh) and state/police_roster.json.
Export: police.json, media.json, records_contacts.json in the Electo bundle (export.py).
"""
from __future__ import annotations

import datetime as dt
import json
from .registry import write_json_atomic
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import extract

POLICE_LINK = re.compile(r"\b(police|sheriff|law[\s-]*enforcement|public[\s-]*safety|marshal)\b", re.I)
SUB_LINK = re.compile(r"\b(chief|command[\s-]*staff|leadership|administration|about|contact|precincts?|zones?|"
                      r"stations?|districts?|divisions?|internal[\s-]*affairs|professional[\s-]*standards|"
                      r"complaints?|commendation|records)\b", re.I)
SKIP_LINK = re.compile(r"\.(pdf|jpg|jpeg|png|gif|mp4|zip)(\?|$)|facebook|twitter|instagram|youtube|nextdoor|linkedin|"
                       r"calendar|news[\s-]*flash|newsflash|civicalerts|/news/|/blog|press|careers?|jobs?|recruit|donate|login|"
                       r"pay[\s-]*(a[\s-]*)?(ticket|fine)|fire[\s-]*marshal",
                       re.I)
# a link label that is a news headline ("Newnan Police Department Receives Grant...", "Police Department Celebrates
# Retired Asst. Chief") leads to a story, not to the department's own section
HEADLINE_LABEL = re.compile(r"\b(receives?|celebrates?|recogni[sz]es?|announces?|welcomes?|honors?|awarded|launch(es)?|"
                            r"hosts?|joins?|arrests?|seeks?|investigat\w+|reminds?|warns?)\b", re.I)
TITLE_RX = re.compile(r"\b(Chief of Police|Police Chief|Interim (?:Police )?Chief|Acting (?:Police )?Chief|Deputy Chief|"
                      r"Assistant Chief|Sheriff|Chief Deputy|Director of Public Safety|Public Safety Director|"
                      r"Chief Marshal|Marshal|Commander|Major|Captain|Lieutenant)\b", re.I)
NAME_RX = r"((?:[A-Z][A-Za-z'’.\-]+ ){1,3}[A-Z][A-Za-z'’\-]+(?:,? (?:Jr|Sr|II|III|IV)\.?)?)"
NAME_TITLE = re.compile(NAME_RX + r"\s*[,–\-|:]?\s*(?:is (?:the|our) )?" + TITLE_RX.pattern)
TITLE_NAME = re.compile(TITLE_RX.pattern + r"\s*[:–\-|]?\s*" + NAME_RX)
PHONE_RX = re.compile(r"(?<!\d)(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]\d{4}(?!\d)")
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ADDR_RX = re.compile(r"\b\d{1,6}[A-Z]?\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z0-9.'’\-]*(?:\s+[A-Z][A-Za-z0-9.'’\-]*){0,4}\s+"
                     r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Parkway|Pkwy|Highway|Hwy|Lane|Ln|Way|"
                     r"Circle|Cir|Court|Ct|Place|Pl|Trail|Trl|Square|Sq|Plaza|Terrace|Ter)\.?"
                     r"(?:\s+(?:NE|NW|SE|SW|N|S|E|W)\b\.?)?(?:,?\s+(?:Suite|Ste|Unit|Bldg|Building|Room|Rm)\.?\s*#?\s*[\w-]+)?"
                     r",?\s+[A-Z][A-Za-z.'’ \-]{2,30}?,?\s+(?:GA|Georgia)\.?\s+\d{5}(?:-\d{4})?\b")
PRECINCT_HEAD = re.compile(r"\b(precinct|zone|station|district|substation|headquarters|annex)\b", re.I)
LABELS = (("non-emergency", "Non-emergency"), ("nonemergency", "Non-emergency"), ("emergency", "Emergency"),
          ("dispatch", "Dispatch"), ("records", "Records"), ("internal affairs", "Internal Affairs"),
          ("professional standards", "Professional Standards"), ("complaint", "Complaints"), ("tip", "Tips"),
          ("detective", "Detectives"), ("investigat", "Investigations"), ("jail", "Jail"), ("fax", "Fax"),
          ("main", "Main"), ("front desk", "Front desk"), ("administration", "Administration"), ("office", "Office"))
STALE_DAYS = 30
NOT_FOUND_DAYS = 7     # a place where no department was found is tried again sooner (a site that moved, a new menu)
MAX_SUBPAGES = 4
# a link that leads to one of the department's services, not to the department ("File a Police Report", "Obtain a
# Police Report", "Pay a Ticket"): Gwinnett's and Fulton's entries were such pages, with the county's purchasing
# office's phone numbers read as the police department's
NOT_LANDING = re.compile(r"\b(report|reports|file|obtain|request|pay|apply|application|online|permits?|tips?|wanted|offenders?|"
                         r"registry|warrants?|crash|accident|camera|alarm|recruit\w*|careers?|jobs?|hiring|academy|explorers?|"
                         r"volunteer\w*|museum|foundation|auction|calendar|events?|news|press|releases?|faq|forms?|"
                         r"bond|inmate|visitation|commissary|fingerprint\w*|background|clearance|fees?)\b", re.I)
# a menu page that lists the government's departments: read when the home page itself links to none
INDEX_LINK = re.compile(r"\b(departments?|government|public[\s-]*safety|services|directory|agencies|offices|"
                        r"departments?\s*(?:&|and)\s*services|county\s+departments|city\s+departments)\b", re.I)
# a sitemap entry whose address is a department's landing page (cobbcounty.gov/police)
POLICE_PATH = re.compile(r"/(police(?:[-_]department)?|sheriff(?:s|'s)?(?:[-_]office)?|public[-_]safety|law[-_]enforcement|"
                         r"marshals?(?:[-_]office)?)/?$", re.I)
# hosts that serve a department's forms or alerts but are not the department (crash reports, alerts, job boards)
VENDOR_HOST = re.compile(r"lexisnexis|buycrash|crimereports|spotcrime|communitycrimemap|cityprotect|nixle|everbridge|"
                         r"governmentjobs|neogov|municode|civicplus\.com|granicus|paymentus|govpay|nicusa|tylertech|"
                         r"arcgis|google\.|youtube|apple\.com|microsoft|instagram|twitter|x\.com|facebook|linkedin", re.I)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _host(u: str) -> str:
    return urlsplit(u).netloc.lower().removeprefix("www.")


def _base(host: str) -> str:
    p = host.split(".")
    return ".".join(p[-3:] if len(p) > 2 and p[-2] in ("ga", "co", "ci", "org", "com", "net") and p[-1] == "us" else p[-2:])


def _norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p)
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else ""


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


NAME_STOP = re.compile(r"\b(Department|Police|Sheriff|Office|County|City|Contact|About|Welcome|Meet|Our|The|Home|Page|"
                       r"Georgia|Division|Bureau|Precinct|Zone|Services?|Message|From|Fire|Rescue|EMS|Emergency|Management|"
                       r"Animal|Control|Code|Enforcement|Court|Courts|Municipal|Clerk|Mayor|Council|Commissioners?|Board|"
                       r"Unit|Investigations?|Patrol|Training|Records|Support|Operations|Jail|Detention|Warrants?|Civil|"
                       r"Process|Criminal|Community|Public|Safety|Affairs|Internal|Professional|Standards|Administration|"
                       r"Administrative|Staff|Command|Report|Online|Annual|Agency|Center|Headquarters|Uniform|Traffic|"
                       r"Special|Victim|School|Resource|Accreditation|Leadership|Team|Chief|Deputy|Captain|Major|"
                       r"Lieutenant|Sergeant|Officer|Assistant|Interim|Acting|Directory|Employment|Contact|Info)\b", re.I)
SUFFIX_FIRST = re.compile(r"^(Jr|Sr|II|III|IV)\.?$", re.I)


def _name_ok(n: str) -> bool:
    """A person's name as a page prints it: two to five words, none of them a department word ("Jr. Fire" was read as
    Valdosta's chief), not starting with a suffix, every word capitalized, and at least two words longer than an
    initial."""
    w = n.split()
    if not 2 <= len(w) <= 5 or NAME_STOP.search(n) or SUFFIX_FIRST.match(w[0]):
        return False
    if not all(x[0].isupper() for x in w if x[0].isalpha()):
        return False
    full = [x for x in w if len(x.strip(".,'’")) > 1 and not SUFFIX_FIRST.match(x)]
    return len(full) >= 2


def official_email(addr: str, *sites: str) -> bool:
    dom = addr.rsplit("@", 1)[-1].lower()
    for s in sites:
        h = _host(s)
        if h and (dom == h or dom.endswith("." + h) or _base(dom) == _base(h)):
            return True
    return dom.endswith(".gov") or dom.endswith(".ga.us") or dom.endswith(".us")


# ----------------------------------------------------------------------------- parsing one page
def _text_lines(html: str) -> list[str]:
    soup = extract.soup_of(html)
    for t in soup(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    txt = soup.get_text("\n")
    return [_clean(x) for x in txt.split("\n") if _clean(x)]


def find_chief(lines: list[str]) -> dict | None:
    """The chief, sheriff or public-safety director as the page prints them: 'Chief of Police John Doe',
    'John Doe, Chief of Police', or a name on one line and the title on the next."""
    best = None
    rank = {"sheriff": 0, "chief of police": 0, "police chief": 0, "interim": 1, "acting": 1, "director of public safety": 0,
            "public safety director": 0, "chief marshal": 0, "marshal": 2, "chief": 2}

    def score(title: str) -> int:
        t = title.lower()
        for k, v in rank.items():
            if k in t:
                return v
        return 5

    def consider(name: str, title: str):
        nonlocal best
        name, title = _clean(name).strip(",-–: "), _clean(title)
        if not _name_ok(name) or re.search(r"deputy|assistant|commander|major|captain|lieutenant", title, re.I):
            return
        s = score(title)
        if best is None or s < best[0]:
            best = (s, {"name": name, "title": title})

    for i, ln in enumerate(lines):
        if len(ln) > 160:
            continue
        m = NAME_TITLE.search(ln)
        if m:
            consider(m.group(1), m.group(2))
        m = TITLE_NAME.search(ln)
        if m:
            consider(m.group(2), m.group(1))
        # name on one line, title on the next (staff cards)
        if i + 1 < len(lines) and TITLE_RX.fullmatch(lines[i + 1].strip(" :")) and re.fullmatch(NAME_RX, ln):
            consider(ln, lines[i + 1])
        if i + 1 < len(lines) and TITLE_RX.fullmatch(ln.strip(" :")) and re.fullmatch(NAME_RX, lines[i + 1]):
            consider(lines[i + 1], ln)
    return best[1] if best else None


def find_phones(lines: list[str]) -> list[dict]:
    out, seen = [], set()
    for i, ln in enumerate(lines):
        for m in PHONE_RX.finditer(ln):
            n = _norm_phone(m.group(0))
            if not n or n in seen:
                continue
            ctx = ((lines[i - 1] + " " if i else "") + ln[: m.start()]).lower()[-80:]
            hits = [(ctx.rfind(key) + len(key), len(key), lab) for key, lab in LABELS if key in ctx]
            label = max(hits)[2] if hits else ""          # the label printed closest before the number ("non-emergency" over "emergency")
            if not label and i + 1 < len(lines):
                label = next((lab for key, lab in LABELS if key in lines[i + 1].lower()[:40]), "")
            seen.add(n)
            out.append({"label": label or "Phone", "number": n})
    return out[:12]


def find_emails(html: str, *sites: str) -> list[str]:
    soup = extract.soup_of(html)
    mails = [a["href"][7:].split("?")[0] for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")]
    mails += [m.group(0).rstrip(".") for m in EMAIL_RX.finditer(html)]
    out = []
    for m in (x.strip().lower() for x in mails):
        if m and m not in out and official_email(m, *sites) and not re.search(r"@example\.(com|org|net)$|noreply|no-reply|donotreply|sentry|wixpress|\.png$|\.jpg$", m):
            out.append(m)
    return out[:8]


def find_address(lines: list[str]) -> str:
    for ln in lines:
        m = ADDR_RX.search(ln)
        if m:
            return _clean(m.group(0))
    joined = " ".join(lines[:400])
    m = ADDR_RX.search(joined)
    return _clean(m.group(0)) if m else ""


def find_precincts(lines: list[str], page_url: str) -> list[dict]:
    """Headings like 'Zone 3 Precinct' or 'North Precinct' followed within a few lines by an address and a phone."""
    out = []
    for i, ln in enumerate(lines):
        if len(ln) > 80 or not PRECINCT_HEAD.search(ln) or ADDR_RX.search(ln):
            continue
        if re.search(r"\b(voting|polling|election|court|fire)\b", ln, re.I):
            continue
        win = lines[i + 1: i + 8]
        addr = next((_clean(ADDR_RX.search(w).group(0)) for w in win if ADDR_RX.search(w)), "")
        if not addr:
            continue
        phone = next((_norm_phone(PHONE_RX.search(w).group(0)) for w in win if PHONE_RX.search(w)), "")
        name = _clean(ln).strip(":")
        if any(p["name"].lower() == name.lower() for p in out):
            continue
        out.append({"name": name, "address": addr, "phone": phone, "src": page_url})
        if len(out) >= 30:
            break
    return out


def parse_page(html: str, url: str, site: str) -> dict:
    lines = _text_lines(html)
    soup = extract.soup_of(html)
    h1 = soup.find("h1")
    title = _clean(h1.get_text(" ")) if h1 else _clean((soup.title.get_text() if soup.title else "").split("|")[0])
    return {"url": url, "title": title[:120], "chief": find_chief(lines), "phones": find_phones(lines),
            "emails": find_emails(html, site, url), "address": find_address(lines), "precincts": find_precincts(lines, url),
            "lines": lines}


# ----------------------------------------------------------------------------- the department of one jurisdiction
def link_kind(label: str, href: str = "") -> str | None:
    """What kind of department a link leads to, by its text (or, with none, its address): sheriff, marshal or police
    (police, public safety, law enforcement). A fire marshal is none of them."""
    for text in (label or "", urlsplit(href or "").path.replace("-", " ").replace("_", " ")):
        if not text.strip() or re.search(r"fire\s+marshal", text, re.I):
            continue
        if re.search(r"sheriff", text, re.I):
            return "sheriff"
        if re.search(r"\bmarshal", text, re.I):
            return "marshal"
        if re.search(r"\b(police|public\s+safety|law\s+enforcement)\b", text, re.I):
            return "police"
    return None


def _offsite_ok(u: str, label: str) -> bool:
    """A link off the government's own site is followed when it is a public body's address (.gov, .us, .org) or the
    department's own site (cobbsheriff.org, a sheriff's .com), never a vendor's (crash reports, alerts, job boards)."""
    h = _host(u)
    if VENDOR_HOST.search(h):
        return False
    if re.search(r"\.(gov|us|org)$", h):
        return True
    # a department's own .com or .net names itself in the address: gwinnettcountysheriff.com, henrycountyso.com
    return bool(link_kind(label, u) and re.search(r"sheriff|police|marshal|(?:so|pd)\.(?:com|net)$", h, re.I))


def police_links(home_html: str, home_url: str, name: str, limit: int = 8) -> list[tuple[int, str, str]]:
    """Links on a government page to its police / sheriff / public-safety section: (score, url, label), best first.
    The department's own landing page scores above links to one of its services ("File a Police Report")."""
    soup = extract.soup_of(home_html)
    host = _host(home_url)
    out = {}
    for a in soup.find_all("a", href=True):
        label = _clean(a.get_text(" "))
        href = a["href"]
        if not POLICE_LINK.search(label + " " + href) or SKIP_LINK.search(href) or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        if re.search(r"fire\s+marshal", label, re.I) or (len(label.split()) > 6 and HEADLINE_LABEL.search(label)):
            continue
        u = urljoin(home_url, href).split("#")[0]
        if not u or not u.startswith("http"):
            continue
        same = _base(_host(u)) == _base(host)
        if not same and not _offsite_ok(u, label):
            continue
        score = (3 if re.search(r"police|sheriff", label, re.I) else 2 if re.search(r"public safety|marshal", label, re.I) else 1) + (1 if same else 0)
        if re.search(r"\b(department|office|dept)\b", label, re.I):
            score += 1
        if NOT_LANDING.search(label) or NOT_LANDING.search(urlsplit(u).path.rsplit("/", 1)[-1].replace("-", " ")):
            score -= 3
        if POLICE_PATH.search(urlsplit(u).path):
            score += 1
        if u not in out or out[u][0] < score:
            out[u] = (score, u, label)
    return sorted(out.values(), key=lambda x: -x[0])[:limit]


def index_links(html: str, base_url: str, limit: int = 2) -> list[str]:
    """The government's own pages that list its departments ("Departments", "Government", "Public Safety"), on its
    own site: read when the home page links to no department."""
    soup = extract.soup_of(html)
    host = _base(_host(base_url))
    out = []
    for a in soup.find_all("a", href=True):
        label = _clean(a.get_text(" "))
        if not label or len(label) > 40 or not INDEX_LINK.search(label) or SKIP_LINK.search(a["href"]) or link_kind(label):
            continue        # "Police Department" is a department, not a list of them
        u = urljoin(base_url, a["href"]).split("#")[0]
        if u.startswith("http") and _base(_host(u)) == host and u.rstrip("/") != base_url.rstrip("/") and u not in out:
            out.append(u)
    out.sort(key=lambda u: 0 if re.search(r"department|public.safety", u, re.I) else 1)
    return out[:limit]


def sitemap_links(fetcher, home_url: str, max_files: int = 5) -> list[tuple[int, str, str]]:
    """Department landing pages listed in the sitemaps the site's robots.txt names (cobbcounty.gov/police), for a site
    whose menus are built by scripts. Only sitemaps the site itself names are read; none is guessed."""
    from .channels import _sitemap_entries

    try:
        maps = list((getattr(fetcher, "robots", None) and fetcher.robots.sitemaps(home_url)) or [])
    except Exception:  # noqa: BLE001
        return []
    host = _base(_host(home_url))
    out, seen, reads = {}, set(), 0
    queue = [m for m in maps if _base(_host(m)) == host][:3]
    while queue and reads < max_files:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = fetcher.get(sm)
        except Exception:  # noqa: BLE001
            continue
        reads += 1
        if r.status != 200:
            continue
        entries = _sitemap_entries(r.content)
        if entries and all(loc.endswith(".xml") or "sitemap" in loc.lower() for loc, _ in entries[:5]):
            # an index of sitemaps: the ones for departments and plain pages first, never events or news
            kids = [loc for loc, _ in entries if _base(_host(loc)) == host and not re.search(r"event|news|article|post|blog|calendar", loc, re.I)]
            kids.sort(key=lambda loc: 0 if re.search(r"depart|landing|basic|page", loc, re.I) else 1)
            queue = kids[:max_files] + queue
            continue
        for loc, _ in entries:
            if _base(_host(loc)) != host or not POLICE_PATH.search(urlsplit(loc).path):
                continue
            seg = urlsplit(loc).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
            label = seg.title() if seg else "Police"
            out[loc] = (3 + (1 if link_kind(label) in ("police", "sheriff") else 0), loc, label)
        if out:
            break
    return sorted(out.values(), key=lambda x: -x[0])


CHROME = re.compile(r"header|footer|top-?nav|topbar|utility|global|site-?nav|main-?nav|mainnav|navbar|mega-?menu|"
                    r"quick-?links|breadcrumb|skip|social|banner|alert", re.I)


def _content_links(soup) -> list:
    """The page's links outside its site-wide header, footer and main menus: a county site's "Contact Us" and
    "About" in its header are the county's, not the department's (Fulton's purchasing office's phones were read as
    the police department's that way)."""
    chrome = set()
    for el in soup.find_all(["header", "footer"]):
        chrome.update(id(a) for a in el.find_all("a", href=True))
    for el in soup.find_all(True, attrs={"class": True}):
        if CHROME.search(" ".join(el.get("class") or [])):
            chrome.update(id(a) for a in el.find_all("a", href=True))
    for el in soup.find_all(True, attrs={"id": True}):
        if CHROME.search(str(el.get("id") or "")):
            chrome.update(id(a) for a in el.find_all("a", href=True))
    return [a for a in soup.find_all("a", href=True) if id(a) not in chrome]


def wanted_kinds(j: dict) -> list[tuple[str, ...]]:
    """The departments a government has, each a group of kinds any one of which fills it: a county its sheriff's
    office and, in some, a county police department; a consolidated government both; a city or town its police
    department, or a marshal where it has no police (a city's "Sheriff" link is the county's sheriff, read from the
    county's own site)."""
    k = j.get("kind")
    if k == "county":
        return [("sheriff",), ("police",)]
    if k == "consolidated":
        return [("police",), ("sheriff",)]
    return [("police", "marshal")]


def _read_section(fetcher, j: dict, home_url: str, website: str, u: str, label: str, want: str | tuple, direct: bool = False) -> dict | None:
    """One department's landing page and up to four of its own pages, as a department record; None when the page is
    not this government's department of that kind."""
    from .blocked import Blocked
    from .fetch import Disallowed, OutOfTime

    try:
        p = fetcher.get(u)
    except OutOfTime:
        raise
    except (Disallowed, Blocked, Exception):  # noqa: BLE001
        return None
    if p.status != 200 or "html" not in (p.content_type or ""):
        return None
    same = _base(_host(p.url)) == _base(_host(home_url))
    if not same and not direct and place_of(j).split()[0].lower() not in p.text.lower():
        return None        # another host that never names the place: not this government's department
    P = parse_page(p.text, p.url, website)
    head = f"{P['title']} {label}"
    kind = "sheriff" if re.search(r"sheriff", head, re.I) else "marshal" if re.search(r"\bmarshal", head, re.I) and not re.search(r"fire\s+marshal", head, re.I) else "police"
    if not direct and kind not in ((want,) if isinstance(want, str) else want):
        return None
    if re.search(r"fire\s+marshal", head, re.I):
        return None
    dept = {"found": True, "kind": kind, "name": P["title"] or label, "url": p.url, "link_label": label, "v": POLICE_VERSION,
            "chief": dict(P["chief"], src=p.url) if P["chief"] else None,
            "phones": [dict(x, src=p.url) for x in P["phones"]], "emails": [{"address": e, "src": p.url} for e in P["emails"]],
            "address": {"text": P["address"], "src": p.url} if P["address"] else None,
            "precincts": P["precincts"], "pages": {}, "checked_at": _now(), "home": home_url}
    # the section's own pages: chief, contact, precincts, complaints, records. On the government's main site only the
    # links in the page's own content (not its site-wide menus), under the department's address, or naming the
    # department; on the department's own site, any of its pages.
    soup = extract.soup_of(p.text)
    own_site = not same or bool(re.search(r"police|sheriff|marshal", _host(p.url), re.I))
    base_path = urlsplit(p.url).path.rstrip("/")
    content = {id(a) for a in _content_links(soup)}
    subs = {}
    for a in soup.find_all("a", href=True):
        lab = _clean(a.get_text(" "))
        href = a["href"]
        if not SUB_LINK.search(lab + " " + href) or SKIP_LINK.search(href) or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        su = urljoin(p.url, href).split("#")[0]
        if _host(su) != _host(p.url) or su.rstrip("/") == p.url.rstrip("/"):
            continue
        in_section = own_site or (base_path and urlsplit(su).path.startswith(base_path + "/")) or \
            bool(re.search(r"police|sheriff|marshal|precinct|internal affairs|professional standards", lab + " " + urlsplit(su).path, re.I))
        if not in_section and id(a) not in content:
            continue
        key = ("complaints" if re.search(r"internal affairs|professional standards|complaint|commendation", lab + href, re.I)
               else "records" if re.search(r"records", lab + href, re.I)
               else "precincts" if re.search(r"precinct|zone|station|district|substation", lab + href, re.I)
               else "chief" if re.search(r"chief|command|leadership|administration|about", lab + href, re.I)
               else "contact" if re.search(r"contact", lab + href, re.I) else "")
        if key in ("chief", "contact") and not in_section:
            continue        # "About" and "Contact Us" in a county page's body are still the county's
        if key and key not in subs:
            subs[key] = (su, lab)
    for key, (su, lab) in list(subs.items())[:MAX_SUBPAGES]:
        try:
            s = fetcher.get(su)
        except OutOfTime:
            raise
        except (Disallowed, Blocked, Exception):  # noqa: BLE001
            continue
        if s.status != 200 or "html" not in (s.content_type or ""):
            continue
        S = parse_page(s.text, s.url, website)
        dept["pages"][key] = {"url": s.url, "label": lab}
        if S["chief"] and (not dept["chief"] or key == "chief"):
            dept["chief"] = dict(S["chief"], src=s.url)
        for x in S["phones"]:
            if all(y["number"] != x["number"] for y in dept["phones"]):
                dept["phones"].append(dict(x, src=s.url))
        for e in S["emails"]:
            if all(y["address"] != e for y in dept["emails"]):
                dept["emails"].append({"address": e, "src": s.url, "page": key})
        if not dept["address"] and S["address"]:
            dept["address"] = {"text": S["address"], "src": s.url}
        for pr in S["precincts"]:
            if all(q["name"].lower() != pr["name"].lower() for q in dept["precincts"]):
                dept["precincts"].append(pr)
        if key in ("complaints", "records"):
            dept[key] = {"url": s.url, "emails": [e for e in S["emails"]][:3],
                         "phones": [x["number"] for x in S["phones"][:2]]}
    dept["heading"] = dept["name"]
    dept["name"], dept["name_from"] = department_name(j, dept)
    dept["phones"] = dept["phones"][:12]
    dept["emails"] = dept["emails"][:8]
    dept["precincts"] = dept["precincts"][:30]
    return dept


def _read_all(fetcher, j: dict, website: str, log=print, direct: bool = False) -> tuple[str, list[dict]] | None:
    """(the home page's address, every department the government's own site leads to): one of each kind it has
    (wanted_kinds), from its home page's links first; with none of a wanted kind there, its department index pages (one
    level), then the sitemaps its robots.txt names. None when the site itself could not be read; an empty list when
    it was read and leads to no department.
    With `direct`, the site given is the department's own (config/overrides.yaml `police_site:`, set by hand where
    the government's site refuses the crawler or links nowhere useful) and is read as the department's page."""
    from .blocked import Blocked
    from .fetch import Disallowed, OutOfTime

    r = fetcher.get(website)
    if r.status != 200 or "html" not in (r.content_type or ""):
        return None
    if direct:
        d = _read_section(fetcher, j, r.url, website, r.url, f"{place_of(j)} Police", "police", direct=True)
        return r.url, ([d] if d else [])
    want = wanted_kinds(j)
    cands = police_links(r.text, r.url, j["name"])

    def missing(cs):
        """The wanted departments with no landing-page link yet (Gwinnett's home page links only "File Police Report"
        and "Search inmate information"; its Departments page links the Police and Sheriff's Office pages)."""
        return [g for g in want if not any(link_kind(lab, u) in g and s >= 2 for s, u, lab in cs)]

    if missing(cands):
        for u in index_links(r.text, r.url):
            try:
                ix = fetcher.get(u)
            except OutOfTime:
                raise
            except (Disallowed, Blocked, Exception):  # noqa: BLE001
                continue
            if ix.status == 200 and "html" in (ix.content_type or ""):
                cands += [c for c in police_links(ix.text, ix.url, j["name"]) if c[1] not in {x[1] for x in cands}]
            if not missing(cands):
                break
    if missing(cands) == want:
        try:
            cands += [c for c in sitemap_links(fetcher, r.url) if c[1] not in {x[1] for x in cands}]
        except OutOfTime:
            raise
        except Exception:  # noqa: BLE001
            pass
    found, tried = [], set()
    for group in want:
        for _, u, label in sorted((c for c in cands if link_kind(c[2], c[1]) in group), key=lambda c: -c[0])[:3]:
            if u in tried:
                continue
            tried.add(u)
            d = _read_section(fetcher, j, r.url, website, u, label, group)
            if d and all(x["url"] != d["url"] for x in found):
                found.append(d)
                break
    return r.url, found


def read_departments(fetcher, j: dict, website: str, log=print, direct: bool = False) -> list[dict] | None:
    """Every department the government's own site leads to (see _read_all); None when the site could not be read."""
    res = _read_all(fetcher, j, website, log, direct)
    return None if res is None else res[1]


def read_department(fetcher, j: dict, website: str, log=print, direct: bool = False) -> dict | None:
    """The government's first department (read_departments), or a not-found record; None when the site could not be
    read. Others it has are under `also`."""
    res = _read_all(fetcher, j, website, log, direct)
    if res is None:
        return None
    home, r = res
    if not r:
        return {"found": False, "checked_at": _now(), "home": home, "v": POLICE_VERSION}
    first = dict(r[0])
    if r[1:]:
        first["also"] = r[1:]
    return first


DEPT_WORD = re.compile(r"\b(police|sheriff|marshal|public safety)\b", re.I)
NOT_A_NAME = re.compile(r"\b(news|flash|notice|welcome|board of commissioners|reports?|file|obtain\w*|online|request|pay|"
                        r"apply|jail|programs?|services|celebrat\w*|recogni\w*|receives?|grant|honor|loyalty|integrity|"
                        r"courage|homes|mail|fire)\b", re.I)
SMALL_WORDS = re.compile(r"\b(Of|And|The|For)\b")


def is_fire_marshal(d: dict) -> bool:
    """A fire marshal's office is not a police department (an early read took Walker County's for one)."""
    return bool(re.search(r"fire\s+marshal", f"{d.get('heading') or d.get('name') or ''} {d.get('link_label') or ''}", re.I))
POLICE_VERSION = 3   # bump when department discovery changes: every place is read again (3: sheriff and county police
                    # both kept, department index pages and sitemaps, landing pages over service pages)


def place_of(j: dict) -> str:
    """'DeKalb County', 'Atlanta', 'Macon-Bibb County': the government's name without 'City of'."""
    return re.sub(r"^(city|town) of\s+", "", (j.get("name") or "").strip(), flags=re.I)


def department_name(j: dict, d: dict) -> tuple[str, str]:
    """(name, how it was named). The heading of the page read is used when it names the department ("Chatham County
    Police Department"); a bare heading ("Police Department", "Sheriff's Office") gets the place in front; anything
    else ("News Flash", "File Police Report Online", "Notice of Change to Mail Services") gives the standard form
    for the kind of department. The standard form is a label, not a quotation: Electo shows it as such."""
    title = re.sub(r"\s+", " ", (d.get("heading") or d.get("name") or "")).strip(" -–—|,")
    title = re.sub(r"\s*[-–—|,]\s*(city of .*|[a-z ]+, georgia|ga|georgia)$", "", title, flags=re.I).strip()
    kind = d.get("kind") or "police"
    place = place_of(j)
    if kind == "sheriff" and j.get("kind") != "county" and j.get("counties"):
        place = f"{j['counties'][0]} County"      # the sheriff of a consolidated government is the county's (Bibb, not Macon)
    word = re.sub(r"\s+county$", "", place, flags=re.I).split(",")[0].strip()
    label = re.sub(r"\s+", " ", d.get("link_label") or "").strip(" -–—|,")
    for text, how in ((title, "the department's page"), (label, "the link to the department")):
        if text.isupper() and len(text) > 4:
            text = SMALL_WORDS.sub(lambda m: m.group(1).lower(), text.title().replace("'S", "'s"))
        if not text or not DEPT_WORD.search(text) or NOT_A_NAME.search(text) or len(text) > 60:
            continue
        if word.lower() in text.lower():
            return text, how
        if len(text.split()) <= 4:
            t = text + (" Department" if text.lower() == "police" else "")
            t = "Sheriff's Office" if t.lower().replace("’", "'") in ("sheriff", "sheriff office", "sheriff's office") else t
            return f"{place} {t}", f"the place's name and {how}"
    std = {"sheriff": f"{place} Sheriff's Office", "marshal": f"{place} Marshal's Office"}.get(kind, f"{place} Police Department")
    return std, "standard form (the page read gives no name)"


def news_phrase(j: dict, d: dict) -> str:
    """What a news story calls the department: "DeKalb County police", "Atlanta police", "Hall County sheriff".
    Never the bare page heading: "Police Department" matched every police story in the country."""
    kind = d.get("kind") or "police"
    place = place_of(j)
    if kind == "sheriff" and j.get("kind") != "county" and j.get("counties"):
        place = f"{j['counties'][0]} County"
    return f"{place} {'sheriff' if kind == 'sheriff' else 'marshal' if kind == 'marshal' else 'police'}"


def _stale(rec: dict | None, days: int, versioned: bool = True) -> bool:
    """Due for another read: never read, read by an older version of the department reader (found or not: a place
    where the old reader found nothing is read again by the new one), nothing found and NOT_FOUND_DAYS passed, or
    `days` passed. Outlets (versioned=False) carry no version: they were read again on every run while this test
    applied the departments' version to them."""
    if not rec:
        return True
    if versioned and rec.get("v") != POLICE_VERSION:
        return True
    if versioned and not rec.get("found"):
        days = min(days, NOT_FOUND_DAYS)
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(rec["checked_at"])).days >= days
    except (KeyError, ValueError):
        return True


def discover_police(fetcher, jurisdictions: list[dict], state_dir: Path, deadline: float | None = None,
                    log=print, only: str | None = None, refresh: bool = False) -> int:
    """Read each seeded county's and city's police section; largest first; results in state/police.json."""
    from .blocked import Blocked
    from .fetch import Disallowed, OutOfTime

    disc_p = state_dir / "discovered.json"
    disc = json.loads(disc_p.read_text()) if disc_p.exists() else {}
    out_p = state_dir / "police.json"
    out = json.loads(out_p.read_text()) if out_p.exists() else {}
    n = 0
    for j in sorted(jurisdictions, key=lambda j: -(j.get("population") or 0)):
        if j.get("kind") not in ("county", "city", "consolidated", "municipality", "town"):
            continue
        if only and j["id"] != only:
            continue
        site = j.get("police_site") or j.get("website") or (disc.get(j["id"]) or {}).get("website")
        rec = out.get(j["id"]) or {}
        # a department site set by hand (config/overrides.yaml police_site:) is read at once, then on the usual schedule
        new_override = bool(j.get("police_site")) and not rec.get("direct")
        if not site or (not refresh and not new_override and not _stale(rec, STALE_DAYS)):
            continue
        if deadline and time.monotonic() > deadline:
            break
        try:
            d = read_department(fetcher, j, site, log, direct=bool(j.get("police_site")))
        except OutOfTime:
            break
        except (Disallowed, Blocked, Exception) as exc:  # noqa: BLE001
            d = {"found": False, "checked_at": _now(), "error": f"{type(exc).__name__}: {exc}"[:160]}
        if d is None:
            d = {"found": False, "checked_at": _now(), "error": "the site's home page did not answer with a page"}
        if not d.get("found") and d.get("error") and rec.get("found"):
            # the site failed this time (refused, timed out, down): the department read before is kept as it was, and
            # the failure noted; it is read again on its usual schedule
            out[j["id"]] = {**rec, "last_error": d["error"], "error_at": d["checked_at"]}
            n += 1
            continue
        d["v"] = POLICE_VERSION
        if j.get("police_site"):
            d["direct"] = True
        out[j["id"]] = d
        n += 1
        for x in ([d] + list(d.get("also") or [])) if d.get("found") else []:
            log(f"{j['id']}: {x['name']}" + (f" · {x['chief']['title']} {x['chief']['name']}" if x.get("chief") else "")
                + f" · {len(x['phones'])} phones, {len(x['emails'])} emails, {len(x['precincts'])} precincts")
        if n % 10 == 0:
            write_json_atomic(out_p, out, indent=1, ensure_ascii=False)
    write_json_atomic(out_p, out, indent=1, ensure_ascii=False)
    return n


# ----------------------------------------------------------------------------- media tip lines
TIPS_LINK = re.compile(r"\b(news[\s-]*tips?|tips?|contact[\s-]*(us|the newsroom)?|newsroom|submit[\s-]*(a[\s-]*)?(story|tip|news)|"
                       r"send[\s-]*(us[\s-]*)?(a[\s-]*)?(tip|story)|about[\s-]*us)\b", re.I)
TIP_ADDR = re.compile(r"^(tips?|news(tips?|room|desk)?|assignment(desk)?|editor(s|ial)?|investigat\w*|desk|stories|"
                      r"story|press|newsroom|iteam|i-team|contact|info|feedback|letters|digital|web)", re.I)


def read_outlet(fetcher, domain: str) -> dict:
    """The outlet's contact or tips page and the news-tip addresses on it (its own domain only)."""
    from .blocked import Blocked
    from .fetch import Disallowed, OutOfTime

    home = f"https://www.{domain}/" if not domain.count(".") > 1 else f"https://{domain}/"
    try:
        r = fetcher.get(home)
    except OutOfTime:
        raise
    except (Disallowed, Blocked, Exception) as exc:  # noqa: BLE001
        return {"found": False, "checked_at": _now(), "error": f"{type(exc).__name__}: {exc}"[:160]}
    if r.status != 200 or "html" not in r.content_type:
        return {"found": False, "checked_at": _now(), "status": r.status}
    soup = extract.soup_of(r.text)
    name = _clean((soup.title.get_text() if soup.title else domain).split("|")[0].split(" - ")[0])[:80]
    cands = {}
    for a in soup.find_all("a", href=True):
        lab = _clean(a.get_text(" "))
        href = a["href"]
        if not TIPS_LINK.search(lab + " " + href) or href.startswith(("mailto:", "tel:", "#")) or SKIP_LINK.search(href):
            continue
        u = urljoin(r.url, href).split("#")[0]
        if _base(_host(u)) != _base(domain):
            continue
        score = 3 if re.search(r"tip", lab + href, re.I) else 2 if re.search(r"newsroom|contact", lab + href, re.I) else 1
        if u not in cands or cands[u] < score:
            cands[u] = score
    tips, page = [], None
    pages = [r.url] + [u for u, _ in sorted(cands.items(), key=lambda x: -x[1])[:2]]
    for u in pages:
        try:
            p = r if u == r.url else fetcher.get(u)
        except OutOfTime:
            raise
        except (Disallowed, Blocked, Exception):  # noqa: BLE001
            continue
        if p.status != 200 or "html" not in p.content_type:
            continue
        for e in find_emails(p.text, home):
            local = e.split("@")[0]
            if TIP_ADDR.search(local) and all(t["address"] != e for t in tips):
                tips.append({"address": e, "src": p.url, "label": "News tips" if re.search(r"tip", local, re.I) else "Newsroom" if re.search(r"news|desk|assign|editor|story|stories", local, re.I) else "Contact"})
        if tips and page is None:
            page = p.url
        if len(tips) >= 3:
            break
    return {"found": bool(tips), "name": name, "home": r.url, "page": page, "tips": tips[:4], "checked_at": _now()}


def discover_media(fetcher, root: Path, state_dir: Path, deadline: float | None = None, log=print, refresh: bool = False) -> int:
    from .fetch import OutOfTime
    from .registry import load_yaml

    cfg = load_yaml(root / "config" / "outlets.yaml")
    domains = [d for d in (cfg.get("outlets") or []) if isinstance(d, str)]
    out_p = state_dir / "media.json"
    out = json.loads(out_p.read_text()) if out_p.exists() else {}
    n = 0
    for dom in domains:
        if not refresh and not _stale(out.get(dom), STALE_DAYS, versioned=False):
            continue
        if deadline and time.monotonic() > deadline:
            break
        try:
            out[dom] = read_outlet(fetcher, dom)
        except OutOfTime:
            break
        n += 1
        if out[dom].get("found"):
            log(f"{dom}: " + ", ".join(t["address"] for t in out[dom]["tips"]))
    write_json_atomic(out_p, out, indent=1, ensure_ascii=False)
    return n


# ----------------------------------------------------------------------------- the FBI's roster of agencies
ROSTER_URL = "https://api.usa.gov/crime/fbi/cde/agency/byStateAbbr/GA"
ROSTER_SITE = "https://cde.ucr.cjis.gov/"       # the Crime Data Explorer, where a reader can look an agency up
ROSTER_DAYS = 7


def _api_key() -> str:
    """An api.data.gov key: DATA_GOV_API_KEY, or the project's FEC key (api.data.gov issues it, and one key works
    for every API it serves), else DEMO_KEY (a few requests an hour per address: enough for one a week)."""
    import os

    for k in ("DATA_GOV_API_KEY", "FEC_API_KEY"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return "DEMO_KEY"


def _age_days(iso: str | None) -> float:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9


def parse_roster(data: dict) -> list[dict]:
    """The API's answer ({county: [agency, ...]}) as one list, each agency once (by ORI)."""
    out, seen = [], set()
    for county, lst in (data or {}).items():
        for a in lst or []:
            ori = (a.get("ori") or "").strip()
            name = re.sub(r"\s+", " ", a.get("agency_name") or "").strip()
            if not ori or not name or ori in seen:
                continue
            seen.add(ori)
            counties = [c.strip() for c in (a.get("counties") or county or "").split(",") if c.strip()]
            out.append({"ori": ori, "name": name, "type": a.get("agency_type_name") or "", "counties": counties,
                        "lat": a.get("latitude"), "lon": a.get("longitude"), "nibrs": bool(a.get("is_nibrs"))})
    return sorted(out, key=lambda x: x["ori"])


def read_roster(fetcher, state_dir: Path, log=print, refresh: bool = False) -> dict:
    """Every Georgia agency on the FBI's Uniform Crime Reporting roster -> state/police_roster.json, once a week.
    The key travels in a header, never in an address that could be logged."""
    p = state_dir / "police_roster.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    if not refresh and cur.get("agencies") and _age_days(cur.get("as_of")) < ROSTER_DAYS:
        return {"agencies": len(cur["agencies"]), "read": False}
    key = _api_key()
    r = fetcher.get(ROSTER_URL, headers={"X-Api-Key": key}, use_cache=False)
    if r.status in (401, 403):        # a gateway that wants the key as a parameter
        r = fetcher.get(ROSTER_URL, params={"API_KEY": key}, use_cache=False)
    if r.status != 200 or not (r.content or b"").strip().startswith(b"{"):
        log(f"police roster: HTTP {r.status} from the FBI's Crime Data Explorer" + (" (no DATA_GOV_API_KEY or FEC_API_KEY: DEMO_KEY)" if key == "DEMO_KEY" else "")
            + "; the last roster is kept")
        return {"agencies": len(cur.get("agencies") or []), "read": False, "status": r.status}
    agencies = parse_roster(r.json())
    if len(agencies) < 0.8 * len(cur.get("agencies") or []):
        log(f"police roster: only {len(agencies)} agencies this time ({len(cur['agencies'])} before); the last roster is kept")
        return {"agencies": len(cur["agencies"]), "read": False}
    write_json_atomic(p, {"as_of": _now(), "source": ROSTER_URL, "site": ROSTER_SITE,
                          "note": "Every Georgia law enforcement agency the FBI's Uniform Crime Reporting program lists (Crime Data "
                                  "Explorer API): name, type, counties and ORI as the FBI publishes them.",
                          "agencies": agencies}, indent=1, ensure_ascii=False)
    log(f"police roster: {len(agencies)} agencies")
    return {"agencies": len(agencies), "read": True}


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


AGENCY_SUFFIX = re.compile(r"\s+(police\s+department|police\s+dept\.?|police|department\s+of\s+public\s+safety|"
                           r"public\s+safety\s+department|public\s+safety|city\s+marshal'?s?(\s+office)?|marshal'?s?(\s+office)?)$", re.I)


def roster_departments(roster: list[dict], jurisdictions: list[dict]) -> tuple[list[dict], list[dict]]:
    """(departments, others) from the FBI's roster. A county's sheriff's office and county police, and a city's (or a
    consolidated government's) police or marshal, become departments of that jurisdiction; every other agency
    (campus, school-system, transit and airport police, county marshals, State Patrol posts, state agencies) is
    listed by county."""
    counties = {_alnum(re.sub(r"\s+county$", "", j["name"], flags=re.I)): j for j in jurisdictions if j.get("kind") == "county"}
    places = [j for j in jurisdictions if j.get("kind") in ("city", "town", "consolidated", "municipality")]

    def county_of(a):
        for c in a.get("counties") or []:
            j = counties.get(_alnum(re.sub(r"\s+county$", "", c, flags=re.I)))
            if j:
                return j
        return None

    def place_for(core, a):
        k = _alnum(re.sub(r"^(city|town) of\s+", "", core, flags=re.I))
        if not k:
            return None
        cs = {_alnum(c) for c in a.get("counties") or []}
        here = [j for j in places if cs & {_alnum(c) for c in j.get("counties") or []}] or places
        for j in here:
            names = {_alnum(j["name"]), _alnum(place_of(j))}
            if j.get("kind") == "consolidated":
                cw = _alnum(re.sub(r"\s+county$", "", j.get("consolidated_with") or "", flags=re.I))
                names |= {_alnum(j["name"]) + cw, _alnum(j["name"]) + cw + "county", cw + "county" + _alnum(j["name"])}
            if k in names:
                return j
        # a merged city keeps its first town's department name ("McRae Police Department" serves McRae-Helena)
        for j in here:
            if "-" in j["name"] and _alnum(j["name"].split("-")[0]) == k:
                return j
        return None

    depts, others = [], []
    for a in roster:
        name, typ = a["name"], (a.get("type") or "").lower()
        cj = county_of(a)
        cname = re.sub(r"\s+county$", "", cj["name"], flags=re.I) if cj else ((a.get("counties") or [""])[0].title())
        base = {"ori": a["ori"], "name": name, "county": cname, "lat": a.get("lat"), "lon": a.get("lon"), "listed_by": "fbi"}
        if typ == "county" and cj and re.search(r"sheriff", name, re.I):
            depts.append({**base, "jurisdiction": cj["id"], "kind": "sheriff"})
            continue
        if typ == "county" and cj and re.search(r"\bpolice\b", name, re.I):
            depts.append({**base, "jurisdiction": cj["id"], "kind": "police"})
            continue
        if typ == "city":
            core = AGENCY_SUFFIX.sub("", name).strip()
            j = place_for(core, a)
            if j:
                kind = "marshal" if re.search(r"marshal", name, re.I) else "police"
                depts.append({**base, "jurisdiction": j["id"], "kind": kind})
                continue
        kind = ("campus" if "university" in typ or "college" in typ else "state_patrol" if typ == "state police"
                else "state" if "state agency" in typ else "marshal" if re.search(r"\bmarshal", name, re.I)
                else "schools" if re.search(r"board of education|school", name, re.I) else "other")
        if cj:                    # an agency the FBI files under no county ("NOT SPECIFIED") is shown nowhere
            others.append({**base, "kind": kind, "jurisdiction": cj["id"]})
    return depts, others


def department_list(root: Path, jurisdictions: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], dict]:
    """([(jurisdiction, department)], others, counts): every department the governments' own pages gave
    (state/police.json, the first and any under `also`), then each county's and city's departments on the FBI's
    roster that the pages did not give, as listed-only entries (name and ORI, no contact read yet). A sheriff's office
    a consolidated government's site leads to is the county's. Others: the roster's other agencies, by county."""
    from .registry import load_json

    police = load_json(root / "state" / "police.json") or {}
    roster = (load_json(root / "state" / "police_roster.json") or {}).get("agencies") or []
    by_id = {j["id"]: j for j in jurisdictions}
    county_id = {_alnum(re.sub(r"\s+county$", "", j["name"], flags=re.I)): j["id"] for j in jurisdictions if j.get("kind") == "county"}
    got: dict[tuple, dict] = {}
    order: list[tuple] = []
    cands = []
    for jid, d in police.items():
        j = by_id.get(jid)
        if not j or not isinstance(d, dict) or not d.get("found"):
            continue
        for x in [d] + [y for y in (d.get("also") or []) if isinstance(y, dict) and y.get("found")]:
            if is_fire_marshal(x):
                continue
            home = jid
            if x.get("kind") == "sheriff" and j.get("kind") != "county":
                home = county_id.get(_alnum((j.get("counties") or [""])[0])) or jid
            # a county and its consolidated government share one site and both lead to the same pages: the sheriff
            # is the county's, the police the consolidated government's
            mine = (x.get("kind") == "sheriff") == (by_id.get(home, j).get("kind") == "county")
            cands.append((0 if mine else 1, home, x))
    taken: set[str] = set()
    for _, home, x in sorted(cands, key=lambda c: c[0]):
        key = (home, x.get("kind") or "police")
        url = (x.get("url") or "").rstrip("/").lower()
        if key in got or (url and url in taken):
            continue
        taken.add(url)
        got[key] = {k: v for k, v in x.items() if k != "also"}
        order.append(key)
    rdepts, others = roster_departments(roster, jurisdictions)
    n_pages, n_listed = len(order), 0
    for r in rdepts:
        key = (r["jurisdiction"], r["kind"])
        if key in got:
            d = got[key]
            d["ori"] = r["ori"]
            if str(d.get("name_from") or "").startswith("standard form"):
                d["name"], d["name_from"] = r["name"], "the FBI's list of law enforcement agencies"
            continue
        got[key] = {"found": False, "kind": r["kind"], "name": r["name"], "name_from": "the FBI's list of law enforcement agencies",
                    "ori": r["ori"], "listed_by": "fbi", "url": "", "chief": None, "phones": [], "emails": [], "address": None,
                    "precincts": [], "pages": {}, "lat": r.get("lat"), "lon": r.get("lon")}
        order.append(key)
        n_listed += 1
    out = [(by_id[k[0]], got[k]) for k in order if k[0] in by_id]
    return out, others, {"from_pages": n_pages, "listed_only": n_listed, "others": len(others), "roster": len(roster)}


# ----------------------------------------------------------------------------- export
def export_policing(root: Path, out_dir: Path, jurisdictions: list[dict]) -> dict:
    from .registry import load_json, load_yaml

    state = root / "state"
    media = load_json(state / "media.json")
    disc = load_json(state / "discovered.json")
    _p = root / "config" / "private" / "openrecords.yaml"
    orr = load_yaml(_p if _p.exists() else root / "config" / "openrecords.yaml")
    roster_asof = ((load_json(state / "police_roster.json") or {}).get("as_of") or "")
    by_id = {j["id"]: j for j in jurisdictions}
    pairs, others, counts = department_list(root, jurisdictions)
    depts = []
    for j, d in pairs:
        name, name_from = (d["name"], d.get("name_from")) if d.get("v") == POLICE_VERSION or d.get("listed_by") else department_name(j, d)
        depts.append({"jurisdiction": j["id"], "jurisdiction_name": j["name"], "jurisdiction_kind": j.get("kind"),
                      "county": (j.get("counties") or [None])[0] if j.get("kind") != "county" else j["name"].replace(" County", ""),
                      **{k: d.get(k) for k in ("kind", "url", "chief", "phones", "emails", "address", "precincts",
                                              "complaints", "records", "pages", "checked_at", "ori", "listed_by")},
                      "name": name, "name_from": name_from or "the department's page",
                      **({"checked_at": roster_asof} if d.get("listed_by") and roster_asof else {})})
    (out_dir / "police.json").write_text(json.dumps({
        "note": "Police departments, sheriff's offices and public-safety departments as each government's own site "
                "presents them: the chief or sheriff with the title printed, phones with the label beside them, emails "
                "on the government's domain, the street address, precincts with addresses. Every field carries the "
                "page it was read on (src). Nothing is inferred; a missing field was not printed on the pages read. "
                "Departments marked listed_by \"fbi\" are on the FBI's Uniform Crime Reporting roster (name and ORI as the "
                "FBI lists them) and their own pages have not been read yet; `others` are the roster's other agencies "
                "in each county (campus, school-system, transit and airport police, marshals, State Patrol posts).",
        "roster": {"source": ROSTER_SITE, "as_of": roster_asof},
        "departments": depts,
        "others": [{k: o.get(k) for k in ("county", "jurisdiction", "kind", "name", "ori")} for o in others]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    ocfg = load_yaml(root / "config" / "outlets.yaml")
    names = ocfg.get("names") or {}
    listed = [d for d in (ocfg.get("outlets") or []) if isinstance(d, str)]
    outlets = []
    for dom in listed:
        o = media.get(dom) or {}
        if dom not in names and not o.get("found"):
            continue           # wires and outlets without a Georgia desk are not offered as local news
        outlets.append({"domain": dom, "name": names.get(dom) or o.get("name") or dom, "home": o.get("home"), "page": o.get("page"),
                        "tips": o.get("tips") or [], "checked_at": o.get("checked_at")})
    (out_dir / "media.json").write_text(json.dumps({
        "note": "News outlets from config/outlets.yaml with the news-tip and newsroom addresses printed on their own "
                "contact pages (src per address), and where each reports from: statewide, regions (nearest anchor) and "
                "county papers.", "outlets": outlets, "statewide": ocfg.get("statewide") or [],
        "regions": ocfg.get("regions") or {}, "county_outlets": ocfg.get("county_outlets") or {}}, ensure_ascii=False, indent=1), encoding="utf-8")
    contacts = {}
    approved = orr.get("approved") or {}
    for jid, d in disc.items():
        rc = d.get("records") or {}
        if not (rc.get("emails") or rc.get("portal") or approved.get(jid)):
            continue
        j = by_id.get(jid) or {}
        contacts[jid] = {"name": j.get("name", jid), "kind": j.get("kind"), "emails": ([approved[jid]] if approved.get(jid) else []) + [e for e in rc.get("emails") or [] if e != approved.get(jid)],
                         "portal": rc.get("portal"), "page": rc.get("page"), "website": d.get("website")}
    (out_dir / "records_contacts.json").write_text(json.dumps({
        "note": "Open-records contacts found on each government's own site (a clerk or records address on its domain, "
                "or its request portal), for Electo's records-request drafter. Never guessed.",
        "contacts": contacts}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"departments": len(depts), "from_pages": counts["from_pages"], "listed_only": counts["listed_only"],
            "others": counts["others"], "outlets": len(outlets), "records_contacts": len(contacts)}
