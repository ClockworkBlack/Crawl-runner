"""The rules every source's contests pass through before the bundle is built.

The readers (sources.py) turn a publisher's file into contests; this module is where what they wrote is made to say
one thing, whatever file it came from and whenever it was read. Three kinds of rule, each with the case that made it:

  one contest at a time (repair_contest)
    - a name is written as a person writes it, however the file quoted or cased it;
    - a party is only a party: the state's "(I)" beside a name marks the incumbent, not an Independent, and a name
      that ends "D." carries its party, not an initial;
    - a candidate is one row: three party lines of one candidate are added, the same line listed twice is not;
    - the statewide votes equal the county rows they came with (an import that dropped a spelling's votes is put
      right from the counties, which are the files' own numbers);
    - a contest nobody voted in is not a result.

  Georgia's elections (reconcile_ga)
    - an election has one date and one kind, whichever file reports it: a special election is not a "General
      Election" because a file's name says "special__general", a general election is not its own runoff, and the
      December runoff of a November election is dated December.

  the federal files (fix_federal)
    - one contest is one election: a spring special election listed with the November general, a party's primary
      listed with the other party's, are taken apart;
    - the winner shown is the winner of the election shown: a runoff's or a later special election's winner is not
      marked as winning a count someone else led; a note says what decided the seat.

Every change is counted (`stats`), so a run's log and the bundle's audit say what was put right and how often.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .model import general_day, is_writein_bucket, nice_name, pkey, tag_outcomes, tidy_name, unify_names

GA_COUNTIES = 159
RCV = {"ME": 2018, "AK": 2022}          # ranked-choice general elections for Congress from that year
LA_CLOSED = (2008, 2010)               # Louisiana held party primaries for Congress in these two cycles only
TERRITORIES = ("AS", "DC", "GU", "MP", "PR", "VI")
_PARTY_TAIL = re.compile(r"\s+(?:(D|R|L)\.|\((D|R|L|I)\)|(Dem|Rep|Lib|Ind)\.?)$")
_PARTY_HEAD = re.compile(r"^(?:Lib|Dem|Rep|Ind|Grn)\s+(?=[A-Z][a-z]+\s+\S)")
# a ballot question's choices, however a file writes them: "Yes", "Question #5 - No", "For Approval", "YES (Non-Partisan)"
_CHOICE = re.compile(r"^(?:.*[-\u2013]\s*)?(?:yes|no|for|against)(?:\s+approval)?(?:\s*\(non-?partisan\))?$", re.I)
# the 2006 and 2008 files' "(Non-Partisan)" column, whole or cut into the name: "Yes (Non-Partisan)", "Gary Andrews
# Partisan", "Jr. Partisan Charles B. Mikell"
_NP_MARK = re.compile(r"\s*\(Non-?Partisan\)\s*$", re.I)
_NP_HEAD = re.compile(r"^((?:Jr|Sr)\.?|II|III|IV)\s+(?:\(Non-?Partisan\)|Partisan)\s+(.+)$", re.I)
_NP_TAIL = re.compile(r"\s+Partisan$", re.I)
_INC_CUT = re.compile(r"\s*\(I\)?$")                 # the incumbent mark cut short: "Amanda H. Mercier (I"
# offices that are never a county's own: a county file's row for one of them is its slice of a district or state contest
_NOT_COUNTY = re.compile(r"^(?:U\.?S\.? (?:House|Senate|Representative|Senator)|President|Vice President|Governor|"
                         r"Lieutenant Governor|Secretary of State|Attorney General|State School Superintendent|"
                         r"Commissioner of (?:Agriculture|Insurance|Labor))\b", re.I)
_STATEWIDE_COURT = re.compile(r"appeals|supreme", re.I)


def src_of(c: dict) -> str:
    return str(c.get("item") or "").split(":", 1)[0]


def _real(c: dict) -> list:
    return [x for x in c.get("o") or [] if not is_writein_bucket(x[0])]


def _note(c: dict, text: str) -> None:
    if text and text not in (c.get("note") or ""):
        c["note"] = ((c.get("note") or "") + " " + text).strip()


def _retag(c: dict, majority: bool) -> None:
    for x in c["o"]:
        x[4] = ""
    if c.get("lvl") != "question":
        tag_outcomes(c["o"], c.get("tot"), majority=majority, seats=int(c.get("vf") or 1))


def _needs_majority(c: dict) -> bool:
    return (c.get("scope") == "ga" or c.get("st") == "GA") and c.get("office") != "President" and c.get("lvl") != "question"


def person_key(name: str) -> frozenset:
    """Who a row is, whatever order a file writes the name in ("Rudolph (Rudy), III # Yakym", "Rudolph (Rudy) Yakym
    III") and whatever party it writes before it ("Lib David C. Graham")."""
    n = _PARTY_HEAD.sub("", re.sub(r"\s*#\s*", " ", str(name or "")))
    return frozenset(pkey(n).split(" ")) - {""}


# ------------------------------------------------------------------------------------------------ one contest
def repair_contest(c: dict, stats: Counter | None = None) -> dict | None:
    """One contest put right in place (None: it is not a result and is left out). Safe to run twice."""
    S = stats if stats is not None else Counter()
    src = src_of(c)
    o = c.get("o") or []
    ga = c.get("scope") == "ga" or c.get("st") == "GA"
    # names first: a mark a file left on them hides what they are (a question's "Yes (Non-Partisan)")
    for x in o:
        if not isinstance(x[0], str):
            continue
        n = _NP_HEAD.sub(lambda m: m.group(2) + " " + m.group(1), x[0])
        n = _NP_MARK.sub("", n)
        n = _NP_TAIL.sub("", n).strip()
        if ga and _INC_CUT.search(n) and len(_INC_CUT.sub("", n).split()) >= 2:
            n = _INC_CUT.sub("", n).strip()
            x[3] = 1
            S["incumbent mark cut short taken off a name"] += 1
        if n and n != x[0]:
            x[0] = n
            S["ballot's nonpartisan mark taken off a name"] += 1
    # a ballot question filed as a contest for an office: about 1,000 county and city referendums (and the 1996-2006
    # amendments) came in as "local" contests whose candidates were Yes and No, one of them the "winner"
    if ga and c.get("lvl") != "question":
        ch = [x for x in o if isinstance(x[0], str) and not is_writein_bucket(x[0])]
        if len(ch) >= 2 and all(_CHOICE.match(x[0].strip()) for x in ch):
            c["lvl"] = "question"
            S["ballot questions filed as offices"] += 1
    # Georgia elects no Vice President of its own: a write-in tally under that title (2008, 13 votes) is not a contest
    if ga and re.match(r"^vice president$", str(c.get("office") or c.get("t") or ""), re.I):
        S["vice-presidential write-in rows left out"] += 1
        return None
    # a county file's row for a federal or statewide office is that county's slice of a contest read whole elsewhere
    # (2014: "U.S. House District 7" filed as a Forsyth County contest, with Rob Woodall listed twice)
    if ga and c.get("lvl") == "local" and _NOT_COUNTY.match(str(c.get("office") or c.get("t") or "")):
        S["a county's slice of a district or statewide contest left out"] += 1
        return None
    isq = c.get("lvl") == "question"

    # a ballot question whose heading the document broke across lines: only its last line was read as the title
    if isq and c.get("t") and str(c["t"])[:1].islower() and "cut off" not in str(c["t"]):
        c["t"] = "Ballot question (its title is cut off in the source document) \u2026" + str(c["t"])
        S["ballot question titles marked as cut off"] += 1
    # names and the party a name carries
    for x in o:
        if isq or not isinstance(x[0], str):
            continue
        n = tidy_name(x[0])
        if c.get("scope") == "ga" and len(n.split()) >= 3:
            m = _PARTY_TAIL.search(n)
            if m:
                code = (m.group(1) or m.group(2) or {"Dem": "D", "Rep": "R", "Lib": "L", "Ind": "I"}[m.group(3)])
                n = n[:m.start()].strip()
                if m.group(2) == "I":
                    x[3] = 1                            # "(I)" is the state's incumbent mark
                elif not x[1] and "primary" not in c.get("stage", ""):
                    x[1] = code
                S["party read from the end of a name"] += 1
                if n.upper() == n:
                    n = nice_name(n)
        if n != x[0]:
            S["names tidied"] += 1
            x[0] = n
    # the state's files mark the incumbent "(I)"; read as a party it made every sitting nonpartisan official an Independent
    if src in ("sos_zip", "manual") and c.get("scope") == "ga":
        for x in o:
            if x[1] == "I" and x[3]:
                x[1] = ""
                S["incumbent mark no longer read as Independent"] += 1

    # one row a candidate
    if c.get("scope") == "ga" and not isq:
        _one_spelling_a_candidate(c, S)
        o = c["o"]
    elif c.get("lvl") == "federal" and not isq:
        _one_row_a_candidate(c, S)
        o = c["o"]
    # one winner a seat: a file that marks several (a write-in column read as the winner's, a page's formatting)
    if c.get("lvl") == "federal" and c.get("basis") != "seated" and int(c.get("vf") or 1) == 1:
        won = [x for x in o if x[4] == "won"]
        if len(won) > 1:
            top = max(won, key=lambda x: x[2] or 0)
            for x in won:
                if x is not top:
                    x[4] = ""
            S["more than one winner marked: the one with the most votes kept"] += 1
    # counts "as reported" whose page total is less than its own rows
    if (src == "reported" or c.get("basis") == "reported") and c.get("tot") is not None:
        ssum = sum(x[2] or 0 for x in o)
        if ssum > c["tot"]:
            _note(c, f"The page gave a total of {c['tot']:,}, less than its candidates' votes; their sum is shown.")
            c["tot"] = ssum
            S["as-reported totals below their own rows: the sum shown"] += 1

    # the statewide line equals its county rows
    cty = c.get("cty")
    if cty and o and all(len(v) == len(o) for v in cty.values()):
        sums = [sum((v[i] or 0) for v in cty.values()) for i in range(len(o))]
        if any(x[2] is not None and s > x[2] for s, x in zip(sums, o)) and all(x[2] is None or s >= x[2] for s, x in zip(sums, o)):
            for s, x in zip(sums, o):
                x[2] = s
            order = sorted(range(len(o)), key=lambda i: -(o[i][2] or 0))
            c["o"] = o = [o[i] for i in order]
            c["cty"] = {cn: [v[i] for i in order] for cn, v in cty.items()}
            c["tot"] = sum(x[2] or 0 for x in o)
            _retag(c, majority=True)
            S["statewide votes restored from the county rows"] += 1
        if c.get("scope") == "ga" and not c.get("j") and c.get("lvl") in ("federal", "statewide") and src == "sos_zip" \
                and len(cty) < GA_COUNTIES and "counties" not in (c.get("note") or ""):
            _note(c, f"Counted from {len(cty)} of Georgia's {GA_COUNTIES} counties: the state's archive for this election lacks the others' files.")

    # the FEC's total is the sum of the candidates it lists
    if src == "fec" and o and any(x[2] is not None for x in o):
        c["tot"] = sum(x[2] or 0 for x in o) or None

    # nobody voted (a national results table's race has its winner and no count: national.py; not this)
    if c.get("basis") != "seated" and not c.get("natl") and o and not any((x[2] or 0) > 0 for x in o):
        real = _real(c)
        if len(real) == 1 and not isq and "runoff" in str(c.get("stage")) and src != "fec":
            S["contests with no votes left out"] += 1     # a runoff has two candidates: a lone name with no votes is a leftover of the first round
            return None
        if len(real) == 1 and not isq:
            if c.get("tot") is not None or len(o) > 1 or real[0][2] is not None or real[0][4] != "unopposed":
                S["unopposed, no count on file"] += 1
            for x in o:
                x[2] = None
            real[0][4] = "unopposed"
            c["o"] = [real[0]]
            c["tot"] = None
            c.pop("cty", None)
        elif not any(x[4] == "unopposed" for x in o):
            S["contests with no votes left out"] += 1
            return None
    return c


_SUFFIX = re.compile(r"\b(JR|SR|II|III|IV)\b\.?", re.I)


def _one_spelling_a_candidate(c: dict, S: Counter) -> None:
    """Georgia contests gathered from many counties' files: one candidate under several spellings ("Carol W. Hunstein",
    "Carol Hunstein", "Carol W.Hunstein") is one row, their votes and county columns added. Two rows are one candidate
    only when their names are the same words (initials aside) and neither carries a suffix the other lacks."""
    o = c["o"]
    groups: dict = {}
    # spellings of one name: the same words, or one's words all in the other's with no other candidate of that surname
    # ("L. Westmoreland" and "Lynn Westmoreland")
    alias = unify_names({x[0]: (x[2] or 0) for x in o if not is_writein_bucket(x[0])})
    for i, x in enumerate(o):
        if is_writein_bucket(x[0]):
            continue
        canon = alias.get(x[0], x[0])
        k = (person_key(canon), frozenset(m.upper() for m in _SUFFIX.findall(x[0] or "")), x[1] if "primary" not in c.get("stage", "") else "")
        if len(k[0]) >= 1:
            groups.setdefault(k, []).append(i)
    if not any(len(v) > 1 for v in groups.values()):
        return
    drop = set()
    cty = c.get("cty") if c.get("cty") and all(len(v) == len(o) for v in c["cty"].values()) else None
    for idx in groups.values():
        if len(idx) < 2:
            continue
        keep = max(idx, key=lambda i: (len(person_key(o[i][0])), o[i][2] or 0))     # filed under the fullest spelling
        for i in idx:
            if i == keep:
                continue
            o[keep][2] = (o[keep][2] or 0) + (o[i][2] or 0)
            o[keep][3] = o[keep][3] or o[i][3]
            if cty:
                for v in cty.values():
                    v[keep] = (v[keep] or 0) + (v[i] or 0)
            drop.add(i)
    order = sorted((i for i in range(len(o)) if i not in drop), key=lambda i: -(o[i][2] or 0))
    c["o"] = [o[i] for i in order]
    if cty:
        c["cty"] = {cn: [v[i] for i in order] for cn, v in cty.items()}
    else:
        c.pop("cty", None) if c.get("cty") else None
    _retag(c, majority=True)
    S["spellings of one candidate joined"] += 1


def _one_row_a_candidate(c: dict, S: Counter) -> None:
    o = c["o"]
    # a row that is the sum of a candidate's other rows (a "Total" line beside the party lines) is not a line
    per: dict = defaultdict(list)
    for x in o:
        if x[2] is not None and not is_writein_bucket(x[0]):
            per[person_key(x[0])].append(x)
    sums = []
    for k, lst in per.items():
        vs = [x[2] for x in lst]
        if len(lst) >= 3 and max(vs) == sum(vs) - max(vs):
            sums.append(next(x for x in lst if x[2] == max(vs)))
    if sums:
        for t in sums:
            lines = [x for x in per[person_key(t[0])] if x is not t]
            for x in lines:
                x[3] = x[3] or t[3]
                x[4] = x[4] or t[4]
        o = c["o"] = [x for x in o if not any(x is t for t in sums)]
        S["a candidate's total row beside the party lines removed"] += 1
    by: dict = {}
    out, merged = [], False
    fec = src_of(c) == "fec"
    for x in o:
        marked = "#" in (x[0] or "")
        name = re.sub(r"\s+", " ", re.sub(r"\s*#\s*", " ", x[0] or "")).strip()
        if not name or re.fullmatch(r"\d+", name):
            merged = True                               # "#4": a footnote's own row, not a candidate
            continue
        k = person_key(name)
        if not k or is_writein_bucket(name):
            out.append([name] + list(x[1:]))
            continue
        y = by.get(k)
        if y is None:
            y = [_PARTY_HEAD.sub("", name)] + list(x[1:])
            y.append([x[1]])                            # the lines seen, dropped below
            y.append(marked)
            by[k] = y
            out.append(y)
            continue
        merged = True
        if x[2] is not None:
            # the FEC lists a candidate's same line twice when two elections share a sheet (a full and an unexpired
            # term, an election and its runoff): the larger count is this election's; different lines are added
            y[2] = max(y[2] or 0, x[2]) if (fec and x[1] == y[1]) else (y[2] or 0) + x[2]
        y[3] = max(y[3] or 0, x[3] or 0)
        y[4] = y[4] or x[4]
        y[5].append(x[1])
        if not marked and (y[6] or len(name) > len(y[0])):
            y[0], y[6] = _PARTY_HEAD.sub("", name), False   # the fuller spelling, never one a footnote mark cut in two
    for y in out:
        if len(y) > 5:
            y.pop()
            lines = y.pop()
            y[1] = next((p for p in lines if p in ("D", "R")), y[1] or next((p for p in lines if p), ""))
    if merged:
        S["party lines of one candidate joined"] += 1
        had = {x[4] for x in out}
        c["o"] = sorted(out, key=lambda x: -(x[2] or 0))
        if not ({"runoff", "unopposed"} & had):
            if c.get("tot") is not None and fec:
                c["tot"] = sum(x[2] or 0 for x in c["o"]) or None
            _retag(c, majority=_needs_majority(c))      # the outcome was worked out on the rows before they were joined
    else:
        c["o"] = out


# ------------------------------------------------------------------------------------------------ Georgia
_REGULAR_PRIMARY_MONTHS = (2, 3, 5, 6, 7, 8, 9)      # presidential preference (February, March) and general primaries with their runoffs


def ga_stage(c: dict, group_size: int = 0) -> str:
    """The kind of election a Georgia contest belongs to, from its date (the publishers' labels disagree):
      general         only on the November general election day (even years; odd years for cities and counties);
      special         any other election that is not a primary or a runoff, whatever a file's name calls it;
      ... runoff      never on the general election day itself: a whole election filed so is the general;
      special primary a primary outside the months general and presidential primaries are held."""
    stage, date, y = c.get("stage") or "general", str(c.get("date") or ""), int(c["y"])
    on_general_day = len(date) == 10 and date == general_day(y)
    regular_general = on_general_day and (y % 2 == 0 or c.get("lvl") in ("local", "question"))
    ro, pr, sp = "runoff" in stage, "primary" in stage, "special" in stage or bool(c.get("sp"))
    if ro and on_general_day and group_size > 20:
        ro = False
    if pr:
        month = int(date[5:7]) if len(date) == 10 else 0
        if month and month not in _REGULAR_PRIMARY_MONTHS:
            sp = True
        return ("special primary" if sp else "primary") + (" runoff" if ro else "")
    if ro:
        return "special runoff" if sp else "general runoff"
    return "general" if regular_general and not sp else "special"


def _sig(c: dict):
    votes = tuple(sorted(((x[2] or 0) for x in _real(c)), reverse=True))
    if not votes or not votes[0]:
        return None
    if c.get("lvl") not in ("federal", "statewide", "legislative") and sum(votes) < 200:
        return None                                     # small local counts repeat by chance: not evidence of one contest
    return (c["y"], c.get("lvl"), c.get("office") if c.get("lvl") in ("federal", "statewide", "legislative") else "",
            str(c.get("dist") or ""), votes[:3])


def reconcile_ga(cs: list[dict], stats: Counter | None = None) -> None:
    """Georgia contests of every source given one date and one kind of election (in place)."""
    S = stats if stats is not None else Counter()
    ga = [c for c in cs if c.get("scope") == "ga"]
    groups: dict = defaultdict(list)
    for c in ga:
        groups[(c.get("item"), c.get("date"), c.get("stage"))].append(c)
    # 1. the same contest in two sources (identical votes) under two dates: the election has one date.
    #    A runoff dated on the general election day carries the general's date from its archive's name; otherwise the
    #    state's own files are the ones believed.
    by_sig: dict = defaultdict(list)
    for c in ga:
        s = _sig(c)
        if s:
            by_sig[s].append(c)

    def suspicious(c):
        d = str(c.get("date") or "")
        return len(d) != 10 or ("runoff" in c["stage"] and d == general_day(c["y"]))

    def trust(c):
        return (0 if suspicious(c) else 1, {"manual": 4, "sos_zip": 3, "sos_pdf": 3, "openelections": 2}.get(src_of(c), 1))

    moved: dict = defaultdict(Counter)
    for s, lst in by_sig.items():
        if len({src_of(c) for c in lst}) < 2:
            continue
        dates = {c.get("date") for c in lst}
        if len(dates) < 2:
            continue
        ds = sorted(d for d in dates if len(str(d)) == 10)
        if not ds or (_days(ds[0], ds[-1]) or 99) > 45:
            continue                                    # the same numbers months apart are not one election
        best = max(lst, key=trust)
        for c in lst:
            if c.get("date") != best.get("date"):
                moved[(c.get("item"), c.get("date"), c.get("stage"))][best["date"]] += 1
    for key, to in moved.items():
        date = to.most_common(1)[0][0]
        for c in groups[key]:
            c["date"] = date
            S["election dates corrected from a second source"] += 1
    groups = defaultdict(list)
    for c in ga:
        groups[(c.get("item"), c.get("date"), c.get("stage"))].append(c)
    # 2. the kind of election, from the date
    for key, lst in groups.items():
        for c in lst:
            st = ga_stage(c, len(lst))
            if "runoff" in st and str(c.get("date")) == general_day(c["y"]) :
                c["date"] = str(c["y"])                 # its archive is named for the general; the runoff's own day is not on file
                S["runoffs dated on the general election day: day removed"] += 1
            if st != c["stage"]:
                S[f"kind of election: {c['stage']} -> {st}"] += 1
                c["stage"] = st
            if "special" in st:
                c["sp"] = 1
            elif c.get("sp"):
                c.pop("sp")
    # 3. two sources, identical votes, same day: one kind of election (a special if either says so, a runoff if either does)
    for s, lst in by_sig.items():
        for d in {c.get("date") for c in lst}:
            same = [c for c in lst if c.get("date") == d]
            kinds = {c["stage"] for c in same}
            if len(kinds) < 2:
                continue
            pr, ro, sp = any("primary" in k for k in kinds), any("runoff" in k for k in kinds), any("special" in k for k in kinds)
            st = (("special primary" if sp else "primary") + (" runoff" if ro else "")) if pr else \
                ("special runoff" if sp else "general runoff") if ro else "special" if sp else "general"
            for c in same:
                if c["stage"] != st:
                    S[f"kind of election agreed between sources: {c['stage']} -> {st}"] += 1
                    c["stage"] = st
                    if sp:
                        c["sp"] = 1
    # 4. the second special election for a seat within ten weeks, between candidates of the first: its runoff
    seats: dict = defaultdict(list)
    for c in ga:
        if c["stage"] in ("special", "special runoff") and c.get("lvl") in ("federal", "statewide", "legislative") and len(str(c.get("date"))) == 10:
            seats[(c.get("office"), str(c.get("dist") or ""))].append(c)
    for lst in seats.values():
        lst.sort(key=lambda c: c["date"])
        for i, c in enumerate(lst):
            if c["stage"] != "special":
                continue
            names = {person_key(x[0]) for x in _real(c)}
            for e in lst[:i]:
                gap = _days(e["date"], c["date"]) or 0
                if e["stage"] == "special" and 7 <= gap <= 70 and len(names) == 2 and len(_real(e)) > 2 \
                        and names <= {person_key(x[0]) for x in _real(e)}:
                    c["stage"] = "special runoff"
                    S["kind of election: a special election's runoff recognized"] += 1
                    break
    # a majority is needed in every Georgia election: outcomes follow the kind of election now on file
    for c in ga:
        if c.get("lvl") != "question" and "runoff" in c["stage"]:
            real = sorted((x for x in _real(c) if x[2] is not None), key=lambda x: -x[2])
            if len(real) >= 2 and real[0][4] in ("led", "") and int(c.get("vf") or 1) == 1 and real[0][2] > real[1][2]:
                for x in c["o"]:
                    x[4] = ""
                real[0][4] = "won"                      # a runoff is decided by the most votes


def _days(a: str, b: str):
    import datetime as dt
    try:
        return abs((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ federal files
def fix_federal(cs: list[dict], stats: Counter | None = None) -> list[dict]:
    """The federal files' contests taken apart where a file ran two elections together, and their winners made the
    winners of the election shown. Returns the list (contests may be added)."""
    S = stats if stats is not None else Counter()
    out: list[dict] = []
    gens: dict = defaultdict(list)
    for c in cs:
        if c.get("scope") == "us" and c.get("stage") in ("general", "special") and c.get("basis") != "seated":
            gens[(c["y"], c.get("st"), c.get("office"), str(c.get("dist") or ""))].append(c)
    for c in cs:
        if c.get("scope") != "us" or c.get("lvl") != "federal" or c.get("basis") == "seated":
            out.append(c)
            continue
        st, y, stage = c.get("st"), c["y"], c.get("stage") or ""
        # Louisiana's party primaries of 2008 and 2010, filed as one all-party election
        if st == "LA" and y in LA_CLOSED and "primary" in stage and not c.get("p") and c.get("office") != "President":
            parts = defaultdict(list)
            for x in c["o"]:
                parts[x[1]].append(x)
            made = 0
            for p, rows in sorted(parts.items()):
                if p not in ("D", "R", "L", "G"):
                    continue
                n = dict(c, p=p, o=[list(x) for x in rows])
                n["tot"] = sum(x[2] or 0 for x in rows) or None
                if not any(x[4] == "unopposed" for x in rows) or len(rows) > 1:
                    _retag(n, majority=True)
                out.append(n)
                made += 1
            if made:
                S["Louisiana party primaries separated"] += 1
                continue
        # a candidate "unopposed" with no count beside candidates with counts: two elections in one list
        if "primary" in stage or stage in ("general", "special"):
            un = [x for x in c["o"] if x[4] == "unopposed" and x[2] is None]
            voted = [x for x in c["o"] if x[2] is not None]
            if un and voted and (stage not in ("general", "special") or False) \
                    and len([x for x in voted if x[1]]) >= 2 and sum(x[2] for x in voted) >= 1000:
                k = (y, st, c.get("office"), str(c.get("dist") or ""))
                sp_gen = [g for g in gens.get(k, []) if g.get("sp")]
                reg_gen = [g for g in gens.get(k, []) if not g.get("sp")]
                names = lambda lst: {person_key(x[0]) for g in lst for x in g["o"]}       # noqa: E731
                un_keys, voted_keys = {person_key(x[0]) for x in un}, {person_key(x[0]) for x in voted}
                un_sp = bool(un_keys & names(sp_gen)) and not (un_keys & names(reg_gen))
                voted_sp = bool(voted_keys & names(sp_gen)) and not (voted_keys & names(reg_gen))
                a = dict(c, o=[list(x) for x in voted])
                a["tot"] = sum(x[2] or 0 for x in voted) or None
                _retag(a, majority=_needs_majority(a))
                b = dict(c, o=[list(x) for x in un], tot=None)
                b.pop("cty", None)
                if un_sp != voted_sp:
                    for part, is_sp in ((a, voted_sp), (b, un_sp)):
                        if is_sp:
                            part["sp"] = 1
                            if "special" not in part["stage"]:
                                part["stage"] = "special primary" if "primary" in part["stage"] else "special"
                            part["date"] = str(y)
                        else:
                            part.pop("sp", None)
                    out += [a, b]
                    S["a special election separated from the regular one"] += 1
                else:
                    _note(a, f"{' and '.join(x[0] for x in un)} is listed unopposed in the same file for another election to this seat that year.")
                    out.append(a)
                    S["an unopposed row of another election set aside"] += 1
                continue
        if "primary" in stage:
            un = [x for x in c["o"] if x[4] == "unopposed" and x[2] is None]
            if len(un) > 1 and len(un) == len(c["o"]):
                k = (y, st, c.get("office"), str(c.get("dist") or ""))
                sp_names = {person_key(x[0]) for g in gens.get(k, []) if g.get("sp") for x in g["o"]}
                a_rows = [x for x in un if person_key(x[0]) not in sp_names]
                b_rows = [x for x in un if person_key(x[0]) in sp_names]
                if a_rows and b_rows:
                    out.append(dict(c, o=[list(x) for x in a_rows]))
                    out.append(dict(c, o=[list(x) for x in b_rows], sp=1, stage="special primary", date=str(y)))
                    S["a special election separated from the regular one"] += 1
                    continue
        out.append(c)

    # a general election's list that also holds that year's special election to the seat: the special's rows are not the general's
    by_seat: dict = defaultdict(list)
    for c in out:
        if c.get("scope") == "us" and c.get("basis") != "seated" and c.get("stage") in ("general", "special"):
            by_seat[(src_of(c), c["y"], c.get("st"), c.get("office"), str(c.get("dist") or ""))].append(c)
    for lst in by_seat.values():
        reg = [c for c in lst if not c.get("sp")]
        spc = [c for c in lst if c.get("sp")]
        for g in reg:
            for s in spc:
                dup = [x for x in g["o"] if any(person_key(x[0]) == person_key(z[0]) and x[2] == z[2] and x[2] for z in s["o"])]
                rest = [x for x in g["o"] if x not in dup]
                if dup and {person_key(x[0]) for x in dup} <= {person_key(x[0]) for x in rest}:
                    g["o"] = rest
                    g["tot"] = sum(x[2] or 0 for x in rest) or None
                    S["a special election's rows removed from the general"] += 1

    # the winner shown is the winner of the election shown
    for c in out:
        if c.get("scope") != "us" or c.get("basis") == "seated" or c.get("stage") not in ("general", "special") or int(c.get("vf") or 1) > 1:
            continue
        real = sorted((x for x in _real(c) if x[2] is not None), key=lambda x: -x[2])
        won = [x for x in c["o"] if x[4] == "won"]
        if len(real) < 2 or len(won) != 1 or won[0] is real[0] or (won[0][2] or 0) >= real[0][2]:
            continue
        w, lead = won[0], real[0]
        st, y = c.get("st"), c["y"]
        if st in RCV and y >= RCV[st]:
            _note(c, f"Decided by ranked-choice tabulation: {w[0]} won after later rounds. First-choice votes are shown.")
            S["ranked-choice winners noted"] += 1
        elif st == "LA":
            for x in c["o"]:
                x[4] = ""
            for x in real[:2]:
                x[4] = "runoff"
            _note(c, f"No candidate had a majority in this election; {w[0]} won the runoff that followed, which is not on file.")
            S["Louisiana runoffs: the November leader no longer shown as losing to the winner"] += 1
        else:
            for x in c["o"]:
                x[4] = ""
            lead[4] = "led"
            if w[1] and w[1] == lead[1]:
                # two candidates of one party, the marked one far behind: the mark is for a later election to the seat,
                # and so are that row's votes (North Carolina's 9th District, 2018: Dan Bishop's 2019 special election)
                c["o"] = [x for x in c["o"] if x is not w]
                c["tot"] = sum(x[2] or 0 for x in c["o"]) or None
            _note(c, f"{lead[0]} had the most votes in this count, but {w[0]} was the candidate seated: the seat was decided by a later election or ruling. See the source.")
            S["a later election's winner no longer marked as winning this count"] += 1
    return out


def county_contests(c: dict, stats: Counter | None = None) -> list[dict]:
    """A county office the state's 2012-2015 archives filed with no county: the contest as each county held it.

    The archives' county files title the same office alike ("CLERK, SUPERIOR COURT"), and the reader put rows of one
    title together as if they were one contest across counties, as a judicial circuit's is: 78 candidates for clerk in
    one "contest" with one leader (2012). The county table the reader kept says where each candidate ran. A contest
    whose county table has one county is that county's; one whose candidates each have votes in one county only is
    one contest per county; a name with votes in two of the counties is two people of one name the reader put in one
    row (2012: "Becky Robinson", clerk in Fulton and in Haralson), and has a row in each. A contest its candidates ran
    in across counties (a circuit's judge, a district attorney) is left as it is, and so is a statewide court's."""
    S = stats if stats is not None else Counter()
    cty = c.get("cty")
    if not (c.get("scope") == "ga" and not c.get("j") and cty and c.get("lvl") in ("local", "judicial")
            and not _STATEWIDE_COURT.search(str(c.get("t") or c.get("office") or ""))):
        return [c]
    o = c["o"]
    if not all(len(v) == len(o) for v in cty.values()):
        return [c]
    if len(cty) == 1:
        c["j"] = next(iter(cty))
        c.pop("cty", None)
        S["county offices given their county"] += 1
        return [c]
    real = [i for i, x in enumerate(o) if isinstance(x[0], str) and not is_writein_bucket(x[0])]
    where = {i: [k for k, v in cty.items() if (v[i] or 0) > 0] for i in real}
    single = [w[0] for w in where.values() if len(w) == 1]
    if not real or len(single) < 0.8 * len(real) or len(set(single)) < 2:
        return [c]
    out = []
    for county in sorted({k for w in where.values() for k in w}):
        v = cty[county]
        rows = [[x[0], x[1], v[i], x[3], ""] for i, x in enumerate(o) if (v[i] or 0) > 0]
        n = dict(c, j=county, o=rows, tot=sum(r[2] or 0 for r in rows))
        n.pop("cty", None)
        _retag(n, majority=_needs_majority(n))
        out.append(n)
    S["county offices filed together, taken apart by county"] += 1
    S["county contests from them"] += len(out)
    return out


def repair_all(contests, stats: Counter | None = None) -> list[dict]:
    """Everything above, in order, over every contest read."""
    S = stats if stats is not None else Counter()
    cs = []
    for c in contests:
        c = repair_contest(c, S)
        if c is not None:
            cs.extend(county_contests(c, S))
    reconcile_ga(cs, S)
    cs = fix_federal(cs, S)
    # taking contests apart can leave two with one identity: the fuller stands
    return cs
