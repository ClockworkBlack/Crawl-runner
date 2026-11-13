"""Georgia state boards' and commissions' public meetings (config/agencies.yaml), for Electo's Committee Meetings tab.

Each body posts its meetings on its own page, in its own layout: a table of dates with "agenda" and "minutes" links
(Board of Regents), a date heading followed by files (DBHDD), a list of minutes named by date (Corrections, TCSG),
event pages with the date in the address (georgia.gov sites). `meetings_on(html, url)` reads all of these the same
way: every date on the page, with the files linked beside it; a file's own name or address gives the date when the
text around it does not. A date with no file is kept only where the page lays out a schedule (a table row, or an
event's own address), so a date mentioned in a paragraph is not taken for a meeting.

Records: source `ga-agency:<id>`, kind `meeting`, jurisdiction `ga`, one per body and day, with the meeting's files
(meta.EventAgendaFile, EventMinutesFile, docs) under the same field names the Legistar meetings use. The export
(export_state_meetings) writes them, with the General Assembly's committee meetings, to meetings_state.json.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december"]
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_LONG = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:\s*(?:-|–|&|and)\s*\d{{1,2}})?(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I)
DATE_NUM = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d|\d\d)\b")
DATE_ISO = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
DATE_NOYEAR = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})\b(?!\s*,?\s*20\d\d)", re.I)
YEAR = re.compile(r"\b(20\d\d)\b")
DOC_RX = re.compile(r"\.(pdf|docx?|pptx?|xlsx?)(?:$|\?)", re.I)
KIND_RX = [("minutes", re.compile(r"minutes|summary of actions|action summary|\bsummary\b", re.I)),
           ("agenda", re.compile(r"agenda|meeting notice|public notice|\bnotice\b", re.I))]
MEETING_WORDS = re.compile(r"meeting|board|commission|committee|hearing|session|retreat|agenda|minutes", re.I)


def _mon(s: str) -> int:
    s = s.lower()[:3]
    return next(i + 1 for i, m in enumerate(MONTHS) if m.startswith(s))


def _ok(y: int, m: int, d: int) -> str:
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return ""


def dates_in(text: str, year_hint: int | None = None) -> list[str]:
    """Every date written in the text, as ISO days, in the order written."""
    found: list[tuple[int, str]] = []
    for m in DATE_LONG.finditer(text):
        found.append((m.start(), _ok(int(m.group(3)), _mon(m.group(1)), int(m.group(2)))))
    for m in DATE_ISO.finditer(text):
        found.append((m.start(), _ok(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    for m in DATE_NUM.finditer(text):
        y = int(m.group(3))
        found.append((m.start(), _ok(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))))
    if not found and year_hint:
        for m in DATE_NOYEAR.finditer(text):
            found.append((m.start(), _ok(year_hint, _mon(m.group(1)), int(m.group(2)))))
    return [d for _, d in sorted(found) if d and "2005" <= d[:4] <= str(dt.date.today().year + 3)]


def _kind(label: str, href: str) -> str:
    s = f"{label} {urlsplit(href).path.rsplit('/', 1)[-1]}".replace("_", " ").replace("-", " ").replace("%20", " ")
    for k, rx in KIND_RX:
        if rx.search(s):
            return k
    return "doc"


SCHEDULE_FILE = re.compile(r"meeting dates|meeting schedule|board calendar|meeting calendar", re.I)
# "Tuesday, January 14: Committee Meetings" - a schedule written as lines, the year in the page's heading
SCHEDULE_LINE = re.compile(rf"^(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
                           rf"(?:,?\s+(20\d\d))?\s*[:–—-]\s*(.{{4,160}})$", re.I)
TRAILING_DATE = re.compile(r"\s*[-–,]?\s*\(?\b\d{1,2}[/.-]\d{1,2}[/.-](?:20)?\d\d\)?\s*$")


def _data_html(html: str) -> list[str]:
    """The pieces of a page that sit in its own data script, not in its markup (a Next.js site ships each page's
    content as HTML inside the __NEXT_DATA__ script and draws it in the browser: the State Board of Education)."""
    out, seen, size = [], set(), 0
    for m in re.finditer(r"(?is)<script\b[^>]*(?:__NEXT_DATA__|application/json)[^>]*>(.*?)</script>", html):
        try:
            stack = [json.loads(m.group(1))]
        except ValueError:
            continue
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
            elif isinstance(v, str) and len(v) > 60 and v not in seen and re.search(r"<(?:p|h[1-6]|li|table)\b", v) and size < 600_000:
                seen.add(v)
                size += len(v)
                out.append(v)
    return out


def schedule_files(html: str, url: str) -> list[dict]:
    """[{year, t, u}] - the files a page links that are a year's meeting dates ("2026 Board Meeting Dates", a PDF)."""
    from bs4 import BeautifulSoup

    out = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = urljoin(url, a["href"]).split("#")[0]
        y = YEAR.search(label)
        if y and SCHEDULE_FILE.search(label) and (DOC_RX.search(href) or "/download" in href) and all(x["u"] != href for x in out):
            out.append({"year": int(y.group(1)), "t": re.sub(r"\s+", " ", label)[:90], "u": href})
    return out


def meetings_on(html: str, url: str, event_links: str = "") -> list[dict]:
    """[{date, what, agenda, minutes, docs: [{t, u}], where}] for every meeting the page lists, newest first.
    `event_links`: a part of the address of the site's own pages for single sessions, each named with its date in the
    link ("Administrative Session 11-17-2026"); such a link is a meeting on that day."""
    from bs4 import BeautifulSoup

    html += "".join(f'<section data-shipped="1">{x}</section>' for x in _data_html(html))
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        t.decompose()
    out: dict[str, dict] = {}

    def meet(d: str) -> dict:
        return out.setdefault(d, {"date": d, "what": "", "agenda": "", "minutes": "", "docs": [], "where": ""})

    def add_what(m: dict, t: str) -> None:
        t = re.sub(r"\s+", " ", t).strip(" *:-–")
        if t and t.lower() not in m["what"].lower() and len(m["what"]) + len(t) < 220:
            m["what"] = f"{m['what']}; {t}" if m["what"] else t

    def add_doc(d: str, label: str, href: str) -> None:
        m, k = meet(d), _kind(label, href)
        if k in ("agenda", "minutes") and not m[k]:
            m[k] = href
        elif href not in (m["agenda"], m["minutes"]) and all(x["u"] != href for x in m["docs"]) and len(m["docs"]) < 12:
            m["docs"].append({"t": re.sub(r"\s+", " ", label).strip()[:90] or k, "u": href})

    # the year a part of the page is about: its nearest heading above that names one ("2026 Meeting Minutes")
    year_at: dict[int, int] = {}
    cur = None
    ym = YEAR.search(soup.title.get_text(" ") if soup.title else "") or YEAR.search(urlsplit(url).path)
    cur = page_year = int(ym.group(1)) if ym else None
    for el in soup.find_all(True):
        if el.get("data-shipped"):               # content from the page's data script: the page's own year again
            cur = page_year
        if el.name in ("h1", "h2", "h3", "h4", "h5", "strong", "summary", "button", "caption"):
            y = YEAR.search(el.get_text(" ", strip=True)[:80])
            if y:
                cur = int(y.group(1))
        year_at[id(el)] = cur

    def row_of(a):
        """The smallest block around a link that is one entry of a list: a table row, a list item, a paragraph."""
        for p in a.parents:
            if p.name in ("tr", "li", "p", "dd", "article") or (p.name == "div" and len(p.get_text(" ", strip=True)) < 400):
                return p
        return a.parent

    def heading_date(el) -> str:
        """The date heading a block sits under (DBHDD: 'Thursday, February 26, 2026', then its files)."""
        for prev in el.find_all_previous(["h2", "h3", "h4", "h5", "strong", "b", "dt", "summary"], limit=4):
            ds = dates_in(prev.get_text(" ", strip=True)[:120], year_at.get(id(prev)))
            if ds:
                return ds[0]
        return ""

    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if not href.startswith("http") or re.search(r"unpublished|do-not-share", href, re.I):
            continue                              # not a link, or a file whose own address says not to pass it on
        label = a.get_text(" ", strip=True)
        name = urlsplit(href).path.rsplit("/", 1)[-1]
        is_doc = bool(DOC_RX.search(href)) or bool(re.search(r"agenda|minutes", label, re.I) and "/download" in href)
        ev = re.search(r"/events?/(20\d\d-\d{2}-\d{2})/", href)
        if ev:                                    # an event's own page: the day is in its address
            if MEETING_WORDS.search(label + " " + name):
                m = meet(ev.group(1))
                if all(x["u"] != href for x in m["docs"]) and len(m["docs"]) < 12:
                    m["docs"].append({"t": label[:90] or "Meeting page", "u": href})
            continue
        if event_links and event_links in href and not is_doc:
            ds = dates_in(label)
            what = TRAILING_DATE.sub("", label)
            if ds and what and what != label:
                m = meet(ds[0])
                add_what(m, what)
                if all(x["u"] != href.split("?")[0] for x in m["docs"]) and len(m["docs"]) < 12:
                    m["docs"].append({"t": what[:90], "u": href.split("?")[0]})
            continue
        if not is_doc:
            continue
        yh = year_at.get(id(a))
        row = row_of(a)
        d = (dates_in(label, yh) or dates_in(row.get_text(" ", strip=True)[:300], yh) or
             dates_in(name.replace("_", " ").replace("-", " ").replace("%20", " ").replace("2C", ","), yh) or [""])[0]
        if not d:
            d = heading_date(a)
        if d:
            add_doc(d, label, href)

    # a schedule laid out as a table: a row that opens with a date is a meeting, with or without files
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        ds = dates_in(cells[0].get_text(" ", strip=True)[:80], year_at.get(id(tr)))
        if ds:
            m = meet(ds[0])
            where = cells[1].get_text(" ", strip=True)
            if not where or dates_in(where) or re.search(r"agenda|minutes", where, re.I):
                continue                           # a second date (a two-day meeting) or the files' column
            if re.search(r"meeting|session|hearing|retreat|workshop", where, re.I) and len(where) < 90:
                add_what(m, where)                 # "Committee Meeting", "Board Meeting"
            elif not m["where"]:
                m["where"] = where[:140]

    # a schedule written as lines of text under a heading that names the year (State Board of Education)
    for el in soup.find_all(["p", "li"]):
        if el.find("a"):
            continue
        for line in el.get_text("\n", strip=True).split("\n"):      # two days of one month share a paragraph
            sm = SCHEDULE_LINE.match(re.sub(r"\s+", " ", line).strip())
            if not sm or not MEETING_WORDS.search(sm.group(4)):
                continue
            y = int(sm.group(3)) if sm.group(3) else year_at.get(id(el))
            d = _ok(y, _mon(sm.group(1)), int(sm.group(2))) if y else ""
            if d:
                add_what(meet(d), sm.group(4))
    return sorted(out.values(), key=lambda m: m["date"], reverse=True)


def load(root: Path) -> list[dict]:
    p = Path(root) / "config" / "agencies.yaml"
    return (yaml.safe_load(p.read_text()) or {}).get("agencies") or [] if p.exists() else []


def open_minutes(fetcher, store, ag: dict, found: dict, deadline: float | None, cap: int, log=print) -> int:
    """Open a board's minutes files not opened yet, newest first (at most `cap`), and keep each as a document
    (doc_type "minutes") so the minutes reader (gacrawl/meeting_summaries.py) can say what the meeting decided."""
    from .ingest import pdf_to_record

    n = 0
    for d in sorted((d for d, m in found.items() if m.get("minutes")), reverse=True):
        if n >= cap or (deadline is not None and time.monotonic() > deadline - 30):
            break
        u = found[d]["minutes"]
        if store.url_seen(u):                     # opened before (or refused before): once is enough for a posted file
            continue
        try:
            r = fetcher.get(u, use_cache=False)
        except Exception as exc:  # noqa: BLE001 - a file that refuses is left; the meeting keeps its link
            store.mark_url(u, 0, f"ga-agency:{ag['id']}")
            log(f"agencies: {ag['id']} minutes {d}: {type(exc).__name__}")
            continue
        store.mark_url(u, r.status, f"ga-agency:{ag['id']}", r.content_type)
        if r.status != 200 or r.content[:5] != b"%PDF-":
            continue
        try:
            rec = pdf_to_record(r.content, source_id=f"ga-agency:{ag['id']}", kind="document", url=u, title=f"{ag['name']} minutes, {d}",
                                meta={"doc_type": "minutes", "opened": True, "agency": ag["id"], "found_on": found[d].get("page") or ""},
                                ocr_max_pages=0)
        except Exception as exc:  # noqa: BLE001
            log(f"agencies: {ag['id']} minutes {d}: unreadable ({type(exc).__name__})")
            continue
        rec.published, rec.jurisdiction, rec.evidence = d, "ga", "primary"
        rec.key = f"ga-agency:{ag['id']}|minutes|{u}"
        store.add(rec)
        n += 1
    return n


def run(fetcher, store, root, deadline: float | None = None, only: list[str] | None = None, dry_run: bool = False,
        log=print, minutes_cap: int = 6) -> dict:
    """Each body's pages (and the pages they link, one level, where `follow` says so) read into meeting records."""
    from .store import Record

    out = {"agencies": 0, "pages": 0, "meetings": 0, "new": 0, "by": {}, "errors": []}
    for ag in load(root):
        if only and ag["id"] not in only:
            continue
        if deadline is not None and time.monotonic() > deadline:
            break
        pages, seen, found = list(ag.get("pages") or []), set(), {}
        follow = [f.lower() for f in ag.get("follow") or []]
        depth = {p: 0 for p in pages}
        while pages and len(seen) < 14:
            u = pages.pop(0)
            if u in seen:
                continue
            seen.add(u)
            try:
                r = fetcher.get(u, use_cache=False)
            except Exception as exc:  # noqa: BLE001 — a page that refuses is left for the next run
                out["errors"].append(f"{ag['id']} {u}: {type(exc).__name__}"[:160])
                continue
            if r.status != 200 or "html" not in (r.content_type or "html"):
                out["errors"].append(f"{ag['id']} {u}: HTTP {r.status}"[:160])
                continue
            out["pages"] += 1
            got = meetings_on(r.text, r.url, ag.get("event_links") or "")
            # a year's dates kept in a file: this year's and next year's are opened and read (two files at most)
            this_year = dt.date.today().year
            for sf in [x for x in schedule_files(r.text, r.url) if x["year"] in (this_year, this_year + 1)][:2]:
                try:
                    fr = fetcher.get(sf["u"], use_cache=False)
                    if fr.status != 200 or fr.content[:5] != b"%PDF-":
                        raise ValueError(f"HTTP {fr.status}, not a PDF")
                    import io

                    import pdfplumber

                    with pdfplumber.open(io.BytesIO(fr.content)) as pdf:
                        text = "\n".join(pg.extract_text() or "" for pg in pdf.pages[:6])
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"{ag['id']} {sf['u']}: {type(exc).__name__}: {exc}"[:200])
                    continue
                days = {d for d in dates_in(text)} | {d for ln in text.splitlines() for d in dates_in(ln, sf["year"])}
                for d in sorted(x for x in days if x[:4] == str(sf["year"])):
                    got.append({"date": d, "what": "", "agenda": "", "minutes": "", "docs": [{"t": sf["t"], "u": sf["u"]}], "where": ""})
            for m in got:
                old = found.get(m["date"])
                if not old:
                    found[m["date"]] = {**m, "page": r.url}
                else:                             # the same day on two pages (the schedule, then the minutes)
                    old["agenda"] = old["agenda"] or m["agenda"]
                    old["minutes"] = old["minutes"] or m["minutes"]
                    old["where"] = old["where"] or m["where"]
                    old["what"] = old["what"] or m["what"]
                    old["docs"] += [x for x in m["docs"] if all(x["u"] != y["u"] for y in old["docs"])][:12 - len(old["docs"])]
            if follow and depth.get(u, 0) == 0:
                from bs4 import BeautifulSoup

                host = urlsplit(r.url).netloc
                for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
                    h = urljoin(r.url, a["href"]).split("#")[0]
                    if urlsplit(h).netloc == host and h not in seen and h not in pages and not DOC_RX.search(h) and \
                            any(f in h.lower() or f in a.get_text(" ", strip=True).lower() for f in follow):
                        pages.append(h)
                        depth[h] = 1
        out["agencies"] += 1
        out["by"][ag["id"]] = {"meetings": len(found), "with_agenda": sum(1 for m in found.values() if m["agenda"]),
                               "with_minutes": sum(1 for m in found.values() if m["minutes"]),
                               "newest": max(found, default=""), "oldest": min(found, default="")}
        out["meetings"] += len(found)
        if dry_run:
            out["by"][ag["id"]]["sample"] = sorted(found.values(), key=lambda m: m["date"], reverse=True)[:3]
            continue
        for d, m in found.items():
            rec = Record(f"ga-agency:{ag['id']}", "meeting", f"{m['page']}#meeting-{d}", title=f"{ag['name']} {d}",
                         text=" · ".join(x for x in (ag["name"], d, m.get("what"), m["where"]) if x), published=d, channel="html",
                         jurisdiction="ga", evidence="primary", key=f"ga-agency:{ag['id']}|meeting|{d}",
                         meta={"agency": ag["id"], "agency_name": ag.get("agency") or "", "EventBodyName": ag["name"], "EventDate": d,
                               "EventAgendaFile": m["agenda"], "EventMinutesFile": m["minutes"], "docs": m["docs"],
                               "what": m.get("what") or "", "location": m["where"], "found_on": m["page"], "opened": True})
            out["new"] += store.add(rec)
        opened = open_minutes(fetcher, store, ag, found, deadline, minutes_cap, log=log) if minutes_cap else 0
        out["minutes_opened"] = out.get("minutes_opened", 0) + opened
        log(f"agencies: {ag['name']}: {len(found)} meetings ({out['by'][ag['id']]['with_minutes']} with minutes; {opened} minutes files opened now)")
    if not dry_run:
        store.flush()
    return out


# ---------------------------------------------------------------------------------------------- the bundle's file
def export_state_meetings(store, root: Path, path: Path, today: str | None = None) -> dict:
    """state_meetings.json: every state board's meetings kept here, and the General Assembly's committee meetings.

    {"as_of", "note", "agencies": [{"id","name","agency","pages":[url], "links":[{t,u}], "meetings":[{d, n, a, m, p, w, x:[{t,u}]}]}],
     "legislature": {"source", "meetings":[{d, t, c, b, w, a, v, n, x}]}}
    d date, n what the page calls the meeting, a agenda link, m minutes link, p the page it was listed on, w where, x other linked files; for the
    legislature t the clock time, c the chamber, b the committee or body, v the livestream link, n the notice's note,
    x true when the notice says the meeting was cancelled. Newest first. A field with nothing is left out."""
    import datetime as _dt

    today = today or _dt.datetime.now(_dt.timezone.utc).date().isoformat()

    def slim(d: dict) -> dict:
        return {k: v for k, v in d.items() if v not in ("", None, [], False)}

    ags = []
    for ag in load(root):
        rows = []
        for r in store.read(f"ga-agency:{ag['id']}", "meeting"):
            mt = r.get("meta") or {}
            d = str(mt.get("EventDate") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                continue
            rows.append(slim({"d": d, "n": mt.get("what") or "", "a": mt.get("EventAgendaFile") or "", "m": mt.get("EventMinutesFile") or "",
                              "p": mt.get("found_on") or "", "w": mt.get("location") or "",
                              "x": [x for x in (mt.get("docs") or []) if isinstance(x, dict) and x.get("u")][:8]}))
        rows.sort(key=lambda x: x["d"], reverse=True)
        ags.append({"id": ag["id"], "name": ag["name"], "agency": ag.get("agency") or "", "pages": list(ag.get("pages") or []),
                    "links": [x for x in (ag.get("links") or []) if isinstance(x, dict) and x.get("u")], "meetings": rows})
    leg = []
    for r in store.read("ga:legis", "meeting"):
        mt = r.get("meta") or {}
        d = str(mt.get("EventDate") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            continue
        leg.append(slim({"d": d, "t": mt.get("EventTime") or "", "c": mt.get("chamber") or "", "b": mt.get("EventBodyName") or "",
                         "w": mt.get("location") or "", "a": mt.get("EventAgendaFile") or "", "v": mt.get("EventVideoPath") or "",
                         "n": (r.get("text") or "")[:400], "x": bool(mt.get("cancelled"))}))
    leg.sort(key=lambda x: (x["d"], x.get("t", "")), reverse=True)
    cfg_path = Path(root) / "config" / "agencies.yaml"
    not_read = [{"name": x["name"], "why": x.get("why") or "", "u": x.get("u") or ""}
                for x in ((yaml.safe_load(cfg_path.read_text()) or {}).get("not_read") or [] if cfg_path.exists() else [])
                if isinstance(x, dict) and x.get("name")]
    out = {"as_of": today, "not_read": not_read,
           "note": "State boards' and commissions' meetings as their own pages list them, and the General Assembly's "
                   "committee meetings from the legislature's schedule. A meeting with no agenda or minutes link here "
                   "had none on the page when it was read.",
           "agencies": ags,
           "legislature": {"source": "https://www.legis.ga.gov/schedule/all", "meetings": leg}}
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"agencies": sum(1 for a in ags if a["meetings"]), "agency_meetings": sum(len(a["meetings"]) for a in ags),
            "legislature_meetings": len(leg)}
