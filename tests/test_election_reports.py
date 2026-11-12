"""County election reports (gacrawl/election_reports.py): the county's own summary report of the election day is found,
checked and stored for the elections deriver; a report of another day, or an unofficial host, is not kept; a county
is not searched twice in a month. Fictional search results; the report text is DeKalb's Nov. 7, 2023 layout."""
import io
import json
from types import SimpleNamespace

from gacrawl import election_reports as er
from gacrawl import websearch
from gacrawl.elections import _derive
from gacrawl.store import Store


def _pdf(lines):
    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    y = 800
    for ln in lines:
        c.drawString(40, y, ln)
        y -= 14
    c.save()
    return b.getvalue()


REPORT = ["Election Summary Report", "General Election", "November 07, 2023", "Mayor - Dunwoody (Vote for 1)",
          "Lynn Deutsch (I) 3,994 3,250 86 11 7,341 100.00%", "Total Votes 3,994 3,250 86 11 7,341",
          "Dunwoody City Council, District 1 Post 4 At Large (Vote for 1)", "Stacey Harris (I) 3,270 2,712 79 10 6,071 78.69%",
          "Chris Ozor 919 698 25 2 1,644 21.31%", "Total Votes 4,189 3,410 104 12 7,715"]
OTHER_DAY = [l.replace("November 07, 2023", "November 08, 2022") for l in REPORT]
PAGES = {"https://www.dekalbcountyga.gov/files/2022.pdf": _pdf(OTHER_DAY),
         "https://www.dekalbcountyga.gov/files/esr-2023.pdf": _pdf(REPORT),
         "https://some-blog.example.com/results.pdf": _pdf(REPORT)}


class Fetcher:
    def get(self, url, **k):
        b = PAGES.get(url, b"")
        return SimpleNamespace(status=200 if b else 404, content=b, url=url)


def test_finds_checks_and_stores_the_countys_report(tmp_path, monkeypatch):
    asked = []

    def search(self, q, count=8):
        asked.append(q)
        self.run_n += 1
        return [{"url": "https://some-blog.example.com/results.pdf", "title": "results pdf"},
                {"url": "https://www.dekalbcountyga.gov/files/2022.pdf", "title": "Election Summary Report pdf"},
                {"url": "https://www.dekalbcountyga.gov/files/esr-2023.pdf", "title": "Election Summary Report pdf"}]

    monkeypatch.setattr(websearch.Search, "search", search)
    monkeypatch.setattr(websearch.Search, "save", lambda self: None)
    monkeypatch.setattr(websearch.Search, "left", lambda self: 99)
    store = Store(tmp_path / "data")
    out = er.run(Fetcher(), store, er_root(tmp_path), tmp_path / "state", counties=["co-dekalb"], log=lambda *a: None)
    assert out["kept"][0] == {"county": "co-dekalb", "election": "2023-11-07", "url": "https://www.dekalbcountyga.gov/files/esr-2023.pdf"}
    docs = store.read("co-dekalb:elections", "document")
    assert len(docs) == 1 and docs[0]["jurisdiction"] == "co-dekalb" and docs[0]["meta"]["election_report"]["date"] == "2023-11-07"
    # the deriver reads it into the city's contests
    recs = [r for r in _derive({**docs[0], "source_id": "co-dekalb:elections"}, {"jurisdictions": []}) if r.kind == "election_result"]
    assert {r.meta["office"] for r in recs} == {"Mayor - Dunwoody", "Dunwoody City Council, District 1 Post 4 At Large"}
    assert {r.meta["date"] for r in recs} == {"2023-11-07"}
    # found: not searched again; the other elections not found are not searched again inside a month
    n = len(asked)
    er.run(Fetcher(), store, er_root(tmp_path), tmp_path / "state", counties=["co-dekalb"], log=lambda *a: None)
    assert len(asked) == n
    st = er.status(store, er_root(tmp_path))
    assert st["2023-11-07"]["found"] == 1 and "co-dekalb" in st["2021-11-02"]["missing"]
    store.close()


def er_root(tmp_path):
    from gacrawl import config

    return config.ROOT


def test_only_official_or_archival_hosts():
    assert er._official("https://www.dekalbcountyga.gov/x.pdf", "dekalbcountyga.gov")
    assert er._official("https://www.fultoncountyga.gov/x.pdf", "")
    assert er._official("https://election.lab.ufl.edu/data-downloads/localelections/GA/x.pdf", "")
    assert not er._official("https://some-blog.example.com/x.pdf", "dekalbcountyga.gov")


def test_a_citys_copy_is_kept_but_the_county_report_is_still_sought(tmp_path, monkeypatch):
    pages = {"https://www.clarkstonga.gov/files/esr.pdf": _pdf(REPORT)}

    class F:
        def get(self, url, **k):
            b = pages.get(url, b"")
            return SimpleNamespace(status=200 if b else 404, content=b, url=url)

    def search(self, q, count=8):
        self.run_n += 1
        return [{"url": "https://www.clarkstonga.gov/files/esr.pdf", "title": "Election Summary Report pdf"}]

    monkeypatch.setattr(websearch.Search, "search", search)
    monkeypatch.setattr(websearch.Search, "save", lambda self: None)
    monkeypatch.setattr(websearch.Search, "left", lambda self: 99)
    store = Store(tmp_path / "data")
    out = er.run(F(), store, er_root(tmp_path), tmp_path / "state", counties=["co-dekalb"], log=lambda *a: None)
    assert out["found"] == 0 and {k["url"] for k in out["kept"]} == {"https://www.clarkstonga.gov/files/esr.pdf"}
    st = json.loads(store.kv_get("elections:report:co-dekalb:2023-11-07"))
    assert not st.get("url") and st["partial"] == ["https://www.clarkstonga.gov/files/esr.pdf"] and st["tries"] == 1
    assert len(store.read("co-dekalb:elections", "document")) == 1
    store.close()
