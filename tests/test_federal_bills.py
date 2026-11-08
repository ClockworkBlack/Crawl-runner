import datetime as dt
from pathlib import Path

from gacrawl import federal_bills as fb

XML = (Path(__file__).parent / "fixtures" / "BILLSTATUS-118sjres9.xml").read_bytes()


def test_parse_real_bill_status():
    m = fb.parse(XML)
    assert (m["c"], m["t"], m["n"]) == (118, "sjres", 9)
    assert m["sponsor"]["name"].startswith("Sen. Marshall") and m["sponsor"]["party"] == "R"
    assert m["cos"] == 2 and m["cos_other"] == 1 and m["cos_ga"] == ["Sen. Ossoff, Jon [D-GA]"]   # withdrawn left out
    assert m["m"]["ps"] == "2023-05-03" and m["m"]["ph"] == "2023-07-27" and m["m"]["veto"] == "2023-09-26"
    assert m["m"].get("rep") == "2023-05-03"          # discharged; the Rules Committee's own line is not this bill's report
    assert fb.stage_of(m) == "vetoed" and fb.stage_of(m, "2023-06-01") == "passed_one"
    assert m["summary"].startswith("This joint resolution nullifies")
    assert m["u"] == "https://www.congress.gov/bill/118th-congress/senate-joint-resolution/9"


def _bill(c, t, n, intro, party="R", cos_other=0, **marks):
    return {"c": c, "t": t, "n": n, "intro": intro, "sponsor": {"name": "X", "party": party}, "cos": cos_other,
            "cos_other": cos_other, "m": {"intro": intro, **marks}}


def test_outlook_from_earlier_congresses():
    past = []
    for c in (116, 118):          # House majority: D in 116, R in 118
        y = fb.congress_start(c).year
        for i in range(40):       # passed the House by day 300; 30 of 40 became law
            marks = {"ph": f"{y}-06-01"}
            if i < 30:
                marks.update({"ps": f"{y + 1}-03-01", "law": f"{y + 1}-04-01"})
            past.append(_bill(c, "hr", i, f"{y}-02-01", party="D" if c == 116 else "R", **marks))
    ol = fb.Outlook(past)
    today = dt.date(2025, 11, 1)
    now = _bill(119, "hr", 1, "2025-02-01", party="R", ph="2025-06-01")
    o = ol.rate(now, today)
    assert o["label"] == "Pass Likely" and o["pct"] == 75.0 and o["n"] == 80
    assert ol.rate(_bill(118, "hr", 1, "2023-02-01"), today) is None            # a past Congress: no outlook
    assert ol.rate(_bill(119, "hr", 2, "2025-02-01", ph="2025-03-01", ps="2025-04-01", law="2025-05-01"), today) is None


def test_congress_now():
    assert fb.congress_now(dt.date(2026, 11, 8)) == 119 and fb.congress_now(dt.date(2027, 1, 2)) == 119
    assert fb.congress_now(dt.date(2027, 1, 3)) == 120 and fb.congress_now(dt.date(2016, 6, 1)) == 114


def test_export(tmp_path):
    import json
    from gacrawl.store import Store
    s = Store(tmp_path / "data")
    fb._save(s, fb.parse(XML))
    s.flush()
    out = fb.export_federal(s, tmp_path / "fed", log=lambda *a: None, today=dt.date(2026, 11, 8))
    d = json.loads((tmp_path / "fed" / "118.json").read_text())
    row = dict(zip(d["cols"], d["rows"][0]))
    assert out["bills"] == 1 and row["t"] == "sjres" and row["stage"] == "vetoed" and row["ga"] == ["Sen. Ossoff, Jon [D-GA]"]
    assert json.loads((tmp_path / "fed" / "meta.json").read_text())["now"] == 119
