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
import time
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


GOSA_PARSER = "3"    # stored with each file read; a newer parser reads the files again (3: the CCRPI single score)

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
    """The series' rows from a GOSA CSV (_gosa_rows); with none, from the file's own counts when the series says how
    (`counts`, for files without an all-grades row)."""
    out = _gosa_rows(text, series_id, s)
    if not out and s.get("counts"):
        out = _gosa_counts(text, series_id, s)
    return out


def _gosa_rows(text: str, series_id: str, s: dict) -> list[dict]:
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
    add = [_colx(H, x) for x in (s.get("add") or [])]     # value = these columns added up (proficient + distinguished)
    if s.get("add") and None in add:
        return []
    lv = {k: _colx(H, v) for k, v in (s.get("level_value") or {}).items()}
    c_val = _colx(H, s["value"]) if s.get("value") else None
    if c_val is None and not ratio and not lv and not add:
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
        elif add:
            vals = [_num(cell(r, i)) for i in add]
            v = round(sum(vals), 1) if all(x is not None for x in vals) else None
        elif lv:
            v = _num(cell(r, lv.get(level)))
        else:
            v = _num(cell(r, c_val))
        if v is None:
            continue
        key = (dc, sc if is_school else "", year)
        if key in places:
            if s.get("sum") or s.get("avg"):
                places[key]["value"] = round(places[key]["value"] + v, 2)
                places[key]["rows"] += 1
            continue                      # one value per place: the first matching row
        places[key] = {"series": series_id, "year": year, "district_code": dc, "district_name": dn,
                       "school_code": sc if is_school else "", "school_name": sn if is_school else "", "value": v,
                       "level": level, "rows": 1}
    out = list(places.values())
    for row in out:
        if s.get("avg"):
            row["value"] = round(row["value"] / row["rows"], 1)     # the mean over the matching rows (EOC courses)
        if not s.get("sum"):
            row.pop("rows", None)
    return out


def _gosa_counts(text: str, series_id: str, s: dict) -> list[dict]:
    """A percentage from the file's own counts, for a file that has no all-grades row: the rows `counts.where` keeps
    (all students, one subject) are added up per place across `counts.over` (the grades), and the value is
    100 x sum(num) / sum(den), the same figure an all-grades row would print. A place with any suppressed count
    (TFS) among its rows gets no value rather than a partial one."""
    c = s["counts"]
    rd = csv.reader(io.StringIO(text))
    try:
        H = [h.strip().lstrip("\ufeff").lstrip("#").strip().upper() for h in next(rd)]
    except StopIteration:
        return []
    pc = {k: _colx(H, *v) for k, v in _PLACE.items()}
    num = [_colx(H, x) for x in c.get("num") or []]
    den = _colx(H, c.get("den") or "")
    where = {(_colx(H, k)): {str(v).strip().lower() for v in (vals if isinstance(vals, list) else [vals])}
             for k, vals in (c.get("where") or {}).items()}
    if None in num or den is None or None in where or (pc["dc"] is None and pc["dn"] is None):
        return []

    def cell(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""

    acc: dict[tuple, dict] = {}
    for r in rd:
        if not r or any(cell(r, i).lower() not in ok for i, ok in where.items()):
            continue
        dc = cell(r, pc["dc"]).lstrip("0") or cell(r, pc["dc"])
        sc, sn, dn, year = cell(r, pc["sc"]), cell(r, pc["sn"]), cell(r, pc["dn"]), cell(r, pc["year"])
        is_school = bool(sc and sc.upper() not in ("ALL", "0", "000") and sn)
        level = "school" if is_school else ("state" if dc.upper() == "ALL" else "district")
        a = acc.setdefault((dc, sc if is_school else "", year), {"series": series_id, "year": year, "district_code": dc,
                                                                 "district_name": dn, "school_code": sc if is_school else "",
                                                                 "school_name": sn if is_school else "", "level": level,
                                                                 "n": 0.0, "d": 0.0, "bad": False})
        vals = [_num(cell(r, i)) for i in num] + [_num(cell(r, den))]
        if any(v is None for v in vals):
            a["bad"] = True
            continue
        a["n"] += sum(vals[:-1])
        a["d"] += vals[-1]
    out = []
    for a in acc.values():
        if a.pop("bad") or not a["d"]:
            continue
        n, d = a.pop("n"), a.pop("d")
        out.append({**a, "value": round(100.0 * n / d, 1)})
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
    expenditure and the three-year average come with it, and the newest CCRPI single score (Sch_SingleScore_25:
    GaDOE's 0-100 score, which GOSA lists beside the spending) when the workbook has it."""
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
                ss = sorted(((int(m.group(2)), i) for i, h in enumerate(H)
                             if (m := re.fullmatch(r"(DISTRICT|DIST|SYSTEM|SYS|SCH|SCHOOL)_SINGLE_?SCORE_(\d\d)", h))), reverse=True)
                c_ss, ssyy = (ss[0][1], ss[0][0]) if ss else (None, None)
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
        single = _num(cells[c_ss]) if c_ss is not None and c_ss < len(cells) else None
        if single is not None:
            out[-1].update({"single_score": single, "single_score_year": f"FY{ssyy}"})
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
            fesr_rows = parse_fesr(parse_xlsx_rows(fr.content), sid)
            # the same workbook carries each place's per-pupil spending: its own series, for comparisons
            ppe_rows = [{**r, "series": sid.replace("fesr", "ppe"), "value": r["per_pupil"]} for r in fesr_rows if r.get("per_pupil") is not None]
            parts = [(sid, s.get("label", sid), fesr_rows),
                     (sid.replace("fesr", "ppe"), "Spending per student ($, " + ("district" if "district" in sid else "school") + ")", ppe_rows)]
            # and the CCRPI single score each place got (GaDOE's own score, as GOSA lists it in this workbook). GaDOE's
            # CCRPI page, where the score files were linked, is gone (Nov 2026: a 404 on its new site)
            cc_rows = [{**{k: v for k, v in r.items() if k not in ("per_pupil", "per_pupil_3yr_avg", "single_score", "single_score_year")},
                        "series": "ccrpi", "value": r["single_score"], "year": r.get("single_score_year") or r["year"]}
                       for r in fesr_rows if r.get("single_score") is not None]
            if cc_rows:
                parts.append(("ccrpi", "CCRPI single score", cc_rows))
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
                head = "" if (s.get("xlsx") or url.lower().endswith((".xlsx", ".xls"))) else fr.content[:400].decode("utf-8", "replace").split("\n", 1)[0]
                out["errors"].append(f"{oid}: no rows matched in {url.rsplit('/', 1)[-1]} (the file's layout may have changed)"
                                     + (f"; its columns: {head[:300]}" if head else ""))
        if n_rows:
            store.kv_set(f"schools:gosa:{sid}", f"{url}#p{GOSA_PARSER}")   # read: skipped until the file or parser changes
        out["files"] += 1
        out["rows"] += n_rows
    # CCRPI files linked from GaDOE's page
    c = conf.get("ccrpi") or {}
    if c.get("page") and not timed_out():
        try:
            r = fetcher.get(c["page"])
            html = r.text if r.status == 200 else ""
            if r.status == 200 and not re.search(r"\.(xlsx?|csv)", html, re.I):
                from .legis_video import _rendered

                html = _rendered(fetcher, c["page"]) or html     # the page builds its links with scripts
            if html:
                soup = extract.soup_of(html)
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
# a district page that lists its schools, by its address or a title that says so ("Brantley County Schools" in every
# title of a district's site says nothing)
SCHOOL_LIST_PATH_RX = re.compile(r"(?i)/(?:our[-_]?schools|schools|find[-_]a[-_]school|school[-_](?:directory|list|locator|finder)|"
                                 r"campuses|elementary[-_]schools|middle[-_]schools|high[-_]schools)(?:/|\.|$)")
SCHOOL_LIST_TITLE_RX = re.compile(r"(?i)\b(our schools|find a school|school directory|school list|school locator|school finder|campuses)\b")
LEVEL_RX = re.compile(r"(?i)\b(elementary|middle|high|primary|intermediate|junior|senior)\b")
KIND_RX = re.compile(r"(?i)\b(academy|center|centre|magnet|charter)\b")
PRINCIPAL_LINK_RX = re.compile(r"\b(principal|administration|administrators|about(?: us| our school)?|our school|staff|"
                               r"leadership|welcome|faculty|office)\b", re.I)


MD_LINK_RX = re.compile(r"\[([^\]]{2,120})\]\((https?://[^)\s]+)\)")


def school_site_links(doc: str, base_url: str, schools: list[dict]) -> dict[str, str]:
    """school id -> website, for links on a district page (HTML, or the stored Markdown of it) whose text names one
    of the district's schools."""
    # a name without its level words is shared ("Brantley County Middle School" and "... High School" are both
    # "brantley"): matched with its level first, and without it only when one school has it
    by_level: dict[str, set] = defaultdict(set)
    by_name: dict[str, set] = defaultdict(set)
    for s in schools:
        if school_key(s["name"]):
            by_level[_level_key(s["name"])].add(s["id"])
            by_name[school_key(s["name"])].add(s["id"])

    def which(text: str) -> str | None:
        if not school_key(text):
            return None
        for ids in (by_level.get(_level_key(text)) or set(), by_name.get(school_key(text)) or set()):
            if len(ids) == 1:
                return next(iter(ids))
        return None

    host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    found: dict[str, str] = {}
    if "<a " in doc or "<A " in doc:
        pairs = [(a.get_text(" ", strip=True), a["href"]) for a in extract.soup_of(doc).find_all("a", href=True)]
    else:
        pairs = [(m.group(1), m.group(2)) for m in MD_LINK_RX.finditer(doc)]
    for text, href in pairs:
        sid = which(text)
        if not sid:
            continue
        u = urljoin(base_url, href).split("#")[0]
        if not u.startswith("http"):
            continue
        lh = urlsplit(u).netloc.lower().removeprefix("www.")
        # a school's own site (another host or a sub-site path), not a document or a listing on the same page
        if lh == host and (u.rstrip("/") == base_url.rstrip("/") or re.search(r"\.(pdf|docx?)$", u, re.I)):
            continue
        found.setdefault(sid, u)
    return found


def _level_key(name: str) -> str:
    """The name with its level: 'Brantley County High School' -> 'brantley|high'; 'Impact Academy' -> 'impact|academy'
    and 'Impact Academy Middle School' -> 'impact|middle' (the grade level counts first; academy, charter... only
    when a name has none)."""
    lv = {x.lower() for x in LEVEL_RX.findall(name or "")} or {x.lower() for x in KIND_RX.findall(name or "")}
    return school_key(name) + "|" + "-".join(sorted(lv))


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

    # 0. each district's superintendent and board pages, with up to half of this run's time
    if fetcher is not None:
        half = None if deadline is None else time.monotonic() + max(0.0, deadline - time.monotonic()) / 2
        try:
            out["people_pages"] = district_people_pages(fetcher, root, store, log=log, deadline=half)
        except OutOfTime:
            pass

    # 1. school websites, from the district's own pages that list its schools: its home page (the menu names them) and
    #    the pages crawled from its site (source <sd>:site) whose address or title says they list them. The stored
    #    copies mostly keep no links (Nov 12: 480 of 3,416 district pages had any, and no school site was ever found),
    #    so a page whose copy has none is read again, live: a few a district, each district once a month until every
    #    one of its schools is found
    from .registry import load_json

    disc = load_json(root / "state" / "discovered.json") or {}
    checked = json.loads(store.kv_get("schools:sites:checked") or "{}")
    month_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat(timespec="seconds")
    sch_dist = {s["id"]: s["district_id"] for s in reg["schools"]}
    have: dict[str, int] = defaultdict(int)
    for sid in sites_known:
        have[sch_dist.get(sid, "")] += 1
    live_max = int(pc.get("district_pages_live") or 3)
    for did, schools in by_dist.items():
        if timed_out():
            break
        if have[did] >= len(schools) or (checked.get(did) or "") > month_ago:
            continue
        cands: list[tuple[str, str]] = []
        home = str((disc.get(did) or {}).get("website") or "").strip()
        if home:
            cands.append((home, ""))
        for r in store.query("source_id=? AND kind='page'", (f"{did}:site",), limit=400):
            u = r.get("url") or ""
            if u and (SCHOOL_LIST_PATH_RX.search(urlsplit(u).path) or SCHOOL_LIST_TITLE_RX.search(r.get("title") or "")):
                cands.append((u, r.get("markdown") or r.get("text") or ""))
        if not cands:
            checked[did] = _now()
            continue
        out["districts_scanned"] += 1
        live, stopped, tried = 0, False, set()
        for u, doc in cands:
            if u in tried or len(tried) >= 8:
                continue
            tried.add(u)
            found = school_site_links(doc, u, schools) if doc and ("](http" in doc or "<a " in doc) else {}
            if not found and fetcher is not None and live < live_max:
                if timed_out():
                    stopped = True
                    break
                live += 1
                try:
                    fr = fetcher.get(u)
                    if fr.status == 200 and "html" in (fr.content_type or ""):
                        found = school_site_links(fr.text, fr.url, schools)
                except OutOfTime:
                    stopped = True
                    break
                except Exception:  # noqa: BLE001 — a district page that won't load waits for next month
                    pass
            for sid, su in found.items():
                if sid not in sites_known:
                    sites_known[sid] = {"url": su, "found_on": u, "found_at": _now()}
        if not stopped:
            checked[did] = _now()
    store.kv_set("schools:sites:checked", json.dumps(checked))
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


# ----------------------------------------------------------------------------- the districts' own people pages
SRC_DPAGES = "schools:district_pages"
SUP_LINK_RX = re.compile(r"\bsuperintendent", re.I)
SUP_LINK_NOT = re.compile(r"\b(assistant|deputy|associate|area|search|evaluation|contract|former|state school|advisory|"
                          r"student|teacher|council|cabinet|calendar|application|apply)\b", re.I)
BOARD_LINK_RX = re.compile(r"\b(board of education|board members|members of the board|school board|our board|meet the board|"
                           r"board of trustees|boe members|meet our board|board)\b", re.I)
BOARD_LINK_NOT = re.compile(r"\b(meetings?|agendas?|minutes|polic(?:y|ies)|calendar|videos?|live\s*stream|budget|elections?|"
                            r"bylaws|boarddocs|simbli|eboard|work session|hearing|retreat|recognition|approved|notices?|"
                            r"advisory|foundation|health|state board)\b", re.I)


def district_people_pages(fetcher: Fetcher, root: Path, store: Store, log=print, deadline: float | None = None) -> dict:
    """Each district's superintendent and board-member pages, followed from the links on its own home page, once a
    month, kept as `page` records (source schools:district_pages, meta role "superintendent", "board" or "home"). The
    district `:site` crawl keeps the pages that hold meetings and minutes: of its pages only 64 named a board or a
    superintendent (16 superintendents and 51 members for 180 districts, Nov 13, 2026)."""
    from .registry import jurisdictions

    dists = {j["id"]: j for j in jurisdictions(root) if j.get("kind") == "school_district" or str(j.get("id", "")).startswith("sd-")}
    checked = json.loads(store.kv_get("schools:people_pages:checked") or "{}")
    month_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat(timespec="seconds")
    out = {"districts": 0, "pages": 0}

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    for did, j in sorted(dists.items()):
        if timed_out():
            break
        if (checked.get(did) or "") > month_ago:
            continue
        home = str(j.get("website") or "").strip()
        if not home:
            checked[did] = _now()
            continue
        try:
            r = fetcher.get(home)
        except OutOfTime:
            break
        except Exception:  # noqa: BLE001 — a site that refuses or fails waits for next month
            checked[did] = _now()
            continue
        if r.status != 200 or "html" not in (r.content_type or ""):
            checked[did] = _now()
            continue
        host = urlsplit(r.url).netloc.lower().removeprefix("www.")
        picks: dict[str, list] = defaultdict(list)
        for a in extract.soup_of(r.text).find_all("a", href=True):
            lab = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            u = urljoin(r.url, a["href"]).split("#")[0]
            if not u.startswith("http") or u.rstrip("/") == r.url.rstrip("/") or re.search(r"\.(pdf|docx?|jpe?g|png)$", u, re.I):
                continue
            if urlsplit(u).netloc.lower().removeprefix("www.") != host:
                continue                 # the district's own pages only (BoardDocs and Simbli hold meetings, not rosters)
            hay = f"{lab} {urlsplit(u).path.replace('-', ' ').replace('_', ' ')}"
            if SUP_LINK_RX.search(hay) and not SUP_LINK_NOT.search(hay):
                picks["superintendent"].append((2 if SUP_LINK_RX.search(lab) else 1, u))
            elif BOARD_LINK_RX.search(hay) and not BOARD_LINK_NOT.search(hay) and len(lab) <= 50:
                picks["board"].append((2 if re.search(r"member|board of education|school board|our board|meet", hay, re.I) else 1, u))
        pages = [(r.url, r.text, "home")]          # a home page often names its superintendent
        stopped = False
        for role in ("superintendent", "board"):
            for _, u in sorted(picks.get(role, []), key=lambda x: -x[0])[:1]:
                try:
                    p = fetcher.get(u)
                except OutOfTime:
                    stopped = True
                    break
                except Exception:  # noqa: BLE001
                    continue
                if p.status == 200 and "html" in (p.content_type or ""):
                    pages.append((p.url, p.text, role))
            if stopped:
                break
        for u, html, role in pages:
            title, md = extract.html_to_markdown(html, u)
            if not md:
                continue
            store.add(Record(SRC_DPAGES, "page", u, title=title or "", text=md, markdown=md, channel="html", evidence="primary",
                             jurisdiction=did, key=f"{SRC_DPAGES}|page|{u}",
                             meta={"district_id": did, "role": role, "opened": True, "found_on": r.url}))
            out["pages"] += 1
        if stopped:
            break
        checked[did] = _now()
        out["districts"] += 1
    store.kv_set("schools:people_pages:checked", json.dumps(checked))
    store.flush()
    log(f"district people pages: {out['pages']} pages from {out['districts']} districts")
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


SUP_BEFORE = re.compile(r"(assistant|asst\.?|associate|deputy|former|retired|area|state school|state|georgia)\s*$", re.I)


def superintendent_from_text(text: str) -> str | None:
    """The district's superintendent as its page names them; never an assistant's, a former one's, or the State School
    Superintendent's (district pages often quote "State School Superintendent Richard Woods")."""
    return _titled(text, "superintendent", SUP_BEFORE)


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

    sup_best: dict[str, tuple] = {}

    def sup_cand(prio: int, p: dict):
        """One superintendent per district: the one its superintendent page names, then its board page, then any other
        page (a home page can quote a former or the state's superintendent)."""
        cur = sup_best.get(p["district_id"])
        if cur is None or prio < cur[0]:
            sup_best[p["district_id"]] = (prio, p)

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
                        (sup_cand if m["role"] == "Superintendent" else (lambda _p, x: put(x)))(1, {
                            "name": m["name"], "role": m["role"], "seat": m["seat"],
                            "district_id": did, "district_name": dists[did]["name"], "county": dists[did].get("county", ""),
                            "url": r["url"], "page_title": r.get("title") or "", "line": m["line"], "found_at": r.get("fetched_at") or ""})
            sup = superintendent_from_text(text)
            if sup:
                sup_cand(1 if is_sup else 2, {"name": sup, "role": "Superintendent", "seat": "", "district_id": did, "district_name": dists[did]["name"],
                                              "county": dists[did].get("county", ""), "url": r["url"], "page_title": r.get("title") or "", "line": "",
                                              "found_at": r.get("fetched_at") or ""})
    # the districts' own superintendent and board pages (district_people_pages), read live from their home pages
    for r in store.query("source_id=? AND kind='page'", (SRC_DPAGES,), limit=5000):
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        did = meta.get("district_id") or r.get("jurisdiction") or ""
        text = r.get("text") or ""
        if did not in dists or len(text) < 80:
            continue
        n_pages += 1
        if meta.get("role") == "board":
            members = [m for m in board_members_from_text(text, seat_rx) if m["role"] != "Principal"]
            if sum(1 for m in members if m["role"] != "Superintendent") >= 2:
                for m in members:
                    (sup_cand if m["role"] == "Superintendent" else (lambda _p, x: put(x)))(1, {
                        "name": m["name"], "role": m["role"], "seat": m["seat"], "district_id": did, "district_name": dists[did]["name"],
                        "county": dists[did].get("county", ""), "url": r["url"], "page_title": r.get("title") or "", "line": m["line"],
                        "found_at": r.get("fetched_at") or ""})
        sup = superintendent_from_text(text)
        if sup:
            sup_cand({"superintendent": 0, "board": 1}.get(meta.get("role"), 3),
                     {"name": sup, "role": "Superintendent", "seat": "", "district_id": did, "district_name": dists[did]["name"],
                      "county": dists[did].get("county", ""), "url": r["url"], "page_title": r.get("title") or "", "line": "",
                      "found_at": r.get("fetched_at") or ""})
    for _, p in sup_best.values():
        put(p)
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
    # (district code, the school's name with its level) -> series -> year. By the name alone, "Brantley County High
    # School" and "Brantley County Middle School" were one school ("brantley"), and each showed the other's numbers
    st_s: dict[tuple, dict] = defaultdict(lambda: defaultdict(dict))
    st_names: dict[tuple, set] = defaultdict(set)      # (district code, name without level) -> the level keys under it
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
            st_s[(m["district_code"], _level_key(m["school_name"]))][m["series"]][m["year"]] = entry
            st_names[(m["district_code"], school_key(m["school_name"]))].add(_level_key(m["school_name"]))
        else:
            st_d[m["district_code"]][m["series"]][m["year"]] = entry
    def stats_of(dc: str, name: str) -> dict:
        """A school's statistics: by its name and level; by the name alone only when one school of the district has it
        (GOSA and NCES spell a level differently now and then: "Academy", or none)."""
        got = st_s.get((dc, _level_key(name)))
        if got:
            return got
        under = st_names.get((dc, school_key(name))) or set()
        return st_s.get((dc, next(iter(under))), {}) if len(under) == 1 else {}

    # policies, meetings and votes per district
    policies: dict[str, list] = defaultdict(list)
    meetings: dict[str, list] = defaultdict(list)
    votes: dict[str, list] = defaultdict(list)
    videos: dict[str, list] = defaultdict(list)
    readings: dict[str, dict] = {}     # video page -> the open model's reading of its transcript (video_ai.py)
    for r in store.iter_query("kind='transcript' AND jurisdiction LIKE 'sd-%' AND meta LIKE '%\"ai\"%'"):
        m = _meta(r)
        a = m.get("ai") or {}
        if m.get("video_page") and (a.get("summary") or a.get("items")):
            readings[m["video_page"]] = {"summary": a.get("summary") or "", "m": a.get("m") or "",
                                         "items": [{k: it.get(k) for k in ("type", "who", "text", "needs_review") if it.get(k)}
                                                   for it in (a.get("items") or []) if it.get("type") in ("motion", "vote", "agenda")][:8],
                                         "source": m.get("transcript_source") or ""}
    for r in store.iter_query("kind IN ('policy','meeting','vote','video') AND jurisdiction LIKE 'sd-%'"):
        did = r["jurisdiction"]
        if r["kind"] == "policy":
            m = _meta(r)
            policies[did].append({"code": m.get("code", ""), "title": (r.get("title") or "")[:160], "u": r["url"],
                                  "adopted": m.get("adopted") or r.get("published") or "", "text": (r.get("text") or "")[:400]})
        elif r["kind"] == "meeting":
            meetings[did].append({"d": r.get("published") or "", "title": (r.get("title") or "")[:120], "u": r["url"]})
        elif r["kind"] == "video":
            # meeting recordings from the district's own channel (official, or confirmed by the owner)
            oc = _meta(r).get("official_channel") or {}
            videos[did].append({"d": (r.get("published") or "")[:10], "title": (r.get("title") or "")[:160], "u": r["url"],
                                "ch": oc.get("title") or "", "chu": f"https://www.youtube.com/channel/{oc['id']}" if oc.get("id") else "",
                                "basis": (oc.get("basis") or "")[:200], **({"read": readings[r["url"]]} if r["url"] in readings else {})})
        else:
            votes[did].append(r)
    # budget documents: the district's own (its site's documents and pages tagged Budget and taxes) and the state's
    # funding files (GaDOE allotments, revenues, expenditures: `gacrawl schools budgets`)
    budget_by_d: dict[str, list] = defaultdict(list)
    for r in store.iter_query("source_id LIKE 'sd-%:site' AND kind IN ('document','page')"):
        m = _meta(r)
        if "Budget and taxes" not in (m.get("topics") or []) and not BUDGET_WORDS.search(r.get("title") or ""):
            continue
        budget_by_d[r["source_id"].split(":", 1)[0]].append({"t": (r.get("title") or "")[:160], "u": r["url"], "k": r["kind"],
                                                             "d": r.get("published") or "", "at": (r.get("fetched_at") or "")[:10]})
    funding = []
    for r in store.iter_query("kind='budget_doc'"):
        m = _meta(r)
        d = {"t": (r.get("title") or "")[:160], "u": r["url"], "on": m.get("found_on") or "", "at": (r.get("fetched_at") or "")[:10],
             **({"how": m["qbe"]["how"]} if (m.get("qbe") or {}).get("how") else {})}
        if (r.get("jurisdiction") or "").startswith("sd-"):
            # a school system's own allotment sheet (GaDOE's QBE reports): listed with that district
            budget_by_d[r["jurisdiction"]].append({"t": d["t"], "u": d["u"], "k": "budget_doc", "d": "", "at": d["at"],
                                                   **({"how": d["how"]} if d.get("how") else {})})
        else:
            funding.append(d)
    people_by_d: dict[str, list] = defaultdict(list)
    for p in ppl:
        people_by_d[p["district_id"]].append(p)
    sites_known = json.loads(store.kv_get("schools:sites") or "{}")
    dcode = lambda d: (d.get("st_leaid") or "").split("-")[-1].lstrip("0")  # noqa: E731  "GA-601" -> "601"
    index = {"as_of": reg.get("as_of", ""), "exported_at": _now(), "labels": labels, "stat_sources": stat_urls, "state": {k: v for k, v in st_state.items()},
             "funding_docs": sorted(funding, key=lambda d: (d["at"], d["t"]), reverse=True)[:150],
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
               "stats": st_d.get(dc, {}), "board_metrics": bm["by_year"], "nces_url": d.get("url", ""),
               "budget_docs": len(budget_by_d.get(did, []))}
        index["districts"].append(row)
        detail = {**row, "people": peo, "policy_list": sorted(policies.get(did, []), key=lambda p: (p["code"], p["title"]))[:600],
                  "meeting_list": sorted(meetings.get(did, []), key=lambda m: m["d"], reverse=True)[:120],
                  "video_list": sorted(videos.get(did, []), key=lambda m: m["d"], reverse=True)[:200],
                  "member_votes": bm["members"],
                  "budget_list": sorted(budget_by_d.get(did, []), key=lambda x: (x["d"] or x["at"]), reverse=True)[:40],
                  "school_list": [{**s, "stats": stats_of(dc, s["name"]), "website": (sites_known.get(s["id"]) or {}).get("url", ""),
                                   "principal": next((p["id"] for p in peo if p.get("school_id") == s["id"] and p["role"] == "Principal"), "")}
                                  for s in sorted(schools_here, key=lambda s: (s["kind"], s["name"]))]}
        (out / "schools" / f"{did}.json").write_text(json.dumps(detail, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for s in reg["schools"]:
        dc = dcode(reg["districts"].get(s["district_id"], {})) if s.get("district_id") else ""
        stt = stats_of(dc, s["name"]) if dc else {}
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


# ----------------------------------------------------------------------------- state funding and district budgets
SRC_BUDGETS = "schools:budgets"
BUDGET_WORDS = re.compile(r"allotment|qbe|revenue|expenditure|budget|per.pupil|financial|funding|appropriation", re.I)


def budgets(fetcher: Fetcher, root: Path, store: Store, cfg: dict | None = None, log=print, deadline: float | None = None) -> dict:
    """State funding and spending documents from GaDOE's finance pages (config/schools.yaml `budgets`): the pages are
    rendered (they build their links with scripts), links whose text names allotments, QBE, revenues, expenditures or
    budgets are followed one level, and every spreadsheet or PDF reached is kept as a `budget_doc` record with its text
    (so each district's allotment sheet is searchable and the AI can read it). Nothing is built from a URL pattern."""
    from .ingest import pdf_to_record
    from .legis_video import _rendered

    conf = cfg or load_config(root)
    bc = conf.get("budgets") or {}
    out = {"pages": 0, "files": 0, "new": 0, "errors": []}
    seen: set[str] = set()

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    def links_of(html: str, base: str) -> list[tuple[str, str]]:
        soup = extract.soup_of(html)
        res = []
        for a in soup.find_all("a", href=True):
            u = urljoin(base, a["href"]).split("#")[0]
            txt = f"{a.get_text(' ', strip=True)} {urlsplit(u).path.rsplit('/', 1)[-1]}"
            if BUDGET_WORDS.search(txt):
                res.append((u, a.get_text(' ', strip=True)[:160]))
        return res

    def page(url: str) -> str:
        r = fetcher.get(url)
        html = r.text if r.status == 200 else ""
        if not html or not re.search(r'href="[^"]*\.(xlsx?|csv|pdf)', html, re.I):
            html = _rendered(fetcher, url) or html
        out["pages"] += 1
        return html

    for start in bc.get("pages") or []:
        if timed_out():
            break
        try:
            html = page(start)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{start}: {type(exc).__name__}")
            continue
        queue = [(u, t, 0) for u, t in links_of(html, start)]
        while queue and not timed_out():
            u, label, depth = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            path = urlsplit(u).path.lower()
            if re.search(r"\.(xlsx?|csv|pdf)$", path):
                if store.kv_get(f"schools:budget:{u}") and not bc.get("refresh"):
                    continue
                try:
                    fr = fetcher.get(u)
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"{u}: {type(exc).__name__}"[:160])
                    continue
                if fr.status != 200 or len(fr.content) > int(bc.get("max_file_bytes") or 40000000):
                    continue
                out["files"] += 1
                meta = {"label": label, "found_on": start, "opened": True, "topics": ["Budget and taxes"]}
                if path.endswith(".pdf"):
                    rec = pdf_to_record(fr.content, SRC_BUDGETS, "budget_doc", url=fr.url, title=label or path.rsplit("/", 1)[-1], meta=meta,
                                        ocr_max_pages=0)
                else:
                    rows = parse_xlsx_rows(fr.content) if not path.endswith(".csv") else [r for r in csv.reader(io.StringIO(fr.content.decode("utf-8", "replace")))]
                    text = "\n".join("\t".join(str(c or "") for c in r) for r in rows[:4000])
                    rec = Record(SRC_BUDGETS, "budget_doc", fr.url, title=label or path.rsplit("/", 1)[-1], text=text, channel="file",
                                 jurisdiction="ga", evidence="primary", meta={**meta, "rows": len(rows)})
                if rec:
                    rec.evidence = "primary"
                    out["new"] += store.add(rec)
                store.kv_set(f"schools:budget:{u}", _now())
            elif depth < int(bc.get("depth") or 1) and urlsplit(u).netloc.endswith(tuple(bc.get("hosts") or ["gadoe.org"])):
                try:
                    queue += [(u2, t2, depth + 1) for u2, t2 in links_of(page(u), u)]
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"{u}: {type(exc).__name__}"[:160])
    store.flush()
    log(f"budgets: {out['pages']} pages, {out['files']} files, {out['new']} new")
    return out
