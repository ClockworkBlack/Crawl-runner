"""Police / media parsing: chiefs, phones, emails, addresses and precincts as official pages print them."""
import json
from pathlib import Path

from gacrawl import police


HOME = """<html><body><nav><a href="/departments/police">Police Department</a><a href="/fire">Fire</a>
<a href="https://www.facebook.com/x">Police on Facebook</a><a href="/news">News</a></nav></body></html>"""
DEPT = """<html><head><title>Police Department | City of Example</title></head><body>
<h1>Example Police Department</h1>
<p>Chief of Police Jane Q. Doe leads 120 sworn officers.</p>
<p>Non-Emergency: (770) 555-0100<br>Records: 770-555-0101</p>
<p>Email: <a href="mailto:police@examplega.gov">police@examplega.gov</a> · webmaster@notourdomain.com</p>
<p>240 Main Street, Example, GA 30000</p>
<ul><li><a href="/departments/police/precincts">Precincts</a></li><li><a href="/departments/police/internal-affairs">Internal Affairs</a></li>
<li><a href="/departments/police/records">Records Unit</a></li><li><a href="/departments/police/chief">Office of the Chief</a></li></ul>
</body></html>"""
PRECINCTS = """<html><body><h1>Precincts</h1>
<h3>North Precinct</h3><p>100 North Ave, Example, GA 30001</p><p>Phone: 770-555-0111</p>
<h3>South Precinct</h3><p>200 South Blvd SW, Example, GA 30002</p><p>770-555-0122</p>
<h3>Voting Precinct 4</h3><p>5 Poll Rd, Example, GA 30003</p>
</body></html>"""
IA = """<html><body><h1>Internal Affairs</h1><p>To file a complaint email <a href="mailto:ia@examplega.gov">ia@examplega.gov</a> or call 770-555-0199.</p>
<p>Captain John Roe commands the unit.</p></body></html>"""


class R:
    def __init__(self, url, text, status=200):
        self.url, self.text, self.status, self.content_type = url, text, status, "text/html"


class F:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        return R(url, self.pages.get(url, ""), 200 if url in self.pages else 404)


def test_department_read_from_official_links():
    site = "https://www.examplega.gov/"
    f = F({site: HOME, site + "departments/police": DEPT, site + "departments/police/precincts": PRECINCTS,
           site + "departments/police/internal-affairs": IA, site + "departments/police/records": "<html><body><h1>Records</h1><p>records@examplega.gov · 770-555-0101</p></body></html>",
           site + "departments/police/chief": "<html><body><h1>Office of the Chief</h1><p>Jane Q. Doe</p><p>Chief of Police</p></body></html>"})
    d = police.read_department(f, {"id": "example", "name": "Example"}, site, log=lambda *a: None)
    assert d["found"] and d["kind"] == "police" and d["name"] == "Example Police Department"
    assert d["chief"]["name"] == "Jane Q. Doe" and d["chief"]["title"] == "Chief of Police" and d["chief"]["src"].endswith("/chief")
    nums = {p["label"]: p["number"] for p in d["phones"]}
    assert nums["Non-emergency"] == "770-555-0100" and nums["Records"] == "770-555-0101"
    assert [e["address"] for e in d["emails"]][:2] == ["police@examplega.gov", "ia@examplega.gov"]
    assert all("notourdomain" not in e["address"] for e in d["emails"])
    assert d["address"]["text"] == "240 Main Street, Example, GA 30000"
    names = [p["name"] for p in d["precincts"]]
    assert names == ["North Precinct", "South Precinct"]       # the voting precinct is not a police precinct
    assert d["precincts"][1]["address"] == "200 South Blvd SW, Example, GA 30002" and d["precincts"][1]["phone"] == "770-555-0122"
    assert d["complaints"]["url"].endswith("/internal-affairs") and d["complaints"]["emails"] == ["ia@examplega.gov"]
    assert d["records"]["emails"] == ["records@examplega.gov"]
    assert "https://www.facebook.com/x" not in f.calls and len(f.calls) <= 6


def test_no_police_link_means_not_found():
    f = F({"https://www.x.gov/": "<html><body><a href='/fire'>Fire</a></body></html>"})
    d = police.read_department(f, {"id": "x", "name": "X"}, "https://www.x.gov/")
    assert d == {"found": False, "checked_at": d["checked_at"], "home": "https://www.x.gov/", "v": police.POLICE_VERSION}


def test_sheriff_title_forms():
    lines = ["Sheriff", "Robert Smith", "Welcome to the Sheriff's Office", "Chief Deputy Alan Jones"]
    c = police.find_chief(lines)
    assert c == {"name": "Robert Smith", "title": "Sheriff"}
    assert police.find_chief(["Deputy Chief Al Jones"]) is None
    assert police.find_chief(["Interim Police Chief Maria Lopez-Garcia"]) == {"name": "Maria Lopez-Garcia", "title": "Interim Police Chief"}


def test_media_tips_from_outlet_contact_page():
    f = F({"https://www.example-news.com/": "<html><head><title>Example News | Georgia</title></head><body><a href='/contact-us'>Contact Us</a></body></html>",
           "https://www.example-news.com/contact-us": "<html><body>Send news tips to <a href='mailto:tips@example-news.com'>tips@example-news.com</a>; advertising: ads@example-news.com; partner: tips@other.com</body></html>"})
    o = police.read_outlet(f, "example-news.com")
    assert o["found"] and o["name"] == "Example News" and [t["address"] for t in o["tips"]] == ["tips@example-news.com"]
    assert o["tips"][0]["label"] == "News tips" and o["page"] == "https://www.example-news.com/contact-us"


def test_export_shapes(tmp_path: Path):
    (tmp_path / "state").mkdir(); (tmp_path / "config").mkdir()
    (tmp_path / "state" / "police.json").write_text(json.dumps({"marietta": {"found": True, "kind": "police", "name": "Marietta Police Department", "url": "https://www.mariettaga.gov/police", "chief": {"name": "Ann Brown", "title": "Chief of Police", "src": "u"}, "phones": [], "emails": [], "address": None, "precincts": [], "checked_at": "2026-11-06T00:00:00+00:00"}, "nowhere": {"found": False}}))
    (tmp_path / "state" / "media.json").write_text(json.dumps({"ajc.com": {"found": True, "name": "AJC", "page": "p", "tips": [{"address": "tips@ajc.com", "src": "p", "label": "News tips"}], "checked_at": "x"}, "gone.com": {"found": False}}))
    (tmp_path / "state" / "discovered.json").write_text(json.dumps({"marietta": {"website": "https://www.mariettaga.gov/", "records": {"emails": ["clerk@mariettaga.gov"], "portal": None, "page": "https://www.mariettaga.gov/clerk"}}, "co-cobb": {"website": "https://www.cobbcounty.gov/", "records": {}}}))
    (tmp_path / "config" / "openrecords.yaml").write_text("approved: {co-cobb: records@cobbcounty.gov}\nskip: []\n")
    (tmp_path / "config" / "outlets.yaml").write_text("outlets: [ajc.com, gone.com]\nstatewide: [ajc.com]\nregions: {atlanta: {name: Metro Atlanta, anchor: [33.7, -84.4], outlets: [gone.com]}}\nnames: {ajc.com: The AJC}\n")
    out = tmp_path / "out"; out.mkdir()
    jur = [{"id": "marietta", "name": "Marietta", "kind": "city", "counties": ["Cobb"]}, {"id": "co-cobb", "name": "Cobb County", "kind": "county"}]
    n = police.export_policing(tmp_path, out, jur)
    assert n == {"departments": 1, "from_pages": 1, "listed_only": 0, "others": 0, "outlets": 1, "records_contacts": 2}
    M = json.loads((out / "media.json").read_text())
    assert M["outlets"][0]["name"] == "The AJC" and M["statewide"] == ["ajc.com"] and "atlanta" in M["regions"]
    P = json.loads((out / "police.json").read_text())["departments"][0]
    assert P["jurisdiction"] == "marietta" and P["county"] == "Cobb" and P["chief"]["name"] == "Ann Brown"
    C = json.loads((out / "records_contacts.json").read_text())["contacts"]
    assert C["marietta"]["emails"] == ["clerk@mariettaga.gov"] and C["co-cobb"]["emails"] == ["records@cobbcounty.gov"]


def test_incident_names_and_excerpt():
    from gacrawl import policing_news as pn
    text = ("Marietta police said Officer John Doe shot a man during a traffic stop on Cherokee Street. Chief of Police Jane Q. Doe "
            "placed Doe on leave; Sgt. Al B. Smith, a supervisor, was also named. The GBI is investigating. Monday Morning is not a name.")
    names = pn.names_in(text)
    assert [n["name"] for n in names] == ["John Doe", "Jane Q. Doe", "Al B. Smith"]
    assert names[0]["rank"] == "Officer" and names[1]["rank"] == "Chief of Police"
    ex = pn.excerpt_of(text, "Marietta Police Department", 120)
    assert "shot" in ex and len(ex) <= 125
    assert pn._mentions_department("Cobb County police arrested a man", {"name": "Cobb County Police Department", "kind": "police"}, "Cobb County")
    assert not pn._mentions_department("The Cobb County school board met", {"name": "Cobb County Police Department", "kind": "police"}, "Cobb County")


def test_export_incidents(tmp_path: Path):
    from gacrawl import policing_news as pn
    from gacrawl.store import Record, Store
    st = Store(tmp_path / "data")
    st.add(Record("police:marietta", "police_incident", "https://www.ajc.com/x1", title="Officer named in lawsuit", text="…", published="2026-10-02",
                  channel="html", jurisdiction="marietta", evidence="reported", meta={"department": "Marietta Police Department", "department_kind": "police", "outlet": "ajc.com", "names": [{"name": "John Doe", "rank": "Officer"}], "cues": ["lawsuit"]}))
    st.add(Record("police:marietta", "police_incident", "https://www.ajc.com/x2", title="Chief comments", text="…", published="2026-11-01",
                  channel="html", jurisdiction="marietta", evidence="reported", meta={"department": "Marietta Police Department", "department_kind": "police", "outlet": "ajc.com", "names": [{"name": "John Doe", "rank": "Sgt."}, {"name": "Jane Q. Doe", "rank": "Chief"}], "cues": ["fired"]}))
    st.flush()
    out = tmp_path / "police_incidents.json"
    n = pn.export_incidents(st, out, [{"id": "marietta", "name": "Marietta"}])
    st.close()
    assert n == {"departments": 1, "incidents": 2}
    D = json.loads(out.read_text())["departments"][0]
    assert D["incidents"][0]["d"] == "2026-11-01" and D["people"][0]["name"] == "John Doe" and D["people"][0]["mentions"] == 2
    assert sorted(D["people"][0]["ranks"]) == ["Officer", "Sgt."]


# ----------------------------------------------------------------------------- department discovery (version 3)
class Robots:
    def __init__(self, maps):
        self.maps = maps

    def sitemaps(self, url):
        return self.maps


class FR(F):
    """The fake fetcher with a robots.txt that names sitemaps, and XML answers."""
    def __init__(self, pages, maps=()):
        super().__init__(pages)
        self.robots = Robots(list(maps))

    def get(self, url, **kw):
        r = super().get(url, **kw)
        r.content = (self.pages.get(url) or "").encode()
        if url.endswith(".xml"):
            r.content_type = "application/xml"
        return r


SHERIFF_HOME = """<html><head><title>Cobb County Sheriff's Office</title></head><body><h1>Cobb County Sheriff's Office</h1>
<p>Sheriff Craig Owens</p><p>Main: 770-499-4600</p><a href="/contact">Contact the Sheriff's Office</a></body></html>"""


def test_county_keeps_its_sheriff_and_its_police():
    """A county with both keeps both (Cobb, Gwinnett, DeKalb, Chatham...); the sheriff's own .org site is followed
    because the county's page links to it and it names the county."""
    site = "https://www.cobbcounty.gov/"
    home = ("<html><body><a href='/police'>Police Department</a> <a href='/police/file-a-report'>File a Police Report</a>"
            "<a href='https://www.cobbsheriff.org/'>Sheriff's Office</a><a href='https://www.crimereports.com/x'>Crime map police</a></body></html>")
    f = F({site: home, site + "police": "<html><body><h1>Cobb County Police Department</h1><p>Chief of Police Stuart VanHoozer</p></body></html>",
           "https://www.cobbsheriff.org/": SHERIFF_HOME})
    j = {"id": "co-cobb", "name": "Cobb County", "kind": "county"}
    d = police.read_department(f, j, site)
    kinds = [d["kind"]] + [x["kind"] for x in d.get("also", [])]
    assert kinds == ["sheriff", "police"]
    assert d["name"] == "Cobb County Sheriff's Office" and d["chief"]["name"] == "Craig Owens" and d["chief"]["title"] == "Sheriff"
    assert d["also"][0]["url"] == site + "police" and d["also"][0]["chief"]["name"] == "Stuart VanHoozer"
    assert site + "police/file-a-report" not in f.calls and not any("crimereports" in u for u in f.calls)


def test_a_city_does_not_take_the_county_sheriff():
    site = "https://www.examplega.gov/"
    home = "<html><body><a href='https://www.examplecountysheriff.com/'>Sheriff</a></body></html>"
    f = F({site: home, "https://www.examplecountysheriff.com/": "<html><body><h1>Example County Sheriff</h1><p>Example</p></body></html>"})
    d = police.read_department(f, {"id": "example", "name": "Example", "kind": "city", "counties": ["Example"]}, site)
    assert d["found"] is False


def test_department_index_page_when_the_home_page_links_none():
    site = "https://www.townga.gov/"
    f = F({site: "<html><body><a href='/departments'>Departments</a><a href='/news'>News</a></body></html>",
           site + "departments": "<html><body><ul><li><a href='/departments/police'>Police</a></li><li><a href='/departments/fire'>Fire</a></li></ul></body></html>",
           site + "departments/police": "<html><body><h1>Town Police Department</h1><p>Non-emergency: 706-555-0100</p></body></html>"})
    d = police.read_department(f, {"id": "town", "name": "Town", "kind": "city"}, site)
    assert d["found"] and d["url"] == site + "departments/police" and d["phones"][0]["number"] == "706-555-0100"


def test_sitemap_named_in_robots_when_the_menu_is_built_by_scripts():
    """cobbcounty.gov's home page has no department links in its HTML; the sitemap its robots.txt names lists /police."""
    site = "https://www.cobbcounty.gov/"
    index = ("<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
             "<sitemap><loc>https://www.cobbcounty.gov/sitemap/event/0.xml</loc></sitemap>"
             "<sitemap><loc>https://www.cobbcounty.gov/sitemap/department/0.xml</loc></sitemap></sitemapindex>")
    deps = ("<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            "<url><loc>https://www.cobbcounty.gov/fire</loc></url><url><loc>https://www.cobbcounty.gov/police</loc></url></urlset>")
    f = FR({site: "<html><body><a href='/news'>News</a></body></html>", site + "sitemap.xml": index,
            site + "sitemap/department/0.xml": deps,
            site + "police": "<html><body><h1>Cobb County Police Department</h1><p>Cobb County</p></body></html>"}, maps=[site + "sitemap.xml"])
    d = police.read_department(f, {"id": "co-cobb", "name": "Cobb County", "kind": "county"}, site)
    assert d["found"] and d["kind"] == "police" and d["url"] == site + "police"
    assert site + "sitemap/event/0.xml" not in f.calls          # event sitemaps are never read


def test_section_pages_skip_the_county_wide_header():
    """On a county's site, "Contact Us" in the site-wide header is the county's (Fulton's purchasing office's phones
    were read as the police department's); the department's own links are read."""
    site = "https://www.fultoncountyga.gov/"
    dept = ("<html><body><header><a href='/contact-us'>Contact Us</a><a href='/about'>About</a></header>"
            "<main><h1>Fulton County Police Department</h1><p>Chief of Police Wade Yates</p>"
            "<a href='/departments/police/precincts'>Precincts</a></main></body></html>")
    f = F({site: "<html><body><a href='/departments/police'>Police</a></body></html>", site + "departments/police": dept,
           site + "contact-us": "<html><body><p>Purchasing: 404-612-5800</p></body></html>",
           site + "departments/police/precincts": PRECINCTS})
    d = police.read_department(f, {"id": "co-fulton", "name": "Fulton County", "kind": "county"}, site)
    assert d["kind"] == "police" and site + "contact-us" not in f.calls and site + "about" not in f.calls
    assert [p["name"] for p in d["precincts"]] == ["North Precinct", "South Precinct"]
    assert all(p["number"] != "404-612-5800" for p in d["phones"])


def test_chief_names_are_names():
    assert police.find_chief(["Jr. Fire Chief"]) is None
    assert police.find_chief(["Fire Chief Jr. Fire"]) is None
    assert not police._name_ok("Jr. Fire") and not police._name_ok("Animal Control") and not police._name_ok("Q. X.")
    assert police._name_ok("Leslie Manahan") and police._name_ok("Gerald W. Couch") and police._name_ok("Jane Q. Doe")


def test_staleness_rules():
    old = (police.dt.datetime.now(police.dt.timezone.utc) - police.dt.timedelta(days=8)).isoformat()
    new = police._now()
    assert police._stale({"found": True, "checked_at": new, "v": police.POLICE_VERSION - 1}, 30)       # older reader
    assert police._stale({"found": False, "checked_at": new}, 30)                                      # never versioned
    assert police._stale({"found": False, "checked_at": old, "v": police.POLICE_VERSION}, 30)          # nothing found, a week on
    assert not police._stale({"found": True, "checked_at": old, "v": police.POLICE_VERSION}, 30)
    # outlets carry no version and are not read again every run
    assert not police._stale({"found": True, "checked_at": new}, 30, versioned=False)


ROSTER = {"COBB": [{"ori": "GA0330000", "counties": "COBB", "agency_name": "Cobb County Sheriff's Office", "agency_type_name": "County", "latitude": 33.9, "longitude": -84.5},
                   {"ori": "GA0330200", "counties": "COBB", "agency_name": "Cobb County Police Department", "agency_type_name": "County"},
                   {"ori": "GA0330100", "counties": "COBB", "agency_name": "Marietta  Police Department", "agency_type_name": "City"},
                   {"ori": "GA0331Y00", "counties": "COBB", "agency_name": "Kennesaw State University", "agency_type_name": "University or College"},
                   {"ori": "GA033SP00", "counties": "COBB", "agency_name": "State Patrol: Marietta", "agency_type_name": "State Police"}],
          "CLARKE": [{"ori": "GA0290100", "counties": "CLARKE", "agency_name": "Athens-Clarke County Police Department", "agency_type_name": "City"},
                     {"ori": "GA0290000", "counties": "CLARKE", "agency_name": "Clarke County Sheriff's Office", "agency_type_name": "County"}],
          "DEKALB": [{"ori": "GA0440000", "counties": "DEKALB", "agency_name": "DeKalb County Marshal", "agency_type_name": "Other"},
                     {"ori": "GA0330100", "counties": "COBB, DOUGLAS", "agency_name": "Marietta  Police Department", "agency_type_name": "City"}]}
JUR = [{"id": "co-cobb", "name": "Cobb County", "kind": "county"}, {"id": "co-clarke", "name": "Clarke County", "kind": "county"},
       {"id": "co-dekalb", "name": "DeKalb County", "kind": "county"},
       {"id": "marietta", "name": "Marietta", "kind": "city", "counties": ["Cobb"]},
       {"id": "athens-clarke", "name": "Athens", "kind": "consolidated", "counties": ["Clarke"], "consolidated_with": "Clarke County"}]


def test_roster_parse_and_match():
    rows = police.parse_roster(ROSTER)
    assert len(rows) == 8 and next(r for r in rows if r["ori"] == "GA0330100")["name"] == "Marietta Police Department"
    depts, others = police.roster_departments(rows, JUR)
    got = {(d["jurisdiction"], d["kind"]): d["name"] for d in depts}
    assert got == {("co-cobb", "sheriff"): "Cobb County Sheriff's Office", ("co-cobb", "police"): "Cobb County Police Department",
                   ("marietta", "police"): "Marietta Police Department", ("athens-clarke", "police"): "Athens-Clarke County Police Department",
                   ("co-clarke", "sheriff"): "Clarke County Sheriff's Office"}
    assert {(o["county"], o["kind"]) for o in others} == {("Cobb", "campus"), ("Cobb", "state_patrol"), ("DeKalb", "marshal")}


def test_department_list_merges_pages_and_roster(tmp_path: Path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "police.json").write_text(json.dumps({
        "marietta": {"found": True, "kind": "police", "name": "Marietta Police Department", "name_from": "the department's page",
                     "url": "https://www.mariettaga.gov/police", "v": police.POLICE_VERSION, "checked_at": "2026-11-13T00:00:00+00:00"},
        "athens-clarke": {"found": True, "kind": "police", "name": "Athens-Clarke County Police", "url": "https://www.accgov.com/police", "v": police.POLICE_VERSION,
                          "also": [{"found": True, "kind": "sheriff", "name": "Clarke County Sheriff's Office", "url": "https://www.accgov.com/sheriff", "v": police.POLICE_VERSION}]}}))
    (tmp_path / "state" / "police_roster.json").write_text(json.dumps({"as_of": police._now(), "agencies": police.parse_roster(ROSTER)}))
    pairs, others, counts = police.department_list(tmp_path, JUR)
    got = {(j["id"], d["kind"]): d for j, d in pairs}
    assert set(got) == {("marietta", "police"), ("athens-clarke", "police"), ("co-clarke", "sheriff"), ("co-cobb", "sheriff"), ("co-cobb", "police")}
    assert got[("marietta", "police")]["ori"] == "GA0330100" and got[("marietta", "police")]["url"]          # read from its pages
    assert got[("co-clarke", "sheriff")]["url"] == "https://www.accgov.com/sheriff"                         # the county's sheriff
    assert got[("co-cobb", "sheriff")]["listed_by"] == "fbi" and not got[("co-cobb", "sheriff")]["url"]     # listed only
    assert counts == {"from_pages": 3, "listed_only": 2, "others": 3, "roster": 8}


def test_county_and_consolidated_government_share_pages_once(tmp_path: Path):
    (tmp_path / "state").mkdir()
    v = police.POLICE_VERSION
    (tmp_path / "state" / "police.json").write_text(json.dumps({
        "co-clarke": {"found": True, "kind": "police", "name": "Athens-Clarke County Police", "url": "https://www.accgov.com/police", "v": v,
                      "also": [{"found": True, "kind": "sheriff", "name": "Clarke County Sheriff's Office", "url": "https://www.accgov.com/sheriff", "v": v}]},
        "athens-clarke": {"found": True, "kind": "police", "name": "Athens-Clarke County Police", "url": "https://www.accgov.com/police/", "v": v,
                          "also": [{"found": True, "kind": "sheriff", "name": "Clarke County Sheriff's Office", "url": "https://www.accgov.com/sheriff", "v": v}]}}))
    pairs, _, _ = police.department_list(tmp_path, JUR)
    assert sorted((j["id"], d["kind"]) for j, d in pairs) == [("athens-clarke", "police"), ("co-clarke", "sheriff")]


def test_news_search_covers_every_department(tmp_path: Path, monkeypatch):
    """The news search covers the departments read from their own pages and the FBI-listed ones (a search needs only
    a name and a place), each once a week, a county's sheriff and its police separately."""
    from gacrawl import policing_news as pn
    from gacrawl.store import Store

    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "outlets.yaml").write_text("outlets: []\n")
    (tmp_path / "state" / "police.json").write_text(json.dumps({
        "marietta": {"found": True, "kind": "police", "name": "Marietta Police Department", "url": "https://www.mariettaga.gov/police", "v": police.POLICE_VERSION}}))
    (tmp_path / "state" / "police_roster.json").write_text(json.dumps({"as_of": police._now(), "agencies": police.parse_roster(ROSTER)}))
    (tmp_path / "state" / "police_news.json").write_text(json.dumps({"_version": pn.NEWS_VERSION, "marietta": police._now()}))
    calls = []
    monkeypatch.setattr(pn.PoliceNews, "department", lambda self, jid, d, name, j=None: calls.append((jid, d["kind"], d["name"])) or 0)
    monkeypatch.setattr("gacrawl.registry.jurisdictions", lambda root: JUR)
    st = Store(tmp_path / "data")
    out = pn.research(None, st, tmp_path, tmp_path / "state")
    st.close()
    assert ("co-cobb", "sheriff", "Cobb County Sheriff's Office") in calls and ("co-cobb", "police", "Cobb County Police Department") in calls
    assert not any(c[0] == "marietta" for c in calls)            # searched this week (keyed by its place before)
    seen = json.loads((tmp_path / "state" / "police_news.json").read_text())
    assert "co-cobb:sheriff" in seen and "co-cobb:police" in seen and out["departments"] == len(calls)


def test_a_failed_read_keeps_the_department_read_before(tmp_path: Path):
    (tmp_path / "police.json").write_text(json.dumps({"x": {"found": True, "kind": "police", "name": "X Police Department",
                                                              "url": "https://www.x.gov/police", "checked_at": "2026-10-01T00:00:00+00:00", "v": 2}}))

    class Down:
        def get(self, url, **kw):
            raise ConnectionError("down")

    n = police.discover_police(Down(), [{"id": "x", "name": "X", "kind": "city", "website": "https://www.x.gov/"}], tmp_path)
    d = json.loads((tmp_path / "police.json").read_text())["x"]
    assert n == 1 and d["found"] and d["url"] == "https://www.x.gov/police" and "down" in d["last_error"]
