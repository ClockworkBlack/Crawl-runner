"""School boards' meetings from the districts' own public calendars (`gacrawl schools calendars`).

Why. Georgia's 180 school boards keep their meeting lists in Simbli, which refuses the crawler, and the Internet
Archive's copies of Simbli stop in 2024 (gacrawl/simbli_archive.py); a schedule posted as text is read by the derive
step board_schedules. Most districts also put every board meeting on the district's own calendar: this reads it.

What the districts' sites are, sampled Nov. 19, 2026 (53 of the 180 districts, large and small; what each platform
serves and where its board meetings are):
  - Finalsite (most of the larger systems: Gwinnett, Fulton, Clayton, Forsyth, Cherokee, Henry, Atlanta, Savannah-
    Chatham, Richmond, Muscogee, Bibb, Fayette, Paulding, Effingham, Rockdale, Oconee, Whitfield, Marietta City, and
    small ones such as Appling, Baldwin, Ben Hill and Emanuel). Its iCal and RSS exports and its calendar elements
    live under /fs/ (/fs/calendar-manager/..., /fs/elements/<id>), and every Finalsite robots.txt says `Disallow: /fs/`,
    so those are never asked for. The calendar page itself (/calendar, /about-us/calendar, /boe/boe-calendar ...)
    carries the month's events in its HTML: `a.fsCalendarEventLink` (or a `.fsCalendarTitle` without a link), inside
    `.fsCalendarInfo` (month grid) or `article` (list view), with `time.fsStartTime` / `time.fsDate` datetime="2026-11-
    19T19:00:00-05:00", the day box's `.fsCalendarDate` data-day / data-month (0 = January) / data-year, and the event
    link's data-occur-id ("1286056341_2026-11-20T00:00:00Z_2026-11-20T02:00:00Z": event id, start and end in UTC; or a
    bare number on older sites). `?cal_date=2027-01-01` on the same page shows that month (checked on Gwinnett and
    Fulton). A few calendar elements are filled by script from /fs/elements (DeKalb's meeting calendar): nothing to read.
  - Apptegy / Thrillshare (Bartow, Bryan, Carroll, Clarke, Crawford, Douglas, Lowndes, Pickens, Berrien, Dalton City):
    the site's /events page links "Click to Download Calendar",
    https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/<org>/cms/events/generate_ical?filter_ids&section_ids
    (the org number is also in every page's __NUXT_DATA__, ".../api/v4/o/<org>/cms/events"). An iCalendar file with
    floating local times (DTSTART:20261208T183000, no TZID) and all-day DTSTART;VALUE=DATE; its UIDs are new random
    UUIDs on every request (two reads a minute apart), so a record is keyed on its date and title, not its UID. The same
    host answers JSON at .../cms/events (title, start_at with offset, all_day, a stable id), the fallback.
  - Edlio (Bacon, Banks, Columbia): /apps/events/ and its iCal links sit behind a browser check (HTTP 403 to automated
    readers), so they are never asked for (a challenge would put the whole host on the block list for a week); the home
    page's "Upcoming Events" lists the next events as links /apps/events/2026/11/19/<REC_ID>/?REC_ID=...&id=0 (the date
    in the address, the title as the link text, "Time: 4:30 PM - 6:30 PM" beside it), read on every run.
  - Google Calendar embeds (Cook: calendar.google.com/calendar/embed?src=<id>): the public address is
    https://calendar.google.com/calendar/ical/<id>/public/basic.ics, but calendar.google.com's robots.txt is
    `Disallow: /` (all but the front page), so the crawler itself is refused there. Google publishes that address for
    other applications to subscribe to, and since Nov 19, 2026 (the owner's decision, schools.calendar_subscriptions)
    it is subscribed to as a calendar application does, by gacrawl/calendar_subscriber.py: only the public address a
    district linked on its own site, an honest User-Agent, at most every 12 hours, If-None-Match. Switched off, such a
    feed is read only from the Internet Archive's existing copies (gacrawl/archive_sources.py's rule).
  - SchoolBundle (Cobb, Griffin-Spalding: cicmsapi.azurewebsites.net/<site>/_ci/calendar/ics/<event>): one .ics per
    event, and category feeds built by the page's script: nothing whole to read; their posted schedules are read by
    board_schedules.
  - ParentSquare SmartSites (Barrow, Catoosa, Glynn, Jackson): the calendar (an Outlook feed behind event_view links)
    is drawn by script; nothing in the served page.
  - WordPress (Hall: the school year as an uploaded .ics, no board meetings; The Events Calendar's `?ical=1` export where
    a site has it), Blackboard / SchoolMessenger (/site/handlers/icalfeed.ashx?MIID=), plain .ics and webcal:// links,
    RSS feeds named for a calendar, and schema.org Event JSON-LD: read where a district's pages link or carry them.
  - Not reached: Houston and Cook's own CMS (FullCalendar by script), Coweta and Newton (no calendar on the home page),
    Liberty (robots.txt refuses the home page). Simbli's own calendar is never asked for (Simbli refuses the crawler).

What it does. Once a week per district (state/district_calendars.json, keyed by district id: the feeds found, the
platform, where each was found, when it was last read and what it gave) the district's home page and up to MAX_PAGES of
its own calendar, events and board pages are opened and their feeds found; config/district_calendars.yaml (feeds checked
by hand, with the day) comes first. Every run reads every feed found (one request each; a Finalsite page this month, the
next two and one more month in turn, so a year ahead and two years back fill in over a few days). Only board meetings
are kept: regular, work session, called or special meetings, budget and tax hearings, retreats, committee meetings on a
board's own calendar; holidays, games, testing and school events are left. The window is board_schedules': 800 days back
to 420 ahead (simple RRULEs expanded 13 months ahead). A future meeting a feed no longer lists (moved or cancelled) is
removed when the whole feed was read.

Records: kind `meeting`, source `<district>:calendar`, jurisdiction the district, meta EventDate, EventBodyName "Board of
Education", time, provider "district calendar", scheduled, platform, from_feed (the feed or page read), found_on, and
the meeting type. Keyed on the date and the event's own stable id (a Google or Outlook UID, Finalsite's event number,
Edlio's REC_ID), else its title.

Polite like every reader here: the Fetcher's robots.txt, per-host pacing and Crawl-delay (Finalsite asks 5 s), the run's
deadline (OutOfTime), a refusal (Blocked, Disallowed) recorded and left until the next week's look. Never a login,
never a CAPTCHA, never another User-Agent, never Save Page Now.
"""
from __future__ import annotations

import base64
import datetime as dt
import html as htmlmod
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .blocked import Blocked
from .fetch import Disallowed, OutOfTime
from .store import Record

PROVIDER = "district calendar"
STATE_NAME = "district_calendars.json"
SEED_NAME = "district_calendars.yaml"
BACK_DAYS, AHEAD_DAYS = 800, 420          # the window board_schedules uses
RRULE_AHEAD_DAYS = 396                    # recurring meetings expanded about 13 months ahead
REDISCOVER_DAYS = 7
MAX_PAGES = 6                             # calendar, events and board pages opened per district when looking
FS_EVERY_RUN = (0, 1, 2)                  # Finalsite months read every run: this month and the next two
FS_ROTATE = list(range(3, 13)) + list(range(-1, -25, -1))   # and one more in turn, a year ahead, two years back
ARCHIVE_PER_RUN = 10                      # refused feeds looked up in the Internet Archive per run
ARCHIVE_RECENT_DAYS = 60                  # a capture older than this says nothing about the coming meetings
ARCHIVE_RECHECK_DAYS = 7
MAX_OCCURRENCES = 500
ET = "America/New_York"
THRILL = "https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/{org}/cms/events"
GCAL_ICS = "https://calendar.google.com/calendar/ical/{id}/public/basic.ics"
NEVER_HOSTS = ("eboardsolutions.com", "boarddocs.com")    # read by their own readers (Simbli refuses the crawler)

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo(ET)
except Exception:  # noqa: BLE001 — no time zone database: US Eastern rules below
    ZoneInfo = None
    _ET = None

WINDOWS_TZ = {"eastern standard time": "America/New_York", "us eastern standard time": "America/New_York",
              "central standard time": "America/Chicago", "mountain standard time": "America/Denver",
              "pacific standard time": "America/Los_Angeles", "utc": "UTC", "gmt standard time": "Europe/London"}


# ----------------------------------------------------------------------------- what is a board meeting
BOARD = re.compile(r"\bboard\b|\b[a-z]{0,6}boe\b|\bb\.\s?o\.\s?e\b", re.I)
MEETING = re.compile(r"meeting|\bmtg\b|session|hearing|retreat|workshop|committee|commission|\bcalled\b|town\s*hall", re.I)
STRONG = re.compile(r"work\s*session|called\s+(?:board\s+)?(?:meeting|session)|special(?:\s+called)?\s+(?:board\s+)?(?:meeting|session)|"
                    r"(?:budget|tax(?:\s+digest)?|millage(?:\s+rate)?|mill\s+rate)\s+(?:public\s+)?hearings?|public\s+hearings?|"
                    r"board\s+retreat|executive\s+session|committee\s+of\s+the\s+whole|budget\s+(?:meeting|work\s*session|commission)", re.I)
# never a board meeting, whatever else the title says
HARD_NOT = re.compile(r"\bno\s+school\b|holiday|\bbreak\b|vacation|basketball|football|baseball|softball|soccer|volleyball|"
                      r"wrestling|tennis|\bgolf\b|\bswim|track\s*(?:&|and)\s*field|cross\s+country|cheer|lacrosse|esports|"
                      r"\bgames?\b|\bvs\.?\s|tournament|scrimmage|\bband\b|chorus|orchestra|concert|musical|graduation|"
                      r"commencement|\bprom\b|homecoming|\bpta\b|\bpto\b|\bptsa\b|\bptso\b|booster|foundation|alumni|hall\s+of\s+fame|"
                      r"advisory|\bcouncil\b|go\s+team|school\s+governance|candidate|\bclub\b|teacher\s+of\s+the\s+year|"
                      r"testing|\beoc\b|\beog\b|milestones|\bexams?\b|early\s+release|report\s+cards?|progress\s+reports?|"
                      r"registration|picture\s+day|book\s+fair|field\s+trip|spirit|pep\s+rally|banquet|signing\s+day|"
                      r"subscribe\s+to\s+calendar|\bcancel+ed\b|postponed", re.I)
# on a board's own calendar every meeting is the board's, except these
BOARD_FEED_NOT = re.compile(r"\bstaff\b|faculty|teachers?\b|principals?\b|counselors?|nurses?|coaches|leadership\s+team|"
                            r"department|\bparents?\b|famil(?:y|ies)|student|community\s+conversation|\bpl\b|professional\s+"
                            r"(?:learning|development)|training", re.I)
KINDS = [("work session", re.compile(r"work\s*session|workshop", re.I)),
         ("called meeting", re.compile(r"\bcalled\b|special\s+(?:board\s+)?(?:meeting|session)|emergency", re.I)),
         ("budget or tax hearing", re.compile(r"(?:budget|tax|millage|mill\s+rate|digest).{0,30}hearing|hearing.{0,30}(?:budget|tax|millage)", re.I)),
         ("public hearing", re.compile(r"public\s+hearing", re.I)),
         ("retreat", re.compile(r"retreat", re.I)),
         ("executive session", re.compile(r"executive\s+session", re.I)),
         ("budget meeting", re.compile(r"budget", re.I)),
         ("committee", re.compile(r"committee|commission", re.I)),
         ("regular meeting", re.compile(r"regular|business|monthly|voting|board\s+meeting|boe\s+meeting|education\s+meeting|"
                                         r"meeting\s+of\s+the\s+board", re.I))]


def meeting_type(title: str) -> str:
    return next((k for k, rx in KINDS if rx.search(title or "")), "meeting")


def board_meeting(title: str, board_feed: bool = False) -> str:
    """The kind of board meeting a calendar entry's title names ("work session", "regular meeting" ...), or "" when it
    is not one. A title must name the board and a meeting ("BOE Work Session", "School Board Business Meeting"), or be a
    meeting only a board holds ("Called Work Session", "Budget Hearing", "Executive Session"); on a board's own calendar
    (board_feed) any meeting, session, hearing or committee is the board's but staff, parent and student meetings."""
    t = re.sub(r"\s+", " ", title or "").strip()
    if not t or HARD_NOT.search(t):
        return ""
    if (BOARD.search(t) and MEETING.search(t)) or STRONG.search(t):
        return meeting_type(t)
    if board_feed and MEETING.search(t) and not BOARD_FEED_NOT.search(t):
        return meeting_type(t)
    return ""


# ----------------------------------------------------------------------------- times
def _eastern_fallback(u: dt.datetime) -> dt.datetime:
    """UTC (naive) -> US Eastern local (naive) by the rules since 2007, for a machine without a time zone database."""
    y = u.year

    def nth_sunday(month: int, n: int) -> dt.date:
        d = dt.date(y, month, 1)
        d += dt.timedelta(days=(6 - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)
    start = dt.datetime.combine(nth_sunday(3, 2), dt.time(7))      # 2:00 EST is 07:00 UTC
    end = dt.datetime.combine(nth_sunday(11, 1), dt.time(6))       # 2:00 EDT is 06:00 UTC
    return u + dt.timedelta(hours=-4 if start <= u < end else -5)


def to_local(d: dt.datetime) -> dt.datetime:
    """An aware time as Georgia's wall-clock time (naive)."""
    if d.tzinfo is None:
        return d
    if _ET is not None:
        return d.astimezone(_ET).replace(tzinfo=None)
    return _eastern_fallback(d.astimezone(dt.timezone.utc).replace(tzinfo=None))


def _zone(tzid: str | None):
    if not tzid or ZoneInfo is None:
        return None
    t = tzid.strip().strip('"')
    low = t.lower()
    name = WINDOWS_TZ.get(low)
    if not name and "eastern" in low:
        name = ET                       # "(UTC-05:00) Eastern Time (US & Canada)"
    if not name:
        m = re.search(r"([A-Za-z]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)$", t)     # "/mozilla.org/20070129_1/America/New_York"
        name = m.group(1) if m else t
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


def _iso_local(s: str) -> dt.datetime | None:
    """'2026-11-19T19:00:00-05:00' (or ...Z, or naive) -> Georgia wall-clock time (naive)."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4})-(\d\d)-(\d\d)", s)
        return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    return to_local(d)


def fmt_time(d) -> str:
    """'7:00pm', as board_schedules writes times; '' for a day."""
    if not isinstance(d, dt.datetime):
        return ""
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d}{'am' if d.hour < 12 else 'pm'}"


def day_of(d) -> dt.date:
    return d.date() if isinstance(d, dt.datetime) else d


# ----------------------------------------------------------------------------- iCalendar
def unfold(text: str) -> list[str]:
    """RFC 5545 lines: a line that starts with a space or a tab continues the one before it."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def _split_prop(line: str):
    """'DTSTART;TZID="a:b";VALUE=DATE-TIME:2026...' -> ('DTSTART', {'TZID': 'a:b', ...}, '2026...')."""
    q, cut, parts, start = False, -1, [], 0
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == ";" and not q:
            parts.append(line[start:i])
            start = i + 1
        elif ch == ":" and not q:
            cut = i
            break
    if cut < 0:
        return None
    parts.append(line[start:cut])
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params, line[cut + 1:]


def _text(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v or "").strip()


def parse_dt(value: str, params: dict, default_tz=None):
    """(Georgia wall-clock datetime, or a date for an all-day value, all_day). UTC ('Z') and TZID times are converted;
    a floating time is the district's own local time (Apptegy writes those)."""
    v = (value or "").strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        return dt.date(int(v[:4]), int(v[4:6]), int(v[6:8])), True
    m = re.fullmatch(r"(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)?(Z)?", v)
    if not m:
        raise ValueError(f"not a date-time: {v!r}")
    naive = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
    if m.group(7):
        return to_local(naive.replace(tzinfo=dt.timezone.utc)), False
    z = _zone(params.get("TZID")) or default_tz
    if z is not None:
        return to_local(naive.replace(tzinfo=z)), False
    return naive, False


def parse_ics(text: str) -> dict:
    """{'name': X-WR-CALNAME, 'events': [{uid, summary, start, end, all_day, location, url, description, status, rrule,
    exdates, rdates, recurrence_id}]} of an iCalendar file. Lines that do not parse are skipped, never fatal."""
    out = {"name": "", "events": []}
    default_tz = None
    ev, depth = None, 0
    for line in unfold(text):
        if not line.strip():
            continue
        p = _split_prop(line)
        if not p:
            continue
        name, params, value = p
        if name == "BEGIN":
            if value.strip().upper() == "VEVENT" and ev is None:
                ev, depth = {"exdates": set(), "rdates": [], "rrule": "", "uid": "", "summary": "", "status": "",
                             "location": "", "url": "", "description": "", "recurrence_id": None, "start": None,
                             "end": None, "all_day": False}, 0
            elif ev is not None:
                depth += 1                        # a VALARM inside the event
            continue
        if name == "END":
            if ev is not None and value.strip().upper() == "VEVENT" and depth == 0:
                if ev["start"] is not None:
                    out["events"].append(ev)
                ev = None
            elif ev is not None and depth:
                depth -= 1
            continue
        if ev is None:
            if name == "X-WR-CALNAME":
                out["name"] = _text(value)
            elif name == "X-WR-TIMEZONE":
                default_tz = _zone(value)
            continue
        if depth:
            continue
        try:
            if name == "DTSTART":
                ev["start"], ev["all_day"] = parse_dt(value, params, default_tz)
            elif name == "DTEND":
                ev["end"], _ = parse_dt(value, params, default_tz)
            elif name == "SUMMARY":
                ev["summary"] = re.sub(r"\s+", " ", _text(value))
            elif name == "UID":
                ev["uid"] = value.strip()
            elif name == "LOCATION":
                ev["location"] = re.sub(r"\s+", " ", _text(value))[:200]
            elif name == "URL":
                ev["url"] = value.strip()
            elif name == "DESCRIPTION":
                ev["description"] = _text(value)[:1000]
            elif name == "STATUS":
                ev["status"] = value.strip().upper()
            elif name == "RRULE":
                ev["rrule"] = value.strip()
            elif name == "EXDATE":
                for x in value.split(","):
                    ev["exdates"].add(_occ_key(parse_dt(x, params, default_tz)[0]))
            elif name == "RDATE" and params.get("VALUE", "").upper() != "PERIOD":
                ev["rdates"] += [parse_dt(x, params, default_tz)[0] for x in value.split(",") if x.strip()]
            elif name == "RECURRENCE-ID":
                ev["recurrence_id"] = parse_dt(value, params, default_tz)[0]
        except (ValueError, OverflowError):
            continue
    return out


def _occ_key(d) -> str:
    """An occurrence as EXDATE and RECURRENCE-ID name it: its day, and its time when it has one."""
    return d.isoformat(timespec="minutes") if isinstance(d, dt.datetime) else d.isoformat()


WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _rrule(s: str) -> dict:
    r = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            r[k.strip().upper()] = v.strip()
    return r


def _month_days(y: int, m: int, r: dict, start) -> list[int]:
    """The days of a month a MONTHLY (or YEARLY ... BYMONTH) rule picks: BYDAY ('2TU', '-1FR', 'TH'), BYMONTHDAY, then
    BYSETPOS; with neither, the start's day of the month."""
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    days: list[int] = []
    if r.get("BYDAY"):
        for spec in r["BYDAY"].split(","):
            mm = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", spec.strip().upper())
            if not mm:
                continue
            wd = WD[mm.group(2)]
            all_ = [d for d in range(1, last + 1) if dt.date(y, m, d).weekday() == wd]
            if mm.group(1):
                n = int(mm.group(1))
                if 1 <= abs(n) <= len(all_):
                    days.append(all_[n - 1] if n > 0 else all_[n])
            else:
                days += all_
    elif r.get("BYMONTHDAY"):
        for x in r["BYMONTHDAY"].split(","):
            try:
                n = int(x)
            except ValueError:
                continue
            d = n if n > 0 else last + n + 1
            if 1 <= d <= last:
                days.append(d)
    else:
        if start.day <= last:
            days.append(start.day)
    days = sorted(set(days))
    if r.get("BYSETPOS") and days:
        picked = []
        for x in r["BYSETPOS"].split(","):
            try:
                n = int(x)
            except ValueError:
                continue
            if 1 <= abs(n) <= len(days):
                picked.append(days[n - 1] if n > 0 else days[n])
        days = sorted(set(picked))
    return days


def occurrences(ev: dict, lo: dt.date, hi: dt.date) -> list:
    """The starts of an event within [lo, hi]: itself, or the dates its RRULE (DAILY, WEEKLY, MONTHLY, YEARLY with
    INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH, BYSETPOS) and RDATEs give, less its EXDATEs."""
    start = ev["start"]
    if not ev.get("rrule"):
        return [start] if lo <= day_of(start) <= hi else []
    r = _rrule(ev["rrule"])
    freq = r.get("FREQ", "").upper()
    try:
        interval = max(1, int(r.get("INTERVAL") or 1))
    except ValueError:
        interval = 1
    count = int(r["COUNT"]) if (r.get("COUNT") or "").isdigit() else None
    until = None
    if r.get("UNTIL"):
        try:
            until = day_of(parse_dt(r["UNTIL"], {})[0])
        except ValueError:
            until = None
    t = start.time() if isinstance(start, dt.datetime) else None
    sday = day_of(start)

    def make(d: dt.date):
        return dt.datetime.combine(d, t) if t is not None else d

    found, n, periods = [], 0, 0
    if freq == "DAILY":
        def gen():
            d = sday
            while True:
                yield [d]
                d += dt.timedelta(days=interval)
    elif freq == "WEEKLY":
        wds = sorted({WD[x.strip()[-2:].upper()] for x in (r.get("BYDAY") or "").split(",") if x.strip()[-2:].upper() in WD}
                     or {sday.weekday()})

        def gen():
            wk = sday - dt.timedelta(days=sday.weekday())
            while True:
                yield [wk + dt.timedelta(days=w) for w in wds]
                wk += dt.timedelta(weeks=interval)
    elif freq == "MONTHLY":
        def gen():
            y, m = sday.year, sday.month
            while True:
                if not r.get("BYMONTH") or str(m) in r["BYMONTH"].split(","):
                    yield [dt.date(y, m, d) for d in _month_days(y, m, r, sday)]
                else:
                    yield []
                m += interval
                while m > 12:
                    m -= 12
                    y += 1
    elif freq == "YEARLY":
        months = [int(x) for x in (r.get("BYMONTH") or "").split(",") if x.strip().isdigit()] or [sday.month]

        def gen():
            y = sday.year
            while True:
                ds = []
                for m in months:
                    if r.get("BYDAY") or r.get("BYMONTHDAY"):
                        ds += [dt.date(y, m, d) for d in _month_days(y, m, r, sday)]
                    else:
                        try:
                            ds.append(dt.date(y, m, sday.day))
                        except ValueError:
                            pass
                yield sorted(ds)
                y += interval
    else:
        return [start] if lo <= sday <= hi else []
    for days in gen():
        periods += 1
        if periods > 5000:
            break
        stop = False
        for d in days:
            if d < sday:
                continue
            if (until and d > until) or d > hi:
                stop = True
                break
            n += 1
            if count is not None and n > count:
                stop = True
                break
            occ = make(d)
            if _occ_key(occ) not in ev["exdates"] and _occ_key(d) not in ev["exdates"] and d >= lo:
                found.append(occ)
                if len(found) >= MAX_OCCURRENCES:
                    stop = True
                    break
        if stop:
            break
    for x in ev.get("rdates") or []:
        if lo <= day_of(x) <= hi and _occ_key(x) not in ev["exdates"]:
            found.append(x)
    return sorted(set(found), key=lambda x: (day_of(x), fmt_time(x)))


def ics_events(text: str, lo: dt.date, hi: dt.date, rrule_hi: dt.date | None = None) -> tuple[str, list[dict]]:
    """(calendar name, [{title, start, all_day, uid, location, url}]) of an iCalendar file within [lo, hi]: recurring
    events expanded (to rrule_hi at most), a moved occurrence (RECURRENCE-ID) in place of the one it replaces, cancelled
    events left out."""
    cal = parse_ics(text)
    rrule_hi = min(hi, rrule_hi or hi)
    moved: dict[str, set] = {}
    for e in cal["events"]:
        if e["recurrence_id"] is not None and e["uid"]:
            moved.setdefault(e["uid"], set()).update({_occ_key(e["recurrence_id"]), _occ_key(day_of(e["recurrence_id"]))})
    out = []
    for e in cal["events"]:
        if e["status"] == "CANCELLED":
            continue
        if e["recurrence_id"] is None and e["rrule"]:
            e = dict(e, exdates=set(e["exdates"]) | moved.get(e["uid"], set()))
            starts = occurrences(e, lo, rrule_hi)
        else:
            starts = [e["start"]] if lo <= day_of(e["start"]) <= hi else []
        for s in starts:
            out.append({"title": e["summary"], "start": s, "all_day": e["all_day"] or not isinstance(s, dt.datetime),
                        "uid": e["uid"], "location": e["location"], "url": e["url"], "recurring": bool(e["rrule"])})
    return cal["name"], out


# ----------------------------------------------------------------------------- platform pages and feeds
def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html or "", "lxml")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(s or "")).strip()


OCC = re.compile(r"^(\d+)_(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)_(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$")
FS_TITLE = re.compile(r"\bfsCalendar(?:Event)?Title\b|\bfsCalendarEventLink\b")


def parse_finalsite(html: str) -> tuple[list[dict], tuple[dt.date, dt.date] | None]:
    """([{title, start, all_day, uid, location}], the month the page's grid shows, or None) of a Finalsite calendar page
    as served (see the module docstring for the markup)."""
    soup = _soup(html)
    out, seen = [], set()
    month_days = []
    for box in soup.select(".fsCalendarDaybox"):
        if "fsCalendarOutOfRange" in (box.get("class") or []):
            continue
        dd = box.select_one(".fsCalendarDate")
        if dd is not None and dd.get("data-year"):
            try:
                month_days.append(dt.date(int(dd["data-year"]), int(dd["data-month"]) + 1, int(dd["data-day"])))
            except (KeyError, ValueError):
                pass
    for el in soup.find_all(class_=FS_TITLE):
        if el.find_parent(class_=FS_TITLE) is not None:
            continue                                   # the link inside a title element: the same event
        title = _clean(el.get_text(" ", strip=True)) or _clean(el.get("title") or "")
        if not title or re.match(r"subscribe to calendar", title, re.I):
            continue
        box = el.find_parent(class_="fsCalendarInfo") or el.find_parent("article") or el.parent
        start, all_day = None, False
        tm = box.find("time", class_=re.compile(r"fsStartTime")) if box is not None else None
        td = box.find("time", class_=re.compile(r"fsDate")) if box is not None else None
        if tm is not None and tm.get("datetime"):
            start = _iso_local(tm["datetime"])
        occ = el.get("data-occur-id") or ""
        if not occ and box is not None:
            a = box.find(attrs={"data-occur-id": True})
            occ = a.get("data-occur-id", "") if a is not None else ""
        daybox = el.find_parent(class_="fsCalendarDaybox")
        if start is None and daybox is not None:
            dd = daybox.select_one(".fsCalendarDate")
            try:
                start = dt.date(int(dd["data-year"]), int(dd["data-month"]) + 1, int(dd["data-day"]))
                all_day = True
            except (TypeError, KeyError, ValueError):
                start = None
        if start is None and td is not None and td.get("datetime"):
            d = _iso_local(td["datetime"])
            if d is not None:
                start, all_day = (d.date(), True) if (d.hour, d.minute) == (0, 0) else (d, False)
        m = OCC.match(occ)
        if start is None and m:
            a, b = m.group(2), m.group(3)
            if a.endswith("T00:00:00Z") and b.endswith("T00:00:00Z"):
                start, all_day = dt.date.fromisoformat(a[:10]), True       # a day event: the date as written
            else:
                start = to_local(dt.datetime.fromisoformat(a.replace("Z", "+00:00")))
        if start is None:
            continue
        loc = box.select_one(".fsLocation") if box is not None else None
        uid = f"fs:{m.group(1) if m else occ}" if occ else ""
        k = (day_of(start).isoformat(), title.lower())
        if k in seen:
            continue                                   # a grid's day view and month view list each event twice
        seen.add(k)
        out.append({"title": title, "start": start, "all_day": all_day or not isinstance(start, dt.datetime), "uid": uid,
                    "location": _clean(loc.get_text(" ", strip=True))[:200] if loc is not None else "", "url": ""})
    rng = (min(month_days), max(month_days)) if month_days else None
    return out, rng


EDLIO_EVENT = re.compile(r"/apps/events/(20\d\d)/(\d{1,2})/(\d{1,2})/(\d+)")
TIME_TXT = re.compile(r"\b(\d{1,2}(?::\d\d)?\s*[ap]\.?\s?m\.?)", re.I)


def parse_edlio(html: str, base: str) -> list[dict]:
    """The events an Edlio home page lists: links /apps/events/YYYY/M/D/<REC_ID>/ (date in the address, title as the
    link's text), the time from the text beside the link ("Time: 4:30 PM – 6:30 PM")."""
    soup = _soup(html)
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = EDLIO_EVENT.search(a["href"])
        if not m:
            continue
        title = _clean(a.get_text(" ", strip=True))
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if not title or (d, title.lower()) in seen:
            continue
        seen.add((d, title.lower()))
        start, near = d, ""
        node = a
        for _ in range(4):                       # the event's own block: the link's heading, then its container
            node = node.parent
            if node is None:
                break
            txt = node.get_text(" ", strip=True)
            if len(EDLIO_EVENT.findall(str(node))) > 1:
                break                            # the list of all events: no longer this one's block
            near = txt
        tm = re.search(r"time:\s*" + TIME_TXT.pattern, near, re.I)
        if tm:
            start = _with_time(d, tm.group(1))
        out.append({"title": title, "start": start, "all_day": not isinstance(start, dt.datetime), "uid": f"edlio:{m.group(4)}",
                    "location": "", "url": urljoin(base, a["href"])})
    return out


def _with_time(d: dt.date, t: str):
    m = re.match(r"(\d{1,2})(?::(\d\d))?\s*([ap])", t.replace(".", "").replace(" ", "").lower())
    if not m:
        return d
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    return dt.datetime.combine(d, dt.time(h, int(m.group(2) or 0)))


def parse_apptegy_json(obj) -> list[dict]:
    """Apptegy's events JSON (.../cms/events): {events: [{id, title, start_at, all_day, venue}], meta}."""
    out = []
    for e in (obj or {}).get("events") or []:
        st = _iso_local(e.get("start_at") or "")
        if st is None:
            continue
        all_day = bool(e.get("all_day"))
        out.append({"title": _clean(e.get("title") or ""), "start": st.date() if all_day else st, "all_day": all_day,
                    "uid": f"apptegy:{e['id']}" if e.get("id") else "", "location": _clean(e.get("venue") or "")[:200], "url": ""})
    return out


def parse_jsonld(html: str) -> list[dict]:
    """schema.org Event objects a page carries in JSON-LD (The Events Calendar and other WordPress plugins write them)."""
    out = []
    for m in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack += x
                continue
            if not isinstance(x, dict):
                continue
            stack += x.get("@graph") or []
            types = x.get("@type") if isinstance(x.get("@type"), list) else [x.get("@type")]
            if not any(str(t or "").endswith("Event") for t in types):
                continue
            st = _iso_local(str(x.get("startDate") or ""))
            if st is None:
                continue
            all_day = len(str(x.get("startDate") or "")) <= 10
            loc = x.get("location")
            loc = loc.get("name", "") if isinstance(loc, dict) else (loc if isinstance(loc, str) else "")
            out.append({"title": _clean(str(x.get("name") or "")), "start": st.date() if all_day else st, "all_day": all_day,
                        "uid": "", "location": _clean(loc)[:200], "url": str(x.get("url") or "")})
    return out


_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
TEXT_DATE = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b|\b(\d{{1,2}})/(\d{{1,2}})/(20\d\d)\b", re.I)


def _text_date(s: str):
    m = TEXT_DATE.search(s or "")
    if not m:
        return None
    try:
        if m.group(1):
            from .agencies import MONTHS

            mon = next(i + 1 for i, x in enumerate(MONTHS) if x.startswith(m.group(1).lower()[:3]))
            d = dt.date(int(m.group(3)), mon, int(m.group(2)))
        else:
            d = dt.date(int(m.group(6)), int(m.group(4)), int(m.group(5)))
    except (ValueError, StopIteration):
        return None
    rest = (s or "")[m.end():m.end() + 40]
    t = TIME_TXT.search(rest)
    return _with_time(d, t.group(1)) if t else d


def parse_rss(text: str) -> list[dict]:
    """An RSS or Atom feed of events: the event's date from an event field (ev:startdate, xCal:dtstart, startDate ...),
    else the first full date in its title or description (a feed's own pubDate is when the item was posted, not the
    meeting's day, and is not used)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text or "", "xml")
    out = []
    for it in soup.find_all(["item", "entry"]):
        title = _clean((it.find("title").get_text(" ", strip=True) if it.find("title") else ""))
        start = None
        for tag in it.find_all(True):
            nm = tag.name.split(":")[-1].lower()
            if nm in ("startdate", "dtstart", "start", "eventdate", "event_date", "startdatetime", "start_date"):
                v = tag.get_text(strip=True)
                start = _iso_local(v) if re.match(r"\d{4}-\d\d-\d\d", v) else (_text_date(v) or None)
                if start is not None:
                    if isinstance(start, dt.datetime) and (start.hour, start.minute) == (0, 0) and len(v) <= 10:
                        start = start.date()
                    break
        if start is None:
            desc = it.find(["description", "summary", "content"])
            start = _text_date(title) or _text_date(desc.get_text(" ", strip=True) if desc else "")
        if start is None or not title:
            continue
        link = it.find("link")
        url = (link.get("href") or link.get_text(strip=True)) if link is not None else ""
        out.append({"title": title, "start": start, "all_day": not isinstance(start, dt.datetime), "uid": "", "location": "",
                    "url": url if str(url).startswith("http") else ""})
    return out


# ----------------------------------------------------------------------------- finding the feeds
PLATFORMS = [("finalsite", re.compile(r"finalsite|\bfsElement\b|/fs/resource-manager", re.I)),
             ("apptegy", re.compile(r"apptegy|thrillshare", re.I)),
             ("edlio", re.compile(r"edlio", re.I)),
             ("smartsites", re.compile(r"smartsites\.parentsquare", re.I)),
             ("schoolbundle", re.compile(r"SchoolBundle|cicmsapi\.azurewebsites|sb45storage", re.I)),
             ("blackboard", re.compile(r"schoolwires|/site/Default\.aspx\?PageID|/site/handlers/|Blackboard Web Community", re.I)),
             ("catapult", re.compile(r"catapultcms", re.I)),
             ("schoolpointe", re.compile(r"schoolpointe", re.I)),
             ("schoolinsites", re.compile(r"schoolinsites", re.I)),
             ("wordpress", re.compile(r"/wp-content/|/wp-includes/", re.I))]
GOOGLE_EMBED = re.compile(r"(?:calendar|www)\.google\.com/calendar/(?:[a-z]/\d+/)?embed\?([^\"'\s<>]+)", re.I)
GOOGLE_ICS = re.compile(r"calendar\.google\.com/calendar/ical/([^/\"'\s<>]+)/public/basic\.ics", re.I)
GOOGLE_CID = re.compile(r"calendar\.google\.com/calendar/(?:[a-z]/\d+/)?r?\??(?:[^\"'\s<>]*[?&])?cid=([A-Za-z0-9_\-=%.@]+)", re.I)
THRILL_ORG = re.compile(r"thrillshare-cmsv2\.services\.thrillshare\.com/api/v4/o/(\d+)/cms/events", re.I)
HREF = re.compile(r"""(?:href|src|data-url|data-src)\s*=\s*["']([^"']+)["']""", re.I)
ICS_LINK = re.compile(r"\.ics(?:$|[?#])|^webcal:|[?&]ical=1\b|icalfeed\.ashx|generate_ical|/ical/?(?:$|[?#])|/ical\.aspx|"
                      r"[?&]format=ical", re.I)
RSS_LINK = re.compile(r"rss|\.xml(?:$|\?)|/feed/?(?:$|\?)", re.I)
CAL_WORDS = re.compile(r"calendar|events?\b|board|\bboe\b|meetings?", re.I)
PAGE_NOT = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|jpe?g|png|gif|zip|mp[34])(?:$|[?#])|athletic|lunch|menu|testing|assessment|"
                      r"employee|work-?calendar|bell|bus\b|transportation|directory|polic(?:y|ies)|minutes|agenda|videos?|"
                      r"livestream|youtube|vimeo|member|zone|map\b|vacanc|election|login|sign-?in|survey|registration|"
                      r"enroll|employment|careers?|jobs?|staff", re.I)


def platform_of(html: str) -> str:
    head = (html or "")[:400000]
    return next((name for name, rx in PLATFORMS if rx.search(head)), "")


def _host(u: str) -> str:
    return urlsplit(u).netloc.lower().split(":")[0].removeprefix("www.")


def _canonical(html: str, url: str) -> str:
    m = re.search(r"""<link[^>]+rel=["']canonical["'][^>]*href=["']([^"']+)["']""", html or "", re.I) or \
        re.search(r"""<link[^>]+href=["']([^"']+)["'][^>]*rel=["']canonical["']""", html or "", re.I)
    return urljoin(url, htmlmod.unescape(m.group(1))) if m else url


def _template(u: str) -> str:
    return re.sub(r"\d+", "#", u)


def _gcal_id_from_cid(cid: str) -> str:
    cid = unquote(cid)
    if "@" in cid or cid.endswith(".com"):
        return cid
    try:
        raw = base64.urlsafe_b64decode(cid + "=" * (-len(cid) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    return raw if re.fullmatch(r"[\w.\-]+@[\w.\-]+|[\w.\-]+\.(?:calendar\.google\.com|gmail\.com)", raw) else ""


def gcal_ics(cal_id: str) -> str:
    return GCAL_ICS.format(id=quote(unquote(cal_id), safe=""))


def find_feeds(html: str, page_url: str, platform: str = "") -> list[dict]:
    """The calendar feeds a page offers: [{url, kind, platform, found_on}]. kind is 'ics' (an iCalendar file: .ics,
    webcal://, a Google Calendar embed's public address, Apptegy's generate_ical, Blackboard's icalfeed.ashx, WordPress's
    ?ical=1), 'apptegy_json', 'finalsite_page' (the page's own calendar, as served), 'edlio_page', 'jsonld_page' or 'rss'."""
    text = htmlmod.unescape(html or "")
    platform = platform or platform_of(html)
    page = _canonical(html, page_url)
    out: list[dict] = []
    seen: set[str] = set()

    def add(u: str, kind: str, plat: str):
        u = u.strip()
        if u.lower().startswith("webcal://"):
            u = "https://" + u[9:]
        if not u.startswith("http") or u in seen or any(h in _host(u) for h in NEVER_HOSTS):
            return
        seen.add(u)
        out.append({"url": u, "kind": kind, "platform": plat, "found_on": page})

    # Google Calendar: embeds, direct public addresses, "add to Google Calendar" links
    for m in GOOGLE_EMBED.finditer(text):
        for cid in parse_qs(m.group(1).replace("&amp;", "&")).get("src", []):
            if cid and not cid.startswith("http"):
                add(gcal_ics(cid), "ics", "google")
    for m in GOOGLE_ICS.finditer(text):
        add(gcal_ics(m.group(1)), "ics", "google")
    for m in GOOGLE_CID.finditer(text):
        cid = _gcal_id_from_cid(m.group(1))
        if cid:
            add(gcal_ics(cid), "ics", "google")
    # Apptegy: the org's events, as its own "Click to Download Calendar" link gives them
    for org in list(dict.fromkeys(THRILL_ORG.findall(text)))[:2]:
        add(THRILL.format(org=org) + "/generate_ical?filter_ids&section_ids", "ics", "apptegy")
    # links to iCalendar files and event feeds (one per address pattern: a per-event "add to my calendar" link repeated
    # for every event is not a feed)
    hrefs = [urljoin(page_url, h.strip()) if not h.lower().startswith("webcal:") else h.strip() for h in HREF.findall(text)]
    per_tpl: dict[str, int] = {}
    for h in hrefs:
        if ICS_LINK.search(h):
            per_tpl[_template(h)] = per_tpl.get(_template(h), 0) + 1
    for h in hrefs:
        low = h.lower()
        path = urlsplit(h.replace("webcal://", "https://")).path.lower()
        if platform == "finalsite" and path.startswith("/fs/") and _host(h) == _host(page):
            continue                         # Finalsite's robots.txt: Disallow /fs/ (its exports); the page is read instead
        if platform == "edlio" and path.startswith("/apps/"):
            continue                         # behind Edlio's browser check
        if "calendar.google.com" in low or "thrillshare" in low:
            continue                         # handled above
        if ICS_LINK.search(h) and per_tpl.get(_template(h), 0) <= 3:
            add(h, "ics", "apptegy" if "thrillshare" in low else "blackboard" if "icalfeed.ashx" in low else
                "wordpress" if "ical=1" in low else platform or "ics")
    for m in re.finditer(r"""<link[^>]+type=["']application/(?:rss|atom)\+xml["'][^>]*>""", html or "", re.I):
        tag = m.group(0)
        hm = re.search(r"""href=["']([^"']+)["']""", tag, re.I)
        tm = re.search(r"""title=["']([^"']*)["']""", tag, re.I)
        if hm and re.search(r"calendar|event", (hm.group(1) + " " + (tm.group(1) if tm else "")), re.I):
            add(urljoin(page_url, htmlmod.unescape(hm.group(1))), "rss", platform or "rss")
    for h in hrefs:
        if RSS_LINK.search(h) and re.search(r"calendar|event", h, re.I) and not ICS_LINK.search(h) \
                and not (platform == "finalsite" and urlsplit(h).path.lower().startswith("/fs/")) \
                and not (platform == "edlio" and urlsplit(h).path.lower().startswith("/apps/")):
            add(h, "rss", platform or "rss")
    # the page's own calendar, where its HTML carries the events
    if "fsCalendar" in (html or "") and parse_finalsite(html)[0]:
        add(page, "finalsite_page", "finalsite")
    if EDLIO_EVENT.search(html or ""):
        add(page, "edlio_page", "edlio")
    if "application/ld+json" in (html or "") and parse_jsonld(html):
        add(page, "jsonld_page", platform or "jsonld")
    return out


def calendar_links(html: str, page_url: str, hosts: set[str], platform: str = "") -> list[str]:
    """The district's own pages worth opening for a calendar: links whose text or address says calendar, events, board
    or meetings, best first (a board calendar, then calendars, then events and board meetings)."""
    soup = _soup(html)
    scored: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        u = urljoin(page_url, a["href"]).split("#")[0]
        if not u.startswith("http") or _host(u) not in hosts:
            continue
        path = urlsplit(u).path.lower()
        if (platform == "finalsite" and path.startswith("/fs/")) or (platform == "edlio" and path.startswith("/apps/")):
            continue
        lab = _clean(a.get_text(" ", strip=True))
        hay = f"{lab} {path.replace('-', ' ').replace('_', ' ')}"
        if not CAL_WORDS.search(hay) or PAGE_NOT.search(path) or PAGE_NOT.search(lab) or len(lab) > 60:
            continue
        s = 0
        if re.search(r"calendar", hay, re.I):
            s += 2
        if re.search(r"\bboard\b|\bboe\b", hay, re.I):
            s += 1
            if re.search(r"calendar|meeting|schedule", hay, re.I):
                s += 2
        if re.search(r"\bevents?\b", hay, re.I):
            s += 1
        if s:
            scored[u] = max(scored.get(u, 0), s)
    home = page_url.rstrip("/")
    return [u for u, _ in sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0]))) if u.rstrip("/") != home]


def _board_place(u: str, title: str = "") -> bool:
    """A page that is the board's own calendar ("/boe/boe-calendar", "/board/calendar", "Board of Education Calendar"),
    not any board page (a board page's events box can list the whole district's events)."""
    hay = f"{urlsplit(u).path.replace('-', ' ').replace('_', ' ').replace('/', ' ')} | {title}"
    return any(re.search(r"\bboard\b|\bboe\b", part, re.I) and re.search(r"calendar|meetings?\b|schedule", part, re.I)
               for part in hay.split("|"))


# ----------------------------------------------------------------------------- state and seed
def load_seed(root: Path) -> dict:
    from .registry import load_yaml

    return (load_yaml(root / "config" / SEED_NAME) or {}).get("districts") or {}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:50]


def _with_param(u: str, k: str, v: str) -> str:
    p = urlsplit(u)
    q = [(a, b) for a, b in parse_qs(p.query, keep_blank_values=True).items()]
    flat = [(a, x) for a, bs in q for x in bs if a != k] + [(k, v)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(flat), ""))


def _month(today: dt.date, offset: int) -> dt.date:
    y, m = today.year, today.month + offset
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return dt.date(y, m, 1)


def stable_uid(uid: str) -> bool:
    """A UID that names the event the same way on every read: Google's and Outlook's (…@google.com, …@Cobb.sbc), and
    the platforms' own event numbers this reader makes (fs:, edlio:, apptegy:). Apptegy's iCalendar UIDs are random
    UUIDs made anew on every request and are not."""
    return bool(uid) and len(uid) < 300 and ("@" in uid or re.match(r"(?:fs|edlio|apptegy):\d", uid) is not None)


class Calendars:
    def __init__(self, fetcher, store, root: Path, log=print, workers: int = 6, archive: bool = True, today: dt.date | None = None,
                 wayback=None, subscriber=None):
        self.f, self.store, self.root, self.log = fetcher, store, Path(root), log
        # the public calendars a district publishes on Google Calendar, subscribed to as a calendar application does
        # (gacrawl/calendar_subscriber.py); None: those feeds come only from the Internet Archive's copies
        self.sub = subscriber
        self.workers = max(1, int(workers))
        self.today = today or dt.date.today()
        self.lo = self.today - dt.timedelta(days=BACK_DAYS)
        self.hi = self.today + dt.timedelta(days=AHEAD_DAYS)
        self.rrule_hi = self.today + dt.timedelta(days=RRULE_AHEAD_DAYS)
        self.path = self.root / "state" / STATE_NAME
        from .registry import load_json

        self.state = load_json(self.path, {})
        self.state.pop("_about", None)
        self.seed = load_seed(self.root)
        self._lock = threading.Lock()
        self.archive_on = archive
        self.archive_left = ARCHIVE_PER_RUN
        self._wb = wayback
        self.deadline: float | None = None
        self.stopped = ""
        self.stats = {"districts": 0, "looked": 0, "feeds_read": 0, "events": 0, "meetings": 0, "upcoming": 0, "removed": 0,
                      "refused": 0, "archived_reads": 0, "errors": 0}

    # ------------------------------------------------------------------ requests
    def _out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def get(self, url: str):
        if self._out_of_time():
            raise OutOfTime("district calendars: the step's time is up")
        return self.f.get(url)

    def wayback(self):
        if self._wb is None:
            from .wayback import Wayback

            self._wb = Wayback(self.f, log=self.log)
        return self._wb

    # ------------------------------------------------------------------ looking for feeds
    def site_of(self, did: str, j: dict) -> str:
        return str((self.seed.get(did) or {}).get("site") or j.get("website") or "").strip()

    def discover(self, did: str, j: dict) -> dict:
        """Open the district's home page and its calendar, events and board pages; the feeds they offer."""
        st = dict(self.state.get(did) or {})
        site = self.site_of(did, j)
        st["site"] = site
        found: list[dict] = []
        pages: list[str] = []
        note = ""
        if not site:
            note = "no website in the registry or the seed file"
        else:
            try:
                r = self.get(site)
                if r.status != 200 or "html" not in (r.content_type or "html"):
                    note = f"home page: HTTP {r.status}"
                else:
                    home = _canonical(r.text, r.url)
                    plat = platform_of(r.text)
                    st["platform"] = plat or st.get("platform") or ""
                    hosts = {_host(site), _host(r.url), _host(home)} - {""}
                    pages.append(home)
                    found += find_feeds(r.text, home, plat)
                    todo = calendar_links(r.text, home, hosts, plat)
                    if plat == "apptegy" and not any(x["platform"] == "apptegy" and x["kind"] == "ics" for x in found):
                        todo.insert(0, urljoin(home, "/events"))
                    for u in todo[:MAX_PAGES]:
                        try:
                            p = self.get(u)
                        except (Disallowed, Blocked) as exc:
                            note = f"{type(exc).__name__}: {str(exc)[:120]}"
                            continue
                        except OutOfTime:
                            raise
                        except Exception as exc:  # noqa: BLE001 — one page that fails: the others still count
                            note = f"{type(exc).__name__} on {u[:80]}"
                            continue
                        if p.status != 200 or "html" not in (p.content_type or "html"):
                            continue
                        pages.append(_canonical(p.text, p.url))
                        title = re.search(r"<title[^>]*>(.*?)</title>", p.text or "", re.S | re.I)
                        for x in find_feeds(p.text, p.url, plat):
                            x["board"] = _board_place(x["found_on"], title.group(1) if title else "")
                            found.append(x)
            except (Disallowed, Blocked) as exc:
                note = f"home page refused: {type(exc).__name__}: {str(exc)[:140]}"
            except OutOfTime:
                raise
            except Exception as exc:  # noqa: BLE001 — the site is down or answers oddly: next week
                note = f"home page: {type(exc).__name__}: {str(exc)[:120]}"
        # the seed's feeds first, then the old ones still listed, then what was found now
        feeds: dict[str, dict] = {}
        old = {x["url"]: x for x in st.get("feeds") or []}
        for x in (self.seed.get(did) or {}).get("feeds") or []:
            if x.get("url"):
                u = "https://" + x["url"][9:] if str(x["url"]).lower().startswith("webcal://") else x["url"]
                feeds[u] = {**old.get(u, {}), "url": u, "kind": x.get("kind") or "ics", "platform": x.get("platform") or "",
                            "found_on": x.get("found_on") or u, "board": bool(x.get("board")), "source": "seed",
                            "checked_by_hand": str(x.get("checked") or "")}
        for x in found:
            if x["url"] in feeds:
                feeds[x["url"]]["board"] = bool(feeds[x["url"]].get("board") or x.get("board"))   # also on the board's calendar
                continue
            prev = old.get(x["url"], {})
            feeds[x["url"]] = {**prev, **x, "board": bool(x.get("board") or prev.get("board")), "source": prev.get("source") or "found",
                               "first_seen": prev.get("first_seen") or self.today.isoformat()}
        for u, x in old.items():
            if u not in feeds and x.get("source") == "found" and x.get("board_events"):
                feeds[u] = x                    # a feed that gave meetings is kept though this look missed it
        if not feeds and (self.seed.get(did) or {}).get("note"):
            note = "; ".join(x for x in (note, str(self.seed[did]["note"])) if x)
        st.update({"feeds": list(feeds.values()), "checked": self.today.isoformat(), "pages": pages[:MAX_PAGES + 1],
                   "note": note})
        return st

    # ------------------------------------------------------------------ reading
    def _archive_copy(self, url: str, feed: dict) -> tuple[str, dict] | None:
        """The newest Internet Archive capture of a refused feed from the last ARCHIVE_RECENT_DAYS: (text, meta) or None.
        Only existing captures (never Save Page Now), at the Archive's pace (gacrawl/wayback.py)."""
        from .wayback import CDX, RAW, SHOW, day

        if not self.archive_on:
            return None
        last = (feed.get("archive") or {}).get("checked") or ""
        if last and (self.today - dt.date.fromisoformat(last)).days < ARCHIVE_RECHECK_DAYS:
            return None
        with self._lock:
            if self.archive_left <= 0:
                return None
            self.archive_left -= 1
        wb = self.wayback()
        since = (self.today - dt.timedelta(days=ARCHIVE_RECENT_DAYS)).strftime("%Y%m%d")
        r = wb._get(CDX, [("url", url), ("output", "json"), ("fl", "timestamp,original"), ("filter", "statuscode:200"),
                          ("from", since), ("limit", "-1")])
        feed["archive"] = {"checked": self.today.isoformat(), "found": ""}
        if r is None or r.status != 200:
            return None
        try:
            rows = r.json()
        except ValueError:
            return None
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        cap = dict(zip(rows[0], rows[-1]))
        got = wb._get(RAW.format(ts=cap["timestamp"], url=cap["original"]))
        if got is None or got.status != 200 or "BEGIN:VCALENDAR" not in (got.text or "")[:2000]:
            return None
        feed["archive"]["found"] = day(cap["timestamp"])
        return got.text, {"archived": True, "captured": day(cap["timestamp"]), "via": "wayback",
                          "archive_copy": SHOW.format(ts=cap["timestamp"], url=cap["original"])}

    def read_feed(self, did: str, feed: dict, taken: dict) -> int:
        """Read one feed: its board meetings stored, its record in the state updated. The meetings stored."""
        kind, url = feed.get("kind") or "ics", feed["url"]
        events: list[dict] = []
        extra: dict = {}
        covered: list[tuple[dt.date, dt.date]] = []      # the days this read saw in full (stale meetings there go)
        page_of: dict[int, str] = {}
        try:
            if kind == "finalsite_page":
                rot = int(feed.get("rot") or 0)
                home = urlsplit(url).path in ("", "/")
                # a home page's calendar is its next few events: read as it is; a calendar page month by month
                offs = [None] if home else list(FS_EVERY_RUN) + [FS_ROTATE[rot % len(FS_ROTATE)]]
                feed["rot"] = rot + (not home)
                answered, status = 0, 0
                for off in offs:
                    first = _month(self.today, off or 0)
                    if first < _month(self.lo, 0) or first > self.hi:
                        continue
                    mu = url if off is None else _with_param(url, "cal_date", first.isoformat())
                    r = self.get(mu)
                    if r.status != 200:
                        status = r.status
                        continue
                    answered += 1
                    evs, rng = parse_finalsite(r.text)
                    for e in evs:
                        page_of[id(e)] = mu
                    events += evs
                    if rng:
                        covered.append(rng)
                if not answered and status:
                    raise ValueError(f"HTTP {status}")
            else:
                if kind == "ics" and self.sub is not None and self.sub.handles(url):
                    if self._out_of_time():
                        raise OutOfTime("district calendars: the step's time is up")
                    r = self.sub.get(url, feed)
                    if r is None:
                        return 0                                  # not due yet (or not a published address): as it was
                    if r.unchanged:
                        feed["status"], feed["read_at"] = "ok (unchanged since the last read)", _now()
                        return 0
                else:
                    r = self.get(url)
                if r.status != 200:
                    raise ValueError(f"HTTP {r.status}")
                body = r.text
                if kind == "ics" and "BEGIN:VCALENDAR" not in body[:5000]:
                    raise ValueError("not an iCalendar file")     # a page in its place (a moved feed, a sign-in page)
                if "BEGIN:VCALENDAR" in body[:5000]:
                    name, events = ics_events(body, self.lo, self.hi, self.rrule_hi)
                    if name and re.search(r"\bboard\b|\bboe\b", name, re.I):
                        feed["board"] = True
                    if events:
                        covered.append((self.today, self.hi))
                elif kind == "apptegy_json":
                    events = parse_apptegy_json(r.json())
                elif kind == "edlio_page":
                    events = parse_edlio(body, url)
                elif kind == "jsonld_page":
                    events = parse_jsonld(body)
                elif kind == "rss":
                    events = parse_rss(body)
            feed["status"] = "ok"
        except (Disallowed, Blocked) as exc:
            feed["status"] = f"refused: {type(exc).__name__}: {str(exc)[:160]}"
            with self._lock:
                self.stats["refused"] += 1
            if kind == "ics":
                got = self._archive_copy(url, feed)
                if got:
                    _, events = ics_events(got[0], self.lo, self.hi, self.rrule_hi)
                    extra = got[1]
                    with self._lock:
                        self.stats["archived_reads"] += 1
        except OutOfTime:
            raise
        except Exception as exc:  # noqa: BLE001 — a feed that fails: the others still count
            feed["status"] = f"error: {type(exc).__name__}: {str(exc)[:160]}"
            with self._lock:
                self.stats["errors"] += 1
        feed["read_at"] = _now()
        stored = self.store_events(did, feed, events, extra, covered, page_of, taken)
        with self._lock:
            self.stats["feeds_read"] += 1
            self.stats["events"] += len(events)
        return stored

    def store_events(self, did: str, feed: dict, events: list[dict], extra: dict, covered, page_of: dict, taken: dict) -> int:
        sid = f"{did}:calendar"
        keys, n, up = set(), 0, 0
        board_feed = bool(feed.get("board"))
        for e in events:
            d = day_of(e["start"])
            if not self.lo <= d <= self.hi:
                continue
            what = board_meeting(e["title"], board_feed)
            if not what:
                continue
            day = d.isoformat()
            tm = "" if e.get("all_day") else fmt_time(e["start"])
            same = (day, re.sub(r"[^a-z0-9]+", "", e["title"].lower()))
            with self._lock:
                owner = taken.get(same)
                if owner and owner != feed["url"]:
                    continue                    # the same meeting from another of the district's feeds, read first
                taken[same] = feed["url"]
            ident = e["uid"] if stable_uid(e.get("uid") or "") else _slug(e["title"]) + (f"|{tm}" if tm else "")
            key = f"{sid}|meeting|cal|{day}|{ident}"
            if key in keys:
                continue
            keys.add(key)
            link = e.get("url") if str(e.get("url") or "").startswith("http") and not ICS_LINK.search(e.get("url") or "") else ""
            url = link or page_of.get(id(e)) or (feed.get("found_on") if feed.get("kind") != "finalsite_page" else "") or feed["url"]
            meta = {"scheduled": True, "EventDate": day, "EventBodyName": "Board of Education", "time": tm, "provider": PROVIDER,
                    "platform": feed.get("platform") or "", "from_feed": feed["url"], "found_on": feed.get("found_on") or "",
                    "meeting_type": what, "all_day": bool(e.get("all_day")), "opened": True}
            if e.get("location"):
                meta["location"] = e["location"]
            if stable_uid(e.get("uid") or ""):
                meta["uid"] = e["uid"]
            if e.get("recurring"):
                meta["recurring"] = True
            meta.update(extra)
            line = " · ".join(x for x in (e["title"], tm, e.get("location") or "") if x)
            self.store.add(Record(sid, "meeting", url, title=e["title"][:200], text=line[:400], published=day, jurisdiction=did,
                                  channel="archive" if extra.get("archived") else "html" if feed.get("kind", "").endswith("_page") else "feed",
                                  evidence="primary", key=key, meta=meta))
            n += 1
            up += day >= self.today.isoformat()
        removed = 0
        if covered and events and not extra.get("archived"):
            # a future meeting this feed listed before and does not now, on days it showed in full: moved or cancelled
            t0 = self.today.isoformat()
            for r in self.store.raw("source_id=? AND kind='meeting' AND published>=? AND json_extract(meta,'$.from_feed')=?",
                                    (sid, t0, feed["url"]), cols="rid, key, published"):
                p = str(r["published"] or "")[:10]
                if r["key"] in keys or not any(a.isoformat() <= p <= b.isoformat() for a, b in covered):
                    continue
                removed += self.store.delete_where("rid=?", (r["rid"],))
        feed["events"] = len(events)
        feed["board_events"] = n
        feed["upcoming"] = up
        with self._lock:
            self.stats["meetings"] += n
            self.stats["upcoming"] += up
            self.stats["removed"] += removed
        return n

    # ------------------------------------------------------------------ a district
    def district(self, did: str, j: dict, look: bool) -> None:
        if self._out_of_time():
            return
        try:
            st = self.discover(did, j) if look else dict(self.state.get(did) or {})
            if look:
                with self._lock:
                    self.stats["looked"] += 1
            taken: dict = {}
            for feed in sorted(st.get("feeds") or [], key=lambda x: (x.get("source") != "seed", not x.get("board"), x["url"])):
                if self._out_of_time():
                    raise OutOfTime("district calendars: the step's time is up")
                if str(feed.get("status") or "").startswith("refused") and not look:
                    continue                    # refused: asked again (and its Archive copy looked up) at next week's look
                self.read_feed(did, feed, taken)
            st["board_events"] = sum(int(x.get("board_events") or 0) for x in st.get("feeds") or [])
            st["upcoming"] = sum(int(x.get("upcoming") or 0) for x in st.get("feeds") or [])
            st["read_at"] = _now()
            with self._lock:
                self.state[did] = st
                self.stats["districts"] += 1
        except OutOfTime as exc:
            self.stopped = str(exc)[:200]
        except Exception as exc:  # noqa: BLE001 — one district's oddity must not end the step for the others
            self.log(f"district calendars: {did}: {type(exc).__name__}: {str(exc)[:200]}")
            with self._lock:
                self.stats["errors"] += 1

    def run(self, jurisdictions: list[dict], deadline: float | None = None) -> dict:
        self.deadline = deadline
        dists = {j["id"]: j for j in jurisdictions if j.get("kind") == "school_district"}

        def due(did: str) -> bool:
            c = (self.state.get(did) or {}).get("checked") or ""
            seeded = {re.sub(r"(?i)^webcal://", "https://", str(x.get("url") or ""))
                      for x in (self.seed.get(did) or {}).get("feeds") or []}
            have = {x.get("url") for x in (self.state.get(did) or {}).get("feeds") or []}
            return (not c) or (self.today - dt.date.fromisoformat(c)).days >= REDISCOVER_DAYS or bool(seeded - have)

        # districts with feeds already found first (a read is a request or a few), then those due a look, the ones
        # never looked at before those looked at longest ago
        order = sorted(dists, key=lambda d: (due(d), (self.state.get(d) or {}).get("checked") or "", d))
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            for fut in [pool.submit(self.district, did, dists[did], due(did)) for did in order]:
                fut.result()
        except KeyboardInterrupt:
            # the step's watchdog (SIGINT): no new request, nothing more started; what was read is kept
            self.stopped = "interrupted"
            self.deadline = time.monotonic() - 1
            pool.shutdown(wait=False, cancel_futures=True)
        finally:
            pool.shutdown(wait=False)
            self.save()
        self.store.flush()
        st = self.state
        by_platform: dict[str, int] = {}
        for v in st.values():
            if isinstance(v, dict) and v.get("feeds"):
                p = v.get("platform") or (v["feeds"][0].get("platform") or "other")
                by_platform[p] = by_platform.get(p, 0) + 1
        return {**self.stats, "stopped": self.stopped,
                "with_feeds": sum(1 for v in st.values() if isinstance(v, dict) and v.get("feeds")),
                "with_board_meetings": sum(1 for v in st.values() if isinstance(v, dict) and v.get("board_events")),
                "with_upcoming": sum(1 for v in st.values() if isinstance(v, dict) and v.get("upcoming")),
                "by_platform": dict(sorted(by_platform.items()))}

    def save(self) -> None:
        from .registry import write_json_atomic

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            body = {"_about": "School districts' calendar feeds (gacrawl/district_calendars.py): per district the feeds "
                              "found, where, when last read and what each gave. Looked for again weekly.",
                    **{k: self.state[k] for k in sorted(self.state)}}
        write_json_atomic(self.path, body, indent=1, ensure_ascii=False)


def run(fetcher, store, root: Path, deadline: float | None = None, workers: int = 6, archive: bool = True, log=print,
        subscribe: bool = False) -> dict:
    from . import registry

    sub = None
    if subscribe:
        from .calendar_subscriber import Subscriber

        sub = Subscriber()
    c = Calendars(fetcher, store, root, log=log, workers=workers, archive=archive, subscriber=sub)
    out = c.run(registry.jurisdictions(root), deadline)
    if sub is not None:
        out = {**out, "subscriptions": dict(sub.stats)}
    return out


def summary(root: Path) -> dict:
    """For the coverage report: districts with a calendar feed found, with board meetings read, with upcoming ones."""
    from .registry import load_json

    st = {k: v for k, v in load_json(Path(root) / "state" / STATE_NAME, {}).items() if isinstance(v, dict)}
    feeds = [x for v in st.values() for x in v.get("feeds") or []]
    plat: dict[str, int] = {}
    for v in st.values():
        if v.get("feeds"):
            p = v.get("platform") or v["feeds"][0].get("platform") or "other"
            plat[p] = plat.get(p, 0) + 1
    return {"looked": len(st), "with_feeds": sum(1 for v in st.values() if v.get("feeds")), "feeds": len(feeds),
            "refused": sum(1 for x in feeds if str(x.get("status") or "").startswith("refused")),
            "with_board_meetings": sum(1 for v in st.values() if v.get("board_events")),
            "with_upcoming": sum(1 for v in st.values() if v.get("upcoming")), "by_platform": dict(sorted(plat.items()))}
