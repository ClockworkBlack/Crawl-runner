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


# --- Nov. 15, 2026: the cases behind Electo's "Share of contests won" chart -------------------------------------------
def test_ballot_question_filed_as_an_office():
    c = C(y=1996, t="Ag Products Fees", office="Ag Products Fees",
          o=[["Amendment #4 - No", "", 1089801, 0, "won"], ["Amendment #4 - Yes", "", 714285, 0, ""]])
    assert R.repair_contest(c)["lvl"] == "question"
    c = C(o=[["For Approval", "", 43231, 0, "won"], ["Against Approval", "", 19064, 0, ""]])
    assert R.repair_contest(c)["lvl"] == "question"
    c = C(o=[["Yes (Non-Partisan)", "", 18, 0, "won"], ["No (Non-Partisan)", "", 2, 0, ""]])
    c = R.repair_contest(c)
    assert c["lvl"] == "question" and [x[0] for x in c["o"]] == ["Yes", "No"]
    c = C(o=[["Noe Yesenia", "", 18, 0, "won"], ["Ana Forde", "", 2, 0, ""]])       # people, not choices
    assert R.repair_contest(c)["lvl"] == "local"


def test_nonpartisan_and_cut_incumbent_marks_off_names():
    c = C(y=2008, lvl="judicial", o=[["Gary Andrews Partisan", "", 9, 0, "unopposed"]])
    assert R.repair_contest(c)["o"][0][0] == "Gary Andrews"
    c = C(y=2008, lvl="judicial", o=[["Jr. Partisan Charles B. Mikell", "", 9, 0, "unopposed"]])
    assert R.repair_contest(c)["o"][0][0] == "Charles B. Mikell Jr."
    c = C(y=2006, lvl="judicial", o=[["Sr. (Non-Partisan) Marvin S. Arrington", "", 9, 0, "unopposed"]])
    assert R.repair_contest(c)["o"][0][0] == "Marvin S. Arrington Sr."
    c = C(y=2012, lvl="judicial", o=[["Amanda H. Mercier (I", "", 9, 0, "won"], ["Matthew Ledbetter(I", "", 4, 0, ""]])
    o = R.repair_contest(c)["o"]
    assert [(x[0], x[3]) for x in o] == [("Amanda H. Mercier", 1), ("Matthew Ledbetter", 1)]


def test_vice_president_rows_and_county_slices_are_not_contests():
    assert R.repair_contest(C(y=2008, office="Vice President", t="Vice President", j="Not Available",
                              o=[["Rosa Clemente", "", 13, 0, "won"], ["Jeff Stath", "", 1, 0, ""]])) is None
    assert R.repair_contest(C(office="U.S. House", dist="7", t="U.S. House District 7", j="Forsyth",
                              o=[['W. Robert "Bob" Woodall R.', "", 80603, 1, "led"], ["Thomas D. Wight", "D", 60112, 0, ""]])) is None
    assert R.repair_contest(C(office="U.S. House", dist="7", lvl="federal", t="U.S. House District 7",
                              o=[["Rob Woodall", "R", 113557, 1, "won"], ["Thomas D. Wight", "D", 60112, 0, ""]])) is not None


def test_county_offices_filed_together_are_taken_apart():
    S = Counter()
    # one county in its table: that county's contest
    c = C(y=2012, t="CLERK OF STATE COURT", o=[["Diane B. Webb", "R", 219785, 1, "unopposed"]], cty={"Cobb": [219785]})
    [c] = R.county_contests(c, S)
    assert c["j"] == "Cobb" and "cty" not in c
    # three counties' clerks under one title, one "leader" for all: one contest each, two people of one name kept apart
    o = [["Ann Lee", "R", 900, 0, "led"], ["Bo Diaz", "R", 300, 0, ""], ["Cy Fox", "R", 700, 0, ""], ["Di Roe", "R", 650, 0, ""],
         ["Becky Robinson", "R", 500, 0, ""]]
    cty = {"Fulton": [900, 300, 0, 0, 200], "Gwinnett": [0, 0, 700, 0, 0], "Haralson": [0, 0, 0, 650, 300]}
    out = R.county_contests(C(y=2012, stage="primary", p="R", t="CLERK, SUPERIOR COURT", o=o, cty=cty), S)
    by = {c["j"]: c for c in out}
    assert sorted(by) == ["Fulton", "Gwinnett", "Haralson"]
    assert [(x[0], x[2], x[4]) for x in by["Fulton"]["o"]] == [("Ann Lee", 900, "won"), ("Bo Diaz", 300, ""), ("Becky Robinson", 200, "")]
    assert by["Gwinnett"]["o"][0][4] == "unopposed" and by["Haralson"]["tot"] == 950
    # a circuit's judges ran in every county of the circuit: left as one contest
    j = C(y=2012, lvl="judicial", t="Superior Court Judge, Alcovy Circuit", o=[["A", "", 60, 0, "won"], ["B", "", 40, 0, ""]],
          cty={"Newton": [30, 20], "Walton": [30, 20]})
    assert R.county_contests(j, S) == [j] and not j["j"]
    # a statewide court is never a county's
    a = C(y=2012, lvl="judicial", t="Appeals Court Judge, Boggs", o=[["Boggs", "", 9, 0, "unopposed"]], cty={"Appling": [9]})
    assert R.county_contests(a, S) == [a] and not a["j"]


def test_party_from_another_copy_and_from_the_primary():
    from gacrawl.history.build import _party_from_copies, _party_from_primary, _one_contest_two_titles
    best = C(y=2000, lvl="federal", office="U.S. Senate", o=[["Zell Miller", "", 1413224, 0, "won"], ["Mack Mattingly", "", 920478, 0, ""]],
             item="sos_pdf:a")
    oe = C(y=2000, lvl="federal", office="U.S. Senate", o=[["Zell Miller", "D", 1413224, 0, "won"], ["Mack Mattingly", "R", 920478, 0, ""]],
           item="openelections:a", basis="compiled")
    A = {}
    _party_from_copies(best, [oe], A)
    assert [x[1] for x in best["o"]] == ["D", "R"]
    # "I" from the transcription is the state's incumbent mark when the state's file marks the incumbent
    best = C(y=2012, lvl="legislative", office="State House", dist="89", o=[["Stacey Abrams", "", 23292, 1, "unopposed"]])
    _party_from_copies(best, [C(y=2012, o=[["Stacey Abrams", "I", 23292, 0, "unopposed"]], item="openelections:b")], A)
    assert best["o"][0][1] == ""
    pri = C(y=2012, date="2012-07-31", stage="primary", p="D", lvl="legislative", office="State House", dist="89",
            o=[["Stacey Abrams", "", 7059, 1, "unopposed"]])
    S = Counter()
    _party_from_primary([best, pri], S)
    assert best["o"][0][1] == "D" and pri["o"][0][1] == ""
    ind = C(y=2014, lvl="legislative", office="State House", dist="145", o=[["Rusty Kidd", "", 9934, 1, "unopposed"]])
    _party_from_primary([ind], S)
    assert ind["o"][0][1] == ""                         # in no primary: no party is made up


def test_one_contest_under_two_titles():
    from gacrawl.history.build import _one_contest_two_titles
    st = C(y=2000, lvl="statewide", office="Public Service Commission", dist="#2", t="Public Service Commission (seat 2 on the ballot)",
           o=[["Stan Wise", "R", 1197415, 0, "won"], ["Jim Boyd", "D", 928005, 0, ""]], item="sos_pdf:a")
    oe = C(y=2000, lvl="statewide", office="Public Service Commission", dist="5", t="Public Service Commission District 5",
           o=[["Stan Wise", "R", 1197415, 0, "won"], ["Jim Boyd", "D", 928005, 0, ""]], item="openelections:a", basis="compiled")
    out = _one_contest_two_titles([st, oe], {})
    assert out == [st] and st["t"] == "Public Service Commission District 5" and st["dist"] == "5"
    # three seats' candidates in one transcribed contest, each counted by the state under its own title (2010)
    seats = [C(y=2010, lvl="judicial", t=f"Appeals Court Judge, {n}", o=[[n, "", v, 0, "unopposed"]], item="sos_pdf:b")
             for n, v in (("Barnes", 1910477), ("Adams", 1894387))]
    merged = C(y=2010, lvl="judicial", t="Appeals Court Judge", item="openelections:b", basis="compiled",
               o=[["Barnes", "", 1910477, 0, ""], ["Adams", "", 1894387, 0, ""]])
    assert _one_contest_two_titles(seats + [merged], {}) == seats


def test_check_warns_of_these(tmp_path):
    b = _bundle(tmp_path, [
        {"t": "homestead", "lvl": "local", "tot": 20, "o": [["Yes", "", 18, 0, "won"], ["No", "", 2, 0, ""]], "i": 0},
        {"t": "Probate", "lvl": "judicial", "j": "Fulton", "tot": 9, "o": [["Gary Andrews Partisan", "", 9, 0, "unopposed"]], "i": 1},
    ])
    W = check_bundle(b)["warnings"]
    assert any("ballot question filed" in k for k in W) and any("nonpartisan or incumbent mark" in k for k in W)


# --- Nov. 15, 2026 (second pass) -------------------------------------------------------------------------------------
def test_an_independent_who_holds_a_partisan_seat_stays_independent():
    c = C(y=2014, lvl="legislative", office="State House", dist="145", item="sos_zip:a",
          o=[['E. Culver "Rusty" Kidd', "I", 9934, 1, "unopposed"]])
    assert R.repair_contest(c)["o"][0][1] == "I"
    j = C(y=2012, lvl="judicial", office="Superior Court Judge", item="sos_zip:a", o=[["Ann Lee", "I", 99, 1, "unopposed"]])
    assert R.repair_contest(j)["o"][0][1] == ""         # a judge: the incumbent mark, as before


def test_a_statewide_courts_seats_filed_as_one_contest():
    o = [["Anne E. Barnes", "", 1004473, 0, "led"], ["A. Harris Adams", "", 995946, 0, ""], ["Debra Bernes", "", 309080, 0, ""],
         ["Mike Sheffield", "", 207499, 0, ""], ["Howard Mead", "", 207136, 0, ""], ["Thomas Rawlings", "", 250000, 0, ""]]
    c = C(y=2004, lvl="judicial", office="Appeals Court Judge", t="Appeals Court Judge", item="openelections:a", basis="compiled", o=o)
    out = R.court_seats(c)
    assert [(n["t"], len(n["o"]), n["o"][0][4]) for n in out] == [
        ("Appeals Court Judge, Barnes", 1, "unopposed"), ("Appeals Court Judge, Adams", 1, "unopposed"),
        ("Appeals Court Judge (open seat)", 4, "led")]
    assert len({n["office"] for n in out}) == 3
    real = C(y=2004, lvl="judicial", office="Appeals Court Judge", t="Appeals Court Judge", item="openelections:b", basis="compiled",
             o=[["Debra Bernes", "", 1109267, 0, "led"], ["Howard Mead", "", 1018419, 0, ""], ["Mike Sheffield", "", 524140, 0, ""]])
    assert R.court_seats(real) == [real]                # three people for one seat (vote for one): left alone


def test_two_seats_of_a_circuit_crossed_in_one_countys_file():
    a = C(y=2012, lvl="judicial", t="Superior Court Judge, Appalachian Circuit (Mercier)", office="m",
          o=[["Amanda H. Mercier", "", 10599, 1, "won"], ["Brenda S. Weaver", "", 4977, 1, ""], ["Write-in", "", 85, 0, ""]],
          cty={"Fannin": [5316, 0, 31], "Gilmer": [0, 4977, 44], "Pickens": [5283, 0, 10]})
    b = C(y=2012, lvl="judicial", t="Superior Court Judge, Appalachian Circuit (Weaver)", office="w",
          o=[["Brenda S. Weaver", "", 11003, 1, "won"], ["Amanda H. Mercier", "", 4569, 1, ""], ["Write-in", "", 112, 0, ""]],
          cty={"Fannin": [5411, 0, 32], "Gilmer": [0, 4569, 21], "Pickens": [5592, 0, 59]})
    S = Counter()
    R._mirrored_circuit_seats([a, b], S)
    assert a["o"] == [["Amanda H. Mercier", "", 15168, 1, "unopposed"], ["Write-in", "", 62, 0, ""]]
    assert b["o"] == [["Brenda S. Weaver", "", 15980, 1, "unopposed"], ["Write-in", "", 135, 0, ""]]
    assert a["cty"]["Gilmer"] == [4569, 21] and b["tot"] == 16115
    # two people who did run against each other in every county: nothing to put back
    x = C(y=2012, lvl="judicial", t="s1", o=[["A B", "", 60, 0, "won"], ["C D", "", 40, 0, ""]], cty={"X": [30, 20], "Y": [30, 20]})
    y = C(y=2012, lvl="judicial", t="s2", o=[["A B", "", 10, 0, ""], ["C D", "", 90, 0, "won"]], cty={"X": [5, 45], "Y": [5, 45]})
    R._mirrored_circuit_seats([x, y], S)
    assert x["o"][0][2] == 60 and len(x["o"]) == 2


def test_a_party_from_a_published_account():
    c = C(y=1998, date="1998-11-03", lvl="statewide", office="Public Service Commission",
          o=[["Lauren McDonald", "", 638065, 0, "led"], ["Jim Cole", "", 541817, 0, ""]])
    other = C(y=1998, date="1998-11-03", lvl="statewide", office="Public Service Commission",
              o=[["Bobby Baker", "R", 927696, 0, "won"]])
    R._known_parties([c, other], Counter())
    assert [x[1] for x in c["o"]] == ["D", ""] and other["o"][0][1] == "R"
