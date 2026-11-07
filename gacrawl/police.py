"""Policing contacts and media tip lines, read from official pages only (Electo's Policing tab).

police     For each seeded jurisdiction (county or city with a website), follow the government's own link to its
           police department, sheriff's office or public-safety department (same site, or another host the home
           page itself links to that names the place), then up to four of that section's own pages (chief /
           command staff, contact, precincts / zones / stations, internal affairs / complaints, records).
           What is kept, each field with the URL it was read on: the department's name, the chief or sheriff
           (name and title as printed), phone numbers with the label printed beside them, email addresses on the
           government's domain, the street address, precincts with their addresses and phones, and the complaint
           and records pages. Nothing is guessed: no pattern-built emails, no inferred chiefs.
media      For each outlet in config/outlets.yaml, the outlet's own contact / tips page and the news-tip addresses
           printed there (on the outlet's domain only).
records    The open-records contacts the seed found (state/discovered.json `records`), plus config/private/openrecords.yaml
           approvals, in one file for Electo's records-request drafter.

State: state/police.json and state/media.json (checked_at per entry; re-read after 30 days or with --refresh).
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
MAX_SUBPAGES = 4


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


def _name_ok(n: str) -> bool:
    bad = re.compile(r"\b(Department|Police|Sheriff|Office|County|City|Contact|About|Welcome|Meet|Our|The|Home|Page|"
                     r"Georgia|Division|Bureau|Precinct|Zone|Services?|Message|From)\b")
    return 2 <= len(n.split()) <= 5 and not bad.search(n)


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
def police_links(home_html: str, home_url: str, name: str) -> list[tuple[int, str, str]]:
    """Links on the home page to the police / sheriff / public-safety section: (score, url, label)."""
    soup = extract.soup_of(home_html)
    host = _host(home_url)
    out = {}
    for a in soup.find_all("a", href=True):
        label = _clean(a.get_text(" "))
        href = a["href"]
        if not POLICE_LINK.search(label + " " + href) or SKIP_LINK.search(href) or href.startswith(("mailto:", "tel:", "#")):
            continue
        if re.search(r"fire\s+marshal", label, re.I) or (len(label.split()) > 6 and HEADLINE_LABEL.search(label)):
            continue
        u = urljoin(home_url, href).split("#")[0]
        if not u:
            continue
        same = _base(_host(u)) == _base(host)
        score = (3 if re.search(r"police|sheriff", label, re.I) else 2 if re.search(r"public safety|marshal", label, re.I) else 1) + (1 if same else 0)
        if re.search(r"\b(department|office|dept)\b", label, re.I):
            score += 1
        if not same and not re.search(r"\.(gov|us|org)$", _host(u)):
            continue
        if u not in out or out[u][0] < score:
            out[u] = (score, u, label)
    return sorted(out.values(), key=lambda x: -x[0])[:3]


def read_department(fetcher, j: dict, website: str, log=print, direct: bool = False) -> dict | None:
    """Follow the government's own police link and read that section (at most five requests). With `direct`, the
    site given is the department's own (config/overrides.yaml `police_site:`, set by hand where the government's
    site refuses the crawler or links nowhere useful) and is read as the department's page."""
    from .blocked import Blocked
    from .fetch import Disallowed, OutOfTime

    r = fetcher.get(website)
    if r.status != 200 or "html" not in r.content_type:
        return None
    links = [(9, r.url, f"{place_of(j)} Police")] if direct else police_links(r.text, r.url, j["name"])
    if not links:
        return {"found": False, "checked_at": _now(), "home": r.url}
    dept = None
    for _, u, label in links:
        try:
            p = fetcher.get(u)
        except OutOfTime:
            raise
        except (Disallowed, Blocked, Exception):  # noqa: BLE001
            continue
        if p.status != 200 or "html" not in p.content_type:
            continue
        same = _base(_host(p.url)) == _base(_host(r.url))
        if not same and j["name"].split()[0].lower() not in p.text.lower():
            continue   # another host that never names the place: not this government's department
        P = parse_page(p.text, p.url, website)
        kind = "sheriff" if re.search(r"sheriff", (P["title"] + " " + label), re.I) else "marshal" if re.search(r"marshal", P["title"] + " " + label, re.I) else "police"
        dept = {"found": True, "kind": kind, "name": P["title"] or label, "url": p.url, "link_label": label, "v": POLICE_VERSION,
                "chief": dict(P["chief"], src=p.url) if P["chief"] else None,
                "phones": [dict(x, src=p.url) for x in P["phones"]], "emails": [{"address": e, "src": p.url} for e in P["emails"]],
                "address": {"text": P["address"], "src": p.url} if P["address"] else None,
                "precincts": P["precincts"], "pages": {}, "checked_at": _now(), "home": r.url}
        # the section's own pages: chief, contact, precincts, complaints, records
        soup = extract.soup_of(p.text)
        subs = {}
        for a in soup.find_all("a", href=True):
            lab = _clean(a.get_text(" "))
            href = a["href"]
            if not SUB_LINK.search(lab + " " + href) or SKIP_LINK.search(href) or href.startswith(("mailto:", "tel:", "#")):
                continue
            su = urljoin(p.url, href).split("#")[0]
            if _host(su) != _host(p.url) or su == p.url:
                continue
            key = ("complaints" if re.search(r"internal affairs|professional standards|complaint|commendation", lab + href, re.I)
                   else "records" if re.search(r"records", lab + href, re.I)
                   else "precincts" if re.search(r"precinct|zone|station|district|substation", lab + href, re.I)
                   else "chief" if re.search(r"chief|command|leadership|administration|about", lab + href, re.I)
                   else "contact" if re.search(r"contact", lab + href, re.I) else "")
            if key and key not in subs:
                subs[key] = (su, lab)
        for key, (su, lab) in list(subs.items())[:MAX_SUBPAGES]:
            try:
                s = fetcher.get(su)
            except OutOfTime:
                raise
            except (Disallowed, Blocked, Exception):  # noqa: BLE001
                continue
            if s.status != 200 or "html" not in s.content_type:
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
        break
    if dept is None:
        return {"found": False, "checked_at": _now(), "home": r.url}
    dept["heading"] = dept["name"]
    dept["name"], dept["name_from"] = department_name(j, dept)
    dept["phones"] = dept["phones"][:12]
    dept["emails"] = dept["emails"][:8]
    dept["precincts"] = dept["precincts"][:30]
    return dept


DEPT_WORD = re.compile(r"\b(police|sheriff|marshal|public safety)\b", re.I)
NOT_A_NAME = re.compile(r"\b(news|flash|notice|welcome|board of commissioners|reports?|file|obtain\w*|online|request|pay|"
                        r"apply|jail|programs?|services|celebrat\w*|recogni\w*|receives?|grant|honor|loyalty|integrity|"
                        r"courage|homes|mail|fire)\b", re.I)
SMALL_WORDS = re.compile(r"\b(Of|And|The|For)\b")


def is_fire_marshal(d: dict) -> bool:
    """A fire marshal's office is not a police department (an early read took Walker County's for one)."""
    return bool(re.search(r"fire\s+marshal", f"{d.get('heading') or d.get('name') or ''} {d.get('link_label') or ''}", re.I))
POLICE_VERSION = 2   # bump when department discovery changes: every department is read again


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


def _stale(rec: dict | None, days: int) -> bool:
    if not rec:
        return True
    if rec.get("found") and rec.get("v") != POLICE_VERSION:
        return True
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
        if not site or (not refresh and not _stale(out.get(j["id"]), STALE_DAYS)
                        and not (j.get("police_site") and (out.get(j["id"]) or {}).get("url", "").find(j["police_site"].split("//")[-1].strip("/")) < 0)):
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
            d = {"found": False, "checked_at": _now()}
        out[j["id"]] = d
        n += 1
        if d.get("found"):
            log(f"{j['id']}: {d['name']}" + (f" · {d['chief']['title']} {d['chief']['name']}" if d.get("chief") else "")
                + f" · {len(d['phones'])} phones, {len(d['emails'])} emails, {len(d['precincts'])} precincts")
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
        if not refresh and not _stale(out.get(dom), STALE_DAYS):
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


# ----------------------------------------------------------------------------- export
def export_policing(root: Path, out_dir: Path, jurisdictions: list[dict]) -> dict:
    from .registry import load_json, load_yaml

    state = root / "state"
    police = load_json(state / "police.json")
    media = load_json(state / "media.json")
    disc = load_json(state / "discovered.json")
    _p = root / "config" / "private" / "openrecords.yaml"
    orr = load_yaml(_p if _p.exists() else root / "config" / "openrecords.yaml")
    by_id = {j["id"]: j for j in jurisdictions}
    depts = []
    for jid, d in police.items():
        j = by_id.get(jid)
        if not j or not d.get("found") or is_fire_marshal(d):
            continue
        name, name_from = (d["name"], d.get("name_from")) if d.get("v") == POLICE_VERSION else department_name(j, d)
        depts.append({"jurisdiction": jid, "jurisdiction_name": j["name"], "jurisdiction_kind": j.get("kind"),
                      "county": (j.get("counties") or [None])[0] if j.get("kind") != "county" else j["name"].replace(" County", ""),
                      **{k: d.get(k) for k in ("kind", "url", "chief", "phones", "emails", "address", "precincts",
                                              "complaints", "records", "pages", "checked_at")},
                      "name": name, "name_from": name_from or "the department's page"})
    (out_dir / "police.json").write_text(json.dumps({
        "note": "Police departments, sheriff's offices and public-safety departments as each government's own site "
                "presents them: the chief or sheriff with the title printed, phones with the label beside them, emails "
                "on the government's domain, the street address, precincts with addresses. Every field carries the "
                "page it was read on (src). Nothing is inferred; a missing field was not printed on the pages read.",
        "departments": depts}, ensure_ascii=False, indent=1), encoding="utf-8")
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
    return {"departments": len(depts), "outlets": len(outlets), "records_contacts": len(contacts)}
