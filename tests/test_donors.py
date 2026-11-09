"""donors.json: recipients, gifts, spend, cross-reference donors and totals by dimension."""
import json
from pathlib import Path

from gacrawl.donors import export_donors
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parents[1]


def test_export_donors(tmp_path, monkeypatch):
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
    # a profile for every donor: one shard file per id, a search index by first letter
    from gacrawl.donors import shard_of
    pd = tmp_path / "donor_profiles"
    meta = json.loads((pd / "meta.json").read_text())
    assert meta["donors"] == 2 and len(list((pd / "dp").glob("*.json"))) == 128
    P = json.loads((pd / "dp" / f"{shard_of('d1')}.json").read_text())["d1"]
    assert P["name"] == "Jane Donor" and P["total"] == 900 and P["n"] == 3 and len(P["to"]) == 2
    assert P["employer"] == "Acme Inc" and P["occupation"] == "Engineer" and P["years"] == {"2026": 900} and P["rank"] == 2
    assert "about" not in P and "org" not in P          # a person: the filings only
    pac = json.loads((pd / "dp" / f"{shard_of('d2')}.json").read_text())["d2"]
    assert pac["kind"] == "committee" and "employer" not in pac
    idx = json.loads((pd / "dpi" / "j.json").read_text())
    assert idx[0][:3] == ["d1", "Jane Donor", "person"]
    # profiles for the largest donors only (the site carries every profile): the rest keep their gifts, not a profile
    monkeypatch.setenv("DONOR_PROFILE_CAP", "1")
    export_donors(s, ROOT, cands, tmp_path / "donors.json", log=lambda *a: None)
    meta = json.loads((pd / "meta.json").read_text())
    assert (meta["donors"], meta["all_donors"], meta["cap"]) == (1, 2, 1)
    assert "d2" in json.loads((pd / "dp" / f"{shard_of('d2')}.json").read_text())                   # Big PAC, $5,000
    assert "d1" not in json.loads((pd / "dp" / f"{shard_of('d1')}.json").read_text())
    assert json.loads((tmp_path / "donors.json").read_text())["donors"][0]["name"] == "Jane Donor"  # still counted


def test_shard_of_matches_the_page():
    from gacrawl.donors import shard_of
    # the page's dnShard: FNV-1a 32-bit, % 128 (ga/template.html); values worked out by hand from the algorithm
    assert shard_of("") == f"{0x811C9DC5 % 128:02x}"
    assert len({shard_of(f"p{i}") for i in range(2000)}) == 128


def test_summary_sentence():
    from gacrawl.donors import summary_of
    s = summary_of({"kind": "person", "total": 900, "n": 3, "first": "2024-03-01", "last": "2026-01-01",
                    "to": [{"name": "Ann Able", "party": "D", "t": 650}, {"name": "Bob Baker", "party": "R", "t": 250}],
                    "via": [{"name": "ActBlue", "t": 150}]})
    assert s == ("This donor gave $900 in 3 gifts to 2 recipients from 2024 to 2026; the most, $650, to Ann Able "
                 "($650 to Democrats, $250 to Republicans). $150 of it passed through ActBlue.")


def test_public_figures_only_when_the_filing_says_so():
    from gacrawl.donors import _public_figure_index, public_figure
    idx = _public_figure_index([{"id": "jan-jones", "name": "Jan Jones", "electo_key": "JAN JONES", "office": "State House District 47"}])
    official = {"kind": "person", "name": "JONES, JAN", "_occ": "STATE REPRESENTATIVE", "employer": "STATE OF GEORGIA"}
    private = {"kind": "person", "name": "JONES, JAN", "_occ": "TEACHER", "employer": "FULTON COUNTY SCHOOLS"}
    assert public_figure(official, idx)["key"] == "JAN JONES"
    assert public_figure(private, idx) is None           # same name, a private person: no political profile attached
    assert public_figure({**official, "name": "SMITH, PAT"}, idx) is None


def test_giving_record():
    from gacrawl.donors import giving_record
    P = {"years": {"2017": 500, "2020": 9000, "2024": 1000}, "to": [{"party": "D", "office": "U.S. Senate"}, {"party": "R", "office": "State Senator"}]}
    assert giving_record(P) == ("Giving since 2017, in 3 of the 8 years through 2024; the most in 2020 ($9,000). Has given to both "
                                "Democrats and Republicans. Gives to federal and to state or local candidates.")
    assert giving_record({"years": {"2026": 100}, "to": []}) == "Giving since 2026."


def test_every_candidate_keeps_its_own_largest_gifts_in_the_capped_list(monkeypatch):
    """Ranked only by size, one Senate campaign's thousands of large gifts left every other campaign's donors out of
    the list Electo keeps; each candidate's own largest now come first."""
    from gacrawl import donors

    monkeypatch.setattr(donors, "GIFT_FLOOR", 3)
    big = [{"r": 0, "total": 5000.0 + i} for i in range(200)]          # one big campaign
    small = [{"r": r, "total": 100.0 + i} for r in (1, 2, 3) for i in range(5)]
    committee = [{"r": 9, "total": 9000.0}]                              # not a candidate: only by size
    out = donors.capped(big + small + committee, 40, candidates={0, 1, 2, 3})
    assert len(out) == 40 and [g["total"] for g in out] == sorted((g["total"] for g in out), reverse=True)
    for r in (1, 2, 3):
        assert sorted(g["total"] for g in out if g["r"] == r)[-3:] == [102.0, 103.0, 104.0]    # each one's largest
    assert sum(1 for g in out if g["r"] == 0) >= 20 and out[0]["total"] == 9000.0
    assert donors.capped(small, 100) == sorted(small, key=lambda g: -g["total"])             # under the cap: all
