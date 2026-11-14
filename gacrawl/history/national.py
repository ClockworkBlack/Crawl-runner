"""The national results tables on Wikipedia's page for each cycle ("2026 United States House of Representatives
elections", "2026 United States Senate elections"): every race in one place, each with the winner as called (a check
mark beside the name) and no vote counts.

A state's own page gives the count, but a state page's result boxes are often still empty a week or two after the
election (Illinois, Pennsylvania and Massachusetts on Nov. 14, 2026: 84 House races with no count), and a race decided
by ranked-choice rounds (Alaska) is shown in a table of its own. The national table is kept current as races are
called, so it gives the winner of every race on file: where a state page has the race, its count stands and the
national table only marks the winner the page has not marked yet (build.merge_national); where no state page has it,
the national table's winner stands, without a count.

Only the tables of the elections to the new Congress are read: on the House page the sections named for the states
(not "Special elections", which lists the year's specials to fill the old Congress's vacancies), and on the Senate page
the rows that link the year's regular and special elections.
"""
from __future__ import annotations

import re

from .model import STATE_BY_NAME, STATES, general_day, party_code

SRC_T = "Wikipedia, {y} national results table: the winner as called (unofficial; no vote counts)"
_USHR = re.compile(r"\{\{\s*ushr\s*\|\s*([A-Za-z]{2})\s*\|\s*([A-Za-z0-9]+)\s*(?:\|[^{}]*)?\}\}", re.I)
_AYE = re.compile(r"\{\{\s*(?:aye|yes|check(?:mark)?|tick)\s*(?:\|[^{}]*)?\}\}", re.I)
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _plain(s: str) -> str:
    s = re.sub(r"<ref[^<>]*/>", "", s, flags=re.I)
    s = re.sub(r"<ref(?:\s[^<>]*)?>.*?</ref>", "", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"\{\{\s*sortname\s*\|([^|}]*)\|([^|}]*)[^}]*\}\}", r"\1 \2", s, flags=re.I)
    for _ in range(3):
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"'{2,}", "", re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def _fold(n: str) -> str:
    return re.sub(r"[^a-z]", "", re.sub(r"\s+(?:jr|sr|ii|iii|iv)\.?$", "", str(n).lower()))


def candidate_lines(cell: str) -> list[list]:
    """The candidates of one table row: [name, party, None, incumbent (0), "won" or ""]; the winner is the line with a
    check mark. A line without a name in it (a party stripe alone) is not a candidate."""
    out, seen = [], set()
    for line in re.findall(r"^\s*\*(.*)$", cell, re.M):
        won = bool(_AYE.search(line))
        text = _PCT.sub("", _plain(line)).strip(" -\u2013\u2014:")
        party = ""
        m = re.search(r"\(([^()]*)\)\s*$", text)
        if m:
            party, text = m.group(1).strip(), text[:m.start()].strip()
        name = re.sub(r"\s+", " ", text).strip()
        if not name or len(name) < 3 or re.fullmatch(r"[\d.,% ]+", name):
            continue
        k = _fold(name)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append([name, party_code(party), None, 0, "won" if won else ""])
    if sum(1 for x in out if x[4] == "won") > 1:          # a table that marks two: not a call this reader can trust
        for x in out:
            x[4] = ""
    return out


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) of each level-2 section."""
    marks = [(m.start(), m.end(), m.group(1).strip()) for m in re.finditer(r"^==([^=].*?)==\s*$", text, re.M)]
    out = []
    for i, (a, b, h) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((h, text[b:end]))
    return out


def _rows(body: str) -> list[str]:
    return re.split(r"^\s*\|-[^\n]*$", body, flags=re.M)


def _incumbent(row: str) -> str:
    m = re.search(r"\{\{\s*sortname\s*\|([^|}]*)\|([^|}]*)[^}]*\}\}", row, re.I)
    return f"{m.group(1).strip()} {m.group(2).strip()}" if m else ""


def _contest(y: int, st: str, office: str, dist: str, sp: int, o: list, url: str, inc: str) -> dict:
    if inc:
        k = _fold(inc)
        exact = [x for x in o if _fold(x[0]) == k]
        if not exact:                                  # "Dan Sullivan" listed as "Dan S. Sullivan": first and last name
            first, last = _fold(inc.split()[0]), _fold(inc.split()[-1])
            exact = [x for x in o if _fold(x[0]).startswith(first) and _fold(x[0]).endswith(last)]
        if len(exact) == 1:
            exact[0][3] = 1
    o.sort(key=lambda x: (x[4] != "won",))
    where = STATES[st] + ((" At Large" if dist == "AL" else " District " + dist) if office == "U.S. House" else "")
    c = {"scope": "us", "y": y, "date": general_day(y), "stage": "special" if sp else "general", "st": st, "office": office,
         "dist": dist, "lvl": "federal", "p": "", "j": "", "t": f"{office} · {where}", "tot": None, "o": o,
         "src": {"t": SRC_T.format(y=y), "u": url}, "basis": "reported", "natl": 1}
    if sp:
        c["sp"] = 1
    return c


SINGLE = ("AK", "DE", "ND", "SD", "VT", "WY")


def house_contests(text: str, y: int, url: str):
    """Every district's row in the state sections of the national House page."""
    for head, body in _sections(text):
        st = STATE_BY_NAME.get(head.upper())
        if not st or st not in STATES:
            continue
        for row in _rows(body):
            first = next((ln for ln in row.split("\n") if ln.strip()), "")
            m = _USHR.search(first) if first.lstrip().startswith("!") else None
            if not m or m.group(1).upper() != st:
                continue
            d = m.group(2).upper()
            dist = "AL" if d in ("AL", "0", "AT-LARGE") or st in SINGLE else str(int(d)) if d.isdigit() else ""
            if not dist:
                continue
            o = candidate_lines(row)
            if not o:
                continue
            yield _contest(y, st, "U.S. House", dist, 0, o, url, _incumbent(row))


def senate_contests(text: str, y: int, url: str):
    """Every row of the national Senate page's race tables that links one of the year's regular or special elections
    in its first cell."""
    rx = re.compile(rf"\[\[\s*{y} United States Senate (special )?election in ([^|\]]+?)\s*(?:\|[^\]]*)?\]\]")
    body = next((b for h, b in _sections(text) if re.search(r"race summary|summary of results|elections leading", h, re.I)), "") or text
    seen = set()
    for row in _rows(body):
        lines = [ln for ln in row.strip().split("\n") if ln.strip()]
        if not lines or not lines[0].lstrip().startswith("!"):
            continue
        m = rx.search(lines[0])
        if not m:
            continue
        st = STATE_BY_NAME.get(m.group(2).strip().upper())
        if not st:
            continue
        sp = 1 if m.group(1) else 0
        if (st, sp) in seen:
            continue
        o = candidate_lines(row)
        if not o:
            continue
        seen.add((st, sp))
        yield _contest(y, st, "U.S. Senate", "", sp, o, url, _incumbent(row))


def national_contests(text: str, where: dict, url: str):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    y = int(where["y"])
    if where["office"] == "U.S. House":
        yield from house_contests(text, y, url)
    else:
        yield from senate_contests(text, y, url)
