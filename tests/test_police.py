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
    assert d == {"found": False, "checked_at": d["checked_at"], "home": "https://www.x.gov/"}


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
    (tmp_path / "state" / "police.json").write_text(json.dumps({"marietta": {"found": True, "kind": "police", "name": "Marietta Police Department", "url": "https://www.mariettaga.gov/police", "chief": {"name": "A B", "title": "Chief of Police", "src": "u"}, "phones": [], "emails": [], "address": None, "precincts": [], "checked_at": "2026-11-06T00:00:00+00:00"}, "nowhere": {"found": False}}))
    (tmp_path / "state" / "media.json").write_text(json.dumps({"ajc.com": {"found": True, "name": "AJC", "page": "p", "tips": [{"address": "tips@ajc.com", "src": "p", "label": "News tips"}], "checked_at": "x"}, "gone.com": {"found": False}}))
    (tmp_path / "state" / "discovered.json").write_text(json.dumps({"marietta": {"website": "https://www.mariettaga.gov/", "records": {"emails": ["clerk@mariettaga.gov"], "portal": None, "page": "https://www.mariettaga.gov/clerk"}}, "co-cobb": {"website": "https://www.cobbcounty.gov/", "records": {}}}))
    (tmp_path / "config" / "openrecords.yaml").write_text("approved: {co-cobb: records@cobbcounty.gov}\nskip: []\n")
    (tmp_path / "config" / "outlets.yaml").write_text("outlets: [ajc.com, gone.com]\nstatewide: [ajc.com]\nregions: {atlanta: {name: Metro Atlanta, anchor: [33.7, -84.4], outlets: [gone.com]}}\nnames: {ajc.com: The AJC}\n")
    out = tmp_path / "out"; out.mkdir()
    jur = [{"id": "marietta", "name": "Marietta", "kind": "city", "counties": ["Cobb"]}, {"id": "co-cobb", "name": "Cobb County", "kind": "county"}]
    n = police.export_policing(tmp_path, out, jur)
    assert n == {"departments": 1, "outlets": 1, "records_contacts": 2}
    M = json.loads((out / "media.json").read_text())
    assert M["outlets"][0]["name"] == "The AJC" and M["statewide"] == ["ajc.com"] and "atlanta" in M["regions"]
    P = json.loads((out / "police.json").read_text())["departments"][0]
    assert P["jurisdiction"] == "marietta" and P["county"] == "Cobb" and P["chief"]["name"] == "A B"
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
