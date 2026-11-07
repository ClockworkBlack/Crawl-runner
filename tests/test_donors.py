"""donors.json: recipients, gifts, spend, cross-reference donors and totals by dimension."""
import json
from pathlib import Path

from gacrawl.donors import export_donors
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parents[1]


def test_export_donors(tmp_path):
    s = Store(tmp_path / "data")
    cands = [{"id": "a", "name": "Ann Able", "electo_key": "ANN ABLE", "party": "D", "office": "U.S. House GA-5", "levels": ["federal"], "counties": ["fulton"], "jurisdiction": ""},
             {"id": "b", "name": "Bob Baker", "electo_key": "BOB BAKER", "party": "R", "office": "City Council", "levels": ["local"], "counties": ["cobb"], "jurisdiction": "marietta"}]
    rows = [("a", "Jane Donor", 500, "contribution", "Atlanta", "GA", "Acme Inc", "Engineer", "FEC", "d1"),
            ("a", "Jane Donor", 150, "contribution", "Atlanta", "GA", "Acme Inc", "Engineer", "FEC", "d1"),
            ("a", "Big PAC", 5000, "contribution", "Washington", "DC", "", "", "FEC", "d2"),
            ("b", "Jane Donor", 250, "contribution", "Marietta", "GA", "Acme Inc", "Engineer", "CCDR", "d1"),
            ("b", "Print Shop LLC", 1200, "expenditure", "Marietta", "GA", "", "", "CCDR", "p1")]
    for i, (cid, name, amt, kind, city, st, emp, occ, fs, ent) in enumerate(rows):
        m = {"type": kind, "name": name, "amount": amt, "date": f"2026-0{i+1}-01", "employer": emp, "occupation": occ, "city": city, "state": st,
             "purpose": "mailers" if kind == "expenditure" else "", "recipient": cands[0 if cid == "a" else 1]["name"], "recipient_kind": "candidate",
             "donor_is_org": name.endswith(("PAC", "LLC")), "filing_source": fs, "candidate_id": cid, "donor_entity": ent,
             "contributor_committee_id": "C1" if name == "Big PAC" else None}
        s.add(Record(f"cand:{cid}", "contribution", f"https://filings.example/{cid}/{i}", title=name, text="{}", published=m["date"],
                     channel="api", evidence="primary", key=f"k{i}", meta=m))
    s.flush()
    out = export_donors(s, ROOT, cands, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    R = {r["name"]: r for r in d["recipients"]}
    assert out["rows"] == 5 and R["Ann Able"]["raised"] == 5650 and R["Ann Able"]["n"] == 3 and R["Ann Able"]["small"] == 150
    assert R["Ann Able"]["in_ga"] == 650 and R["Ann Able"]["orgs"] == 5000 and R["Ann Able"]["employers"][0] == {"name": "Acme Inc", "total": 650}
    assert R["Bob Baker"]["spent"] == 1200 and R["Bob Baker"]["nx"] == 1 and R["Bob Baker"]["level"] == "local"
    g = d["gifts"]
    assert g[0]["name"] == "Big PAC" and g[0]["kind"] == "committee" and g[0]["state"] == "DC"
    jane = [x for x in g if x["name"] == "Jane Donor"]
    assert len(jane) == 2 and {x["total"] for x in jane} == {650, 250} and all(x["county"] in ("Fulton", "Cobb") for x in jane)
    assert d["spend"][0]["payee"] == "Print Shop LLC" and d["spend"][0]["purpose"] == "mailers"
    assert d["donors"][0]["name"] == "Jane Donor" and d["donors"][0]["r"] == [0, 1] and d["donors"][0]["total"] == 900
    by = {b["k"]: b["t"] for b in d["by"]["party"]}
    assert by == {"D": 5650, "R": 250}
    assert d["by"]["donor_state"][0]["k"] == "DC"
