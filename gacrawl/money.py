"""One set of rules for counting campaign money, used by every export that adds it up (donors.json for the Donors
tab, the connections graph, each profile's money summary), so they can never disagree again.

A contribution record is a filing line as the filer reported it (FEC schedule A, a Georgia CCDR line, an import).
How a line counts:
  memo        FEC memo lines (memo code X) repeat money already counted on another line (an earmark's conduit
              total, a partnership's attribution): never counted
  repeat      the same line read twice: an FEC line is one line however often it is read (the FEC's committee and
              transaction id, which an amendment keeps); another filer's line, once per recipient, donor, date, amount,
              kind and filing line (a report posted twice, or amended)
  legacy      FEC lines read before the crawler kept the FEC's own fields (no memo code, no transaction id): they
              cannot be told from memo lines, so they are not counted at all; the FEC step reads each committee again
              and replaces them (finance.py _fec_committee)
  transfer    from another committee of the same candidate: counted where it was first given
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


def via_of(m: dict) -> str:
    """The conduit an earmarked gift passed through, or ''."""
    if m.get("conduit_committee_name") or m.get("conduit_name"):
        return (m.get("conduit_committee_name") or m.get("conduit_name")).strip()
    mt = EARMARK.search(m.get("memo_text") or "")
    if mt:
        return mt.group(1).strip()
    return ""


def platform_of(m: dict) -> str:
    """A platform's own lump line (ActBlue, WinRed ... passing on many people's gifts at once)."""
    p = PLATFORMS.get(m.get("contributor_committee_id") or "")
    if p:
        return p
    pm = PLATFORM_NAME.match((m.get("name") or "").strip())
    return PLATFORM_LABEL[pm.group(1).lower()] if pm else ""


def lines(store, stats: dict | None = None):
    """Every contribution, expenditure and loan line that counts, once, with how it counts:
    {"rid", "url", "published", "meta", "amount", "type", "via", "platform"}. `stats` gets the counts left out."""
    own: dict[str, set] = defaultdict(set)
    for r in store.iter_query("kind='contribution'"):
        m = r.get("meta") or {}
        if m.get("candidate_id") and m.get("committee"):
            own[m["candidate_id"]].add(m["committee"])
    yield from clean(store.iter_query("kind='contribution'"), own, stats)


def clean(records, own: dict | None = None, stats: dict | None = None):
    """The rules over any iterable of contribution records (a whole store, or one candidate's)."""
    st = stats if stats is not None else {}
    for k in ("memo", "repeat", "legacy", "transfer", "lines"):
        st.setdefault(k, 0)
    own = own if own is not None else {}
    seen: set = set()
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
            continue
        if (m.get("memo_code") or "").upper() == "X":
            st["memo"] += 1
            continue
        if fec and m.get("committee"):
            dk = ("fec", m["committee"], m.get("transaction_id") or m.get("sub_id") or r.get("key"))
        else:
            dk = (m.get("committee") or m.get("recipient_entity") or _norm(m.get("recipient") or ""), _norm(m.get("name") or ""),
                  (m.get("date") or "")[:10], round(amt, 2), m.get("type") or "contribution", m.get("line") or "")
        if dk in seen:
            st["repeat"] += 1
            continue
        seen.add(dk)
        if m.get("candidate_id") and m.get("contributor_committee_id") and m["contributor_committee_id"] in own.get(m["candidate_id"], ()):
            st["transfer"] += 1
            continue
        st["lines"] += 1
        yield {"rid": r.get("rid"), "url": r.get("url"), "published": r.get("published"), "meta": m, "amount": amt,
               "type": m.get("type") or "contribution", "via": via_of(m), "platform": platform_of(m)}
