"""School boards' meeting schedules from the districts' own websites (derive step `board_schedules`).

Georgia's Open Meetings Act has every board post the time, place and dates of its regular meetings "on the agency's
website, if any", at least a week ahead (O.C.G.A. § 50-14-1(d)(1)). The crawl reads every district's website
(`sd-<id>:site`, pages and documents), so the upcoming meetings are already in the database as text; nothing read them
out. School boards' own meeting systems (Simbli) refuse the crawler, and their Archive copies stop in 2024
(gacrawl/simbli_archive.py), so this is where the coming meetings come from.

A schedule is read only from a part of a page that says it is one ("2025-2026 Board Meeting Schedule", "Regular
meetings are held ...", "Calendar of Board Meetings"), and only its lines that begin with a date; holidays, testing days
and other calendar entries are left; a date without a year takes it from the school year the heading names (August to
December the first year, January to July the second). A part holding fewer than two such dates is not a schedule.

Records: kind `meeting` under the page's own source and district, one per date (`meta.scheduled`, `meta.from_page`).
"""
from __future__ import annotations

import datetime as dt
import re

from .agencies import MONTHS, _MON, _ok
from .derive import Deriver, register
from .store import Record

CUE = re.compile(r"board (?:of education |of trustees )?(?:regular |work session )?meetings? (?:schedule|dates|calendar)|"
                 r"(?:^|\W)meeting (?:schedule|dates|calendar)|calendar of (?:board |regular )?meetings|"
                 r"schedule of (?:board |regular )?meetings|(?:regular|monthly) (?:board )?meetings? (?:are|will be) held|"
                 r"board (?:meeting )?calendar", re.I)
NOT_MEETING = re.compile(r"no school|holiday|\bbreak\b|teacher work|planning day|early release|graduation|first day|last day|"
                         r"\bprom\b|homecoming|testing|\bexams?\b|registration|report cards?|vacation|conference day|"
                         r"professional (?:learning|development)|student holiday|labor day|thanksgiving|winter|spring", re.I)
SCHOOL_YEAR = re.compile(r"\b(20\d\d)\s*[-–/]\s*(?:20)?(\d\d)\b")
YEAR = re.compile(r"\b(20\d\d)\b")
WEEKDAY = r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\.?,?\s+"
LEAD_DATE = re.compile(rf"^\W*(?:{WEEKDAY})?(?P<mon>{_MON})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>20\d\d))?\b", re.I)
LEAD_NUM = re.compile(r"^\W*(?:" + WEEKDAY + r")?(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>20\d\d|\d\d))?\b", re.I)
TIME = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\.?)", re.I)
KIND = re.compile(r"work session|called|special|regular|retreat|budget hearing|public hearing|tax hearing|"
                  r"organizational|annual|committee", re.I)
MAX_SECTION = 80
MAX_PER_PAGE = 40


def _mon(s: str) -> int:
    s = s.lower()[:3]
    return next(i + 1 for i, m in enumerate(MONTHS) if m.startswith(s))


def _years(text: str) -> tuple[int | None, int | None]:
    """(first year, second year) of a school year named in the text ("2025-2026", "2025-26"); (y, y) for a single year."""
    m = SCHOOL_YEAR.search(text or "")
    if m:
        y1 = int(m.group(1))
        y2 = int(str(y1)[:2] + m.group(2))
        if y2 == y1 + 1:
            return y1, y2
    m = YEAR.search(text or "")
    return (int(m.group(1)), int(m.group(1))) if m else (None, None)


def _date(line: str, years: tuple[int | None, int | None]) -> tuple[str, int]:
    """(ISO day, where the date ends) of a date the line begins with; ("", 0) when it begins with none."""
    m = LEAD_DATE.match(line)
    if m:
        mon, d, y = _mon(m.group("mon")), int(m.group("d")), m.group("y")
    else:
        m = LEAD_NUM.match(line)
        if not m:
            return "", 0
        mon, d, y = int(m.group("m")), int(m.group("d")), m.group("y")
        if not 1 <= mon <= 12:
            return "", 0
    if y:
        y = int(y) + (2000 if int(y) < 100 else 0)
    else:
        y1, y2 = years
        if not y1:
            return "", 0
        y = y1 if mon >= 8 or y1 == y2 else y2
    return _ok(y, mon, d), m.end()


def schedule(text: str, title: str = "") -> list[dict]:
    """[{d, time, what, line}] of the board meetings a page's schedule lists (see the module docstring)."""
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").split("\n")]
    out, seen = [], set()
    for i, cue in enumerate(lines):
        if not CUE.search(cue):
            continue
        years = _years(cue)
        if not years[0]:
            years = _years(title)
        found = []
        for line in lines[i + 1: i + 1 + MAX_SECTION]:
            if line.startswith("#") and not CUE.search(line) and not _date(line.lstrip("# "), years)[0]:
                break                                   # the next part of the page
            cells = [c.strip() for c in line.strip("|").split("|")] if line.startswith("|") else [line]
            flat = " · ".join(c for c in cells if c and not re.fullmatch(r"[-: ]+", c))
            body = flat.lstrip("-*•· ").strip()
            if not body or NOT_MEETING.search(body):
                continue
            d, end = _date(body, years)
            if not d:
                continue
            rest = body[end:]
            t = TIME.search(rest)
            what = TIME.sub("", rest)
            what = re.sub(r"^[\s,:·\-–—|]+|[\s,:·\-–—|]+$", "", re.sub(r"\s*·\s*", " · ", what)).strip()
            k = KIND.search(what)
            what = what[:90] if what and (k or len(what) <= 60) else ""
            found.append({"d": d, "time": t.group(1).lower().replace(" ", "").replace(".", "") if t else "",
                          "what": what or "Board meeting", "line": body[:200]})
        if len(found) < 2:
            continue                                    # a date or two in a paragraph is not a schedule
        for f in found:
            key = (f["d"], f["what"].lower())
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out[:MAX_PER_PAGE]


def _derive(src: dict, ctx: dict):
    title = src.get("title") or ""
    text = src.get("text") or src.get("markdown") or ""
    if not CUE.search(title + "\n" + text[:200000]):
        return []
    today = dt.date.today()
    lo, hi = (today - dt.timedelta(days=800)).isoformat(), (today + dt.timedelta(days=420)).isoformat()
    out = []
    for m in schedule(text, title):
        if not lo <= m["d"] <= hi:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", m["what"].lower()).strip("-")[:40]
        out.append(Record(src["source_id"], "meeting", src["url"], title=m["what"], text=m["line"], published=m["d"],
                          jurisdiction=src.get("jurisdiction") or src["source_id"].split(":", 1)[0], channel="derived",
                          key=f"{src['source_id']}|meeting|schedule|{m['d']}|{slug}",
                          meta={"scheduled": True, "from_page": src["url"], "EventBodyName": "Board of Education",
                                "EventDate": m["d"], "time": m["time"], "provider": "district website", "opened": True}))
    return out


register(Deriver("board_schedules", "source_id LIKE 'sd-%:site' AND kind IN ('page','document')", _derive, version="1"))
