"""The county election offices' Election Summary Report (the layout every Georgia county's tabulator prints) is read
contest by contest, also inside a board's meeting packet: Dunwoody's 2023 mayor and at-large seats were missing from
Electo because this layout was not read at all. Numbers from DeKalb County's Nov. 7, 2023 report."""
import re

from gacrawl.elections import _derive, looks_like_results, parse_results

PACKET = """DeKalb County Board of Registration and Elections
Meeting of November 14, 2023
Election Summary Report
General Election
November 07, 2023
Unofficial and Incomplete
Mayor - Dunwoody (Vote for 1)
Precincts Reported: 13 of 13 (100.00%)
Times Cast 4,699 3,714 112 12 8,537 / 30,920 27.61%
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Lynn Deutsch (I) 3,994 3,250 86 11 7,341 100.00%
Total Votes 3,994 3,250 86 11 7,341
Dunwoody City Council, District 2 Post 5 At Large (Vote for 1)
Marianella Lopez 1,369 977 27 5 2,378 30.37%
Joe Seconder (I) 2,900 2,470 75 7 5,452 69.63%
Write-in 3 2 0 0 5 0.06%
Total Votes 4,269 3,447 102 12 7,830
Pine Lake City Council (Vote for 2)
Ann Able 300 200 5 1 506 40.00%
Bob Baker 280 190 4 1 475 37.55%
Cy Cole 150 130 3 1 284 22.45%
Total Votes 730 520 12 3 1,265
Dunwoody General Obligation Bond Referendum (Vote for 1)
Yes 1,980 1,642 40 4 3,666 43.18%
No 2,688 2,058 70 8 4,824 56.82%
Total Votes 4,668 3,700 110 12 8,490
"""


def test_summary_report_contests():
    assert looks_like_results("BRE final packet", "https://example.test/packet.pdf", PACKET)
    c = {x["office"]: x for x in parse_results(PACKET)}
    m = c["Mayor - Dunwoody"]["candidates"]
    assert [(x["name"], x["votes"], x["incumbent"], x["outcome"]) for x in m] == [("Lynn Deutsch", 7341, True, "unopposed")]
    p5 = {x["name"]: x for x in c["Dunwoody City Council, District 2 Post 5 At Large"]["candidates"]}
    assert set(p5) == {"Marianella Lopez", "Joe Seconder"} and p5["Joe Seconder"]["outcome"] == "won" and p5["Joe Seconder"]["votes"] == 5452
    two = {x["name"]: x["outcome"] for x in c["Pine Lake City Council"]["candidates"]}
    assert two == {"Ann Able": "won", "Bob Baker": "won", "Cy Cole": ""}           # two seats: the top two


def test_summary_report_takes_the_elections_date_not_the_meetings():
    recs = list(_derive({"source_id": "co-dekalb:elections", "title": "BRE final packet", "url": "https://example.test/packet.pdf",
                         "text": PACKET, "jurisdiction": "co-dekalb"}, {"jurisdictions": []}))
    res = [r for r in recs if r.kind == "election_result"]
    assert len(res) == 3 and {r.meta["date"] for r in res} == {"2023-11-07"}
    q = [r for r in recs if r.kind == "ballot_question"]
    assert len(q) == 1 and q[0].meta["result"] == "failed" and q[0].meta["no"] == 4824


def test_summary_report_kept_as_one_long_line():
    """The store keeps a PDF's text without its line breaks; the report is read all the same, page stamps aside."""
    flat = " ".join(PACKET.splitlines()).replace("Dunwoody City Council, District 2", "Page 3 of 9 11/14/2023 7:44:25 AM Dunwoody City Council, District 2")
    c = {x["office"]: x for x in parse_results(flat)}
    assert set(c) == {"Mayor - Dunwoody", "Dunwoody City Council, District 2 Post 5 At Large", "Pine Lake City Council",
                      "Dunwoody General Obligation Bond Referendum"}
    assert {x["name"]: x["outcome"] for x in c["Dunwoody City Council, District 2 Post 5 At Large"]["candidates"]} == \
        {"Marianella Lopez": "", "Joe Seconder": "won"}


# Gordon County's Nov. 3, 2026 reports, as the PDF text comes out: a name that wraps onto three lines, a party left on
# its own line, the write-ins block after "Total Votes", and (unofficial report) rows with no share
GORDON = """Page: 1 of 9 11/9/2026 9:01:05 AM
Election Summary Report
General Election
Gordon
November 03, 2026
Summary for: All Contests, All Districts, All Tabulators, All Counting Groups
Official and Complete
Elector Group Counting Group Ballots Voters Registered Voters Turnout
Total Election Day 9,167 9,167 22.28%
Advance Voting 9,385 9,385 22.81%
Absentee by Mail 487 487 1.18%
Provisional 14 14 0.03%
Total 19,053 19,053 41,140 46.31%
Precincts Reported: 11 of 11 (100.00%)
Registered Voters: 19,053 of 41,140 (46.31%)
Ballots Cast: 19,053
Commissioner of Labor (Vote for 1)
Precincts Reported: 11 of 11 (100.00%)
Election Day Advance Voti Absentee by Provisional Total
Times Cast 9,167 9,385 482 14 19,048 / 41,140 46.30%
Candidate Party Election Day Advance Absentee by Provisional Total
Voting Mail
Bárbara Rivera Holmes (I)
7,250 6,512 290 9 14,061 74.99%
(Rep)
Nikki Porcher (Dem) 1,780 2,728 176 5 4,689 25.01%
Total Votes 9,030 9,240 466 14 18,750
Election Day Advance Absentee by Provisional Total
Voting Mail
Unresolved Write-In 0 0 0 0 0
Page: 4 of 9 11/9/2026 9:01:05 AM
Governor (Vote for 1)
Precincts Reported: 11 of 11 (100.00%)
Election Day Advance Voti Absentee by Provisional Total
Times Cast 9,167 9,385 482 14 19,048 / 41,140 46.30%
Candidate Party Election Day Advance Absentee by Provisional Total
Voting Mail
Rick Jackson (Rep) 7,415 6,621 306 10 14,352 75.77%
Keisha Lance Bottoms (Dem) 1,704 2,705 171 4 4,584 24.20%
Total Votes 9,123 9,328 477 14 18,942
Election Day Advance Absentee by Provisional Total
Voting Mail
Leland J. Olinger, III WRITE-IN 2 0 0 0 2 0.01%
Jacqueline Angela DaCosta WRITE-IN 1 2 0 0 3 0.02%
Milton J. Lofton WRITE-IN 0 0 0 0 1 0.01%
Unresolved Write-In 0 0 0 0 0
"""


def _read(text):
    from gacrawl.elections import parse_summary_report
    return {c["office"]: [(x["name"], x["party"], x["votes"]) for x in c["candidates"]] for c in parse_summary_report(text)}


def test_gordon_report_wrapped_names_and_write_ins():
    c = _read(GORDON)
    assert list(c) == ["Commissioner of Labor", "Governor"]          # no turnout rows, no heading shifted by one
    assert c["Commissioner of Labor"] == [("Bárbara Rivera Holmes", "R", 14061), ("Nikki Porcher", "D", 4689)]
    assert c["Governor"][:2] == [("Rick Jackson", "R", 14352), ("Keisha Lance Bottoms", "D", 4584)]
    assert sum(v for *_, v in c["Governor"]) == 18942                 # the named write-ins count in the total


def test_gordon_report_kept_as_one_long_line_and_without_shares():
    flat = re.sub(r"\s+", " ", GORDON)
    assert _read(flat) == _read(GORDON)
    unofficial = re.sub(r" \d{1,3}\.\d+%(?=\n)", "", GORDON)         # the unofficial report prints no shares
    assert _read(unofficial) == _read(GORDON)
    assert _read(re.sub(r"\s+", " ", unofficial)) == _read(GORDON)
