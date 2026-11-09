import json

from gacrawl import money
from gacrawl.store import Record, Store


def rec(key, **meta):
    return Record("cand:x", "contribution", "https://docquery.fec.gov/x", key=key, meta={"type": "contribution", **meta})


def test_rules(tmp_path):
    s = Store(tmp_path / "db")
    base = {"filing_source": "FEC", "committee": "C1", "candidate_id": "x", "recipient": "X"}
    # an old-format FEC line (no FEC fields): not counted
    s.add(rec("a", name="AIPAC PAC", amount=27525.25, **base))
    # the FEC's own lines: a memo line, a gift, the same gift read again (an amendment), an earmark through a PAC
    s.add(rec("fec2|C1|T1", name="AIPAC PAC", amount=27525.25, memo_code="X", transaction_id="T1", fec_v=2, **base))
    s.add(rec("fec2|C1|T2", name="AIPAC PAC", amount=5000, transaction_id="T2", fec_v=2, contributor_committee_id="C00797670", **base))
    s.add(rec("fec2|C1|T2b", name="AIPAC PAC", amount=5000, transaction_id="T2", fec_v=2, **base))
    s.add(rec("fec2|C1|T3", name="JANE DOE", amount=1000, transaction_id="T3", fec_v=2,
              memo_text="EARMARKED THROUGH AIPAC PAC", **base))
    # a transfer from another of the candidate's own committees
    s.add(rec("fec2|C1|T4", name="X VICTORY FUND", amount=900, transaction_id="T4", fec_v=2, contributor_committee_id="C1", **base))
    s.flush()
    st = {}
    L = list(money.lines(s, st))
    assert [round(x["amount"]) for x in L] == [5000, 1000]
    assert L[0]["via"] == "" and L[1]["via"] == "AIPAC PAC"
    assert st == {"memo": 1, "repeat": 1, "legacy": 1, "transfer": 1, "lines": 2}


def test_fec_name_match(tmp_path):
    from gacrawl.finance import Finance

    class F:
        def __init__(self):
            self.calls = []

        def _fec(self, path, params=None):
            return {"results": [
                {"name": "MCBATH, LUCIA KAY MS.", "candidate_id": "H8GA06393", "office": "H", "district": "06"},
                {"name": "SCOTT, DAVID ALBERT", "candidate_id": "H2GA13012", "office": "H", "district": "13"},
                {"name": "SCOTT, JAMES AUSTIN", "candidate_id": "H0GA08099", "office": "H", "district": "08"},
                {"name": "OSSOFF, T. JONATHAN", "candidate_id": "H8GA06195", "office": "H", "district": "06"},
                {"name": "OSSOFF, T. JONATHAN", "candidate_id": "S8GA00180", "office": "S", "district": "00"}]}

    s = Store(tmp_path / "db")
    f = F(); f.store = s; f.log = print
    ids = lambda c: Finance.fec_candidate_ids.__get__(f)(c)
    f.NICK = Finance.NICK
    assert ids({"id": "lucy-mcbath", "name": "Lucy McBath", "office": "U.S. Representative, District 6"}) == ["H8GA06393"]
    assert ids({"id": "austin-scott", "name": "Austin Scott", "office": "U.S. Representative, District 8"}) == ["H0GA08099"]
    assert ids({"id": "david-scott", "name": "David Scott", "office": "U.S. Representative, District 13"}) == ["H2GA13012"]
    assert ids({"id": "jon-ossoff", "name": "Jon Ossoff", "office": "U.S. Senate"}) == ["H8GA06195", "S8GA00180"]


def test_blank_forms_are_not_reports():
    from gacrawl.finance import NOT_A_DONOR, looks_like_finance_report

    guide = "GEORGIA GOVERNMENT TRANSPARENCY AND CAMPAIGN FINANCE ACT 2018 EDITION ... Campaign Contribution Disclosure Report"
    assert not looks_like_finance_report("4 - Georgia Government Transparency", "https://x.gov/4%20-%20Guide.pdf", guide)
    assert not looks_like_finance_report("Affidavit of a candidate", "https://x.gov/10%20-%20Affidavit.pdf", "")
    assert looks_like_finance_report("Campaign Contribution Disclosure Report - Jane Doe", "https://x.gov/ccdr.pdf",
                                     "Contributor Employer Amount")
    assert NOT_A_DONOR.search("GEORGIA GOVERNMENT TRANSPARENCY AND CAMPAIGN FINANCE COMMISSION 200 Piedmont Avenue SE")
    assert not NOT_A_DONOR.search("SMITH, JOHN A")


def test_packet_money_lines_have_a_source_kind():
    from gacrawl.electo import build_packet
    rec = {"rid": 1, "url": "https://example.com/ga", "kind": "contribution", "published": "2026-10-01",
           "meta": {"type": "contribution", "name": "DOE, JANE", "amount": 500, "date": "2026-10-01", "recipient": "Ann Able",
                    "candidate_id": "a", "filing_source": "Georgia State Ethics Commission", "line": "1"}}
    pk = build_packet({"id": "a", "name": "Ann Able"}, {"contribution": [rec]}, [], set())
    assert pk["finance"][0]["amount"] == 500
    assert pk["sources"][pk["finance"][0]["src"][0]]["kind"] == "contribution"
