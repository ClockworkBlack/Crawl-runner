"""donors.json: a gift's date when its own filing rules the filed one out (2022 typed as 2028, 2021 as 2001), and the
note the page shows while no reader loads what campaigns spent."""
import datetime as dt
import json
from pathlib import Path

from gacrawl import money
from gacrawl.donors import cycle_of, export_donors
from gacrawl.ethics import GA_SOURCE
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.datetime.now(dt.timezone.utc).date().isoformat()
CANDS = [{"id": "a", "name": "Ann Able", "electo_key": "ANN ABLE", "party": "D", "office": "U.S. House GA-6", "levels": ["federal"],
          "counties": [], "jurisdiction": ""},
         {"id": "b", "name": "Bob Baker", "electo_key": "BOB BAKER", "party": "R", "office": "State House 12", "levels": ["legislative"],
          "counties": [], "jurisdiction": ""}]


def fec(s, key, name, amount, date, image, report_type="12P", **extra):
    m = {"type": "contribution", "name": name, "amount": amount, "date": date, "recipient": "Ann Able", "recipient_kind": "candidate",
         "candidate_id": "a", "committee": "C00000001", "filing_source": "FEC", "fec_v": 2, "transaction_id": key,
         "image_number": image, "report_type": report_type, "state": "NY", "city": "White Plains", "donor_entity": name, **extra}
    s.add(Record("cand:a", "contribution", f"https://docquery.fec.gov/{image}", title=name, text="", published=date, channel="api",
                 evidence="primary", key=f"fec2|C00000001|{key}", meta=m))


def ga(s, key, name, amount, date, report_name="", **extra):
    m = {"type": "contribution", "name": name, "amount": amount, "date": date, "recipient": "Bob Baker", "recipient_kind": "candidate",
         "candidate_id": "b", "filing_source": GA_SOURCE, "ga_system": "efile", "ga_filer": "g1", "filer_name": "Baker, Bob",
         "report_name": report_name, "state": "GA", "city": "Macon", "donor_entity": name, "line": key, **extra}
    s.add(Record("cand:b", "contribution", "https://recordsearch.ethics.ga.gov/public/cf/contributions", title=name, text="",
                 published=date, channel="api", evidence="primary", key=f"ga2|efile|{key}", meta=m))


def test_a_date_its_filing_rules_out_makes_no_year_to_come(tmp_path):
    s = Store(tmp_path / "data")
    # Nov 2026: a May 2022 pre-primary report (image 20220512...) listed gifts dated 2028-04-01 and 2027-04-01
    fec(s, "T1", "FRIEDMAN, AVI", 5800, "2028-04-01", "202205129502825419")
    fec(s, "T2", "LANE, CURTIS", 1000, "2027-04-01", "202205129502825297")
    fec(s, "T3", "DOE, JANE", 500, "2022-04-01", "202205129502825300")                     # as filed: it stands
    # a year-end report filed in January 2023 covers through December 31, 2022
    fec(s, "T4", "ROE, RICK", 250, "2029-02-01", "202301319500000001", report_type="YE")
    # Georgia: "2001" typed for 2021 in a filing of the Commission's current systems (reports filed 2022 on)
    ga(s, "1", "Williams, Jim", 100, "2001-01-10")
    ga(s, "2", "Jacobs, Jer", 50, "2001-03-16", report_name="2022 Jan 31 CCDR")
    ga(s, "3", "Hubbard, Pete", 480.20, "2019-09-01")                                         # within four years: stands
    ga(s, "4", "Moss, Ann", 300, "2022-01-20", report_name="2022 Jan 31 CCDR")
    ga(s, "5", "Gale, Lee", 75, "2099-05-01")                                                 # after today, no dated filing
    s.flush()
    export_donors(s, ROOT, CANDS, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    R = {r["name"]: r for r in d["recipients"]}
    A, B = R["Ann Able"], R["Bob Baker"]
    # every dollar still counts; only the dates move
    assert A["raised"] == 7550 and B["raised"] == 1005.2
    # no cycle or year that has not happened yet, on any recipient, any gift or the file
    assert all(y <= TODAY[:4] for y in d["years"]) and all(c <= cycle_of(TODAY) for c in d["cycles"])
    assert "2028" not in d["cycles"] and "2100" not in d["cycles"] and not {"2027", "2028", "2029", "2099"} & set(d["years"])
    assert not {"2001", "2002"} & (set(d["years"]) | set(d["cycles"]))
    # the FEC lines: under the day the filing was received (a pre-primary report), or the end of the year-end period
    assert A["yr"]["2022"]["raised"] == 7550 and set(A["yr"]) == {"2022"} and A["last"] == "2022-12-31"
    gifts = {g["name"]: g for g in d["gifts"]}
    assert gifts["FRIEDMAN, AVI"]["last"] == "2022-05-12" and gifts["FRIEDMAN, AVI"]["redated"] == 1
    assert gifts["ROE, RICK"]["first"] == "2022-12-31" and gifts["ROE, RICK"]["cy"] == {"2022": [250, 1]}
    assert "redated" not in gifts["DOE, JANE"] and gifts["DOE, JANE"]["last"] == "2022-04-01"
    # Georgia: the report's own date when its name gives one, else no date at all (counted in raised, in no year)
    assert gifts["Jacobs, Jer"]["first"] == "2022-01-31" and gifts["Jacobs, Jer"]["yr"] == {"2022": [50, 1]}
    assert gifts["Williams, Jim"]["first"] == "" and "yr" not in gifts["Williams, Jim"] and gifts["Williams, Jim"]["redated"] == 1
    assert gifts["Gale, Lee"]["first"] == "" and "cy" not in gifts["Gale, Lee"]
    assert gifts["Hubbard, Pete"]["yr"] == {"2019": [480.2, 1]} and "redated" not in gifts["Hubbard, Pete"]
    assert B["first"] == "2019-09-01" and B["last"] == "2022-01-31" and sum(v["raised"] for v in B["yr"].values()) == 830.2
    assert d["redated"] == {"after_filing": 3, "after_today": 1, "before_first_filing": 2}
    assert A["redated"] == 3 and B["redated"] == 3
    # and the donor's own profile: years from the same dates, each set-aside line counted on its recipient
    from gacrawl.donors import shard_of
    P = json.loads((tmp_path / "donor_profiles" / "dp" / f"{shard_of('FRIEDMAN, AVI')}.json").read_text())["FRIEDMAN, AVI"]
    assert P["years"] == {"2022": 5800} and P["last"] == "2022-05-12" and P["to"][0]["redated"] == 1


def test_the_date_rules():
    m = {"filing_source": "FEC", "image_number": "202205129502825419", "report_type": "12P"}
    assert money.filing_of(m, "2026-11-19") == ("2022-05-12", "2022-05-12", "2022-05-12")
    assert money.dated(m, "2028-04-01", "2017-02-01", "2026-11-19") == ("2022-05-12", "after_filing")
    assert money.dated(m, "2022-04-01", "2017-02-01", "2026-11-19") == ("2022-04-01", "")
    # an FEC line is never held to its committee's first filing: an amendment's image day says nothing about that
    q2 = {"filing_source": "FEC", "image_number": "202012239393681649", "report_type": "Q2", "amendment_indicator": "A"}
    assert money.dated(q2, "2016-05-04", "2020-12-23", "2026-11-19") == ("2016-05-04", "")
    assert money.dated(q2, "2021-01-04", "2020-12-23", "2026-11-19") == ("2020-06-30", "after_filing")
    first: dict = {}
    money.note_filed({**q2, "committee": "C00000001"}, first, "2026-11-19")
    money.note_filed({"filing_source": GA_SOURCE, "ga_filer": "g1", "report_name": "2022 Jan 31 CCDR"}, first, "2026-11-19")
    assert first == {"g1": "2022-01-31"}
    # report periods by type: the latest end of that period on or before the day the report was filed
    assert money.filing_of({**m, "report_type": "Q1", "image_number": "202204159000000001"}, "2026-11-19")[1] == "2022-03-31"
    assert money.filing_of({**m, "report_type": "YE", "image_number": "202301319000000001"}, "2026-11-19")[1] == "2022-12-31"
    assert money.filing_of({**m, "report_type": "M3", "image_number": "202403209000000001"}, "2026-11-19")[1] == "2024-02-29"
    # an image number that is not a date (an old 11-digit one, or nonsense) says nothing
    assert money.filing_of({**m, "image_number": "29934001234"}, "2026-11-19") == ("", "", "")
    assert money.filing_of({**m, "image_number": "209913319000000001"}, "2026-11-19") == ("", "", "")
    # Georgia report names and a posted report's period; a name with no year gives no date
    assert money.named_date("2026 Oct 20 CCDR") == "2026-10-20" and money.named_date("2022 Sept 30 CCDR") == "2022-09-30"
    assert money.named_date("January 1, 2022 through June 30, 2022") == "2022-06-30"
    assert money.named_date("June 30th - Election Year") == "" and money.named_date("Two Business Day Report") == ""
    assert money.filing_of({"report": {"period": "01/01/2022 - 06/30/2022"}}, "2026-11-19")[:2] == ("2022-06-30", "2022-06-30")
    # a late gift or two past a Georgia report's date is the filer's habit, not a typed year
    g = {"report_name": "2022 Jun 30 CCDR"}
    assert money.dated(g, "2022-07-04", "", "2026-11-19") == ("2022-07-04", "")
    assert money.dated(g, "2028-06-01", "", "2026-11-19") == ("2022-06-30", "after_filing")
    # long before the committee's first filing: a typed year; within four years: as filed
    assert money.dated({}, "2001-01-10", "2022-01-01", "2026-11-19") == ("", "before_first_filing")
    assert money.dated({}, "2018-02-01", "2022-01-01", "2026-11-19") == ("2018-02-01", "")
    assert money.dated({}, "2026-12-01", "", "2026-11-19") == ("", "after_today")
    assert money.dated({}, "", "2022-01-01", "2026-11-19") == ("", "")


def test_lines_carry_the_checked_date(tmp_path):
    s = Store(tmp_path / "db")
    fec(s, "T1", "FRIEDMAN, AVI", 5800, "2028-04-01", "202205129502825419")
    fec(s, "T3", "DOE, JANE", 500, "2017-03-01", "201704159000000001", report_type="Q1")
    # Georgia: the committee's first report is named for March 15, 2017, so a gift dated 2002 in it is a typed year
    ga(s, "1", "Old, Typo", 20, "2002-03-01", report_name="2017 Mar 15 CCDR", ga_system="")
    ga(s, "2", "Roe, Rick", 40, "2017-02-01", report_name="2017 Mar 15 CCDR", ga_system="")
    s.flush()
    L = {x["meta"]["name"]: x for x in money.lines(s, today="2026-11-19")}
    assert (L["FRIEDMAN, AVI"]["date"], L["FRIEDMAN, AVI"]["redated"]) == ("2022-05-12", "after_filing")
    assert (L["Old, Typo"]["date"], L["Old, Typo"]["redated"]) == ("2017-03-15", "before_first_filing")
    assert (L["Roe, Rick"]["date"], L["Roe, Rick"]["redated"]) == ("2017-02-01", "")
    assert (L["DOE, JANE"]["date"], L["DOE, JANE"]["redated"]) == ("2017-03-01", "")
    assert L["FRIEDMAN, AVI"]["meta"]["date"] == "2028-04-01"           # the line as filed is kept as filed


# A committee that re-filed its old reports: the FEC returns each line from the latest amendment, with the amendment's
# image number, so every line of a Georgia congressman's 2016 reports points to a filing received December 23, 2020
# and none to an earlier one. His 2016 gifts are his 2016 gifts: the image day bounds them from above, and is no sign
# of when the committee first filed.
REFILED = (("Q2", "2016-05-04", "202012239393681649"), ("12G", "2016-10-19", "202012239393681701"),
           ("YE", "2016-11-08", "202012239393681802"))


def test_refiled_fec_reports_keep_their_years(tmp_path):
    s = Store(tmp_path / "db")
    for i, (rt, day, img) in enumerate(REFILED):
        fec(s, f"R{i}", f"GIVER{i}, PAT", 1000, day, img, report_type=rt, amendment_indicator="A")
    fec(s, "R9", "LATER, LEE", 500, "2020-11-30", "202012239393681900", report_type="YE", amendment_indicator="A")
    s.flush()
    L = {x["meta"]["name"]: x for x in money.lines(s, today="2026-11-19")}
    for i, (rt, day, img) in enumerate(REFILED):
        assert (L[f"GIVER{i}, PAT"]["date"], L[f"GIVER{i}, PAT"]["redated"]) == (day, ""), rt
    assert (L["LATER, LEE"]["date"], L["LATER, LEE"]["redated"]) == ("2020-11-30", "")
    # and in donors.json: his 2016 year and cycle stand, and 2019 and 2020 get nothing that is not theirs
    export_donors(s, ROOT, CANDS, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    A = {r["name"]: r for r in d["recipients"]}["Ann Able"]
    assert {k: v["raised"] for k, v in A["yr"].items()} == {"2016": 3000, "2020": 500}
    assert "2016" in d["cycles"] and "2016" in d["years"] and "2019" not in d["years"]
    assert "redated" not in A and d["redated"] == {}


def test_spending_held_while_no_reader_loads_it(tmp_path):
    """No reader loads disbursements (the FEC is read for schedule A, the Commission for contribution lines): spend
    was empty and every spent 0, with nothing to say so. The file now says what is missing, and stops once it is not."""
    s = Store(tmp_path / "data")
    fec(s, "T1", "DOE, JANE", 500, "2022-04-01", "202205129502825300")
    ga(s, "1", "Moss, Ann", 300, "2022-01-20", report_name="2022 Jan 31 CCDR")
    s.flush()
    export_donors(s, ROOT, CANDS, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    assert d["spend"] == [] and all(r["spent"] == 0 and r["nx"] == 0 for r in d["recipients"])
    assert d["spend_hold"].startswith("What campaigns spent is not available yet") and "no amount spent" in d["spend_hold"]
    assert d["hold"] == ""                         # the federal money note is about receipts and stays apart
    assert "disbursements for federal candidates" not in d["note"] and "spend_hold" in d["note"]
    # expenditure lines from every source: nothing held
    for src, key, day in (("FEC", "x1", "2022-03-01"), (GA_SOURCE, "x2", "2022-03-02")):
        s.add(Record("cand:a", "contribution", f"https://filings.example/{key}", title="Print Shop LLC", text="", published=day,
                     channel="api", evidence="primary", key=key,
                     meta={"type": "expenditure", "name": "Print Shop LLC", "amount": 1200, "date": day, "purpose": "mailers",
                           "recipient": "Ann Able", "candidate_id": "a", "filing_source": src}))
    s.flush()
    export_donors(s, ROOT, CANDS, tmp_path / "donors.json", log=lambda *a: None)
    d = json.loads((tmp_path / "donors.json").read_text())
    assert d["spend_hold"] == "" and d["spend"][0]["payee"] == "Print Shop LLC" and d["spend"][0]["n"] == 2
    assert {r["name"]: r["spent"] for r in d["recipients"]}["Ann Able"] == 2400
