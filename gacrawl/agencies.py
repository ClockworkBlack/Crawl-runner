"""Georgia state boards', state authorities' and regional transit authorities' public meetings (config/agencies.yaml),
for Electo's Committee Meetings tab.

HOW A BODY IS ADDED (the whole procedure; nothing else in the code names a body):
  1. An entry in config/agencies.yaml: id, kind (board | authority | transit), name, agency, pages. Most bodies need no
     more: the general reader (`meetings_on`) reads a table of dates with agenda and minutes links (Board of Regents),
     a date heading over its files (DBHDD), a list of files named by date (Corrections), georgia.gov event links with
     the day in the address, a schedule of dates alone under words that say it is a schedule.
  2. Only where a site's layout needs it, a small reader function below, registered by name in READERS (one table,
     at the end of the readers) and named in the entry (`reader: marta_documents`, or per page
     `- {u: <address>, reader: <name>}`). A reader is `def name(html, url, ag, page) -> list[dict]`; it builds its
     meetings with `Book` (book.meet(date, body=..., mk=...), book.file(m, slot, label, href)) and returns
     book.out(). Readers read the page as lines in reading order (`page_lines`), not by a site's CSS classes, so a
     redesign that keeps the words keeps working.
  3. A saved piece of the page and a test in tests/test_agencies*.py.
Entry keys the code understands are listed at the top of config/agencies.yaml.

WHAT IS KEPT. One record per body + committee + day (+ "work session" / "special called", which are sittings of
their own): source `ga-agency:<id>`, kind `meeting`, key `ga-agency:<id>|meeting|<day>[|<committee>][|<sitting>]`,
meta.fmt 2. The meeting's files keep the field names the Legistar meetings use (meta.EventAgendaFile,
EventMinutesFile, docs) and add summary_file (what the body posts as its account of the meeting: a summary, a brief,
"actions"), EventVideoPath, EventTime, what (the meeting's name), committee, status, location, found_on, pub.
A meeting marked cancelled by its page is kept as cancelled, never as held.

A SITE THAT REFUSES AUTOMATED READERS (`source: archive`, or one page `{u: ..., archive: true}`) is read only from
the Wayback Machine's EXISTING copies (gacrawl/wayback.py; never Save Page Now), the newest copy first; the copy's day
is kept (meta.captured) and the export says source "archive" and archive_as_of.

THE EXPORT (export_state_meetings -> state_meetings.json) writes one strict format (STATE_MEETINGS_SCHEMA below, also
written into the bundle's schema.md) and runs `validate_body` / `validate_meeting` on every row: a row that does not
fit is left out and counted in the returned warning, never written malformed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import yaml

from .blocked import Blocked

# a refusal that is a closed door (a login, a paywall), not a public page that turns crawlers away: never read from a copy
CLOSED_REFUSAL = re.compile(r"HTTP 40[12]\b|login|log in|sign in|paywall|subscri", re.I)

FMT = 2                         # meta.fmt of a record written by these readers
# A reader corrected so that it files a page's meetings another way (a committee under another name, a sitting no longer
# split in two) gets a higher number here; a reader not listed is 1. A record written by a reader above 1 keeps its
# number (meta.reader_rev). A page read from the Internet Archive's copies is read again when its reader's number is
# higher than at its last reading, whatever its recheck_days, and a meeting an earlier reader filed from a copy of that
# page is withdrawn when today's reader does not file it and files every one of its files elsewhere (run, below):
# nothing the page holds stops being shown, only the earlier reader's way of filing it.
#   gwcca_about 2 (Nov. 20, 2026): one name per committee (config `committees`), one sitting per committee and day;
#     the labels "09-16 & 17-21 ...", "12-10-24-GWCCA ...", "... MEETING - 04-30-26", a deck kept at Adobe
READER_REV = {"gwcca_about": 2}
_COPY = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})(?:[a-z]{2}_)?/(.+)$", re.I)


def copy_of(page: str) -> tuple[str, str]:
    """(the page's own address, the copy's timestamp) of an address at the Internet Archive; ("", "") of any other."""
    m = _COPY.match(page or "")
    return (m.group(2), m.group(1)) if m else ("", "")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december"]
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
# "November 16, 2026", "November 16,2026", "August, 20 2026", "Sept 19, 2025", "1September 21, 2026" (a stray digit
# glued to the month), "October 6-7, 2026"
DATE_LONG = re.compile(rf"(?<![A-Za-z])({_MON})\.?,?\s+(\d{{1,2}})(?:\s*(?:-|–|&|and)\s*\d{{1,2}})?(?:st|nd|rd|th)?,?\s*(20\d\d)\b", re.I)
DATE_DMY = re.compile(rf"\b(\d{{1,2}})\s+({_MON})\.?,?\s+(20\d\d)\b", re.I)                 # "14 October 2026"
DATE_NUM = re.compile(r"(?<![\d.])(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d|\d\d)(?!\d)")
DATE_ISO = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
DATE_NOYEAR = re.compile(rf"(?<![A-Za-z])({_MON})\.?\s+(\d{{1,2}})\b(?!\s*,?\s*20\d\d)(?!\s*:\d)", re.I)
DATE_8 = re.compile(r"(?<!\d)(0[1-9]|1[0-2])([0-3]\d)(20\d\d)(?!\d)")                        # 11202026 in a file's name
DATE_6 = re.compile(r"(?<!\d)(0[1-9]|1[0-2])([0-3]\d)([12]\d)(?!\d)")
YEAR = re.compile(r"\b(20\d\d)\b")
DOC_RX = re.compile(r"\.(pdf|docx?|pptx?|xlsx?)(?:$|\?)", re.I)
# a file kept on a document service the body links from its page (the Jekyll Island Authority's agendas and minutes)
SHARED_DOC = re.compile(r"^https://(?:acrobat\.adobe\.com/(?:id/urn:aaid:|link/)|drive\.google\.com/file/|[\w.-]*sharepoint\.com/:[a-z]:/)", re.I)
RECORDING = re.compile(r"\b(?:audio|video|recording|watch|livestream|live stream|webcast)\b", re.I)
CANCEL = re.compile(r"\bcancel+ed\b|\bcanceled\b|\bno meeting\b|\bnot meet\b", re.I)
TENTATIVE = re.compile(r"\btentative\b|\bsubject to change\b|\bmay be adjusted\b|\bmay change\b|\btbd\b", re.I)
# which of a meeting's files a link is, by its words. Minutes are the approved record; a "summary", "brief" or
# "actions" file is the body's own account posted before the minutes are approved and is kept apart from them.
KIND_RX = [("cancel", CANCEL),
           ("minutes", re.compile(r"\bminutes\b|\bmins?\b", re.I)),
           ("summary", re.compile(r"summary|\bbriefs?\b|\bactions?\b|\bhighlights\b|\brecap\b", re.I)),
           ("agenda", re.compile(r"agenda|meeting notice|public notice|\bnotice\b", re.I)),
           ("video", RECORDING)]
FILE_WORDS = re.compile(r"agenda|minutes|summary|\bbriefs?\b|\bactions\b|notice|packet|presentation|materials|resolution", re.I)
MEETING_WORDS = re.compile(r"meeting|board|commission|committee|hearing|session|retreat|agenda|minutes", re.I)
TIME_RX = re.compile(r"(?<![\d:])(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?\s*m\b\.?|\b(noon)\b", re.I)
TIME_RANGE = re.compile(r"(?<![\d:])(\d{1,2})(?::([0-5]\d))?\s*(?:-|–|—|to)\s*(\d{1,2})(?::[0-5]\d)?\s*([ap])\.?\s*m\b", re.I)
WEEKDAY = r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day|mon|tues?|wed|thur?s?|fri|sat|sun)"
STATUSES = ("scheduled", "held", "cancelled", "tentative")
KINDS = ("board", "authority", "transit")


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
    for m in DATE_DMY.finditer(text):
        if not any(abs(m.start() - p) < 12 for p, _ in found):      # "June 9 2026" is not "9 2026"
            found.append((m.start(), _ok(int(m.group(3)), _mon(m.group(2)), int(m.group(1)))))
    for m in DATE_ISO.finditer(text):
        found.append((m.start(), _ok(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    for m in DATE_NUM.finditer(text):
        y = int(m.group(3))
        found.append((m.start(), _ok(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))))
    if not found and year_hint:
        for m in DATE_NOYEAR.finditer(text):
            found.append((m.start(), _ok(year_hint, _mon(m.group(1)), int(m.group(2)))))
    return [d for _, d in sorted(found) if d and "2005" <= d[:4] <= str(dt.date.today().year + 3)]


def clock(text: str) -> str:
    """The first clock time written in the text as HH:MM (24-hour, the body's local time): "9:30 a.m." -> "09:30",
    "Noon" -> "12:00", "2 PM" -> "14:00"; "" when none is written."""
    rg = TIME_RANGE.search(text or "")
    if rg:                                         # "4:30 - 5 p.m.": the start, with the end's half of the day
        h, mi, eh, ap = int(rg.group(1)), int(rg.group(2) or 0), int(rg.group(3)), rg.group(4).lower()
        if 1 <= h <= 12 and 1 <= eh <= 12:
            if ap == "p" and h != 12 and (h > eh and eh != 12 or eh == 12):
                ap = "a"
            return f"{h % 12 + (12 if ap == 'p' else 0):02d}:{mi:02d}"
    m = TIME_RX.search(text or "")
    if not m:
        return ""
    if m.group(4):
        return "12:00"
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if not 1 <= h <= 12:
        return ""
    h = h % 12 + (12 if ap == "p" else 0)
    return f"{h:02d}:{mi:02d}"


def file_name(href: str) -> str:
    return unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])


def name_dates(href: str, year_hint: int | None = None) -> list[str]:
    """The dates a file's own name carries: "Board_Minutes_-9-21-2026.doc" (hyphens kept: 9-21-2026),
    "Agenda Audit 11202026.pdf" (eight digits), "board-agenda-november-6-2026" (words)."""
    name = file_name(href)
    if name.lower() == "download":                       # georgia.gov: /document/document/<the name>/download
        name = unquote(urlsplit(href).path.rstrip("/").rsplit("/", 2)[-2])
    kept = name.replace("_", " ").replace("%20", " ")
    out = dates_in(kept)
    out += [d for d in (_ok(int(m.group(3)), int(m.group(1)), int(m.group(2))) for m in DATE_8.finditer(kept)) if d and d not in out]
    out += [d for d in dates_in(kept.replace("-", " ").replace("2C", ","), year_hint) if d not in out]
    for m in DATE_6.finditer(kept):                # 060624 = June 6, 2024
        d = _ok(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
        if d and d not in out and "2010" <= d[:4] <= str(dt.date.today().year + 1):
            out.append(d)
    return out


def _kind(label: str, href: str) -> str:
    """agenda | minutes | summary | video | cancel | doc: by the link's words, then by the file's name."""
    name = file_name(href)
    if name.lower() == "download":
        name = urlsplit(href).path.rstrip("/").rsplit("/", 2)[-2]
    for i, s in enumerate((label, name.replace("_", " ").replace("-", " ").replace("%20", " "))):
        for k, rx in KIND_RX:
            if rx.search(s) and not (i and k == "video"):      # "...-final-without-video.pdf" is not a recording
                return k
    return "doc"


def link_address(page: str, href: str) -> str:
    """A link's full address as it is kept and written everywhere (the meeting's file, the opened minutes document,
    the export): a backslash as a slash, a space in a file's name as %20 (MARTA: "Agenda PCP 11192026.pdf")."""
    return urljoin(page, href.strip().replace("\\", "/")).split("#")[0].replace(" ", "%20")


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# ------------------------------------------------------------------------------------------- a page as lines
BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "td", "th", "section",
         "article", "main", "body", "dl", "dt", "dd", "blockquote", "details", "summary", "figure", "figcaption", "hr", "br",
         "aside", "form", "button", "caption", "address", "time"}
HEADS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def clean_soup(html: str):
    """The page without what is not its own content: scripts, navigation, the header and footer, and a site-wide
    notice banner (georgia.gov's `.alert`: GSFIC shows its next Commission meeting on every page, the GHEFA's too).
    A header inside an article is the article's own (its title and link) and stays: the Macon-Bibb County Transit
    Authority's notices, read from the Internet Archive's copies, Nov. 20, 2026, lost every post's title with it."""
    from bs4 import BeautifulSoup

    html += "".join(f'<section data-shipped="1">{x}</section>' for x in _data_html(html))
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "noscript", "template"]):
        if t.name == "header" and t.find_parent("article"):
            continue                       # an article's own header holds its title (a post's heading and link), not the site's
        t.decompose()
    for t in soup.select(".alert, [role=alert], .site-alert, .usa-alert"):
        t.decompose()
    return soup


def page_lines(html: str, url: str, soup=None) -> list[dict]:
    """The page as its lines in reading order, whatever markup draws them:
    [{t: the line's text, links: [(label, address)], h: heading level or 0, bold: the whole line is bold,
      struck: some of it is struck out, li: in a list item, row: the table row it is in (a number) or None, cell: n}].
    Site readers read these, so they do not depend on a site's class names."""
    from bs4 import NavigableString, Tag

    soup = soup if soup is not None else clean_soup(html)
    out: list[dict] = []
    cur = {"parts": [], "links": [], "bold": [], "struck": False}
    ctx = {"h": 0, "li": False, "row": None, "cell": None}
    rows, cells = [0], []

    def flush():
        t = squash("".join(cur["parts"]))
        if t or cur["links"]:
            plain = [squash(x) for x, b in cur["bold"] if squash(x)]
            bold = bool(plain) and all(b for x, b in cur["bold"] if squash(x))
            out.append({"t": t, "links": list(cur["links"]), "h": ctx["h"], "bold": bold, "struck": cur["struck"],
                        "li": ctx["li"], "row": ctx["row"], "cell": ctx["cell"]})
        cur["parts"], cur["links"], cur["bold"], cur["struck"] = [], [], [], False

    def walk(el, bold=False, struck=False):
        for ch in el.children:
            if isinstance(ch, NavigableString):
                s = str(ch)
                cur["parts"].append(s)
                cur["bold"].append((s, bold))
                if struck and s.strip():
                    cur["struck"] = True
                continue
            if not isinstance(ch, Tag):
                continue
            name = ch.name
            if name == "a" and ch.get("href"):
                href = link_address(url, ch["href"])
                label = squash(ch.get_text(" ", strip=True)) or squash(" ".join(i.get("alt") or "" for i in ch.find_all("img")))
                if href.startswith("http"):
                    cur["links"].append((label, href))
                walk(ch, bold, struck)
                continue
            if name in BLOCK:
                flush()
                if name == "br" or name == "hr":
                    continue
                save = dict(ctx)
                if name in HEADS:
                    ctx["h"] = HEADS[name]
                elif name == "li":
                    ctx["li"] = True
                elif name == "tr":
                    rows[0] += 1
                    ctx["row"] = rows[0]
                    cells.append(-1)
                elif name in ("td", "th") and cells:
                    cells[-1] += 1
                    ctx["cell"] = cells[-1]
                walk(ch, bold or name == "th", struck)
                flush()
                if name == "tr":
                    cells.pop()
                ctx.update(save)
                continue
            walk(ch, bold or name in ("strong", "b"), struck or name in ("s", "del", "strike"))

    walk(soup.body or soup)
    flush()
    return out


# ------------------------------------------------------------------------------------------- a meeting
def new_meeting(date: str, body: str = "", mk: str = "") -> dict:
    """date YYYY-MM-DD; time HH:MM; name what the page calls the meeting; body the committee when not the full board;
    mk a sitting of its own on the same day ("work-session", "special"); status "" (decided by the day at export),
    "cancelled" or "tentative"; agenda, minutes (approved), summary, video: addresses; docs other files [{t, u}]."""
    return {"date": date, "time": "", "name": "", "body": body, "mk": mk, "where": "", "status": "", "agenda": "", "minutes": "",
            "summary": "", "video": "", "docs": [], "pub": "", "page": "", "event": ""}


SLOTS = ("agenda", "minutes", "summary", "video")
SLOT_LABEL = {"agenda": "Agenda", "minutes": "Minutes", "summary": "Summary", "video": "Recording", "cancel": "Cancellation notice"}


class Book:
    """The meetings one page (or one body's pages) lists, one per day + committee + sitting."""

    def __init__(self):
        self.m: dict[tuple, dict] = {}

    def meet(self, date: str, body: str = "", mk: str = "") -> dict:
        return self.m.setdefault((date, slug(body), mk), new_meeting(date, body, mk))

    def has(self, date: str, body: str = "", mk: str = "") -> bool:
        return (date, slug(body), mk) in self.m

    @staticmethod
    def file(m: dict, slot: str, label: str, href: str) -> None:
        """A file of the meeting: the first agenda, minutes, summary and recording each have a place of their own;
        anything else (and a second file of a kind) is kept with its label. Nothing is capped or dropped."""
        if not href or any(m[s] == href for s in SLOTS) or any(x["u"] == href for x in m["docs"]):
            return
        if slot == "cancel":
            m["status"] = "cancelled"
        if slot in SLOTS and not m[slot]:
            m[slot] = href
            return
        label = squash(label) or SLOT_LABEL.get(slot, "File")
        m["docs"].append({"t": label[:160], "u": href})

    @staticmethod
    def name(m: dict, t: str) -> None:
        t = squash(t).strip(" *:;,-–—|")
        if t and t.lower() not in m["name"].lower() and len(m["name"]) + len(t) < 220:
            m["name"] = f"{m['name']}; {t}" if m["name"] else t

    def out(self) -> list[dict]:
        return sorted(self.m.values(), key=lambda m: (m["date"], m["time"], m["body"]), reverse=True)


def merge(old: dict, m: dict) -> dict:
    """The same meeting found again (on a second page, or in the store): what the first lacks is filled in."""
    for k in ("time", "name", "where", "pub", "page", "event"):
        old[k] = old[k] or m.get(k, "")
    if m["status"] == "cancelled" or (m["status"] and not old["status"]):
        old["status"] = m["status"]
    for s in SLOTS:
        if m[s]:
            Book.file(old, s, SLOT_LABEL[s], m[s])
    for x in m["docs"]:
        Book.file(old, "doc", x["t"], x["u"])
    return old


# ------------------------------------------------------------------------------------------- the general reader
SCHEDULE_FILE = re.compile(r"meeting dates|meeting schedule|board calendar|meeting calendar", re.I)
# "Tuesday, January 14: Committee Meetings", "December 3, 2026, at Noon — Board Working Session": a schedule written
# as lines, the year in the line or in the page's heading
SCHEDULE_LINE = re.compile(rf"^(?:{WEEKDAY},?\s+)?({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?"
                           rf"(?:,?\s*(?:at\s+)?(\d{{1,2}}(?::\d\d)?\s*[ap]\.?\s*m\.?|noon))?\s*[:–—-]\s*(.{{4,160}})$", re.I)
TRAILING_DATE = re.compile(r"\s*[-–,]?\s*\(?\b\d{1,2}[/.-]\d{1,2}[/.-](?:20)?\d\d\)?\s*$")
# a georgia.gov event teaser, the event's name then its day, hour and town: "GEFA Board Meeting August 20 10:00 a.m. Atlanta, GA"
TEASER = re.compile(rf"^(?P<n>.+)\s+(?:{WEEKDAY},?\s+)?{_MON}\.?\s+\d{{1,2}}(?:,?\s+20\d\d)?(?:\s+(?P<t>\d{{1,2}}(?::\d\d)?\s*[ap]\.?m\.?|noon)"
                    rf"(?:\s*(?:-|–|to)\s*\d{{1,2}}(?::\d\d)?\s*[ap]\.?m\.?)?)?(?:\s+(?P<w>.+))?$", re.I)
EVENT_URL = re.compile(r"/events?/(20\d\d-\d{2}-\d{2})/")


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


DATE_LINE_TAGS = ["p", "div", "span", "li", "h5", "h6", "time", "td"]
DATE_ALONE = re.compile(rf"^(?:{WEEKDAY},?\s+)?{_MON}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d\d\s*\*?$", re.I)
SCHEDULE_CUE = re.compile(r"meeting schedule|schedule of meetings|meeting dates|following dates|upcoming (?:board )?meeting|"
                          r"regular meetings on", re.I)
UPCOMING = re.compile(r"\bupcoming\b|\bnext (?:board )?meeting\b", re.I)


def date_line(el, year_hint=None) -> str:
    """The day, when an element's whole text is one date ("October 14th, 2026", "Tuesday, November 18, 2026",
    "February 16, 2027 *"); "" otherwise. An element holding more (a sentence, a row, a block of several) is not."""
    t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    if not t or len(t) > 45 or not DATE_ALONE.match(t):
        return ""
    ds = dates_in(t, year_hint)
    return ds[0] if ds else ""


BARE_DATE = re.compile(rf"(?:{WEEKDAY},?\s+)?{_MON}\.?\s+\d{{1,2}}(?:,?\s*20\d\d)?", re.I)      # a file listed by its day alone


def is_file_link(label: str, href: str) -> bool:
    """A link that is a meeting's file: a document by its address, a file on a document service the body links, or
    a georgia.gov download whose label says what it is (an agenda, minutes, a summary, a notice, a brief)."""
    if re.search(r"unpublished|do-not-share", href, re.I) or not href.startswith("http"):
        return False                              # not a link, or a file whose own address says not to pass it on
    if DOC_RX.search(href) or SHARED_DOC.search(href):
        return True
    return "/download" in href and bool(FILE_WORDS.search(label) or MEETING_WORDS.search(label) or BARE_DATE.fullmatch(label.strip()))


def teaser(label: str) -> dict:
    """{n, t, w} from an event link's text; the whole text as n when it is not laid out as name, day, hour, place."""
    m = TEASER.match(squash(label))
    if not m:
        return {"n": squash(label), "t": "", "w": ""}
    n = m.group("n").strip(" -–:")
    lead = DATE_LONG.match(n)
    if lead and len(n) - lead.end() > 5:           # "December 8, 2026 Board Meeting"
        n = n[lead.end():].strip(" -–:,")
    return {"n": n, "t": clock(m.group("t") or ""), "w": squash(m.group("w") or "")}


def meetings_on(html: str, url: str, event_links: str = "", match: str = "", default_name: str = "") -> list[dict]:
    """Every meeting the page lists (new_meeting dicts), newest first: the general reader, for a body with no reader
    of its own. `event_links`: a part of the address of the site's own pages for single sessions, each named with its
    date in the link ("Administrative Session 11-17-2026"). `match`: on a site several bodies share, the words (a
    pattern) an event link or file must carry to be this body's."""
    soup = clean_soup(html)
    book = Book()
    meet = book.meet
    wanted = re.compile(match, re.I) if match else None

    # the year a part of the page is about: its nearest heading above that names one ("2026 Meeting Minutes")
    year_at: dict[int, int] = {}
    ym = YEAR.search(soup.title.get_text(" ") if soup.title else "") or YEAR.search(urlsplit(url).path)
    cur = page_year = int(ym.group(1)) if ym else None
    for el in soup.find_all(True):
        if el.get("data-shipped"):               # content from the page's data script: the page's own year again
            cur = page_year
        if el.name in ("h1", "h2", "h3", "h4", "h5", "strong", "summary", "button", "caption"):
            y = YEAR.search(el.get_text(" ", strip=True)[:80])
            if y:
                cur = int(y.group(1))
        elif el.name in ("p", "div", "span", "li", "td", "dt", "button") and el.string and re.fullmatch(r"\s*20\d\d\s*", el.string):
            cur = int(el.string)                 # the year on a line of its own over that year's entries (SRTA)
        year_at[id(el)] = cur

    def row_of(a):
        """The smallest block around a link that is one entry of a list: a table row, a list item, a paragraph."""
        for p in a.parents:
            if p.name in ("tr", "li", "p", "dd", "article") or (p.name == "div" and len(p.get_text(" ", strip=True)) < 400):
                return p
        return a.parent

    def heading_date(el) -> str:
        """The date heading a block sits under (DBHDD: 'Thursday, February 26, 2026', then its files); else the
        nearest line above that is a date and nothing else, as page builders lay it out in a plain text block; else,
        under an "Upcoming meeting" heading, the date written after the files. A line that is itself a link, or
        another entry of the same list, is a neighbouring entry and never the heading (the Stone Mountain Memorial
        Association's list of minutes: one item's file was filed under the item above it)."""
        for prev in el.find_all_previous(["h2", "h3", "h4", "h5", "strong", "b", "dt", "summary"], limit=4):
            if prev.find_parent("a") or prev.find("a"):
                continue
            ds = dates_in(prev.get_text(" ", strip=True)[:120], year_at.get(id(prev)))
            if ds:
                return ds[0]
        own = row_of(el)
        for prev in el.find_all_previous(DATE_LINE_TAGS, limit=60):
            if prev.find("a") or prev.find_parent("a") or (own.name == "li" and prev.name == "li" and prev.parent is own.parent):
                continue
            d = date_line(prev, year_at.get(id(prev)))
            if d:
                return d
        up = next((h for h in el.find_all_previous(["h1", "h2", "h3", "h4"], limit=3)
                   if UPCOMING.search(h.get_text(" ", strip=True))), None)
        if up is not None:
            for nxt in el.find_all_next(DATE_LINE_TAGS, limit=60):
                d = date_line(nxt, year_at.get(id(nxt)))
                if d:
                    return d
                if nxt.name in ("h1", "h2", "h3") and nxt is not up:
                    break
        return ""

    def first_date_cell(tr):
        cells = tr.find_all(["td", "th"])
        for i, c in enumerate(cells[:3]):
            ds = dates_in(c.get_text(" ", strip=True)[:80], year_at.get(id(tr)))
            if ds:
                return cells, i, ds[0]
        return cells, -1, ""

    page_title = squash((soup.find("h1") or soup.title).get_text(" ", strip=True)) if (soup.find("h1") or soup.title) else ""

    def file_here(m: dict, label: str, href: str) -> None:
        k = _kind(label, href)
        if BARE_DATE.fullmatch(label.strip()) and page_title:      # "June 5" under "Board Meeting Presentations"
            k = _kind(page_title, "") if k == "doc" else k
            label = re.sub(r"s$", "", page_title.split("|")[0].strip())
        book.file(m, k, label, href)

    for a in soup.find_all("a", href=True):
        href = link_address(url, a["href"])
        if not href.startswith("http") or re.search(r"unpublished|do-not-share", href, re.I):
            continue
        label = squash(a.get_text(" ", strip=True)) or squash(" ".join(i.get("alt") or "" for i in a.find_all("img")))
        name = file_name(href)
        ev = EVENT_URL.search(href)
        if ev:                                    # an event's own page: the day is in its address
            if MEETING_WORDS.search(label + " " + name) and (not wanted or wanted.search(label + " " + href)):
                m = meet(ev.group(1))
                ts = teaser(label)
                book.name(m, ts["n"] if len(ts["n"]) < 200 else ts["n"][:200])
                m["time"], m["where"], m["event"] = m["time"] or ts["t"], m["where"] or ts["w"], m["event"] or href
            continue
        is_doc = is_file_link(label, href)
        if event_links and event_links in href and not is_doc:
            ds = dates_in(label)
            what = TRAILING_DATE.sub("", label)
            if ds and what and what != label:
                m = meet(ds[0])
                book.name(m, what)
                m["page"] = m["page"] or href.split("?")[0]
            continue
        if not is_doc or not label:               # a link with no words is not one the page shows its readers
            continue
        if wanted and not wanted.search(label + " " + href):
            continue
        tr = a.find_parent("tr")
        if tr is not None and first_date_cell(tr)[2]:
            continue                              # a row of a schedule: read below, with the row's own date
        yh = year_at.get(id(a))
        row = row_of(a)
        d = (dates_in(label, yh) or dates_in(row.get_text(" ", strip=True)[:300], yh) or name_dates(href, yh) or [""])[0]
        if not d:
            d = heading_date(a)
        if d:
            file_here(meet(d), label, href)

    # a schedule laid out as a table: a row that opens with a date is a meeting, with or without files, and every
    # file linked in the row is that meeting's, wherever it is kept (the Jekyll Island Authority links Adobe's
    # document service). What the date's cell says besides the date is the meeting's name, or that it is cancelled.
    for table in soup.find_all("table"):
        head: list[str] = []
        last = None
        for tr in table.find_all("tr"):
            cells, di, d = first_date_cell(tr)
            texts = [squash(c.get_text(" ", strip=True)) for c in cells]
            if not d:
                if last is not None and cells and len(cells) == last[1] and sum(1 for t in texts if t) == 1 and not tr.find("a"):
                    i = next(i for i, t in enumerate(texts) if t)
                    if (re.search(r"location|place|where|venue|site", head[i]) if i < len(head) else i == len(texts) - 1) and texts[i] not in last[0]["where"]:
                        last[0]["where"] = f"{last[0]['where']}, {texts[i]}"[:200].strip(", ")   # the place goes on in the next row
                    continue
                last = None
                if not head and any(re.fullmatch(r"(?:meeting )?(?:date|dates|time|location|place|agenda|minutes|meeting)s?", t, re.I) for t in texts):
                    head = [t.lower() for t in texts]
                continue
            if len(cells) < 2 or (event_links and any(event_links in x["href"] for x in tr.find_all("a", href=True))):
                continue                           # one cell, or a calendar's grid of session links (read above)
            m = meet(d)
            last = (m, len(cells))
            yh = year_at.get(id(tr))
            rest = texts[di]
            for rx in (DATE_LONG, DATE_NUM, DATE_ISO) + ((DATE_NOYEAR,) if yh else ()):
                rest = rx.sub(" ", rest, count=1)
            rest = squash(re.sub(rf"\b{WEEKDAY}\b,?", " ", rest, flags=re.I)).strip(" ,;:-–—*()")
            if cells[di].find(["s", "del", "strike"]) or CANCEL.search(" ".join(texts)):
                m["status"] = "cancelled"
            elif TENTATIVE.search(rest) or texts[di].endswith("*"):
                m["status"] = m["status"] or "tentative"
            if clock(rest) and not m["time"]:
                m["time"] = clock(rest)
            rest = squash(TIME_RX.sub(" ", rest)).strip(" ,;:-–—*()@")
            if rest and not CANCEL.fullmatch(rest) and not TENTATIVE.fullmatch(rest) and len(rest) < 120 and re.search(r"[A-Za-z]{3}", rest):
                book.name(m, rest)
            for i, c in enumerate(cells):
                if i == di:
                    continue
                t, col = texts[i], (head[i] if i < len(head) else "")
                has_files = any(is_file_link(squash(x.get_text(" ", strip=True)), urljoin(url, x["href"])) or
                                _kind(squash(x.get_text(" ", strip=True)), "") != "doc" for x in c.find_all("a", href=True))
                if not t or has_files or re.search(r"agenda|minutes|summary|audio|materials|files?$", col):
                    continue
                if "time" in col or (clock(t) and len(t) < 40 and not re.search(r"[A-Za-z]{4}", TIME_RX.sub("", t))):
                    m["time"] = m["time"] or clock(t)
                elif re.search(r"location|place|where|venue|site", col):
                    m["where"] = m["where"] or t[:200]
                elif dates_in(t) or re.search(r"agenda|minutes", t, re.I):
                    continue                       # a second date (a two-day meeting) or the files' column
                elif re.search(r"meeting|event|type|committee|description|purpose", col) or \
                        (re.search(r"meeting|session|hearing|retreat|workshop", t, re.I) and len(t) < 90):
                    book.name(m, t)                # "Committee Meeting", "Board Meeting"
                elif not m["where"]:
                    m["where"] = t[:200]
                    if clock(t) and not m["time"] and len(t) < 60:
                        m["time"] = clock(t)
            for a in tr.find_all("a", href=True):
                href = link_address(url, a["href"])
                label = squash(a.get_text(" ", strip=True))
                if not label or not href.startswith("http") or re.search(r"unpublished|do-not-share|^mailto:", href, re.I):
                    continue
                k = _kind(label, href)
                if is_file_link(label, href) or k != "doc" or FILE_WORDS.search(label):
                    book.file(m, k, label, href)

    # a schedule written as dates alone, one a line, under a heading or sentence that says it is the schedule ("FY27
    # Board and Committee Meeting Schedule ... the following dates: November 18, 2026 / February 16, 2027 *"): each is
    # a meeting, files or not; a date the page marks with an asterisk is one it says may change. Only under such
    # words, so a date standing alone elsewhere is not taken for a meeting.
    for el in soup.find_all(DATE_LINE_TAGS):
        d = date_line(el, year_at.get(id(el)))
        if not d or el.find("a"):
            continue
        cue = next((x for x in el.find_all_previous(DATE_LINE_TAGS + ["h1", "h2", "h3", "h4", "h5", "strong", "b"], limit=12)
                    if SCHEDULE_CUE.search(x.get_text(" ", strip=True)[:200])), None)
        if cue is not None:
            m = meet(d)
            if el.get_text(" ", strip=True).rstrip().endswith("*"):
                m["status"] = m["status"] or "tentative"
            if UPCOMING.search(cue.get_text(" ", strip=True)[:200]) and not m["name"]:
                book.name(m, "Board meeting")
    # a schedule written as lines of text under a heading that names the year (State Board of Education)
    for el in soup.find_all(["p", "li"]):
        if el.find("a"):
            continue
        for line in el.get_text("\n", strip=True).split("\n"):      # two days of one month share a paragraph
            sm = SCHEDULE_LINE.match(re.sub(r"\s+", " ", line).strip())
            if not sm or not MEETING_WORDS.search(sm.group(5)):
                continue
            y = int(sm.group(3)) if sm.group(3) else year_at.get(id(el))
            d = _ok(y, _mon(sm.group(1)), int(sm.group(2))) if y else ""
            if d:
                m = meet(d)
                book.name(m, sm.group(5))
                m["time"] = m["time"] or clock(sm.group(4) or "")
                if CANCEL.search(sm.group(5)):
                    m["status"] = "cancelled"
    out = book.out()
    for m in out:
        if not m["name"] and default_name:
            m["name"] = default_name
        if not m["body"] and re.fullmatch(r"committee meetings?", m["name"], re.I):
            m["body"] = "Committees"              # the committees' day, apart from the board's (Georgia Ports Authority)
    return out


# ------------------------------------------------------------------------------------------- site readers
# Each reads one site's own layout, as lines in reading order. Signature: (html, url, ag, page) -> [meeting].
# `ag` is the body's entry in config/agencies.yaml, `page` the page's own entry ({u, reader, section, ...}).
def _is_board(title: str) -> bool:
    return bool(re.search(r"\bboard\b|\bBOD\b|\bdirectors\b|\bgovernors\b|\bauthority meeting\b|\bcommission\b", title, re.I)) and \
        not re.search(r"committee", title, re.I)


def _committee(title: str) -> str:
    """The committee a title names ("Administrative Committee Agenda" -> "Administrative Committee"); "" for the board.
    The words that say what the file is are no part of the name, however they are spelled ("Prelimanary Agenda",
    "Committee Mtg. Minutes")."""
    t = squash(re.sub(r"\((?:PDF|BOD)[^)]*\)", " ", title, flags=re.I))
    t = re.sub(r"\bmtg\b\.?", " ", t, flags=re.I)
    t = squash(re.sub(r"\b(?:draft|prelim\w*|signed|approved|view|meeting|agenda|minutes|presentation|materials|summary)\b", " ", t, flags=re.I)).strip(" -–:,")
    if not t or _is_board(title):
        return ""
    return t if re.search(r"committee", t, re.I) else f"{t} Committee"


def committee_name(ag: dict, label: str) -> str:
    """The one name a committee is filed under when its body labels it several ways on its files ("Executive_Finance
    Committee", "Joint Finance Executive Committee", "FB Review Committee"): the body's `committees` in
    config/agencies.yaml, a list of {name, rx}. The first pattern the label holds (case ignored, an underscore read as
    a space) names it; "" when none does, and for a body with no list."""
    text = squash(str(label or "").replace("_", " "))
    for c in ag.get("committees") or ():
        if re.search(str(c["rx"]), text, re.I):
            return squash(str(c["name"]))
    return ""


def govhub_event(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """A georgia.gov (GovHub) event page, the day in its address: the event's name (h1), then "Date" (the day and
    hour), "Location" (the lines up to "Get Directions"), the agenda written out on the page (then the page itself is
    the agenda) or linked, and "Related Files". GEFA, GSFIC's GHEFA meetings, LLIDA, GTA."""
    ev = EVENT_URL.search(url)
    if not ev:
        return []
    book = Book()
    m = book.meet(ev.group(1))
    m["page"] = m["event"] = url
    ld_where = ""
    for raw in re.findall(r'(?is)<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html):   # the page's own event data
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for it in (data.get("@graph") or [data]) if isinstance(data, dict) else []:
            if not isinstance(it, dict) or it.get("@type") != "Event":
                continue
            sd = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", str(it.get("startDate") or ""))
            if sd and sd.group(1) == m["date"] and sd.group(2) != "00:00":
                m["time"] = sd.group(2)
            loc = it.get("location") if isinstance(it.get("location"), dict) else {}
            ad = loc.get("address") if isinstance(loc.get("address"), dict) else {}
            ld_where = ", ".join(x for x in (loc.get("name"), ad.get("streetAddress"), ad.get("addressLocality")) if x)
    lines = page_lines(html, url)
    mode, where, agenda_lines = "", [], 0
    for ln in lines:
        t = ln["t"]
        if ln["h"] == 1 and not m["name"]:
            book.name(m, t)
            if CANCEL.search(t):
                m["status"] = "cancelled"
            continue
        low = t.lower().strip(" :")
        if low in ("date", "date and time", "when"):
            mode = "date"
            continue
        if low in ("location", "where", "venue"):
            mode = "where"
            continue
        if low == "agenda" or (ln["h"] and low.startswith("agenda")):
            mode = "agenda"
            continue
        if ln["h"] and re.search(r"related files|related links|documents", low):
            mode = "files"
        if mode == "date":
            if clock(t) and not m["time"]:
                m["time"] = clock(t)
            if not dates_in(t) and not clock(t):
                mode = ""
        if mode == "where":
            if re.search(r"get directions|google\.com/maps", t + " ".join(h for _, h in ln["links"]), re.I) or ln["h"] or len(where) > 5:
                mode = ""
            elif t and not ln["links"]:
                where.append(t)
                continue
        if mode == "agenda" and ln["li"] and not ln["links"]:
            agenda_lines += 1
        if re.search(r"\b(?:has been|is|was)\s+cancel+ed\b|\bmeeting cancel+ed\b", t, re.I):
            m["status"] = "cancelled"
        for label, href in ln["links"]:
            if label and is_file_link(label, href) or (mode == "files" and "/download" in href and label):
                book.file(m, _kind(label, href), label, href)
    if where and not re.fullmatch(r"(?:virtual|online|tbd|n/?a)", " ".join(where).strip().lower()):
        m["where"] = ", ".join(where)[:200]
    m["where"] = m["where"] or ld_where[:200]
    if agenda_lines >= 2 and not m["agenda"]:
        m["agenda"] = url                          # the agenda is the page: GEFA writes it out under "Agenda"
    wanted = ag.get("match")
    if wanted and not re.search(wanted, m["name"] + " " + url, re.I):
        return []
    return book.out()


MARTA_BODY = {"BDM": ("", "", "Board Meeting"), "WS": ("", "work-session", "Board Work Session"),
              "PCP": ("Planning & Capital Programs Committee", "", ""), "OSC": ("Operations & Safety Committee", "", ""),
              "BMC": ("Business Management Committee", "", ""), "ERC": ("External Relations Committee", "", ""),
              "AC": ("Audit Committee", "", ""), "GSC": ("GM/CEO Search Committee", "", "")}
MARTA_HEAD = [(r"work(?:ing)? session", "WS"), (r"planning\s*&\s*capital", "PCP"), (r"operations\s*&\s*safety", "OSC"),
              (r"business management", "BMC"), (r"external relations", "ERC"), (r"audit", "AC"), (r"search committee", "GSC"),
              (r"board of directors|board meeting", "BDM")]


def _marta_code(text: str) -> str:
    return next((c for rx, c in MARTA_HEAD if re.search(rx, text, re.I)), "")


def _marta_meet(book: Book, date: str, code: str, special: bool = False) -> dict:
    body, mk, name = MARTA_BODY[code]
    if special and code == "BDM":
        mk, name = "special", "Special Called Board Meeting"
    m = book.meet(date, body, mk)
    m["name"] = m["name"] or name or body
    return m


def marta_schedule(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """MARTA's official schedule: "December 3, 2026, at Noon — Board Working Session", then each committee's name in
    bold over its dates and hours ("November 20, 2026, at 10:00 a.m. (Friday)"; "December - No Meeting" is no date)."""
    book, code, where = Book(), "", ""
    for ln in page_lines(html, url):
        t = ln["t"]
        if re.search(r"held through remote access", t, re.I):
            link = next((h for _, h in ln["links"]), "") or (re.search(r"https?://\S+", t) or [""])[0]
            where = "Remote access" + (f" ({link})" if link else "")
        if not where.endswith(")") and where and re.fullmatch(r"<?https?://\S+>?", t):
            where = f"Remote access ({t.strip('<>')})"
        ds = dates_in(t)
        if not ds:
            c = _marta_code(t) if (ln["bold"] or ln["h"]) and len(t) < 80 else ""
            if c and not re.search(r"board meeting schedule", t, re.I):
                code = c
            elif ln["h"] and re.search(r"board meeting schedule", t, re.I):
                code = "BDM"
            continue
        c = _marta_code(t.split("—")[-1] if "—" in t or "–" in t else "") or code
        if "—" in t or " – " in t:
            c = _marta_code(re.split(r"—|–", t)[-1]) or c
        if not c:
            continue
        m = _marta_meet(book, ds[0], c)
        m["time"] = m["time"] or clock(t)
        if CANCEL.search(t):
            m["status"] = "cancelled"
    for m in book.m.values():
        m["where"] = where
    return book.out()


def marta_documents(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """MARTA's Board Documents pages (this year's and each earlier year's): three sections (Meeting Agendas, Meeting
    Summaries, Meeting Minutes), in each the board, the work sessions and every committee under its own name, and
    under it one link a meeting, "AC - November 19, 2026". Each committee is a body of its own. A summary is not the
    approved minutes. A file named "No Meeting ..." is a cancellation. MARTA's own slips are read around: a link
    with no words is not shown to its readers and is skipped; a label whose year is another year's while the file's
    name carries this page's year takes the file's date; a file whose name carries another meeting's date (more than
    a week from the label's) is filed under neither. `ag["_schedule"]` (the schedule page's dates, read first)
    decides between a label and a file name a day or two apart (the Audit Committee's Nov. 20, 2026 agenda is
    labelled Nov. 19)."""
    book, sect, head = Book(), "", ""
    sched = ag.get("_schedule") or {}             # {(body, mk): {dates}}
    ym = YEAR.search(file_name(url))
    page_year = int(ym.group(1)) if ym else None
    years: dict[int, int] = {}
    lines = page_lines(html, url)
    if not page_year:
        for ln in lines:
            for label, _ in ln["links"]:
                for d in dates_in(label):
                    years[int(d[:4])] = years.get(int(d[:4]), 0) + 1
        page_year = max(years, key=years.get) if years else None
    skipped = 0
    for ln in lines:
        t = ln["t"]
        if (ln["h"] or ln["bold"]) and not ln["links"]:
            s = re.search(r"meeting (agendas|summaries|minutes)\b", t, re.I)
            if s and len(t) < 40:
                sect = {"agendas": "agenda", "summaries": "summary", "minutes": "minutes"}[s.group(1).lower()]
                continue
            c = _marta_code(t) if len(t) < 80 else ""
            if c:
                head = c
            continue
        if not sect:
            continue
        for label, href in ln["links"]:
            if not label or not DOC_RX.search(href):
                continue                           # a link with no words: left in the markup, not shown on the page
            pm = re.match(r"\s*([A-Z]{2,3})\s*[-–]", label)
            code = pm.group(1) if pm and pm.group(1) in MARTA_BODY else head
            ld = (dates_in(label) or [""])[0]
            fds = [d for d in name_dates(href)]
            fd = fds[0] if fds else ""
            if not code or not (ld or fd):
                continue
            d = ld or fd
            if ld and fd and fd != ld:
                a, b = dt.date.fromisoformat(ld), dt.date.fromisoformat(fd)
                body, mk, _ = MARTA_BODY[code]
                known = sched.get((slug(body), mk)) or set()
                if (a.month, a.day) == (b.month, b.day):
                    d = fd if page_year and b.year == page_year and a.year != page_year else ld      # a year mistyped
                elif abs((a - b).days) <= 7:
                    d = fd if (fd in known and ld not in known) else ld
                else:
                    skipped += 1                   # another meeting's file linked here by mistake
                    continue
            special = bool(re.search(r"\(special\)|special called", t + " " + file_name(href), re.I))
            m = _marta_meet(book, d, code, special)
            fname = file_name(href)
            if CANCEL.search(label + " " + fname):
                book.file(m, "cancel", "Cancellation notice", href)
            elif sect == "minutes" and not re.search(r"minutes", fname, re.I) and re.search(r"summary", fname, re.I):
                book.file(m, "summary", "Summary", href)
            else:
                book.file(m, sect, {"agenda": "Agenda", "summary": "Meeting summary", "minutes": "Approved minutes"}[sect], href)
    out = book.out()
    if skipped:
        ag.setdefault("_notes", []).append(f"{skipped} mislinked files left out on {url}")
    return out


def title_table(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """A table of DATE | TITLE | AGENDA | MINUTES | PRESENTATION, one row for the board and one for each committee that
    met that day (the ATL board's page, kept by the Internet Archive): the title is who met, not where."""
    book = Book()
    soup = clean_soup(html)
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        ds = dates_in(cells[0].get_text(" ", strip=True)[:80])
        title = squash(cells[1].get_text(" ", strip=True))
        if not ds or not title:
            continue
        body = _committee(title)
        mk = "special" if re.search(r"special", title + " " + " ".join(a["href"] for a in tr.find_all("a", href=True)), re.I) and \
            re.search(r"special(?:ly|ty)?[- ]called", " ".join(a["href"] for a in tr.find_all("a", href=True)).replace("-", " "), re.I) else ""
        m = book.meet(ds[0], body, mk)
        m["name"] = m["name"] or (body or squash(re.sub(r"\bagenda\b|\(BOD\)", "", title, flags=re.I)))
        if mk:
            m["name"] = f"{m['name']} (specially called)" if "special" not in m["name"].lower() else m["name"]
        for a in tr.find_all("a", href=True):
            label = squash(a.get_text(" ", strip=True))
            href = link_address(url, a["href"])
            if label and href.startswith("http"):
                k = "minutes" if re.search(r"minute", label, re.I) else _kind(label, href)
                book.file(m, k, re.sub(r"^view\s+", "", label, flags=re.I).capitalize(), href)
    return book.out()


def gtea_board(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """The Georgia Transportation Efficiency Authority's board page. Three parts: the coming meeting ("10:30 AM – GTEA
    Committee Meetings" over each committee's agenda, "11:15 AM – GTEA Board Meeting" over the board's, then "Date" and
    the day); the year's schedule (dates alone; one with an asterisk may change, the page says); and past meetings (a
    date on a line of its own, then who met on a line, then View Agenda / View Minutes / View Presentation). The board
    and each committee are rows of their own with their own agenda. The minutes linked in a meeting's row are the ones
    approved AT that meeting (Aug. 19's row links "6.16.26-Approved-BOD-Meeting-Minutes"), so a minutes file belongs
    to the day its name carries, or else to that body's meeting before."""
    book = Book()
    lines = page_lines(html, url)
    part, date, body, known = "", "", None, False
    up: list[tuple[str, str, str, str]] = []      # the coming meeting's files, before its date is read: (body, time, label, href)
    up_time, up_body, want_date = "", None, False
    held: list[tuple[str, str, str, str]] = []    # minutes: (row date, body, label, href)
    for ln in lines:
        t = ln["t"]
        if ln["h"]:
            if re.search(r"upcoming", t, re.I):
                part = "up"
            elif re.search(r"schedule", t, re.I):
                part = "schedule"
            elif re.search(r"past", t, re.I):
                part = "past"
            continue
        if part == "up":
            tm = re.match(r"^(\d{1,2}(?::\d\d)?\s*[ap]\.?m\.?)\s*[–—-]\s*(.+)$", t, re.I)
            if tm:
                up_time, up_body = clock(tm.group(1)), ("" if _is_board(tm.group(2)) else None)
                continue
            if t.lower() == "date":
                want_date = True
                continue
            if want_date and dates_in(t):
                date, want_date = dates_in(t)[0], False
                for b, tmv, label, href in up:
                    m = book.meet(date, b)
                    m["time"], m["name"] = m["time"] or tmv, m["name"] or (b or "Board Meeting")
                    book.file(m, _kind(label, href), label, href)
                continue
            for label, href in ln["links"]:
                if label and DOC_RX.search(href):
                    b = "" if up_body == "" else _committee(label)
                    up.append((b, up_time, "Agenda" if re.search(r"agenda", label, re.I) else label, href))
        elif part == "schedule":
            if DATE_ALONE.match(t) and dates_in(t):
                m = book.meet(dates_in(t)[0])
                m["name"] = m["name"] or "Board and committee meetings"
                if t.rstrip().endswith("*"):
                    m["status"] = m["status"] or "tentative"
        elif part == "past":
            if DATE_ALONE.match(t) and not ln["links"]:
                date, body = dates_in(t)[0], None
                continue
            if not date:
                continue
            if t and not ln["links"]:
                body = _committee(t) if len(t) < 80 else body
                if body is not None:
                    m = book.meet(date, body)
                    m["name"] = m["name"] or (body or "Board Meeting")
                continue
            for label, href in ln["links"]:
                if not label or body is None:
                    continue
                short = re.sub(r"^view\s+", "", label, flags=re.I).capitalize()
                if re.search(r"minutes", label, re.I):
                    held.append((date, body, short, href))
                else:
                    book.file(book.meet(date, body), _kind(label, href), short, href)
    for row, b, label, href in held:
        own = [d for d in name_dates(href) if d <= row]
        days = sorted(d for (d, bs, mk) in book.m if bs == slug(b) and not mk)
        before = [d for d in days if d < row]
        if own:
            target = own[0]
        elif before:
            target = before[-1]
        else:
            book.file(book.meet(row, b), "doc", "Minutes approved at this meeting (an earlier meeting's)", href)
            continue
        m = book.meet(target, b)
        m["name"] = m["name"] or (b or "Board Meeting")
        book.file(m, "minutes", "Minutes", href)
    return book.out()


def feed_as_html(text: str) -> str:
    """A site's RSS feed laid out as a page of posts (a heading that links the post, its day, its text)."""
    import html as _h

    if "<item" not in text[:200000] or "<rss" not in text[:2000] and "<?xml" not in text[:200]:
        return text
    out = []
    for it in re.findall(r"(?is)<item\b.*?</item>", text):
        def tag(n):
            m = re.search(rf"(?is)<{n}\b[^>]*>(.*?)</{n}>", it)
            v = m.group(1).strip() if m else ""
            v = re.sub(r"(?s)^<!\[CDATA\[(.*)\]\]>$", r"\1", v)
            return v
        body = tag("content:encoded") or _h.unescape(tag("description"))
        out.append(f'<h2><a href="{_h.escape(_h.unescape(tag("link")))}">{tag("title")}</a></h2><p class="pub">{tag("pubDate")}</p><div>{body}</div>')
    return "<html><body>" + "\n".join(out) + "</body></html>"


POST_CUE = re.compile(r"\b(?:agenda|meeting|session|date|scheduled for|held on|on)\b[\s:–—-]*$", re.I)


def notice_posts(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """A list of a body's own notices, each a heading that links the post over its text (a WordPress category page or
    its feed): the Stone Mountain Memorial Association ("Board of Directors Meeting Agenda – November 16,2026 /
    Evergreen Conference Center Rotunda 2:00 PM"; "AGENDA November 9,2026 LOCATION: Confederate Hall TIME: 9:00 AM"),
    the GTEA ("... to hold a Board Work Session scheduled for November 10, 2026 at 10 a.m."), Chatham Area Transit
    ("Regular Board Meeting on Tuesday, September 22, 2026, at 4 p.m. ... at CAT Central, located at ..."), the
    Macon-Bibb County Transit Authority ("Date: Tuesday, October 27, 2026 Time: 5:30 p.m. Location: ...").
    A post is a meeting when its title names a board or committee meeting, session or agenda and a day is written."""
    book = Book()
    posts: list[dict] = []
    lines = page_lines(feed_as_html(html), url)
    for ln in lines:
        if ln["h"] and 1 <= ln["h"] <= 4 and ln["links"] and len(ln["t"]) < 220:
            posts.append({"title": ln["t"], "u": ln["links"][0][1], "lines": []})
        elif posts:
            posts[-1]["lines"].append(ln["t"])
    if not posts and (page or {}).get("post"):
        # a post's own page, set down so in the config (`post: true`): the post is the page, under its first heading
        # (the Macon-Bibb County Transit Authority's notices of Sept. and Oct. 2025, kept by the Internet Archive one
        # by one). A list of posts that shows none is never read so.
        for i, ln in enumerate(lines):
            if ln["h"] and 1 <= ln["h"] <= 2 and not ln["links"] and 4 < len(ln["t"]) < 220:
                posts.append({"title": ln["t"], "u": url, "lines": [x["t"] for x in lines[i + 1:i + 60]]})
                break
    seen = set()
    for p in posts:
        if p["u"] in seen:
            continue
        seen.add(p["u"])
        title, text = p["title"], " ".join(p["lines"])[:3000]
        slugw = file_name(p["u"]).replace("-", " ")
        if not re.search(r"board|committee|authority meeting", title + " " + slugw, re.I) or \
                not re.search(r"meeting|session|agenda", title + " " + slugw, re.I) or \
                re.search(r"public speakers|ordinance|welcomes|appoint", title, re.I):
            continue
        d, pos = "", -1
        # a post that opens with its day ("Tuesday, October 28, 2025, at 5:30 p.m. ..."): that day is the meeting's,
        # whatever later dates the agenda names ("... Annual Conference in Augusta on November 19-21, 2025")
        lead = re.match(rf"\s*{WEEKDAY},?\s*", text, re.I)
        first = DATE_LONG.match(text, lead.end()) if lead else None
        if first and _ok(int(first.group(3)), _mon(first.group(1)), int(first.group(2))):
            d, pos = _ok(int(first.group(3)), _mon(first.group(1)), int(first.group(2))), first.end()
        for mm in ([] if d else list(DATE_LONG.finditer(text))):
            day = _ok(int(mm.group(3)), _mon(mm.group(1)), int(mm.group(2)))
            before = text[max(0, mm.start() - 60):mm.start()]
            before = re.sub(rf"{WEEKDAY},?\s*$", "", before, flags=re.I)
            if day and (POST_CUE.search(before) or not d):
                if POST_CUE.search(before) or not d:
                    d, pos = day, mm.end()
                if POST_CUE.search(before):
                    break
        if not d:
            ds = dates_in(title) or dates_in(slugw.replace(" at ", " , at "))
            sl = re.search(rf"({_MON})\s+(\d{{1,2}})\s+(20\d\d)", slugw, re.I)
            d = ds[0] if ds else (_ok(int(sl.group(3)), _mon(sl.group(1)), int(sl.group(2))) if sl else "")
        if not d:
            continue
        both = title + " " + slugw
        mk = "special" if re.search(r"special", both, re.I) else ("work-session" if re.search(r"work session", both + " " + text[:300], re.I) else "")
        only_committee = bool(re.search(r"committee", both, re.I)) and not re.search(r"board of directors|board meeting|and board|board and", both, re.I)
        body = "Committees" if only_committee and not mk else ""
        m = book.meet(d, body, mk)
        m["name"] = m["name"] or ({"special": "Special Called Board Meeting", "work-session": "Board Work Session"}.get(mk) or
                                  ("Committee Meetings" if body else "Board Meeting"))
        m["page"] = p["u"]
        if CANCEL.search(title):
            m["status"] = "cancelled"
        after = text[pos:pos + 140] if pos >= 0 else ""
        tm = re.search(r"\bTIME:?\s*([^A-Za-z]{0,3}\d[^.]{0,12}?[ap]\.?m\.?)", text, re.I)
        m["time"] = m["time"] or clock(tm.group(1) if tm else "") or clock(after) or \
            clock(re.sub(r"\bat (\d{1,2})(?: (\d\d))? ([ap]) m\b", r"at \1:\2 \3m", slugw).replace(": ", ":00 "))
        wm = re.search(r"\b(?:LOCATION:?|Location:)\s*(.+?)(?=\s+(?:TIME\b|Time:|Agenda\b|AGENDA\b|\d\.\s|I\.\s)|$)", text) or \
            re.search(r"take place at\s+(.{4,120}?)(?=,? and will|\.\s+(?:The|To|Meeting|For)\b|\.$|$)", text)
        where = squash(wm.group(1)) if wm else ""
        if not where and pos >= 0:                # the line under the agenda's heading: the room, then the hour
            nxt = squash(TIME_RX.sub("", text[pos:pos + 90].split(" I. ")[0].split(" 1. ")[0])).strip(" ,;:-–")
            if 4 < len(nxt) < 70 and re.search(r"center|hall|room|rotunda|building|floor|station|office", nxt, re.I):
                where = nxt
        m["where"] = m["where"] or re.sub(r"^at\s+", "", where)[:200]
        if re.search(r"\bagenda\b", text + " " + title, re.I):
            m["agenda"] = m["agenda"] or p["u"]
        pub = re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), (\d{1,2}) ([A-Z][a-z]{2}) (20\d\d)\b", text[:80]) if p["lines"] else None
        if pub:
            m["pub"] = m["pub"] or _ok(int(pub.group(3)), _mon(pub.group(2)), int(pub.group(1)))
        else:
            pn = re.match(r"\s*(\d{1,2})/(\d{1,2})/(20\d\d)\s*[–-]", text)
            if pn:
                m["pub"] = m["pub"] or _ok(int(pn.group(3)), int(pn.group(1)), int(pn.group(2)))
    return book.out()


def smma_minutes(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """The Stone Mountain Memorial Association's minutes page: under "2026 Board Minutes" and "2026 Committee Minutes"
    (a heading for each year), one link a meeting, its words the day ("1September 21, 2026", "May 12, 2026 Special
    Called Mtg"). Board minutes are the board's; a committee's (the files are named Fin-Dev: Finance & Development)
    are a row of their own; a special called meeting listed among the committees is the board's. Where the words and
    the file's name give different days and a second file's name agrees with the first's, the files are believed
    (both Jan. 2020 entries say "January 17" over files named 1-27-2020)."""
    book, sect, year = Book(), "", None
    found: list[tuple[str, str, str, str, str]] = []           # (label day, file day, section, label, href)
    for ln in page_lines(html, url):
        t = ln["t"]
        if ln["h"] and re.search(r"minutes", t, re.I):
            sect = "committee" if re.search(r"committee", t, re.I) else "board"
            y = YEAR.search(t)
            year = int(y.group(1)) if y else year
            continue
        for label, href in ln["links"]:
            if not sect or not label or not (DOC_RX.search(href) or SHARED_DOC.search(href)):
                continue
            ld = (dates_in(label, year) or [""])[0]
            fd = (name_dates(href) or [""])[0]
            if ld or fd:
                found.append((ld, fd, sect, label, href))
    file_days: dict[str, int] = {}
    for _, fd, _, _, _ in found:
        file_days[fd] = file_days.get(fd, 0) + 1
    for ld, fd, sect, label, href in found:
        d = fd if (fd and ld and fd != ld and file_days.get(fd, 0) >= 2) or not ld else ld
        words = label + " " + file_name(href)
        special = bool(re.search(r"special[\s_-]*called", words, re.I))
        body = "" if sect == "board" or special else \
            ("Finance & Development Committee" if re.search(r"fin[\s_-]*dev|finance", words, re.I) else "Committee")
        m = book.meet(d, body, "special" if special else "")
        rest = label
        for rx in (DATE_LONG, DATE_NUM):
            rest = rx.sub(" ", rest, count=1)
        rest = squash(re.sub(r"^\d\b|\bmtg\b\.?|\bminutes\b", " ", rest, flags=re.I)).strip(" ,-–")
        m["name"] = m["name"] or ("Special Called Board Meeting" if special else (body or "Board of Directors Meeting"))
        if rest and not special and re.search(r"[A-Za-z]{4}", rest) and rest.lower() not in m["name"].lower():
            m["name"] = f"{m['name']} ({rest})"
        book.file(m, "minutes", "Minutes", href)
    return book.out()


def month_files(book: Book, items: list[tuple[int, int, str, str, str]]) -> int:
    """Files named by month alone ("2026 November Preliminary Agenda", "Board August 2026 Minutes") are filed with
    the board's one meeting of that month; with none or several known, they are left out (a meeting needs its day).
    items: (year, month, slot, label, href). Returns how many were left out."""
    left = 0
    for y, mo, slot, label, href in items:
        days = [k for k in book.m if k[0].startswith(f"{y}-{mo:02d}-") and not k[1] and not k[2]]
        if len(days) == 1:
            book.file(book.m[days[0]], slot, label, href)
        else:
            left += 1
    return left


MONTH_YEAR = re.compile(rf"\b(?:(20\d\d)\s+({_MON})|({_MON})\s+(20\d\d))\b", re.I)


def month_named(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """A year's meeting dates as a list under a heading that names the year ("2026 Board of Directors Meeting Dates",
    then "Wednesdays – 11am", "January 14", "April 15 (budget workshop)"), and under it the files, some named by day
    ("SGCCA 14 October 2026 Board Meeting Minutes Signed"), some by month alone ("SGCCA 2026 November Preliminary
    Agenda"), some a committee's ("Hotel Development Committee 10 November 2026 Preliminary Agenda"): the
    Savannah-Georgia Convention Center Authority; and a page of minutes named by month alone (the Macon-Bibb County
    Transit Authority), which are filed only with meetings already known from `ag["_known"]`."""
    book = Book()
    for k in ag.get("_known") or []:
        book.meet(*k)
    preset = set(book.m)
    year, t0, where, monthly, in_sched = None, "", "", [], False
    lines = page_lines(html, url)
    for i, ln in enumerate(lines):
        t = ln["t"]
        if ln["h"] or ln["bold"]:
            if re.search(r"meeting dates|meeting schedule", t, re.I) and YEAR.search(t):
                year, in_sched = int(YEAR.search(t).group(1)), True
                continue
            if ln["h"] and in_sched and not clock(t) and not DATE_NOYEAR.search(t):
                in_sched = False
        if in_sched and not ln["links"]:
            if clock(t) and not DATE_NOYEAR.search(t) and not t0:
                t0 = clock(t)
            if re.search(r"take place", t, re.I) and not DATE_NOYEAR.search(t):
                nxt = lines[i + 1]["t"] if i + 1 < len(lines) and "~" not in t else ""
                wm = re.search(r"take place\s+(.+?)\s*(?:~|$)", squash(f"{t} {nxt}"), re.I)
                where = squash(wm.group(1)) if wm else where
            dm = DATE_NOYEAR.match(t.strip("* "))
            if dm and len(t) < 60:
                d = _ok(year, _mon(dm.group(1)), int(dm.group(2)))
                if d:
                    m = book.meet(d)
                    note = squash(t[dm.end():]).strip(" *()")
                    nxt = lines[i + 1]["t"] if i + 1 < len(lines) else ""
                    if not note and re.fullmatch(r"\([^)]{3,40}\)", nxt):
                        note = nxt.strip("()")
                    m["name"] = "Board of Directors Meeting" + (f" ({note})" if note else "")
            continue
        for label, href in ln["links"]:
            if not label or not is_file_link(label, href):
                continue
            words = label + " " + file_name(href).replace("-", " ")
            k = _kind(label, href)
            if k == "doc" and not MEETING_WORDS.search(words):
                continue
            cm = re.search(r"([A-Z][\w&/' ]+? Committee)", label)
            body = squash(re.sub(r"^(?:SGCCA|MBCTA|MTA)\s+", "", cm.group(1))) if cm else ""
            body = squash(re.sub(rf"^(?:\d{{1,2}}\s+)?{_MON}\s+(?:\d{{1,2}},?\s+)?(?:20\d\d\s+)?", "", body, flags=re.I))
            ds = dates_in(label) or name_dates(href)
            if ds:
                m = book.meet(ds[0], body)
                m["name"] = m["name"] or (body or "Board of Directors Meeting")
                book.file(m, k, SLOT_LABEL.get(k, label), href)
                continue
            my = MONTH_YEAR.search(words)
            if my and not body:
                y, mo = (int(my.group(1)), _mon(my.group(2))) if my.group(1) else (int(my.group(4)), _mon(my.group(3)))
                monthly.append((y, mo, k, SLOT_LABEL.get(k, label), href))
    for m in book.m.values():
        if not m["body"] and (m["date"], "", "") not in preset:
            m["time"], m["where"] = m["time"] or (t0 if m["date"][:4] == str(year) else ""), m["where"] or (where if m["date"][:4] == str(year) else "")
    left = month_files(book, monthly)
    if left:
        ag.setdefault("_notes", []).append(f"{left} files named by month alone could not be given a day on {url}")
    return [m for m in book.out() if (m["date"], slug(m["body"]), m["mk"]) not in preset or any(m[s] for s in SLOTS) or m["docs"]]


GW_CALL = re.compile(r"\bconf(?:erence)?\.?\s+call\b", re.I)                          # how they met, not who
GW_SPECIAL = re.compile(r"\bspecial(?:\s+called)?(?=\s+(?:conf|meeting|mtg))", re.I)       # "... Special Meeting ...", "... Special Conference Call ..."
GW_LIST = re.compile(r"\bcommittee assignments\b", re.I)                                  # a list kept among the files
GW_OPEB = re.compile(r"\bOPEB Board of Trustees\b", re.I)                                 # the Finance Committee sitting as it
# "04-14-26 ...", "12-10-24-GWCCA ..." (a hyphen after the day only before a word: "09-16-17-27 ..." is two days)
GW_DAY = re.compile(r"\s*(\d{1,2}-\d{1,2}-(?:20)?\d\d)(?:\s*-\s*(?=\D)|\s+)(?=\S)(.+)$")
# "09-16 & 17-21 ...", "09-16-17-21 ...": two days, filed on the first
GW_DAYS = re.compile(r"\s*(\d{1,2})-(\d{1,2})(?:\s*&\s*|-)\d{1,2}-((?:20)?\d\d)\s+(.+)$")
# "CALL FOR SPECIAL BOG MEETING - 04-30-26": the day last, on a call or a notice of a meeting only (a roster "as of" a
# day, a schedule "- 11-05-26" are no meeting's files)
GW_DAY_LAST = re.compile(r"\s*((?=.*\b(?:call\s+for|notice)\b).*\bmeeting\b.*?)\s*[-–]\s*(\d{1,2}-\d{1,2}-(?:20)?\d\d)\s*$", re.I)
GW_NOTICE = re.compile(r"^(?:call\s+for|notice\s+of)\s+|\s+notice$", re.I)                 # "Call for ...", "Notice of ...", "... Notice": what the file is
GW_FILE = re.compile(r"^https://acrobat\.adobe\.com/id/\S+$", re.I)                      # a deck kept at Adobe


def gwcca_label(label: str) -> tuple[str, str] | None:
    """(the day, what the file is) of a file's label on the Authority's page; None when it carries no day."""
    if len(label) > 300:                           # no label: a paragraph that is a link
        return None
    for rx in (GW_DAY, GW_DAYS, GW_DAY_LAST):
        lm = rx.match(label)
        if not lm:
            continue
        day, what = ((lm.group(1), lm.group(2)) if rx is GW_DAY else (f"{lm.group(1)}-{lm.group(2)}-{lm.group(3)}", lm.group(4)) if rx is GW_DAYS
                     else (lm.group(2), lm.group(1)))
        ds = dates_in(day)
        if ds:
            # "GWCCA" before the words, and a second day written out before them ("& 9-15-22 GWCCA Board ...")
            what = re.sub(r"^&\s*\d{1,2}-\d{1,2}-(?:20)?\d\d\s+", "", what.strip())
            return ds[0], squash(re.sub(r"^GWCCA\s+", "", what).replace("_", " "))
    return None


def gwcca_about(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """The Georgia World Congress Center Authority's About page (read from the Internet Archive's copy: the live site
    turns automated readers away): "2026 Meeting Schedule" as a month and a day ("JAN" / "27"), and "Meeting Minutes
    and Materials", each file named by its day and who met ("04-14-26 Executive_Finance Committee Meeting Minutes").

    A committee is one sitting a day under one name, whatever its files' labels say (seen on the whole page, Nov. 20,
    2026: one sitting's agenda under "Joint Finance Executive Committee" and its minutes under "Executive_Finance
    Committee"; an agenda marked "Conference Call" or "Special Meeting" and the minutes not). The name is the body's
    `committees` entry the label matches (config/agencies.yaml), else the label's own without the words that say how
    they met. A conference call is the sitting's place, a special meeting its name.

    A label carries its day first ("04-14-26 ...", "12-10-24-GWCCA ..."), as two days ("09-16 & 17-21 ...", filed on
    the first) or last ("CALL FOR SPECIAL BOG MEETING - 04-30-26"); a file is a document by its address, or a deck
    kept at Adobe. A file whose label carries no day ("2024 BOG Planning Retreat Preliminary Agenda") is not filed:
    a meeting has a day. A file passed over on purpose is noted for the run (ag["_passed"])."""
    book, year, mon, in_sched, special, opeb = Book(), None, 0, False, set(), set()
    for ln in page_lines(html, url):
        t = ln["t"]
        sm = re.search(r"(20\d\d) meeting schedule", t, re.I)
        if sm:
            year, in_sched = int(sm.group(1)), True
            continue
        if in_sched and not ln["links"]:
            if re.fullmatch(rf"{_MON}\.?", t, re.I):
                mon = _mon(t)
                continue
            dm = re.fullmatch(r"(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?", t)
            if dm and mon:
                d = _ok(year, mon, int(dm.group(1)))
                if d:
                    m = book.meet(d, "", "retreat" if dm.group(2) else "")
                    m["name"] = f"Authority planning retreat ({t.strip()})" if dm.group(2) else "Board of Governors Meeting"
                mon = 0
                continue
            if ln["h"] and not re.search(r"schedule", t, re.I) and len(t) > 12:
                in_sched = False
        for label, href in ln["links"]:
            href = re.sub(r"(?:%22|[\"'])+$", "", href)      # an address written with one quotation mark too many
            got = gwcca_label(label)
            if not got or not (DOC_RX.search(href) or GW_FILE.match(href)):
                continue
            day, what = got
            if GW_LIST.search(what):               # "Committee Assignments by Committee": no sitting's file
                ag.setdefault("_passed", []).append(href)
                continue
            named = committee_name(ag, what)
            if named or re.search(r"committee", what, re.I):
                # the label's own name: not how they met, nor whose committee it is ("Board Executive Committee",
                # "Special Meeting of the Executive Committee")
                body = named or squash(re.sub(r"^(?:of\s+)?(?:the\s+)?(?:board(?:\s+of\s+governors)?\s+)?", "",
                                              _committee(GW_CALL.sub(" ", GW_SPECIAL.sub(" ", GW_NOTICE.sub(" ", what)))), flags=re.I))
                m = book.meet(day, body)
                m["name"] = m["name"] or body
                if GW_SPECIAL.search(what):
                    special.add((day, slug(body)))
                if GW_OPEB.search(what):
                    opeb.add((day, slug(body)))
            else:
                mk = "special" if re.search(r"special called", what, re.I) else ("retreat" if re.search(r"retreat", what, re.I) else "")
                m = book.meet(day, "", mk)
                m["name"] = m["name"] or {"special": "Board of Governors Special Called Meeting", "retreat": "Board of Governors Planning Retreat"}.get(mk, "Board of Governors Meeting")
            if GW_CALL.search(what):
                m["where"] = m["where"] or "Conference call"
            book.file(m, _kind(what, href), what, href)
    for m in book.m.values():                      # by what any of the sitting's labels said, in whatever order they stand
        k = (m["date"], slug(m["body"]))
        if m["body"] and (k in special or k in opeb):
            m["name"] = m["body"] + (" - OPEB Board of Trustees" if k in opeb else "") + (" Special Meeting" if k in special else "")
    return book.out()


def dated_lines(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """A page of lines, each a meeting's day with its hour and files ("Wednesday, March 25, 2026 - 11:00 a.m. -
    Agenda"; "December 2, 2025 Minutes"), under headings (or bold lines) that say whose they are. `section` (a
    pattern) keeps only the lines under a heading that matches; `skip_section` leaves those out. A heading that names
    a committee makes the lines under it that committee's. Georgia Public Broadcasting's page lists the Commission's
    dates and its Foundation's; the DNR's minutes page lists the Board's, each committee's and the North Georgia
    Mountains Authority's."""
    book = Book()
    want = re.compile(page.get("section") or ag.get("section") or "", re.I) if (page.get("section") or ag.get("section")) else None
    skip = re.compile(page.get("skip_section") or ag.get("skip_section") or "", re.I) if (page.get("skip_section") or ag.get("skip_section")) else None
    on, body, year = want is None, "", None
    for ln in page_lines(html, url):
        t = ln["t"]
        heading = bool(ln["h"] or ln["bold"]) and not ln["links"] and not dates_in(t) and 4 < len(t) < 160
        if heading and (ln["h"] or t.rstrip().endswith(":") or re.search(r"minutes|dates|meetings|committee|authority|board|commission", t, re.I)) \
                and not re.search(r"^\*|^note\b|\(cancel", t, re.I):
            y = YEAR.search(t)
            year = int(y.group(1)) if y else year
            on = (want.search(t) is not None if want else True) if (want is None or want.search(t) or ln["h"] or re.search(r"dates for|will meet", t, re.I)) else on
            if skip and skip.search(t):
                on = False
            body = _committee(t) if re.search(r"committee", t, re.I) and not re.search(r"committee meeting minutes", t, re.I) else ""
            continue
        if not on:
            continue
        ds = dates_in(t, year)
        if not ds:
            for label, href in ln["links"]:
                ds = dates_in(label, year) or name_dates(href, year)
                if ds:
                    break
        if not ds or not (ln["links"] or clock(t) or ln["li"]):
            continue
        m = book.meet(ds[0], body)
        m["name"] = m["name"] or body
        m["time"] = m["time"] or clock(t)
        if CANCEL.search(t) or any(CANCEL.search(l) for l, _ in ln["links"]):
            m["status"] = "cancelled"
        if re.search(r"\bvirtual\b", t, re.I) and not m["where"]:
            m["where"] = "Virtual"
        for label, href in ln["links"]:
            if label and (is_file_link(label, href) or FILE_WORDS.search(label)) and not CANCEL.search(label):
                k = _kind(label, href)
                book.file(m, k, SLOT_LABEL.get(k) or re.sub(r"\s*\(PDF[^)]*\)", "", label), href)
    return book.out()


def jekyll_board(html: str, url: str, ag: dict, page: dict) -> list[dict]:
    """The Jekyll Island Authority's board page: a table a year (date, place, documents), read by the general reader
    (struck-out or "Cancelled" rows are cancelled, "Tentative" is tentative, Adobe-hosted agendas, summaries and
    minutes are each in their own place). Added here: a "Finance Committee Meeting" row is the committee's, not the
    board's; the hour the page gives for the coming meeting; and the "Audio" link, which is one folder of all the
    recordings and so is kept as a file, not as that meeting's recording."""
    out = meetings_on(html, url)
    up = next((ln["t"] for ln in page_lines(html, url) if re.search(rf"^{WEEKDAY},?\s+{_MON}", ln["t"], re.I) and clock(ln["t"]) and " at " in ln["t"]), "")
    today = dt.date.today().isoformat()
    coming = min((m["date"] for m in out if m["date"] >= today and m["status"] != "cancelled"), default="")
    for m in out:
        if m["video"]:
            m["docs"].append({"t": "Audio (the authority's folder of board meeting recordings)", "u": m["video"]})
            m["video"] = ""
        cm = re.search(r"\b([A-Z][\w& ]*? Committee)\b", m["name"])
        if cm:
            m["body"] = cm.group(1)
        elif not m["name"]:
            m["name"] = "Board Meeting"
        elif not re.search(r"meeting", m["name"], re.I):
            m["name"] = f"Board Meeting ({m['name']})"
        if up and m["date"] == coming and not m["time"]:
            ud = DATE_NOYEAR.search(up)
            if ud and (_mon(ud.group(1)), int(ud.group(2))) == (int(m["date"][5:7]), int(m["date"][8:10])):
                m["time"] = clock(up)
    return out


READERS = {
    "general": None,                      # meetings_on: the default
    "govhub_event": govhub_event,
    "marta_schedule": marta_schedule,
    "marta_documents": marta_documents,
    "title_table": title_table,
    "gtea_board": gtea_board,
    "notice_posts": notice_posts,
    "smma_minutes": smma_minutes,
    "month_named": month_named,
    "gwcca_about": gwcca_about,
    "dated_lines": dated_lines,
    "jekyll_board": jekyll_board,
}


# ------------------------------------------------------------------------------------------- reading the bodies
def load(root: Path) -> list[dict]:
    p = Path(root) / "config" / "agencies.yaml"
    return (yaml.safe_load(p.read_text()) or {}).get("agencies") or [] if p.exists() else []


def page_entries(ag: dict) -> list[dict]:
    """A body's pages as entries: "https://..." -> {u: ...}; an entry may name its own reader, section, archive."""
    out = []
    for p in ag.get("pages") or []:
        e = {"u": p} if isinstance(p, str) else dict(p)
        if e.get("u"):
            out.append(e)
    return out


def page_urls(ag: dict) -> list[str]:
    return [e["u"] for e in page_entries(ag)]


def key_of(agency_id: str, m: dict) -> str:
    return f"ga-agency:{agency_id}|meeting|{m['date']}" + (f"|{slug(m['body'])}" if m["body"] else "") + (f"|{m['mk']}" if m["mk"] else "")


def from_meta(mt: dict) -> dict:
    """A stored meeting record's meta as a meeting."""
    m = new_meeting(str(mt.get("EventDate") or "")[:10], mt.get("committee") or "", mt.get("sitting") or "")
    m.update(time=mt.get("EventTime") or "", name=mt.get("what") or "", where=mt.get("location") or "", status=mt.get("status") or "",
             agenda=mt.get("EventAgendaFile") or "", minutes=mt.get("EventMinutesFile") or "", summary=mt.get("summary_file") or "",
             video=mt.get("EventVideoPath") or "", pub=mt.get("pub") or "", page=mt.get("found_on") or "",
             docs=[{"t": x.get("t") or x.get("l") or "File", "u": x["u"]} for x in (mt.get("docs") or []) if isinstance(x, dict) and x.get("u")])
    return m


def read_archived(wb, u: str, copies: int = 4):
    """(html, capture timestamp) of the newest EXISTING Wayback Machine copy of a page that is the site's own page
    (not its firewall's challenge), trying the newest `copies`; (None, "") when there is none. Nothing is ever asked
    to be captured."""
    from .archive_sources import is_real
    from .wayback import RAW

    for cap in reversed(wb.captures(u, newest=copies)):
        r = wb._get(RAW.format(ts=cap["timestamp"], url=cap["original"]))
        if r is not None and r.status == 200 and is_real(r.text):
            return r.text, cap["timestamp"]
        if wb.off:
            break
    return None, ""


def open_minutes(fetcher, store, ag: dict, found, deadline: float | None, cap: int, log=print) -> int:
    """Open a body's minutes files not opened yet, newest first (at most `cap`), and keep each as a document
    (doc_type "minutes") so the minutes reader (gacrawl/meeting_summaries.py) can say what the meeting decided.
    `found`: the meetings ({key: meeting}, or the older {day: {minutes, page}}). A file on a document service whose
    robots.txt keeps crawlers out (Adobe's) is linked, never opened."""
    from .ingest import pdf_to_record

    n = 0
    rows = sorted(((m.get("date") or k, m) for k, m in found.items() if m.get("minutes")), key=lambda x: x[0], reverse=True)
    for d, m in rows:
        if n >= cap or (deadline is not None and time.monotonic() > deadline - 30):
            break
        u = m["minutes"]
        if SHARED_DOC.search(u) or "web.archive.org" in u or store.url_seen(u):   # opened before (or refused before): once is enough
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
                                meta={"doc_type": "minutes", "opened": True, "agency": ag["id"], "found_on": m.get("page") or ""},
                                ocr_max_pages=0)
        except Exception as exc:  # noqa: BLE001
            log(f"agencies: {ag['id']} minutes {d}: unreadable ({type(exc).__name__})")
            continue
        rec.published, rec.jurisdiction, rec.evidence = d, "ga", "primary"
        rec.key = f"ga-agency:{ag['id']}|minutes|{u}"
        store.add(rec)
        n += 1
    return n


def _correct(found: dict, ag: dict) -> None:
    """The entry's `corrections`: a page's own mistake put right, each with its reason in the config
    ({d, b, set: {name, time, where, status}, drop: [agenda, ...], move: <day>})."""
    for c in ag.get("corrections") or []:
        for k in [k for k in found if k[0] == str(c.get("d")) and (c.get("b") is None or k[1] == slug(c["b"]))]:
            m = found[k]
            for f in c.get("drop") or []:
                if f in SLOTS:
                    m[f] = ""
            for f, v in (c.get("set") or {}).items():
                if f in ("name", "time", "where", "status", "body"):
                    m[f] = str(v)
            if c.get("move"):
                del found[k]
                m["date"] = str(c["move"])
                nk = (m["date"], slug(m["body"]), m["mk"])
                found[nk] = merge(found[nk], m) if nk in found else m


def read_body(fetcher, store, ag: dict, deadline: float | None, errors: list, wb=None, log=print, dry_run: bool = False) -> tuple[dict, dict]:
    """One body's pages read: ({(day, committee, sitting): meeting}, {pages read, archive day, ok}). `dry_run`: a copy
    that is not taken is not marked asked for today (the run that stores is to ask for it)."""
    from .store import utcnow  # noqa: F401  (the store stamps records itself)

    ag = dict(ag)                                  # the readers' notes for one another (_schedule, _known) stay per run
    aid = ag["id"]
    stored, live_on_file = {}, False
    for r in store.read(f"ga-agency:{aid}", "meeting"):
        mt = r.get("meta") or {}
        if not mt.get("withdrawn") and not mt.get("captured"):
            live_on_file = True                    # a reading of the body's own site, in this format or the one before
        if mt.get("fmt") == FMT:
            sm = from_meta(mt)
            stored[(sm["date"], slug(sm["body"]), sm["mk"])] = (sm, mt)
    queue = page_entries(ag)
    follow = [f.lower() for f in ag.get("follow") or []]
    depth = {e["u"]: 0 for e in queue}
    seen, found, read_ok, keep_stored = set(), {}, [], set()
    info = {"pages": 0, "captured": "", "failed": 0, "live": 0, "archived": 0, "copies": {}}
    cap_pages = int(ag.get("max_pages") or 14)
    today = dt.date.today()
    while queue and len(seen) < cap_pages:
        if deadline is not None and time.monotonic() > deadline:
            info["failed"] += 1
            break
        e = queue.pop(0)
        u = e["u"]
        if u in seen:
            continue
        seen.add(u)
        arch = bool(e.get("archive") or ag.get("source") == "archive")
        shown, captured, html, refused = u, "", None, ""
        if not arch:
            try:
                r = fetcher.get(u, use_cache=False)
            except Blocked as exc:
                # The site refuses the crawler (a 403, a firewall's challenge; its host is then left alone for days).
                # A body with no reading of its own site on file is read from the Internet Archive's newest existing copy
                # instead, as a body set down `source: archive` is (the owner's rule of Nov. 19, 2026), and looked for
                # again every recheck_days while its site refuses; never a page behind a login. A body that has
                # readings of its own site on file (of this format or the one before) keeps them as they are until its
                # site answers again: an older copy is not read over a newer reading.
                if live_on_file or CLOSED_REFUSAL.search(str(exc)):
                    errors.append(f"{aid} {u}: {type(exc).__name__}"[:160])
                    info["failed"] += 1
                    continue
                arch, refused = True, str(exc)[:80]
            except Exception as exc:  # noqa: BLE001 — a page that refuses is left for the next run
                errors.append(f"{aid} {u}: {type(exc).__name__}"[:160])
                info["failed"] += 1
                continue
            else:
                ctype = r.content_type or "html"
                if r.status != 200 or not re.search(r"html|xml|rss", ctype):
                    errors.append(f"{aid} {u}: HTTP {r.status}"[:160])
                    info["failed"] += 1
                    continue
                html, final, shown = r.text, r.url or u, r.url or u
                info["live"] += 1
        if arch:
            stamp = json.loads(store.kv_get(f"agency_archive:{aid}:{u}") or "{}")
            every = int(e.get("recheck_days") or ag.get("recheck_days") or 7)
            rev = READER_REV.get(e.get("reader") or ag.get("reader") or "general", 1)
            # a copy read by an earlier reader than today's (READER_REV) is read again now, whatever its days
            if (stamp.get("at") and int(stamp.get("rev") or 1) >= rev and (e.get("once") or (today - dt.date.fromisoformat(stamp["at"])).days < every)) \
                    or stamp.get("tried") == today.isoformat():
                # (tried: asked for earlier today and not taken, see not_taken below: once a day, not on every run)
                info["captured"] = max(info["captured"], stamp.get("as_of") or "")
                info["archived"] += 1
                keep_stored.add("*")               # not read again this run: what is stored stays as it is
                continue

            def not_taken(why: str, stamp=stamp, u=u, every=every) -> None:
                """A copy that came and is not read over what is on file: the stamp is as it was, marked asked for
                today (asked for again tomorrow), and the run is told; an error once it has gone on for `every` days."""
                since = stamp.get("since") or today.isoformat()
                if not dry_run:
                    store.kv_set(f"agency_archive:{aid}:{u}", json.dumps(stamp | {"tried": today.isoformat(), "since": since}))
                if (today - dt.date.fromisoformat(since)).days >= every:
                    errors.append(f"{aid} {u}: {why} (since {since})"[:300])
                else:
                    ag.setdefault("_notes", []).append(why)
                info["captured"] = max(info["captured"], stamp.get("as_of") or "")
                info["archived"] += 1
                keep_stored.add("*")
            if wb is None:
                from .wayback import Wayback

                wb = Wayback(fetcher, log=log)
            try:
                html, ts = read_archived(wb, u)
            except Exception as exc:  # noqa: BLE001
                html, ts = None, ""
                errors.append(f"{aid} {u}: archive {type(exc).__name__}"[:160])
            if not html:
                errors.append(f"{aid} {u}: " + (f"refused ({refused}), and " if refused else "") + "no readable Internet Archive copy"
                              + (f" ({wb.off})" if wb.off else ""))
                info["failed"] += 1
                keep_stored.add("*")
                continue
            from .wayback import SHOW, day

            # the latest copy of this page that a meeting on file was read from (by the records, not by the stamp)
            on_file = max((copy_of(sm["page"])[1] for sm, mt in stored.values()
                           if mt.get("captured") and not mt.get("withdrawn") and copy_of(sm["page"])[0] == u), default="")
            # ... and for a page whose meetings are filed under addresses of their own (each notice under its post):
            # the day of the last copy the reader found meetings on (the stamp moves to no other, below)
            last = day(on_file) if on_file else str(stamp.get("as_of") or "")
            if (on_file and ts < on_file) or (not on_file and last and day(ts) < last):
                # an earlier copy than that one (the later one did not answer this time): not read over it. Its
                # meetings would take the place of the later copy's, without the files posted since.
                not_taken(f"the Archive gave its copy of {day(ts)} of {u}, earlier than the one on file ({last}): not read over it")
                continue
            shown, captured, final = SHOW.format(ts=ts, url=u), day(ts), u
            info["captured"] = max(info["captured"], captured)
            info["archived"] += 1
            # ... with the number it had: run gives it its reader's once what an earlier reader filed has been judged
            store.kv_set(f"agency_archive:{aid}:{u}", json.dumps({"at": today.isoformat(), "as_of": captured} | ({"rev": stamp["rev"]} if stamp.get("rev") else {})))
        info["pages"] += 1
        read_ok.append(shown)
        name = e.get("reader") or ag.get("reader") or "general"
        fn = READERS.get(name)
        if name not in READERS:
            errors.append(f"{aid}: no reader named {name!r} (see READERS in gacrawl/agencies.py)")
            continue
        try:
            got = fn(html, final, ag, e) if fn else meetings_on(html, final, ag.get("event_links") or "", ag.get("match") or "")
        except Exception as exc:  # noqa: BLE001 — one page's layout changed: said, and the rest goes on
            errors.append(f"{aid} {u}: reader {name}: {type(exc).__name__}: {exc}"[:200])
            info["failed"] += 1
            if arch:                               # the copy's stamp as it was: asked for again tomorrow, not after its days
                store.kv_set(f"agency_archive:{aid}:{u}", json.dumps(stamp | ({} if dry_run else {"tried": today.isoformat(), "since": stamp.get("since") or today.isoformat()})))
            continue
        if arch and not got and last:
            # a later copy on which the reader finds no meeting at all (a page that is not the body's page any more,
            # however real it looks): it says nothing of what is on file, least of all that a coming meeting is gone.
            # The stamp is as it was: its day stays that of the last copy the reader found meetings on.
            if dry_run:
                store.kv_set(f"agency_archive:{aid}:{u}", json.dumps(stamp))
            not_taken(f"the Archive's copy of {captured} of {u} lists no meeting: what is on file from its copy of {last} stays")
            continue
        if arch and not got:
            # no meeting found and none known: the stamp names no copy's day (a copy without meetings is never "the
            # one on file" that an earlier copy is measured against), and the page is looked for again after its days
            store.kv_set(f"agency_archive:{aid}:{u}", json.dumps({"at": today.isoformat()} | ({"rev": stamp["rev"]} if stamp.get("rev") else {})))
        if not fn:
            # a year's dates kept in a file: this year's and next year's are opened and read (two files at most)
            for sf in [x for x in schedule_files(html, final) if x["year"] in (today.year, today.year + 1)][:2]:
                try:
                    fr = fetcher.get(sf["u"], use_cache=False)
                    if fr.status != 200 or fr.content[:5] != b"%PDF-":
                        raise ValueError(f"HTTP {fr.status}, not a PDF")
                    import io

                    import pdfplumber

                    with pdfplumber.open(io.BytesIO(fr.content)) as pdf:
                        text = "\n".join(pg.extract_text() or "" for pg in pdf.pages[:6])
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{aid} {sf['u']}: {type(exc).__name__}: {exc}"[:200])
                    continue
                days = {d for d in dates_in(text)} | {d for ln in text.splitlines() for d in dates_in(ln, sf["year"])}
                for d in sorted(x for x in days if x[:4] == str(sf["year"])):
                    sm = new_meeting(d)
                    sm["docs"] = [{"t": sf["t"], "u": sf["u"]}]
                    got.append(sm)
        if arch and fn and got:
            # this page, read just now from a copy by the reader of this number (a copy as late as any on file)
            info["copies"][u] = {"rev": READER_REV.get(name, 1), "ts": ts, "at": today.isoformat(), "as_of": captured}
        for m in got:
            m["page"] = m["page"] or m["event"] or shown
            if arch:
                m["captured"] = captured
                if READER_REV.get(name, 1) > 1:
                    m["reader_rev"] = READER_REV[name]
            k = (m["date"], slug(m["body"]), m["mk"])
            if k in found and e.get("_event"):     # the meeting's own page says more than the list that led to it
                old = found[k]
                found[k] = merge(m, old)
                for f in ("captured", "event_read"):
                    if old.get(f):
                        found[k][f] = old[f]
                found[k]["event_read"] = True
            elif k in found:                       # the same meeting on two pages (the schedule, then the minutes)
                merge(found[k], m)
                if arch:
                    found[k].setdefault("captured", captured)
            else:
                found[k] = m
        # the readers' notes for the pages after this one: who is scheduled when, and which meetings are known
        sched: dict[tuple, set] = {}
        for (d, b, mk) in found:
            sched.setdefault((b, mk), set()).add(d)
        ag["_schedule"], ag["_known"] = sched, [(m["date"], m["body"], m["mk"]) for m in found.values()]
        # each meeting's own event page (georgia.gov): its place, hour, agenda and files. A page read before for a
        # meeting more than two weeks past is not read again; what it gave is kept.
        if ag.get("event_pages") and not e.get("_event"):
            host = urlsplit(final).netloc
            for k, m in sorted(found.items(), reverse=True):
                pg = m["event"]
                if not pg or not EVENT_URL.search(pg) or urlsplit(pg).netloc != host or pg in seen or any(q["u"] == pg for q in queue):
                    continue
                old = stored.get(k)
                if old and old[1].get("event_read") and m["date"] < (today - dt.timedelta(days=14)).isoformat():
                    keep_stored.add(k)
                    continue
                queue.append({"u": pg, "reader": "govhub_event", "_event": True})
                depth[pg] = 2
        if follow and depth.get(u, 0) == 0 and not arch:
            from bs4 import BeautifulSoup

            host = urlsplit(final).netloc
            for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                h = urljoin(final, a["href"]).split("#")[0]
                if urlsplit(h).netloc == host and h not in seen and all(q["u"] != h for q in queue) and not DOC_RX.search(h) and \
                        any(f in h.lower() or f in a.get_text(" ", strip=True).lower() for f in follow):
                    queue.append({k2: v for k2, v in e.items() if k2 not in ("u", "_event")} | {"u": h})
                    depth[h] = 1
    _correct(found, ag)
    for m in found.values():
        d = ag.get("defaults") or {}
        if not m["body"] and not m["mk"]:
            m["name"] = m["name"] or str(d.get("name") or "")
        if d.get("time") and not m["time"] and not m["body"] and not m["mk"] and m["status"] != "cancelled":
            m["time"] = str(d["time"])
        if d.get("where") and not m["where"] and not m.get("captured"):
            m["where"] = str(d["where"])
    # what the store holds and this run could not read again is kept (an event page not reopened; a page that failed)
    for k, (sm, mt) in stored.items():
        if k in found and (k in keep_stored or info["failed"]):
            merge(found[k], sm)
            found[k]["event_read"] = found[k].get("event_read") or bool(mt.get("event_read"))
            if mt.get("captured"):
                found[k].setdefault("captured", mt["captured"])
    info.update(read_ok=read_ok, stored=stored, notes=ag.get("_notes") or [], passed=set(ag.get("_passed") or []),
                complete=not info["failed"] and "*" not in keep_stored)
    return found, info


def run(fetcher, store, root, deadline: float | None = None, only: list[str] | None = None, dry_run: bool = False,
        log=print, minutes_cap: int = 6) -> dict:
    """Each body's pages (and the pages they link, one level, where `follow` says so) read into meeting records. The
    body read longest ago goes first, so a run that ends at its deadline starts the next one where this one stopped."""
    from .store import Record

    out = {"agencies": 0, "pages": 0, "meetings": 0, "new": 0, "by": {}, "errors": [], "notes": []}
    todo = [ag for ag in load(root) if not only or ag["id"] in only]
    todo.sort(key=lambda ag: store.kv_get(f"agency_read:{ag['id']}") or "")
    # one for the whole run: after the Archive's first 429, 5xx or refusal it is off for every body that follows (a new
    # one per body asked it again for each)
    from .wayback import Wayback

    wb = Wayback(fetcher, log=log)
    today = dt.date.today().isoformat()
    for ag in todo:
        if deadline is not None and time.monotonic() > deadline:
            out["errors"].append(f"out of time before {ag['id']}: read first on the next run")
            continue
        found, info = read_body(fetcher, store, ag, deadline, out["errors"], wb=wb, log=log, dry_run=dry_run)
        out["pages"] += info["pages"]
        out["notes"] += [f"{ag['id']}: {n}" for n in info["notes"]]
        ms = list(found.values())
        out["agencies"] += 1
        out["by"][ag["id"]] = {"meetings": len(ms), "with_agenda": sum(1 for m in ms if m["agenda"]),
                               "with_minutes": sum(1 for m in ms if m["minutes"]), "with_summary": sum(1 for m in ms if m["summary"]),
                               "with_time": sum(1 for m in ms if m["time"]), "with_place": sum(1 for m in ms if m["where"]),
                               "cancelled": sum(1 for m in ms if m["status"] == "cancelled"),
                               "newest": max((m["date"] for m in ms), default=""), "oldest": min((m["date"] for m in ms), default="")}
        if info["captured"]:
            out["by"][ag["id"]]["archive_as_of"] = info["captured"]
        out["meetings"] += len(ms)
        if not dry_run and (info["live"] or info["archived"]):
            # how the body's pages were read this time: from its own site, or (its site refuses, or it is set down so)
            # all from the Archive's copies. A run that read nothing leaves the last answer. The export's `source`.
            store.kv_set(f"agency_via:{ag['id']}", "live" if info["live"] else "archive")
        if dry_run:
            out["by"][ag["id"]]["sample"] = sorted(ms, key=lambda m: m["date"], reverse=True)[:3]
            continue
        for k, m in found.items():
            page = m["page"]
            meta = {"fmt": FMT, "agency": ag["id"], "agency_name": ag.get("agency") or "", "EventBodyName": ag["name"], "EventDate": m["date"],
                    "EventTime": m["time"], "EventAgendaFile": m["agenda"], "EventMinutesFile": m["minutes"], "summary_file": m["summary"],
                    "EventVideoPath": m["video"], "docs": m["docs"], "what": m["name"], "committee": m["body"], "sitting": m["mk"],
                    "status": m["status"], "location": m["where"], "found_on": page, "pub": m["pub"], "opened": True}
            if m.get("captured"):
                meta.update(captured=m["captured"], via="wayback", archived=True)
                if m.get("reader_rev"):
                    meta["reader_rev"] = m["reader_rev"]
            if m.get("event_read"):
                meta["event_read"] = True
            tail = "-".join(x for x in (m["date"], slug(m["body"]), m["mk"]) if x)
            rec = Record(f"ga-agency:{ag['id']}", "meeting", f"{page}#meeting-{tail}", title=f"{ag['name']} {m['date']}",
                         text=" · ".join(x for x in (ag["name"], m["body"], m["date"], m["name"], m["where"]) if x), published=m["date"],
                         channel="archive" if m.get("captured") else "html", jurisdiction="ga", evidence="primary", key=key_of(ag["id"], m), meta=meta)
            out["new"] += store.add(rec)
        # Two kinds of meeting the store holds and this run did not find are kept, marked withdrawn, and left out of the
        # export:
        #   gone: a coming meeting that its page, read in full just now (every page of the body was), no longer lists:
        #     the body took it off its schedule (a date that moved). For a page read from the Archive's copies: a copy
        #     on which the reader found meetings, as late as any on file (read_body reads no other).
        #   refiled: a meeting of any day that an earlier reader filed from a copy of a page read again just now by a
        #     corrected one (READER_REV), when today's reader does not file it and files every one of its files under
        #     another meeting of the same day (or passed them over on purpose): a committee under another spelling, a
        #     sitting split in two. Whatever copy was read and however much of it came, no file stops being shown
        #     on its day by this. A meeting with no file is never judged so, nor one read from the body's own site: a
        #     page drops its old meetings itself.
        day_files: dict[str, set] = {}
        for m in found.values():
            day_files.setdefault(m["date"], set()).update({m[s] for s in SLOTS if m[s]} | {x["u"] for x in m["docs"]})
        for k, (sm, mt) in info["stored"].items():
            if k in found or mt.get("withdrawn"):
                continue
            copy = info["copies"].get(copy_of(sm["page"])[0]) if mt.get("captured") else None
            gone = info["complete"] and sm["date"] >= today and (sm["page"] in info["read_ok"] or bool(copy))
            files = {sm[s] for s in SLOTS if sm[s]} | {x["u"] for x in sm["docs"]}
            refiled = bool(copy) and int(mt.get("reader_rev") or 1) < copy["rev"] and bool(files) and files <= (day_files.get(sm["date"], set()) | info["passed"])
            if gone or refiled:
                rec = Record(f"ga-agency:{ag['id']}", "meeting", f"{sm['page']}#meeting-{sm['date']}", title=f"{ag['name']} {sm['date']}",
                             text=f"{ag['name']} · {sm['date']} · no longer listed", published=sm["date"], channel="html", jurisdiction="ga",
                             evidence="primary", key=key_of(ag["id"], sm), meta={**mt, "withdrawn": today})
                store.add(rec)
                out["notes"].append(f"{ag['id']}: {sm['date']} is no longer on {sm['page']}: withdrawn" if gone else
                                    f"{ag['id']}: {sm['date']} {sm['body'] or 'the board'}: filed by an earlier reader of {sm['page']}, not by today's: withdrawn")
        for u, c in info["copies"].items():        # judged: the copy's stamp takes its reader's number (read_body left it as it was)
            if c["rev"] > 1:
                store.kv_set(f"agency_archive:{ag['id']}:{u}", json.dumps({"at": c["at"], "as_of": c["as_of"], "rev": c["rev"]}))
        if info["complete"]:
            store.kv_set(f"agency_read:{ag['id']}", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        opened = open_minutes(fetcher, store, ag, {key_of(ag["id"], m): m for m in ms}, deadline, minutes_cap, log=log) if minutes_cap else 0
        out["minutes_opened"] = out.get("minutes_opened", 0) + opened
        log(f"agencies: {ag['name']}: {len(ms)} meetings ({out['by'][ag['id']]['with_agenda']} with an agenda, "
            f"{out['by'][ag['id']]['with_minutes']} with minutes; {opened} minutes files opened now)")
    if not dry_run:
        store.flush()
    return out


# ------------------------------------------------------------------------------------------- the bundle's file
MEETING_KEYS = ("d", "t", "n", "b", "w", "st", "a", "m", "s", "v", "x", "p", "pub", "seen", "upd")
BODY_KEYS = ("id", "kind", "name", "agency", "pages", "links", "source", "archive_as_of", "meetings")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?")
_URL = re.compile(r"https?://\S+")

STATE_MEETINGS_SCHEMA = """
## state_meetings.json (format 2)

Written by `gacrawl/agencies.py` (`export_state_meetings`); every body and every meeting passes `validate_body` /
`validate_meeting` before it is written. A row that does not fit is left out and counted in the export's warning
(`left_out`), never written malformed.

Top level: `{as_of, format: 2, note, not_read: [{name, why, u}], bodies: [body], legislature: {source, meetings}}`.
Every body in config/agencies.yaml is written every night, read or not, with or without meetings: a body that is
missing was dropped or renamed.
`legislature` is the General Assembly's committee schedule, unchanged (d, t, c, b, w, a, v, n, x).

### Body (`bodies[]`), sorted by kind (board, authority, transit), then name
| key | meaning |
|---|---|
| id | short id, stable (required) |
| kind | `board` (a state board or commission), `authority` (a state authority), `transit` (a regional transit authority created by Georgia law) (required) |
| name | the body that meets (required) |
| agency | the department it governs, or what it is (required, may repeat the name) |
| pages | the body's own pages the meetings are read from (required, may be empty) |
| links | pages the body sends readers to for what it does not post itself: `[{l: label, u: address}]`; absent when none |
| source | `live` (read from the body's site) or `archive` (the site refuses automated readers: read from an existing Internet Archive copy) (required) |
| archive_as_of | the day the archive copy was captured, YYYY-MM-DD; only with source `archive` (absent only while no copy has been read yet, and then the body has no meetings) |
| meetings | rows, newest first (by d, then t) |

### Meeting, keys in this order; a key with nothing known is absent, never an empty string
| key | meaning |
|---|---|
| d | the day, YYYY-MM-DD (required) |
| t | the hour it starts, HH:MM, 24-hour, the body's local time |
| n | the meeting's name: "Board Meeting", "Audit Committee", "Board Work Session", "Special Called Board Meeting" |
| b | the committee or sub-body that met, when it is not the full board |
| w | the place |
| st | `scheduled` (a coming day), `held` (the day has passed and the body's page does not say it was cancelled), `cancelled` (the page says so), `tentative` (the page says the date may change) (required) |
| a | the agenda (an address) |
| m | the approved minutes |
| s | the body's own account of the meeting posted before or instead of minutes: a summary, a brief, "actions" |
| v | the recording |
| x | other files: `[{l: label, u: address}]`, all of them |
| p | the body's page that lists the meeting; for a meeting read from an archive copy, that copy (required) |
| pub | when the body published the entry, ISO 8601; only where the page or feed states it |
| seen | when this system first stored the meeting, ISO 8601 UTC (required) |
| upd | when the stored meeting last changed, ISO 8601 UTC (required) |

One row per body + committee (b) + day (d) + hour (t). The full board sitting twice in a day (a work session, then
the board meeting) is two rows told apart by t; where the body posts no hour, the second sitting carries its own
name as b ("Board Work Session"). upd is never before seen. Every row can be opened: p is always there. A cancelled meeting is a row with st `cancelled`.
"""


def validate_meeting(row: dict) -> list[str]:
    """What is wrong with an exported meeting row; [] when it fits the format."""
    bad = []
    if not isinstance(row, dict):
        return ["not an object"]
    keys = list(row)
    if any(k not in MEETING_KEYS for k in keys):
        bad.append(f"unknown key {[k for k in keys if k not in MEETING_KEYS]}")
    elif keys != [k for k in MEETING_KEYS if k in row]:
        bad.append("keys out of order")
    for k in ("d", "st", "p", "seen", "upd"):
        if k not in row:
            bad.append(f"{k} missing")
    for k, v in row.items():
        if k == "x":
            if not isinstance(v, list) or not v or any(not isinstance(i, dict) or list(i) != ["l", "u"] or not isinstance(i["l"], str) or not i["l"].strip()
                                                       or not isinstance(i["u"], str) or not _URL.fullmatch(i["u"]) for i in v):
                bad.append("x is not a list of {l, u}")
        elif not isinstance(v, str) or not v.strip():
            bad.append(f"{k} empty or not text")
    s = lambda k: row.get(k) if isinstance(row.get(k), str) else ""  # noqa: E731
    if "d" in row and (not _DAY.fullmatch(s("d")) or not _ok(*map(int, s("d").split("-")))):
        bad.append("d is not a day")
    if "t" in row and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", s("t")):
        bad.append("t is not HH:MM")
    if "st" in row and s("st") not in STATUSES:
        bad.append("st is not a status")
    for k in ("a", "m", "s", "v", "p"):
        if k in row and not _URL.fullmatch(s(k)):
            bad.append(f"{k} is not an address")
    if "pub" in row and not _ISO.fullmatch(s("pub")):
        bad.append("pub is not ISO 8601")
    for k in ("seen", "upd"):
        if k in row and not _UTC.fullmatch(s(k)):
            bad.append(f"{k} is not ISO 8601 UTC")
    return bad


def validate_body(body: dict) -> list[str]:
    """What is wrong with an exported body (its own keys; its meetings are checked row by row)."""
    bad = []
    if list(body) != [k for k in BODY_KEYS if k in body] or any(k not in body for k in BODY_KEYS if k not in ("archive_as_of", "links")):
        bad.append("keys missing, unknown or out of order")
    for k in ("id", "name", "agency"):
        if not isinstance(body.get(k), str) or not body.get(k, "").strip():
            bad.append(f"{k} empty")
    if body.get("kind") not in KINDS:
        bad.append("kind is not board, authority or transit")
    if body.get("source") not in ("live", "archive"):
        bad.append("source is not live or archive")
    if ("archive_as_of" in body and (body.get("source") != "archive" or not _DAY.fullmatch(str(body["archive_as_of"])))) or \
            (body.get("source") == "archive" and "archive_as_of" not in body and body.get("meetings")):
        bad.append("archive_as_of must be a day, with source archive only (and always when it has meetings)")
    if "links" in body and (not isinstance(body["links"], list) or not body["links"] or any(
            not isinstance(i, dict) or list(i) != ["l", "u"] or not _URL.fullmatch(str(i["u"])) for i in body["links"])):
        bad.append("links is not a list of {l, u}")
    if not isinstance(body.get("pages"), list) or any(not isinstance(u, str) or not _URL.fullmatch(u) for u in body.get("pages") or []):
        bad.append("pages is not a list of addresses")
    if not isinstance(body.get("meetings"), list):
        bad.append("meetings is not a list")
    return bad


def _utc(s: str) -> str:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def _u(u: str) -> str:
    """An address as it is written out: a space in a file's name as %20 (MARTA's "Agenda PCP 11192026.pdf")."""
    return (u or "").strip().replace(" ", "%20")


def meeting_row(r: dict, today: str) -> dict:
    """A stored meeting record as an export row (keys in order, nothing empty). A record stored before format 2 is
    read by the same rules: its "minutes" that is a summary by its name becomes the summary."""
    mt = r.get("meta") or {}
    m = from_meta(mt)
    if mt.get("fmt") != FMT and m["minutes"] and _kind("", m["minutes"]) == "summary":
        m["summary"], m["minutes"] = m["summary"] or m["minutes"], ""
    st = m["status"] if m["status"] in ("cancelled", "tentative") else ("scheduled" if m["date"] >= today else "held")
    if st == "tentative" and m["date"] < today:
        st = "held"
    if not m["status"] and CANCEL.search(m["name"]):
        st = "cancelled"
    x = [{"l": squash(i["t"]) or "File", "u": _u(i["u"])} for i in m["docs"] if _URL.fullmatch(_u(i["u"]))]
    row = {"d": m["date"], "t": m["time"], "n": squash(m["name"]), "b": squash(m["body"]), "w": squash(m["where"]), "st": st, "a": _u(m["agenda"]),
           "m": _u(m["minutes"]), "s": _u(m["summary"]), "v": _u(m["video"]), "x": x, "p": _u(m["page"]), "pub": m["pub"],
           "seen": _utc(r.get("first_seen") or ""), "upd": _utc(r.get("changed_at") or r.get("first_seen") or "")}
    return {k: v for k, v in row.items() if v not in ("", None, [])}


def export_state_meetings(store, root: Path, path: Path, today: str | None = None, log=print) -> dict:
    """state_meetings.json in the one strict format (STATE_MEETINGS_SCHEMA): every listed body with the meetings kept
    here, and the General Assembly's committee meetings. Every body and row is validated; what does not fit is left
    out and counted (`left_out`, `warning`). Returns the counts."""
    import datetime as _dt

    today = today or _dt.datetime.now(_dt.timezone.utc).date().isoformat()

    def slim(d: dict) -> dict:
        return {k: v for k, v in d.items() if v not in ("", None, [], False)}

    ags, left, why = [], 0, {}
    for ag in load(root):
        recs = [r for r in store.read(f"ga-agency:{ag['id']}", "meeting") if not (r.get("meta") or {}).get("withdrawn")]
        # the board's own meeting of a day before a sitting of its own that day (a retreat, a work session), whichever
        # the store gives first: the sitting is the one told apart by its name below. (Nov. 20, 2026, the World Congress
        # Center Authority, Sept. 17, 2020: the retreat came first, and the board's meeting was left out as a second row.)
        recs.sort(key=lambda r: bool((r.get("meta") or {}).get("sitting")))
        new_days = {str((r.get("meta") or {}).get("EventDate") or "")[:10] for r in recs if (r.get("meta") or {}).get("fmt") == FMT}
        rows, seen_rows = [], set()
        for r in recs:
            mt = r.get("meta") or {}
            if mt.get("fmt") != FMT and (ag.get("legacy") == "drop" or str(mt.get("EventDate") or "")[:10] in new_days):
                continue                           # a record from before format 2 that the body's reader has since replaced
            row = meeting_row(r, today)
            bad = validate_meeting(row)
            k = (row.get("d"), row.get("b", ""), row.get("t", ""))
            if not bad and k in seen_rows and mt.get("sitting") and "b" not in row and row.get("n"):
                # the full board sitting twice in a day with no hour posted (a work session, then the board meeting):
                # the second sitting is told apart by its own name
                row = {kk: vv for kk in MEETING_KEYS for vv in [row.get(kk) if kk != "b" else row["n"]] if vv is not None}
                k = (row["d"], row["b"], row.get("t", ""))
            if not bad and k in seen_rows:
                bad = ["a second row for the same body, committee, day and hour"]
            if bad:
                left += 1
                why[bad[0]] = why.get(bad[0], 0) + 1
                continue
            seen_rows.add(k)
            rows.append(row)
        rows.sort(key=lambda x: (x["d"], x.get("t", ""), x.get("b", "")), reverse=True)
        # read from the Archive's copies: set down so in the config, or its own site refused the last time it was read
        # and the copies were read instead (read_body). The second only with a copy's day to show.
        conf_arch = ag.get("source") == "archive"
        via_arch = conf_arch or store.kv_get(f"agency_via:{ag['id']}") == "archive"
        as_of = max((str((r.get("meta") or {}).get("captured") or "") for r in recs), default="") if via_arch else ""
        if via_arch and not as_of:
            as_of = max((json.loads(store.kv_get(f"agency_archive:{ag['id']}:{u}") or "{}").get("as_of") or "" for u in page_urls(ag)), default="")
        arch = conf_arch or bool(via_arch and as_of)
        links = [{"l": squash(str(x.get("t") or x.get("l") or "")) or "More", "u": _u(x["u"])} for x in (ag.get("links") or []) if isinstance(x, dict) and x.get("u")]
        body = {"id": ag["id"], "kind": ag.get("kind") or "board", "name": ag["name"], "agency": ag.get("agency") or ag["name"],
                "pages": list(dict.fromkeys(_u(u) for u in page_urls(ag)))}
        if links:
            body["links"] = links
        body["source"] = "archive" if arch else "live"
        as_of = as_of or (max((json.loads(store.kv_get(f"agency_archive:{ag['id']}:{u}") or "{}").get("as_of") or "" for u in page_urls(ag)), default="") if arch else "")
        if arch and as_of:
            body["archive_as_of"] = as_of
        body["meetings"] = rows
        bad = validate_body(body)
        if bad:                                    # every listed body is written every night (one missing reads as dropped):
            left += len(rows)                      # a body that does not fit is written with what does, its rows left out
            why[f"body {ag['id']}: {bad[0]}"] = 1
            body = {k: v for k, v in body.items() if k not in ("archive_as_of", "links")} | {"meetings": []}
            body["kind"] = body["kind"] if body["kind"] in KINDS else "board"
        ags.append(body)
    ags.sort(key=lambda a: (KINDS.index(a["kind"]), a["name"].casefold(), a["id"]))
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
    out = {"as_of": today, "format": FMT, "not_read": not_read,
           "note": "State boards', state authorities' and regional transit authorities' meetings as their own pages list "
                   "them (source \"archive\": from an existing Internet Archive copy of a site that refuses automated "
                   "readers, captured on archive_as_of), and the General Assembly's committee meetings from the "
                   "legislature's schedule. A meeting with no agenda or minutes link here had none on the page when it was read.",
           "bodies": ags,
           "legislature": {"source": "https://www.legis.ga.gov/schedule/all", "meetings": leg}}
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    counts = {"bodies": len(ags), "agencies": sum(1 for a in ags if a["meetings"]), "agency_meetings": sum(len(a["meetings"]) for a in ags),
              "legislature_meetings": len(leg), "left_out": left}
    if left:
        counts["warning"] = f"{left} rows did not fit the format and were left out: " + "; ".join(f"{k} ({v})" for k, v in sorted(why.items()))
        log(f"state meetings: WARNING {counts['warning']}")
    return counts
