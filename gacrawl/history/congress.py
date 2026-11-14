"""Party control of the U.S. House and Senate after every general election, the seats each party won, and net gains in
each chamber and in both together: congress.json in the bundle (Electo's party-control answers, ga/history_ui.js).

  House     the seats each party won at each election: the House's own party divisions (sources.Parties, election-day
            results). An election the House does not list yet (the newest, until its Congress is seated) is counted
            from its races as reported: a race is counted once it is called, and the seats not yet called are listed.
  Senate    the members list (congress-legislators, sources.Legislators): the seats each side held on election day, and
            right after the election: that election's winners (runoffs and recounts included) at the party they took
            the seat under, and the senators not up that year at their party on election day (Richard Shelby, a
            Democrat on election day 1994, changed party the day after: the election's own result is 52-48). The
            newest election before its winners are seated: its races as reported, and the senators not up.
  Sides     a senator counts with the party they caucus with (Bernie Sanders, Angus King with the Democrats); the House's
            divisions count party labels, as the House does.
  Control   the side with more seats; a Senate split evenly goes to the Vice President's party. A change between
            elections (James Jeffords, June 6, 2001) is listed with its day.
  Net gain  seats after the election minus seats before: in the House, against the seats each party won at the
            election before (the House's own convention: 1994 Republicans +54, 2006 Democrats +31); in the Senate,
            against the seats each side held on election day (1994 Republicans +8, 2006 Democrats +6). "Both chambers"
            adds the two.

    congress.json   {"v", "built", "first", "how": {...}, "src": {...}, "years": {"1994": {"house": {...},
                     "senate": {...}, "flipped": [...], "gain": {...}}, ...}}
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path

from .model import general_day

FIRST = 1912                     # the first election counted (the 17th Amendment's first popular Senate elections: 1913-14)
EXACT_FROM = 1980                # from here the members list dates every term to the day (control on a given day can be read)
HOUSE_SEATS = 435
TERRITORIES = {"DC", "PR", "GU", "VI", "AS", "MP"}
# a House whose plurality did not organize it (the House's own footnotes): 1916, Republicans 215 and Democrats 214, the
# Democrats organized with the minor parties' votes; 1930, Republicans 218 at the election, 14 members-elect died before
# the Congress met and the special elections gave the Democrats the majority
HOUSE_ORGANIZED = {1916: ("D", "Republicans won more seats (215 to 214), but the Democrats organized the House with the minor parties' members."),
                   1930: ("D", "Republicans won 218 seats, but 14 members-elect died before the Congress met and the special elections gave the Democrats the majority.")}
REGULAR_CLASS = {0: 1, 1: 2, 2: 3}          # (year - 2024) / 2 % 3 -> the class up that year: 2024 class 1, 2026 class 2, 2028 class 3


def regular_class(y: int) -> int:
    return REGULAR_CLASS[((y - 2024) // 2) % 3]


def side(party: str, caucus: str = "") -> str:
    x = caucus or party or ""
    if x.startswith("Democrat") or x in ("Democratic-Farmer-Labor", "Popular Democrat"):
        return "D"
    if x.startswith("Republican") or x == "Conservative":     # James Buckley (New York's Conservative Party) sat with the Republicans
        return "R"
    return "O"


def party_on(t: dict, d: str) -> tuple[str, str]:
    """(party, caucus) of a term on day d: the party period covering d when the member changed party in the term."""
    pa = t.get("pa") or []
    for i, a in enumerate(pa):
        if a["s"] <= d and (d < a["e"] or (i == len(pa) - 1 and d <= a["e"])):
            return a.get("p") or t.get("p") or "", a.get("c") or ""
    if pa and d < pa[0]["s"]:
        return pa[0].get("p") or t.get("p") or "", pa[0].get("c") or ""
    return t.get("p") or "", t.get("c") or ""


def _serving(terms: list, d: str) -> list:
    return [t for t in terms if t["s"] <= d < t["e"]]


def _vp(vps: list, d: str) -> str:
    x = [v for v in vps if v["s"] <= d < v["e"]]
    return side(x[-1]["p"]) if x else ""


def _ind(party: str, caucus: str) -> str:
    """The side an independent (or a minor party's member) counts with, or "" for a Democrat or a Republican."""
    sd = side(party, caucus)
    return sd if sd in ("D", "R") and side(party) != sd else ""


def _count(terms: list, d: str, vps: list) -> dict:
    serving = _serving(terms, d)
    c = Counter(side(*party_on(t, d)) for t in serving)
    ind = Counter(_ind(*party_on(t, d)) for t in serving)
    out = {"D": c["D"], "R": c["R"], "O": c["O"]}
    if ind["D"] or ind["R"]:
        out["ind"] = {k: ind[k] for k in ("D", "R") if ind[k]}
    out["maj"] = "D" if out["D"] > out["R"] else "R" if out["R"] > out["D"] else _vp(vps, d)
    if out["D"] == out["R"]:
        out["tie"] = 1
    return out


def _add_days(d: str, n: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def _late(y: int) -> str:
    """The last day a term can start and still be this election's result: runoffs (Georgia, January 2021) and recounts
    (Minnesota, July 2009) included. Before the 20th Amendment (the Congress elected in 1932 was the last to meet in
    the December after its term began) the members list starts a term on the day the new Senate first met, which
    could be in December of the year after the election."""
    return f"{y + 1}-08-01" if y >= 1934 else f"{y + 1}-12-31"


def senate_election(terms: list, y: int, vps: list) -> dict:
    """The Senate on election day and right after the election of year y (see the module's note)."""
    eday = general_day(y)
    late = _late(y)
    before = _count(terms, _add_days(eday, -1), vps)
    seats: dict = {}
    eve = _add_days(eday, -1)
    for t in terms:
        k = (t["st"], t.get("cl") or 0)
        e = seats.setdefault(k, {"held": [], "won": []})
        if t["s"] <= eve < t["e"]:
            e["held"].append(t)
        elif eday < t["s"] < late and t.get("how") != "appointment":
            e["won"].append(t)
        elif t.get("how") == "appointment" and f"{y + 1}-01-01" <= t["s"] <= f"{y + 1}-01-10":
            # appointed as the new term began to the seat this election filled (Missouri 2000: Mel Carnahan won after
            # his death and Jean Carnahan was appointed to the seat): the election's result
            e.setdefault("start", []).append(t)
    after = Counter()
    ind = Counter()
    up = regular_class(y)
    for (st, cl), e in seats.items():
        if e["won"] or e.get("start"):
            w = min(e["won"] or e["start"], key=lambda t: t["s"])
            after[side(*party_on(w, w["s"]))] += 1
            ind[_ind(*party_on(w, w["s"]))] += 1
        elif e["held"]:
            h = max(e["held"], key=lambda t: t["s"])
            if cl == up and h["e"] <= f"{y + 1}-03-05":
                # the seat was up and nobody took it under this election (a result contested and left vacant: New
                # Hampshire 1974): not counted
                continue
            after[side(*party_on(h, eve))] += 1
            ind[_ind(*party_on(h, eve))] += 1
    a = {"D": after["D"], "R": after["R"], "O": after["O"]}
    if ind["D"] or ind["R"]:
        a["ind"] = {k: ind[k] for k in ("D", "R") if ind[k]}
    seated = f"{y + 1}-02-01"
    a["maj"] = "D" if a["D"] > a["R"] else "R" if a["R"] > a["D"] else _vp(vps, seated)
    if a["D"] == a["R"]:
        a["tie"] = 1
    return {"before": before, "after": a, "chg": {p: a[p] - before[p] for p in ("D", "R", "O")}}


def senate_between(terms: list, y: int, vps: list, start_maj: str) -> list:
    """Days within the Congress elected in year y, before the next election, when control of the Senate changed and
    held for 30 days (Jeffords: June 6, 2001)."""
    d0, stop = f"{y + 1}-02-01", f"{y + 2}-11-01"
    days = sorted({x for t in terms for x in [t["s"], t["e"]] + [z for a in t.get("pa") or [] for z in (a["s"], a["e"])] if d0 < x < stop})
    out, cur = [], start_maj
    for x in days:
        k = _count(terms, x, vps)
        if k["maj"] and k["maj"] != cur:
            later = _add_days(x, 30)
            if later < stop and _count(terms, later, vps)["maj"] != k["maj"]:
                continue
            out.append({"d": x, "maj": k["maj"], "D": k["D"], "R": k["R"], "O": k["O"], **({"tie": 1} if k.get("tie") else {})})
            cur = k["maj"]
    return out


def house_rows(rows: list) -> dict:
    """year -> the House's party divisions after that election."""
    out = {}
    for r in sorted(rows, key=lambda r: r["y"]):
        out[r["y"]] = r
    return out


def _house_maj(D: int, R: int, y: int) -> tuple[str, str]:
    if y in HOUSE_ORGANIZED:
        return HOUSE_ORGANIZED[y]
    return ("D" if D > R else "R" if R > D else ""), ""


def current_house(cs: list, y: int) -> dict | None:
    """The House of the newest election, from its races as reported: a seat counts once its race is called."""
    races = [c for c in cs if c.get("scope") == "us" and c.get("y") == y and c.get("office") == "U.S. House"
             and c.get("stage") == "general" and not c.get("sp") and c.get("st") not in TERRITORIES]
    if len(races) < 300:
        return None
    won = Counter()
    called = 0
    for c in races:
        w = next((x for x in c["o"] if x[4] in ("won", "unopposed")), None)
        if w:
            won[side(_party_name(w[1]))] += 1
            called += 1
    return {"D": won["D"], "R": won["R"], "O": won["O"], "called": called, "undecided": HOUSE_SEATS - called, "on_file": len(races)}


def _party_name(code: str) -> str:
    return {"D": "Democrat", "R": "Republican"}.get(code or "", code or "")


def current_senate(cs: list, terms: list, y: int, vps: list) -> dict | None:
    """The Senate after the newest election, before its winners are seated: the year's races as reported (a race not
    yet called is listed as undecided) and the senators not up, at their party on election day."""
    eday = _add_days(general_day(y), -1)        # serving through election day (an appointee's term ends on it)
    races = [c for c in cs if c.get("scope") == "us" and c.get("y") == y and c.get("office") == "U.S. Senate"
             and c.get("stage") in ("general", "special")]
    if len(races) < 25:
        return None
    serving = _serving([t for t in terms if t["ch"] == "S"], eday)
    by_state: dict = {}
    for t in serving:
        by_state.setdefault(t["st"], []).append(t)
    up = regular_class(y)
    before = _count([t for t in terms if t["ch"] == "S"], _add_days(eday, -1), vps)
    after = Counter()
    ind = Counter()
    undecided, decided = [], 0
    taken: set = set()
    for c in sorted(races, key=lambda c: 1 if c.get("sp") else 0):
        st = c["st"]
        held = by_state.get(st, [])
        if c.get("sp"):
            # the special election's seat: the state's appointed senator's, else the seat that is not this year's class
            cand = [t for t in held if t.get("how") == "appointment" and id(t) not in taken] or \
                   [t for t in held if (t.get("cl") or 0) != up and id(t) not in taken]
        else:
            cand = [t for t in held if (t.get("cl") or 0) == up and id(t) not in taken]
        seat = cand[0] if cand else None
        if seat is not None:
            taken.add(id(seat))
        w = next((x for x in c["o"] if x[4] in ("won", "unopposed")), None)
        if w:
            after[side(_party_name(w[1]))] += 1
            decided += 1
        else:
            undecided.append({"st": st, **({"sp": 1} if c.get("sp") else {}), "t": c.get("t", "")})
    for t in serving:
        if id(t) in taken:
            continue
        if (t.get("cl") or 0) == up:
            if not any(c["st"] == t["st"] and not c.get("sp") for c in races):
                undecided.append({"st": t["st"], "t": "no result on file"})       # the seat was up: its race is not on file
            continue
        after[side(*party_on(t, eday))] += 1
        ind[_ind(*party_on(t, eday))] += 1
    a = {"D": after["D"], "R": after["R"], "O": after["O"], "undecided": len(undecided), "races": len(races), "called": decided}
    if ind["D"] or ind["R"]:
        a["ind"] = {k: ind[k] for k in ("D", "R") if ind[k]}
    if undecided:
        a["undecided_races"] = undecided
    return {"before": before, "after": a}


# results known exactly, which the table must give: a reader or a rule that goes wrong moves one, and then congress.json
# is not written (the page says party control is not on file rather than give wrong numbers)
ANCHORS = {
    1994: {"house": {"D": 204, "R": 230}, "senate": {"D": 48, "R": 52}, "flipped": ["house", "senate"]},
    2006: {"house": {"D": 233, "R": 202}, "senate": {"D": 51, "R": 49}, "flipped": ["house", "senate"]},
    2000: {"senate": {"D": 50, "R": 50}},
    1980: {"house": {"D": 243, "R": 192}, "senate": {"D": 47, "R": 53}, "flipped": ["senate"]},
    2020: {"senate": {"D": 50, "R": 50}, "flipped": ["senate"]},
}


def anchor_faults(out: dict) -> list[str]:
    bad = []
    Y = out.get("years") or {}
    for y, want in ANCHORS.items():
        e = Y.get(str(y))
        if not e:
            continue
        h, s = e.get("house") or {}, (e.get("senate") or {}).get("after") or {}
        if sum(s.get(k) or 0 for k in ("D", "R", "O")) < 90:
            s = {}                     # a members list read in part (a test, a file that failed): nothing to hold it to
        if h and "house" in want and any(h.get(k) != v for k, v in want["house"].items()):
            bad.append(f"{y} House: {h.get('D')}-{h.get('R')}, want {want['house']}")
        if s and "senate" in want and any(s.get(k) != v for k, v in want["senate"].items()):
            bad.append(f"{y} Senate after the election: {s.get('D')}-{s.get('R')}, want {want['senate']}")
        if h and s and "flipped" in want and sorted(e.get("flipped") or []) != sorted(want["flipped"]):
            bad.append(f"{y} chambers that changed hands: {e.get('flipped')}, want {want['flipped']}")
    return bad


def build_congress(work: Path, cs: list, log=print) -> dict | None:
    work = Path(work)
    terms = []
    for kind in ("historical", "current"):
        p = work / "people" / f"terms-{kind}.json"
        if p.exists():
            terms += json.loads(p.read_text(encoding="utf-8"))
    vps = []
    p = work / "people" / "executive.json"
    if p.exists():
        vps = json.loads(p.read_text(encoding="utf-8")).get("vp") or []
    hp = work / "congress" / "house_parties.json"
    house = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else {}
    hrow = house_rows(house.get("rows") or [])
    sen_terms = [t for t in terms if t["ch"] == "S" and t["st"] not in TERRITORIES]
    if not sen_terms and not hrow:
        log("congress: no members list and no House party divisions read yet")
        return None
    today = dt.date.today().isoformat()
    newest = max([y for y in range(FIRST, 2100, 2) if general_day(y) <= today] or [FIRST])
    years: dict = {}
    pr = hrow.get(FIRST - 2)
    prev_h = {"D": pr["D"], "R": pr["R"], "O": pr.get("O", 0), "maj": _house_maj(pr["D"], pr["R"], FIRST - 2)[0]} if pr else None
    prev_s = ""
    for y in range(FIRST, newest + 1, 2):
        e: dict = {}
        r = hrow.get(y)
        if r:
            D, R, O = r["D"], r["R"], r.get("O", 0)
            maj, why = _house_maj(D, R, y)
            h = {"D": D, "R": R, "O": O, "seats": r["seats"], "maj": maj}
            if why:
                h["note"] = why
            if prev_h:
                h["chg"] = {"D": D - prev_h["D"], "R": R - prev_h["R"], "O": O - prev_h["O"]}
                h["maj_before"] = prev_h["maj"]
            e["house"] = h
            prev_h = h
        elif y == newest and prev_h:
            cur = current_house(cs, y)
            if cur:
                h = dict(cur, current=1, prev={"D": prev_h["D"], "R": prev_h["R"], "O": prev_h["O"]}, maj_before=prev_h["maj"])
                h["chg"] = {p: cur[p] - prev_h[p] for p in ("D", "R", "O")}
                h["maj"] = "D" if cur["D"] > HOUSE_SEATS // 2 else "R" if cur["R"] > HOUSE_SEATS // 2 else ""      # 218 called seats
                e["house"] = h
        if sen_terms:
            seated = any(general_day(y) < t["s"] < _late(y) for t in sen_terms)
            if seated and general_day(y) < today:
                s = senate_election(sen_terms, y, vps)
                s["after"]["n"] = s["after"]["D"] + s["after"]["R"] + s["after"]["O"]
                # control before the election: from 1980 the seats each side held on election day (Jeffords's switch in
                # 2001 is why the Democrats ran the Senate before 2002's election); before 1980 the members list gives
                # many terms the days of a whole Congress, and a count of a day can show a side "ahead" that never ran
                # the Senate (the Republicans organized it through 1953-54 although the Democrats briefly had a seat
                # more), so control is who ran the Senate the Congress began with
                before_maj = s["before"]["maj"] if y >= EXACT_FROM or not prev_s else prev_s
                mid = senate_between(sen_terms, y, vps, s["after"]["maj"]) if y >= EXACT_FROM else []
                e["senate"] = {"before": s["before"], "after": s["after"], "chg": s["chg"], "maj": s["after"]["maj"],
                               "maj_before": before_maj, **({"between": mid} if mid else {})}
                prev_s = (mid[-1]["maj"] if mid else s["after"]["maj"])
            elif y == newest:
                cur = current_senate(cs, sen_terms, y, vps)
                if cur:
                    a = cur["after"]
                    s = {"before": cur["before"], "after": a, "current": 1, "maj_before": cur["before"]["maj"],
                         "chg": {p: a[p] - cur["before"][p] for p in ("D", "R", "O")}}
                    # control once a side has 51 seats, or 50 with the Vice President of its party
                    vp = _vp(vps, f"{y + 1}-02-01")
                    has = lambda p: a[p] >= 51 or (a[p] >= 50 and vp == p)      # noqa: E731
                    s["maj"] = "D" if has("D") else "R" if has("R") else ""
                    e["senate"] = s
        if not e:
            continue
        flipped = []
        for ch in ("house", "senate"):
            x = e.get(ch)
            if x and x.get("maj") and x.get("maj_before") and x["maj"] != x["maj_before"]:
                flipped.append(ch)
        e["flipped"] = flipped
        # the party that gained: the one with the larger net gain over both chambers (each chamber's own change)
        g = {}
        for p in ("D", "R"):
            hc = (e.get("house") or {}).get("chg", {}).get(p)
            sc = (e.get("senate") or {}).get("chg", {}).get(p)
            if hc is not None or sc is not None:
                g[p] = {"house": hc, "senate": sc, "both": (hc or 0) + (sc or 0)}
        if g:
            top = max(g, key=lambda p: g[p]["both"])
            e["gain"] = dict(g[top], party=top)
            e["gains"] = g
        if (e.get("house") or {}).get("current") or (e.get("senate") or {}).get("current"):
            e["current"] = 1
        years[str(y)] = e
    out = {"v": 1, "first": FIRST, "years": years,
           "src": {"house": (house.get("src") or {}),
                   "members": {"t": "unitedstates/congress-legislators (public domain): members of Congress, their terms, party changes and caucus",
                               "u": "https://github.com/unitedstates/congress-legislators"}},
           "how": {"house": "Seats each party won at the election (the House's party divisions, from election-day results); the net gain is "
                            "against the seats each party won at the election before.",
                   "senate": "Seats each side held on election day and right after the election (that election's winners, runoffs and "
                             "recounts included, and the senators not up at their party on election day), counting independents with the "
                             "party they caucus with; the net gain is the difference.",
                   "control": "The side with more seats; a Senate split 50-50 goes to the Vice President's party. A change between "
                              "elections is listed with its day.",
                   "both": "Net gains in both chambers added together.",
                   "current": "The newest election before its Congress is seated: its races as reported (unofficial), a race counted "
                              "once it is called; the races not yet called are listed."}}
    bad = anchor_faults(out)
    log(json.dumps({"congress": {"years": len(years), "house_from": min((int(y) for y, e in years.items() if "house" in e), default=None),
                                 "senate_from": min((int(y) for y, e in years.items() if "senate" in e), default=None),
                                 "anchor_faults": bad}}))
    if bad:
        log("congress: not written, a known result does not match: " + "; ".join(bad))
        return None
    return out
