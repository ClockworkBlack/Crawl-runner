"""For the places still without a code of ordinances: read each one's own website a little deeper than the seed
does, to find

  contacts  the clerk / open-records address (or request portal) the site publishes, for the open-records
            requests (openrecords.plan uses it when the seed found none; vet() still decides who is asked);
  codes     a code of ordinances (whole, or chapter by chapter), charter, zoning or development code, or a single
            ordinance the site posts as a PDF or Word file, downloaded into imports/ordinances/<id>/ with a
            sources.csv row (the ordinances workflow reads it and sends it to Electo, and the place is then never
            asked for its code). Agendas, minutes, applications, request forms and calendars are left alone, and so
            is a document that names another place (a county site that also carries its seat city's ordinances);
  libraries a link to this place's code on a code library (Municode, eCode360, American Legal, ...), which means the
            code is online: listed in data/openrecords/online_codes.md for the downloader instead of being
            requested. A library link to another place's code (a county site linking its seat city's) is only noted.

A place with no website in the registry gets the one GMA's city directory lists (municipalities only).
Same-site pages only (the site a redirect lands on counts as the same site), robots.txt respected (the crawler's
own fetcher, which keeps each host's pace while several places are read at once), a page budget per place, and
nothing guessed: an address is kept only when it is printed on the place's own site and on its own (or a
government) domain. Results: data/openrecords/found.json, kept in the private repository (people's addresses).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

VERSION = 2   # a place looked at by an older version is looked at again (v2: redirects, chapters, filters)
FOUND = Path("data") / "openrecords" / "found.json"
ONLINE = Path("data") / "openrecords" / "online_codes.md"

FOLLOW = re.compile(r"clerk|records|ordinance|code|charter|zoning|development|planning|government|administration|"
                    r"departments?|contact|staff|directory|documents?|forms|commission|council|board|aldermen|"
                    r"city[\s-]*hall|courthouse|about", re.I)
FOLLOW_FIRST = re.compile(r"clerk|records|ordinance|code|charter|zoning|development", re.I)
CODE_DOC = re.compile(r"code[\s_-]*of[\s_-]*ordinances|ordinances?|charter|zoning|development[\s_-]*(code|ordinance|"
                      r"regulations|standards)|land[\s_-]*(use|development)|unified[\s_-]*development|subdivision|"
                      r"municipal[\s_-]*code|(city|county|town)[\s_-]*code|code[\s_-]*book|codebook|\budc\b|\bldc\b|"
                      r"\bldr\b", re.I)
NOT_CODE = re.compile(r"agenda|minutes|packet|budget|audit|application|permit|\bforms?\b|newsletter|meeting|notice|"
                      r"\bbids?\b|\brfp\b|\brfq\b|calendar|election|ballot|invoice|payroll|\bjobs?\b|employment|"
                      r"request|hearing|checklist|fee[\s_-]*schedule|\bmap\b", re.I)
DOC_EXT = re.compile(r"\.(pdf|docx?)$", re.I)
LIBRARY = re.compile(r"https?://[^\s\"'<>\\)]*(library\.municode\.com|ecode360\.com|codelibrary\.amlegal\.com|"
                     r"codepublishing\.com|franklinlegal\.net|encodeplus\.com|generalcode\.com|"
                     r"municipalcodeonline\.com|codelibrary\.)[^\s\"'<>\\)]*", re.I)
ASSET = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js)([?#&]|$)", re.I)
STRONG = re.compile(r"record|orr|foia|clerk", re.I)
SKIP_HOSTS = re.compile(r"(facebook|twitter|x\.com|instagram|linkedin|youtube|google|apple|cloudinary|gacities|"
                        r"vimeo|flickr|nextdoor|tiktok)\.", re.I)
OTHER_CITY = re.compile(r"\b(?:city|town)[\s_-]+of[\s_-]+([a-z]+)", re.I)
OTHER_COUNTY = re.compile(r"\b([a-z]+)[\s_-]+county\b", re.I)
NOT_A_NAME = {"the", "this", "said", "a", "each", "any", "per", "our", "your", "in", "of", "for", "and", "unincorporated"}
MAX_FILE = 95_000_000   # GitHub refuses files over 100 MB
FULL_CODE = re.compile(r"code[\s_-]*of[\s_-]*ordinances|municipal[\s_-]*code|(city|county|town)[\s_-]*code|"
                       r"code[\s_-]*book|codebook", re.I)
NOT_WHOLE = re.compile(r"amend|suspen|portions?[\s_-]+of|repeal|index", re.I)
CHAPTER = re.compile(r"\b(chapter|article)\b|chapter[\s_-]*\d", re.I)


def is_complete(saved: list[dict], labels: dict[str, str], more: bool) -> bool:
    """Do the saved files add up to the place's code? One that is the whole code (not an amendment or an index), or a
    series of at least five chapters, with nothing left to fetch. Individual ordinances alone are not: the place is
    still asked for its code (and its ordinances still go to Electo)."""
    if more:
        return False
    texts = [f"{labels.get(s['url'], '')} {s['file'].split('/', 1)[-1]}" for s in saved]
    whole = any(FULL_CODE.search(t) and not NOT_WHOLE.search(t) for t in texts)
    return whole or sum(1 for t in texts if CHAPTER.search(t)) >= 5


def _base(host: str) -> str:
    host = host.lower().removeprefix("www.")
    return ".".join(host.split(".")[-3:]) if host.endswith((".ga.us", ".ga.gov")) else ".".join(host.split(".")[-2:])


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _place_word(j: dict) -> str:
    """The distinctive word(s) of a place's name: 'Toombs County' -> 'toombs', 'City of Forsyth' -> 'forsyth'."""
    n = re.sub(r"^(city|town) of\s+", "", j.get("name", "").lower())
    return re.sub(r"\s+(county|city|town)$", "", n).strip()


def names_other_place(label: str, j: dict) -> bool:
    """Does a document's link text / file name name a different place ('City of Millen' on Jenkins County's site)?"""
    mine = _place_word(j)
    mine_parts = set(re.split(r"[\s-]+", mine)) | {mine.replace(" ", "")}
    low = label.lower().replace("_", " ")
    for rx in (OTHER_CITY, OTHER_COUNTY):
        for m in rx.finditer(low):
            w = m.group(1)
            if w not in NOT_A_NAME and w not in mine_parts:
                return True
    return False


def library_is_this_place(url: str, j: dict) -> bool | None:
    """True / False when the library address names a place (Municode /ga/<slug>, <slug>.municipalcodeonline.com,
    American Legal /codes/<slug>, Code Publishing /GA/<Place>); None when it can't tell (eCode360's numbers)."""
    m = (re.search(r"library\.municode\.com/ga/([^/?#]+)", url, re.I)
         or re.search(r"//([^./]+)\.municipalcodeonline\.com", url, re.I)
         or re.search(r"codelibrary\.amlegal\.com/codes/([^/?#]+)", url, re.I)
         or re.search(r"codepublishing\.com/GA/([^/?#]+)", url, re.I))
    if not m:
        return None
    slug = re.sub(r"[^a-z]", "", m.group(1).lower())
    mine = re.sub(r"[^a-z]", "", _place_word(j))
    if not mine:
        return None
    # the forms a library gives a place's name: candler_county, greenecounty, city_of_x, xga ("union" is not "unioncity")
    return slug in {mine, mine + "county", mine + "countyga", "countyof" + mine, "cityof" + mine, "townof" + mine,
                    mine + "ga", mine + "city" if j.get("kind") != "county" else mine}


def _doc_score(label: str) -> int:
    if re.search(r"code[\s_-]*of[\s_-]*ordinances|municipal[\s_-]*code|(city|county|town)[\s_-]*code|code[\s_-]*book", label, re.I):
        return 5
    if re.search(r"unified[\s_-]*development|land[\s_-]*development|development[\s_-]*code|\budc\b|\bldc\b|zoning[\s_-]*(code|ordinance)", label, re.I):
        return 4
    if re.search(r"charter", label, re.I):
        return 3
    if re.search(r"zoning|subdivision|chapter|article", label, re.I):
        return 2
    return 1


def _kind(label: str) -> str:
    if re.search(r"charter", label, re.I):
        return "charter"
    if re.search(r"zoning|development|subdivision|land[\s_-]*use", label, re.I):
        return "zoning"
    if re.search(r"code|chapter", label, re.I):
        return "code"
    return "ordinance"


def gma_website(fetcher, j: dict) -> str | None:
    """The website GMA's city directory lists for a municipality (its page is keyed by the place's Census code)."""
    fips = str(j.get("fips") or "")
    if j.get("kind") == "county" or len(fips) < 5:
        return None
    from . import extract

    slug = re.sub(r"[^a-z0-9]+", "-", j["name"].lower()).strip("-")
    try:
        r = fetcher.get(f"https://www.gacities.com/gma-cities-districts/{slug}/{fips[-5:]}")
    except Exception:  # noqa: BLE001 — blocked, refused or down: no website from here
        return None
    if r.status != 200 or "html" not in r.content_type:
        return None
    for a in extract.soup_of(r.text).find_all("a", href=True):
        href, text = a["href"].strip(), a.get_text(" ", strip=True)
        if not href.startswith("http") or SKIP_HOSTS.search(urlsplit(href).netloc):
            continue
        if re.fullmatch(r"(https?://)?(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+/?", text.lower()) or "website" in text.lower():
            return href
    return None


def look(fetcher, website: str, j: dict | None = None, max_pages: int = 15, deadline: float | None = None) -> dict:
    """Walk a place's own site (same site only, the clerk / records / code pages first) and return what it publishes:
    {"records": {"emails", "portal", "page"} | {}, "docs": [{url, label, score, found_on}], "libraries": [url],
    "other_libraries": [url], "pages": n}. `j` (the place) lets documents and library links that name another
    place be told apart."""
    from . import extract
    from .seed import EMAIL_RX, RECORDS_PORTALS, _official_email

    j = j or {}
    sites = {_base(urlsplit(website).netloc)}
    landed = website
    queue: list[tuple[int, int, str]] = [(0, 0, website)]   # (priority, depth, url); lower priority first
    seen: set[str] = set()
    emails: dict[str, tuple[int, str]] = {}
    docs: dict[str, dict] = {}
    libraries: list[str] = []
    others: list[str] = []
    portal = None
    pages = 0
    while queue and pages < max_pages and not (deadline and time.monotonic() > deadline):
        queue.sort()
        _, depth, u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            r = fetcher.get(u)
        except Exception:  # noqa: BLE001 — refused, blocked, timed out: go on with the rest
            continue
        if r.status != 200 or "html" not in r.content_type:
            continue
        if pages == 0:   # where the front door redirects to is the place's site too (chathamcounty.org -> ...ga.gov)
            landed = r.url
            sites.add(_base(urlsplit(r.url).netloc))
        pages += 1
        html = r.text
        soup = extract.soup_of(html)
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")
        page_hint = 2 if STRONG.search(f"{r.url} {title}") else 0
        mails = [m.group(0).rstrip(".") for m in EMAIL_RX.finditer(soup.get_text(" "))]
        mails += [a["href"][7:].split("?")[0] for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")]
        for m in dict.fromkeys(unquote(x).strip().lower() for x in mails):
            if "@" not in m or not (_official_email(m, website) or _official_email(m, landed)):
                continue
            score = (3 if STRONG.search(m.split("@")[0]) else 0) + page_hint
            if score > emails.get(m, (-1, ""))[0]:
                emails[m] = (score, r.url)
        p = RECORDS_PORTALS.search(html)
        if p and not portal:
            portal = p.group(0)
        for lib in LIBRARY.finditer(html):
            url = re.split(r"&#|&quot;|&amp;quot;", lib.group(0))[0].rstrip("/.,;")
            if ASSET.search(url) or url in libraries or url in others:
                continue
            (others if library_is_this_place(url, j) is False else libraries).append(url)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            full = urljoin(r.url, href).split("#")[0]
            path = unquote(urlsplit(full).path)
            text = a.get_text(" ", strip=True)
            label = f"{text} {path}"
            if DOC_EXT.search(urlsplit(full).path):
                fname = Path(path).name
                if CODE_DOC.search(label) and not NOT_CODE.search(f"{text} {fname}") and full not in docs \
                        and not (j and names_other_place(f"{text} {fname}", j)):
                    docs[full] = {"url": full, "label": (text or fname)[:200], "score": _doc_score(label), "found_on": r.url}
                continue
            if depth < 2 and _base(urlsplit(full).netloc) in sites and full not in seen and FOLLOW.search(label):
                queue.append((0 if FOLLOW_FIRST.search(label) else 1, depth + 1, full))
    ranked = sorted(emails.items(), key=lambda kv: (-kv[1][0], len(kv[0])))
    records = {}
    if ranked or portal:
        records = {"emails": [m for m, _ in ranked[:3]], "portal": portal,
                   "page": ranked[0][1][1] if ranked else landed}
    return {"records": records, "docs": sorted(docs.values(), key=lambda d: -d["score"]), "libraries": libraries[:5],
            "other_libraries": others[:5], "pages": pages, "landed": landed}


def _safe_name(url: str, content_type: str) -> str:
    name = unquote(Path(urlsplit(url).path).name) or "document"
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")[:120] or "document"
    if not DOC_EXT.search(name):
        name += ".docx" if "officedocument" in content_type else ".doc" if "msword" in content_type else ".pdf"
    return name


class _Budget:
    """Bytes left for this run's downloads, shared by the worker threads."""

    def __init__(self, n: int):
        self.left, self._lock = n, threading.Lock()

    def take(self, n: int) -> bool:
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= n
            return True

    @property
    def empty(self) -> bool:
        return self.left <= 0


def save_docs(fetcher, jid: str, docs: list[dict], inbox: Path, max_docs: int = 60, budget: _Budget | None = None,
              log=print, done: set | None = None) -> tuple[list[dict], bool]:
    """Download the best-scoring code documents into inbox/<jid>/ and add a sources.csv row for each, up to
    max_docs a run, skipping the addresses in `done` (saved by an earlier run). Returns (saved, more): more is True
    when documents were left for the next run (the cap, or the run's byte budget)."""
    folder = inbox / jid
    have = {}
    if folder.exists():
        for f in folder.iterdir():
            if f.is_file() and f.name != "sources.csv":
                have[hashlib.sha256(f.read_bytes()).hexdigest()] = f.name
    saved, rows, more = [], [], False
    todo = [d for d in docs if d["url"] not in (done or set())]
    for d in todo[:max_docs]:
        if budget is not None and budget.empty:
            more = True
            break
        try:
            r = fetcher.get(d["url"], kind="document")
        except Exception as exc:  # noqa: BLE001 — too large, refused, timed out
            log(f"{jid}: not downloaded {d['url']} ({type(exc).__name__})")
            continue
        ctype = r.content_type
        if r.status != 200 or not r.content or len(r.content) > MAX_FILE or "html" in ctype:
            continue
        if not (r.content[:4] == b"%PDF" or r.content[:2] == b"PK" or r.content[:4] == b"\xd0\xcf\x11\xe0"):
            continue  # not a PDF or Word file, whatever it was called
        sha = hashlib.sha256(r.content).hexdigest()
        if sha in have:
            continue
        if budget is not None and not budget.take(len(r.content)):
            more = True
            break
        folder.mkdir(parents=True, exist_ok=True)
        name = _safe_name(d["url"], ctype)
        stem, ext, i = Path(name).stem, Path(name).suffix, 2
        while (folder / name).exists():
            name, i = f"{stem}-{i}{ext}", i + 1
        (folder / name).write_bytes(r.content)
        have[sha] = name
        rows.append({"file": name, "url": d["url"], "title": d["label"], "date": "", "kind": _kind(d["label"] + " " + name),
                     "note": f"found on {d['found_on']} by codefinder {_now()[:10]}"})
        saved.append({"file": f"{jid}/{name}", "url": d["url"], "bytes": len(r.content)})
        log(f"{jid}: saved {name} ({len(r.content) // 1000} kB)")
    if len(todo) > max_docs:
        more = True
    if rows:
        src = folder / "sources.csv"
        new = not src.exists()
        with open(src, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["file", "url", "title", "date", "kind", "note"])
            if new:
                w.writeheader()
            w.writerows(rows)
    return saved, more


def load_found(root: Path) -> dict:
    p = root / FOUND
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def due(rec: dict, cutoff: str) -> bool:
    """Look again: never looked at, looked at by an older version, downloads left over, or past the recheck window."""
    return not rec or rec.get("v") != VERSION or bool(rec.get("more")) or (rec.get("checked_at") or "") <= cutoff


def run(root: Path, jurisdictions: list[dict], fetcher, deadline: float | None = None, recheck_days: int = 30,
        max_pages: int = 15, max_docs: int = 60, max_mb: int = 900, only: list[str] | None = None, workers: int = 6,
        log=print) -> dict:
    """Look at every askable place with no downloaded code (largest first), several sites at once."""
    from .openrecords import ASKABLE_KINDS, downloaded_places

    found = load_found(root)
    have = downloaded_places(root)
    inbox = root / "imports" / "ordinances"
    budget = _Budget(max_mb * 1_000_000)
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=recheck_days)).isoformat()
    out = {"looked": 0, "contacts": 0, "docs_saved": 0, "libraries": 0, "no_website": 0, "left": 0}
    todo = [j for j in jurisdictions if j.get("kind") in ASKABLE_KINDS and j["id"] not in have
            and (not only or j["id"] in only) and (only or due(found.get(j["id"], {}), cutoff))]
    todo.sort(key=lambda j: -(j.get("population") or 0))
    lock = threading.Lock()
    if hasattr(fetcher, "set_pool"):
        fetcher.set_pool(max(16, workers * 2))

    def one(j: dict):
        if deadline and time.monotonic() > deadline:
            with lock:
                out["left"] += 1
            return
        rec = found.get(j["id"], {})
        site, source = j.get("website"), "registry"
        if not site:
            site, source = gma_website(fetcher, j), "GMA city directory"
        entry = {"v": VERSION, "checked_at": _now(), "name": j["name"], "website": site,
                 "website_from": source if site else None}
        if not site:
            with lock:
                out["no_website"] += 1
                found[j["id"]] = {**rec, **entry, "records": rec.get("records") or {}}
            return
        res = look(fetcher, site, j, max_pages=max_pages, deadline=deadline)
        done = {s["url"] for s in rec.get("saved") or []}
        saved, more = save_docs(fetcher, j["id"], res["docs"], inbox, max_docs, budget, log, done) if res["docs"] else ([], False)
        labels = {d["url"]: d["label"] for d in res["docs"]} | (rec.get("labels") or {})
        all_saved = (rec.get("saved") or []) + saved
        entry.update({"pages": res["pages"], "landed": res["landed"],
                      "records": res["records"] or rec.get("records") or {}, "libraries": res["libraries"],
                      "other_libraries": res["other_libraries"],
                      "docs": [{k: d[k] for k in ("url", "label")} for d in res["docs"][:80]],
                      "saved": all_saved, "more": more, "complete": is_complete(all_saved, labels, more),
                      "labels": {s["url"]: labels.get(s["url"], "") for s in all_saved}})
        with lock:
            found[j["id"]] = entry
            out["looked"] += 1
            out["contacts"] += bool(res["records"])
            out["docs_saved"] += len(saved)
            out["libraries"] += bool(res["libraries"])
            if out["looked"] % 10 == 0:
                _save(root, found, jurisdictions)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for fut in [pool.submit(one, j) for j in todo]:
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001 — one place's failure must not stop the others
                log(f"codefinder: {type(exc).__name__}: {exc}")
    with lock:
        _save(root, found, jurisdictions)
    return out


def _save(root: Path, found: dict, jurisdictions: list[dict]):
    p = root / FOUND
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(found, indent=1, ensure_ascii=False, sort_keys=True))
    tmp.replace(p)
    pop = {j["id"]: j.get("population") or 0 for j in jurisdictions}
    online = sorted(((jid, f) for jid, f in found.items() if f.get("libraries")), key=lambda x: -pop.get(x[0], 0))
    lines = ["# Codes that are online but not downloaded", "",
             "These places have no code in imports/ordinances/, but their own website links to their code on a code "
             "library. Download them (the Municode downloader, or save the pages) instead of asking the clerk; they "
             "are not sent open-records requests. Rewritten by `gacrawl records find`.", ""]
    lines += [f"- **{f.get('name', jid)}** (`{jid}`): " + ", ".join(f["libraries"][:3]) for jid, f in online]
    (root / ONLINE).write_text("\n".join(lines) + "\n")
