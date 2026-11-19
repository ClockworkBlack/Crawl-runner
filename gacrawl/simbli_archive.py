"""School boards' Simbli pages as the Internet Archive already holds them (`gacrawl schools simbli-archive`).

Why. GSBA's eBOARD (Simbli, simbli.eboardsolutions.com) hosts the meeting lists, agendas, minutes and policy manuals of
most Georgia school boards (98 districts' entries point at it). Its bot protection answers the crawler with a challenge
page, and the crawler never works around that, so the School Districts and Committee Meetings tabs had no school board
meetings at all. The owner asked (Nov. 19, 2026) for a lawful way to the same public records, and then made it the
rule for every refusing source (gacrawl/archive_sources.py; README, *Sourcing rules*). This is the Simbli part: the
Wayback Machine's copies, captured by the Internet Archive before Simbli's protection began turning the Archive away
too (from about October 2024 every capture is the 1-2 KB challenge page). So it gives the past (2016 to mid-2024), not
upcoming meetings; those come from the districts' own websites, where the Open Meetings Act puts them
(gacrawl/board_schedules.py), and from open records requests.

What it does and does not do (the lines that keep it lawful and within the project's rules):
  - reads only captures the Archive already holds, through its documented capture index (CDX) and its pages, at the
    Archive's pace (gacrawl/wayback.py: one request every 4 s, no retry after a 429, the first refusal ends the run);
  - never sends a request to simbli.eboardsolutions.com or its other hosts (assistive.eboardsolutions.com is behind
    the same protection), never asks the Archive to capture a page (Save Page Now would have the Archive fetch Simbli
    on the crawler's behalf: a way around Simbli's refusal, not a reading of what was already public), never logs in,
    never solves or replays a challenge, never presents itself as anything but the crawler;
  - keeps only real content: a capture that is the challenge page, or that does not read as Simbli's own page, is left;
  - files every record as an archived copy: `url` is the Archive's copy (what readers follow), meta.original the
    Simbli address, meta.captured the day it was captured, meta.archived true.
The records are public records of Georgia public bodies (agendas and minutes: O.C.G.A. § 50-14-1(e); board policies)
and the Archive offers its copies to the public; nothing here reaches anything that is not public.

Switch: `schools.simbli_archive` in config/settings.yaml (true by default since the owner asked for it). Progress per
district in kv `simbli:archive:<S>` (read again after RECHECK_DAYS, in case the Archive gains new captures).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time

from .store import Record
from .wayback import CDX, RAW, SHOW, Wayback, day

HOST = "simbli.eboardsolutions.com"
PAGES = {"meetings": "https://" + HOST + "/SB_Meetings/SB_MeetingListing.aspx?S={s}",
         "policies": "https://" + HOST + "/Policy/PolicyListing.aspx?S={s}"}
MEETING = "https://" + HOST + "/SB_Meetings/ViewMeeting.aspx?S={s}&MID={mid}"
REAL_MIN = 6000            # a capture's stored size (CDX `length`) below this is the challenge page (1-2 KB), not content
MAX_CAPTURES = 6           # captures of a listing read per district (each holds the meetings listed when it was taken)
DETAIL_PER_DISTRICT = 8    # meetings whose own page (agenda, minutes) is looked up per district and run
RECHECK_DAYS = 90
CHALLENGE = re.compile(r"incapsula|_incapsula_resource|request unsuccessful|pardon our interruption|incident id|"
                       r"access denied|attention required|captcha|cf-chl|are you a robot|verify you are human", re.I)
SIMBLI_MARK = re.compile(r"eboardsolutions|simbli", re.I)
S_ID = re.compile(r"(?i)[?&]s=(\d+)")
MID = re.compile(r"(?i)\bMID=(\d+)")
ROW_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(20\d\d)(?:\s*-\s*(\d{1,2}:\d{2}\s*[AP]\.?M\.?))?", re.I)
POLICY_CODE = re.compile(r"^[A-Z]{1,6}(?:[-(][A-Z0-9()]{1,6}\)?)*$")


def site_ids(jurisdictions: list[dict]) -> dict[str, str]:
    """{district id: Simbli site number} from the registry's simbli portal links."""
    out = {}
    for j in jurisdictions:
        u = (j.get("portals") or {}).get("simbli") or ""
        m = S_ID.search(u)
        if m and j.get("kind") == "school_district":
            out[j["id"]] = m.group(1)
    return out


def is_real(html: str) -> bool:
    """A capture of Simbli's own page, not of its bot-protection challenge."""
    if not html or len(html) < 3000:
        return False
    head = html[:20000]
    return bool(SIMBLI_MARK.search(head)) and not CHALLENGE.search(head)


def _iso(m) -> str:
    try:
        return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return ""


def parse_listing(html: str) -> list[dict]:
    """The meeting table of a captured SB_MeetingListing page: [{d, time, title, type, minutes, mid}], as listed.
    Header 'Date - Time | Meeting Title | Minutes | Meeting Type'; a row's meeting number (MID) from its link or the
    script its click runs, when the row carries one."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for table in soup.find_all("table"):
        cols = None
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            low = [c.lower() for c in cells]
            if any("date" in c for c in low) and any("title" in c or "meeting" in c for c in low) and not ROW_DATE.search(cells[0]):
                cols = {"title": next((i for i, c in enumerate(low) if "title" in c), 1),
                        "minutes": next((i for i, c in enumerate(low) if "minutes" in c), None),
                        "type": next((i for i, c in enumerate(low) if "type" in c), None)}
                continue
            m = ROW_DATE.search(cells[0])
            if not m or len(cells) < 2:
                continue
            c = cols or {"title": 1, "minutes": 2 if len(cells) > 3 else None, "type": 3 if len(cells) > 3 else None}
            d = _iso(m)
            title = cells[c["title"]] if c["title"] < len(cells) else ""
            if not d or not title:
                continue
            mm = MID.search(str(tr))
            a = next((x for x in tr.find_all("a", href=True) if "viewmeeting" in x["href"].lower()), None)
            row = {"d": d, "time": (m.group(4) or "").upper().replace(".", ""), "title": title[:200],
                   "href": _unarchive(a["href"]) if a else "",
                   "type": cells[c["type"]] if c["type"] is not None and c["type"] < len(cells) else "",
                   "minutes": cells[c["minutes"]] if c["minutes"] is not None and c["minutes"] < len(cells) else "",
                   "mid": mm.group(1) if mm else ""}
            k = (row["d"], row["time"], row["title"].lower(), row["mid"])
            if k not in seen:
                seen.add(k)
                out.append(row)
    return out


def _unarchive(u: str) -> str:
    """A link as the Archive rewrote it ("/web/20240909160017/https://simbli...") back to the address it points at."""
    u = re.sub(r"^(?:https?://web\.archive\.org)?/web/\d+(?:[a-z_]+)?/", "", u or "")
    if u.startswith("/"):
        u = "https://" + HOST + u
    return u


def parse_policies(html: str, base: str) -> list[dict]:
    """The policy manual's index of a captured PolicyListing page: [{code, title, u}] (u: the Simbli address of the
    policy, as the page links it)."""
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "viewpolicy" not in href.lower():
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        row = a.find_parent("tr")
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in row.find_all("td")] if row else []
        code, title = "", text
        m = re.match(r"^([A-Z]{1,6}(?:[-(][A-Z0-9()]{1,6}\)?)*)\s*[-:–]\s*(.+)$", text)
        if m:
            code, title = m.group(1), m.group(2)
        elif cells and POLICY_CODE.match(cells[0]):
            code, title = cells[0], next((c for c in cells[1:] if c and c != cells[0]), text)
        elif POLICY_CODE.match(text) and cells:
            code, title = text, next((c for c in cells if c and c != text), "")
        if not title or len(title) < 3:
            continue
        u = _unarchive(href) if "/web/" in href else urljoin(base, href)
        k = (code, title.lower())
        if k not in seen:
            seen.add(k)
            out.append({"code": code, "title": title[:160], "u": u})
    return out


class SimbliArchive:
    def __init__(self, fetcher, store, log=print, wayback: Wayback | None = None):
        self.store, self.log = store, log
        self.wb = wayback or Wayback(fetcher, log=log)
        self.stats = {"districts": 0, "captures_read": 0, "challenge_captures": 0, "meetings": 0, "policies": 0,
                      "meeting_pages": 0, "no_capture": 0}

    def captures(self, url: str, newest: int = 40, prefix: bool = False) -> list[dict]:
        """Captures of `url` large enough to be content (status 200), oldest first: [{timestamp, original, length}]."""
        params = [("url", url + ("*" if prefix else "")), ("output", "json"), ("fl", "timestamp,original,length"),
                  ("filter", "statuscode:200"), ("collapse", "digest"), ("limit", f"-{int(newest)}")]
        r = self.wb._get(CDX, params)
        if r is None or r.status != 200:
            return []
        try:
            rows = r.json()
        except ValueError:
            return []
        if not rows or not isinstance(rows, list):
            return []
        head = rows[0]
        out = []
        for row in rows[1:]:
            c = dict(zip(head, row))
            try:
                if int(c.get("length") or 0) >= REAL_MIN:
                    out.append(c)
            except ValueError:
                continue
        return sorted(out, key=lambda c: c["timestamp"])

    def read(self, cap: dict) -> str | None:
        r = self.wb._get(RAW.format(ts=cap["timestamp"], url=cap["original"]))
        if r is None or r.status != 200:
            return None
        self.stats["captures_read"] += 1
        html = r.text
        if not is_real(html):
            self.stats["challenge_captures"] += 1
            return None
        return html

    def district(self, did: str, s: str) -> dict:
        """One district: its meetings as each captured listing showed them, its policy index, and the own pages of
        up to DETAIL_PER_DISTRICT meetings (agenda and minutes text) that the Archive holds."""
        sid = f"{did}:simbli"
        got = {"meetings": 0, "policies": 0, "pages": 0, "captures": []}
        listed: dict[str, dict] = {}
        for kind, tmpl in PAGES.items():
            url = tmpl.format(s=s)
            caps = self.captures(url)[-MAX_CAPTURES:]
            if not caps:
                continue
            for cap in caps:                         # oldest first: a newer capture's reading of a meeting wins
                if self.wb.off:
                    return got
                html = self.read(cap)
                if not html:
                    continue
                got["captures"].append(cap["timestamp"])
                shown = SHOW.format(ts=cap["timestamp"], url=cap["original"])
                base = {"provider": "simbli", "archived": True, "captured": day(cap["timestamp"]), "via": "wayback",
                        "opened": True, "archive_listing": shown}
                if kind == "meetings":
                    for m in parse_listing(html):
                        orig = m["href"] or (MEETING.format(s=s, mid=m["mid"]) if m["mid"] else cap["original"])
                        key = f"{sid}|meeting|{m['mid'] or (m['d'] + '|' + m['time'] + '|' + m['title'].lower())}"
                        listed[key] = m
                        self.store.add(Record(
                            sid, "meeting", shown, title=m["title"], text=" · ".join(x for x in (m["title"], m["type"], m["time"]) if x),
                            published=m["d"], jurisdiction=did, channel="archive", evidence="primary", key=key,
                            meta={**base, "original": orig, "MID": m["mid"], "EventDate": m["d"], "time": m["time"],
                                  "meeting_type": m["type"], "minutes_status": m["minutes"],
                                  "EventBodyName": _body(m["type"])}))
                        got["meetings"] += 1
                else:
                    for p in parse_policies(html, cap["original"]):
                        self.store.add(Record(
                            sid, "policy", SHOW.format(ts=cap["timestamp"], url=p["u"]) if p["u"].startswith("http") else shown,
                            title=p["title"], text=f"{p['code']} {p['title']}".strip(), jurisdiction=did, channel="archive",
                            evidence="primary", key=f"{sid}|policy|{p['code'] or p['title'].lower()}",
                            meta={**base, "original": p["u"], "code": p["code"]}))
                        got["policies"] += 1
        # each meeting's own page, where the Archive has one: looked up by the exact address the captured listing
        # links to (the addresses the Archive's crawler followed), never by a search over Simbli's addresses (a prefix
        # query over them is more than the capture index answers: HTTP 503). Published minutes first, newest first.
        tried = json.loads(self.store.kv_get(f"simbli:archive:{s}:pages") or "{}")
        todo = [m for m in listed.values() if m.get("href") and m["href"] not in tried]
        todo.sort(key=lambda m: m["d"], reverse=True)
        todo.sort(key=lambda m: not (m["minutes"] or "").lower().startswith("published"))
        for m in todo[:DETAIL_PER_DISTRICT]:
            if self.wb.off:
                break
            caps = self.captures(m["href"], newest=5)
            tried[m["href"]] = dt.date.today().isoformat()
            html = self.read(caps[-1]) if caps else None
            if not html:
                continue
            title, md = meeting_text(html, caps[-1]["original"])
            if not md or len(md) < 200 or not CONTENT.search(md):
                continue
            kind = "minutes" if (m["minutes"] or "").lower().startswith("published") else "agenda"
            self.store.add(Record(
                sid, "document", SHOW.format(ts=caps[-1]["timestamp"], url=caps[-1]["original"]),
                title=f"{m['title']} {m['d']}", text=md[:200000], markdown=md[:200000], published=m["d"], jurisdiction=did,
                channel="archive", evidence="primary", key=f"{sid}|document|{m['mid'] or m['href']}",
                meta={"provider": "simbli", "archived": True, "captured": day(caps[-1]["timestamp"]), "via": "wayback",
                      "opened": True, "original": caps[-1]["original"], "MID": m["mid"], "doc_type": kind,
                      "EventBodyName": _body(m["type"]), "EventDate": m["d"]}))
            got["pages"] += 1
        self.store.kv_set(f"simbli:archive:{s}:pages", json.dumps(tried))
        return got

    def run(self, ids: dict[str, str], deadline: float | None = None, max_districts: int = 20) -> dict:
        today = dt.date.today()

        def due(s):
            st = json.loads(self.store.kv_get(f"simbli:archive:{s}") or "{}")
            at = st.get("at")
            return (not at) or (today - dt.date.fromisoformat(at)).days >= RECHECK_DAYS, at or ""

        order = sorted(ids.items(), key=lambda kv: due(kv[1])[1])        # never read first, then the oldest read
        n = 0
        for did, s in order:
            if n >= max_districts or self.wb.off or (deadline and time.monotonic() > deadline):
                break
            ok, _ = due(s)
            if not ok:
                continue
            n += 1
            got = self.district(did, s)
            self.stats["districts"] += 1
            self.stats["meetings"] += got["meetings"]
            self.stats["policies"] += got["policies"]
            self.stats["meeting_pages"] += got["pages"]
            if not got["captures"]:
                self.stats["no_capture"] += 1
            if self.wb.off:
                break                                # not stamped: the archive stopped answering part-way
            self.store.kv_set(f"simbli:archive:{s}", json.dumps({"at": today.isoformat(), "district": did, "meetings": got["meetings"],
                                                                 "policies": got["policies"], "pages": got["pages"],
                                                                 "captures": got["captures"][-12:]}))
            self.log(f"simbli archive: {did} (S={s}): {got['meetings']} meetings, {got['policies']} policies, {got['pages']} meeting pages "
                     f"from {len(got['captures'])} captures")
        self.store.flush()
        return {**self.stats, "archive_off": self.wb.off or ""}


# Simbli's own furniture on every page: the help desk, the log-in box, the support numbers, the session timer. A page
# carries far more of it than of the meeting, so the generic extractor's "largest block" was the help text.
CHROME = re.compile(r"simbli help desk|learning center|knowledge base|session timeout|forgot your password|login using simbli|"
                    r"get support|want to chat|please click left side link|tap to add an icon|single sign-on|"
                    r"prefer to call|video-based training|product updates|rate this help topic", re.I)
CONTENT = re.compile(r"call to order|approv(?:e|al) of|motion|adjourn|consent agenda|executive session|public comment|"
                     r"superintendent'?s report|action items?|minutes of", re.I)


def meeting_text(html: str, url: str) -> tuple[str, str]:
    """(title, markdown) of a captured Simbli meeting page without Simbli's own furniture: every block that holds
    the furniture's wording and none of a meeting's is dropped before the text is read."""
    from bs4 import BeautifulSoup

    from . import extract

    soup = BeautifulSoup(html, "html.parser")
    for el in list(soup.find_all(["div", "section", "ul", "nav", "form", "table", "aside", "span", "p"])):
        if getattr(el, "decomposed", False):
            continue                     # inside a block already dropped
        t = el.get_text(" ", strip=True)
        if CHROME.search(t) and not CONTENT.search(t):
            el.decompose()
    return extract.html_to_markdown(str(soup), url)


def _body(meeting_type: str) -> str:
    """The body that met, from Simbli's meeting type ("A-I. Board of Education Meetings" -> "Board of Education")."""
    t = re.sub(r"^[A-Z]{1,3}-[IVX]+\.\s*", "", meeting_type or "").strip()
    t = re.sub(r"\s+Meetings?$", "", t, flags=re.I).strip()
    return t or "Board of Education"


def run(fetcher, store, root, deadline: float | None = None, max_districts: int = 20, log=print) -> dict:
    from . import registry

    ids = site_ids(registry.jurisdictions(root))
    if not ids:
        return {"districts": 0, "note": "no district has a Simbli link in the registry"}
    return SimbliArchive(fetcher, store, log=log).run(ids, deadline=deadline, max_districts=max_districts)
