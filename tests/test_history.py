"""Election history lane (gacrawl/history): every reader against a small file in its publisher's layout, then the
bundle Electo reads. No network: a stand-in serves the files."""
import io
import json
import zipfile

import pytest

from gacrawl.history import build as hbuild
from gacrawl.history import run as hrun
from gacrawl.history.model import general_day, nice_name, party_code, pkey, shard
from gacrawl.history.sources import (_stage_of, clarity_contests, fec_contests, ga_office, legislators_read, medsl_contests,
                                     oe_contests, xls_sheets)

HOUSE = """year,state,state_po,office,district,stage,runoff,special,candidate,party,writein,candidatevotes,totalvotes
1976,GEORGIA,GA,US HOUSE,5,GEN,FALSE,FALSE,ANDREW YOUNG,DEMOCRAT,FALSE,95191,142559
1976,GEORGIA,GA,US HOUSE,5,GEN,FALSE,FALSE,ED GADRIX,REPUBLICAN,FALSE,47998,142559
1976,ALASKA,AK,US HOUSE,0,GEN,FALSE,FALSE,DON YOUNG,REPUBLICAN,FALSE,83722,118208
1976,ALASKA,AK,US HOUSE,0,GEN,FALSE,FALSE,WRITEIN,,TRUE,292,118208
1976,NEW YORK,NY,US HOUSE,1,GEN,FALSE,FALSE,OTIS G PIKE,DEMOCRAT,FALSE,100000,160000
1976,NEW YORK,NY,US HOUSE,1,GEN,FALSE,FALSE,OTIS G PIKE,LIBERAL,FALSE,10000,160000
1976,NEW YORK,NY,US HOUSE,1,GEN,FALSE,FALSE,SOME ONE,REPUBLICAN,FALSE,50000,160000
1966,GEORGIA,GA,US HOUSE,5,GEN,FALSE,FALSE,TOO EARLY,DEMOCRAT,FALSE,1,1
"""
SENATE = """year\tstate\tstate_po\toffice\tdistrict\tstage\tspecial\tcandidate\tparty_detailed\twritein\tcandidatevotes\ttotalvotes\tparty_simplified
2020\tGEORGIA\tGA\tUS SENATE\tstatewide\tgen\tFALSE\tDAVID A. PERDUE\tREPUBLICAN\tFALSE\t2462617\t4952175\tREPUBLICAN
2020\tGEORGIA\tGA\tUS SENATE\tstatewide\tgen\tFALSE\tJON OSSOFF\tDEMOCRAT\tFALSE\t2374519\t4952175\tDEMOCRAT
2020\tGEORGIA\tGA\tUS SENATE\tstatewide\tgen\tFALSE\tSHANE HAZEL\tLIBERTARIAN\tFALSE\t115039\t4952175\tLIBERTARIAN
"""
PRES = """year,state,state_po,office,candidate,party_detailed,writein,candidatevotes,totalvotes,party_simplified
1976,GEORGIA,GA,US PRESIDENT,"CARTER, JIMMY",DEMOCRAT,FALSE,979409,1467458,DEMOCRAT
1976,GEORGIA,GA,US PRESIDENT,"FORD, GERALD",REPUBLICAN,FALSE,483743,1467458,REPUBLICAN
"""


def test_names_and_keys():
    assert nice_name("JOHN H BUCHANAN JR") == "John H. Buchanan Jr."
    assert nice_name("CARTER, JIMMY") == "Jimmy Carter"
    assert nice_name("Carter, Earl L. \"Buddy\"") == "Earl L. \"Buddy\" Carter"
    assert nice_name("DON MCDONALD") == "Don McDonald"
    assert pkey("Earl “Buddy” Carter, Jr.") == "EARL BUDDY CARTER"
    assert party_code("DEMOCRATIC-FARMER-LABOR") == "D" and party_code("Rep") == "R" and party_code("LIBERAL") == "O" and party_code("") == ""
    assert general_day(1968) == "1968-11-05" and general_day(2024) == "2024-11-05" and general_day(1976) == "1976-11-02"
    assert 0 <= shard("PARK CANNON", 64) < 64
    assert _stage_of("July 31, 2012 - General Primary-General Nonpartisan-Special Election") == "primary"
    assert _stage_of("August 21, 2012 - General Primary Runoff") == "primary runoff"
    assert _stage_of("December 3, 2013 - Special Election Runoff") == "special runoff"
    assert _stage_of("November 6, 2012 - General Election") == "general"
    assert _stage_of("March 1, 2016 - Presidential Preference Primary") == "primary"


def test_ga_office():
    assert ga_office("U.S. House", "5")[:3] == ("federal", "U.S. House", "5")
    assert ga_office("State Representative, District 43")[:3] == ("legislative", "State House", "43")
    assert ga_office("State Senator, District 006")[3] == "State Senate District 6"
    assert ga_office("Lieutenant Governor")[1] == "Lieutenant Governor" and ga_office("Governor")[1] == "Governor"
    assert ga_office("Public Service Commission, District 3")[3] == "Public Service Commission District 3"
    assert ga_office("Constitutional Amendment 1")[0] == "question"
    assert ga_office("Justice, Supreme Court")[0] == "judicial"
    assert ga_office("Sheriff")[0] == "local"
    assert ga_office("President of the United States")[1] == "President"


def test_medsl():
    cs = list(medsl_contests(HOUSE, "house", "https://doi.org/x"))
    assert len(cs) == 3
    ga = next(c for c in cs if c["st"] == "GA")
    assert ga["t"] == "U.S. House · Georgia District 5" and ga["date"] == "1976-11-02" and ga["tot"] == 142559
    assert ga["o"][0] == ["Andrew Young", "D", 95191, 0, "won"]
    ak = next(c for c in cs if c["st"] == "AK")
    assert ak["dist"] == "AL" and len(ak["o"]) == 1 and ak["o"][0][4] == "unopposed"
    ny = next(c for c in cs if c["st"] == "NY")
    assert ny["o"][0][:3] == ["Otis G. Pike", "D", 110000]          # one candidate on two party lines: votes added
    sen = list(medsl_contests(SENATE, "senate", "u"))
    assert sen[0]["office"] == "U.S. Senate" and sen[0]["o"][0][0] == "David A. Perdue" and sen[0]["o"][2][1] == "L"
    pr = list(medsl_contests(PRES, "president", "u"))
    assert pr[0]["t"] == "President · Georgia" and pr[0]["o"][0][0] == "Jimmy Carter"


LEGIS = [
    {"id": {"bioguide": "Y000028"}, "name": {"first": "Andrew", "last": "Young", "official_full": "Andrew Young"}, "bio": {"birthday": "1932-03-12"},
     "terms": [{"type": "rep", "start": "1973-01-03", "end": "1975-01-03", "state": "GA", "district": 5, "party": "Democrat"},
               {"type": "rep", "start": "1975-01-14", "end": "1977-01-03", "state": "GA", "district": 5, "party": "Democrat"}]},
    {"id": {"bioguide": "T000035"}, "name": {"first": "Herman", "last": "Talmadge"}, "bio": {},
     "terms": [{"type": "sen", "start": "1957-01-03", "end": "1963-01-03", "state": "GA", "party": "Democrat", "class": 3},
               {"type": "sen", "start": "1969-01-03", "end": "1975-01-03", "state": "GA", "party": "Democrat", "class": 3}]},
    {"id": {"bioguide": "O000001"}, "name": {"first": "Old", "last": "Timer"}, "terms": [{"type": "rep", "start": "1901-01-03", "end": "1903-01-03", "state": "GA", "district": 1, "party": "Democrat"}]},
    {"id": {"bioguide": "A000001"}, "name": {"first": "App", "last": "Ointed"},
     "terms": [{"type": "sen", "start": "1971-01-03", "end": "1972-11-07", "state": "GA", "party": "Democrat", "how": "appointment"}]},
]


def test_legislators():
    profiles, contests = legislators_read(LEGIS, "https://example.org/l.json")
    assert set(profiles) == {"Y000028", "T000035", "A000001"}
    assert len(profiles["T000035"]["terms"]) == 2
    got = {(c["y"], c["office"], c["dist"]): c for c in contests}
    assert set(got) == {(1972, "U.S. House", "5"), (1968, "U.S. Senate", "")}      # Jan. 14 start and the appointment are not general-election seats
    assert got[(1968, "U.S. Senate", "")]["o"] == [["Herman Talmadge", "D", None, 0, "won"]] and got[(1968, "U.S. Senate", "")]["basis"] == "seated"


OE_PRI = """county,office,district,party,candidate,votes
Fulton,Governor,,REP,SONNY PERDUE (I),100
Fulton,Governor,,REP,RAY MCBERRY,20
DeKalb,Governor,,REP,SONNY PERDUE (I),50
DeKalb,Governor,,REP,RAY MCBERRY,10
Fulton,Governor,,DEM,MARK TAYLOR,90
Fulton,Governor,,DEM,CATHY COX,85
Fulton,Governor,,DEM,BILL BOLTON,20
Fulton,State House,43,DEM,A PERSON,5
Fulton,Registered Voters,,,,1000
"""
OE_RUN = """county,office,district,party,candidate,votes
Fulton,Governor,,DEM,MARK TAYLOR,60
Fulton,Governor,,DEM,CATHY COX,40
"""
OE_GEN = """county,office,district,party,candidate,votes
Fulton,U.S. Senate,,DEM,MAX CLELAND,489
Fulton,U.S. Senate,,REP,GUY MILLNER,478
Fulton,U.S. Senate,,LIB,JACK CASHIN,33
"""


def test_openelections():
    cs = list(oe_contests([OE_PRI], "2006-07-18", "primary", "", 0, "u"))
    gov = {c["p"]: c for c in cs if c["office"] == "Governor"}
    assert gov["R"]["o"][0] == ["Sonny Perdue", "", 150, 1, "won"] and gov["R"]["cty"] == {"DeKalb": [50, 10], "Fulton": [100, 20]}
    assert gov["D"]["o"][0][4] == "led" and gov["D"]["tot"] == 195            # 46 percent: no majority
    assert any(c["office"] == "State House" and c["o"][0][4] == "unopposed" for c in cs)
    assert not any("Registered" in c["office"] for c in cs)


def _clarity(region, by, contests):
    x = f'<ElectionResult><ElectionName>E</ElectionName><Region>{region}</Region>'
    for title, vf, choices in contests:
        x += f'<Contest key="1" text="{title}" voteFor="{vf}">'
        for name, party, per in choices:
            x += f'<Choice key="1" text="{name}" party="{party}" totalVotes="{sum(per.values())}"><VoteType name="Election Day" votes="{sum(per.values())}">'
            x += "".join(f'<{by} name="{k}" votes="{v}"/>' for k, v in per.items()) + "</VoteType></Choice>"
        x += "</Contest>"
    return (x + "</ElectionResult>").encode()


def test_clarity():
    state = _clarity("GA", "County", [("Governor", 1, [("NATHAN DEAL (I)", "REP", {"Fulton": 60, "Cobb": 70}), ("JASON CARTER", "DEM", {"Fulton": 90, "Cobb": 30})]),
                                      ("Constitutional Amendment A", 1, [("YES", "", {"Fulton": 10}), ("NO", "", {"Fulton": 5})])])
    county = _clarity("Fulton", "Precinct", [("Governor", 1, [("NATHAN DEAL (I)", "REP", {"P1": 60}), ("JASON CARTER", "DEM", {"P1": 90})]),
                                             ("City of Atlanta Council Member District 2", 1, [("A B", "", {"P1": 7}), ("C D", "", {"P1": 3})]),
                                             ("Board of Education At Large", 2, [("E F", "", {"P1": 9}), ("G H", "", {"P1": 8}), ("I J", "", {"P1": 1})])])
    cs = list(clarity_contests([("c/detail.xml", county), ("detail.xml", state)], "2014-11-04", "general", "November 4, 2014 - General Election", "u"))
    gov = next(c for c in cs if c["office"] == "Governor")
    assert gov["tot"] == 250 and gov["o"][0] == ["Nathan Deal", "R", 130, 1, "won"] and gov["cty"]["Fulton"] == [60, 90]
    assert sum(1 for c in cs if c["office"] == "Governor") == 1            # the county's copy of a statewide contest is not added again
    city = next(c for c in cs if "Atlanta" in c["office"])
    assert city["lvl"] == "local" and city["j"] == "Fulton" and city["o"][0][4] == "won"
    boe = next(c for c in cs if "Education" in c["office"])
    assert boe["vf"] == 2 and [x[4] for x in boe["o"]] == ["won", "won", ""]
    q = next(c for c in cs if c["lvl"] == "question")
    assert q["o"][0][0] == "YES" and q["o"][0][4] == ""


def _fec_xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2022 US House Results by State"
    ws.append(["1", "STATE ABBREVIATION", "STATE", "D", "FEC ID#", "(I)", "CANDIDATE NAME (First)", "CANDIDATE NAME (Last)", "CANDIDATE NAME",
               "TOTAL VOTES", "PARTY", "PRIMARY VOTES", "PRIMARY %", "RUNOFF VOTES", "RUNOFF %", "GENERAL VOTES "])
    ws.append([2, "GA", "Georgia", "02", "H2GA02031", "(I)", "Sanford", "Bishop", "Bishop, Sanford D. Jr.", None, "D", "58,000", None, None, None, 132675])
    ws.append([3, "GA", "Georgia", "02", "H2GA02999", None, "Chris", "West", "West, Chris", None, "R", 20000, None, 14622, None, 108665])
    ws.append([4, "GA", "Georgia", "02", "H2GA02998", None, "Jeremy", "Hunt", "Hunt, Jeremy", None, "R", 28000, None, 12565, None, None])
    ws.append([5, "GA", "Georgia", "02", "H2GA02997", None, "Wayne", "Johnson", "Johnson, Wayne", None, "R", 12000, None, None, None, None])
    ws.append([6, "GA", "Georgia", "02", None, None, None, None, "District Votes:", None, None, 118000, None, None, None, None])
    ws.append([7, "CA", "California", "12", "H2CA00001", None, "A", "B", "B, A", None, "D", 100, None, None, None, None])
    ws.append([8, "CA", "California", "12", "H2CA00002", None, "C", "D", "D, C", None, "R", 50, None, None, None, None])
    ws.append([9, "GA", "Georgia", "S", "S0GA00001", "(I)", "Raphael", "Warnock", "Warnock, Raphael", None, "D", "Unopposed", None, None, None, None])
    b = io.BytesIO(); wb.save(b)
    return b.getvalue()


def test_fec():
    cs = list(fec_contests(xls_sheets(_fec_xlsx(), "x.xlsx"), 2022, "u"))
    k = {(c["st"], c["office"], c["dist"], c["stage"], c["p"]): c for c in cs}
    rp = k[("GA", "U.S. House", "2", "primary", "R")]
    assert [x[0] for x in rp["o"]] == ["Jeremy Hunt", "Chris West", "Wayne Johnson"] and rp["o"][0][4] == "runoff" and rp["o"][1][4] == "runoff"
    assert k[("GA", "U.S. House", "2", "primary runoff", "R")]["o"][0][:5] == ["Chris West", "R", 14622, 0, "won"]
    assert k[("GA", "U.S. House", "2", "primary", "D")]["o"][0] == ["Sanford D. Bishop Jr.", "D", 58000, 1, "unopposed"]
    assert k[("CA", "U.S. House", "12", "primary", "")]["tot"] == 150           # top-two: one primary for every party
    assert k[("GA", "U.S. Senate", "", "primary", "D")]["o"][0][4] == "unopposed"
    assert rp["ids"]["Chris West"] == {"fec": "H2GA02999"}


class FakeNet:
    def __init__(self, routes):
        self.routes = routes

    def bytes(self, url, params=None):
        for k, v in self.routes.items():
            if (k[1:] == url) if k.startswith("=") else (k in url + json.dumps(params or {})):
                return v if isinstance(v, bytes) else v.encode()
        raise IOError("HTTP 404 from " + url)

    def text(self, url, params=None):
        return self.bytes(url, params).decode()

    def json(self, url, params=None):
        return json.loads(self.bytes(url, params))


def _zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


def test_run_build_verify(tmp_path, monkeypatch):
    cfg = {"data_dir": tmp_path / "data", "state_dir": tmp_path / "state"}
    ds = lambda fid: json.dumps({"data": {"latestVersion": {"versionNumber": 8, "versionMinorNumber": 0, "files": [
        {"label": "f.tab", "dataFile": {"id": fid, "filename": "f.tab", "originalFileName": "f.csv", "originalFileFormat": "text/csv"}},
        {"label": "codebook.md", "dataFile": {"id": 99, "filename": "codebook.md"}}]}}})
    state_xml = _clarity("GA", "County", [("Governor", 1, [("NATHAN DEAL (I)", "REP", {"Fulton": 60}), ("JASON CARTER", "DEM", {"Fulton": 40})])])
    routes = {
        "42MVDX": ds(1), "PEJ5QU": ds(2), "IG0UN2": ds(3), "datafile/1": PRES, "datafile/2": SENATE.replace("\t", ","), "datafile/3": HOUSE,
        "legislators-historical.json": json.dumps(LEGIS), "legislators-current.json": "[]",
        "git/trees": json.dumps({"tree": [
            {"path": "2006/20060718__ga__primary.csv", "type": "blob", "sha": "a" * 40},
            {"path": "2006/20060718__ga__primary__fulton__precinct.csv", "type": "blob", "sha": "b" * 40},
            {"path": "2006/20060808__ga__primary__runoff.csv", "type": "blob", "sha": "c" * 40},
            {"path": "1996/19961105__ga__general.csv", "type": "blob", "sha": "d" * 40},
            {"path": "2018/20181106__ga__general.csv", "type": "blob", "sha": "e" * 40}]}),
        "20060718__ga__primary.csv": OE_PRI, "20060808__ga__primary__runoff.csv": OE_RUN, "19961105__ga__general.csv": OE_GEN,
        "=https://api.github.com/repos/openelections/openelections-data-ga": json.dumps({"default_branch": "master"}),
        "historical-elections-results": '<a href="/sites/default/files/2026-04/november_4_2014_-_general_election.zip">November 4, 2014 - General Election.zip &gt;&gt;&gt;</a>'
                                        '<a href="/sites/default/files/2026-04/november_8_2016_-_general_election.zip">November 8, 2016 - General Election.zip</a>',
        "november_4_2014_-_general_election.zip": _zip({"detail.xml": state_xml, "readme.txt": "x"}),
        "election-results-and-voting-information": '<a href="/resources/cms-content/documents/federalelections2022.xlsx">Federal Elections 2022 (Excel)</a>',
        "federalelections2022.xlsx": _fec_xlsx(),
        "georgia-election-results": '<a href="https://sos.ga.gov/sites/default/files/2022-01/november_1988_generalelection_totals.pdf">General Election - November 8, 1988</a>',
        "Election-Statistics/Election-Statistics": '<a href="https://history.house.gov/Institution/Election-Statistics/1968election/">1968 Election Statistics</a>',
    }
    # the tree listing is matched before the repository record: order the lookups from the most specific
    routes = dict(sorted(routes.items(), key=lambda kv: -len(kv[0])))
    logs = []
    s = hrun.run(cfg, net=FakeNet(routes), log=logs.append)
    assert s["by_source"]["medsl"]["read"] == 3 and s["by_source"]["legislators"]["contests"] == 2
    assert s["by_source"]["openelections"]["read"] == 3, logs                 # 2018 is Electo's already; the precinct file gives way to the statewide one
    assert s["by_source"]["sos_zip"]["contests"] == 1 and s["by_source"]["fec"]["contests"] >= 4
    assert s["by_source"]["sos_pdf"]["failed"] == 1 and s["by_source"]["clerk"]["failed"] == 1      # nothing to download here
    st = json.loads((tmp_path / "state" / "history.json").read_text())
    assert sum(1 for r in st["items"].values() if r["status"] == "done") == 3 + 2 + 3 + 1 + 1
    again = hrun.run(cfg, net=FakeNet(routes), log=logs.append, only=["medsl", "openelections"])
    assert again["read"] == 0                                                  # nothing is read twice

    out = tmp_path / "bundle"
    res = hbuild.build(cfg, out, log=logs.append)
    idx = json.loads((out / "index.json").read_text())
    assert idx["years"]["ga"] == [1996, 2006, 2014] and 1968 in idx["years"]["us"] and 2022 in idx["years"]["us"]
    g06 = json.loads((out / "ga_2006.json").read_text())
    pri = next(e for e in g06["elections"] if e["kind"] == "primary")
    dem = next(c for c in pri["c"] if c["off"] == "Governor" and c["p"] == "D")
    assert [x[4] for x in dem["o"]] == ["runoff", "runoff", ""] and dem["ro"][1] == "2006-08-08"     # linked to the runoff file
    g96 = json.loads((out / "ga_1996.json").read_text())
    assert g96["elections"][0]["c"][0]["o"][0][4] == "won"                     # 48.9 percent won a general election in 1996
    u76 = json.loads((out / "us_1976.json").read_text())
    c5 = next(c for c in u76["elections"][0]["c"] if c.get("st") == "GA" and c["off"] == "U.S. House")
    assert c5["bg"] == {"Andrew Young": "Y000028"}
    u68 = json.loads((out / "us_1968.json").read_text())
    assert u68["elections"][0]["c"][0]["b"] == "seated" and u68["elections"][0]["c"][0]["tot"] is None
    k = "ANDREW YOUNG"
    ppl = json.loads((out / "people" / f"{shard(k, 64)}.json").read_text())
    assert ppl[k]["b"] == "Y000028" and len(ppl[k]["r"]) == 2 and ppl[k]["r"][0][0] == "us1972gen"
    prof = json.loads((out / "prof" / f"{shard('Y000028', 16)}.json").read_text())
    assert prof["Y000028"]["terms"][0]["st"] == "GA"
    sm = json.loads((out / "summary.json").read_text())
    assert sm["us"]["1976"]["U.S. House"]["seats"] == {"D": 2, "R": 1} and sm["ga"]["2014"]["statewide"]["Governor"]["w"] == "Nathan Deal"
    assert res["people"] > 10

    electo = tmp_path / "electo" / "hist"
    assert hbuild.verify(out, electo)["ok"] is False
    import shutil
    shutil.copytree(out, electo)
    assert hbuild.verify(out, electo)["ok"] is True
    (electo / "ga_2006.json").write_text("{}")
    assert hbuild.verify(out, electo)["differ"] == ["ga_2006.json"]
