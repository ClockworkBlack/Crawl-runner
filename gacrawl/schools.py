"""`gacrawl schools`: every school in Georgia, who runs it, and how it is doing — from official sources only.

  schools seed    every public school (NCES Common Core of Data, 2024-25), private school (NCES Private School
                  Survey) and postsecondary institution (IPEDS) in Georgia with its address and coordinates, read
                  from NCES's EDGE open-data services (config/schools.yaml `nces`), plus every public school
                  district's outline, so an address can be placed in its district. Each school is tied to the
                  district in config/school_districts.yaml (the 180 county and city systems); LEAs that are not
                  one of those (state charter schools, state schools) are kept as their own districts.
                  -> state/schools.json, state/school_bounds.json, records kind `school` (source schools:nces)
  schools stats   GOSA's downloadable data (graduation rate, enrollment, attendance, dropouts, certified personnel,
                  retained students, HOPE eligibility, completers, financial efficiency, ACT) and GaDOE's CCRPI
                  files, newest year of each series, by district and school. The files are found on the directory
                  and CCRPI pages themselves (their names carry timestamps), never built from a pattern.
                  -> records kind `school_stat` (source schools:gosa / schools:ccrpi), one per series/year/place
  schools sites   school websites: links from each district's own "schools" pages whose text names a school in
                  the registry; then the school's home page and its administration/about/principal page.
                  -> records kind `page` (source schools:sites), tied to the school
  schools people  board members, superintendents and principals, read from the board pages, superintendent pages
                  and school pages already in the database (the district `:site` sources and schools:sites).
                  -> state/school_people.json: each person with role, seat, district, school and the page that
                  names them. load_candidates() folds them into the research candidates, so official-record
                  mentions, minutes votes (board roll calls from BoardDocs/Simbli minutes), news, statements and
                  profile supplements are built for them exactly as for everyone on a ballot; a person who is
                  also in Electo's archive (board members are elected) keeps that Electo key.
  schools export  (part of `gacrawl export`) dist/electo/schools.json, schools/<district>.json, school_bounds.json

Sourcing: every URL opened is a published resource or a link a page itself gave; evidence `primary` throughout
(government data services and the districts' and schools' own pages). Nothing is inferred about a person here:
names, roles and seats are read off the page that lists them, and that page is cited.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from .registry import write_json_atomic
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from . import extract
from .fetch import Blocked, Disallowed, Fetcher, OutOfTime
from .registry import load_yaml, school_districts
from .store import Record, Store

SRC_NCES, SRC_GOSA, SRC_CCRPI, SRC_SITES = "schools:nces", "schools:gosa", "schools:ccrpi", "schools:sites"
GRADES = ["PK", "KG", "G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08", "G09", "G10", "G11", "G12", "G13", "UG"]
NAME_RX = re.compile(r"(?:(?:Dr|Mr|Mrs|Ms|Rev|Hon)\.?[ \t]+)?([A-Z][A-Za-z'\-]+(?:[ \t]+(?:[A-Z]\.|[A-Z][A-Za-z'\-]+)){1,3}"
                     r"(?:,?[ \t]+(?:Jr|Sr|II|III|IV)\.?)?)")
ROLE_WORDS = r"(chair(?:man|woman|person)?|vice[- ]chair(?:man|woman|person)?|president|vice[- ]president|member|superintendent|principal)"
STOP_NAMES = re.compile(r"\b(board|education|school|district|county|city|meeting|agenda|minutes|policy|policies|office|"
                        r"contact|email|phone|home|about|news|calendar|welcome|read more|learn more|click here|"
                        r"skip to|menu|search|login|staff|directory|department|students|parents|community|our|the)\b", re.I)
HONORIFIC = re.compile(r"^(?:dr|mr|mrs|ms|rev|hon)\.?\s+", re.I)
SUFFIX = re.compile(r",?\s+(?:jr|sr|ii|iii|iv)\.?$", re.I)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_config(root: Path) -> dict:
    return load_yaml(root / "config" / "schools.yaml")


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def norm_name(s: str) -> str:
    """A person's name for matching: no honorific, suffix, middle initial or punctuation."""
    s = SUFFIX.sub("", HONORIFIC.sub("", (s or "").strip()))
    s = re.sub(r"\s+[A-Z]\.?(\s+)", r"\1", s)
    return " ".join(re.sub(r"[^a-z' \-]", "", s.lower()).split())


def school_key(s: str) -> str:
    """A school's name for matching across sources: no level words, no punctuation."""
    s = re.sub(r"\b(elementary|middle|high|primary|intermediate|junior|senior|school|academy|center|centre|of|the|"
               r"and|at|for|learning|magnet|charter|public|county|city)\b", " ", (s or "").lower())
    return _alnum(s)


# ----------------------------------------------------------------------------- districts: registry match
def district_matcher(root: Path):
    """lea_name -> district id in config/school_districts.yaml, by county or city name."""
    dists = school_districts(root)
    by_key: dict[str, str] = {}
    for d in dists:
        names = [d["name"]] + list(d.get("aliases") or [])
        core = _core(d["name"])
        if core:
            by_key.setdefault(core, d["id"])
        for n in names:
            c = _core(n)
            if c:
                by_key.setdefault(c, d["id"])
        if d.get("county") and not d["id"].startswith("sd-city-"):
            by_key.setdefault(_alnum(d["county"]) + "county", d["id"])
            by_key.setdefault(_alnum(d["county"]), d["id"])
        if d["id"].startswith("sd-city-"):
            by_key.setdefault(_alnum(d["id"].removeprefix("sd-city-")) + "city", d["id"])

    def match(lea_name: str) -> str | None:
        c = _core(lea_name)
        if c in by_key:
            return by_key[c]
        # "Atlanta Public Schools" -> atlanta; "Carrollton City" -> carrolltoncity; "Bibb County" -> bibbcounty
        for cand in (c + "city", c + "county", c.removesuffix("city"), c.removesuffix("county")):
            if cand in by_key:
                return by_key[cand]
        return None

    return match


def _core(name: str) -> str:
    n = re.sub(r"\b(school district|school system|public schools|schools|board of education|district|system|"
               r"independent|consolidated|the)\b", " ", (name or "").lower())
    return _alnum(n)


# ----------------------------------------------------------------------------- ArcGIS feature services
def arcgis_rows(fetcher: Fetcher, layer: str, where: str, out_fields: str = "*", geometry: bool = False,
                offset: float | None = None, page: int = 2000, log=print) -> Iterator[tuple[dict, dict | None, str]]:
    """Every feature of a layer matching `where`, paged the way the service pages (resultOffset); yields
    (attributes, geometry, the query URL that was opened)."""
    start = 0
    while True:
        params = {"where": where, "outFields": out_fields, "f": "json", "resultOffset": start, "resultRecordCount": page,
                  "returnGeometry": "true" if geometry else "false", "outSR": 4326}
        if geometry:
            params["geometryPrecision"] = 5
            if offset:
                params["maxAllowableOffset"] = offset
        r = fetcher.get(layer.rstrip("/") + "/query", params=params)
        if r.status != 200:
            raise RuntimeError(f"{layer}: HTTP {r.status}")
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"{layer}: {data['error']}")
        feats = data.get("features") or []
        for f in feats:
            yield f.get("attributes") or {}, f.get("geometry"), r.url
        if not data.get("exceededTransferLimit") or not feats:
            return
        start += len(feats)


def _int(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _float(v, signed: bool = False):
    """A non-negative number (NCES codes missing values as -1/-2/-9); `signed` for coordinates."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if signed or v >= 0 else None


def _level(a: dict) -> str:
    lv = (a.get("SCHOOL_LEVEL") or "").strip()
    if lv and lv.lower() not in ("not applicable", "not reported", "n/a", "-2", "-9"):
        return lv
    lo, hi = (a.get("GSLO") or ""), (a.get("GSHI") or "")
    order = {"PK": -1, "KG": 0, **{f"{i:02d}": i for i in range(1, 14)}}
    h = order.get(hi, None)
    if h is None:
        return ""
    return "High" if h >= 10 else "Middle" if h >= 7 else "Elementary" if h >= 1 else "Prekindergarten"


def public_school(a: dict, district_id: str) -> dict:
    grades = {g: _int(a.get(g)) for g in GRADES}
    return {"id": f"sch-{a.get('NCESSCH')}", "nces_id": a.get("NCESSCH"), "kind": "public", "name": (a.get("SCH_NAME") or "").strip(),
            "district_id": district_id, "lea_name": (a.get("LEA_NAME") or "").strip(), "leaid": a.get("LEAID"),
            "st_leaid": a.get("ST_LEAID"), "level": _level(a), "grades": f"{a.get('GSLO') or ''}-{a.get('GSHI') or ''}".strip("-"),
            "type": (a.get("SCHOOL_TYPE_TEXT") or "").strip(), "charter": (a.get("CHARTER_TEXT") or "").strip().lower() == "yes",
            "virtual": (a.get("VIRTUAL") or "").strip(), "status": (a.get("SY_STATUS_TEXT") or "").strip(),
            "address": " ".join(x for x in ((a.get("LSTREET1") or "").strip(), (a.get("LSTREET2") or "").strip()) if x),
            "city": (a.get("LCITY") or "").strip().title(), "zip": (a.get("LZIP") or "").strip(), "phone": (a.get("PHONE") or "").strip(),
            "county": (a.get("NMCNTY") or "").replace(" County", "").strip(), "locale": (a.get("ULOCALE") or "").strip(),
            "lat": _float(a.get("LATCOD"), True), "lon": _float(a.get("LONCOD"), True),
            "enrollment": _int(a.get("MEMBER")) if _int(a.get("MEMBER")) is not None else _int(a.get("TOTAL")),
            "teachers": _float(a.get("FTE")), "ratio": _float(a.get("STUTERATIO")), "frl": _int(a.get("TOTFRL")),
            "grade_counts": {k: v for k, v in grades.items() if v},
            "year": (a.get("SURVYEAR") or "").strip()}


def private_school(a: dict) -> dict:
    return {"id": f"pss-{a.get('PPIN')}", "nces_id": a.get("PPIN"), "kind": "private", "name": (a.get("NAME") or "").strip().title(),
            "district_id": "", "level": "", "address": (a.get("STREET") or "").strip().title(), "city": (a.get("CITY") or "").strip().title(),
            "zip": (a.get("ZIP") or "").strip(), "county": (a.get("NMCNTY") or "").replace(" County", "").strip(),
            "locale": (a.get("LOCALE") or "").strip(), "lat": _float(a.get("LAT"), True), "lon": _float(a.get("LON"), True),
            "sd": (a.get("SLDU") or "").lstrip("0"), "hd": (a.get("SLDL") or "").lstrip("0"), "year": (a.get("SCHOOLYEAR") or "").strip()}


def college(a: dict) -> dict:
    return {"id": f"ipeds-{a.get('UNITID')}", "nces_id": a.get("UNITID"), "kind": "college", "name": (a.get("NAME") or "").strip(),
            "district_id": "", "level": "Postsecondary", "address": (a.get("STREET") or "").strip(), "city": (a.get("CITY") or "").strip().title(),
            "zip": (a.get("ZIP") or "").strip(), "county": (a.get("NMCNTY") or "").replace(" County", "").strip(),
            "locale": (a.get("LOCALE") or "").strip(), "lat": _float(a.get("LAT"), True), "lon": _float(a.get("LON"), True),
            "sd": (a.get("SLDU") or "").lstrip("0"), "hd": (a.get("SLDL") or "").lstrip("0"), "year": (a.get("SCHOOLYEAR") or "").strip()}


def school_record(s: dict, url: str) -> Record:
    line = ", ".join(x for x in (s["name"], s.get("level") or "", s.get("address") or "", s.get("city") or "", s.get("zip") or "",
                                 (s.get("county") + " County") if s.get("county") else "") if x)
    return Record(SRC_NCES, "school", url, title=s["name"], text=line, channel="api", evidence="primary",
                  jurisdiction=s.get("district_id") or "", key=f"{SRC_NCES}|school|{s['id']}",
                  meta={"school_id": s["id"], **{k: v for k, v in s.items() if k not in ("name", "id")}, "opened": True})


def _ring_simplify(ring: list, keep: int = 400) -> list:
    """At most `keep` points per ring (the service already simplified to the configured offset)."""
    if len(ring) <= keep:
        return ring
    step = len(ring) / keep
    out = [ring[int(i * step)] for i in range(keep)]
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def seed(fetcher: Fetcher, root: Path, store: Store, cfg: dict | None = None, log=print, deadline: float | None = None) -> dict:
    """Schools, colleges and district outlines from NCES EDGE; writes state/schools.json + state/school_bounds.json."""
    import time

    conf = cfg or load_config(root)
    n = conf["nces"]
    st, fips = n.get("state", "GA"), str(n.get("state_fips", "13"))
    match = district_matcher(root)
    dists = {d["id"]: d for d in school_districts(root)}
    out = {"as_of": _now(), "districts": {}, "schools": [], "unmatched_leas": {}}
    bounds: dict[str, list] = {}

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    # public school districts (LEA directory + outlines)
    where = f"LSTATE='{st}'"
    for a, g, url in arcgis_rows(fetcher, n["districts"], where, geometry=True, offset=n.get("boundary_offset"), log=log):
        lea = (a.get("LEA_NAME") or "").strip()
        did = match(lea) or f"lea-{slug(lea)}"
        d = {"id": did, "leaid": a.get("LEAID"), "st_leaid": a.get("ST_LEAID"), "lea_name": lea,
             "name": dists.get(did, {}).get("name") or lea, "kind": "school_district" if did in dists else "other_lea",
             "type": (a.get("LEA_TYPE_TEXT") or "").strip(), "status": (a.get("SY_STATUS_TEXT") or "").strip(),
             "address": " ".join(x for x in ((a.get("LSTREET1") or "").strip(), (a.get("LSTREET2") or "").strip()) if x),
             "city": (a.get("LCITY") or "").strip().title(), "zip": (a.get("LZIP") or "").strip(), "phone": (a.get("PHONE") or "").strip(),
             "county": (a.get("CONAME") or "").replace(" County", "").strip() or dists.get(did, {}).get("county", ""),
             "grades": f"{a.get('GSLO') or ''}-{a.get('GSHI') or ''}".strip("-"), "schools_operational": _int(a.get("SCH")),
             "enrollment": _int(a.get("MEMBER")), "teachers": _float(a.get("TOTTCH")), "ratio": _float(a.get("STUTERATIO")),
             "locale": (a.get("LOCALE_TEXT") or "").strip(), "lat": _float(a.get("Lat"), True), "lon": _float(a.get("Long"), True),
             "year": (a.get("SURVYEAR") or "").strip(), "url": url}
        if did in out["districts"]:          # an LEA the matcher folded onto a district already seen: keep the first, note it
            out["unmatched_leas"][lea] = did
            continue
        out["districts"][did] = d
        if g and g.get("rings"):
            bounds[did] = [_ring_simplify([[round(x, 5), round(y, 5)] for x, y in r]) for r in g["rings"]]
        if did not in dists:
            out["unmatched_leas"][lea] = did
        if timed_out():
            break
    log(f"districts: {len(out['districts'])} ({sum(1 for d in out['districts'].values() if d['kind'] == 'school_district')} in the registry, "
        f"{len(bounds)} with outlines)")

    # public schools
    n_rec = 0
    for a, _, url in arcgis_rows(fetcher, n["public_schools"], where, log=log):
        lea = (a.get("LEA_NAME") or "").strip()
        did = match(lea) or f"lea-{slug(lea)}"
        s = public_school(a, did)
        if not s["name"]:
            continue
        out["schools"].append(s)
        n_rec += store.add(school_record(s, url))
        if timed_out():
            break
    log(f"public schools: {sum(1 for s in out['schools'] if s['kind'] == 'public')}")
    # private schools and colleges (county from the service; district by point-in-outline later, on the site)
    for key, make, where2 in (("private_schools", private_school, f"STATE='{st}'"), ("postsecondary", college, f"STATE='{st}'")):
        if timed_out():
            break
        try:
            for a, _, url in arcgis_rows(fetcher, n[key], where2, log=log):
                s = make(a)
                if not s["name"]:
                    continue
                s["district_id"] = _district_at(bounds, s.get("lat"), s.get("lon")) or ""
                out["schools"].append(s)
                n_rec += store.add(school_record(s, url))
        except (Blocked, Disallowed, RuntimeError) as exc:
            log(f"{key}: {exc}")
    for d in out["districts"].values():
        d["schools"] = sum(1 for s in out["schools"] if s["district_id"] == d["id"] and s["kind"] == "public")
    log(f"private schools: {sum(1 for s in out['schools'] if s['kind'] == 'private')}, colleges: "
        f"{sum(1 for s in out['schools'] if s['kind'] == 'college')}; {n_rec} records added or changed")
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    write_json_atomic((state / "schools.json"), out, ensure_ascii=False, indent=0)
    (state / "school_bounds.json").write_text(json.dumps({"as_of": out["as_of"], "crs": "EPSG:4326 lon,lat", "rings": bounds},
                                                         ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    store.flush()
    return {"districts": len(out["districts"]), "schools": len(out["schools"]), "outlines": len(bounds), "records": n_rec}


def _in_ring(ring: list, x: float, y: float) -> bool:
    ins = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            ins = not ins
        j = i
    return ins


def _district_at(bounds: dict[str, list], lat, lon) -> str | None:
    if lat is None or lon is None:
        return None
    hit = None
    for did, rings in bounds.items():
        inside = sum(1 for r in rings if _in_ring(r, lon, lat))
        if inside % 2 == 1:
            if did.startswith("sd-city-"):      # a city system sits inside its county's outline: the city wins
                return did
            hit = hit or did
    return hit


def load_registry(root: Path) -> dict:
    p = root / "state" / "schools.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"districts": {}, "schools": []}


# ----------------------------------------------------------------------------- statistics (GOSA, CCRPI)
YEAR_RX = re.compile(r"(20\d{2})")


def _year_of(url: str) -> int:
    ys = [int(y) for y in YEAR_RX.findall(urlsplit(url).path)]
    return max(ys) if ys else 0


def pick_series_files(index_html: str, base_url: str, series: dict) -> dict[str, str]:
    """For each configured series, the newest file the directory page links to whose name carries the series stem."""
    out = {}
    for u in extract.links(index_html, base_url):
        name = urlsplit(u).path.rsplit("/", 1)[-1]
        for sid, s in series.items():
            if s["match"].lower() in name.lower() and re.search(r"\.(csv|xlsx?)$", name, re.I):
                if sid not in out or _year_of(u) > _year_of(out[sid]):
                    out[sid] = u
    return out


def _col(headers: list[str], *frags: str) -> int | None:
    for frag in frags:
        for i, h in enumerate(headers):
            if frag.lower() in h.lower():
                return i
    return None


GOSA_PARSER = "2"    # stored with each file read; a newer parser reads the files again

# the place columns, by their exact names in GOSA's files (which spell them several ways: the HOPE file has
# SCHOOL_DISTRCT_CD, the retention file School_District_Code)
_PLACE = {"year": ("LONG_SCHOOL_YEAR", "SCHOOL_YEAR"),
          "dc": ("SCHOOL_DSTRCT_CD", "SCHOOL_DISTRCT_CD", "SCHOOL_DISTRICT_CODE", "SYSTEM_ID", "DISTRICT_ID", "DSTRCT_CD"),
          "dn": ("SCHOOL_DSTRCT_NM", "SCHOOL_DISTRICT_NAME", "SYSTEM_NAME", "DISTRICT_NAME", "DSTRCT_NM"),
          "sc": ("INSTN_NUMBER", "SCHOOL_CODE", "SCHOOL_ID", "INSTN_NUM", "SCHOOL_CD"),
          "sn": ("INSTN_NAME", "SCHOOL_NAME", "INSTN_NM"),
          "lvl": ("DETAIL_LVL_DESC", "DATA_REPORTING_LEVEL", "DETAIL_LVL")}


def _colx(H: list[str], *names: str) -> int | None:
    """A column by its exact name (case aside). Substrings would mislead: "GRADE" is in GRADES_SERVED_DESC."""
    want = [n.upper() for n in names if n]
    for n in want:
        if n in H:
            return H.index(n)
    return None


def parse_gosa_csv(text: str, series_id: str, s: dict) -> list[dict]:
    """GOSA CSVs: one row per place x subgroup (x grade, x category). The series' rules say which rows count:
      value        the column holding the number
      where        {column: value or [values]}: only rows with exactly these values (e.g. LABEL_LVL_1_DESC
                   "Grad Rate -ALL Students"; ENROLLMENT_PERIOD "Fall")
      sum          add the matching rows up per place (enrollment by grade, diplomas by type)
      ratio        [numerator, denominator] columns: the value is their percentage (students retained)
      level_value  {school|district|state: column}: the value column depends on the row's level (ACT averages)
    Without `where`, rows of a subgroup other than all students, or of one grade, are left out (files that carry
    SUBGRP_DESC / GRADE_LEVEL columns with "All Students" / "All Grades" rows)."""
    rd = csv.reader(io.StringIO(text))
    try:
        headers = [h.strip().lstrip("\ufeff").lstrip("#").strip() for h in next(rd)]
    except StopIteration:
        return []
    H = [h.upper() for h in headers]
    c = {k: _colx(H, *v) for k, v in _PLACE.items()}
    if c["dc"] is None and c["dn"] is None:
        return []
    where = {(_colx(H, k)): (v if isinstance(v, list) else [v]) for k, v in (s.get("where") or {}).items()}
    if None in where:
        return []                         # a rule names a column this file does not have: not this series' file
    where = {i: {str(x).strip().lower() for x in vals} for i, vals in where.items()}
    ratio = [_colx(H, x) for x in (s.get("ratio") or [])]
    if s.get("ratio") and None in ratio:
        return []
    lv = {k: _colx(H, v) for k, v in (s.get("level_value") or {}).items()}
    c_val = _colx(H, s["value"]) if s.get("value") else None
    if c_val is None and not ratio and not lv:
        c_val = next((i for i, h in enumerate(H) if re.search(r"PERCENT|_PCT|COUNT|SCORE|RATE", h) and "YEAR" not in h), None)
        if c_val is None:
            return []
    c_sub = _colx(H, "SUBGRP_DESC", "SUBGROUP") if not where else None
    c_grade = _colx(H, "GRADE_LEVEL", "GRADE") if not where and not s.get("sum") else None

    def cell(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""

    places: dict[tuple, dict] = {}
    for r in rd:
        if not r:
            continue
        if any(cell(r, i).lower() not in ok for i, ok in where.items()):
            continue
        sub = cell(r, c_sub)
        if sub and not re.search(r"^all students$|^all$|^total$", sub, re.I):
            continue
        grade = cell(r, c_grade)
        if grade and not re.search(r"^(all|total|k-?12|all grades)", grade, re.I):
            continue
        lvl = cell(r, c["lvl"]).lower()
        dc = cell(r, c["dc"]).lstrip("0") or cell(r, c["dc"])
        sc, sn, dn, year = cell(r, c["sc"]), cell(r, c["sn"]), cell(r, c["dn"]), cell(r, c["year"])
        is_school = bool(sc and sc.upper() not in ("ALL", "0", "000") and sn and (lvl in ("", "school") or "school" in lvl))
        if lvl == "state":
            dc = "ALL"
        level = "school" if is_school else ("state" if dc.upper() == "ALL" else "district")
        if ratio:
            num, den = _num(cell(r, ratio[0])), _num(cell(r, ratio[1]))
            v = round(100.0 * num / den, 2) if num is not None and den else None
        elif lv:
            v = _num(cell(r, lv.get(level)))
        else:
            v = _num(cell(r, c_val))
        if v is None:
            continue
        key = (dc, sc if is_school else "", year)
        if key in places:
            if s.get("sum"):
                places[key]["value"] = round(places[key]["value"] + v, 2)
                places[key]["rows"] += 1
            continue                      # one value per place: the first matching row
        places[key] = {"series": series_id, "year": year, "district_code": dc, "district_name": dn,
                       "school_code": sc if is_school else "", "school_name": sn if is_school else "", "value": v,
                       "level": level, "rows": 1}
    out = list(places.values())
    for row in out:
        if not s.get("sum"):
            row.pop("rows", None)
    return out


def _num(v) -> float | None:
    v = str(v or "").strip().replace(",", "").replace("%", "")
    if not v or v.upper() in ("TFS", "N/A", "NA", "-", "--", "TBD", "*"):
        return None
    try:
        return round(float(v), 2)
    except ValueError:
        return None


def parse_xlsx_rows(content: bytes) -> list[list[str]]:
    try:
        import openpyxl
    except ImportError:
        return []
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets[:3]:
        for row in ws.iter_rows(values_only=True):
            out.append(["" if v is None else str(v) for v in row])
    return out


def parse_fesr(rows: list[list[str]], series_id: str) -> list[dict]:
    """GOSA's Financial Efficiency Star Rating workbooks (District_/School_FESR_FY25_for_Display.xlsx): a header row
    with system_id (and school_id), per-year per-pupil expenditure columns (District_PPE_25, Sch_PPE_25...), the
    three-year average (PPE_Avg) and the star rating (FESR). The value is the rating; the newest year's per-pupil
    expenditure and the three-year average come with it."""
    out, H = [], None
    for r in rows:
        cells = [str(c or "").strip() for c in r]
        if H is None:
            up = [c.upper() for c in cells]
            if "SYSTEM_ID" in up and "FESR" in up:
                H = up
                c_dc, c_dn = _colx(H, "SYSTEM_ID"), _colx(H, "SYSTEMNAME", "SYSTEM_NAME")
                c_sc, c_sn = _colx(H, "SCHOOL_ID"), _colx(H, "SCHNAME", "SCHOOL_NAME")
                c_star, c_avg = _colx(H, "FESR"), _colx(H, "PPE_AVG")
                years = sorted(((int(m.group(2)), i) for i, h in enumerate(H)
                                if (m := re.fullmatch(r"(DISTRICT|SCH)_PPE_(\d\d)", h))), reverse=True)
                c_ppe, yy = (years[0][1], years[0][0]) if years else (None, None)
            continue
        star = _num(cells[c_star]) if c_star is not None and c_star < len(cells) else None
        ppe = _num(cells[c_ppe]) if c_ppe is not None and c_ppe < len(cells) else None
        if star is None and ppe is None:
            continue
        dc = cells[c_dc].split(".")[0].lstrip("0") if c_dc is not None and c_dc < len(cells) else ""
        if not dc:
            continue
        sc = cells[c_sc].split(".")[0] if c_sc is not None and c_sc < len(cells) else ""
        sn = cells[c_sn] if c_sn is not None and c_sn < len(cells) else ""
        out.append({"series": series_id, "year": f"FY{yy}" if yy else "", "district_code": dc,
                    "district_name": cells[c_dn] if c_dn is not None and c_dn < len(cells) else "",
                    "school_code": sc, "school_name": sn, "value": star, "per_pupil": ppe,
                    "per_pupil_3yr_avg": _num(cells[c_avg]) if c_avg is not None and c_avg < len(cells) else None,
                    "level": "school" if sc and sn else "district"})
    return out


def parse_ccrpi(rows: list[list[str]], year: str) -> list[dict]:
    """CCRPI score files: system and school ids/names with the single score (and components when present)."""
    out, H = [], None
    for r in rows:
        up = [c.upper().strip() for c in r]
        if H is None:
            if any(c in ("SYSTEM_ID", "SYSTEM ID", "SYSTEMID", "SYSTEM_NAME", "SYSTEM NAME") for c in up) and \
                    any("SINGLE" in c or "CCRPI" in c or "SCORE" in c for c in up):
                H = up
            continue
        c_dc, c_dn = _col(H, "SYSTEM_ID", "SYSTEM ID", "SYSTEMID"), _col(H, "SYSTEM_NAME", "SYSTEM NAME")
        c_sc, c_sn = _col(H, "SCHOOL_ID", "SCHOOL ID", "SCHOOLID"), _col(H, "SCHOOL_NAME", "SCHOOL NAME")
        c_score = _col(H, "SINGLE SCORE", "SINGLE_SCORE", "CCRPI SCORE", "CCRPI_SCORE", "SCORE")
        c_year = _col(H, "YEAR")
        if c_score is None:
            return out
        v = _num(r[c_score]) if c_score < len(r) else None
        if v is None:
            continue
        sc = (r[c_sc] if c_sc is not None and c_sc < len(r) else "").strip().split(".")[0]
        sn = (r[c_sn] if c_sn is not None and c_sn < len(r) else "").strip()
        comps = {H[i].lower(): _num(r[i]) for i in range(min(len(H), len(r)))
                 if re.search(r"CONTENT MASTERY|PROGRESS|CLOSING GAPS|READINESS|GRADUATION", H[i]) and _num(r[i]) is not None}
        out.append({"series": "ccrpi", "year": (r[c_year] if c_year is not None and c_year < len(r) else "").strip() or year,
                    "district_code": (r[c_dc] if c_dc is not None and c_dc < len(r) else "").strip().lstrip("0").split(".")[0],
                    "district_name": (r[c_dn] if c_dn is not None and c_dn < len(r) else "").strip(),
                    "school_code": sc if sc and sc not in ("ALL", "0") else "", "school_name": sn if sc and sc not in ("ALL", "0") else "",
                    "value": v, "components": comps, "level": "school" if sc and sc not in ("ALL", "0") else "district"})
    return out


def stat_record(source: str, url: str, row: dict, label: str) -> Record:
    place = row["school_name"] or row["district_name"] or ("Georgia" if row["district_code"] == "ALL" else "")
    title = f"{label} {row['year']}: {place}".strip()
    return Record(source, "school_stat", url, title=title, text=f"{title}: {row['value']}", channel="api", evidence="primary",
                  jurisdiction="", key=f"{source}|school_stat|{row['series']}|{row['year']}|{row['district_code']}|{row['school_code']}",
                  meta={**row, "label": label, "opened": True})


def stats(fetcher: Fetcher, root: Path, store: Store, cfg: dict | None = None, log=print, deadline: float | None = None,
          refresh: bool = False) -> dict:
    import time

    conf = cfg or load_config(root)
    g = conf["gosa"]
    out = {"files": 0, "rows": 0, "skipped": 0, "errors": []}

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    try:
        r = fetcher.get(g["downloads"])
        files = pick_series_files(r.text, r.url, g["series"]) if r.status == 200 else {}
    except (Blocked, Disallowed) as exc:
        files, _ = {}, out["errors"].append(f"gosa index: {exc}")
    for sid, url in files.items():
        if timed_out():
            break
        s = g["series"][sid]
        if not refresh and store.kv_get(f"schools:gosa:{sid}") == f"{url}#p{GOSA_PARSER}":
            out["skipped"] += 1
            continue
        try:
            fr = fetcher.get(url)
        except (Blocked, Disallowed) as exc:
            out["errors"].append(f"{sid}: {exc}")
            continue
        except OutOfTime:
            break
        except Exception as exc:  # noqa: BLE001 — one file the server won't hand over (too large, dropped) is skipped
            out["errors"].append(f"{sid}: {type(exc).__name__}: {exc}"[:200])
            continue
        if fr.status != 200 or len(fr.content) > int(g.get("max_file_bytes") or 60_000_000):
            out["errors"].append(f"{sid}: HTTP {fr.status} ({len(fr.content)} bytes)")
            continue
        if s.get("xlsx") or url.lower().endswith((".xlsx", ".xls")):
            parts = [(sid, s.get("label", sid), parse_fesr(parse_xlsx_rows(fr.content), sid))]
        else:
            text = fr.content.decode("utf-8", "replace")
            parts = [(o["id"], o.get("label", o["id"]), parse_gosa_csv(text, o["id"], {**s, **o}))
                     for o in (s.get("outputs") or [{"id": sid, "label": s.get("label", sid)}])]
        n_rows = 0
        for oid, label, rows in parts:
            n = sum(store.add(stat_record(SRC_GOSA, fr.url, row, label)) for row in rows)
            n_rows += len(rows)
            log(f"gosa {oid}: {len(rows)} rows ({n} new or changed) from {url.rsplit('/', 1)[-1]}")
            if not rows:
                out["errors"].append(f"{oid}: no rows matched in {url.rsplit('/', 1)[-1]} (the file's layout may have changed)")
        if n_rows:
            store.kv_set(f"schools:gosa:{sid}", f"{url}#p{GOSA_PARSER}")   # read: skipped until the file or parser changes
        out["files"] += 1
        out["rows"] += n_rows
    # CCRPI files linked from GaDOE's page
    c = conf.get("ccrpi") or {}
    if c.get("page") and not timed_out():
        try:
            r = fetcher.get(c["page"])
            if r.status == 200:
                soup = extract.soup_of(r.text)
                for a in soup.find_all("a", href=True):
                    u = urljoin(r.url, a["href"]).split("#")[0]
                    hay = f"{a.get_text(' ', strip=True)} {urlsplit(u).path.rsplit('/', 1)[-1]}".lower()
                    if not re.search(r"\.(xlsx?|csv)$", urlsplit(u).path, re.I) or not any(w in hay for w in c.get("link_words") or ["ccrpi"]):
                        continue
                    if not refresh and store.kv_get(f"schools:ccrpi:{u}"):
                        out["skipped"] += 1
                        continue
                    if timed_out():
                        break
                    try:
                        fr = fetcher.get(u)
                    except (Blocked, Disallowed) as exc:
                        out["errors"].append(f"ccrpi: {exc}")
                        continue
                    except OutOfTime:
                        break
                    except Exception as exc:  # noqa: BLE001 — one file that can't be read is skipped
                        out["errors"].append(f"ccrpi: {type(exc).__name__}: {exc}"[:200])
                        continue
                    if fr.status != 200:
                        continue
                    year = str(_year_of(u) or "")
                    if u.lower().endswith(".csv"):
                        rows = parse_ccrpi([row for row in csv.reader(io.StringIO(fr.content.decode("utf-8", "replace")))], year)
                    else:
                        rows = parse_ccrpi(parse_xlsx_rows(fr.content), year)
                    n = sum(store.add(stat_record(SRC_CCRPI, fr.url, row, "CCRPI single score")) for row in rows)
                    store.kv_set(f"schools:ccrpi:{u}", _now())
                    out["files"] += 1
                    out["rows"] += len(rows)
                    log(f"ccrpi: {len(rows)} rows ({n} new or changed) from {u.rsplit('/', 1)[-1]}")
        except (Blocked, Disallowed) as exc:
            out["errors"].append(f"ccrpi page: {exc}")
        except OutOfTime:
            pass
    store.flush()
    return out


# ----------------------------------------------------------------------------- school websites
SCHOOLS_PAGE_RX = re.compile(r"\b(our schools|schools|find a school|school directory|school list|elementary schools|"
                             r"middle schools|high schools|campuses)\b", re.I)
PRINCIPAL_LINK_RX = re.compile(r"\b(principal|administration|administrators|about(?: us| our school)?|our school|staff|"
                               r"leadership|welcome|faculty|office)\b", re.I)


MD_LINK_RX = re.compile(r"\[([^\]]{2,120})\]\((https?://[^)\s]+)\)")


def school_site_links(doc: str, base_url: str, schools: list[dict]) -> dict[str, str]:
    """school id -> website, for links on a district page (HTML, or the stored Markdown of it) whose text names one
    of the district's schools."""
    want = {school_key(s["name"]): s["id"] for s in schools if school_key(s["name"])}
    host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    found: dict[str, str] = {}
    if "<a " in doc or "<A " in doc:
        pairs = [(a.get_text(" ", strip=True), a["href"]) for a in extract.soup_of(doc).find_all("a", href=True)]
    else:
        pairs = [(m.group(1), m.group(2)) for m in MD_LINK_RX.finditer(doc)]
    for text, href in pairs:
        k = school_key(text)
        if not k or k not in want:
            continue
        u = urljoin(base_url, href).split("#")[0]
        if not u.startswith("http"):
            continue
        lh = urlsplit(u).netloc.lower().removeprefix("www.")
        # a school's own site (another host or a sub-site path), not a document or a listing on the same page
        if lh == host and (u.rstrip("/") == base_url.rstrip("/") or re.search(r"\.(pdf|docx?)$", u, re.I)):
            continue
        found.setdefault(want[k], u)
    return found


MODIFIER_RX = re.compile(r"(assistant|asst\.?|vice|associate|deputy|former|interim|co-|retired)\s*$", re.I)


def _titled(text: str, title: str, skip_before: re.Pattern = MODIFIER_RX) -> str | None:
    """The person a page gives a title to: "Title: Dr. Jane Doe", "Jane Doe, Title", "Jane Doe\\nTitle", "Title Jane Doe".
    A modifier right before the title (assistant, vice, deputy, former...) disqualifies the match."""
    t = re.sub(r"[ \t]+", " ", text or "")
    pats = [r"\b" + title + r"\s*[:\-–—]\s*" + NAME_RX.pattern,
            NAME_RX.pattern + r"\s*[,\-–—|]\s*(?:ed\.?d\.?,?\s*)?(?:interim\s+)?" + title + r"\b",
            NAME_RX.pattern + r"[ \t]*\n[ \t]*(?:interim\s+)?" + title + r"\b",
            r"\b" + title + r"(?: of schools)?(?: is)?\s+" + NAME_RX.pattern]
    for p in pats:
        for m in re.finditer(p, t, re.I):
            name = m.group(1).strip()
            tpos = m.start() if p.startswith("\\b") else t.lower().find(title, m.start(1) + len(name) - 1)
            before = t[max(0, tpos - 14):tpos]
            if _plausible_name(name) and not skip_before.search(before):
                return name
    return None


def principal_from_text(text: str) -> tuple[str, str] | None:
    """('Jane Doe', 'Principal') from a school page; assistant and vice principals are skipped."""
    nm = _titled(text, "principal")
    return (nm, "Principal") if nm else None


def _plausible_name(name: str) -> bool:
    w = name.split()
    return 2 <= len(w) <= 4 and not STOP_NAMES.search(name) and all(len(x) <= 20 for x in w) and name[0].isupper()


def sites(fetcher: Fetcher, root: Path, store: Store, cfg: dict | None = None, log=print, deadline: float | None = None) -> dict:
    """School websites from the districts' own school lists, then each school's home and administration pages."""
    import time

    conf = cfg or load_config(root)
    pc = conf.get("people") or {}
    reg = load_registry(root)
    by_dist: dict[str, list[dict]] = defaultdict(list)
    for s in reg["schools"]:
        if s["kind"] == "public":
            by_dist[s["district_id"]].append(s)
    sites_known = json.loads(store.kv_get("schools:sites") or "{}")
    out = {"districts_scanned": 0, "school_sites": len(sites_known), "pages": 0, "principals": 0}

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    # 1. the districts' "schools" pages already crawled (source <sd>:site) -> school websites
    for did, schools in by_dist.items():
        if timed_out():
            break
        rows = store.query("source_id=? AND kind='page'", (f"{did}:site",), limit=400)
        pages = [r for r in rows if SCHOOLS_PAGE_RX.search(f"{r.get('title') or ''} {urlsplit(r.get('url') or '').path}")]
        if not pages:
            continue
        out["districts_scanned"] += 1
        for r in pages[:6]:
            doc = r.get("markdown") or r.get("text") or ""   # the stored Markdown keeps the page's links
            if not doc:
                continue
            for sid, u in school_site_links(doc, r["url"], schools).items():
                if sid not in sites_known:
                    sites_known[sid] = {"url": u, "found_on": r["url"], "found_at": _now()}
    store.kv_set("schools:sites", json.dumps(sites_known))
    out["school_sites"] = len(sites_known)
    # 2. each school's home page and its principal/administration page(s), a bounded number per run
    todo = [(sid, v) for sid, v in sites_known.items() if not v.get("read_at")]
    todo.sort(key=lambda x: x[0])
    sch_by_id = {s["id"]: s for s in reg["schools"]}
    max_sites, per_site = int(pc.get("max_school_sites_per_run") or 400), int(pc.get("school_site_pages") or 3)
    for sid, v in todo[:max_sites]:
        if timed_out():
            break
        s = sch_by_id.get(sid)
        if not s:
            continue
        try:
            fr = fetcher.get(v["url"])
        except (Blocked, Disallowed) as exc:
            v["read_at"], v["error"] = _now(), str(exc)[:120]
            continue
        except Exception as exc:  # noqa: BLE001
            v["read_at"], v["error"] = _now(), str(exc)[:120]
            continue
        v["read_at"] = _now()
        if fr.status != 200 or "html" not in fr.content_type:
            v["error"] = f"HTTP {fr.status}"
            continue
        pages = [(fr.url, fr.text)]
        soup = extract.soup_of(fr.text)
        host = urlsplit(fr.url).netloc
        more = []
        for a in soup.find_all("a", href=True):
            u = urljoin(fr.url, a["href"]).split("#")[0]
            if urlsplit(u).netloc != host or re.search(r"\.(pdf|docx?|jpe?g|png)$", u, re.I):
                continue
            if PRINCIPAL_LINK_RX.search(f"{a.get_text(' ', strip=True)} {urlsplit(u).path}") and u not in more and u != fr.url:
                more.append(u)
        for u in more[:per_site - 1]:
            try:
                pr = fetcher.get(u)
                if pr.status == 200 and "html" in pr.content_type:
                    pages.append((pr.url, pr.text))
            except (Blocked, Disallowed, Exception):  # noqa: BLE001
                break
        for u, html in pages:
            title, md = extract.html_to_markdown(html, u)
            if not md:
                continue
            principal = principal_from_text(md)
            store.add(Record(SRC_SITES, "page", u, title=title or s["name"], text=md, markdown=md, channel="html", evidence="primary",
                             jurisdiction=s["district_id"], key=f"{SRC_SITES}|page|{u}|{sid}",
                             meta={"school_id": sid, "school_name": s["name"], "opened": True, "found_on": v.get("found_on"),
                                   "principal": principal[0] if principal else ""}))
            out["pages"] += 1
            if principal:
                out["principals"] += 1
                v["principal"], v["principal_url"] = principal[0], u
                break
    store.kv_set("schools:sites", json.dumps(sites_known))
    store.flush()
    return out


# ----------------------------------------------------------------------------- people (board, superintendent, principals)
SEAT_RX_DEFAULT = r"(?:district|post|seat|area|ward)\s*(\d+|[A-Z]\b)"


def board_members_from_text(text: str, seat_rx: str = SEAT_RX_DEFAULT) -> list[dict]:
    """Names with roles from a board page's text. Looks at each line (and the line after it) for a name and a role
    or seat; a page that names fewer than two people with roles is not treated as a board page."""
    lines = [ln.strip(" \t#*-•|") for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    seat = re.compile(seat_rx, re.I)
    role_rx = re.compile(r"\b" + ROLE_WORDS + r"\b", re.I)
    out, seen = [], set()
    for i, ln in enumerate(lines):
        if len(ln) > 160:
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        prev = lines[i - 1] if i > 0 else ""
        hay = f"{ln} | {nxt}"
        rm, sm = role_rx.search(hay), seat.search(hay)
        if not rm and not sm:
            continue
        cand = None
        for m in NAME_RX.finditer(ln):
            nm = m.group(1).strip()
            if _plausible_name(nm) and not role_rx.search(nm):
                cand = nm
                break
        if not cand and (rm and rm.start() < len(ln) or sm and sm.start() < len(ln)):   # role on this line, name just above
            for m in NAME_RX.finditer(prev):
                nm = m.group(1).strip()
                if _plausible_name(nm) and not role_rx.search(nm) and len(prev) < 60:
                    cand = nm
                    break
        if not cand:
            continue
        role_word = (rm.group(1) if rm else "member").lower()
        role = ("Chair" if role_word.startswith("chair") or role_word == "president" else
                "Vice Chair" if role_word.startswith("vice") else
                "Superintendent" if role_word == "superintendent" else
                "Principal" if role_word == "principal" else "Member")
        key = norm_name(cand)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": cand, "role": role, "seat": (sm.group(1).upper() if sm else ""), "line": ln[:160]})
    return out


def superintendent_from_text(text: str) -> str | None:
    return _titled(text, "superintendent", re.compile(r"(assistant|asst\.?|associate|deputy|former|retired)\s*$", re.I))


def people(root: Path, store: Store, cfg: dict | None = None, log=print) -> dict:
    """Board members, superintendents and principals from pages already crawled -> state/school_people.json."""
    conf = cfg or load_config(root)
    pc = conf.get("people") or {}
    reg = load_registry(root)
    dists = {d["id"]: d for d in school_districts(root)}
    for did, d in reg.get("districts", {}).items():
        dists.setdefault(did, {"id": did, "name": d.get("name") or d.get("lea_name"), "county": d.get("county", "")})
    sch_by_id = {s["id"]: s for s in reg["schools"]}
    board_words = [w.lower() for w in pc.get("board_page") or ["board of education", "board members"]]
    sup_words = [w.lower() for w in pc.get("superintendent_page") or ["superintendent"]]
    seat_rx = pc.get("seat") or SEAT_RX_DEFAULT
    found: dict[str, dict] = {}

    def put(p: dict):
        k = f"{p['district_id']}|{norm_name(p['name'])}"
        if k in found:
            cur = found[k]
            if p["role"] != "Member" and cur["role"] == "Member":
                cur["role"], cur["url"] = p["role"], p["url"]
            if p.get("seat") and not cur.get("seat"):
                cur["seat"] = p["seat"]
            if p.get("school_id") and not cur.get("school_id"):
                cur.update({k2: p[k2] for k2 in ("school_id", "school_name")})
            return
        found[k] = p

    n_pages = 0
    for did in dists:
        rows = store.query("source_id=? AND kind='page'", (f"{did}:site",), limit=600)
        for r in rows:
            hay = f"{r.get('title') or ''} {urlsplit(r.get('url') or '').path}".lower()
            text = r.get("text") or ""
            is_board = any(w in hay for w in board_words) or any(w in hay.replace("-", " ") for w in board_words)
            is_sup = any(w in hay for w in sup_words)
            if not (is_board or is_sup) or len(text) < 80:
                continue
            n_pages += 1
            if is_board:
                members = board_members_from_text(text, seat_rx)
                members = [m for m in members if m["role"] not in ("Principal",)]
                if sum(1 for m in members if m["role"] != "Superintendent") >= 2:
                    for m in members:
                        put({"name": m["name"], "role": m["role"] if m["role"] != "Superintendent" else "Superintendent", "seat": m["seat"],
                             "district_id": did, "district_name": dists[did]["name"], "county": dists[did].get("county", ""),
                             "url": r["url"], "page_title": r.get("title") or "", "line": m["line"], "found_at": r.get("fetched_at") or ""})
            sup = superintendent_from_text(text)
            if sup:
                put({"name": sup, "role": "Superintendent", "seat": "", "district_id": did, "district_name": dists[did]["name"],
                     "county": dists[did].get("county", ""), "url": r["url"], "page_title": r.get("title") or "", "line": "",
                     "found_at": r.get("fetched_at") or ""})
    # principals from the school pages
    for r in store.query("source_id=? AND kind='page'", (SRC_SITES,), limit=20000):
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        nm = meta.get("principal")
        if not nm:
            continue
        s = sch_by_id.get(meta.get("school_id") or "")
        did = (s or {}).get("district_id") or r.get("jurisdiction") or ""
        put({"name": nm, "role": "Principal", "seat": "", "district_id": did, "district_name": dists.get(did, {}).get("name", ""),
             "county": dists.get(did, {}).get("county", "") or (s or {}).get("county", ""), "school_id": meta.get("school_id"),
             "school_name": meta.get("school_name") or (s or {}).get("name", ""), "url": r["url"], "page_title": r.get("title") or "",
             "line": "", "found_at": r.get("fetched_at") or ""})
    out = []
    for p in found.values():
        p["id"] = "sch-" + slug(f"{p['name']} {p['district_id']}")
        p["electo_key"] = ""
        out.append(p)
    out.sort(key=lambda p: (p["district_id"], {"Superintendent": 0, "Chair": 1, "Vice Chair": 2, "Member": 3, "Principal": 4}.get(p["role"], 5), p["name"]))
    _link_electo(root, out)
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "school_people.json").write_text(json.dumps({"as_of": _now(), "pages_read": n_pages, "people": out}, ensure_ascii=False, indent=0),
                                               encoding="utf-8")
    counts = defaultdict(int)
    for p in out:
        counts[p["role"]] += 1
    log(f"school people: {len(out)} from {n_pages} pages: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) +
        f"; {sum(1 for p in out if p['electo_key'])} also in Electo's archive")
    return {"people": len(out), "pages": n_pages, **counts}


def _link_electo(root: Path, people_: list[dict]) -> None:
    """A board member who ran for office is in Electo's archive: keep that key (same name, same county)."""
    from .electo import electo_people

    by_name: dict[str, list[dict]] = defaultdict(list)
    for p in electo_people(root).values():
        by_name[norm_name(p.get("name") or "")].append(p)
        for a in p.get("aliases") or []:
            by_name[norm_name(a)].append(p)
    for q in people_:
        cands = by_name.get(norm_name(q["name"])) or []
        county = _alnum(q.get("county") or "")
        hit = [p for p in cands if county and any(_alnum(c) == county for c in (p.get("counties") or []))]
        if len(hit) == 1 or (len(cands) == 1 and not hit and not any(p.get("counties") for p in cands)):
            q["electo_key"] = (hit or cands)[0]["key"]


def load_people(root: Path) -> list[dict]:
    p = root / "state" / "school_people.json"
    return (json.loads(p.read_text(encoding="utf-8")).get("people") or []) if p.exists() else []


def people_as_candidates(root: Path) -> list[dict]:
    """School people in the research-candidate shape (merged by candidates.load_candidates)."""
    out = []
    for p in load_people(root):
        c = {"id": p["id"], "electo_key": p.get("electo_key") or "", "name": p["name"], "aliases": [],
             "office": f"{p['role']}, {p.get('school_name') or p.get('district_name') or ''}".strip(", "),
             "jurisdiction": p["district_id"], "jurisdictions": [p["district_id"]], "counties": [p["county"]] if p.get("county") else [],
             "levels": ["school"], "websites": [p["url"]] if p.get("url") else [], "social": [], "news_feeds": [],
             "profile_urls": [], "profile_depth": "", "last_race_date": "", "split": False, "party": "", "races": [],
             "school": {"role": p["role"], "seat": p.get("seat", ""), "district_id": p["district_id"], "school_id": p.get("school_id", ""),
                        "school_name": p.get("school_name", ""), "district_name": p.get("district_name", ""), "source": p["url"]}}
        out.append(c)
    return out


# ----------------------------------------------------------------------------- export
def _meta(r: dict) -> dict:
    m = r.get("meta") or {}
    return json.loads(m) if isinstance(m, str) else m


def board_metrics(votes: list[dict], meetings: list[dict]) -> dict:
    """What the minutes show about a board: meetings and recorded votes per year, how often votes were
    unanimous, the share of divided votes, and each member's participation (counts of recorded votes, never
    characterizations)."""
    by_year: dict[str, dict] = defaultdict(lambda: {"meetings": 0, "votes": 0, "unanimous": 0, "divided": 0})
    members: dict[str, dict] = defaultdict(lambda: {"votes": 0, "yes": 0, "no": 0, "other": 0})
    for m in meetings:
        y = (m.get("d") or m.get("published") or "")[:4]
        if y:
            by_year[y]["meetings"] += 1
    for v in votes:
        meta = _meta(v)
        y = (v.get("published") or "")[:4]
        if not y:
            continue
        by_year[y]["votes"] += 1
        vs = meta.get("votes") or []
        yes = sum(1 for x in vs if re.match(r"^(y|aye|yes|yea|affirm)", str(x.get("vote", "")), re.I))
        no = sum(1 for x in vs if re.match(r"^(n|nay|no|oppos|against)", str(x.get("vote", "")), re.I))
        if meta.get("unanimous") or (yes and not no):
            by_year[y]["unanimous"] += 1
        elif yes and no:
            by_year[y]["divided"] += 1
        for x in vs:
            nm = x.get("person") or x.get("name") or ""
            if not nm:
                continue
            k = members[nm]
            k["votes"] += 1
            vv = str(x.get("vote", ""))
            k["yes" if re.match(r"^(y|aye|yes|yea|affirm)", vv, re.I) else "no" if re.match(r"^(n|nay|no|oppos|against)", vv, re.I) else "other"] += 1
    return {"by_year": dict(sorted(by_year.items())), "members": dict(sorted(members.items(), key=lambda kv: -kv[1]["votes"])[:40])}


def export_schools(root: Path, store: Store, out: Path, log=print) -> dict:
    """dist/electo/schools.json (index), schools/<district>.json (detail), school_bounds.json (outlines)."""
    reg = load_registry(root)
    ppl = load_people(root)
    bounds_p = root / "state" / "school_bounds.json"
    if not reg.get("districts") and not reg.get("schools"):
        return {"districts": 0, "schools": 0, "people": 0}
    (out / "schools").mkdir(parents=True, exist_ok=True)
    # statistics by place
    st_d: dict[str, dict] = defaultdict(lambda: defaultdict(dict))     # district code -> series -> year -> value
    st_s: dict[tuple, dict] = defaultdict(lambda: defaultdict(dict))   # (district code, school key) -> series -> year
    st_state: dict[str, dict] = defaultdict(dict)
    labels, stat_urls = {}, {}
    for r in store.iter_query("kind='school_stat'"):
        m = _meta(r)
        if m.get("value") is None:
            continue
        labels[m["series"]] = m.get("label") or m["series"]
        stat_urls.setdefault(m["series"], r["url"])
        entry = {"v": m["value"], **({"ppe": m["per_pupil"]} if m.get("per_pupil") is not None else {}),
                 **({"c": m["components"]} if m.get("components") else {})}
        if m.get("level") == "state" or m.get("district_code") == "ALL":
            st_state[m["series"]][m["year"]] = entry
        elif m.get("school_code"):
            st_s[(m["district_code"], school_key(m["school_name"]))][m["series"]][m["year"]] = entry
        else:
            st_d[m["district_code"]][m["series"]][m["year"]] = entry
    # policies, meetings and votes per district
    policies: dict[str, list] = defaultdict(list)
    meetings: dict[str, list] = defaultdict(list)
    votes: dict[str, list] = defaultdict(list)
    for r in store.iter_query("kind IN ('policy','meeting','vote') AND jurisdiction LIKE 'sd-%'"):
        did = r["jurisdiction"]
        if r["kind"] == "policy":
            m = _meta(r)
            policies[did].append({"code": m.get("code", ""), "title": (r.get("title") or "")[:160], "u": r["url"],
                                  "adopted": m.get("adopted") or r.get("published") or "", "text": (r.get("text") or "")[:400]})
        elif r["kind"] == "meeting":
            meetings[did].append({"d": r.get("published") or "", "title": (r.get("title") or "")[:120], "u": r["url"]})
        else:
            votes[did].append(r)
    people_by_d: dict[str, list] = defaultdict(list)
    for p in ppl:
        people_by_d[p["district_id"]].append(p)
    sites_known = json.loads(store.kv_get("schools:sites") or "{}")
    dcode = lambda d: (d.get("st_leaid") or "").split("-")[-1].lstrip("0")  # noqa: E731  "GA-601" -> "601"
    index = {"as_of": reg.get("as_of", ""), "exported_at": _now(), "labels": labels, "stat_sources": stat_urls, "state": {k: v for k, v in st_state.items()},
             "districts": [], "schools": [], "people": [], "note": "Schools and districts from NCES EDGE (CCD 2024-25, PSS, IPEDS); statistics "
             "from GOSA's downloadable data and GaDOE's CCRPI files; board members, superintendents and principals from the "
             "districts' and schools' own pages (each person's `u` is the page that names them). Every figure is a published "
             "official value; nothing is estimated."}
    from .registry import jurisdictions as _jur

    jur = {j["id"]: j for j in _jur(root)}
    for did, d in sorted(reg["districts"].items()):
        j = jur.get(did, {})
        dc = dcode(d)
        peo = people_by_d.get(did, [])
        bm = board_metrics(votes.get(did, []), meetings.get(did, []))
        schools_here = [s for s in reg["schools"] if s["district_id"] == did]
        row = {"id": did, "name": d["name"], "lea_name": d.get("lea_name", ""), "kind": d["kind"], "county": d.get("county", ""),
               "city": d.get("city", ""), "website": j.get("website") or "", "portals": {k: v for k, v in (j.get("portals") or {}).items()
                                                                                        if k in ("boarddocs", "simbli", "youtube")},
               "meeting_page": j.get("meeting_page") or "", "address": d.get("address", ""), "zip": d.get("zip", ""), "phone": d.get("phone", ""),
               "grades": d.get("grades", ""), "enrollment": d.get("enrollment"), "teachers": d.get("teachers"), "ratio": d.get("ratio"),
               "schools": len([s for s in schools_here if s["kind"] == "public"]), "private_schools": len([s for s in schools_here if s["kind"] == "private"]),
               "colleges": len([s for s in schools_here if s["kind"] == "college"]), "lat": d.get("lat"), "lon": d.get("lon"),
               "board": [p["id"] for p in peo if p["role"] in ("Chair", "Vice Chair", "Member")],
               "superintendent": next((p["id"] for p in peo if p["role"] == "Superintendent"), ""),
               "policies": len(policies.get(did, [])), "meetings": len(meetings.get(did, [])), "votes": len(votes.get(did, [])),
               "stats": st_d.get(dc, {}), "board_metrics": bm["by_year"], "nces_url": d.get("url", "")}
        index["districts"].append(row)
        detail = {**row, "people": peo, "policy_list": sorted(policies.get(did, []), key=lambda p: (p["code"], p["title"]))[:600],
                  "meeting_list": sorted(meetings.get(did, []), key=lambda m: m["d"], reverse=True)[:120],
                  "member_votes": bm["members"],
                  "school_list": [{**s, "stats": st_s.get((dc, school_key(s["name"])), {}), "website": (sites_known.get(s["id"]) or {}).get("url", ""),
                                   "principal": next((p["id"] for p in peo if p.get("school_id") == s["id"] and p["role"] == "Principal"), "")}
                                  for s in sorted(schools_here, key=lambda s: (s["kind"], s["name"]))]}
        (out / "schools" / f"{did}.json").write_text(json.dumps(detail, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for s in reg["schools"]:
        dc = dcode(reg["districts"].get(s["district_id"], {})) if s.get("district_id") else ""
        stt = st_s.get((dc, school_key(s["name"])), {}) if dc else {}
        index["schools"].append({"id": s["id"], "n": s["name"], "k": s["kind"], "d": s.get("district_id", ""), "lv": s.get("level", ""),
                                 "g": s.get("grades", ""), "a": s.get("address", ""), "c": s.get("city", ""), "z": s.get("zip", ""),
                                 "co": s.get("county", ""), "lat": s.get("lat"), "lon": s.get("lon"), "e": s.get("enrollment"),
                                 "ch": 1 if s.get("charter") else 0, "t": s.get("type", ""),
                                 "w": (sites_known.get(s["id"]) or {}).get("url", ""),
                                 "pr": next((p["id"] for p in people_by_d.get(s.get("district_id", ""), []) if p.get("school_id") == s["id"]), ""),
                                 "st": {k: (max(v.items())[1]["v"] if v else None) for k, v in stt.items()}})
    index["people"] = [{"id": p["id"], "n": p["name"], "r": p["role"], "seat": p.get("seat", ""), "d": p["district_id"], "s": p.get("school_id", ""),
                        "sn": p.get("school_name", ""), "u": p["url"], "k": p.get("electo_key", ""), "at": (p.get("found_at") or "")[:10]} for p in ppl]
    write_json_atomic((out / "schools.json"), index, ensure_ascii=False, separators=(",", ":"))
    if bounds_p.exists():
        (out / "school_bounds.json").write_text(bounds_p.read_text(encoding="utf-8"), encoding="utf-8")
    log(f"schools: {len(index['districts'])} districts, {len(index['schools'])} schools, {len(ppl)} people")
    return {"districts": len(index["districts"]), "schools": len(index["schools"]), "people": len(ppl),
            "stats_series": len(labels), "policies": sum(len(v) for v in policies.values())}
