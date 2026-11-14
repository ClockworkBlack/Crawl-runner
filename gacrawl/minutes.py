"""Council minutes -> motions and who voted how.

Most Georgia cities are not on Legistar; their roll calls live in minutes PDFs.
This deriver reads minutes text and extracts, per motion:
  what was moved (and any ordinance/resolution number), who moved and seconded,
  the result and tally, and named votes when the minutes give them
  ("Ayes: Smith, Jones. Nays: Brown"; "4-1 (Brown opposed)"; "Smith - Yes";
  "unanimous" with the members listed as present at the top).

Records: agenda_item (one per motion) and vote (when any member's position is known).
Each named vote carries its basis:
  named      the minutes list that member under ayes/nays or in a roll call
  moved /    the member made or seconded the motion (treated as a yes)
  seconded
  unanimous  "carried unanimously" and the member was listed as present (the
             presiding officer is left out unless a tally shows they voted)
  tally      "carried 4-1 (Brown opposed)" with 5 voting members present: the four
             unnamed members are recorded as yes only when the counts add up
Nothing is inferred beyond that: "motion carried" with no names yields a motion
record and no vote record.
"""
from __future__ import annotations

import re

from .derive import Deriver, register
from .store import Record
from .topics import tag

MINUTES_HINT = re.compile(r"\bminutes\b", re.I)
TITLE_RX = re.compile(r"\b(council\s*(?:member|man|woman|person)|councilmember|councilwoman|councilman|commissioner|alderman|"
                      r"alderwoman|mayor\s+pro\s+tem(?:pore)?|mayor|chair(?:man|woman|person)?|vice[- ]chair(?:man|woman)?|"
                      r"member|dr|mr|mrs|ms|post\s+\d+|district\s+\d+|ward\s+\d+)\b\.?\s*", re.I)
PRESIDING_RX = re.compile(r"\b(mayor(?!\s+pro)|chair(?:man|woman|person)?)\b", re.I)
NAME = r"(?-i:(?:[A-Z]\.|[A-Z][A-Za-z'’\-]*[A-Za-z])(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'’\-]*[A-Za-z])){0,3})"
TITLE = (r"(?:(?:council\s*(?:member|man|woman|person)|councilmember|commissioner|alder(?:man|woman)|mayor\s+pro\s+tem(?:pore)?|"
         r"mayor|chair(?:man|woman|person)?|vice[- ]chair(?:man|woman)?|dr|mr|mrs|ms)\.?\s+)?")
WHO = TITLE + NAME
SEC = r"\s*[,.;]?\s*(?:and\s+|with\s+)?(?:a\s+)?(?:seconded|second(?:ed)?)\s+(?:by\s+)?"
MOTION_RX = re.compile(
    r"(?:a\s+)?motion\s+(?:was\s+)?(?:made\s+)?by\s+(?P<mover1>" + WHO + r")" + SEC + r"(?P<sec1>" + WHO + r")\s*[,.;:]?\s*(?:to\s+)?(?P<what1>(?:[^.;]|\.(?=\s?\d))*)"
    r"|(?:a\s+)?motion\s+(?:was\s+)?(?:made\s+)?by\s+(?P<mover5>" + WHO + r")\s+(?:to\s+)?(?P<what5>(?:[^.;]|\.(?=\s?\d))*)(?:[.;]?" + SEC + r"(?P<sec5>" + WHO + r"))?"
    r"|(?P<mover2>" + WHO + r")\s+(?:made\s+(?:a|the)\s+motion|moved|motioned)\s*(?:,\s*)?(?:to\s+)?(?P<what2>(?:[^.;]|\.(?=\s?\d))*?)(?:" + SEC + r"(?P<sec2>" + WHO + r"))?\s*[.;](?!\s?\d)"
    r"|motion\s+(?:to\s+)?(?P<what3>(?:[^.;]|\.(?=\s?\d))*?)\s+(?:was\s+)?made\s+by\s+(?P<mover3>" + WHO + r")(?:" + SEC + r"(?P<sec3>" + WHO + r"))?\s*[.;](?!\s?\d)"
    r"|motion\s*:\s*(?P<mover4>" + WHO + r")\s*[,.;]\s*second(?:ed)?\s*:\s*(?P<sec4>" + WHO + r")\s*[,.;]",
    re.I)
SEC2_RX = re.compile(r"(?P<sec>" + WHO + r")\s+(?:seconded(?:\s+the\s+motion)?|provided\s+the\s+second|offered\s+(?:a|the)\s+second)\b", re.I)
SECBY_RX = re.compile(r"second(?:ed)?\s+(?:was\s+)?(?:by|from)\s+(?P<sec>" + WHO + r")", re.I)
LIST_RX = re.compile(r"^\s*(?:\(?\d{1,3}[.)]|\(?[A-Za-z][.)]|[IVXLC]+\.)\s+")
PROCEDURAL_RX = re.compile(r"\b(adjourn|recess|executive\s+session|approve\s+(?:the\s+)?(?:minutes|agenda|consent\s+agenda)|"
                           r"excuse|close\s+the\s+(?:public\s+)?hearing|open\s+the\s+(?:public\s+)?hearing)\b", re.I)
RESULT_RX = re.compile(r"\b(?:motion|vote|it)\s+(?:was\s+)?(?:carried|carries|passed|passes|approved|adopted|failed|fails|denied|"
                       r"defeated|died|tabled)\b|\b(?:carried|passed|approved|adopted|failed|denied|defeated)\s+"
                       r"(?:unanimously|\d+\s*[-–]\s*\d+|by\s+a\s+vote|with)", re.I)
TALLY_RX = re.compile(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b")
UNANIMOUS_RX = re.compile(r"\bunanimous(?:ly)?\b", re.I)
AYES_RX = re.compile(r"\b(?:ayes?|yeas?|yes|in\s+favor|voting\s+(?:yes|aye|in\s+favor))\s*[:\-–]\s*(?P<names>[^.;]+?)"
                     r"(?=\s*(?:\b(?:nays?|noes?|no|against|opposed|abstain\w*|absent)\b\s*[:\-–]|[.;]|$))", re.I)
NAYS_RX = re.compile(r"\b(?:nays?|noes?|no|against|opposed|voting\s+(?:no|nay|against))\s*[:\-–]\s*(?P<names>[^.;]+?)"
                     r"(?=\s*(?:\b(?:ayes?|yeas?|abstain\w*|absent)\b\s*[:\-–]|[.;]|$))", re.I)
ABSTAIN_RX = re.compile(r"\b(?:abstain(?:ed|ing|s)?|abstentions?)\s*[:\-–]\s*(?P<names>[^.;]+?)(?=[.;]|$)", re.I)
OPPOSED_RX = re.compile(r"\(?\s*(?P<names>" + NAME + r"(?:\s*(?:,|and)\s*" + NAME + r")*)\s+(?:opposed|voting\s+no|voted\s+no|"
                        r"against|dissenting|voting\s+nay)\s*\)?", re.I)
ROLLCALL_RX = re.compile(r"(?P<name>" + NAME + r")\s*[-–:]\s*(?P<vote>yes|no|aye|nay|yea|abstain(?:ed)?|absent|recused?)\b", re.I)
PRESENT_RX = re.compile(r"\b(?:members?\s+|those\s+|council\s+members?\s+|commissioners?\s+)?present(?:\s+were|\s+was|\s+included)?"
                        r"\s*[:\-–]?\s*(?P<names>[^.]{5,600}?)(?:\.\s|\n\s*\n|\babsent\b|\balso\s+present\b)", re.I | re.S)
ORD_RX = re.compile(r"\b(ordinance|resolution)\s*(?:no\.?|number|#)?\s*([0-9]{2,4}[-–][0-9A-Za-z]{1,6}|[0-9]{1,5})\b", re.I)
NOISE = re.compile(r"\b(motion|vote|carried|passed|none|all|approved|seconded|second|the|was|were|members?|council)\b", re.I)
# the clerk's closing lines, never a motion's words: Hull's "Respectfully submitted, Sandra Pou, City Clerk" (read by
# OCR as "Re pectfully submitted, Aa/laPoit andra Pou") went out as the motion of the roll call before it (Nov 13, 2026)
SIGNOFF_RX = re.compile(r"^\W*(re\s?s?pectfully\s+submitted|submitted\s+by|attest(ed)?\b|approved\s*:|minutes\s+(recorded|taken|"
                        r"prepared|transcribed)\s+by|(recorded|prepared|transcribed)\s+by|signed\s*:|clerk'?s?\s+signature)", re.I)
# the meeting's own date as the minutes' heading prints it: "Monday, May 11th, 2026", "February 27,2025"
HEAD_DATE_RX = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
                          r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*((?:19|20)\d\d)\b|"
                          r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b", re.I)
_MONTH_N = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _says_more(sentence: str) -> bool:
    """Does a motion's sentence say what was moved, beyond who moved and seconded it ("Motion by Councilmember Bowes;
    second by Councilmember Hunter" does not)?"""
    s = re.sub(WHO, " ", sentence)
    s = re.sub(r"(?i)\b(a|an|the|first|motion|made|was|were|by|and|with|seconded|second(?:ed)?|moved|motioned|from|"
               r"following|after|then|on|of)\b|[^A-Za-z]+", " ", s)
    return len(s.split()) >= 2


def meeting_date(text: str, title: str = "") -> str | None:
    """The date in the minutes' title or heading (their first 600 characters), as YYYY-MM-DD; None when neither has one.
    Only the heading: a date further in is usually another meeting's ("approve the minutes for April 13th")."""
    import datetime as dt

    for part in (title or "", (text or "")[:600]):
        for m in HEAD_DATE_RX.finditer(part):
            try:
                if m.group(1):
                    d = dt.date(int(m.group(3)), _MONTH_N[m.group(1).lower()[:3]], int(m.group(2)))
                else:
                    d = dt.date(int(m.group(6)), int(m.group(4)), int(m.group(5)))
            except (ValueError, KeyError):
                continue
            if 1990 <= d.year <= dt.date.today().year + 1:
                return d.isoformat()
    return None


def _person(s: str) -> str:
    s = TITLE_RX.sub("", s.strip(" ,.;:\n"))
    s = re.sub(r"\s+", " ", s).strip(" ,.;:")
    s = re.sub(r"^(?:the\s+)", "", s, flags=re.I)
    return s[:60]


def _names(s: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|\band\b|&)\s*", s)
    out = []
    for p in parts:
        n = _person(p)
        if n and n[0].isupper() and len(n.split()) <= 4 and not NOISE.search(n):
            out.append(n)
    return list(dict.fromkeys(out))


def attendees(text: str) -> tuple[list[str], set[str]]:
    """(members listed as present, those of them who preside: mayor / chair)."""
    m = PRESENT_RX.search(text[:5000])
    if not m:
        return [], set()
    raw = m.group("names")
    present = _names(raw)
    presiding = set()
    for chunk in re.split(r"\s*(?:,|;|\band\b|&)\s*", raw):
        n = _person(chunk)
        if n in present and PRESIDING_RX.search(chunk):
            presiding.add(n)
    return present, presiding


def _match_present(name: str, present: list[str]) -> str:
    """'Smith' -> 'John Smith' when the minutes list members present."""
    if not name:
        return name
    last = name.split()[-1].lower()
    full = [p for p in present if p.split()[-1].lower() == last]
    if len(full) == 1:
        return full[0]
    full = [p for p in present if p.lower() == name.lower()]
    return full[0] if full else name


def _norm_vote(v: str) -> str:
    v = v.lower()
    return "yes" if v in ("yes", "aye", "yea") else "no" if v in ("no", "nay") else "abstain" if v.startswith("abstain") \
        else "recused" if v.startswith("recus") else "absent"


def parse_motions(text: str) -> list[dict]:
    present, presiding = attendees(text)
    voting = [p for p in present if p not in presiding]
    out = []
    paras = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.;])\s*\n", text) if p.strip()]
    for i, para in enumerate(paras):
        flat = LIST_RX.sub("", re.sub(r"\s+", " ", para))
        if not re.search(r"\bmotion\b|\bmoved\b|\bmotioned\b", flat, re.I):
            continue
        m = MOTION_RX.search(flat)
        g = m.groupdict() if m else {}
        mover = _person(next((g[k] for k in ("mover1", "mover5", "mover2", "mover3", "mover4") if g.get(k)), ""))
        sec = _person(next((g[k] for k in ("sec1", "sec5", "sec2", "sec3", "sec4") if g.get(k)), ""))
        what = next((g[k] for k in ("what1", "what5", "what2", "what3") if g.get(k)), "").strip(" .;:,")
        if not sec:
            s2 = SECBY_RX.search(flat) or SEC2_RX.search(flat)
            sec = _person(s2.group("sec")) if s2 else ""
            if sec and mover and sec == mover:
                sec = ""
        if what and SIGNOFF_RX.search(what):
            what = ""
        if not what or len(what) < 4:
            w = re.search(r"\bmotion\s+to\s+([^.;]{4,240})", flat, re.I)
            if w:
                what = w.group(1).strip(" .;:,")
            else:
                first = re.split(r"(?<=[.;])\s", flat, maxsplit=1)[0].strip(" .;:,")
                what = first[:240] if not re.match(r"motion\b", first, re.I) else flat[:240]
                if m:
                    # the sentence the motion is in, when it says more than who moved and seconded ("the meeting was
                    # adjourned after a motion by Paige Phillips and a second by Steven Hollingsworth"): the paragraph's
                    # first sentence is often another matter's, or the clerk's closing lines
                    a = max(flat.rfind(". ", 0, m.start()), flat.rfind("; ", 0, m.start()))
                    # to the end of the sentence that names the mover and seconder (the match itself can run on into the
                    # clerk's closing lines)
                    e = max((m.end(k) for k in ("mover1", "sec1", "mover5", "sec5", "mover2", "sec2", "mover3", "sec3", "mover4", "sec4")
                             if m.group(k)), default=m.end())
                    b = re.search(r"[.;](?:\s|$)", flat[e:])
                    own = flat[a + 2 if a >= 0 else 0: e + (b.start() if b else len(flat) - e)].strip(" .;:,")[:240]
                    if _says_more(own) or SIGNOFF_RX.search(what):
                        what = own
            if SIGNOFF_RX.search(what):
                what = "Motion"
        res = RESULT_RX.search(flat)
        result = ""
        if res:
            r = res.group(0).lower()
            result = "failed" if re.search(r"fail|denied|defeat|died", r) else "tabled" if "tabled" in r else "passed"
        tally = TALLY_RX.search(flat[res.start():] if res else flat)
        if tally and not result:
            a, b = int(tally.group(1)), int(tally.group(2))
            result = "passed" if a > b else "failed" if b > a else ""
        votes: dict[str, dict] = {}

        def put(name, vote, basis):
            n = _match_present(name, present)
            if not n:
                return
            if n in votes:
                if basis in ("moved", "seconded") and "role" not in votes[n]:
                    votes[n]["role"] = basis
                return
            votes[n] = {"person": n, "vote": vote, "basis": basis}
            if basis in ("moved", "seconded"):
                votes[n]["role"] = basis

        for rx, v in ((AYES_RX, "yes"), (NAYS_RX, "no"), (ABSTAIN_RX, "abstain")):
            for mm in rx.finditer(flat):
                for n in _names(mm.group("names")):
                    put(n, v, "named")
        for mo in OPPOSED_RX.finditer(flat):
            for n in _names(mo.group("names")):
                put(n, "no", "named")
        for rc in ROLLCALL_RX.finditer(flat):
            n = _person(rc.group("name"))
            if n and not NOISE.search(n):
                put(n, _norm_vote(rc.group("vote")), "named")
        if mover:
            put(mover, "yes", "moved")
        if sec:
            put(sec, "yes", "seconded")
        unanimous = bool(UNANIMOUS_RX.search(flat)) or bool(tally and tally.group(2) == "0" and result == "passed")
        nos = sum(1 for v in votes.values() if v["vote"] == "no")
        if result != "failed" and present:
            if tally:
                a, b = int(tally.group(1)), int(tally.group(2))
                pool = voting if len(voting) - nos == a and nos == b else present if len(present) - nos == a and nos == b else []
                for p in pool:
                    put(p, "yes", "tally")
            elif unanimous:
                for p in voting:
                    put(p, "yes", "unanimous")
        ref = ORD_RX.search(flat)
        out.append({"index": i, "motion": what[:400], "mover": mover, "seconder": sec, "result": result,
                    "procedural": bool(PROCEDURAL_RX.search(what)),
                    "tally": f"{tally.group(1)}-{tally.group(2)}" if tally else "", "unanimous": unanimous,
                    "votes": list(votes.values()), "ordinance_ref": f"{ref.group(1).title()} {ref.group(2)}" if ref else "",
                    "text": flat[:1200], "present": present})
    return out


def _derive(src: dict, ctx: dict):
    title, url, text = src.get("title") or "", src.get("url") or "", src.get("text") or ""
    if src["meta"].get("doc_type") != "minutes" and not MINUTES_HINT.search(f"{title} {url}"):
        return
    if len(text) < 200:
        return
    date = src.get("published")
    if not date:
        from .elections import find_date

        # the heading's own date first ("Minutes Regular Session: Monday, May 11th, 2026"): find_date takes neither
        # ordinals nor "February 27,2025", and nine roll calls went out undated (Nov 13, 2026)
        date = meeting_date(text, title) or find_date(text, title)
    for mo in parse_motions(text):
        base_key = f"{src['source_id']}|minutes|{url}|{mo['index']}"
        topics = tag(mo["motion"], mo["text"])
        common = {"from": "minutes", "mover": mo["mover"], "seconder": mo["seconder"], "result": mo["result"],
                  "procedural": mo["procedural"],
                  "tally": mo["tally"], "unanimous": mo["unanimous"], "ordinance_ref": mo["ordinance_ref"],
                  "topics": topics, "page_title": title, "opened": True, "opened_url": url}
        yield Record(src["source_id"], "agenda_item", url, title=mo["motion"][:200] or "Motion", text=mo["text"],
                     published=date, channel="derived", key=base_key + "|item", jurisdiction=src.get("jurisdiction") or "",
                     meta={**common, "present": mo["present"]})
        if mo["votes"]:
            yield Record(src["source_id"], "vote", url, title=mo["motion"][:200] or "Motion", text=mo["text"][:600],
                         published=date, channel="derived", key=base_key + "|vote", jurisdiction=src.get("jurisdiction") or "",
                         meta={**common, "result": mo["result"] or ("passed" if mo["unanimous"] else ""),
                               "votes": mo["votes"], "MatterFile": mo["ordinance_ref"], "EventBodyName": ""})


# version 2 (Nov 13, 2026): the heading's date when the document has none; a motion's words never the clerk's closing lines
register(Deriver("minutes", "kind='document' AND source_id NOT LIKE 'cand:%'", _derive, version="2"))
