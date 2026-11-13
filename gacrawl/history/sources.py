"""Where election history is read from. Every address comes from an index the publisher keeps (a dataset's file
list, a repository's tree, the links on a results page): none is built from a pattern.

Each source gives items (one published file, or one election's files) and reads an item into contests
(gacrawl/history/model.py). A source that cannot read a file says what it found (Skip) so the progress file shows it.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .model import (STATE_BY_NAME, STATES, general_day, is_writein_bucket, nice_name, party_code, tag_outcomes)

FIRST_YEAR = 1968
TRUE = {"TRUE", "T", "YES", "Y", "1"}


class Skip(Exception):
    """This item holds nothing this reader can use (the reason is kept in the progress file)."""


@dataclass
class Item:
    id: str
    url: str
    title: str = ""
    meta: dict = field(default_factory=dict)


def _int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[,\s]", "", str(v))
    m = re.fullmatch(r"-?\d+(\.0+)?", s)
    return int(float(s)) if m else None


def _links(html: str, base: str):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        yield urljoin(base, a["href"].strip()), re.sub(r"\s+", " ", a.get_text(" ", strip=True))


_MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                            "september", "october", "november", "december"])}


def _date_in(text: str) -> str:
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*,?\s+((?:19|20)\d\d)\b", text or "")
    if m:
        mo = next((n for k, n in _MONTHS.items() if k.startswith(m.group(1).lower()[:3])), None)
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
    return ""


def _stage_of(title: str) -> str:
    t = title.lower()
    sp, ro, pr = "special" in t, "runoff" in t or "run-off" in t, "primary" in t
    if pr and "preference" in t:
        return "primary"
    if pr:
        return ("special primary" if sp and "general primary" not in t and "gen primary" not in t else "primary") + (" runoff" if ro else "")
    if ro:
        return "special runoff" if sp else "general runoff"
    return "special" if sp and "general" not in t else "general"


# ---------------------------------------------------------------- Georgia offices -> Electo's levels and titles
_STATEWIDE = (
    (r"lieutenant governor|lt\.? governor", "Lieutenant Governor"), (r"^governor\b", "Governor"),
    (r"secretary of state", "Secretary of State"), (r"attorney general", "Attorney General"),
    (r"agriculture", "Commissioner of Agriculture"), (r"insurance", "Commissioner of Insurance"),
    (r"labor", "Commissioner of Labor"), (r"school superintendent|superintendent of schools", "State School Superintendent"),
    (r"public service comm", "Public Service Commission"),
)


def ga_office(raw: str, district: str = "") -> tuple[str, str, str, str]:
    """(level, office, district, title) for a contest name as Georgia's results files write it."""
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    sl = s.lower()
    d = re.sub(r"^0+(?=\d)", "", str(district or "").strip())
    if d.lower() in ("nan", "none", "statewide"):
        d = ""
    if not d:
        m = re.search(r"\b(?:district|dist\.?)\s*(\d{1,3})\b", sl) or re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\s+(?:congressional\s+)?district", sl)
        if m:
            d = str(int(m.group(1)))
    if re.search(r"amendment|referendum|\bquestion\b|proposed|^shall\b|ballot (?:measure|question)|sunday sales|\bsplost\b|\bbond\b", sl):
        return "question", s, "", s
    if re.search(r"\bpresident\b", sl) and "vice" not in sl.replace("president and vice", "") and "senate" not in sl:
        return "federal", "President", "", "President of the United States"
    if re.search(r"u\.?\s?s\.?\s+senat|united states senat", sl):
        return "federal", "U.S. Senate", "", "U.S. Senate"
    if re.search(r"u\.?\s?s\.?\s+(house|rep)|united states rep|congress", sl):
        return "federal", "U.S. House", d, "U.S. House District " + d if d else "U.S. House"
    if re.search(r"state senat|^senat(e|or)\b|^state senator", sl):
        return "legislative", "State Senate", d, "State Senate District " + d if d else "State Senate"
    if re.search(r"state (house|rep)|^representative\b|house of representatives|general assembly", sl):
        return "legislative", "State House", d, "State House District " + d if d else "State House"
    if re.search(r"justice|supreme court|court of appeals|\bjudge\b|superior court|state court|district attorney|solicitor", sl):
        return "judicial", s, d, s
    for rx, name in _STATEWIDE:
        if re.search(rx, sl):
            if name == "Public Service Commission":
                return "statewide", name, d, name + (" District " + d if d else "")
            return "statewide", name, "", name
    return "local", s, d, s


# ---------------------------------------------------------------- MEDSL (Harvard Dataverse)
class Medsl:
    """U.S. President by state, U.S. Senate, U.S. House: general elections (and the runoffs and specials the files
    carry), 1976 on. Dataset identifiers as published by the MIT Election Data and Science Lab on Harvard Dataverse."""
    name = "medsl"
    DATASETS = (("president", "doi:10.7910/DVN/42MVDX"), ("senate", "doi:10.7910/DVN/PEJ5QU"), ("house", "doi:10.7910/DVN/IG0UN2"))
    API = "https://dataverse.harvard.edu/api"

    def discover(self, net):
        out = []
        for kind, doi in self.DATASETS:
            d = net.json(f"{self.API}/datasets/:persistentId/", params={"persistentId": doi})["data"]
            ver = d.get("latestVersion") or {}
            for f in ver.get("files") or []:
                df = f.get("dataFile") or {}
                fn = (df.get("originalFileName") or df.get("filename") or f.get("label") or "").lower()
                if not re.search(r"\.(csv|tab|tsv)$", fn) or re.search(r"codebook|readme|source", fn):
                    continue
                out.append(Item(id=f"medsl:{kind}:{df['id']}:{ver.get('versionNumber')}.{ver.get('versionMinorNumber')}",
                                url=f"{self.API}/access/datafile/{df['id']}", title=f"MEDSL {kind}: {fn}",
                                meta={"kind": kind, "doi": doi, "format": "original" if df.get("originalFileFormat") else ""}))
        return out

    def read(self, net, item, work):
        # the file as its publisher uploaded it; a file Dataverse did not convert has no "original" form (HTTP 400)
        tries = [{"format": "original"}, None] if item.meta.get("format") else [None, {"format": "original"}]
        raw, err = None, None
        for params in tries:
            try:
                raw = net.bytes(item.url, params=params)
                break
            except IOError as exc:
                err = exc
        if raw is None:
            raise err
        text = raw.decode("utf-8-sig", errors="replace")
        yield from medsl_contests(text, item.meta["kind"], "https://doi.org/" + item.meta["doi"].split(":", 1)[1])


def medsl_contests(text: str, kind: str, src_url: str):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    head = text.split("\n", 1)[0]
    rd = csv.DictReader(io.StringIO(text), delimiter="\t" if head.count("\t") > head.count(",") else ",")
    groups: dict = defaultdict(lambda: {"rows": defaultdict(lambda: {"v": 0, "parties": []}), "tot": None})
    label = {"president": "U.S. President", "senate": "U.S. Senate", "house": "U.S. House"}[kind]
    for r in rd:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        y, st = _int(r.get("year")), (r.get("state_po") or "").upper()
        if not y or y < FIRST_YEAR or st not in STATES:
            continue
        stage = (r.get("stage") or "GEN").upper()
        if stage not in ("GEN", "PRI"):
            continue
        dist = ""
        if kind == "house":
            dn = _int(r.get("district"))
            dist = "AL" if not dn else str(dn)
        special, runoff = (r.get("special") or "").upper() in TRUE, (r.get("runoff") or "").upper() in TRUE
        g = groups[(y, st, dist, stage, special, runoff)]
        tv = _int(r.get("totalvotes"))
        if tv is not None:
            g["tot"] = max(g["tot"] or 0, tv)
        cand = r.get("candidate") or ""
        if not cand or cand.upper() in ("NA", "NAN") or (r.get("writein") or "").upper() in TRUE and is_writein_bucket(cand or "WRITEIN"):
            continue
        if is_writein_bucket(cand):
            continue
        row = g["rows"][re.sub(r"\s+", " ", cand.upper())]
        v = _int(r.get("candidatevotes")) or 0
        row["v"] += v
        row["name"] = cand
        row["parties"].append((v, r.get("party_simplified") or r.get("party") or r.get("party_detailed") or ""))
    for (y, st, dist, stage, special, runoff), g in sorted(groups.items(), key=lambda kv: [str(x) for x in kv[0]]):
        o = []
        for row in g["rows"].values():
            codes = [party_code(p) for _, p in sorted(row["parties"], key=lambda x: -x[0])]
            pc = next((c for c in codes if c in ("D", "R")), codes[0] if codes else "")
            o.append([nice_name(row["name"]), pc, row["v"], 0, ""])
        if not o:
            continue
        o.sort(key=lambda x: -x[2])
        if stage == "PRI":
            st_name = "primary"
        elif runoff:
            st_name = "special runoff" if special else "general runoff"
        else:
            st_name = "special" if special else "general"
        office = {"president": "President", "senate": "U.S. Senate", "house": "U.S. House"}[kind]
        where = STATES[st] + ((" At Large" if dist == "AL" else " District " + dist) if kind == "house" else "")
        c = {"scope": "us", "y": y, "date": general_day(y) if st_name == "general" else str(y), "stage": st_name, "st": st,
             "office": office, "dist": dist, "lvl": "federal", "p": "", "j": "", "t": f"{office} · {where}", "tot": g["tot"],
             "o": o, "src": {"t": f"MIT Election Data and Science Lab, {label} returns (Harvard Dataverse)", "u": src_url},
             "basis": "compiled"}
        if special:
            c["sp"] = 1
        tag_outcomes(o, g["tot"], majority=False)
        yield c


# ---------------------------------------------------------------- members of Congress (unitedstates/congress-legislators)
class Legislators:
    """Who was seated: the winners of the 1968-1974 elections (no vote count on file yet), and a profile for every
    member of Congress who has served since January 1969."""
    name = "legislators"
    FILES = (("historical", "https://unitedstates.github.io/congress-legislators/legislators-historical.json"),
             ("current", "https://unitedstates.github.io/congress-legislators/legislators-current.json"))

    def discover(self, net):
        import datetime as dt
        week = dt.date.today().strftime("%G-W%V")          # read again each week (members change)
        return [Item(id=f"legislators:{k}:{week}", url=u, title=f"congress-legislators {k}", meta={"kind": k}) for k, u in self.FILES]

    def read(self, net, item, work):
        people = json.loads(net.bytes(item.url))
        profiles, contests = legislators_read(people, item.url)
        out = Path(work) / "people"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"legislators-{item.meta['kind']}.json").write_text(json.dumps(profiles, separators=(",", ":")), encoding="utf-8")
        yield from contests


def legislators_read(people: list, src_url: str):
    profiles, seats = {}, defaultdict(list)
    src = {"t": "unitedstates/congress-legislators (public domain): members of the United States Congress", "u": src_url}
    for p in people:
        terms = [t for t in p.get("terms") or [] if str(t.get("end") or "") >= "1969-01-03"]
        if not terms:
            continue
        nm = p.get("name") or {}
        name = nm.get("official_full") or " ".join(x for x in (nm.get("first"), nm.get("middle"), nm.get("last"), nm.get("suffix")) if x)
        ballot = " ".join(x for x in (nm.get("nickname") or nm.get("first"), nm.get("last")) if x)
        bid = (p.get("id") or {}).get("bioguide") or ""
        if not bid or not name:
            continue
        tl = []
        for t in p.get("terms") or []:
            ch = "Senate" if t.get("type") == "sen" else "House"
            d = t.get("district")
            tl.append({"ch": ch, "st": t.get("state"), "d": ("AL" if d in (0, "0") else str(d)) if ch == "House" and d is not None else "",
                       "s": t.get("start"), "e": t.get("end"), "p": t.get("party") or "", "how": t.get("how") or ""})
        bio = p.get("bio") or {}
        ids = p.get("id") or {}
        profiles[bid] = {"n": name, "ballot": ballot, "last": nm.get("last") or "", "born": bio.get("birthday") or "",
                         "terms": tl, "fec": ids.get("fec") or [], "src": src}
        for t in terms:
            start = str(t.get("start") or "")
            if t.get("how") or not re.fullmatch(r"19(69|71|73|75)-01-\d\d", start):
                continue                                   # appointed or elected at a special election: not that November's winner
            y = int(start[:4]) - 1
            ch = "U.S. Senate" if t.get("type") == "sen" else "U.S. House"
            d = t.get("district")
            dist = ("AL" if d in (0, "0", None) else str(d)) if ch == "U.S. House" else ""
            seats[(y, t.get("state"), ch, dist)].append([name, party_code(t.get("party")), None, 0, "won"])

    contests = []
    for (y, st, office, dist), o in sorted(seats.items(), key=lambda kv: [str(x) for x in kv[0]]):
        if st not in STATES:
            continue
        where = STATES[st] + ((" At Large" if dist == "AL" else " District " + dist) if office == "U.S. House" else "")
        contests.append({"scope": "us", "y": y, "date": general_day(y), "stage": "general", "st": st, "office": office, "dist": dist,
                         "lvl": "federal", "p": "", "j": "", "t": f"{office} · {where}", "tot": None, "o": o, "src": src,
                         "basis": "seated"})
    return profiles, contests


# ---------------------------------------------------------------- FEC "Federal Elections" spreadsheets
TOP_TWO = {"WA": 2008, "CA": 2012, "AK": 2022}      # one primary for every party from that year on (no party primaries)


class Fec:
    """Primaries and primary runoffs of every federal candidate (the general elections come from MEDSL), from the
    spreadsheets the FEC publishes with each "Federal Elections" report."""
    name = "fec"
    INDEX = "https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/"

    def discover(self, net):
        seen, out, pages = set(), [], []
        html = net.text(self.INDEX)
        for u, text in _links(html, self.INDEX):
            if re.search(r"\.xlsx?$", urlsplit(u).path, re.I):
                pages.append((u, text, self.INDEX))
            elif urlsplit(u).netloc.endswith("fec.gov") and re.search(r"federal[- ]elections[- ](19|20)\d\d", u + " " + text, re.I):
                try:
                    sub = net.text(u)
                except Exception:
                    continue
                for u2, t2 in _links(sub, u):
                    if re.search(r"\.xlsx?$", urlsplit(u2).path, re.I):
                        pages.append((u2, t2 or text, u))
        for u, text, frm in pages:
            m = re.search(r"((?:19|20)\d\d)", urlsplit(u).path.rsplit("/", 1)[-1]) or re.search(r"((?:19|20)\d\d)", text)
            if not m or u in seen:
                continue
            seen.add(u)
            out.append(Item(id="fec:" + urlsplit(u).path.rsplit("/", 1)[-1].lower(), url=u, title=f"FEC Federal Elections {m.group(1)}",
                            meta={"year": int(m.group(1)), "from": frm}))
        return out

    def read(self, net, item, work):
        raw = net.bytes(item.url)
        sheets = xls_sheets(raw, item.url)
        n = 0
        for c in fec_contests(sheets, item.meta["year"], item.url):
            n += 1
            yield c
        if not n:
            raise Skip("no sheet with candidate names and primary votes: " + "; ".join(f"{s}: {(rows[:1] or [[]])[0][:8]}" for s, rows in sheets)[:600])


def xls_sheets(raw: bytes, name: str):
    """[(sheet name, rows)] of an .xlsx or .xls file."""
    out = []
    if raw[:2] == b"PK":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        for ws in wb.worksheets:
            out.append((ws.title, [list(r) for r in ws.iter_rows(values_only=True)]))
    else:
        import xlrd
        wb = xlrd.open_workbook(file_contents=raw)
        for sh in wb.sheets():
            out.append((sh.name, [sh.row_values(i) for i in range(sh.nrows)]))
    return out


def fec_contests(sheets, year: int, url: str):
    src = {"t": f"Federal Election Commission, Federal Elections {year}: official results as certified by the states", "u": url}
    groups: dict = defaultdict(list)
    for sname, rows in sheets:
        hi = next((i for i, r in enumerate(rows[:15]) if any(re.search(r"CANDIDATE NAME|LAST NAME, *FIRST", str(c or ""), re.I) for c in r)), None)
        if hi is None:
            continue
        H = [re.sub(r"\s+", " ", str(c or "")).strip().upper() for c in rows[hi]]

        def col(*rxs):
            for rx in rxs:
                for i, h in enumerate(H):
                    if re.search(rx, h):
                        return i
            return None
        ci = {"st": col(r"^STATE ABBREVIATION$", r"^STATE$"), "d": col(r"^(D|DISTRICT)$"), "fec": col(r"FEC ID"),
              "inc": col(r"^\(I\)$", r"INCUMBENT"), "name": col(r"^CANDIDATE NAME$", r"^CANDIDATE NAME \(LAST, FIRST\)$", r"LAST NAME, *FIRST", r"CANDIDATE NAME"),
              "party": col(r"^PARTY$"), "pri": col(r"^PRIMARY( VOTES| RESULTS)?$"), "ro": col(r"^RUNOFF( VOTES| RESULTS)?$"),
              "pdate": col(r"^PRIMARY DATE$"), "rdate": col(r"^RUNOFF DATE$")}
        if ci["st"] is None or ci["name"] is None or ci["pri"] is None:
            continue
        sl = sname.lower()
        sheet_office = "President" if "pres" in sl else "U.S. Senate" if "senate" in sl and "house" not in sl else \
            "U.S. House" if "house" in sl and "senate" not in sl else ""
        for r in rows[hi + 1:]:
            def g(k):
                i = ci[k]
                return r[i] if i is not None and i < len(r) else None
            st = str(g("st") or "").strip().upper()
            name = re.sub(r"\s+", " ", str(g("name") or "")).strip()
            if st not in STATES or not name or re.search(r"\btotal\b|scatter|district votes|party votes|^n/a$", name, re.I) or is_writein_bucket(name):
                continue
            dv = str(g("d") if g("d") is not None else "").strip().upper()
            dv = re.sub(r"\.0$", "", dv)
            office, dist, sp = sheet_office, "", 0
            if office != "President" and (not office or dv.startswith("S")):
                office = "U.S. Senate" if dv.startswith("S") else office or "U.S. House"
            if "UNEXPIRED" in dv or "SPECIAL" in dv:
                sp = 1
            if office == "U.S. House":
                m = re.match(r"(\d+)", dv)
                if dv.startswith("H") or not m:
                    continue
                dist = "AL" if int(m.group(1)) == 0 else str(int(m.group(1)))
            pc = party_code(g("party"))
            inc = 1 if str(g("inc") or "").strip() else 0
            fid = str(g("fec") or "").strip()
            for stage, vk, dk in (("primary", "pri", "pdate"), ("primary runoff", "ro", "rdate")):
                raw_v = g(vk)
                v = _int(raw_v)
                unopp = bool(re.search(r"unopposed", str(raw_v or ""), re.I))
                if v is None and not unopp:
                    continue
                since = TOP_TWO.get(st)
                p = "" if (since and year >= since) or st == "LA" else pc
                d = g(dk)
                date = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else (_date_in(str(d or "")) or str(year))
                if not date.startswith(str(year)):
                    date = str(year)
                groups[(st, office, dist, stage, p, sp)].append(([nice_name(name), pc, v, inc, "unopposed" if unopp and v is None else ""], date, fid))
    for (st, office, dist, stage, p, sp), rows in sorted(groups.items(), key=lambda kv: [str(x) for x in kv[0]]):
        merged: dict = {}
        for o, date, fid in rows:                     # a candidate listed once per party line: one row, votes added
            k = o[0].upper()
            if k in merged:
                if o[2] is not None:
                    merged[k][0][2] = (merged[k][0][2] or 0) + o[2]
            else:
                merged[k] = (o, date, fid)
        o = sorted((x[0] for x in merged.values()), key=lambda x: -(x[2] or 0))
        dates = sorted({x[1] for x in merged.values() if len(x[1]) == 10})
        tot = sum(x[2] or 0 for x in o) or None
        where = STATES[st] + ((" At Large" if dist == "AL" else " District " + dist) if office == "U.S. House" else "")
        c = {"scope": "us", "y": year, "date": dates[0] if len(dates) == 1 else str(year), "stage": stage, "st": st, "office": office,
             "dist": dist, "lvl": "federal", "p": p if p in ("D", "R", "L", "G") else "", "j": "", "t": f"{office} · {where}",
             "tot": tot, "o": o, "src": src, "basis": "official",
             "ids": {x[0][0]: {"fec": x[2]} for x in merged.values() if x[2]}}
        if p and p not in ("D", "R", "L", "G"):
            continue                                   # minor-party and independent lines are not primaries
        if sp:
            c["sp"] = 1
        if not any(x[4] for x in o):
            tag_outcomes(o, tot, majority=False)
            if stage == "primary" and len(o) > 1 and o[0][4] == "won" and tot and (o[0][2] or 0) * 2 <= tot \
                    and (st, office, dist, "primary runoff", p, sp) in groups:
                o[0][4] = o[1][4] = "runoff"
        yield c


# ---------------------------------------------------------------- OpenElections, Georgia
_OE_STAGE = {"general", "primary", "runoff", "special", "recount", "nonpartisan"}
_OE_PARTY = {"dem": "D", "democratic": "D", "rep": "R", "republican": "R"}
_OE_LEVEL = {"precinct", "county", "county-level", "state", "statewide", "cd", "congressional_district", "state_legislative"}


class OpenElections:
    """Georgia 2000-2015, every office the Secretary of State certified, by county: primaries, runoffs, general and
    special elections (2016 on is already in Electo from the state's own system)."""
    name = "openelections"
    REPO = "https://api.github.com/repos/openelections/openelections-data-ga"

    def discover(self, net):
        branch = net.json(self.REPO).get("default_branch") or "master"
        tree = net.json(f"{self.REPO}/git/trees/{branch}", params={"recursive": "1"})
        groups: dict = defaultdict(list)
        for f in tree.get("tree") or []:
            p = f.get("path") or ""
            m = re.fullmatch(r"(\d{4})/((\d{4})(\d{2})(\d{2})__ga__([a-z0-9_\-]+))\.csv", p)
            if not m or f.get("type") != "blob":
                continue
            y = int(m.group(3))
            if y < FIRST_YEAR or y >= 2016:
                continue
            toks = m.group(6).split("__")
            stage_t = [t for t in toks if t in _OE_STAGE or t in _OE_PARTY]
            place = [t for t in toks if t not in _OE_STAGE and t not in _OE_PARTY and t not in _OE_LEVEL]
            if "recount" in stage_t:
                continue
            groups[(f"{m.group(3)}-{m.group(4)}-{m.group(5)}", "__".join(stage_t))].append(
                {"path": p, "place": place, "precinct": "precinct" in toks, "sha": (f.get("sha") or "")[:10],
                 "url": f"https://raw.githubusercontent.com/openelections/openelections-data-ga/{branch}/{p}"})
        out = []
        for (date, stage_t), files in sorted(groups.items()):
            whole = [f for f in files if not f["place"]]
            if whole:                                   # a statewide file: county-level before precinct
                files = sorted(whole, key=lambda f: f["precinct"])[:1]
            t = stage_t.split("__")
            sp, ro, pr = "special" in t, "runoff" in t, "primary" in t
            stage = (("special primary" if sp else "primary") + (" runoff" if ro else "")) if pr else \
                ("special runoff" if sp else "general runoff") if ro else "special" if sp and "general" not in t else "general"
            party = next((_OE_PARTY[x] for x in t if x in _OE_PARTY), "")
            sig = "-".join(sorted(f["sha"] for f in files))[:60]
            out.append(Item(id=f"openelections:{date}:{stage_t}:{len(files)}:{sig}", url=files[0]["url"], title=f"Georgia {date} {stage}",
                            meta={"date": date, "stage": stage, "party": party, "sp": 1 if sp else 0, "files": [f["url"] for f in files]}))
        return out

    def read(self, net, item, work):
        texts = [net.bytes(u).decode("utf-8-sig", errors="replace") for u in item.meta["files"]]
        yield from oe_contests(texts, item.meta["date"], item.meta["stage"], item.meta.get("party", ""), item.meta.get("sp", 0),
                               "https://github.com/openelections/openelections-data-ga")


def county_name(c: str) -> str:
    c = re.sub(r"\s+county$", "", re.sub(r"\s+", " ", str(c or "")).strip(), flags=re.I)
    if not c:
        return ""
    c = " ".join(w[:1].upper() + w[1:].lower() for w in c.split(" "))
    return {"Dekalb": "DeKalb", "Mcduffie": "McDuffie", "Mcintosh": "McIntosh", "Jeff Davis": "Jeff Davis", "Ben Hill": "Ben Hill"}.get(c, c)


_OE_PARTS = ("election_day_votes", "advanced_votes", "absentee_by_mail_votes", "provisional_votes", "election_day", "absentee",
             "early_voting", "provisional", "absentee_by_mail", "advance_in_person", "advance_voting", "mail", "early_votes",
             "absentee_votes", "election_day_vote", "advance_votes", "mail_votes")


def _oe_votes(r: dict):
    """A row's votes: its total column, else the sum of the ways of voting the file breaks it into (2012 on)."""
    for k in ("votes", "total_votes", "total"):
        v = _int(r.get(k))
        if v is not None:
            return v
    parts = [_int(r.get(k)) for k in _OE_PARTS if r.get(k) not in (None, "")]
    parts = [x for x in parts if x is not None]
    if parts:
        return sum(parts)
    extra = [_int(v) for k, v in r.items() if k and ("vote" in k or k in ("absentee", "provisional", "early", "mail")) and k != "votes"]
    extra = [x for x in extra if x is not None]
    return sum(extra) if extra else None


def oe_contests(texts: list[str], date: str, stage: str, file_party: str, sp: int, src_url: str):
    y = int(date[:4])
    texts = [t.replace("\r\n", "\n").replace("\r", "\n") for t in texts]      # some files end their lines the old Mac way
    src = {"t": "OpenElections, Georgia: county results transcribed from the Georgia Secretary of State's certified returns", "u": src_url}
    G: dict = {}
    for text in texts:
        for r in csv.DictReader(io.StringIO(text)):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            office, cand = r.get("office") or "", r.get("candidate") or ""
            v = _oe_votes(r)
            if not office or not cand or v is None:
                continue
            if re.search(r"registered voters|ballots cast|turnout|straight party", office, re.I) or is_writein_bucket(cand):
                continue
            lvl, off, dist, title = ga_office(office, r.get("district") or "")
            county = county_name(r.get("county"))
            pc = party_code(r.get("party")) or file_party
            is_pri = "primary" in stage
            p = (pc if pc in ("D", "R") else "") if is_pri and lvl != "question" else ""
            if is_pri and lvl == "question" and pc in ("D", "R"):
                p = pc                                   # party ballot questions
            j = county if lvl == "local" else ""
            g = G.setdefault((lvl, off, dist, p, j), {"title": title, "c": {}, "cty": defaultdict(lambda: defaultdict(int))})
            inc = 1 if re.search(r"\(I\)|\(inc\.?\)|\(incumbent\)", cand, re.I) else 0
            nm = nice_name(re.sub(r"\((I|inc\.?|incumbent|[DRLI]|dem|rep|lib)\)", " ", cand, flags=re.I))
            k = nm.upper()
            row = g["c"].setdefault(k, [nm, "" if is_pri and lvl != "question" else (pc if lvl != "question" else ""), 0, inc, ""])
            row[2] += v
            row[3] = row[3] or inc
            if county:
                g["cty"][county][k] += v
    for (lvl, off, dist, p, j), g in sorted(G.items(), key=lambda kv: [str(x) for x in kv[0]]):
        keys = sorted(g["c"], key=lambda k: -g["c"][k][2])
        o = [g["c"][k] for k in keys]
        tot = sum(x[2] for x in o)
        c = {"scope": "ga", "y": y, "date": date, "stage": stage, "st": "GA", "office": off, "dist": dist, "lvl": lvl, "p": p, "j": j,
             "t": g["title"], "tot": tot, "o": o, "src": src, "basis": "compiled"}
        if len(g["cty"]) > 1 or (g["cty"] and not j):
            c["cty"] = {cn: [g["cty"][cn].get(k, 0) for k in keys] for cn in sorted(g["cty"])}
        if sp:
            c["sp"] = 1
        if lvl != "question":
            tag_outcomes(o, tot, majority=True)
        yield c


# ---------------------------------------------------------------- Georgia Secretary of State: archives, July 2012-2015
class SosZip:
    """The "Historical Elections Results" page's archives (precinct and summary data), for the elections before the
    state's present results system begins (January 2016)."""
    name = "sos_zip"
    INDEX = "https://sos.ga.gov/page/historical-elections-results"

    def discover(self, net):
        out, seen = [], set()
        for u, text in _links(net.text(self.INDEX), self.INDEX):
            if not re.search(r"\.zip$", urlsplit(u).path, re.I) or u in seen:
                continue
            seen.add(u)
            date = _date_in(text)
            if not date or int(date[:4]) >= 2016 or re.search(r"recount", text, re.I):
                continue
            title = re.sub(r"\s*\.zip.*$|\s*>+\s*$", "", text, flags=re.I).strip()
            out.append(Item(id="sos_zip:" + urlsplit(u).path.rsplit("/", 1)[-1].lower(), url=u, title=title,
                            meta={"date": date, "stage": _stage_of(title)}))
        return out

    def read(self, net, item, work):
        raw = net.bytes(item.url)
        docs, names = [], []
        _walk_zip(raw, docs, names, depth=0)
        (Path(work) / "inventory").mkdir(parents=True, exist_ok=True)
        (Path(work) / "inventory" / (re.sub(r"[^a-z0-9]+", "_", item.id.lower()) + ".json")).write_text(
            json.dumps({"url": item.url, "title": item.title, "files": names[:4000]}, indent=0), encoding="utf-8")
        n = 0
        for c in clarity_contests(docs, item.meta["date"], item.meta["stage"], item.title, item.url):
            n += 1
            yield c
        if not n:
            raise Skip(f"no results file this reader knows among {len(names)} files: " + ", ".join(names[:12]))


def _walk_zip(raw: bytes, docs: list, names: list, depth: int, prefix: str = "") -> None:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return
    for info in z.infolist():
        if info.is_dir() or info.file_size > 400_000_000:
            continue
        nm = prefix + info.filename
        names.append(f"{nm} ({info.file_size})")
        low = info.filename.lower()
        if low.endswith(".zip") and depth < 2:
            _walk_zip(z.read(info), docs, names, depth + 1, nm + "!")
        elif low.endswith(".xml"):
            docs.append((nm, z.read(info)))


def _clarity_party(text: str) -> tuple[str, str]:
    """A choice or contest label and the party written in it: "NATHAN DEAL (R)", "Governor - Rep", "REP - Governor"."""
    s = re.sub(r"\s+", " ", text or "").strip()
    m = re.search(r"\s*\((REP|DEM|LIB|IND|R|D|L|I|GRN|G)\)\s*$", s, re.I) or re.search(r"\s+-\s+(REP|DEM|LIB|IND|R|D|L|I)\.?$", s, re.I) \
        or re.search(r",\s+(REP|DEM|LIB|IND)\.?$", s, re.I)
    if m:
        return s[:m.start()].strip(), party_code(m.group(1))
    m = re.match(r"(REP|DEM|LIB|REPUBLICAN|DEMOCRATIC|DEMOCRAT)\s+-\s+", s, re.I)
    if m:
        return s[m.end():].strip(), party_code(m.group(1))
    return s, ""


def clarity_contests(docs, date: str, stage: str, title: str, url: str):
    """Clarity "detail.xml" files: the state's (votes by county) and, when the archive holds them, each county's
    (its own county and city contests, votes by precinct)."""
    from lxml import etree

    y = int(date[:4])
    is_pri = "primary" in stage
    src = {"t": f"Georgia Secretary of State, official results: {title}", "u": url}
    state_seen, county_contests, G = set(), [], {}
    parsed = []
    for nm, raw in docs:
        try:
            root = etree.fromstring(raw, parser=etree.XMLParser(recover=True, huge_tree=True))
        except etree.XMLSyntaxError:
            continue
        if root is None or root.tag != "ElectionResult":
            continue
        region = (root.findtext("Region") or "").strip()
        by_county = root.find(".//Contest/Choice/VoteType/County") is not None
        parsed.append((0 if by_county else 1, region, root))
    for _, region, root in sorted(parsed, key=lambda x: x[0]):
        statewide_file = root.find(".//Contest/Choice/VoteType/County") is not None
        cn_file = "" if statewide_file else county_name(region)
        for ct in root.iter("Contest"):
            raw_title = ct.get("text") or ""
            ctitle, cparty = _clarity_party(raw_title)
            lvl, off, dist, t = ga_office(ctitle)
            seats = _int(ct.get("voteFor")) or 1
            p = cparty if is_pri and cparty in ("D", "R") else ""
            if not statewide_file and lvl != "local" and (lvl, off, dist, p) in state_seen:
                continue                                 # the state's file already has this contest whole
            j = cn_file if (lvl == "local" and not statewide_file) else ""
            key = (lvl, off, dist, p, j)
            g = G.setdefault(key, {"title": t, "c": {}, "cty": defaultdict(lambda: defaultdict(int)), "seats": seats, "files": 0})
            g["files"] += 1
            for ch in ct.iter("Choice"):
                nm_raw, chp = _clarity_party(ch.get("text") or "")
                if not nm_raw:
                    continue
                pc = party_code(ch.get("party")) or chp
                inc = 1 if re.search(r"\(I\)", ch.get("text") or "") else 0
                name = nm_raw if lvl == "question" else nice_name(re.sub(r"\(I\)", " ", nm_raw))
                k = name.upper()
                row = g["c"].setdefault(k, [name, "" if (is_pri or lvl == "question") else pc, 0, inc, ""])
                tv = _int(ch.get("totalVotes")) or 0
                row[2] += tv
                if statewide_file:
                    for vt in ch.iter("VoteType"):
                        for co in vt.iter("County"):
                            g["cty"][county_name(co.get("name"))][k] += _int(co.get("votes")) or 0
                elif cn_file:
                    g["cty"][cn_file][k] += tv
            if statewide_file:
                state_seen.add((lvl, off, dist, p))
    for (lvl, off, dist, p, j), g in sorted(G.items(), key=lambda kv: [str(x) for x in kv[0]]):
        keys = sorted(g["c"], key=lambda k: -g["c"][k][2])
        o = [g["c"][k] for k in keys]
        if not o:
            continue
        tot = sum(x[2] for x in o)
        c = {"scope": "ga", "y": y, "date": date, "stage": stage, "st": "GA", "office": off, "dist": dist, "lvl": lvl, "p": p, "j": j,
             "t": g["title"], "tot": tot, "o": o, "src": src, "basis": "official"}
        if g["cty"] and not j:
            c["cty"] = {cn: [g["cty"][cn].get(k, 0) for k in keys] for cn in sorted(g["cty"]) if cn}
        if g["seats"] > 1:
            c["vf"] = g["seats"]
        if "special" in stage:
            c["sp"] = 1
        if lvl != "question":
            tag_outcomes(o, tot, majority=True, seats=g["seats"])
        yield c


# ---------------------------------------------------------------- documents kept as text for the readers that follow
class _Pdfs:
    """Downloads each document once and keeps its text (data/history/text/<source>/), page by page. The contests are
    read from that text by a reader written against the real pages; until then the progress file shows them on hand."""
    name = ""
    INDEX = ""

    def wanted(self, url: str, text: str):
        raise NotImplementedError

    def discover(self, net):
        out, seen = [], set()
        html, err = None, None
        for index in (self.INDEX,) + tuple(getattr(self, "ALSO", ())):      # the same list, kept on a second site
            try:
                html, self.INDEX = net.text(index), index
                break
            except Exception as exc:
                err = err or exc
        if html is None:
            raise err
        for u, text in _links(html, self.INDEX):
            w = self.wanted(u, text)
            if not w or u in seen:
                continue
            seen.add(u)
            out.append(Item(id=f"{self.name}:" + re.sub(r"[^a-z0-9]+", "_", (urlsplit(u).path.strip("/").rsplit("/", 1)[-1] or u).lower()),
                            url=u, title=text, meta=w))
        return out

    def read(self, net, item, work):
        raw = net.bytes(item.url)
        if raw[:4] != b"%PDF":
            html = raw.decode("utf-8", errors="replace")
            pdf = next((u for u, _ in _links(html, item.url) if urlsplit(u).path.lower().endswith(".pdf")), None)
            if not pdf:
                raise Skip("the page links no PDF")
            raw = net.bytes(pdf)
            if raw[:4] != b"%PDF":
                raise Skip("not a PDF")
        import pdfplumber
        d = Path(work) / "text" / self.name
        d.mkdir(parents=True, exist_ok=True)
        pages = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for pg in pdf.pages:
                try:
                    pages.append(pg.extract_text() or "")
                except Exception:
                    pages.append("")
                pg.flush_cache()
        chars = sum(len(p) for p in pages)
        (d / (item.id.split(":", 1)[1] + ".json")).write_text(json.dumps(
            {"url": item.url, "title": item.title, "meta": item.meta, "pages": pages}, ensure_ascii=False), encoding="utf-8")
        if chars < 200 * max(1, len(pages)) // 4:
            raise Skip(f"kept, but the pages are images ({len(pages)} pages, {chars} characters of text): needs OCR")
        raise Skip(f"kept as text: {len(pages)} pages, {chars} characters (the reader for these pages comes next)")
        yield  # pragma: no cover


class SosPdf(_Pdfs):
    """The Secretary of State's results documents for 1988 to mid-2012 (Georgia Election Results page)."""
    name = "sos_pdf"
    INDEX = "https://sos.ga.gov/page/georgia-election-results"

    def wanted(self, url, text):
        if not urlsplit(url).netloc.endswith("sos.ga.gov") or not urlsplit(url).path.lower().endswith(".pdf"):
            return None
        date = _date_in(text)
        m = re.search(r"((?:19|20)\d\d)", text + " " + urlsplit(url).path)
        if not m:
            return None
        return {"date": date or m.group(1), "stage": _stage_of(text)}


class Clerk(_Pdfs):
    """The Clerk of the House's "Statistics of the Congressional (and Presidential) Election": 1968-1974, the years
    before the MEDSL files begin."""
    name = "clerk"
    INDEX = "https://history.house.gov/Institution/Election-Statistics/Election-Statistics/"
    ALSO = ("https://clerk.house.gov/Members/ViewElectionInformation",)

    def wanted(self, url, text):
        m = re.search(r"/(19\d\d)election(?:\.pdf)?/?$", urlsplit(url).path) or re.search(r"^(19\d\d)(?: Election Statistics)?$", text.strip())
        if not m or not (FIRST_YEAR <= int(m.group(1)) <= 1974):
            return None
        return {"date": m.group(1), "stage": "general"}


SOURCES = {s.name: s for s in (Legislators(), Medsl(), OpenElections(), SosZip(), Fec(), SosPdf(), Clerk())}
