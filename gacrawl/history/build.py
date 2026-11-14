"""The bundle Electo reads (ga/pipeline_archive/hist/): Electo's own archive shapes, so its page code is shared.

    index.json           elections (id, date, title, kind, scope, counts), coverage, sources, what is still missing
    ga_<year>.json       Georgia contests of a year      {"year", "elections": [{eid, date, title, kind, c: [contest]}]}
    us_<year>.json       federal contests in every state (each contest carries "st")
    people/<n>.json      every candidate's races, by Electo's person key, in N shards (FNV-1a, as the page hashes)
    prof/<n>.json        members of Congress since 1969: terms, parties, born (by bioguide id)
    summary.json         the totals the page's charts and the AI's analysis start from (seats and votes by party and
                         year, statewide winners and margins, closest and uncontested races)
    manifest.json        every file's size and hash (what `verify` checks in Electo before the hand-off copy is deleted)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from .model import STATES, is_writein_bucket, pkey, same_person, seat_key, shard, tag_outcomes
from .run import iter_contests, load_state, paths

PEOPLE_SHARDS, PROF_SHARDS = 64, 16
PRIORITY = {"manual": 6, "sos_zip": 5, "openelections": 4, "sos_pdf": 3, "medsl": 5, "fec": 4, "legislators": 1}
STAGE_CODE = {"general": "gen", "primary": "pri", "primary runoff": "prirun", "general runoff": "genrun", "special": "spec",
              "special runoff": "specrun", "special primary": "specpri", "special primary runoff": "specprirun"}
STAGE_TITLE = {"general": "General Election", "primary": "Primary Election", "primary runoff": "Primary Runoff",
               "general runoff": "General Election Runoff", "special": "Special Election", "special runoff": "Special Election Runoff",
               "special primary": "Special Primary", "special primary runoff": "Special Primary Runoff"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
STAGE_ORDER = {"special primary": 0, "primary": 1, "special primary runoff": 2, "primary runoff": 2, "special": 3, "general": 4,
               "special runoff": 5, "general runoff": 5}


def _dump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def _long_date(d: str) -> str:
    return f"{MONTHS[int(d[5:7]) - 1]} {int(d[8:10])}, {d[:4]}" if len(d) == 10 else d


def _lead(c: dict):
    real = sorted((x for x in c["o"] if not is_writein_bucket(x[0]) and x[2] is not None), key=lambda x: -x[2])
    won = next((x for x in c["o"] if x[4] in ("won", "unopposed")), None) or (real[0] if real else None)
    return won, real


def choose(contests, audit: dict | None = None) -> list[dict]:
    """One contest per seat and election: the source ranked highest; a "seated" record only where nothing else is.
    Where two sources report the same seat they are compared (the winner's name, the winner's own votes within
    1 percent, the total within 3 percent: sources count write-ins differently): the kept contest is marked chk "agree" or "differ", and every difference is listed
    in audit (the bundle's audit.json), so a wrong number shows instead of hiding."""
    seats: dict = defaultdict(list)
    for c in contests:
        _merge_marked_lines(c)
        seats[seat_key(c)].append(c)
    out = []
    A = audit if audit is not None else {}
    A.setdefault("checked", 0); A.setdefault("agree", 0); A.setdefault("differ", []); A.setdefault("seated_differs", [])
    # the state's own certified count of each Georgia federal general (the Secretary of State's files): a witness for
    # the federal files' counts of the same contests
    state_gen = {(c["y"], c["office"], str(c.get("dist") or ""), 1 if c.get("sp") else 0): c
                 for lst in seats.values() for c in lst
                 if c.get("scope") == "ga" and c.get("stage") == "general" and c.get("tot") and c.get("office") in ("U.S. Senate", "U.S. House")}
    for k, lst in list(seats.items()):
        witness = state_gen.get((k[1], k[4], k[5], k[8])) if k[0] == "us" and k[3] == "GA" and k[2] == "general" else None
        moved = _runoff_filed_as_general(lst, witness)
        for c in moved:
            lst.remove(c)
            r = dict(c, stage="general runoff", date=str(c["y"]), chk="filed as the general")
            seats[seat_key(r)].append(r)
            A.setdefault("runoffs_filed_as_general", []).append({"y": c["y"], "race": c["t"], "src": c.get("item", "").split(":", 1)[0],
                                                                 "total": c.get("tot")})
        if moved and witness and not any(c.get("tot") for c in lst):
            # nothing else counted the general (MEDSL is the only federal file before 2000): the state's own count
            # stands in for it, under the federal set's name for the race
            lst.append(dict(witness, scope="us", t=moved[0]["t"], o=[list(x) for x in witness["o"]], chk="the state's count"))
    for k, lst in seats.items():
        if not lst:
            continue
        lst.sort(key=lambda c: (-PRIORITY.get(c.get("item", "").split(":", 1)[0], 0), -(c.get("tot") or 0)))
        best = lst[0]
        # Georgia, two files of the same certified count: the fuller one (an archive missing a county's file counts less)
        full = [c for c in lst if c.get("item", "").split(":", 1)[0] in ("sos_zip", "openelections", "manual") and c.get("tot")]
        if best in full and len(full) > 1:
            best = max(full, key=lambda c: (c["tot"], PRIORITY.get(c.get("item", "").split(":", 1)[0], 0)))
            lst = [best] + [c for c in lst if c is not best]
        bw, _ = _lead(best)
        for other in lst[1:]:
            if other.get("item", "").split(":", 1)[0] == best.get("item", "").split(":", 1)[0]:
                continue
            ow, _ = _lead(other)
            if not bw or not ow:
                continue
            same_w = same_person(bw[0], ow[0])
            rec = {"y": best["y"], "race": best["t"], "stage": best["stage"], "p": best.get("p") or "",
                   "kept": {"src": best.get("item", "").split(":", 1)[0], "winner": bw[0], "votes": bw[2], "total": best.get("tot")},
                   "other": {"src": other.get("item", "").split(":", 1)[0], "winner": ow[0], "votes": ow[2], "total": other.get("tot")}}
            if other.get("basis") == "seated" or best.get("basis") == "seated":
                if not same_w:
                    A["seated_differs"].append(rec)     # usually a death, a resignation or a contested seat: worth a look
                    best["chk"] = "seated differs"
                    # someone else took the seat and this count's leader had no majority: a runoff decided it
                    # (Louisiana's December runoffs, Texas specials), so the leader led; never "won"
                    tot = best.get("tot") or sum((x[2] or 0) for x in best["o"])
                    if bw[4] == "won" and tot and (bw[2] or 0) * 2 <= tot:
                        bw[4] = "led"
                        rec["now"] = "led"
                continue
            A["checked"] += 1
            near = lambda a, b: a is not None and b is not None and abs(a - b) <= 0.01 * max(a, b, 1)   # noqa: E731
            near3 = lambda a, b: a is not None and b is not None and abs(a - b) <= 0.03 * max(a, b, 1)  # noqa: E731  totals: write-ins are counted differently
            if same_w and other.get("tot") and best.get("tot") and other["tot"] < 0.97 * best["tot"] and (ow[2] or 0) <= (bw[2] or 0) \
                    and {best.get("item", "").split(":", 1)[0], other.get("item", "").split(":", 1)[0]} <= {"sos_zip", "openelections", "sos_pdf", "manual"}:
                A["partial_other"] = A.get("partial_other", 0) + 1     # the other Georgia file lacks some counties: the fuller is shown
                best.setdefault("chk", "agree")
                A["checked"] -= 1
                continue
            if same_w and near(bw[2], ow[2]) and (near3(best.get("tot"), other.get("tot")) or not other.get("tot") or not best.get("tot")):
                A["agree"] += 1
                best.setdefault("chk", "agree")
            else:
                rec["why"] = "winner" if not same_w else "winner's votes" if not near(bw[2], ow[2]) else "total"
                A["differ"].append(rec)
                best["chk"] = "differ"
        out.append(best)
    return out


def _merge_marked_lines(c: dict) -> None:
    """The FEC's files put a footnote mark in some names ("Sean Patrick # Maloney"), and a candidate on a second party's
    line under the marked name is then a second candidate: New York's 18th District, 2012, showed Nan Hayworth winning
    with 133,049 votes over Sean Patrick Maloney's 132,456 and "Sean Patrick # Maloney"'s 11,389 (Maloney won,
    143,845). The mark is dropped and a candidate's party lines added (one line twice is two elections in one list:
    the larger count, as the reader does); outcomes are worked out again when two rows became one."""
    if c.get("lvl") != "federal" or not any("#" in (x[0] or "") for x in c.get("o") or []):
        return                                  # the FEC's candidates only ("Amendment #4 - Yes" is the state's own label)
    rows, by = [], {}
    merged = False
    for x in c["o"]:
        name = re.sub(r"\s+", " ", re.sub(r"\s*#\s*", " ", x[0] or "")).strip()
        if not name or re.fullmatch(r"\d+", name):
            merged = True                       # "#4": a footnote's own row, not a candidate
            continue
        k = pkey(name)
        if k in by:
            y = by[k]
            if x[2] is not None:
                y[2] = max(y[2] or 0, x[2]) if x[1] == y[1] else (y[2] or 0) + x[2]
            y[3] = max(y[3] or 0, x[3] or 0)
            merged = True
            continue
        y = [name] + list(x[1:])
        by[k] = y
        rows.append(y)
    c["o"] = sorted(rows, key=lambda x: -(x[2] or 0)) if merged else rows
    if merged and not any(x[4] in ("runoff", "unopposed") for x in c["o"]):
        for x in c["o"]:
            x[4] = ""
        tag_outcomes(c["o"], c.get("tot"), majority=_ga_needs_majority(c) and c.get("lvl") != "local")


def _share(c: dict, x: list) -> float | None:
    t = c.get("tot") or sum((y[2] or 0) for y in c["o"])
    return (x[2] or 0) / t if t and x[2] is not None else None


def _runoff_filed_as_general(lst: list[dict], witness: dict | None = None) -> list[dict]:
    """Georgia federal contests a source files as the general though they are the runoff: MEDSL's U.S. Senate, 2008,
    holds the December 2 runoff (Saxby Chambliss 57.4 percent of 2,137,956 votes) as the November 4 general, which the
    FEC's file has (49.8 percent of 3,752,485, no majority); its 1992 race holds the November 24 runoff (Paul Coverdell
    635,114 to Wyche Fowler 618,877) where the Secretary of State's general has Fowler leading with 49.2 percent of
    2,251,576. A "general" whose leader has a majority while another count of the same general (another federal file, or
    `witness`, the state's own) has none, with over a quarter more votes and the same two leaders, is that runoff."""
    gen = [c for c in lst if c.get("scope") == "us" and c.get("st") == "GA" and c.get("stage") == "general"
           and c.get("office") != "President" and c.get("tot")]
    out = []
    for c in gen:
        real = sorted((x for x in c["o"] if not is_writein_bucket(x[0]) and x[2] is not None), key=lambda x: -x[2])
        if len(real) < 2 or (_share(c, real[0]) or 0) <= 0.5:
            continue
        for o in gen + ([witness] if witness else []):
            if o is c or o.get("item", "").split(":", 1)[0] == c.get("item", "").split(":", 1)[0]:
                continue
            ro = sorted((x for x in o["o"] if not is_writein_bucket(x[0]) and x[2] is not None), key=lambda x: -x[2])
            if len(ro) >= 2 and (_share(o, ro[0]) or 1) <= 0.5 and o["tot"] > 1.25 * c["tot"] \
                    and all(any(same_person(a[0], b[0]) for b in ro[:2]) for a in real[:2]):
                out.append(c)
                break
    return out


def _ga_needs_majority(c: dict) -> bool:
    """Georgia's own contests and its federal ones (presidential electors aside): no majority means a runoff."""
    return (c.get("scope") == "ga" or c.get("st") == "GA") and c.get("office") != "President" and c.get("lvl") != "question"


def link_runoffs(cs: list[dict]) -> None:
    """Outcome codes that need the whole year: who went to a runoff, and Georgia's 45 percent general elections."""
    ro = defaultdict(list)
    for c in cs:
        if "runoff" in c["stage"]:
            ro[(c["scope"], c.get("st"), c["office"], str(c.get("dist") or ""), c.get("p") or "", c.get("j") or "")].append(c)
    for c in cs:
        if "runoff" in c["stage"] or c["lvl"] == "question":
            continue
        real = sorted((x for x in c["o"] if not is_writein_bucket(x[0])), key=lambda x: -(x[2] or 0))
        if len(real) < 2 or real[0][2] is None:
            continue
        k = (c["scope"], c.get("st"), c["office"], str(c.get("dist") or ""), c.get("p") or "", c.get("j") or "")

        def after(r: dict) -> bool:
            rd, cd = str(r.get("date") or r["y"]), str(c.get("date") or c["y"])
            return rd >= cd or (len(rd) == 4 and rd == cd[:4])      # a runoff known only by its year: that year's
        later = [r for r in ro.get(k, []) if c["y"] <= r["y"] <= c["y"] + 1 and after(r)
                 and ("primary" in r["stage"]) == ("primary" in c["stage"])
                 and {pkey(x[0]) for x in r["o"]} & {pkey(x[0]) for x in real[:3]}]
        if later:
            r = sorted(later, key=lambda r: str(r.get("date") or r["y"]))[0]
            names = {pkey(x[0]) for x in r["o"]}
            for x in c["o"]:
                x[4] = "runoff" if pkey(x[0]) in names else ""
            c["ro_date"] = r.get("date") or str(r["y"])
        elif real[0][4] == "led" and _ga_needs_majority(c) and c["stage"] == "general" and 1996 <= c["y"] <= 2004 \
                and c.get("tot") and real[0][2] / c["tot"] > 0.45:
            real[0][4] = "won"      # 1996-2004: a general election was won with more than 45 percent (O.C.G.A. 21-2-501 as then written)
        elif real[0][4] == "led" and c["scope"] == "us" and c.get("st") == "GA" and _ga_needs_majority(c) \
                and "primary" not in c["stage"]:
            # Georgia's federal general and special elections: the federal files (MEDSL, the FEC) hold the November
            # count but not the runoff that followed it; the law sends the top two to it, which is what is shown
            for x in real[:2]:
                x[4] = "runoff"


def _match_members(profiles: dict):
    by = defaultdict(list)
    for bid, p in profiles.items():
        last = pkey(p.get("last") or "").split(" ")
        for st in {t["st"] for t in p["terms"]}:
            by[(st, last[-1] if last else "")].append(bid)

    def find(name: str, st: str, year: int):
        k = pkey(name).split(" ")
        if not k:
            return None
        hits = []
        for tok in k[1:] or k:
            for bid in by.get((st, tok), []):
                p = profiles[bid]
                firsts = {pkey(p["n"]).split(" ")[0][:1], pkey(p.get("ballot") or p["n"]).split(" ")[0][:1]}
                if k[0][:1] in firsts and any(int(t["s"][:4]) - 30 <= year <= int(t["e"][:4]) + 30 for t in p["terms"] if t.get("s") and t.get("e")):
                    hits.append(bid)
        hits = sorted(set(hits))
        return hits[0] if len(hits) == 1 else None
    return find


def build(cfg: dict, out: Path, log=print) -> dict:
    work, sp = paths(cfg)
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    audit: dict = {}
    cs = choose(iter_contests(work), audit)
    by_year = defaultdict(list)
    for c in cs:
        by_year[c["y"]].append(c)
        # Georgia's majority rule over the federal files' counts as they were read (the readers apply it since Nov 13,
        # 2026; the files read before then said "won" for every leader: Raphael Warnock with 32.9 percent in the 2020
        # special, 49.4 percent in the 2022 general, both decided in runoffs)
        if c["scope"] == "us" and c.get("st") == "GA" and _ga_needs_majority(c) and c.get("basis") != "seated" \
                and "runoff" not in c["stage"]:
            real = sorted((x for x in c["o"] if not is_writein_bucket(x[0]) and x[2] is not None), key=lambda x: -x[2])
            if len(real) >= 2 and real[0][4] == "won" and (_share(c, real[0]) or 1) <= 0.5:
                real[0][4] = "led"
        # presidential electors go to the plurality in every state, Georgia included: the Secretary of State's files are
        # read with the state's majority rule, which left Bill Clinton (1992, 43.5 percent) and Bob Dole (1996, 47.0)
        # as having only led Georgia
        if c.get("office") == "President" and c.get("lvl") == "federal" and c["stage"] == "general":
            for x in c["o"]:
                if x[4] == "led":
                    x[4] = "won"
    for y in sorted(by_year):
        link_runoffs(by_year[y] + [c for c in by_year.get(y + 1, []) if "runoff" in c["stage"] and str(c.get("date", ""))[5:7] in ("01", "02")])

    profiles = {}
    for f in sorted((work / "people").glob("legislators-*.json")) if (work / "people").exists() else []:
        profiles.update(json.loads(f.read_text(encoding="utf-8")))
    find = _match_members(profiles)

    index_el, people, sources = [], {}, Counter()
    src_urls = {}
    summary = {"us": {}, "ga": {}}
    used_prof = set()
    for scope in ("ga", "us"):
        years = sorted({c["y"] for c in cs if c["scope"] == scope})
        for y in years:
            groups = defaultdict(list)
            for c in by_year[y]:
                if c["scope"] != scope:
                    continue
                groups[(c.get("date") or str(y), c["stage"]) if scope == "ga" else (c["stage"],)].append(c)
            elections = []
            for gk in sorted(groups, key=lambda k: (k[0] if scope == "ga" else "", STAGE_ORDER.get(k[-1], 9))):
                stage = gk[-1]
                lst = groups[gk]
                if scope == "ga":
                    date = gk[0]
                    eid = "h" + re.sub(r"\D", "", date) + STAGE_CODE.get(stage, "x")
                    title = f"{_long_date(date)} - {STAGE_TITLE.get(stage, stage.title())}"
                else:
                    dates = Counter(c.get("date") for c in lst if len(str(c.get("date") or "")) == 10)
                    date = dates.most_common(1)[0][0] if stage == "general" and dates else str(y)
                    eid = f"us{y}{STAGE_CODE.get(stage, 'x')}"
                    title = f"{y} {STAGE_TITLE.get(stage, stage.title())}s, federal offices in every state" if stage != "general" else \
                        f"{_long_date(date)} - General Election, federal offices in every state"
                lo = {"federal": 0, "statewide": 1, "question": 2, "legislative": 3, "judicial": 4, "local": 5}
                lst.sort(key=lambda c: (lo.get(c["lvl"], 9), {"President": 0, "U.S. Senate": 1, "U.S. House": 2}.get(c["office"], 3),
                                        c.get("st") or "", c["office"], int(c["dist"]) if str(c.get("dist") or "").isdigit() else 0,
                                        c.get("p") or "", c.get("j") or ""))
                out_c, lv = [], Counter()
                for i, c in enumerate(lst):
                    item_src = c.get("item", "").split(":", 1)[0]
                    sources[item_src] += 1
                    src_urls[item_src] = c["src"]
                    key = c["office"].replace("U.S. ", "US ") + (" " + str(c["dist"]) if c.get("dist") else "")
                    oc = {"i": i, "lvl": c["lvl"], "sub": "", "key": key, "t": c["t"], "p": c.get("p") or "", "j": c.get("j") or "",
                          "vf": c.get("vf") or 1, "tot": c.get("tot"), "o": c["o"], "u": c["src"]["u"], "srct": c["src"]["t"],
                          "off": c["office"], "d": str(c.get("dist") or ""), "b": c.get("basis") or ""}
                    if scope == "us":
                        oc["st"] = c["st"]
                    if c.get("date") and c["date"] != date:
                        oc["dt"] = c["date"]
                    if c.get("cty"):
                        oc["cty"] = c["cty"]
                    if c.get("sp"):
                        oc["sp"] = 1
                    if c.get("ro_date"):
                        oc["ro"] = ["", c["ro_date"]]
                    if c.get("chk"):
                        oc["chk"] = c["chk"]
                    ids = {}
                    for x in c["o"]:
                        if c["lvl"] == "question" or is_writein_bucket(x[0]):
                            continue
                        k = pkey(x[0])
                        if not k or " " not in k:
                            continue
                        pct = round(100 * x[2] / c["tot"], 1) if c.get("tot") and x[2] is not None else None
                        label = c["t"] + (" (special)" if c.get("sp") and "special" not in stage else "")
                        pe = people.setdefault(k, {"n": x[0], "r": []})
                        pe["r"].append([eid, i, c.get("date") or date, label, c.get("j") or "", c.get("p") or x[1] or "", x[2], pct, x[4] or "",
                                        c["lvl"], c["src"]["u"], stage])
                        if scope == "us" and c["office"] != "President":
                            bid = find(x[0], c["st"], y)
                            if bid:
                                ids[x[0]] = bid
                                pe.setdefault("bs", Counter())[bid] += 1
                                used_prof.add(bid)
                    if ids:
                        oc["bg"] = ids
                    out_c.append(oc)
                    lv[c["lvl"]] += 1
                el = {"eid": eid, "date": date, "title": title, "kind": stage, "official": True, "u": lst[0]["src"]["u"], "c": out_c}
                elections.append(el)
                index_el.append({"eid": eid, "scope": scope, "date": date, "y": str(y), "title": title, "kind": stage, "n": len(out_c),
                                 "lv": dict(lv), "official": True, "u": el["u"],
                                 "votes": sum(1 for c in out_c if c["tot"]), "f": f"{scope}_{y}.json"})
            _dump(out / f"{scope}_{y}.json", {"year": str(y), "scope": scope, "elections": elections})
            summary[scope][str(y)] = _summarize(scope, by_year[y])

    for k, pe in people.items():
        bs = pe.pop("bs", None)
        if bs and len(bs) == 1:
            pe["b"] = next(iter(bs))
        pe["r"].sort(key=lambda r: str(r[2]))
    shards = defaultdict(dict)
    for k, pe in people.items():
        shards[shard(k, PEOPLE_SHARDS)][k] = pe
    for i in range(PEOPLE_SHARDS):
        _dump(out / "people" / f"{i}.json", shards.get(i, {}))
    # a small name index for search-as-you-type: person key -> shard is computed, so only the count is needed
    pshards = defaultdict(dict)
    for bid in sorted(profiles):
        p = profiles[bid]
        pshards[shard(bid, PROF_SHARDS)][bid] = {"n": p["n"], "born": p.get("born") or "", "terms": p["terms"],
                                                  "srct": p["src"]["t"], "u": p["src"]["u"]}
    for i in range(PROF_SHARDS):
        _dump(out / "prof" / f"{i}.json", pshards.get(i, {}))

    st = load_state(sp)
    held = defaultdict(list)
    for iid, rec in sorted(st["items"].items()):
        if rec.get("status") in ("held", "failed"):
            held[iid.split(":", 1)[0]].append({"t": rec.get("title") or iid, "u": rec.get("url"), "note": rec.get("note", "")[:240]})
    ga_years = sorted(int(y) for y in summary["ga"])
    us_years = sorted(int(y) for y in summary["us"])
    index = {
        # the time of the newest file read, not of this build: a build of the same files gives the same bytes
        "v": 1, "built": max([r.get("at", "") for r in st["items"].values() if r.get("status") == "done"] or [""])[:16] + "Z",
        "elections": sorted(index_el, key=lambda e: (e["date"], e["eid"])),
        "years": {"ga": ga_years, "us": us_years},
        "people": {"n": PEOPLE_SHARDS, "count": len(people)}, "prof": {"n": PROF_SHARDS, "count": len(profiles)},
        "sources": [{"id": k, "n": n, "t": src_urls[k]["t"], "u": src_urls[k]["u"]} for k, n in sources.most_common() if k in src_urls],
        "pending": {k: v[:60] for k, v in held.items()},
        "coverage": _coverage(summary, cs),
        "audit": {"checked": audit["checked"], "agree": audit["agree"], "differ": len(audit["differ"]), "partial_other": audit.get("partial_other", 0),
                  "seated_differs": len(audit["seated_differs"]), "seats": _seat_check(summary)},
    }
    _dump(out / "index.json", index)
    _dump(out / "summary.json", summary)
    audit["differ"].sort(key=lambda r: (-r["y"], r["race"]))
    audit["seated_differs"].sort(key=lambda r: (-r["y"], r["race"]))
    _dump(out / "audit.json", {"checked": audit["checked"], "agree": audit["agree"], "differ": audit["differ"][:3000],
                               "seated_differs": audit["seated_differs"][:1500], "seats": _seat_check(summary),
                               "runoffs_filed_as_general": audit.get("runoffs_filed_as_general", []),
                               "how": "Where two sources report the same contest they are compared: the winner (by surname), the "
                                      "winner's votes (within 1 percent) and the total (within 3 percent). 'seats' counts the U.S. House and Senate "
                                      "general-election contests on file each year against the seats there were."})
    man = {}
    for p in sorted(out.rglob("*.json")):
        b = p.read_bytes()
        man[str(p.relative_to(out))] = [len(b), hashlib.sha1(b).hexdigest()[:16]]
    _dump(out / "manifest.json", {"built": index["built"], "files": man})
    res = {"contests": len(cs), "elections": len(index_el), "people": len(people), "profiles": len(profiles),
           "files": len(man) + 1, "bytes": sum(v[0] for v in man.values()), "ga_years": ga_years[:1] + ga_years[-1:],
           "us_years": us_years[:1] + us_years[-1:]}
    log(json.dumps(res))
    return res


def _seat_check(summary: dict) -> dict:
    """U.S. House general elections on file each year against the 435 voting seats (plus the delegates' seats, which the
    sources also list), and Senate against the 33 or 34 regular seats: a year far off is a year to look at."""
    out = {}
    for y, s in sorted(summary.get("us", {}).items()):
        if int(y) % 2:
            continue
        h, sn = s.get("U.S. House", {}).get("n", 0), s.get("U.S. Senate", {}).get("n", 0)
        flag = []
        if h and not 435 <= h <= 441:
            flag.append("house")
        if sn and not 32 <= sn <= 35:
            flag.append("senate")
        out[y] = {"house": h, "senate": sn, **({"look": flag} if flag else {})}
    return out


def _party_of(c: dict):
    w = [x for x in c["o"] if x[4] in ("won", "unopposed")]
    return (w[0][1] or "O") if w else None


def _summarize(scope: str, contests: list[dict]) -> dict:
    """Totals of a year, general elections first: what a trend chart or a question about a year starts from."""
    S: dict = {}
    gen = [c for c in contests if c["scope"] == scope and c["stage"] == "general" and not c.get("sp")]
    offices = ("President", "U.S. Senate", "U.S. House") if scope == "us" else ("State Senate", "State House", "U.S. House")
    for off in offices:
        cc = [c for c in gen if c["office"] == off]
        if not cc:
            continue
        seats, votes, unc, close = Counter(), Counter(), 0, []
        for c in cc:
            p = _party_of(c)
            if p:
                seats[p if p in ("D", "R") else "O"] += 1
            real = sorted((x for x in c["o"] if not is_writein_bucket(x[0]) and x[2] is not None), key=lambda x: -x[2])
            for x in real:
                votes[x[1] if x[1] in ("D", "R") else "O"] += x[2]
            if len(real) < 2 and any(x[4] in ("won", "unopposed") for x in c["o"]) and (real or c.get("b") != "seated"):
                unc += 1 if real else 0
            if len(real) >= 2 and c.get("tot"):
                close.append((round(100 * (real[0][2] - real[1][2]) / c["tot"], 2), c["t"], real[0][0], real[0][1], real[1][0], real[1][1]))
        S[off] = {"n": len(cc), "seats": dict(seats), "votes": dict(votes), "unopposed": unc,
                  "closest": [list(x) for x in sorted(close)[:8]], "no_votes_on_file": sum(1 for c in cc if not c.get("tot"))}
    if scope == "ga":
        sw = {}
        for c in gen:
            if c["lvl"] in ("statewide", "federal") and c["office"] != "U.S. House" and c.get("tot"):
                real = sorted((x for x in c["o"] if x[2] is not None), key=lambda x: -x[2])
                if real:
                    sw[c["t"]] = {"w": real[0][0], "p": real[0][1], "pct": round(100 * real[0][2] / c["tot"], 1), "tot": c["tot"],
                                  "m": round(100 * (real[0][2] - real[1][2]) / c["tot"], 1) if len(real) > 1 else None,
                                  "r": real[1][0] if len(real) > 1 else "", "rp": real[1][1] if len(real) > 1 else "", "out": real[0][4]}
        if sw:
            S["statewide"] = sw
    S["stages"] = dict(Counter(c["stage"] for c in contests if c["scope"] == scope))
    S["levels"] = dict(Counter(c["lvl"] for c in contests if c["scope"] == scope))
    return S


def _coverage(summary: dict, cs: list[dict]) -> str:
    def span(scope, pred):
        ys = sorted({c["y"] for c in cs if c["scope"] == scope and pred(c)})
        return f"{ys[0]}–{ys[-1]}" if ys else "none yet"
    parts = [
        "Federal offices in every state, general elections with vote counts: President by state "
        + span("us", lambda c: c["stage"] == "general" and c.get("tot") and c["office"] == "President") + ", U.S. Senate "
        + span("us", lambda c: c["stage"] == "general" and c.get("tot") and c["office"] == "U.S. Senate") + ", U.S. House "
        + span("us", lambda c: c["stage"] == "general" and c.get("tot") and c["office"] == "U.S. House")
        + ". For earlier House and Senate elections back to 1968 the member seated is shown without a vote count. Federal primaries "
        + span("us", lambda c: "primary" in c["stage"]) + ".",
        "Georgia before 2016: " + span("ga", lambda c: True) + " (statewide, federal, legislative and judicial contests and ballot "
        "questions by county, with primaries and runoffs; county and city contests where the state's archive holds them).",
        "Not on file yet: Georgia's results before 2000 (the Secretary of State's site turns crawlers away, so its 1988–2015 "
        "files are read when downloaded by hand; earlier years exist only in printed registers), presidential results by state "
        "for 1968 and 1972, and city-run elections, which were never reported to the state and are added as each city's or "
        "county's own records are found.",
    ]
    return " ".join(parts)


def verify(bundle: Path, electo_hist: Path) -> dict:
    """Is every file of the bundle in Electo, byte for byte? (before the hand-off copy is deleted)"""
    man = json.loads((Path(bundle) / "manifest.json").read_text(encoding="utf-8"))["files"]
    missing, differ = [], []
    for rel, (size, h) in man.items():
        p = Path(electo_hist) / rel
        if not p.exists():
            missing.append(rel)
            continue
        b = p.read_bytes()
        if len(b) != size or hashlib.sha1(b).hexdigest()[:16] != h:
            differ.append(rel)
    return {"ok": not missing and not differ, "files": len(man), "missing": missing[:20], "differ": differ[:20]}
