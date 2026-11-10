"""codefinder: a deeper look at a place's own site for its clerk address and posted code, with a fake fetcher.
The cases come from the first real run: a front door that redirects to another domain, a "Download" link to a
rezoning application, a request form, a county site carrying its seat city's ordinance and linking the city's
Municode code, and a code posted chapter by chapter."""
import csv
import json
from pathlib import Path

from gacrawl import codefinder as cf
from gacrawl import openrecords as orr
from gacrawl.fetch import FetchResult

PDF = b"%PDF-1.4 code of ordinances"
SITE = {
    "https://smallton.ga.us/": ("text/html", '<a href="/government/city-clerk">City Clerk</a> <a href="/news">News</a>'
                                '<a href="https://facebook.com/smallton">fb</a> info@smallton.ga.us'),
    "https://smallton.ga.us/government/city-clerk": (
        "text/html", '<title>City Clerk</title> Contact <a href="mailto:clerk@smallton.ga.us">the clerk</a> or '
                     'someone@example.com. <a href="/docs/Code%20of%20Ordinances%202024.pdf">Code of Ordinances</a> '
                     '<a href="/docs/agenda-ordinance-hearing.pdf">Agenda</a> '
                     '<a href="/docs/Zoning%20Ordinance.pdf">Zoning Ordinance</a> <a href="/docs/x.pdf">Budget</a> '
                     '<a href="/docs/rezoning-application.pdf">Download</a> '
                     '<a href="/docs/ZoningChange.pdf">Zoning Change Request</a> '
                     '<a href="/docs/Zoning-Calendar.pdf">Zoning and Planning Commission</a>'),
    "https://smallton.ga.us/docs/Code%20of%20Ordinances%202024.pdf": ("application/pdf", PDF),
    "https://smallton.ga.us/docs/Zoning%20Ordinance.pdf": ("application/pdf", b"%PDF-1.4 zoning"),
    "https://libton.gov/": ("text/html", 'Our code: <a href="https://library.municode.com/ga/libton">Municode</a> '
                                         '<img src="https://s3.example/libton.municipalcodeonline.com-new/x/logo.png"> '
                                         '<a href="/clerk">Clerk</a>'),
    "https://libton.gov/clerk": ("text/html", "records@libton.gov"),
    "https://www.gacities.com/gma-cities-districts/nosite/00999": (
        "text/html", '<a href="https://x.com/gma">x</a> <a href="https://www.nosite-ga.org">www.nosite-ga.org</a>'),
    "https://www.nosite-ga.org": ("text/html", "cityclerk@nosite-ga.org"),
    # Seat County: its old domain redirects to a new one; it carries its seat city's ordinance and Municode link
    "http://www.seatcounty.org/": ("redirect", "https://seatcountyga.gov/"),
    "https://seatcountyga.gov/": ("text/html", '<a href="/county-clerk">County Clerk</a> '
                                               '<a href="https://library.municode.com/ga/seatville/codes/code_of_ordinances">'
                                               "Seatville's code</a>"),
    "https://seatcountyga.gov/county-clerk": (
        "text/html", 'clerk@seatcountyga.gov <a href="/f/city-of-seatville-administration_165.pdf">click here - Alcohol '
                     'Ordinance</a> ' + " ".join(f'<a href="/code/Chapter{i}.pdf">Code Book Chapter {i}</a>' for i in range(1, 6))),
    **{f"https://seatcountyga.gov/code/Chapter{i}.pdf": ("application/pdf", f"%PDF-1.4 chapter {i}".encode()) for i in range(1, 6)},
    "https://seatcountyga.gov/f/city-of-seatville-administration_165.pdf": ("application/pdf", b"%PDF-1.4 city"),
}


class FakeFetcher:
    def __init__(self):
        self.got = []

    def get(self, url, kind=None, **_):
        self.got.append(url)
        ctype, body = SITE.get(url, ("text/html", None))
        if ctype == "redirect":
            url = body
            ctype, body = SITE[body]
        if body is None:
            return FetchResult(url, 404, {"content-type": "text/html"}, b"", 0.0)
        return FetchResult(url, 200, {"content-type": ctype}, body if isinstance(body, bytes) else body.encode(), 0.0)


J = [{"id": "smallton", "name": "Smallton", "kind": "city", "population": 900, "website": "https://smallton.ga.us/"},
     {"id": "libton", "name": "Libton", "kind": "city", "population": 5000, "website": "https://libton.gov/"},
     {"id": "nosite", "name": "Nosite", "kind": "town", "population": 300, "fips": "1300999"},
     {"id": "co-had", "name": "Had County", "kind": "county", "population": 9000, "website": "https://had.gov/"},
     {"id": "co-seat", "name": "Seat County", "kind": "county", "population": 7000, "website": "http://www.seatcounty.org/"}]


def _root(tmp_path):
    (tmp_path / "imports" / "ordinances" / "had").mkdir(parents=True)          # Had County already has a code
    (tmp_path / "imports" / "ordinances" / "had" / "code.pdf").write_bytes(b"x")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text(
        "counties:\n- {id: co-had, name: Had County, kind: county}\n- {id: co-seat, name: Seat County, kind: county}\n"
        "municipalities:\n- {id: smallton, name: Smallton, kind: city}\n- {id: libton, name: Libton, kind: city}\n"
        "- {id: nosite, name: Nosite, kind: town}\n")
    return tmp_path


def test_look_finds_clerk_and_code_documents_not_applications_requests_or_calendars():
    res = cf.look(FakeFetcher(), "https://smallton.ga.us/", J[0])
    assert res["records"]["emails"][0] == "clerk@smallton.ga.us" and "someone@example.com" not in res["records"]["emails"]
    assert [Path(d["url"]).name for d in res["docs"]] == ["Code%20of%20Ordinances%202024.pdf", "Zoning%20Ordinance.pdf"]


def test_look_follows_a_redirected_front_door_and_leaves_other_places_alone():
    res = cf.look(FakeFetcher(), "http://www.seatcounty.org/", J[4])
    assert res["landed"] == "https://seatcountyga.gov/" and res["records"]["emails"] == ["clerk@seatcountyga.gov"]
    assert [Path(d["url"]).name for d in res["docs"]] == [f"Chapter{i}.pdf" for i in range(1, 6)]   # not the city's
    assert res["libraries"] == [] and res["other_libraries"][0].startswith("https://library.municode.com/ga/seatville")


def test_names_other_place_and_library_checks():
    county = {"name": "Jenkins County"}
    assert cf.names_other_place("click here - Alcohol Ordinance city-of-millen-administration_165.pdf", county)
    assert not cf.names_other_place("Alcohol Ordinance for Jenkins County 2023.pdf", county)
    assert not cf.names_other_place("Animal Services and Control Ordinance.pdf", county)
    assert cf.library_is_this_place("https://library.municode.com/ga/metter/codes/x", {"name": "Candler County"}) is False
    assert cf.library_is_this_place("https://greenecounty.municipalcodeonline.com", {"name": "Greene County"}) is True
    assert cf.library_is_this_place("https://ecode360.com/12345", {"name": "Greene County"}) is None


def test_run_saves_codes_with_sources_and_feeds_open_records(tmp_path):
    root = _root(tmp_path)
    f = FakeFetcher()
    out = cf.run(root, J, f, log=lambda *_: None, workers=3)
    assert out["looked"] == 4 and out["docs_saved"] == 7 and out["libraries"] == 1
    assert not any("had.gov" in u for u in f.got)
    saved = sorted(p.name for p in (root / "imports" / "ordinances" / "smallton").iterdir())
    assert saved == ["Code of Ordinances 2024.pdf", "Zoning Ordinance.pdf", "sources.csv"]
    rows = list(csv.DictReader(open(root / "imports" / "ordinances" / "smallton" / "sources.csv")))
    assert rows[0]["url"].endswith("Code%20of%20Ordinances%202024.pdf") and rows[0]["kind"] == "code"
    assert rows[1]["kind"] == "zoning"
    assert sorted(p.name for p in (root / "imports" / "ordinances" / "co-seat").iterdir() if p.suffix == ".pdf") == \
        [f"Chapter{i}.pdf" for i in range(1, 6)]
    found = json.loads((root / "data" / "openrecords" / "found.json").read_text())
    assert found["nosite"]["website"] == "https://www.nosite-ga.org" and found["nosite"]["website_from"] == "GMA city directory"
    assert found["nosite"]["records"]["emails"] == ["cityclerk@nosite-ga.org"]
    assert found["libton"]["libraries"] == ["https://library.municode.com/ga/libton"]       # the logo is not a link
    online = (root / "data" / "openrecords" / "online_codes.md").read_text()
    assert "Libton" in online and "Seat County" not in online
    # a second run inside the recheck window looks at nothing, and saves nothing twice
    assert cf.run(root, J, FakeFetcher(), log=lambda *_: None)["looked"] == 0
    assert cf.run(root, J, FakeFetcher(), only=["smallton"], log=lambda *_: None)["docs_saved"] == 0
    # open records: Smallton and Seat County now have codes (skipped), Libton's code is online (skipped), Nosite is
    # asked at the address the deeper look found
    r = orr.Requests(root, {"openrecords": {"enabled": False, "requester": "X"}}, log=lambda *_: None)
    due = orr.plan(J, r.tracker, have=r.have, found=r.found)
    assert [(i["jurisdiction"]["id"], i["to"]) for i in due] == [("nosite", "cityclerk@nosite-ga.org")]


def test_a_long_code_is_taken_over_several_runs(tmp_path):
    root = _root(tmp_path)
    one = [j for j in J if j["id"] == "co-seat"]
    out = cf.run(root, one, FakeFetcher(), max_docs=2, log=lambda *_: None)
    assert out["docs_saved"] == 2
    found = json.loads((root / "data" / "openrecords" / "found.json").read_text())
    assert found["co-seat"]["more"] is True
    # two chapters are not the code yet: the place still counts as without one, and the next runs take the rest
    assert "co-seat" not in orr.downloaded_places(root)
    assert cf.run(root, one, FakeFetcher(), max_docs=2, log=lambda *_: None)["docs_saved"] == 2
    assert cf.run(root, one, FakeFetcher(), max_docs=2, log=lambda *_: None)["docs_saved"] == 1
    assert sorted(p.name for p in (root / "imports" / "ordinances" / "co-seat").iterdir() if p.suffix == ".pdf") == \
        [f"Chapter{i}.pdf" for i in range(1, 6)]
    assert "co-seat" in orr.downloaded_places(root)          # five chapters, nothing left: that is the code


def test_individual_ordinances_alone_are_not_the_code():
    saved = [{"url": "u1", "file": "co-toombs/Alcohol-Ordinance-2023.pdf"}, {"url": "u2", "file": "co-toombs/Solar.pdf"}]
    assert not cf.is_complete(saved, {"u1": "Alcohol Ordinance", "u2": "Solar Energy Ordinance"}, False)
    assert cf.is_complete([{"url": "u", "file": "x/Code-of-Ordinances.pdf"}], {"u": "Code of Ordinances"}, False)
    assert not cf.is_complete([{"url": "u", "file": "x/State-of-Emergency.pdf"}],
                              {"u": "Suspension of Portions of the Code of Ordinances"}, False)
    assert not cf.is_complete([{"url": "u", "file": "x/20240111-Index.pdf"}], {"u": "Code of Ordinances"}, False)
    assert not cf.is_complete([{"url": "u", "file": "x/Code-of-Ordinances.pdf"}], {"u": "Code of Ordinances"}, True)


def test_an_older_version_is_looked_at_again():
    assert cf.due({}, "2026-01-01") and cf.due({"v": 1, "checked_at": "2026-11-09"}, "2026-01-01")
    assert cf.due({"v": cf.VERSION, "checked_at": "2026-11-09", "more": True}, "2026-01-01")
    assert not cf.due({"v": cf.VERSION, "checked_at": "2026-11-09"}, "2026-01-01")
