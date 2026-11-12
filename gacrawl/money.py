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

    from gacrawl.money import lines
    for L in lines(store): L["amount"], L["via"], L["meta"], L["url"] ...
"""
from __future__ import annotations

import re
from collections import defaultdict

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


def lines(store, stats: dict | None = None):
    """Every contribution, expenditure and loan line that counts, once, with how it counts:
    {"rid", "url", "published", "meta", "amount", "type", "via", "platform"}. `stats` gets the counts left out."""
    own: dict[str, set] = defaultdict(set)
    covered: dict[str, str] = {}
    for r in store.iter_query("kind='contribution'"):
        m = r.get("meta") or {}
        if m.get("candidate_id") and m.get("committee"):
            own[m["candidate_id"]].add(m["committee"])
        note_covered(m, covered)
    yield from clean(store.iter_query("kind='contribution'"), own, stats, covered=covered)


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


def clean(records, own: dict | None = None, stats: dict | None = None, covered: dict | None = None):
    """The rules over any iterable of contribution records (a whole store, or one candidate's). `covered`: the newest
    date of each Georgia filer's regular reports (note_covered); without it, Two Business Day lines are read as covered
    only when the same records carry a regular line of that filer from a later date (records are read twice)."""
    st = stats if stats is not None else {}
    for k in ("memo", "repeat", "legacy", "transfer", "lines"):
        st.setdefault(k, 0)
    own = own if own is not None else {}
    if covered is None:
        records = list(records)
        covered = {}
        for r in records:
            note_covered(r.get("meta") or {}, covered)
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
        # the line's own identity (the store's rows carry their row id as "id"; a record built in memory has only its key):
        # the connections graph keeps two lines with the same donor, recipient, date, amount and page apart by it
        yield {"rid": r.get("rid") or r.get("id") or r.get("key") or (m.get("transaction_id") or m.get("line")),
               "key": r.get("key"), "url": r.get("url"), "published": r.get("published"), "meta": m, "amount": amt,
               "type": m.get("type") or "contribution", "via": via_of(m), "platform": platform_of(m)}
