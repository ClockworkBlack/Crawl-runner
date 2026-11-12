"""The county election offices' Election Summary Report (the layout every Georgia county's tabulator prints) is read
contest by contest, also inside a board's meeting packet: Dunwoody's 2023 mayor and at-large seats were missing from
Electo because this layout was not read at all. Numbers from DeKalb County's Nov. 7, 2023 report."""
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
