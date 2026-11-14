"""Party control of Congress, seats and net gains (gacrawl/history/congress.py), the House's party divisions
(sources.house_party_rows) and Wikipedia's national results tables (national.py, build.merge_national). No network."""
from collections import Counter

from gacrawl.history.build import merge_national
from gacrawl.history.congress import anchor_faults, build_congress, current_senate, party_on, regular_class, senate_election, side
from gacrawl.history.national import national_contests
from gacrawl.history.repair import repair_contest
from gacrawl.history.sources import house_party_rows, reported_where

PARTY_DIV = """<table>
<tr><th>Congress (Years)</th><th># of House Seats</th><th>Federalists</th><th>Democratic Republicans</th><th>Other</th><th>Del./Res.</th></tr>
<tr><td>4th (1795-1797)</td><td>106</td><td>47</td><td>59</td><td>0</td><td>1/0</td></tr>
<tr><th>Congress (Years)</th><th># of House Seats</th><th>Democrats</th><th>Republicans</th><th>Other</th><th>Del./Res.</th></tr>
<tr><td>103rd (1993&ndash;1995)</td><td>435</td><td>258</td><td>176</td><td>Independent (1)</td><td>4/1</td></tr>
<tr><td>104th (1995&ndash;1997)</td><td>435 <sup>4</sup></td><td>204</td><td>230</td><td>Independent (1)</td><td>4/1</td></tr>
<tr><td>119th (2025&ndash;2027)</td><td>435</td><td>215</td><td>220</td><td>0</td><td>5/1</td></tr>
</table>"""


def test_house_party_divisions():
    rows = house_party_rows(PARTY_DIV)
    assert [(r["n"], r["y"], r["seats"], r["D"], r["R"], r["O"]) for r in rows] == [
        (103, 1992, 435, 258, 176, 1), (104, 1994, 435, 204, 230, 1), (119, 2024, 435, 215, 220, 0)]   # Federalists' block left out


HOUSE_PAGE = """==Special elections==
{|class="wikitable"
|-
!{{ushr|TX|18|X}}
|Special
|{{plainlist}}
*{{Party stripe|Democratic Party (US)}}{{aye}} '''[[Someone Else]]''' (Democratic)
{{endplainlist}}
|}
==Illinois==
{{USCongressElectionTableHead|PVI=yes}}
|-
!{{ushr|IL|1|X}}
|{{shading PVI|D|18}}
|{{sortname|Jonathan|Jackson|dab=Illinois politician}}
|Democratic
|2022
|Incumbent re-elected.
|{{plainlist}}
*{{Party stripe|Democratic Party (US)}}{{aye}} '''[[Jonathan Jackson (Illinois politician)|Jonathan Jackson]]''' (Democratic)
*{{Party stripe|Republican Party (US)}}Christian Maxwell (Republican)
{{endplainlist}}
|-
!{{ushr|IL|17|X}}
|{{sortname|Eric|Sorensen}}
|Democratic
|Too close to call.
|{{plainlist}}
*{{Party stripe|Democratic Party (US)}}[[Eric Sorensen (politician)|Eric Sorensen]] (Democratic) 50.2%
*{{Party stripe|Republican Party (US)}}Dillan Vancil (Republican) 49.8%
{{endplainlist}}
|}
==Wyoming==
{|class="wikitable"
|-
!{{ushr|WY|AL|X}}
|{{sortname|Harriet|Hageman}}
|Republican
|{{plainlist}}
*{{Party stripe|Republican Party (US)}}{{aye}} '''Someone New''' (Republican)
*{{Party stripe|Democratic Party (US)}}Other Person (Democratic)
{{endplainlist}}
|}
"""

SENATE_PAGE = """== Race summary ==
=== Special elections during the preceding Congress ===
{| class="wikitable"
|-
! [[2026 United States Senate special election in Ohio|Ohio]]<br />(Class 3)
| [[Jon Husted]]
| Republican
| Interim appointee lost election.
| {{Plainlist |
*{{Party stripe|Democratic Party (US)}}{{aye}} '''[[Sherrod Brown]]''' (Democratic)
*{{Party stripe|Republican Party (US)}}[[Jon Husted]] (Republican)
}}
|}
=== Elections leading to the next Congress ===
{| class="wikitable"
|-
! [[2026 United States Senate election in Alaska|Alaska]]
| {{sortname|Dan|Sullivan|dab=U.S. senator}}
| Republican
| {{Plainlist |
*{{Party stripe|Democratic Party (US)}}[[Mary Peltola]] (Democratic)
*{{Party stripe|Republican Party (US)}}[[Dan Sullivan (U.S. senator)|Dan S. Sullivan]] (Republican)
}}
|-
! [[2026 United States Senate election in Georgia|Georgia]]
| {{sortname|Jon|Ossoff}}
| Democratic
| {{Plainlist |
*{{Party stripe|Democratic Party (US)}}{{aye}} '''[[Jon Ossoff]]''' (Democratic)
*{{Party stripe|Republican Party (US)}}[[Mike Collins (politician)|Mike Collins]] (Republican)
}}
|}
"""


def test_national_tables():
    assert reported_where("2026 United States House of Representatives elections", 2026) == {"national": 1, "office": "U.S. House", "sp": 0}
    assert reported_where("2026 United States Senate elections", 2026)["national"] == 1
    H = {(c["st"], c["dist"]): c for c in national_contests(HOUSE_PAGE, {"y": 2026, "office": "U.S. House"}, "u")}
    assert sorted(H) == [("IL", "1"), ("IL", "17"), ("WY", "AL")]          # the special election's table is not read
    il1 = H[("IL", "1")]
    assert [(x[0], x[1], x[2], x[3], x[4]) for x in il1["o"]] == [("Jonathan Jackson", "D", None, 1, "won"), ("Christian Maxwell", "R", None, 0, "")]
    assert il1["t"] == "U.S. House · Illinois District 1" and il1["natl"] == 1 and il1["tot"] is None and il1["basis"] == "reported"
    assert [x[4] for x in H[("IL", "17")]["o"]] == ["", ""]               # not called: nobody marked
    assert H[("IL", "17")]["o"][0][0] == "Eric Sorensen"                   # the share is not part of the name
    S = {(c["st"], c.get("sp", 0)): c for c in national_contests(SENATE_PAGE, {"y": 2026, "office": "U.S. Senate"}, "u")}
    assert sorted(S) == [("AK", 0), ("GA", 0), ("OH", 1)]
    assert S[("OH", 1)]["stage"] == "special" and S[("OH", 1)]["o"][0][:2] == ["Sherrod Brown", "D"] and S[("OH", 1)]["o"][0][4] == "won"
    assert not any(x[4] for x in S[("AK", 0)]["o"])
    assert S[("GA", 0)]["o"][0][3] == 1                                     # the incumbent


def _state_page(o, tot):
    return {"scope": "us", "y": 2026, "date": "2026-11-03", "stage": "general", "st": "IL", "office": "U.S. House", "dist": "17",
            "lvl": "federal", "p": "", "j": "", "t": "U.S. House · Illinois District 17", "tot": tot, "o": o,
            "src": {"t": "x", "u": "u"}, "basis": "reported", "item": "reported:x:1"}


def test_national_winner_marks_the_state_page_and_fills_gaps():
    nat = list(national_contests(HOUSE_PAGE.replace("[[Eric Sorensen (politician)|Eric Sorensen]]", "{{aye}} '''[[Eric Sorensen]]'''"),
                                 {"y": 2026, "office": "U.S. House"}, "u"))
    for c in nat:
        c["item"] = "reported:national:2"
    page = _state_page([["Eric Sorensen", "D", 150000, 1, "led"], ["Dillan Vancil", "R", 149000, 0, ""]], 299000)
    S = Counter()
    out = merge_national([page] + nat, S)
    il17 = [c for c in out if c.get("dist") == "17"]
    assert len(il17) == 1 and il17[0] is page and [x[4] for x in page["o"]] == ["won", ""]
    il1 = next(c for c in out if c.get("dist") == "1")
    assert il1.get("natl") and "not on file yet" in il1["note"]
    # a call for the candidate the page shows behind: marked, with a note (the page's count is from before the call)
    page2 = _state_page([["Dillan Vancil", "R", 150500, 0, "led"], ["Eric Sorensen", "D", 150000, 1, ""]], 300500)
    out2 = merge_national([page2] + nat, Counter())
    p2 = next(c for c in out2 if c.get("dist") == "17")
    assert [x[4] for x in p2["o"]] == ["", "won"] and "Called for Eric Sorensen" in p2["note"]
    # repair keeps a national race (a winner, no count): it is not "a contest nobody voted in"
    assert repair_contest(dict(il1, o=[list(x) for x in il1["o"]]), Counter()) is not None


def _t(name, st, cl, s, e, p, how="", pa=None, c=""):
    t = {"id": name, "n": name, "ch": "S", "st": st, "d": None, "cl": cl, "s": s, "e": e, "p": p, "c": c, "how": how}
    if pa:
        t["pa"] = pa
    return t


VPS = [{"s": "1993-01-20", "e": "2001-01-20", "p": "Democrat"}, {"s": "2025-01-20", "e": "2029-01-20", "p": "Republican"}]


def test_senate_before_and_after_an_election():
    assert regular_class(1994) == 1 and regular_class(2026) == 2 and regular_class(2028) == 3 and regular_class(1914) == 3
    assert side("Independent", "Democrat") == "D" and side("Conservative") == "R" and side("Independent") == "O"
    shelby = _t("Shelby", "AL", 3, "1993-01-05", "1999-01-03", "Republican",
                pa=[{"s": "1993-01-05", "e": "1994-11-09", "p": "Democrat", "c": ""}, {"s": "1994-11-09", "e": "1999-01-03", "p": "Republican", "c": ""}])
    assert party_on(shelby, "1994-11-07")[0] == "Democrat" and party_on(shelby, "1995-01-05")[0] == "Republican"
    terms = [
        shelby,                                                                          # not up in 1994; switched the day after
        _t("Sasser", "TN", 1, "1989-01-03", "1995-01-03", "Democrat"),                  # up, lost
        _t("Frist", "TN", 1, "1995-01-04", "2001-01-03", "Republican"),                 # won
        _t("Kennedy", "MA", 1, "1989-01-03", "1995-01-03", "Democrat"),                 # up, won again
        _t("Kennedy", "MA", 1, "1995-01-04", "2001-01-03", "Democrat"),
        _t("Kerry", "MA", 2, "1991-01-03", "1997-01-03", "Democrat"),                   # not up
    ]
    s = senate_election(terms, 1994, VPS)
    assert (s["before"]["D"], s["before"]["R"]) == (4, 0)
    assert (s["after"]["D"], s["after"]["R"]) == (3, 1)          # Shelby counts as elected: a Democrat on election day
    assert s["chg"] == {"D": -1, "R": 1, "O": 0}


def test_the_newest_election_from_its_races():
    terms = [
        _t("Ossoff", "GA", 2, "2021-01-20", "2027-01-03", "Democrat"),
        _t("Warnock", "GA", 3, "2023-01-03", "2029-01-03", "Democrat"),
        _t("Husted", "OH", 3, "2025-01-21", "2026-11-03", "Republican", how="appointment"),   # ends on election day
        _t("Moreno", "OH", 1, "2025-01-03", "2031-01-03", "Republican"),
        _t("Sullivan", "AK", 2, "2015-01-06", "2027-01-03", "Republican"),
        _t("Murkowski", "AK", 3, "2023-01-03", "2029-01-03", "Republican"),
    ] + [_t(f"R{i}", "TX", 1, "2025-01-03", "2031-01-03", "Republican") for i in range(19)]
    races = [
        {"scope": "us", "y": 2026, "office": "U.S. Senate", "stage": "general", "st": "GA", "o": [["Jon Ossoff", "D", 2, 1, "won"]]},
        {"scope": "us", "y": 2026, "office": "U.S. Senate", "stage": "special", "sp": 1, "st": "OH", "o": [["Sherrod Brown", "D", None, 0, "won"]]},
        {"scope": "us", "y": 2026, "office": "U.S. Senate", "stage": "general", "st": "AK", "o": [["Mary Peltola", "D", None, 0, ""]]},
    ] * 9                                             # (the count needs 25 races on file to be read: the same three, many times)
    cur = current_senate(races, terms, 2026, VPS)
    assert cur["before"]["R"] == 23 and cur["before"]["D"] == 2        # Husted counted on election day
    a = cur["after"]
    assert a["undecided"] >= 1 and any(u["st"] == "AK" for u in a["undecided_races"])


def test_anchors_guard_the_file():
    good = {"years": {"1994": {"house": {"D": 204, "R": 230}, "senate": {"after": {"D": 48, "R": 52}}, "flipped": ["house", "senate"]}}}
    assert anchor_faults(good) == []
    bad = {"years": {"1994": {"house": {"D": 199, "R": 235}, "senate": {"after": {"D": 48, "R": 52}}, "flipped": ["house", "senate"]}}}
    assert anchor_faults(bad) and "1994 House" in anchor_faults(bad)[0]


def test_build_congress_from_files(tmp_path):
    import json
    (tmp_path / "people").mkdir()
    (tmp_path / "congress").mkdir()
    (tmp_path / "congress" / "house_parties.json").write_text(json.dumps({"rows": house_party_rows(PARTY_DIV), "src": {"t": "t", "u": "u"}}))
    terms = [_t("A", "MA", 1, "1989-01-03", "1995-01-03", "Democrat"), _t("A", "MA", 1, "1995-01-04", "2001-01-03", "Democrat")]
    (tmp_path / "people" / "terms-historical.json").write_text(json.dumps(terms))
    (tmp_path / "people" / "executive.json").write_text(json.dumps({"vp": VPS}))
    out = build_congress(tmp_path, [], log=lambda *_: None)
    e = out["years"]["1994"]
    assert e["house"]["chg"] == {"D": -54, "R": 54, "O": 0} and e["house"]["maj_before"] == "D" and e["house"]["maj"] == "R"
    assert "house" in e["flipped"] and e["gain"]["party"] == "R" and e["gain"]["house"] == 54
