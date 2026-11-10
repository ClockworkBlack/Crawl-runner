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


def test_cycles_links_own_money_and_held_federal_lines(tmp_path):
    """The Donors tab's chart: money by election cycle with dates, committees linked to their candidate (the hand link
    table, or the filer's name matched again), the candidate's own money apart, and a campaign whose FEC lines wait for
    the re-read marked as incomplete."""
    from gacrawl.donors import cycle_of, is_self

    s = Store(tmp_path / "data")
    cands = [{"id": "a", "name": "Ann Able", "electo_key": "ANN ABLE", "party": "D", "office": "U.S. Senate", "levels": ["federal"],
              "counties": [], "jurisdiction": ""}]
    lines = [  # (filer as filed, filer type, donor, amount, date, extra meta)
        ("Jackson for Governor, Inc.", "Candidate Committee", "Jackson, Richard L", 40_000_000, "2026-02-04", {}),
        ("Jackson for Governor, Inc.", "Candidate Committee", "Jackson, Marsha", 6_600, "2026-03-01", {}),
        ("Jackson for Governor, Inc.", "Candidate Committee", "Smith, Pat", 1_000, "2026-07-30", {}),
        ("WBJ Leadership Committee, Inc.", "Leadership Committee", "Big Co LLC", 250_000, "2025-08-01", {"donor_is_org": True}),
        ("WBJ Leadership Committee, Inc", "Leadership Committee", "Other Co LLC", 50_000, "2026-01-05",
         {"donor_is_org": True, "recipient_entity": "second-filer-record"}),
        ("Friends of Ann Able", "Candidate Committee", "Doe, Jane", 500, "2022-05-01", {}),
        ("Friends of Ann Able", "Candidate Committee", "Doe, Jane", 300, "2025-12-31", {}),
        ("Some Other PAC", "Political Action Committee", "Roe, Rick", 100, "2026-01-01", {}),
    ]
    for i, (filer, ft, donor, amt, d, extra) in enumerate(lines):
        m = {"type": "contribution", "name": donor, "amount": amt, "date": d, "recipient": filer, "recipient_kind": "committee",
             "filer_name": filer, "filer_type": ft, "ga_system": "peachfile", "filing_source": "GA Ethics", "city": "Atlanta",
             "state": "GA", "donor_entity": donor, "line": str(i), **extra}
        s.add(Record("ga-ethics", "contribution", f"https://peachfile.example/{i}", title=donor, text="", published=d,
                     channel="api", evidence="primary", key=f"g{i}", meta=m))
    # an FEC line read before the FEC's own fields were kept: left out, and counted as waiting for Ann Able's campaign
    s.add(Record("cand:a", "contribution", "https://fec.example/1", title="x", text="", published="2026-01-01", channel="api",
                 evidence="primary", key="fec-legacy", meta={"type": "contribution", "name": "Old, Line", "amount": 999,
                 "date": "2026-01-01", "recipient": "Ann Able", "candidate_id": "a", "committee": "C00000001", "filing_source": "FEC"}))
    s.flush()
    export_donors(s, ROOT, cands, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    R = {r["name"]: r for r in d["recipients"]}
    # Rick Jackson: his committee counted as his (linked by hand, with the source), his own money apart, his wife's not
    J = R["Rick Jackson"]
    assert J["cid"] == "rick-jackson" and J["party"] == "R" and J["committees"] == ["Jackson for Governor, Inc."]
    assert J["link_src"].startswith("https://caselaw.findlaw.com/") and J["raised"] == 40_007_600 and J["self"] == 40_000_000
    assert J["links"] == [{"committee": "Jackson for Governor, Inc.", "source": J["link_src"]}]      # each linked committee's own source
    assert {k: J["cy"]["2026"][k] for k in ("raised", "self", "n", "spent", "loans", "first", "last")} == {
        "raised": 40_007_600, "self": 40_000_000, "n": 3, "spent": 0, "loans": 0, "first": "2026-02-04", "last": "2026-07-30"}
    # the shares per cycle: Georgia addresses, gifts under $200, organizations, and money by donor type
    assert J["cy"]["2026"]["in_ga"] == 40_007_600 and J["cy"]["2026"].get("orgs", 0) == 0
    assert J["cy"]["2026"]["kinds"] == {"person": 7_600, "self": 40_000_000}
    kinds = {g["name"]: g["kind"] for g in d["gifts"]}
    assert kinds["Jackson, Richard L"] == "self" and kinds["Jackson, Marsha"] == "person"
    # Burt Jones's leadership committee: its own row, under him (not his campaign's money)
    W = R["WBJ Leadership Committee, Inc."]
    assert W["pac_of"] == "burt-jones" and W["pac_of_name"] == "Burt Jones" and W["pac_of_party"] == "R" and W["level"] == "committee"
    assert W["cy"]["2026"]["raised"] == 300_000          # August 2025 is in the 2026 cycle; both filer records, one row
    assert sum(1 for r in d["recipients"] if r["name"].startswith("WBJ")) == 1
    # "Friends of Ann Able": matched to her again at export (a full name inside the committee's), split by cycle
    A = R["Ann Able"]
    assert A["raised"] == 800 and set(A["cy"]) == {"2022", "2026"} and A["cy"]["2022"]["raised"] == 500
    assert A["held"] == 1 and d["skipped"]["fec_lines_awaiting_reread"] == 1
    assert "Some Other PAC" in R and not R["Some Other PAC"].get("pac_of") and not R["Some Other PAC"]["cid"]
    gift = next(g for g in d["gifts"] if g["name"] == "Doe, Jane")
    assert gift["cy"] == {"2022": [500, 1], "2026": [300, 1]} and gift["total"] == 800
    assert d["cycles"] == ["2022", "2026"]
    # and by calendar year: August 2025 and 2026 are one cycle but two years
    assert set(W["yr"]) <= {"2025", "2026"} and sum(v["raised"] for v in W["yr"].values()) == 300_000
    assert J["yr"] == {"2026": J["cy"]["2026"]} and set(A["yr"]) <= {"2021", "2022", "2025", "2026"}
    assert sum(v[0] for v in gift["yr"].values()) == 800 and set(d["years"]) >= set(W["yr"])
    # the rules on their own
    assert cycle_of("2025-01-01") == "2026" and cycle_of("2026-11-30") == "2026" and cycle_of("") == ""
    assert is_self("JACKSON, RICHARD L", "Rick Jackson") and is_self("Jones, William Burton", "Burt Jones")
    assert not is_self("Jones, Mary", "Burt Jones") and not is_self("Jackson Healthcare", "Rick Jackson")
    assert is_self("Ann Able", "Ann Able") and not is_self("Able", "Ann Able")


def test_money_moved_between_a_candidates_own_committees_counts_once(tmp_path):
    """A leadership committee paying into its candidate's campaign (For All Georgians to Jackson for Governor), or an old
    campaign account into the new one, is money counted where it was first given: left out of what the receiving
    committee raised and noted under moved. The candidate's own gifts, a committee naming itself, the leadership
    committee's gifts to other candidates and a business carrying the candidate's name are counted as before."""
    s = Store(tmp_path / "data")
    cands = [{"id": "bea-bold", "name": "Bea Bold", "electo_key": "BEA BOLD", "party": "D", "office": "State Senate",
              "levels": ["legislative"], "counties": [], "jurisdiction": ""}]
    lines = [  # (filer, filer type, donor, source type, amount, date, type)
        ("For All Georgians, Inc.", "Leadership Committee", "Republican Governors Association", "Non-Candidate Committee", 10_000_000, "2026-07-01", "contribution"),
        ("Jackson for Governor, Inc.", "Campaign Committee", "For All Georgians, Inc.", "Non-Candidate Committee", 5_000_000, "2026-07-30", "contribution"),
        ("Jackson for Governor, Inc.", "Campaign Committee", "For All Georgians Inc", "Non-Candidate Committee", 2_000, "2026-07-02", "in_kind"),
        ("Jackson for Governor, Inc.", "Campaign Committee", "Smith, Pat", "Individual", 1_000, "2026-07-30", "contribution"),
        ("For All Georgians, Inc.", "Leadership Committee", "Doe, Jane", "Individual", 500, "2026-07-03", "contribution"),
        ("Friends of Bea Bold", "Campaign Committee", "Roe, Rick", "Individual", 900, "2024-05-01", "contribution"),
        ("Committee to Elect Bea Bold", "Campaign Committee", "Friends of Bea Bold-Senate", "Corporation / Business / Unregistered Committee", 700, "2026-01-10", "contribution"),
        ("Committee to Elect Bea Bold", "Campaign Committee", "Bold, Bea", "Individual", 300, "2026-01-11", "contribution"),
        ("Committee to Elect Bea Bold", "Campaign Committee", "Committee to Elect Bea Bold", "Candidate / Candidate Committee", 40, "2026-01-12", "in_kind"),
        ("Committee to Elect Bea Bold", "Campaign Committee", "For All Georgians, Inc.", "Non-Candidate Committee", 2_500, "2026-02-01", "contribution"),
        ("Committee to Elect Bea Bold", "Campaign Committee", "Bea Bold Farms LLC", "Corporation / Business / Unregistered Committee", 250, "2026-02-02", "contribution"),
        # the leadership committee's own candidate's campaign under a name that never filed: by the chair's surname
        ("For All Georgians, Inc.", "Leadership Committee", "Jackson for Governor", "Corporation / Business / Unregistered Committee", 1_500, "2026-07-05", "contribution"),
        # the candidate's own name filed as a committee: her own money
        ("Committee to Elect Bea Bold", "Campaign Committee", "Bold, Bea", "Candidate/Campaign Committee", 600, "2026-01-13", "contribution"),
        # Georgia's shared business / unregistered committee row: a political committee by its name, else a business
        ("Committee to Elect Bea Bold", "Campaign Committee", "Republican Governors Association", "Corporation / Business / Unregistered Committee", 5_000, "2026-02-03", "contribution"),
        ("Committee to Elect Bea Bold", "Campaign Committee", "Georgia Bankers Association", "Corporation / Business / Unregistered Committee", 1_000, "2026-02-04", "contribution"),
    ]
    for i, (filer, ft, donor, st, amt, d, t) in enumerate(lines):
        m = {"type": t, "name": donor, "source_type": st, "amount": amt, "date": d, "recipient": filer, "recipient_kind": "committee",
             "filer_name": filer, "filer_type": ft, "ga_system": "peachfile", "filing_source": "GA Ethics", "city": "Atlanta",
             "state": "GA", "donor_entity": donor, "line": str(i)}
        s.add(Record("ga-ethics", "contribution", f"https://peachfile.example/{i}", title=donor, text="", published=d,
                     channel="api", evidence="primary", key=f"g{i}", meta=m))
    s.flush()
    export_donors(s, ROOT, cands, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    R = {r["name"]: r for r in d["recipients"]}
    J, F, B = R["Rick Jackson"], R["For All Georgians, Inc."], R["Bea Bold"]
    assert F["pac_of"] == "rick-jackson" and F["raised"] == 10_000_500       # the leadership committee: what it raised
    assert F["moved"]["total"] == 1_500 and F["moved"]["from"] == [{"name": "Jackson for Governor", "total": 1_500}]
    assert J["raised"] == 1_000 and J["in_kind"] == 0 and J["cy"]["2026"]["raised"] == 1_000      # the campaign: not again
    assert J["moved"] == {"total": 5_002_000, "n": 2, "from": [{"name": "For All Georgians, Inc.", "total": 5_000_000},
                                                               {"name": "For All Georgians Inc", "total": 2_000}],
                          "cy": {"2026": 5_002_000}, "yr": {"2026": 5_002_000}}
    # Bea Bold: her Senate account's money moved into the new campaign counts once; her own gift, the committee naming
    # itself, For All Georgians' gift to her and her farm's gift count as before
    assert B["moved"]["total"] == 700 and B["moved"]["cy"] == {"2026": 700} and B["self"] == 300 + 600
    assert B["raised"] == 900 + 300 + 40 + 2_500 + 250 + 600 + 5_000 + 1_000
    assert B["kinds"] == {"person": 900, "self": 900, "committee": 40 + 2_500 + 5_000, "org": 250 + 1_000}
    kinds = {g["name"]: g["kind"] for g in d["gifts"]}
    assert kinds["Republican Governors Association"] == "committee" and kinds["Georgia Bankers Association"] == "org" and kinds["Bold, Bea"] == "self"
    named = {(g["name"], g["r"]) for g in d["gifts"]}
    assert ("For All Georgians, Inc.", B["i"]) in named and ("For All Georgians, Inc.", J["i"]) not in named
    assert ("Bea Bold Farms LLC", B["i"]) in named and not any(n == "Friends of Bea Bold-Senate" for n, _ in named)
    assert d["skipped"]["transfers_between_own_committees"] == 4
    assert d["totals"]["raised"] == sum(r["raised"] for r in d["recipients"]) == 10_000_500 + 1_000 + 3_990 + 6_600


def test_source_kind_is_the_filings_own_word():
    from gacrawl.donors import own_campaign, source_kind

    assert source_kind("Individual") == "person" and source_kind("Lobbyist") == "person" and source_kind("Spouse / Dependent Child") == "person"
    assert source_kind("Corporation / Business / Unregistered Committee") == "org" and source_kind("Lobbying Organization") == "org"
    assert source_kind("Committee") == "committee" and source_kind("Non-Candidate Committee") == "committee"
    assert source_kind("Candidate / Candidate Committee") == "committee" and source_kind("Self") == "self"
    assert source_kind("Other") is None and source_kind("") is None and source_kind(None) is None
    assert own_campaign("JACKSON, RICHARD L.", "Jackson for Governor, Inc.") and own_campaign("Rick Jackson", "Jackson for Governor")
    assert not own_campaign("JACKSON, RICHARD L.", "Georgians for Jackson")        # not the campaign form of the name
    assert not own_campaign("Smith, Ann", "Jackson for Governor, Inc.") and not own_campaign("", "Jackson for Governor")


def test_donor_kinds_from_the_filing_and_totals_over_every_line(tmp_path):
    """Georgia's lines say what the donor is (source_type): "The Justice Project" and "AFSCME" are not people, whatever
    their names look like; a person of a campaign's surname who gave it more than anyone else may is the candidate; and
    totals and each recipient's kinds cover every line, not only the listed gifts."""
    s = Store(tmp_path / "data")
    cands = [{"id": "a", "name": "Ann Able", "electo_key": "ANN ABLE", "party": "D", "office": "State Senate", "levels": ["state"], "counties": ["fulton"], "jurisdiction": ""}]
    rows = [  # recipient, candidate id, donor, amount, source_type, filer_type, donor entity
        ("Democratic Party of Georgia", "", "The Justice Project", 1350000, "Corporation / Business / Unregistered Committee", "Party Committee", "d1"),
        ("One Georgia", "", "AFSCME", 1000000, "Other", "Leadership Committee", "d2"),
        ("Take Back Georgia", "", "SAVE AMERICA", 2000000, "Committee", "Independent Committee", "d3"),
        ("Quimby for Governor, Inc.", "", "QUIMBY, PAT L.", 13000000, "Individual", "Candidate Committee", "d4"),
        ("Quimby for Governor, Inc.", "", "QUIMBY, MARY", 5000, "Individual", "Candidate Committee", "d5"),
        ("Ann Able", "a", "Able, Ann", 20000, "Self", "Candidate", "d6"),
        ("Ann Able", "a", "Smith, Joe", 300, "Individual", "Candidate", "d7"),
        ("Ann Able", "a", "Smith Holdings LLC", 700, "Individual", "Candidate", "d8")]
    for i, (rec, cid, name, amt, st, ft, ent) in enumerate(rows):
        m = {"type": "contribution", "name": name, "amount": amt, "date": f"2026-02-{i+1:02d}", "city": "Atlanta", "state": "GA", "recipient": rec,
             "recipient_kind": "candidate" if cid or "Quimby" in rec else "committee", "donor_is_org": False, "source_type": st, "filer_type": ft,
             "filer_name": rec, "filing_source": "Georgia State Ethics Commission", "donor_entity": ent, **({"candidate_id": cid} if cid else {})}
        s.add(Record(f"cand:{cid}" if cid else "ga-ethics", "contribution", f"https://filings.example/{i}", title=name, text="", published=m["date"],
                     channel="api", evidence="primary", key=f"ga2|x|{i}", meta=m))
    s.flush()
    export_donors(s, ROOT, cands, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    kind = {g["name"]: g["kind"] for g in d["gifts"]}
    assert kind["The Justice Project"] == "org" and kind["SAVE AMERICA"] == "committee"      # as filed, not from the name
    assert kind["AFSCME"] == "org"                    # filed as "Other" (the filer passed over "Individual"): not a person
    assert kind["Able, Ann"] == "self" and kind["Smith, Joe"] == "person" and kind["Smith Holdings LLC"] == "org"
    jack = next(g for g in d["gifts"] if g["name"] == "QUIMBY, PAT L.")
    assert jack["kind"] == "self" and jack["self_inferred"] is True
    assert kind["QUIMBY, MARY"] == "person"                                                 # the same surname, an ordinary gift
    R = {r["name"]: r for r in d["recipients"]}
    assert R["Quimby for Governor, Inc."]["kinds"] == {"person": 5000, "self": 13000000} and R["Quimby for Governor, Inc."]["self"] == 13000000
    assert R["Ann Able"]["kinds"] == {"org": 700, "person": 300, "self": 20000}
    T = d["totals"]
    assert T["raised"] == sum(r[3] for r in rows) and T["gifts"] == 8 and T["pairs"] == 8 and T["donors"] == 8 and T["recipients"] == 5
    assert T["listed"] == 8 and T["self_inferred"] == 1
    by = {b["k"]: b["t"] for b in d["by"]["kind"]}
    assert by["self"] == 13020000 and by["person"] == 5300


def test_federal_campaign_gets_the_fecs_own_totals_per_cycle(tmp_path):
    """A federal campaign's money on the Donors tab is what the FEC reports its committees raised (gifts under $200
    included), per cycle; the itemized lines stay the detail. Only cycles from 2016 on, only the candidate's committees."""
    import gacrawl.connections as cx

    cx._COMMITTEES.clear()
    s = Store(tmp_path / "data")
    cands = [{"id": "f", "name": "Fed Person", "electo_key": "FED PERSON", "party": "D", "office": "U.S. Senate",
              "levels": ["federal"], "counties": [], "jurisdiction": ""}]
    for i, (com, d) in enumerate([("C001", "2026-04-06"), ("C002", "2026-05-01")]):
        s.add(Record("cand:f", "contribution", f"https://fec.example/{i}", title="x", text="", published=d, channel="api",
                     evidence="primary", key=f"f{i}", meta={"type": "contribution", "name": f"Giver, {i}", "amount": 1000,
                     "date": d, "recipient": "Fed Person", "candidate_id": "f", "committee": com, "filing_source": "FEC",
                     "fec_v": 2, "donor_entity": f"g{i}", "state": "GA"}))
    s.kv_set("fec_totals", json.dumps({
        "C001": {"checked": "2026-11-09", "cycles": {"2026": {"receipts": 30_000_000, "individual_contributions": 25_000_000,
                 "individual_unitemized_contributions": 9_000_000, "coverage_end_date": "2026-06-30T00:00:00"},
                 "2014": {"receipts": 5}}},
        "C002": {"checked": "2026-11-09", "cycles": {"2026": {"receipts": 3_000_000, "coverage_end_date": "2026-09-30T00:00:00"}}},
        "C999": {"checked": "2026-11-09", "cycles": {"2026": {"receipts": 99}}}}))
    s.flush()
    export_donors(s, ROOT, cands, tmp_path / "donors.json", log=lambda *a: None)
    R = {r["name"]: r for r in json.loads((tmp_path / "donors.json").read_text())["recipients"]}["Fed Person"]
    assert R["fec_cy"] == {"2026": {"receipts": 33_000_000, "individual": 25_000_000, "unitemized": 9_000_000, "through": "2026-09-30"}}
    assert R["raised"] == 2000          # the itemized lines, unchanged
