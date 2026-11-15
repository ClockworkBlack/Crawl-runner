"""The bundle checked against its own rules before Electo gets it.

    python -m gacrawl.history check --bundle dist/history [--report check.json]

An ERROR is a number or a label that cannot be right as it stands (a winner with fewer votes and no note why, a
county table that does not add up to the statewide line, two "General Elections" in one November, votes above the
total). A bundle with errors is not delivered: Electo keeps the one it has. A WARNING is something a person should
look at but that can be true (a total larger than the listed candidates' votes: write-ins the file does not list).

The rules are about the data's shape, so they hold for every year and source; `ANCHORS` are certified results known
exactly, which must be on file with exactly those votes: if a reader starts adding party lines twice or mixing two
elections, an anchor moves and the bundle stops here.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .model import general_day, is_writein_bucket

PARTIES = ("D", "R", "L", "G", "I", "O", "")
OUTCOMES = ("won", "unopposed", "runoff", "led", "")
KINDS = ("general", "primary", "primary runoff", "general runoff", "special", "special runoff", "special primary",
         "special primary runoff")
TERRITORIES = ("AS", "DC", "GU", "MP", "PR", "VI")

# (file, election id part, contest title, [(surname, votes), ...]) as certified
ANCHORS = (
    ("us_2020.json", "gen", "President · Georgia", (("Biden", 2473633), ("Trump", 2461854))),
    ("us_2016.json", "gen", "President · Georgia", (("Trump", 2089104), ("Clinton", 1877963))),
    ("us_2000.json", "gen", "President · Florida", (("Bush", 2912790), ("Gore", 2912253))),
    ("ga_2014.json", "1104gen", "Governor", (("Deal", 1345237), ("Carter", 1144794))),
    ("ga_2010.json", "1102gen", "Governor", (("Deal", 1365832), ("Barnes", 1107011))),
    ("ga_2006.json", "1107gen", "Governor", (("Perdue", 1229724), ("Taylor", 811049))),
    ("ga_2002.json", "1105gen", "Governor", (("Perdue", 1041677), ("Barnes", 937062))),
    ("ga_2014.json", "1104gen", "U.S. Senate", (("Perdue", 1358088), ("Nunn", 1160811))),
    ("ga_2012.json", "1106gen", "President of the United States", (("Romney", 2078688), ("Obama", 1773827))),
    ("ga_2008.json", "1104gen", "President of the United States", (("McCain", 2048759), ("Obama", 1844123))),
)


# certified results a reader is known to have had wrong: reported as warnings until the files are read again, then moved up
ANCHORS_PENDING = (
    ("us_2004.json", "gen", "U.S. House · New York District 1", (("Bishop", 156354), ("Manger", 121855))),
)


def _real(c):
    return [x for x in c.get("o") or [] if not is_writein_bucket(x[0])]


def check_bundle(bundle: Path) -> dict:
    bundle = Path(bundle)
    E: dict = defaultdict(list)
    W: dict = defaultdict(list)
    n_contests = 0
    index = json.loads((bundle / "index.json").read_text(encoding="utf-8"))
    files = sorted(p for p in bundle.glob("*.json") if re.fullmatch(r"(ga|us)_\d{4}\.json", p.name))
    listed = {e["f"] for e in index.get("elections") or []}
    for f in listed - {p.name for p in files}:
        E["a year the index lists has no file"].append(f)
    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        scope, y = d.get("scope"), int(d.get("year"))
        seen_el = Counter()
        gen_days = []
        for e in d.get("elections") or []:
            where = f"{p.name} {e.get('eid')}"
            kind, date = e.get("kind"), str(e.get("date") or "")
            seen_el[e.get("eid")] += 1
            if kind not in KINDS:
                E["an election of an unknown kind"].append(f"{where}: {kind}")
            if not re.fullmatch(r"\d{4}(-\d\d-\d\d)?", date) or not date.startswith(str(y)):
                E["an election's date is not a date of its year"].append(f"{where}: {date}")
            if scope == "ga" and len(date) == 10:
                on_day = date == general_day(y)
                if kind == "general":
                    gen_days.append(date)
                    if not on_day:
                        E["a Georgia \"General Election\" that is not on the general election day"].append(f"{where}: {date}")
                if "runoff" in kind and on_day:
                    E["a runoff dated on the general election day"].append(where)
            ids = Counter()
            for c in e.get("c") or []:
                n_contests += 1
                t = f"{where} · {c.get('t')}" + (f" ({c['p']})" if c.get("p") else "") + (f", {c['j']}" if c.get("j") else "")
                o = c.get("o") or []
                ids[(c.get("st"), c.get("t"), c.get("p"), c.get("j"), c.get("sp", 0), c.get("d"))] += 1
                if not o:
                    E["a contest with no candidates"].append(t)
                    continue
                if scope == "ga" and c.get("lvl") != "question" and len(o) >= 2 \
                        and all(re.search(r"(?:^|[-\u2013]\s*)(?:yes|no|for|against)(?: approval)?$", str(x[0]), re.I) for x in o if not is_writein_bucket(x[0])):
                    W["a ballot question filed as a contest for an office"].append(t)
                if scope == "ga" and c.get("lvl") in ("local", "judicial") and not c.get("j") and len(o) > 15:
                    W["a county office with no county and many candidates (counties' contests filed together?)"].append(f"{t}: {len(o)} candidates")
                if scope == "ga" and c.get("lvl") == "local" and re.match(r"(?:U\.?S\.? (?:House|Senate)|President|Vice President|Governor)\b", str(c.get("t") or "")):
                    W["a federal or statewide office filed as a county contest"].append(t)
                votes = [x[2] for x in o]
                if any(v is not None and (not isinstance(v, int) or v < 0) for v in votes):
                    E["votes that are not a whole number of votes"].append(t)
                    continue
                tot = c.get("tot")
                s = sum(v or 0 for v in votes)
                if tot is not None and s > tot:
                    E["the candidates' votes are more than the contest's total"].append(f"{t}: {s:,} > {tot:,}")
                if tot and s < 0.9 * tot and c.get("lvl") != "question":
                    W["the listed candidates have under 90 percent of the total (candidates the file does not list)"].append(f"{t}: {s:,} of {tot:,}")
                if not tot and c.get("b") != "seated" and any(v for v in votes):
                    E["votes on file but no total"].append(t)
                if not tot and c.get("b") != "seated" and not any(x[4] == "unopposed" for x in o):
                    W["no votes on file"].append(t)
                for x in o:
                    if x[1] not in PARTIES:
                        E["an unknown party code"].append(f"{t}: {x[1]!r}")
                    if x[4] not in OUTCOMES:
                        E["an unknown outcome code"].append(f"{t}: {x[4]!r}")
                    n = str(x[0] or "")
                    if '""' in n or "�" in n or "#" in n and c.get("lvl") == "federal" or not n.strip():
                        E["a damaged name"].append(f"{t}: {n!r}")
                    elif re.search(r"Partisan\b|\(I$", n):
                        W["a ballot's nonpartisan or incumbent mark left on a name"].append(f"{t}: {n!r}")
                    elif c.get("lvl") != "question" and not is_writein_bucket(n):
                        if re.search(r"^No Candidates?\b|\d\s*%|^\d+$", n, re.I):
                            E["a row that is not a candidate"].append(f"{t}: {n!r}")
                        elif len(n) > 6 and n.upper() == n and re.search(r"[A-Z]{4}", n):
                            W["a name in capitals"].append(f"{t}: {n}")
                        elif re.search(r"\s(?:D|R)\.$|\((?:R|D|I)\)$", n) and len(n.split()) >= 3:
                            E["a party or incumbent mark left in a name"].append(f"{t}: {n}")
                real = _real(c)
                names = Counter(re.sub(r"[^a-z]", "", str(x[0]).lower()) for x in real)
                if c.get("lvl") != "question" and any(k and v > 1 for k, v in names.items()):
                    E["a candidate listed twice in one contest"].append(t)
                seats = int(c.get("vf") or 1)
                won = [x for x in o if x[4] in ("won", "unopposed")]
                if len(won) > seats and c.get("lvl") != "question":
                    E["more winners than seats"].append(f"{t}: {[x[0] for x in won]}")
                counted = sorted((x for x in real if x[2] is not None), key=lambda x: -x[2])
                if seats == 1 and len(won) == 1 and counted and won[0][2] is not None and counted[0][2] > won[0][2] and not c.get("nt"):
                    E["a winner with fewer votes than another candidate, and no note why"].append(f"{t}: {won[0][0]} {won[0][2]:,} < {counted[0][0]} {counted[0][2]:,}")
                if seats == 1 and len(won) == 1 and won[0][2] is None and any(x[2] for x in o):
                    E["a winner without votes beside candidates with votes"].append(t)
                if c.get("lvl") != "question" and counted and not any(x[4] for x in o) and not c.get("nt") and c.get("b") != "seated":
                    W["no outcome marked"].append(t)
                if scope == "ga" and c.get("lvl") != "question" and "runoff" not in str(kind) and seats == 1 and won and tot \
                        and c.get("off") != "President" and won[0][4] == "won" and (won[0][2] or 0) * 2 <= tot \
                        and not (1996 <= y <= 2004 and kind == "general" and (won[0][2] or 0) / tot > 0.45):
                    E["a Georgia winner without the majority the law requires"].append(f"{t}: {won[0][0]} {won[0][2]:,} of {tot:,}")
                cty = c.get("cty")
                if cty:
                    if any(len(v) != len(o) for v in cty.values()):
                        E["a county row with the wrong number of candidates"].append(t)
                    else:
                        for i, x in enumerate(o):
                            cs = sum((v[i] or 0) for v in cty.values())
                            if x[2] is not None and cs != x[2]:
                                E["the county rows do not add up to the statewide votes"].append(f"{t}: {x[0]} {cs:,} by county, {x[2]:,} statewide")
                                break
                if scope == "us" and str(kind) in ("general", "special") and c.get("off") != "President" and won \
                        and won[0][1] not in ("D", "R") and c.get("st") not in TERRITORIES:
                    W["a federal winner of neither major party (true for a few; a party label not yet known for others)"].append(
                        f"{t}: {won[0][0]} ({won[0][1] or 'none'})")
            for k, n in ids.items():
                if n > 1:
                    E["the same contest twice in one election"].append(f"{where}: {k[1]} x{n}")
        for eid, n in seen_el.items():
            if n > 1:
                E["two elections with one id"].append(f"{p.name} {eid}")
        if scope == "ga" and len(set(gen_days)) > 1:
            E["two general elections in one year"].append(f"{p.name}: {sorted(set(gen_days))}")
        if scope == "us" and y % 4 == 0:
            g = next((e for e in d.get("elections") or [] if e.get("kind") == "general"), None)
            pres = [c for c in (g or {}).get("c") or [] if c.get("off") == "President"]
            if pres and len({c.get("st") for c in pres}) != 51:
                E["a presidential election without all 50 states and the District of Columbia"].append(f"{p.name}: {len(pres)}")
            for c in pres:
                w = [x for x in c["o"] if x[4] == "won"]
                if len(w) != 1 or w[0][1] not in ("D", "R"):
                    W["a state's presidential winner is not a major party's candidate"].append(f"{p.name} {c.get('t')}")
    # the same election on file twice (two sources, two labels)
    ga = [e for e in index.get("elections") or [] if e.get("scope") == "ga"]
    by_day = defaultdict(list)
    for e in ga:
        by_day[e.get("date")].append(e)
    for day, lst in by_day.items():
        kinds = [e.get("kind") for e in lst]
        if "general" in kinds and "special" in kinds and len(str(day)) == 10 and day != general_day(int(day[:4])):
            E["one day filed as a general and as a special election"].append(str(day))
    # anchors
    for fn, eid_part, title, rows in ANCHORS_PENDING:
        p = bundle / fn
        if p.exists():
            cs = [c for e in json.loads(p.read_text(encoding="utf-8"))["elections"] if eid_part in e["eid"] for c in e["c"] if c.get("t") == title]
            for surname, v in rows:
                got = [x[2] for x in (cs[0]["o"] if cs else []) if surname.lower() in str(x[0]).lower()]
                if got != [v]:
                    W["a certified result not yet matched (its file is waiting to be read again)"].append(f"{fn} {title}: {surname} {got} on file, {v:,} certified")
    for fn, eid_part, title, rows in ANCHORS:
        p = bundle / fn
        if not p.exists():
            W["an anchor's year is not on file"].append(f"{fn} {title}")
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        cs = [c for e in d["elections"] if eid_part in e["eid"] for c in e["c"] if c.get("t") == title]
        if len(cs) != 1:
            E["a certified result that must be on file is missing or doubled"].append(f"{fn} {title}: {len(cs)}")
            continue
        for surname, v in rows:
            got = [x[2] for x in cs[0]["o"] if surname.lower() in str(x[0]).lower()]
            if got != [v]:
                E["a certified result does not match"].append(f"{fn} {title}: {surname} {got} on file, {v:,} certified")
    # party control of Congress (congress.py): written only when its known results match, so its absence is a warning
    cp = bundle / "congress.json"
    if not cp.exists():
        W["party control of Congress (congress.json) is not in the bundle: its sources are not read yet, or a known "
          "result did not match (the build log says which)"].append("congress.json")
    else:
        cg = json.loads(cp.read_text(encoding="utf-8"))
        for y, e in sorted((cg.get("years") or {}).items()):
            h = e.get("house") or {}
            if h and (h.get("D") or 0) + (h.get("R") or 0) + (h.get("O") or 0) > (h.get("seats") or 435):
                E["more House seats counted than the House has"].append(f"{y}: {h.get('D')}-{h.get('R')}-{h.get('O')}")
            a = (e.get("senate") or {}).get("after") or {}
            if a and (a.get("D") or 0) + (a.get("R") or 0) + (a.get("O") or 0) + (a.get("undecided") or 0) > 100:
                E["more Senate seats counted than the Senate has"].append(f"{y}: {a.get('D')}-{a.get('R')}-{a.get('O')}")
    # seats
    for y, s in sorted((index.get("audit") or {}).get("seats", {}).items()):
        if s.get("look"):
            W["a year whose count of U.S. House or Senate contests is off"].append(f"{y}: House {s.get('house')}, Senate {s.get('senate')}")
    return {"contests": n_contests,
            "errors": {k: {"n": len(v), "examples": v[:25]} for k, v in sorted(E.items(), key=lambda kv: -len(kv[1]))},
            "warnings": {k: {"n": len(v), "examples": v[:15]} for k, v in sorted(W.items(), key=lambda kv: -len(kv[1]))},
            "error_count": sum(len(v) for v in E.values()), "warning_count": sum(len(v) for v in W.values())}


def report_text(res: dict) -> str:
    out = [f"{res['contests']:,} contests checked: {res['error_count']:,} errors, {res['warning_count']:,} warnings"]
    for kind in ("errors", "warnings"):
        for k, v in res[kind].items():
            out.append(f"\n{kind[:-1].upper()}: {k} ({v['n']:,})")
            out += [f"   {x}" for x in v["examples"][:8]]
    return "\n".join(out)
