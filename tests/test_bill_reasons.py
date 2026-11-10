"""Why bills passed or died: bill-number matching, reason excerpts, outcomes, the news-API search and the export."""
import datetime as dt
import json
import shutil
from pathlib import Path

from gacrawl import bill_reasons as br
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parents[1]

ARTICLE = """<p>The Senate never took up House Bill 960 before Crossover Day. Opponents said the bill would cost counties
millions to replace their voting machines. Supporters argued hand-marked paper ballots are easier to audit.</p>
<p>Elsewhere, HB 9601 is unrelated. The weather was fine on Sine Die evening and nothing else happened here at all.</p>"""


def test_bill_rx_forms():
    rx = br.bill_rx("HB 960")
    assert rx.search("House Bill 960") and rx.search("HB960") and rx.search("H.B. 960")
    assert not rx.search("HB 9601") and not rx.search("SB 960")


def test_excerpts_and_stance():
    from gacrawl import extract

    _, md = extract.html_to_markdown(ARTICLE, "https://example.com/a")
    ex = br.excerpts_for(md, "HB 960")
    assert ex and ex[0]["stance"] == "outcome" and "Crossover Day" in ex[0]["x"]
    assert any(e["stance"] == "oppose" and "cost" in e["x"] for e in ex)
    assert any(e["stance"] == "support" for e in ex)
    assert not any("9601" in e["x"] for e in ex)


def test_quotes_keep_a_markdown_links_words_not_its_address():
    """An article read with its links in Markdown: the quote reads "builds on HB 340", not the 200-character address."""
    t = ("Kemp signed HB 1009 on Tuesday. The law builds on [HB 340](https://urldefense.com/v3/__https://office.example/t/y-l__;!!HW%24), "
         "signed by the governor last year, which banned devices in K-8 classrooms. ![photo](https://x.example/p.jpg)")
    ex = br.excerpts_for(t, "HB 340")
    assert ex and all("](" not in e["x"] and "urldefense" not in e["x"] for e in ex)
    assert any("The law builds on HB 340, signed by the governor last year" in e["x"] for e in ex)
    assert br.plain("[Wiki (page)](https://en.wikipedia.org/wiki/A_(b)) and [x] y") == "Wiki (page) and [x] y"


def test_outcome_and_session_end():
    assert br.outcome_of({"enacted": True}, True) == "law"
    assert br.outcome_of({"vetoed": True}, False) == "vetoed"
    assert br.outcome_of({}, True) == "died" and br.outcome_of({}, False) is None
    assert br.session_ended([2025, 2026], dt.date(2026, 11, 7)) and not br.session_ended([2027, 2028], dt.date(2027, 3, 1))
    assert br.substantive({"number": "SB 3"}) and not br.substantive({"number": "HR 12", "caption": "Honoring a teacher"})
    assert br.substantive({"number": "HR 32", "caption": "Proposing an amendment to the Constitution"})


class FakeResp:
    def __init__(self, url, status=200, body=b"", ctype="application/json"):
        self.url, self.status, self.content, self.content_type = url, status, body, ctype
        self.text = body.decode()

    def json(self):
        return json.loads(self.content)


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, use_cache=True, headers=None):
        self.calls.append((url, params))
        if url == "https://georgiarecorder.com/":
            return FakeResp(url, body=b'<html><head><link rel="https://api.w.org/" href="https://georgiarecorder.com/wp-json/"></head></html>', ctype="text/html")
        if url.endswith("/wp/v2/posts"):
            posts = [{"link": "https://georgiarecorder.com/2026/04/03/hand-marked-ballots-die/", "date": "2026-04-03T10:00:00",
                      "title": {"rendered": "Hand-marked ballot bill dies"}, "content": {"rendered": ARTICLE}},
                     {"link": "https://elsewhere.example/copied", "date": "2026-04-03T10:00:00", "title": {"rendered": "x"},
                      "content": {"rendered": ARTICLE}}]
            return FakeResp(url, body=json.dumps(posts).encode())
        return FakeResp(url, status=404, body=b"", ctype="text/html")


def _store(tmp_path):
    s = Store(tmp_path / "data")
    s.add(Record("ga:legis", "bill", "https://www.legis.ga.gov/legislation/70001", title="HB 960", key="ga:legis|bill|70001",
                 meta={"legislation_id": 70001, "number": "HB 960", "session": 1033, "session_name": "2025-2026", "caption": "Elections; paper ballots",
                       "status": "Senate Read Second Time", "status_date": "2026-03-01", "enacted": False, "vetoed": False, "chamber": "House",
                       "sponsors": [{"member_id": 1, "name": "A Smith", "sequence": 1}, {"member_id": 2, "name": "B Jones", "sequence": 2}]}))
    s.add(Record("ga:legis", "legislator", "https://x/1", title="A Smith", key="ga:legis|legislator|1", meta={"member_id": 1, "party": "R"}))
    s.add(Record("ga:legis", "legislator", "https://x/2", title="B Jones", key="ga:legis|legislator|2", meta={"member_id": 2, "party": "D"}))
    rows = [{"person": f"R{i}", "person_id": 100 + i, "vote": "Yea", "party": "R"} for i in range(10)] + \
           [{"person": f"D{i}", "person_id": 200 + i, "vote": "Nay", "party": "D"} for i in range(8)]
    s.add(Record("ga:legis", "vote", "https://www.legis.ga.gov/legislation/70001", title="vote", key="ga:legis|vote|9",
                 meta={"VoteId": 9, "legislation_id": 70001, "session": 1033, "chamber": "House", "date": "2026-02-20", "yea": 10, "nay": 8,
                       "result": "passed", "substantive": True, "votes": rows, "vote_caption": "Third reading"}))
    s.flush()
    return s


def test_research_and_export(tmp_path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "legislature.yaml", root / "config" / "legislature.yaml")
    (root / "config" / "outlets.yaml").write_text("outlets: [georgiarecorder.com]\nstatewide: [georgiarecorder.com]\n")
    state = tmp_path / "state"
    state.mkdir()
    store = _store(tmp_path)
    out = br.research(FakeFetcher(), store, root, state, log=lambda *a: None)
    assert out["bills_checked"] == 1 and out["new_reasons"] == 1 and out["outlet_apis"] == 1
    # a second run finds nothing due (the session is closed and the bill has not moved)
    assert br.research(FakeFetcher(), store, root, state, log=lambda *a: None)["bills_checked"] == 0
    counts = br.export_reasons(store, root, tmp_path / "bill_reasons.json")
    doc = json.loads((tmp_path / "bill_reasons.json").read_text())
    b = doc["bills"][0]
    assert counts["with_reasons"] == 1 and b["outcome"] == "died" and b["stage"] == "first chamber"
    assert b["votes"][0]["party_line"] and b["bipartisan_sponsors"] and b["lead_sponsor"]["party"] == "R"
    assert b["reasons"][0]["outlet"] == "georgiarecorder.com" and b["reasons"][0]["excerpts"]
    assert doc["patterns"]["bills"] == 1


def test_veto_statements_from_the_governors_pages(tmp_path):
    import io
    from reportlab.pdfgen import canvas
    from gacrawl.fetch import FetchResult

    buf = io.BytesIO(); c = canvas.Canvas(buf)
    y = 800
    for line in ["VETO NUMBER 3", "HB 960", "I have vetoed House Bill 960. The bill would cost counties millions",
                 "to replace voting machines without funding, and I cannot support an unfunded mandate."]:
        c.drawString(40, y, line); y -= 16
    c.save(); pdf = buf.getvalue()
    year_page = ('<nav><a href="/executive-action/legislation/vetoed-legislation/2025">2025</a></nav>'
                 '<table><tr><td><a href="/document/vetoed-legislation/hb-960pdf/download">HB 960</a></td></tr></table>')
    pages = {br.VETO_START: year_page.encode(),
             "https://gov.georgia.gov/executive-action/legislation/vetoed-legislation/2025": b"<table></table>",
             "https://gov.georgia.gov/document/vetoed-legislation/hb-960pdf/download": pdf}

    class F:
        def get(self, url, **kw):
            body = pages.get(url)
            return FetchResult(url, 200 if body else 404, {"content-type": "text/html"}, body or b"", 0.0)

    store = Store(tmp_path / "db")
    store.add(Record(br.LEGIS, "bill", "https://www.legis.ga.gov/legislation/1", title="HB 960",
                     meta={"number": "HB 960", "session": 1033, "legislation_id": 1, "vetoed": True, "session_name": "2025-2026"}))
    store.flush()
    root = tmp_path / "root"; (root / "config").mkdir(parents=True)
    (root / "config" / "legislature.yaml").write_text("sessions:\n  years:\n    1033: [2025, 2026]\n")
    out = br.vetoes(F(), store, root, tmp_path, log=lambda *a: None)
    assert out["statements"] == 1 and out["documents"] >= 1
    rs = list(store.iter_query("kind='bill_reason' AND source_id=?", (br.VETO_SOURCE,)))
    assert rs and rs[0]["evidence"] == "official" and "unfunded" in rs[0]["text"] and rs[0]["meta"]["official"]
    # a second run inside the week reads nothing again
    out2 = br.vetoes(F(), store, root, tmp_path, log=lambda *a: None)
    assert out2["documents"] == 0
