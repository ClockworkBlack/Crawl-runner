"""The one shape every source is read into, and the small rules shared by all of them.

A contest (one JSON object per line in data/history/norm/<source>/<item>.jsonl.gz):
    scope   "ga" (a Georgia election, any level) or "us" (a federal race in any state)
    y       year (int)            date   "YYYY-MM-DD" when the source gives the day, else "YYYY"
    stage   general | primary | primary runoff | general runoff | special | special runoff | special primary
    st      state postal code     office  President | U.S. Senate | U.S. House | Governor | State Senate | ...
    dist    district ("" when none; "AL" at large)
    lvl     federal | statewide | legislative | judicial | question | local      (Electo's levels)
    p       the party whose primary this is ("" otherwise)
    j       county, for a county or city contest ("" otherwise)
    t       the title shown
    tot     votes counted for the contest (None when the source gives none)
    o       [[name, party, votes, incumbent 0/1, outcome]]   outcome: won | unopposed | runoff | led | "" (Electo's codes)
    cty     {county: [votes in the order of o]}  (Georgia, when the source reports by county)
    sp      1 for a special election
    src     {"t": what the source is, "u": where it was read}
    basis   official | compiled | seated   (seated: the member list shows who took the seat; no vote count on file)
    ids     {name: {"bioguide": .., "fec": ..}} when the source carries identifiers
"""
from __future__ import annotations

import datetime as dt
import re

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands", "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
}
STATE_BY_NAME = {v.upper(): k for k, v in STATES.items()}

_SKIP = ("JR", "SR", "MR", "MRS", "MS", "DR", "HON")


def pkey(name: str) -> str:
    """Electo's person key (hist16/build_archive.py pkey, the page's arcPkey): the two must stay the same."""
    n = re.sub(r"\([^)]*\)", " ", name or "")
    n = re.sub(r"[“”\"]", " ", n)
    n = re.sub(r"[^A-Za-z' -]", " ", n).upper().replace("'", "").replace("-", " ")
    return " ".join(x for x in n.split() if x not in _SKIP and len(x) > 1)


def shard(key: str, n: int) -> int:
    """FNV-1a, 32 bit: the page's arcShard and Electo's ga/site_pack.py."""
    h = 0x811C9DC5
    for ch in key:
        h = ((h ^ (ord(ch) & 0xFFFF)) * 0x01000193) & 0xFFFFFFFF
    return h % n


_PARTY = (
    (r"^(D|DEM|DEMOCRAT|DEMOCRATIC|DFL|DEMOCRATIC[- ]FARMER[- ]LABOR|DEMOCRATIC[- ]NPL|DEMOCRATIC[- ]NONPARTISAN LEAGUE)$", "D"),
    (r"^(R|REP|GOP|REPUBLICAN|INDEPENDENT[- ]REPUBLICAN)$", "R"),
    (r"^(L|LIB|LBT|LIBERTARIAN)$", "L"),
    (r"^(G|GRN|GRE|GREEN|PACIFIC GREEN|GREEN[- ]RAINBOW|MOUNTAIN)$", "G"),
    (r"^(I|IND|INDEPENDENT|NPA|NO PARTY AFFILIATION|UNAFFILIATED|UNENROLLED|NO PARTY PREFERENCE|NONPARTISAN|NON[- ]PARTISAN|NP)$", "I"),
)


def party_code(p: str | None) -> str:
    """D, R, L, G, I, or O (any other named party); "" when the source names none."""
    s = re.sub(r"\s+", " ", str(p or "").strip().upper())
    m = re.match(r"^W\s*\(([A-Z/ ]+)\)", s)          # the FEC's "W(R)": a write-in candidate of that party
    if m:
        s = m.group(1)
    if "/" in s and not re.match(r"^(N/A|W/I)$", s):  # "DEM/WF", "R/CRV/IDP": several lines, the first is the candidate's own
        s = s.split("/")[0]
    s = s.strip(" ()[]*#")
    if not s or s in ("NAN", "NONE", "N/A", "W", "W/I", "WRITE-IN", "WRITEIN"):
        return ""
    for rx, code in _PARTY:
        if re.match(rx, s):
            return code
    return "O"


def nice_name(n: str) -> str:
    """A ballot name as a person would write it: "JOHN H BUCHANAN JR" -> "John H. Buchanan Jr."; mixed case is kept."""
    n = re.sub(r"\s+", " ", str(n or "").replace('\\"', '"').strip().strip(","))
    if not n:
        return n
    if "," in n and not re.search(r",\s*(JR|SR|II|III|IV)\.?$", n, re.I):          # "LAST, FIRST M." -> "FIRST M. LAST"
        last, _, first = n.partition(",")
        first = first.strip()
        suf = ""
        m = re.search(r",?\s+(JR|SR|II|III|IV)\.?$", first, re.I)
        if m:
            suf, first = " " + m.group(1) + ("." if m.group(1).upper() in ("JR", "SR") else ""), first[:m.start()]
        n = f"{first.strip()} {last.strip()}{suf}".strip()
    if n.upper() != n and n.lower() != n:
        return n
    out = []
    for w in n.split(" "):
        u = w.upper().strip(".,")
        if u in ("II", "III", "IV"):
            out.append(u)
        elif u in ("JR", "SR"):
            out.append(u.title() + ".")
        elif len(u) == 1 and u.isalpha():
            out.append(u + ".")
        else:
            x = "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))
            x = re.sub(r"^(Mc)([a-z])", lambda m: m.group(1) + m.group(2).upper(), x)
            x = re.sub(r"^(O')([a-z])", lambda m: m.group(1) + m.group(2).upper(), x)
            x = re.sub(r'^(")([a-z])', lambda m: m.group(1) + m.group(2).upper(), x)
            out.append(x)
    return " ".join(out)


def general_day(year: int) -> str:
    """The Tuesday after the first Monday of November (federal general elections, 2 U.S.C. 7)."""
    d = dt.date(year, 11, 1)
    while d.weekday() != 0:
        d += dt.timedelta(days=1)
    return (d + dt.timedelta(days=1)).isoformat()


def is_writein_bucket(name: str) -> bool:
    s = re.sub(r"[^A-Z]", "", str(name or "").upper())
    return s in ("WRITEIN", "WRITEINS", "SCATTERING", "SCATTER", "OTHER", "OTHERS", "BLANK", "VOID", "OVERVOTES", "UNDERVOTES",
                 "BLANKVOTES", "NONEOFTHESECANDIDATES", "NONEOFTHEABOVE", "TOTAL", "TOTALVOTES", "UNDERVOTE", "OVERVOTE",
                 "BLANKSCATTERING", "MISCELLANEOUS", "ALLOTHERS", "UNCOMMITTED", "NOPREFERENCE")


def tag_outcomes(o: list, tot, majority: bool, runoff_follows: bool = False, seats: int = 1) -> None:
    """Outcome codes in place. majority: the election needs more than half (Georgia; primaries in runoff states)."""
    real = [x for x in o if not is_writein_bucket(x[0])]
    if not real:
        return
    order = sorted(real, key=lambda x: -(x[2] or 0))
    if all(x[2] is None for x in real):
        return
    if len(real) == 1:
        real[0][4] = "unopposed"
        return
    if seats > 1:
        for x in order[:seats]:
            x[4] = "won"
        return
    top = order[0]
    t = tot or sum((x[2] or 0) for x in o)
    if runoff_follows:
        for x in order[:2]:
            x[4] = "runoff"
    elif majority and t and (top[2] or 0) * 2 <= t:
        top[4] = "led"
    else:
        top[4] = "won"


def contest_id(c: dict) -> str:
    return "|".join(str(x) for x in (c["scope"], c["y"], c.get("date") or "", c["stage"], c.get("st") or "", c["office"],
                                     c.get("dist") or "", c.get("p") or "", c.get("j") or ""))


def seat_key(c: dict) -> tuple:
    """The same seat in the same election, whatever source reports it (for choosing between sources)."""
    return (c["scope"], c["y"], c["stage"], c.get("st") or "", c["office"], str(c.get("dist") or ""), c.get("p") or "",
            c.get("j") or "", 1 if c.get("sp") else 0)
