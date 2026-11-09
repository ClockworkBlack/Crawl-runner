"""What the organizations that gave money are, from the registries' own files (gacrawl/registries.py): the FEC's
committee and candidate files, the State Ethics Commission's registrations and the IRS exempt-organization file.
Everything is served by a fake fetcher; fictional organizations and people throughout."""
import datetime as dt
import io
import json
import zipfile

from gacrawl import finance
from gacrawl import registries as R
from gacrawl.store import Record, Store

TODAY = dt.date(2026, 11, 9)


class Res:
    def __init__(self, url, status=200, body=b"", ctype="text/plain"):
        self.url, self.status, self.content = url, status, body if isinstance(body, bytes) else body.encode()
        self.headers, self.content_type = {"content-type": ctype}, ctype

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


def _zip(name, lines):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, "\n".join("|".join(f) for f in lines) + "\n")
    return b.getvalue()


def cm(cid, name, tres="", city="", st="", dsgn="U", tp="Q", party="", org_tp="", conn="", cand=""):
    return [cid, name, tres, "1 Main St", "", city, st, "30303", dsgn, tp, party, "Q", org_tp, conn, cand]


FEC_CM = [cm("C00000001", "GEORGIA REALTORS PAC", "POST, KIM", "ATLANTA", "GA", org_tp="T", conn="GEORGIA ASSOCIATION OF REALTORS"),
          cm("C00000002", "FRIENDS OF PAT LOCAL", "DOE, JANE Q JR", "CANTON", "GA", dsgn="P", tp="H", party="DEM", cand="H6GA11001"),
          cm("C00000003", "AMERICANS FOR PROGRESS", "ROE, SAM", "AUSTIN", "TX", conn="NONE"),
          cm("C00000004", "AMERICANS FOR PROGRESS", "ROE, SAM", "MACON", "GA", conn="NONE")]
FEC_CN = [["H6GA11001", "LOCAL, PAT", "DEM", "2026", "GA", "H", "11"]]

GA_COMMITTEES = {
    "peachfile": [{"filerName": "Civil Justice PAC", "filerType": "Political Action Committee", "filerStatus": "Active",
                   "filingCycleName": "2026", "committeeMailingCity": "ATLANTA", "committeeMailingStateCode": "GA",
                   "chairPerson": "SMITH, ANN", "treasurer": "Mr. BROWN, BOB", "filerEntityId": 11},
                  {"filerName": "Civil Justice PAC", "filerType": "Independent Committee", "filerStatus": "Active",
                   "filingCycleName": "2026", "filerEntityId": 12},
                  {"filerName": "It''s Time Georgia", "filerType": "Ballot Question Committee", "filerStatus": "Active",
                   "filingCycleName": "2026", "ballotQuestionName": "Amendment 1", "stance": "Support", "filerEntityId": 13}],
    "efile": [{"filerName": "Civil Justice PAC", "filerType": "Political Action Committee", "filerStatus": "Active",
               "filingCycleName": "2022", "filerEntityId": 900}]}
GA_CANDIDATES = {
    "peachfile": [{"committeeName": "Friends of Sam Doe", "filerName": "Doe, Sam", "office": "State House", "districtName": "District 5",
                   "politicalPartyCode": "REP", "filingCycleName": "2026", "filerEntityId": 21},
                  {"committeeName": "Friends of Sam Doe", "filerName": "Doe, Samantha", "office": "County Commission",
                   "filingCycleName": "2026", "filerEntityId": 22}],
    "efile": []}

IRS_HEAD = ("EIN,NAME,ICO,STREET,CITY,STATE,ZIP,GROUP,SUBSECTION,AFFILIATION,CLASSIFICATION,RULING,DEDUCTIBILITY,FOUNDATION,"
            "ACTIVITY,ORGANIZATION,STATUS,TAX_PERIOD,ASSET_CD,INCOME_CD,FILING_REQ_CD,PF_FILING_REQ_CD,ACCT_PD,ASSET_AMT,"
            "INCOME_AMT,REVENUE_AMT,NTEE_CD,SORT_NAME")


def irs_row(ein, name, city, sub="06", ruling="196501", rev="5000000", ntee="S41", tp="202412"):
    return f"{ein},{name},,1 Main St,{city},GA,30303,0000,{sub},3,1000,{ruling},2,00,000000000,1,01,{tp},5,5,01,0,12,1,1,{rev},{ntee},"


IRS_GA = "\n".join([IRS_HEAD,
                    irs_row("580000001", "GEORGIA ASSOCIATION OF REALTORS INC", "ATLANTA"),
                    irs_row("580000002", "LIONS CLUB", "MACON", sub="04", rev="12000", ntee="S80"),
                    irs_row("580000003", "LIONS CLUB", "ATHENS", sub="04", rev="9000", ntee="S80"),
                    irs_row("580000004", "HOPE FOUNDATION", "SAVANNAH", sub="03", rev="0", ntee="P20"),
                    irs_row("580000005", "HOPE FOUNDATION", "VALDOSTA", sub="03", rev="0", ntee="P20")]) + "\n"


class Fake:
    """The FEC's bulk files, the IRS file for Georgia and the State Ethics Commission's registry API."""

    def __init__(self, cycles_missing=(), irs_status=200, ga_min_size=0):
        self.gets, self.posts = [], []
        self.cycles_missing, self.irs_status, self.ga_min_size = set(cycles_missing), irs_status, ga_min_size

    def get(self, url, params=None, use_cache=True, kind=None, headers=None, **kw):
        self.gets.append(url)
        if "/bulk-downloads/" in url:
            y = int(url.split("/bulk-downloads/")[1].split("/")[0])
            if y in self.cycles_missing:
                return Res(url, 404)
            if "/cm" in url:
                return Res(url, 200, _zip("cm.txt", [f for f in FEC_CM if y == 2026 or f[0] != "C00000003"]), "application/zip")
            return Res(url, 200, _zip("cn.txt", FEC_CN), "application/zip")
        if url.endswith("eo_ga.csv"):
            return Res(url, self.irs_status, IRS_GA if self.irs_status == 200 else "", "text/csv")
        return Res(url, 404)

    def post(self, url, json_body=None, headers=None, **kw):
        self.posts.append((url, json_body))
        system = "efile" if "recordsearch" in url else "peachfile"
        if json_body["pageSize"] > self.ga_min_size > 0:
            return Res(url, 400, "page size too large")
        items = (GA_COMMITTEES if json_body.get("isCommittee") else GA_CANDIDATES)[system]
        size, page = json_body["pageSize"], json_body["pageNumber"]
        return Res(url, 200, json.dumps({"data": {"items": items[(page - 1) * size: page * size], "totalItems": len(items)}}),
                   "application/json")


def test_names_keys_and_placeholders():
    assert R.clean("It''s  Time") == "It's Time" and R.reg_key("It's Time, Georgia, Inc.") == R.reg_key("ITS TIME GEORGIA INC")
    assert R.reg_key("Ameris Bank") != R.reg_key("Ameris Bank Foundation")
    assert R.person("PHILLIPS, JUSTIN") == "Justin Phillips" and R.person("Mr. BROWN, BOB") == "Bob Brown"
    assert R.person("DOE, JANE Q JR") == "Jane Q Doe Jr"
    assert R.connected("NONE") == "" and R.connected("N/A") == "" and R.connected("Georgia Association of Realtors")


def test_fec_files():
    fec = R.FecFiles(Fake(), today=TODAY, log=lambda *_: None).load()
    assert fec.ok and fec.cycles == [2016, 2018, 2020, 2022, 2024, 2026]
    c = fec.match(["Georgia Realtors PAC"], [], "GA")
    assert c["id"] == "C00000001" and fec.match(["Somebody Else"], ["C00000001"])["id"] == "C00000001"   # its lines' FEC id first
    assert fec.match(["Americans for Progress"], [], "GA")["id"] == "C00000004"        # same name: the donor's state
    what, officers, fields = fec.facts(c)
    assert "trade association" in what[0]["text"] and "2016 to 2026" in what[1]["text"] and "Atlanta, GA" in what[1]["text"]
    assert any(w["text"] == "Connected organization: GEORGIA ASSOCIATION OF REALTORS" for w in what)
    assert officers == [{"name": "Kim Post", "role": "treasurer", "src": c["src"]}] and fields["connected_org"]
    what, officers, fields = fec.facts(fec.match(["Friends of Pat Local"], [], "GA"))
    assert any("campaign committee of Pat Local, a candidate for U.S. House (GA-11)" in w["text"] for w in what)
    assert officers[0]["name"] == "Jane Q Doe Jr"
    assert fec.facts(fec.match(["Americans for Progress"], [], "GA"))[2]["connected_org"] == ""        # "NONE" is no org
    # two cycles' files missing: not usable this run (an organization is then looked up again next run)
    assert not R.FecFiles(Fake(cycles_missing={2024, 2026}), today=TODAY, log=lambda *_: None).load().ok


def test_state_ethics_commission_registry(tmp_path):
    s, f = Store(tmp_path / "x.db"), Fake(ga_min_size=50)
    rows = R.ga_registry(f, s, log=lambda *_: None)
    assert len(rows) == 6 and json.loads(s.kv_get("ga_registry"))["rows"] == rows
    assert any(b["pageSize"] == 50 for _, b in f.posts)                           # smaller pages when 100 is refused
    n = len(f.posts)
    assert R.ga_registry(f, s, log=lambda *_: None) == rows and len(f.posts) == n  # a week's cache
    ga = R.GaRegistry(rows)
    m = ga.match(["Civil Justice PAC"])
    assert {(r["sys"], r["type"]) for r in m} == {("peachfile", "Political Action Committee"), ("efile", "Political Action Committee")}
    what, officers, fields = R.GaRegistry.facts(m)
    assert what[0]["text"].startswith("Registered with the Georgia State Ethics Commission as a political action committee (active)")
    assert "2022 to 2026" in what[0]["text"] and {o["name"] for o in officers} == {"Ann Smith", "Bob Brown"}
    assert ga.match(["Friends of Sam Doe"]) == []                                # two candidates' committees: none chosen
    bq = ga.match(["It's Time Georgia"])
    assert bq and "Registered to support Amendment 1" in [w["text"] for w in R.GaRegistry.facts(bq)[0]]


def test_irs_file(tmp_path):
    s, f = Store(tmp_path / "x.db"), Fake()
    irs = R.IrsFiles(f, s, log=lambda *_: None)
    keys = {R.reg_key(n) for n in ("Georgia Association of Realtors, Inc.", "Lions Club", "Hope Foundation", "Acme Development, LLC")}
    irs.prepare({"GA": keys, "ZZ": {"x"}})
    assert f.gets == ["https://www.irs.gov/pub/irs-soi/eo_ga.csv"] and irs.looked_for(R.reg_key("Lions Club"), "GA")
    r = irs.match(["Georgia Association of Realtors, Inc."], "GA")
    what, fields = R.IrsFiles.facts(r)
    assert fields["ein"] == "580000001" and "501(c)(6)" in what[0]["text"] and "since 1965" in what[0]["text"]
    assert "revenue of $5,000,000" in what[-1]["text"]
    assert irs.match(["Lions Club"], "GA", "Macon")["EIN"] == "580000002"          # chapters: the donor's city
    assert irs.match(["Lions Club"], "GA") is None                                 # both file with revenue: none chosen
    assert irs.match(["Hope Foundation"], "GA") is None                            # two, neither with revenue: none chosen
    assert irs.match(["Acme Development, LLC"], "GA") is None
    # read once a week, and only for names not looked for yet
    R.IrsFiles(f, s, log=lambda *_: None).prepare({"GA": keys})
    assert len(f.gets) == 1


def _store_with_donors(tmp_path):
    s = Store(tmp_path / "data")
    lines = [("Georgia Realtors PAC", 5000, {"contributor_committee_id": "C00000001", "state": "GA", "city": "Atlanta"}),
             ("Lions Club", 300, {"state": "GA", "city": "Macon"}),
             ("Civil Justice PAC", 2500, {}),
             ("Acme Development, LLC", 1000, {"state": "GA", "city": "Canton"})]
    for i, (name, amt, extra) in enumerate(lines):
        s.add(Record("cand:pat-local", "contribution", f"https://e.com/{i}", title=f"{name} to Pat Local",
                     key=f"t|{i}", meta={"name": name, "amount": float(amt), "type": "contribution", "donor_is_org": True,
                                         "recipient": "Pat Local", **extra}))
    s.flush()
    return s


def test_enrich_orgs_from_the_files(tmp_path, monkeypatch):
    monkeypatch.delenv("FEC_API_KEY", raising=False)
    monkeypatch.delenv("OPENCORPORATES_API_KEY", raising=False)
    monkeypatch.setattr(R.FecFiles, "__init__", lambda self, fetcher, today=None, log=print, _i=R.FecFiles.__init__:
                        _i(self, fetcher, today=TODAY, log=log))
    s, f = _store_with_donors(tmp_path), Fake()
    fin = finance.Finance(tmp_path, {}, f, s, log=lambda *_: None)
    fin.resolve_all([])
    out = fin.enrich_orgs()
    assert out["due"] == 4 and out["done"] == 4 and (out["fec"], out["ga"], out["irs"]) == (1, 1, 1)
    orgs = {o["title"]: o["meta"] for o in s.query("kind='organization'")}
    pac = orgs["Georgia Realtors PAC"]
    assert pac["fec_id"] == "C00000001" and pac["registries"] == ["fec"] and pac["treasurer"] == "Kim Post"
    assert pac["given"] == 5000.0 and pac["org_kind"] == "committee"
    assert any(w["text"].startswith("Connected organization GEORGIA ASSOCIATION OF REALTORS: Recognized by the IRS") for w in pac["what"])
    assert orgs["Lions Club"]["ein"] == "580000002" and orgs["Lions Club"]["org_kind"] == "nonprofit"
    assert orgs["Civil Justice PAC"]["registries"] == ["ga_ethics"]
    assert orgs["Acme Development, LLC"]["registries"] == [] and orgs["Acme Development, LLC"]["org_kind"] == "unknown"
    # every registry was read: all four are looked up, and none is due again until the month is out
    assert fin.enrich_orgs()["due"] == 0


def test_enrich_orgs_tries_again_when_a_registry_could_not_be_read(tmp_path, monkeypatch):
    monkeypatch.delenv("FEC_API_KEY", raising=False)
    monkeypatch.delenv("OPENCORPORATES_API_KEY", raising=False)
    monkeypatch.setattr(R.FecFiles, "__init__", lambda self, fetcher, today=None, log=print, _i=R.FecFiles.__init__:
                        _i(self, fetcher, today=TODAY, log=log))
    monkeypatch.setattr(finance.Finance, "_enrich_propublica", lambda self, info, *a, **k: None)
    s = _store_with_donors(tmp_path)
    fin = finance.Finance(tmp_path, {}, Fake(irs_status=503), s, log=lambda *_: None)
    fin.resolve_all([])
    assert fin.enrich_orgs()["done"] == 4
    assert fin.enrich_orgs()["due"] == 4          # the IRS file was not read: everyone is looked up again next run
