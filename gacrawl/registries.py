"""What an organization that gave money is, from the registries that hold every one of them in bulk, so a donor's
background can be written for thousands of organizations instead of the few a name search used to find.

  FEC committee and candidate files   https://www.fec.gov/files/bulk-downloads/{year}/cm{yy}.zip (and cn{yy}.zip), one
                                      small zip per two-year cycle (2016 on): every federal committee's type,
                                      designation, organization type, party, city and state, treasurer, connected
                                      organization and candidate. A federal committee's money lines carry its FEC id
                                      (contributor_committee_id), so most are matched by id, the rest by exact name.
  Georgia State Ethics Commission     the public committee and candidate searches of both current systems (EFile,
                                      2022-2025, and PeachFile, 2026 on), the calls their own search pages make
                                      (PublicFilerDetails/GetCommitteeDetails and GetCandidateDetails): every Georgia
                                      PAC, party, independent and leadership committee and every candidate's campaign
                                      committee, with its type, status, filing cycles, city, chairperson and treasurer.
                                      Read once a week (about 80 requests for all of them).
  IRS exempt organizations            https://www.irs.gov/pub/irs-soi/eo_{st}.csv, the IRS's own Business Master File
                                      by state: every tax-exempt organization's section (501(c)(3), (4), (6)...),
                                      ruling year, activity code, city and latest revenue. Only the states donors are
                                      in, each read at most once a week.

Matching is exact (normalized names, never a prefix or a guess): an organization the registries do not name exactly
gets nothing from them rather than a namesake's facts (Ameris Bank was once given its foundation's IRS record).
Nothing personal is kept: no street address, phone, email or the IRS's "in care of" name; only the officers a filing
names (treasurer, chairperson), as the existing FEC lookups already did.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import zipfile
from collections import defaultdict

from .entities import norm_org

FEC_BULK = "https://www.fec.gov/files/bulk-downloads/{y}/{kind}{yy}.zip"
IRS_BMF = "https://www.irs.gov/pub/irs-soi/eo_{st}.csv"
FIRST_CYCLE = 2016
GA_PAGE = {"efile": "https://recordsearch.ethics.ga.gov/public/cf/publiccommitte",
           "peachfile": "https://peachfile.ethics.ga.gov/public/cf/publiccommitte"}
GA_CAND_PAGE = {"efile": "https://recordsearch.ethics.ga.gov/public/cf/publiccandidate",
                "peachfile": "https://peachfile.ethics.ga.gov/public/cf/publiccandidate"}
GA_RECHECK_DAYS = 7
IRS_RECHECK_DAYS = 7
STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH "
             "OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY".split())

# the FEC's codes (committee master file description)
CMTE_TP = {"C": "communication cost filer", "D": "delegate committee", "E": "electioneering communication filer",
           "H": "House candidate committee", "I": "independent expenditure filer", "N": "PAC (nonqualified)",
           "O": "independent expenditure-only committee (super PAC)", "P": "presidential candidate committee",
           "Q": "PAC (qualified)", "S": "Senate candidate committee", "U": "single-candidate independent expenditure committee",
           "V": "hybrid PAC with a non-contribution account (nonqualified)",
           "W": "hybrid PAC with a non-contribution account (qualified)", "X": "party committee (nonqualified)",
           "Y": "party committee (qualified)", "Z": "national party nonfederal account"}
CMTE_DSGN = {"A": "authorized by a candidate", "B": "lobbyist/registrant PAC", "D": "leadership PAC",
             "J": "joint fundraising committee", "P": "principal campaign committee of a candidate", "U": "unauthorized"}
ORG_TP = {"C": "corporation", "L": "labor organization", "M": "membership organization", "T": "trade association",
          "V": "cooperative", "W": "corporation without capital stock"}
PARTY = {"DEM": "Democratic", "REP": "Republican", "LIB": "Libertarian", "GRE": "Green", "IND": "independent",
         "NPA": "no party", "NNE": "", "UNK": "", "OTH": "other"}
OFFICE = {"H": "U.S. House", "S": "U.S. Senate", "P": "President"}
# the IRS's section codes (Business Master File description)
SUBSECTION = {"02": "501(c)(2) title-holding corporation", "03": "501(c)(3) charity",
              "04": "501(c)(4) social welfare organization", "05": "501(c)(5) labor or agricultural organization",
              "06": "501(c)(6) business league or chamber of commerce", "07": "501(c)(7) social or recreational club",
              "08": "501(c)(8) fraternal beneficiary society", "09": "501(c)(9) voluntary employees' beneficiary association",
              "10": "501(c)(10) domestic fraternal society", "12": "501(c)(12) cooperative or mutual company",
              "13": "501(c)(13) cemetery company", "14": "501(c)(14) credit union", "19": "501(c)(19) veterans' organization",
              "25": "501(c)(25) title-holding corporation", "92": "4947(a)(1) charitable trust"}
NTEE = {"A": "arts, culture and humanities", "B": "education", "C": "environment", "D": "animal-related",
        "E": "health care", "F": "mental health", "G": "diseases and disorders", "H": "medical research",
        "I": "crime and legal", "J": "employment", "K": "food, agriculture and nutrition", "L": "housing and shelter",
        "M": "public safety and disaster relief", "N": "recreation and sports", "O": "youth development",
        "P": "human services", "Q": "international affairs", "R": "civil rights, social action and advocacy",
        "S": "community improvement and capacity building", "T": "philanthropy and grantmaking",
        "U": "science and technology", "V": "social science", "W": "public and societal benefit", "X": "religion",
        "Y": "mutual and membership benefit"}


def clean(name: str) -> str:
    """A filed name as people write it: the State Ethics Commission's API doubles apostrophes ("It''s Time")."""
    return re.sub(r"\s+", " ", (name or "").replace("''", "'")).strip()


def person(name: str) -> str:
    """A filed officer's name as people write it: "PHILLIPS, JUSTIN" -> "Justin Phillips" (courtesy titles dropped)."""
    from .entities import display_person

    n = re.sub(r"(?i)\b(mr|mrs|ms|miss|dr|hon)\b\.?", "", clean(name)).strip(" ,")
    m = re.search(r"(?i)[ ,]+(jr|sr|ii|iii|iv)\.?$", n)        # "SMITH, JOHN Q JR" -> "John Q Smith Jr"
    suffix = ""
    if m and "," in n[:m.start()]:
        n, suffix = n[:m.start()].strip(" ,"), m.group(1)
        suffix = " " + (suffix.upper() if suffix.lower() in ("ii", "iii", "iv") else suffix.title())
    return ((display_person(n) or n).strip() + suffix).strip()


NO_ORG = {"", "NONE", "N/A", "NA", "NONE.", "NO", "-", "NULL", "NOT APPLICABLE"}


def connected(name: str) -> str:
    """The FEC files' connected organization, or "" for their placeholders ("NONE", "N/A")."""
    n = clean(name)
    return "" if n.upper() in NO_ORG else n


def reg_key(name: str) -> str:
    """The key registry names are matched on: norm_org without apostrophes or spaces ("It's Time, Georgia, Inc." and
    "ITS TIME GEORGIA INC" are one key; "Ameris Bank" and "Ameris Bank Foundation" are not)."""
    return norm_org(re.sub(r"['’`]", "", clean(name))).replace(" ", "")


def _year(s: str) -> str:
    m = re.search(r"\b(19|20)\d\d\b", s or "")
    return m.group(0) if m else ""


def _span(years) -> str:
    ys = sorted({y for y in years if y})
    if not ys:
        return ""
    return ys[0] if len(ys) == 1 else f"{ys[0]} to {ys[-1]}"


def _place(city: str, st: str) -> str:
    return ", ".join(x for x in ((city or "").strip().title(), (st or "").strip().upper()) if x)


# ------------------------------------------------------------------------------------------------ the FEC files
class FecFiles:
    """Every federal committee (and candidate) in the FEC's bulk files for the cycles since FIRST_CYCLE."""

    def __init__(self, fetcher, today: dt.date | None = None, log=print):
        self.f, self.log = fetcher, log
        self.committees: dict[str, dict] = {}
        self.candidates: dict[str, dict] = {}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.ok = False
        self.errors: list[str] = []
        today = today or dt.date.today()
        self.cycles = list(range(FIRST_CYCLE, today.year + 2 - today.year % 2, 2))

    def _rows(self, kind: str, y: int):
        url = FEC_BULK.format(y=y, kind=kind, yy=str(y)[2:])
        r = self.f.get(url, use_cache=False, kind="fec-bulk")
        if r.status != 200:
            raise RuntimeError(f"{url}: HTTP {r.status}")
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".txt"))
            for line in io.TextIOWrapper(zf.open(name), encoding="latin-1"):
                yield url, line.rstrip("\r\n").split("|")

    def load(self) -> "FecFiles":
        got = 0
        for y in self.cycles:
            try:
                for url, f in self._rows("cn", y):
                    if len(f) >= 7:
                        self.candidates[f[0]] = {"name": f[1], "party": f[2], "office": f[5], "st": f[4], "district": f[6]}
                for url, f in self._rows("cm", y):
                    if len(f) < 15 or not f[0]:
                        continue
                    c = self.committees.get(f[0])
                    if c is None:
                        c = self.committees[f[0]] = {"id": f[0], "cycles": []}
                        self.by_name[reg_key(f[1])].append(f[0])
                    c.update({"name": f[1], "treasurer": f[2], "city": f[5], "st": f[6], "dsgn": f[8], "tp": f[9],
                              "party": f[10], "org_tp": f[12], "connected": f[13], "cand": f[14], "src": url})
                    c["cycles"].append(y)
                got += 1
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{y}: {type(e).__name__}: {str(e)[:120]}")
        # the newest cycle's file is the one every committee still filing is in; without it the files are not usable
        self.ok = got >= max(1, len(self.cycles) - 1)
        self.log(f"registries: FEC files for {got} of {len(self.cycles)} cycles, {len(self.committees):,} committees"
                 + (f"; {'; '.join(self.errors[:3])}" if self.errors else ""))
        return self

    def match(self, names: list[str], fec_ids: list[str], state: str = "") -> dict | None:
        for cid in fec_ids:
            if cid in self.committees:
                return self.committees[cid]
        cands = []
        for n in names:
            for cid in self.by_name.get(reg_key(n), []):
                if cid not in cands:
                    cands.append(cid)
        if len(cands) > 1 and state:
            same = [c for c in cands if self.committees[c].get("st") == state]
            cands = same or cands
        if len(cands) > 1:   # one name, several committees: the one filing most recently, if only one is
            last = max(max(self.committees[c]["cycles"]) for c in cands)
            cands = [c for c in cands if max(self.committees[c]["cycles"]) == last]
        return self.committees[cands[0]] if len(cands) == 1 else None

    def facts(self, c: dict) -> tuple[list[dict], list[dict], dict]:
        """(what lines, officers, fields) for one committee, each line with the file it came from."""
        src, what = c["src"], []
        kind = ", ".join(x for x in (CMTE_TP.get(c.get("tp") or ""), CMTE_DSGN.get(c.get("dsgn") or ""),
                                     ORG_TP.get(c.get("org_tp") or "")) if x)
        party = PARTY.get(c.get("party") or "", "")
        what.append({"text": f"Registered with the FEC (committee {c['id']}) as a {kind or 'committee'}"
                             + (f", affiliated with the {party} Party" if party and party not in ("independent", "no party", "other") else ""),
                     "src": src, "basis": "FEC committee file"})
        span, place = _span(str(y) for y in c.get("cycles") or []), _place(c.get("city"), c.get("st"))
        if span or place:
            what.append({"text": "Listed in the FEC's committee files" + (f" for the {span} election cycles" if span else "")
                                 + (f"; based in {place}" if place else ""), "src": src, "basis": "FEC committee file"})
        if connected(c.get("connected") or ""):
            what.append({"text": f"Connected organization: {connected(c['connected'])}", "src": src, "basis": "FEC committee file"})
        cand = self.candidates.get(c.get("cand") or "")
        if cand and c.get("dsgn") in ("P", "A"):
            seat = OFFICE.get(cand["office"], "") + (f" ({cand['st']}" + (f"-{cand['district']}" if cand.get("district") not in ("", "00") else "") + ")"
                                                      if cand.get("st") else "")
            what.append({"text": f"The campaign committee of {person(cand['name'])}, a candidate for {seat}".rstrip(),
                         "src": src.replace("/cm", "/cn"), "basis": "FEC candidate file"})
        officers = [{"name": person(c["treasurer"]), "role": "treasurer", "src": src}] if c.get("treasurer") else []
        return what, officers, {"fec_id": c["id"], "connected_org": connected(c.get("connected") or ""),
                                "city": (c.get("city") or "").title(), "state": c.get("st") or ""}


# ------------------------------------------------------------------------------------------------ Georgia
def _ga_body(kind: str, page: int, size: int) -> dict:
    if kind == "committee":
        return {"pageNumber": page, "pageSize": size, "isCommittee": True, "sortBy": "", "sortType": "asc",
                "filerTypeCode": None, "filerName": None, "politicalPartyCode": None, "OfficeSought": None,
                "committeeType": None, "filingYear": None, "chairPersonName": None, "treasurerName": None,
                "accountStatus": None, "jurisdictionIsStateOrIsCounty": None, "committeeMakingIE": None,
                "jurisdictionId": None, "districtTypeDesc": None, "isProfile": False, "filingCycle": None}
    return {"pageNumber": page, "pageSize": size, "sortBy": "", "sortType": "asc", "filerTypeCode": "CAN",
            "filerName": None, "politicalPartyCode": None, "OfficeSought": None, "accountStatus": None, "election": None,
            "electionCycle": None, "transactionSourceTypeCode": None, "treasurerName": None, "jurisdictionId": None,
            "campaignName": None, "cityDistrictId": None, "districtTypeId": None, "jurisdictionIsStateOrIsCounty": None,
            "districtTypeDesc": None, "isProfile": False}


def _ga_row(system: str, kind: str, x: dict) -> dict:
    g = lambda k: (str(x.get(k) or "")).strip()  # noqa: E731
    row = {"sys": system, "k": kind, "type": g("filerType") or ("Candidate" if kind == "candidate" else ""),
           "status": g("filerStatus"), "cycle": g("filingCycleName") or g("electionCycleName"), "city": g("committeeMailingCity"),
           "st": g("committeeMailingStateCode"), "chair": clean(g("chairPerson")), "treasurer": clean(g("treasurer")),
           "affiliated": clean(g("affiliatedCommitteeName")), "stance": g("stance"), "ballot": clean(g("ballotQuestionName")),
           "entity": x.get("filerEntityId")}
    if kind == "candidate":
        row.update({"name": clean(g("committeeName")), "cand": clean(g("filerName")), "office": g("office"),
                    "district": g("districtName"), "party": g("politicalPartyCode"), "juris": g("jurisdiction") or g("jurisdictionTypeName")})
    else:
        row["name"] = clean(g("filerName"))
    return {k: v for k, v in row.items() if v not in ("", None)}


def ga_registry(fetcher, store, log=print, force: bool = False) -> list[dict]:
    """Every committee and candidate committee in both current systems, cached in kv ga_registry for a week."""
    from .ethics import SYSTEMS, _api

    try:
        have = json.loads(store.kv_get("ga_registry") or "{}")
    except ValueError:
        have = {}
    fresh = have.get("at", "") >= (dt.date.today() - dt.timedelta(days=GA_RECHECK_DAYS)).isoformat()
    if have.get("rows") and fresh and not force:
        return have["rows"]
    rows, complete = [], True
    for system, base in SYSTEMS.items():
        for kind, path in (("committee", "PublicFilerDetails/GetCommitteeDetails"), ("candidate", "PublicFilerDetails/GetCandidateDetails")):
            size, page, total, got = 100, 1, None, 0
            while True:
                try:
                    d = _api(fetcher, base, path, _ga_body(kind, page, size))
                except Exception as e:  # noqa: BLE001
                    if page == 1 and size > 25:      # the search may cap its page size: ask for smaller pages
                        size //= 2
                        continue
                    log(f"registries: State Ethics Commission {system} {kind} page {page}: {type(e).__name__}: {str(e)[:160]}")
                    complete = False
                    break
                data = d.get("data") or {}
                items = data.get("items") or []
                total = data.get("totalItems") if total is None else total
                rows += [_ga_row(system, kind, x) for x in items]
                got += len(items)
                if not items or len(items) < size or (total and got >= total):
                    break
                page += 1
            log(f"registries: State Ethics Commission {system}: {got:,} {kind} rows" + (f" of {total:,}" if total else ""))
    if rows and (complete or not have.get("rows")):
        store.kv_set("ga_registry", json.dumps({"at": dt.date.today().isoformat() if complete else have.get("at", ""),
                                                "rows": rows}, ensure_ascii=False, separators=(",", ":")))
        return rows
    return have.get("rows") or rows


TYPE_RANK = {"Political Action Committee": 0, "Political Party": 0, "Political Party Committee": 0, "Leadership Committee": 1,
             "Independent Committee": 2}
GA_TYPE = {"Political Action Committee": "a political action committee", "Independent Committee": "an independent committee",
           "Leadership Committee": "a leadership committee", "Political Party": "a political party committee",
           "Political Party Committee": "a political party committee", "Recall Committee": "a recall committee",
           "Individual": "a contributor that is not a PAC (the Commission's \"Individual\" filer type)",
           "Individual Committee / Other (Non-PAC)": "a contributor that is not a PAC (the Commission's \"Individual Committee / Other (Non-PAC)\" filer type)"}


class GaRegistry:
    def __init__(self, rows: list[dict]):
        self.by_key: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            if r.get("name"):
                self.by_key[reg_key(r["name"])].append(r)
        self.ok = bool(rows)

    def match(self, names: list[str]) -> list[dict]:
        out = []
        for n in names:
            for r in self.by_key.get(reg_key(n), []):
                if r not in out:
                    out.append(r)
        if not out:
            return []
        # one name, two different filers (two "Friends of ..." committees, two candidates): none is chosen. Each system
        # numbers its filers itself, so one filer in both EFile and PeachFile is two ids, one per system.
        cands = {re.sub(r"[^a-z]", "", (r.get("cand") or "").lower()) for r in out if r.get("k") == "candidate"}
        if len(cands) > 1:
            return []
        # one organization registered twice in a system (Civil Justice PAC: a PAC and an independent committee): the
        # registration of the kind it gives money as, a PAC or party first, then the one filing longest
        per_sys = defaultdict(lambda: defaultdict(list))
        for r in out:
            per_sys[r["sys"]][r.get("entity")].append(r)
        keep = []
        for sysrows in per_sys.values():
            best = max(sysrows.values(), key=lambda rs: (min(TYPE_RANK.get(r.get("type", ""), 9) for r in rs) * -1, len(rs)))
            keep += best
        return keep

    @staticmethod
    def facts(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
        rows = sorted(rows, key=lambda r: (_year(r.get("cycle", "")), r["sys"] == "peachfile"))
        last = rows[-1]
        src = (GA_CAND_PAGE if last["k"] == "candidate" else GA_PAGE)[last["sys"]]
        span = _span(_year(r.get("cycle", "")) for r in rows)
        place = _place(last.get("city", ""), last.get("st", ""))
        what = []
        if last["k"] == "candidate":
            seat = ", ".join(x for x in (last.get("office", ""), last.get("district", "")) if x)
            what.append({"text": f"The campaign committee of {last.get('cand', '')}" + (f", a candidate for {seat}" if seat else "")
                                 + (f" ({last['party'].strip()})" if last.get("party", "").strip() not in ("", "NP") else "")
                                 + ", registered with the Georgia State Ethics Commission"
                                 + (f" for the {span} filing cycles" if span else ""), "src": src, "basis": "State Ethics Commission registration"})
        else:
            typ = last.get("type") or ""
            kind = GA_TYPE.get(typ) or ("a ballot question committee" if "ballot" in typ.lower() else f'a filer of type "{typ}"' if typ else "a committee")
            what.append({"text": f"Registered with the Georgia State Ethics Commission as {kind}"
                                 + (f" ({last['status'].lower()})" if last.get("status") else "")
                                 + (f", filing for the {span} cycles" if span else "") + (f"; based in {place}" if place else ""),
                         "src": src, "basis": "State Ethics Commission registration"})
            if last.get("affiliated"):
                what.append({"text": f"Affiliated committee: {last['affiliated']}", "src": src, "basis": "State Ethics Commission registration"})
            if last.get("ballot") and last.get("stance"):
                what.append({"text": f"Registered to {last['stance'].lower()} {last['ballot']}", "src": src,
                             "basis": "State Ethics Commission registration"})
        officers = [{"name": person(last[k]), "role": role, "src": src} for k, role in (("chair", "chairperson"), ("treasurer", "treasurer"))
                    if last.get(k)]
        return what, officers, {"city": (last.get("city") or "").title(), "state": last.get("st", ""), "ga_committee": last.get("name", "")}


# ------------------------------------------------------------------------------------------------ the IRS
class IrsFiles:
    """Exact-name matches in the IRS Business Master File of each state donors are in. Each state's file is read only
    when one of its donors has not been looked for in it yet, and at most once a week; what was found (and what was
    looked for and not found) is kept in kv irs_bmf, so nothing is read again for names already looked for."""

    def __init__(self, fetcher, store, log=print):
        self.f, self.store, self.log = fetcher, store, log
        try:
            self.cache = json.loads(store.kv_get("irs_bmf") or "{}")
        except ValueError:
            self.cache = {}
        self.errors: list[str] = []

    def prepare(self, wanted: dict[str, set[str]], deadline: float | None = None) -> None:
        """wanted: {state: {reg_key, ...}}. Reads the state files needed for keys not looked for yet."""
        import time

        cutoff = (dt.date.today() - dt.timedelta(days=IRS_RECHECK_DAYS)).isoformat()
        for st, keys in sorted(wanted.items(), key=lambda kv: -len(kv[1])):
            if deadline and time.monotonic() > deadline:
                break
            st = st.upper()
            if st not in STATES:
                continue
            c = self.cache.setdefault(st, {"at": "", "hits": {}, "looked": []})
            looked = set(c.get("looked") or [])
            new = keys - looked
            if not new or (c.get("at", "") >= cutoff and looked):
                continue
            url = IRS_BMF.format(st=st.lower())
            try:
                r = self.f.get(url, use_cache=False, kind="irs-bmf")
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                rows = csv.DictReader(io.StringIO(r.content.decode("latin-1")))
                want = keys | looked
                hits: dict[str, list] = defaultdict(list)
                n = 0
                for row in rows:
                    n += 1
                    k = reg_key(row.get("NAME") or "")
                    if k in want:
                        hits[k].append({x: (row.get(x) or "").strip() for x in ("EIN", "NAME", "CITY", "STATE", "SUBSECTION",
                                                                                 "RULING", "NTEE_CD", "REVENUE_AMT", "TAX_PERIOD")})
                c.update({"at": dt.date.today().isoformat(), "src": url, "hits": dict(hits), "looked": sorted(want), "rows": n})
                self.log(f"registries: IRS exempt organizations, {st}: {n:,} read, {len(hits)} of {len(want)} donor names found")
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{st}: {type(e).__name__}: {str(e)[:120]}")
        self.store.kv_set("irs_bmf", json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")))

    def looked_for(self, key: str, state: str) -> bool:
        return key in set((self.cache.get(state.upper()) or {}).get("looked") or [])

    def match(self, names: list[str], state: str, city: str = "") -> dict | None:
        c = self.cache.get((state or "").upper()) or {}
        rows = []
        for n in names:
            for r in (c.get("hits") or {}).get(reg_key(n), []):
                if r not in rows:
                    rows.append(r)
        if len(rows) > 1 and city:
            same = [r for r in rows if (r.get("CITY") or "").lower() == city.lower()]
            rows = same or rows
        if len(rows) > 1:      # a group's subordinate chapters share its name: the one that files with revenue, if one does
            rev = [r for r in rows if (r.get("REVENUE_AMT") or "").isdigit() and int(r["REVENUE_AMT"]) > 0]
            rows = rev if len(rev) == 1 else rows
        if len(rows) != 1:
            return None          # not there, or several organizations of that name in the state: none is chosen
        return {**rows[0], "src": c.get("src") or IRS_BMF.format(st=state.lower())}

    @staticmethod
    def facts(r: dict, label: str = "") -> tuple[list[dict], dict]:
        src, what = r["src"], []
        sub = SUBSECTION.get((r.get("SUBSECTION") or "").zfill(2))
        ruling = (r.get("RULING") or "")[:4]
        if sub:
            what.append({"text": label + f"Recognized by the IRS as tax-exempt, a {sub}" + (f", since {ruling}" if ruling.isdigit() and ruling > "1900" else "")
                                 + f" (EIN {r['EIN'][:2]}-{r['EIN'][2:]})", "src": src, "basis": "IRS Business Master File"})
        ntee = (r.get("NTEE_CD") or "")[:1].upper()
        if ntee in NTEE:
            what.append({"text": label + f"Activity area on the IRS list: {NTEE[ntee]} (code {r['NTEE_CD']})", "src": src,
                         "basis": "IRS Business Master File"})
        place = _place(r.get("CITY"), r.get("STATE"))
        rev, tp = r.get("REVENUE_AMT") or "", r.get("TAX_PERIOD") or ""
        if place or (rev.isdigit() and int(rev) > 0):
            what.append({"text": label + (f"Based in {place}" if place else "")
                                 + (("; " if place else "") + f"revenue of ${int(rev):,} on its latest return on the IRS list"
                                    + (f" (tax period ending {tp[:4]}-{tp[4:6]})" if len(tp) == 6 else "") if rev.isdigit() and int(rev) > 0 else ""),
                         "src": src, "basis": "IRS Business Master File"})
        return what, {"ein": r.get("EIN") or "", "city": (r.get("CITY") or "").title(), "state": r.get("STATE") or ""}
