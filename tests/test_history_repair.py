"""The rules every source's contests pass through (gacrawl/history/repair.py) and the bundle check (check.py): each
test is a case found in the real files on Nov. 14, 2026."""
from collections import Counter

from gacrawl.history import repair as R
from gacrawl.history.check import check_bundle
from gacrawl.history.model import nice_name, party_code, tidy_name


def C(**k):
    c = {"scope": "ga", "y": 2014, "date": "2014-11-04", "stage": "general", "st": "GA", "office": "X", "dist": "", "lvl": "local",
         "p": "", "j": "", "t": "X", "tot": None, "o": [], "src": {"t": "s", "u": "u"}, "basis": "official", "item": "sos_zip:a"}
    c.update(k)
    if c["tot"] is None and c["o"]:
        c["tot"] = sum(x[2] or 0 for x in c["o"]) or None
    return c


def test_party_labels():
    assert party_code("Minnesota Democratic–Farmer–Labor Party") == "D"
    assert party_code("DEM-NPL") == "D" and party_code("R-D") == "R" and party_code("DEM/WF") == "D"
    assert party_code("Republican Party (United States)") == "R"
    assert party_code("Working Families") == "O" and party_code("N/A") == "" and party_code("Combined Parties") == ""


def test_names():
    assert nice_name('Benjamin ""ben"" Bubar') == 'Benjamin "Ben" Bubar'
    assert tidy_name("Wm (Doc)collins") == "Wm (Doc) Collins"
    assert tidy_name("E.h. (Bucky) Woods") == "E.H. (Bucky) Woods"
    assert tidy_name('W.J. "Billy," Tauzin III') == 'W.J. "Billy" Tauzin III'
    assert nice_name("MCDONALD, LARRY P.") == "Larry P. McDonald"


def test_incumbent_mark_is_not_a_party_and_party_tail_is_read():
    c = C(o=[["J. Heneghan", "I", 10, 1, "won"], ["Lynn A. Westmoreland R.", "", 5, 1, ""], ["Wayne Hall Ind", "", 2, 0, ""]])
    R.repair_contest(c)
    assert [(x[0], x[1]) for x in c["o"]] == [("J. Heneghan", ""), ("Lynn A. Westmoreland", "R"), ("Wayne Hall", "I")]


def test_statewide_votes_restored_from_counties():
    # the spelling a row is filed under lost its own votes; the county rows still have them
    c = C(lvl="judicial", o=[["Carol W. Hunstein", "", 156041, 1, "won"], ["Write-in", "", 8721, 0, ""]],
          cty={"Fulton": [600000, 4000], "DeKalb": [578631, 4721]})
    R.repair_contest(c)
    assert c["o"][0][2] == 1178631 and c["tot"] == 1187352 and c["o"][0][4] == "unopposed"


def test_spellings_of_one_candidate():
    c = C(item="openelections:x", lvl="federal", office="U.S. House", scope="ga",
          o=[["L. Westmoreland", "R", 199246, 0, "won"], ["Lynn Westmoreland", "R", 33134, 0, ""]],
          cty={"Coweta": [199246, 0], "Troup": [0, 33134]})
    R.repair_contest(c)
    assert len(c["o"]) == 1 and c["o"][0][2] == 232380 and c["cty"]["Troup"] == [33134]
    # a father and a son are two candidates
    c = C(o=[["John Smith", "R", 500, 0, ""], ["John Smith Jr.", "R", 400, 0, ""]])
    R.repair_contest(c)
    assert len(c["o"]) == 2


def test_total_row_and_party_lines():
    # Connecticut as reported: a candidate's party lines and a row of their total
    c = C(scope="us", st="CT", lvl="federal", office="U.S. House", dist="5", item="reported:x", basis="reported", tot=253066,
          o=[["Jahana Hayes", "O", 146353, 1, "won"], ["Jahana Hayes", "D", 139000, 1, "won"], ["Chris Shea", "R", 106667, 0, ""],
             ["Jahana Hayes", "O", 7353, 1, "won"]])
    R.repair_contest(c)
    assert [(x[0], x[1], x[2]) for x in c["o"]] == [("Jahana Hayes", "D", 146353), ("Chris Shea", "R", 106667)]
    # MEDSL, Kansas 2022: three lines of one candidate
    c = C(scope="us", st="KS", lvl="federal", office="U.S. Senate", item="medsl:x", tot=1004956,
          o=[["Jerry Moran", "R", 602976, 0, "won"], ["Mark R. Holland", "D", 372214, 0, ""], ["David C. Graham", "L", 21436, 0, ""],
             ["David C. Graham", "O", 7137, 0, ""], ["Lib David C. Graham", "O", 1193, 0, ""]])
    R.repair_contest(c)
    assert [x[2] for x in c["o"]] == [602976, 372214, 29766] and c["o"][2][0] == "David C. Graham"


def test_nobody_voted():
    assert R.repair_contest(C(lvl="question", o=[["YES", "", 0, 0, ""], ["NO", "", 0, 0, ""]])) is None
    c = R.repair_contest(C(o=[["J. Heneghan", "", 0, 1, "unopposed"]]))
    assert c["o"][0][2] is None and c["tot"] is None
    assert R.repair_contest(C(stage="primary runoff", o=[["Butch Parrish", "", 0, 0, "won"]])) is None


def test_kinds_of_georgia_elections():
    S = Counter()
    sp = C(item="openelections:a", date="2015-01-06", y=2015, stage="general", sp=1, lvl="legislative", office="State House", dist="120",
           o=[["A B", "R", 900, 0, "won"], ["C D", "D", 300, 0, ""]])
    big = [C(item="sos_pdf:g", date="2010-11-02", y=2010, stage="general runoff", lvl="legislative", office="State House", dist=str(i),
             o=[["A B", "R", 900 + i, 0, "won"], ["C D", "D", 300, 0, ""]]) for i in range(25)]
    ro_zip = C(item="sos_zip:r", date="2012-11-06", y=2012, stage="general runoff", lvl="legislative", office="State Senate", dist="30",
               o=[["Mike Dugan", "", 3606, 0, "won"], ["Bill Hembree", "", 2857, 0, ""]])
    ro_other = C(item="sos_zip:r", date="2012-11-06", y=2012, stage="general runoff", office="Mayor-Brookhaven",
                 o=[["J. Max Davis", "", 4177, 0, "won"], ["Sandy Murray", "", 2194, 0, ""]])
    ro_oe = C(item="openelections:r", date="2012-12-04", y=2012, stage="general", sp=1, lvl="legislative", office="State Senate", dist="30",
              o=[["Mike Dugan", "R", 3606, 0, "won"], ["Bill Hembree", "R", 2857, 0, ""]])
    late = C(item="sos_pdf:p", date="1998-12-01", y=1998, stage="primary runoff", lvl="legislative", office="State Senate", dist="38",
             o=[["Horacena Tate", "", 3850, 0, "won"], ["Gordon Joyner", "", 2625, 0, ""]])
    cs = [sp, ro_zip, ro_other, ro_oe, late] + big
    R.reconcile_ga(cs, S)
    assert sp["stage"] == "special"                                   # "special__general" is a special election
    assert all(c["stage"] == "general" for c in big)                  # a whole general election is not its own runoff
    assert ro_zip["date"] == ro_other["date"] == "2012-12-04"         # the runoff is dated when it was held
    assert ro_zip["stage"] == ro_oe["stage"] == "special runoff" and ro_other["stage"] == "general runoff"
    assert late["stage"] == "special primary runoff"


def test_federal_files_taken_apart():
    S = Counter()
    base = dict(scope="us", lvl="federal", office="U.S. House", item="fec:f", basis="official")
    la = C(**base, st="LA", y=2008, date="2008", stage="primary", dist="1",
           o=[['"Steve" Scalise', "R", None, 0, "unopposed"], ['"Jim" Harlan', "D", 37883, 0, ""], ["Vinny Mendoza", "D", 12000, 0, ""]])
    la5 = C(**base, st="LA", y=2014, date="2014-11-04", stage="general", dist="5",
            o=[["Ralph Lee Abraham", "R", 55489, 0, "won"], ['"Jamie" Mayo', "D", 67611, 0, ""], ["Zach Dasher", "R", 53628, 0, ""]])
    me = C(**base, st="ME", y=2018, date="2018-11-06", stage="general", dist="2",
           o=[["Jared F. Golden", "D", 132013, 0, "won"], ["Bruce L. Poliquin", "R", 134184, 1, ""]])
    nc = C(**base, st="NC", y=2018, date="2018-11-06", stage="general", dist="9",
           o=[["Dan Bishop", "R", 96573, 0, "won"], ["Mark Harris", "R", 139246, 0, ""], ["Dan McCready", "D", 138341, 0, ""]])
    out = R.fix_federal([la, la5, me, nc], S)
    pri = [c for c in out if c["st"] == "LA" and c["stage"] == "primary"]
    assert sorted(c["p"] for c in pri) == ["D", "R"] and [c["o"][0][4] for c in pri if c["p"] == "D"] == ["won"]
    assert [x[4] for x in la5["o"]] == ["runoff", "runoff", ""] or sorted(x[4] for x in la5["o"]) == ["", "runoff", "runoff"]
    assert "runoff" in la5["note"]
    assert me["o"][0][4] == "won" and "ranked-choice" in me["note"]
    assert [x[0] for x in nc["o"]] == ["Mark Harris", "Dan McCready"] and nc["o"][0][4] == "led" and nc["tot"] == 277587


def test_marked_lines_one_candidate_one_winner():
    c = C(scope="us", st="NY", lvl="federal", office="U.S. House", dist="18", item="fec:f", y=2012,
          o=[["Nan Hayworth", "R", 133049, 1, "won"], ["Sean Patrick Maloney", "D", 132456, 0, ""], ["Sean Patrick # Maloney", "O", 11389, 0, ""]])
    R.repair_contest(c)
    assert c["o"][0][:3] == ["Sean Patrick Maloney", "D", 143845] and c["o"][0][4] == "won" and c["o"][1][4] == ""
    c = C(scope="us", st="IN", lvl="federal", office="U.S. House", dist="2", item="fec:f", y=2022,
          o=[["Rudolph (Rudy), III # Yakym", "R", 125537, 0, "won"], ["Rudolph (Rudy) Yakym III", "R", 118997, 0, ""],
             ["Paul D. Steury", "D", 62955, 0, ""], ["Paul Steury", "D", 62792, 0, ""]], tot=376536)
    R.repair_contest(c)
    assert [(x[0], x[2]) for x in c["o"]] == [("Rudolph (Rudy) Yakym III", 125537), ("Paul D. Steury", 62955)] and c["tot"] == 188492


def _bundle(tmp_path, contests, scope="ga", year=2014, eid="h20141104gen", kind="general", date="2014-11-04"):
    import json
    for i, c in enumerate(contests):
        c.setdefault("i", i); c.setdefault("b", "official"); c.setdefault("p", ""); c.setdefault("j", ""); c.setdefault("lvl", "statewide")
        c.setdefault("off", c["t"]); c.setdefault("d", "")
    (tmp_path / f"{scope}_{year}.json").write_text(json.dumps({"year": str(year), "scope": scope, "elections": [
        {"eid": eid, "date": date, "kind": kind, "title": "t", "official": True, "c": contests}]}))
    (tmp_path / "index.json").write_text(json.dumps({"elections": [{"eid": eid, "scope": scope, "date": date, "kind": kind, "f": f"{scope}_{year}.json"}],
                                                      "audit": {"seats": {}}}))
    return tmp_path


def test_check_catches_what_cannot_be_right(tmp_path):
    good = {"t": "Lieutenant Governor", "tot": 100, "o": [["A B", "R", 60, 0, "won"], ["C D", "D", 40, 0, ""]], "cty": {"X": [60, 40]}}
    res = check_bundle(_bundle(tmp_path, [dict(good)]))
    assert not [k for k in res["errors"] if "certified" not in k and "anchor" not in k]
    bad = [
        {"t": "Q1", "tot": 100, "o": [["A B", "R", 70, 0, "won"], ["C D", "D", 40, 0, ""]]},                      # more votes than the total
        {"t": "Q2", "tot": 100, "o": [["A B", "R", 40, 0, "won"], ["C D", "D", 60, 0, ""]]},                      # the winner trails
        {"t": "Q3", "tot": 100, "o": [["A B", "R", 60, 0, "won"], ["C D", "D", 40, 0, ""]], "cty": {"X": [10, 40]}},  # counties do not add up
        {"t": "Q4", "tot": 100, "o": [["A B", "R", 45, 0, "won"], ["C D", "D", 40, 0, ""], ["E F", "L", 15, 0, ""]]},  # no majority
        {"t": "Q5", "tot": 100, "o": [['Benjamin ""ben"" Bubar', "R", 60, 0, "won"], ["C D", "Z", 40, 0, ""]]},     # name, party code
    ]
    res = check_bundle(_bundle(tmp_path, bad))
    keys = " | ".join(res["errors"])
    for want in ("more than the contest's total", "fewer votes than another", "county rows do not add up", "majority the law requires",
                 "damaged name", "unknown party code"):
        assert want in keys, want
    res = check_bundle(_bundle(tmp_path, [dict(good)], eid="h20150106gen", date="2015-01-06", year=2015))
    assert any("not on the general election day" in k for k in res["errors"])


def test_a_results_page_read_wrong_is_not_a_contest():
    from gacrawl.elections import result_fault
    walker = [{"name": "PO Box", "votes": 1105, "outcome": ""}, {"name": "LaFayette GA", "votes": 30728, "outcome": "won"}]
    assert "ballot question" in result_fault("Shall the Act be approved which provides a homestead exemption?", walker, 31833)
    assert "address" in result_fault("Mayor", walker, 31833)
    sugar = [{"name": n, "votes": v, "outcome": o} for n, v, o in (("Amber Chambers", 569, ""), ("Brandon Hembree", 1467, "won"), ("Alvin Hicks", 1400, ""),
                                                                  ("Karl P. Siegele", 637, ""), ("Samantha Piovesan", 1191, ""), ("Kalee Weiland", 780, ""))]
    assert "several contests" in result_fault("MAYOR", sugar, 6044)
    ok = [{"name": "Michael Caldwell", "votes": 3915, "outcome": "won"}, {"name": "Martha Jean Schindler", "votes": 1955, "outcome": ""}]
    assert result_fault("Mayor", ok, 5870) == ""


def test_fec_preferred_unless_its_row_is_broken():
    from gacrawl.history.build import choose
    def mk(item, st, w, wv, tot, other=("B C", 1000)):
        return {"scope": "us", "y": 2004, "date": "2004-11-02", "stage": "general", "st": st, "office": "U.S. Senate", "dist": "", "lvl": "federal",
                "p": "", "j": "", "t": "U.S. Senate · " + st, "tot": tot, "o": [[w, "D", wv, 0, "won"], [other[0], "R", other[1], 0, ""]],
                "src": {"t": item, "u": item}, "basis": "official", "item": item}
    A = {}
    kept = choose([mk("medsl:x", "MS", "Bush", 672660, 1139824), mk("fec:y", "MS", "Bush", 684981, 1152145)], A)
    assert kept[0]["item"] == "fec:y" and "MIT" in kept[0]["alt"]                     # a small difference: the FEC's count, with a note
    A = {}
    kept = choose([mk("medsl:x", "NY", "Charles E. Schumer", 4769824, 7447818), mk("fec:y", "NY", "Charles Schumer", 9539648, 11472699)], A)
    assert kept[0]["item"] == "medsl:x" and "does not add up" in kept[0]["alt"]       # the FEC's row counted twice: kept out, said so
