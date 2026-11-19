"""One set of rules for counting campaign money, used by every export that adds it up (donors.json for the Donors
tab, the connections graph, each profile's money summary), so they can never disagree again.

A contribution record is a filing line as the filer reported it (FEC schedule A, a Georgia CCDR line, an import).
How a line counts:
  memo        FEC memo lines (memo code X) repeat money already counted on another line (an earmark's conduit
              total, a partnership's attribution): never counted
  repeat      the same line read twice: an FEC line is one line however often it is read (the FEC's committee and
              transaction id, which an amendment keeps); another filer's line, once per recipient, donor, date, amount,
              kind and filing line (a report posted twice, or amended). An FEC line filed again in a second filing
              under a new transaction id (an amendment filed as another report: Jim Barksdale's two $1 million loans
              of 2016 were in his amended July report and again in a "Q1" filing, so they counted twice and his total
              read $4.75 million against the FEC's $2.94 million) counts once: for each committee, donor, date, amount
              and line (11, 12 or 13), the lines of one filing count, and copies in other filings beyond that number
              are repeats
  late        a Georgia Two Business Day report (gifts of $1,000 or more in the days before an election, listed again
              in the next regular report): counted only for dates after the filer's newest regular report, so the
              last weeks before an election are on the site before the year-end report, and never counted twice
  legacy      FEC lines read before the crawler kept the FEC's own fields (no memo code, no transaction id): they
              cannot be told from memo lines, so they are not counted at all; the FEC step reads each committee again
              and replaces them (finance.py _fec_committee)
  transfer    from another committee of the same candidate: counted where it was first given
  not a gift  FEC schedule A lines other than 11 (contributions), 12 (transfers) and 13 (loans): refunds and rebates
              from vendors (offsets to operating expenditures), loan repayments, dividends and other receipts. Money
              that came back to the committee, not money anyone gave (a fundraising firm's refunds once showed as
              $1.5 million "given" to a senator)
  via         an earmarked gift: one person's money that passed through a conduit (ActBlue, WinRed, AIPAC PAC ...).
              The donor is the person; the conduit is named beside it. A conduit's own lump line (a platform's
              transfer, not itemized by person) counts toward what the recipient raised, with no donor
  own         everything else: the donor's own money (a person, a business, a PAC giving from its own funds)

A line's date is the filer's, typed by hand, and some cannot be right: "2028-04-01" in a report the FEC received on
May 12, 2022 (2022 typed as 2028), "2001-01-10" in a Georgia report filed in 2022 or later (2021 typed as 2001). Counted
as filed, they made years and election cycles that have not happened yet. A date its own filing rules out (after the
day the FEC received the filing, more than a month after the date a Georgia report is named for, after today, or, for
a line not filed with the FEC, more than EARLY_YEARS years before the committee's first filing) is set aside: the line
counts under the date the filing's period ends, or under no date when the filing does not say (L["date"], with the
reason in L["redated"]). The amount counts either way. FEC lines are never held to the first filing: the FEC returns
each line from the latest amendment, under the amendment's image number, so a committee that re-filed its old reports
seems to have first filed years after its first gifts (one Georgia congressman's reports, re-filed in December 2020,
carry all his 2016 gifts), and the day the FEC received the filing already bounds the line from above.

    from gacrawl.money import lines
    for L in lines(store): L["amount"], L["via"], L["meta"], L["url"], L["date"] ...
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from collections import defaultdict
from functools import lru_cache

PLATFORMS = {"C00694323": "WinRed", "C00401224": "ActBlue"}
PLATFORM_NAME = re.compile(r"^(winred|actblue|democracy engine|anedot)\b", re.I)
PLATFORM_LABEL = {"winred": "WinRed", "actblue": "ActBlue", "democracy engine": "Democracy Engine", "anedot": "Anedot"}
EARMARK = re.compile(r"earmark\w*\s+(?:through|via|by)\s+(.+?)(?:[.;,]|$)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# words a filer writes in place of the conduit's name ("EARMARKED THROUGH THIS ORGANIZATION" is ActBlue's own phrase,
# copied into recipients' reports): not a conduit anyone could look up
NOT_A_CONDUIT = re.compile(r"(?i)^(this|the|our|that)\s+(organization|org|committee|conduit|pac|account)\b|^(see|below|above)\b")


def via_of(m: dict) -> str:
    """The conduit an earmarked gift passed through, or ''."""
    if m.get("conduit_committee_name") or m.get("conduit_name"):
        return (m.get("conduit_committee_name") or m.get("conduit_name")).strip()
    mt = EARMARK.search(m.get("memo_text") or "")
    if mt:
        v = mt.group(1).strip()
        return "" if NOT_A_CONDUIT.search(v) else v
    return ""


def platform_of(m: dict) -> str:
    """A platform's own lump line (ActBlue, WinRed ... passing on many people's gifts at once), or a filer's line of
    unitemized gifts (many small gifts reported as one sum, no donor named)."""
    if m.get("unitemized"):
        return "Unitemized small gifts"
    p = PLATFORMS.get(m.get("contributor_committee_id") or "")
    if p:
        return p
    pm = PLATFORM_NAME.match((m.get("name") or "").strip())
    return PLATFORM_LABEL[pm.group(1).lower()] if pm else ""


def lines(store, stats: dict | None = None, today: str = ""):
    """Every contribution, expenditure and loan line that counts, once, with how it counts:
    {"rid", "url", "published", "meta", "amount", "type", "via", "platform", "date", "redated"}. `stats` gets the
    counts left out."""
    own: dict[str, set] = defaultdict(set)
    covered: dict[str, str] = {}
    first: dict[str, str] = {}
    today = today or _today()
    for r in store.iter_query("kind='contribution'"):
        m = r.get("meta") or {}
        if m.get("candidate_id") and m.get("committee"):
            own[m["candidate_id"]].add(m["committee"])
        note_covered(m, covered)
        note_filed(m, first, today)
    yield from clean(store.iter_query("kind='contribution'"), own, stats, covered=covered, first=first, today=today)


# ----------------------------------------------------------------------------- a line's date
EARLY_YEARS = 4        # a gift this long before its committee's first filing is a typed year (2001 for 2021), not a gift
NAMED_SLACK = 31       # days a Georgia line may run past the date its report is named for (filers list a late gift or two)
# where an FEC report's period ends, by its report type: quarterly, year-end, mid-year; monthly reports (M2 to M12)
# end with the month before. Pre- and post-election reports and the rest end close to the day they are filed.
PERIOD_END = {"Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "YE": (12, 31), "MY": (6, 30)}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MON = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(?:uary|ruary|ch|il|e|y|ust|t|tember|ober|ember)?\.?"
_NAMED = (re.compile(r"(?i)\b(?P<y>(?:19|20)\d\d)[ ,/-]+" + _MON + r"[ -]+(?P<d>\d{1,2})(?:st|nd|rd|th)?\b"),  # 2026 Oct 20 CCDR
          re.compile(r"(?i)\b" + _MON + r" +(?P<d>\d{1,2})(?:st|nd|rd|th)?,? +(?P<y>(?:19|20)\d\d)\b"),          # June 30th, 2022
          re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>(?:19|20)\d\d)\b"),                                       # 06/30/2022
          re.compile(r"\b(?P<y>(?:19|20)\d\d)-(?P<m>\d\d)-(?P<d>\d\d)\b"))


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


@lru_cache(maxsize=1)
def _ga_since() -> str:
    from .ethics import SINCE_YEAR

    return f"{SINCE_YEAR}-01-01"


def _iso(y: int, mo: int, d: int) -> str:
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return ""


@lru_cache(maxsize=65536)
def _image_day(ymd: str) -> str:
    return _iso(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))


@lru_cache(maxsize=65536)
def named_date(s: str) -> str:
    """The date a report's own name or period gives ("2026 Oct 20 CCDR", "01/01/2022 - 06/30/2022": the last one, where
    the period ends), or ''. Georgia's report names repeat across millions of lines, so each is read once."""
    found = []
    for rx in _NAMED:
        for x in rx.finditer(s or ""):
            g = x.groupdict()
            mo = int(g["m"]) if g.get("m") else MONTHS.get((g.get("mon") or "").lower(), 0)
            d = _iso(int(g["y"]), mo, int(g["d"])) if mo else ""
            if d:
                found.append((x.end(), d))
    return max(found)[1] if found else ""


@lru_cache(maxsize=65536)
def _period_end(report_type: str, filed: str) -> str:
    """The last day an FEC report covers: by its type, the latest end of such a period on or before the day it was
    filed (a year-end report filed in January 2023 covers through December 31, 2022); else the day it was filed."""
    rt = (report_type or "").strip().upper()
    md = PERIOD_END.get(rt)
    mo = md[0] if md else (int(rt[1:]) - 1 if re.fullmatch(r"M([2-9]|1[0-2])", rt) else 0)
    if not mo:
        return filed
    y = int(filed[:4])
    for yy in (y, y - 1):
        end = _iso(yy, mo, md[1] if md else calendar.monthrange(yy, mo)[1])
        if end and end <= filed:
            return end
    return filed


def filing_of(m: dict, today: str = "") -> tuple[str, str, str]:
    """(filed, through, latest) for the filing a line was read from, each '' when the line does not say: the day the
    filing was received (FEC: an 18-digit image number begins with it, as 20220512... is May 12, 2022) or the date a
    Georgia report is named for or a posted report's period ends; the date its period ends (what a date the filing rules
    out is replaced with); and the latest date a line in it can carry."""
    today = today or _today()
    img = str(m.get("image_number") or "")
    if m.get("filing_source") == "FEC" and len(img) == 18 and img.isdigit():
        filed = _image_day(img[:8])
        if "1990" <= filed <= today:
            return filed, _period_end(m.get("report_type") or "", filed), filed
    rep = m.get("report") if isinstance(m.get("report"), dict) else {}
    named = named_date(m.get("report_name") or "") or named_date(rep.get("period") or "")
    if named and named <= today:
        return named, named, (dt.date.fromisoformat(named) + dt.timedelta(days=NAMED_SLACK)).isoformat()
    return "", "", ""


def filer_key(m: dict) -> str:
    """The committee a line was filed by: the FEC's committee id, the Commission's filer registration, else the
    recipient as named."""
    return m.get("committee") or m.get("ga_filer") or str(m.get("recipient_entity") or "") or _norm(m.get("recipient") or "")


def note_filed(m: dict, first: dict, today: str = "") -> None:
    """The earliest filing each committee's lines were read from. The Commission's current systems hold the reports
    filed from ethics.SINCE_YEAR on, so a line from them whose report gives no date was filed no earlier than that.
    FEC lines are left out: their image number is the latest amendment's, which can be years after the gift (see the
    module's note), and an FEC line with no committee id is keyed by its recipient, whom a Georgia committee's lines can
    share."""
    if m.get("filing_source") == "FEC":
        return
    f = filing_of(m, today)[0]
    if not f and m.get("ga_system"):
        f = _ga_since()
    k = filer_key(m)
    if f and k and (k not in first or f < first[k]):
        first[k] = f


def dated(m: dict, d: str, first: str = "", today: str = "") -> tuple[str, str]:
    """(the date a line counts under, why the filed date was set aside or ''). `d` is the line's date as filed, `first`
    the day its committee first filed. A date after today, after the filing could carry it, or (not for FEC lines, whose
    first filing an amendment can move years later) more than EARLY_YEARS years before the committee's first filing is
    replaced with the day the filing's period ends, or with '' (no cycle, no year) when the filing does not say."""
    today = today or _today()
    if not d:
        return d, ""
    _, through, latest = filing_of(m, today)
    floor = (f"{int(first[:4]) - EARLY_YEARS}{first[4:10]}"
             if first[:4].isdigit() and m.get("filing_source") != "FEC" else "")
    why = ("after_filing" if latest and d > latest else "after_today" if d > today
           else "before_first_filing" if floor and d < floor else "")
    if not why:
        return d, ""
    return (through if through and through <= today else ""), why


def note_covered(m: dict, covered: dict) -> None:
    """The newest date each Georgia filer's regular reports reach (a Two Business Day line after it is not listed in a
    regular report yet)."""
    if m.get("ga_filer") and not m.get("tbd"):
        d = (m.get("date") or "")[:10]
        if d > covered.get(m["ga_filer"], ""):
            covered[m["ga_filer"]] = d


def _name_key(s: str) -> str:
    """A donor's name as a set of words ("PAC, FRANKLIN FORUM" and "FRANKLIN FORUM PAC" are one name)."""
    return " ".join(sorted(re.findall(r"[a-z0-9]+", (s or "").lower())))


def clean(records, own: dict | None = None, stats: dict | None = None, covered: dict | None = None,
          first: dict | None = None, today: str = ""):
    """The rules over any iterable of contribution records (a whole store, or one candidate's). `covered`: the newest
    date of each Georgia filer's regular reports (note_covered); without it, Two Business Day lines are read as covered
    only when the same records carry a regular line of that filer from a later date (records are read twice). `first`:
    each committee's first filing (note_filed), for dates long before it; read from the same records when `covered` is
    not given, else no date is checked against it."""
    st = stats if stats is not None else {}
    for k in ("memo", "repeat", "legacy", "transfer", "lines"):
        st.setdefault(k, 0)
    own = own if own is not None else {}
    today = today or _today()
    if covered is None:
        records = list(records)
        covered = {}
        fresh = first is None
        first = {} if fresh else first
        for r in records:
            note_covered(r.get("meta") or {}, covered)
            if fresh:
                note_filed(r.get("meta") or {}, first, today)
    first = first if first is not None else {}
    seen: set = set()
    filed: dict = {}         # (committee, donor, date, amount, line) -> {filing: lines counted from it}
    for r in records:
        m = r.get("meta") or {}
        amt = m.get("amount")
        if amt is None:
            continue
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            continue
        if not (m.get("recipient") or m.get("candidate_id") or m.get("committee")):
            st["unattributed"] = st.get("unattributed", 0) + 1
            continue          # no filer named: not anyone's money (a blank form read as a report)
        fec = m.get("filing_source") == "FEC"
        if fec and m.get("committee") and not m.get("fec_v"):
            st["legacy"] += 1
            # whose: the page marks exactly those campaigns' totals as waiting for the re-read
            who = m.get("candidate_id") or m.get("leadership_pac_of") or m.get("committee")
            lb = st.setdefault("legacy_by", {})
            lb[who] = lb.get(who, 0) + 1
            continue
        ln = str(m.get("line_number") or "").upper().lstrip("L")
        if fec and ln and not ln.startswith(("11", "12", "13")):
            st["not_a_gift"] = st.get("not_a_gift", 0) + 1
            continue
        if (m.get("memo_code") or "").upper() == "X":
            st["memo"] += 1
            continue
        if m.get("tbd"):
            # a Two Business Day line: only for the days no regular report of this filer reaches yet
            if (m.get("date") or "")[:10] <= covered.get(m.get("ga_filer") or "", ""):
                st["late_listed_again"] = st.get("late_listed_again", 0) + 1
                continue
            dk = ("tbd", m.get("ga_filer"), _name_key(m.get("name") or ""), (m.get("date") or "")[:10], round(amt, 2))
        elif fec and m.get("committee"):
            dk = ("fec", m["committee"], m.get("transaction_id") or m.get("sub_id") or r.get("key"))
        else:
            dk = (m.get("committee") or m.get("recipient_entity") or _norm(m.get("recipient") or ""), _norm(m.get("name") or ""),
                  (m.get("date") or "")[:10], round(amt, 2), m.get("type") or "contribution", m.get("line") or "")
        if dk in seen:
            st["repeat"] += 1
            continue
        seen.add(dk)
        if fec and m.get("committee") and m.get("file_number"):
            # the same line in a second filing under a new transaction id: as many as one filing holds, no more
            g = filed.setdefault((m["committee"], _name_key(m.get("name") or ""), (m.get("date") or "")[:10], round(amt, 2), ln[:2]), {})
            f = str(m["file_number"])
            g[f] = g.get(f, 0) + 1
            if g[f] <= max((v for k, v in g.items() if k != f), default=0):
                st["repeat"] += 1
                st["refiled"] = st.get("refiled", 0) + 1
                continue
        if m.get("candidate_id") and m.get("contributor_committee_id") and m["contributor_committee_id"] in own.get(m["candidate_id"], ()):
            st["transfer"] += 1
            continue
        st["lines"] += 1
        # the date it counts under: as filed, unless its own filing rules that date out (see the module's note)
        d, why = dated(m, (m.get("date") or r.get("published") or "")[:10], first.get(filer_key(m), ""), today)
        # the line's own identity (the store's rows carry their row id as "id"; a record built in memory has only its key):
        # the connections graph keeps two lines with the same donor, recipient, date, amount and page apart by it
        yield {"rid": r.get("rid") or r.get("id") or r.get("key") or (m.get("transaction_id") or m.get("line")),
               "key": r.get("key"), "url": r.get("url"), "published": r.get("published"), "meta": m, "amount": amt,
               "type": m.get("type") or "contribution", "via": via_of(m), "platform": platform_of(m),
               "date": d, "redated": why}
