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


def test_two_alike_gifts_stay_two_in_the_graph(tmp_path):
    """Two real gifts with the same donor, recipient, date, amount and page (a filer's search page, as the Georgia
    Ethics Commission's lines have) are two lines: the graph keeps them apart by the line's identity."""
    from gacrawl.connections import build
    from gacrawl.entities import EntityIndex
    from gacrawl.store import Record, Store

    s = Store(tmp_path / "data")
    for i in (1, 2):
        s.add(Record("ga", "contribution", "https://peachfile.ethics.ga.gov/public/cf/contributions", key=f"ga2|peachfile|{i}",
                     meta={"type": "contribution", "name": "DOE, JANE", "amount": 100, "date": "2026-10-01", "recipient": "Ann Able",
                           "candidate_id": "a", "donor_entity": "p1", "recipient_entity": "p2", "line": str(i),
                           "filing_source": "Georgia State Ethics Commission"}))
    s.flush()
    g = build(s, EntityIndex(tmp_path / "entities.json", store=s))
    assert sum(e.get("amount") or 0 for e in g.inn.get("p2", ()) if e["type"] == "contributed") == 200


def test_profile_total_counts_unitemized_like_the_donors_tab(tmp_path):
    from gacrawl.connections import build, money_for
    from gacrawl.entities import EntityIndex
    from gacrawl.store import Record, Store

    s = Store(tmp_path / "data")
    ix = EntityIndex(tmp_path / "entities.json", store=s)
    ix.add_candidate({"id": "a", "name": "Ann Able"})
    eid = ix.by_candidate("a")
    base = {"type": "contribution", "date": "2026-10-01", "recipient": "Ann Able", "candidate_id": "a", "recipient_entity": eid,
            "filing_source": "Georgia State Ethics Commission"}
    s.add(Record("ga", "contribution", "https://peachfile.ethics.ga.gov/public/cf/contributions", key="ga2|p|1",
                 meta={**base, "name": "DOE, JANE", "amount": 300, "donor_entity": "p1", "line": "1"}))
    s.add(Record("ga", "contribution", "https://peachfile.ethics.ga.gov/public/cf/contributions", key="ga2|p|2",
                 meta={**base, "name": "", "amount": 5000, "unitemized": True, "line": "2"}))
    s.flush()
    m = money_for(build(s, ix), s, ix, "a")
    assert m["total_raised"] == 5300 and m["contributions"] == 1
    assert m["unattributed"] == [{"name": "Unitemized small gifts", "total": 5000}]


def test_fec_refunds_and_offsets_are_not_gifts():
    from gacrawl.money import clean
    base = {"filing_source": "FEC", "committee": "C1", "fec_v": 2, "recipient": "Jon Ossoff", "date": "2020-01-01"}
    rows = [{"rid": i, "meta": {**base, "transaction_id": str(i), "name": n, "amount": 100, "line_number": ln}}
            for i, (n, ln) in enumerate([("DOE, JANE", "11AI"), ("SOME PAC", "11C"), ("MOTHERSHIP STRATEGIES", "15"),
                                         ("BANK", "15"), ("OTHER COMMITTEE", "12"), ("VENDOR", "17"), ("LENDER", "13A")])]
    st = {}
    kept = [L["meta"]["name"] for L in clean(rows, {}, st)]
    assert kept == ["DOE, JANE", "SOME PAC", "OTHER COMMITTEE", "LENDER"] and st["not_a_gift"] == 3
