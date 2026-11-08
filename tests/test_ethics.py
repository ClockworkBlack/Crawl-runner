from gacrawl import ethics
from gacrawl.money import clean
from gacrawl.store import Store

CANDS = [{"id": "angelia-pressley", "name": "Angelia Pressley", "electo_key": "ANGELIA PRESSLEY", "levels": ["state"]},
         {"id": "jon-burns", "name": "Jon Burns", "levels": ["legislative"]},
         {"id": "jon-burns-2", "name": "Jon Burns", "levels": ["local"]}]


def t(**kw):
    base = {"transactionId": 1, "transactionAmount": 500.0, "filerName": "Pressley, Angelia", "filerTypeCode": "CAN",
            "candidateFirstName": "Angelia", "candidateLastName": "Pressley", "sourceName": "Doe, Jane",
            "transactionDate": "10/01/2026", "transactionSubTypeDesc": "Itemized Contribution", "reportName": "2026 Oct 20 CCDR",
            "filerReportId": 9979, "filerReportVersionId": 1, "filerRegistrationGuid": "g1", "transactionSourceStateCode": "GA"}
    base.update(kw)
    return base


def test_record_matches_candidate_and_dates():
    idx = ethics.candidate_index(CANDS)
    assert "jon burns" not in idx           # two Electo people share the name: not matched
    r = ethics.record_of("peachfile", t(), idx)
    assert r.key == "ga2|peachfile|1" and r.meta["candidate_id"] == "angelia-pressley"
    assert r.meta["date"] == "2026-10-01" and r.meta["filing_source"] == ethics.GA_SOURCE


def test_not_counted():
    idx = ethics.candidate_index(CANDS)
    assert ethics.record_of("efile", t(reportName="Two Business Day Report"), idx) is None
    assert ethics.record_of("efile", t(transactionSubTypeDesc="Interest Earned"), idx) is None
    assert ethics.record_of("efile", t(transactionSubTypeDesc="Returned Contribution"), idx) is None
    assert ethics.record_of("efile", t(transactionSubTypeDesc="In-Kind Contribution"), idx).meta["type"] == "in_kind"


def test_unitemized_is_a_lump_not_a_donor():
    idx = ethics.candidate_index(CANDS)
    r = ethics.record_of("efile", t(transactionSubTypeDesc="Unitemized Contribution", sourceName="Unitemized"), idx)
    L = list(clean([{"rid": 1, "url": r.url, "meta": r.meta}]))
    assert L and L[0]["platform"] == "Unitemized small gifts" and r.meta["name"] == ""


def test_amended_report_replaces_older(tmp_path):
    s = Store(tmp_path / "x.db")
    idx = ethics.candidate_index(CANDS)
    s.add(ethics.record_of("efile", t(transactionId=1), idx))
    s.add(ethics.record_of("efile", t(transactionId=2, filerReportVersionId=2), idx))
    s.add(ethics.record_of("efile", t(transactionId=3, reportName="2026 Jun 30 CCDR"), idx))
    assert ethics.drop_superseded(s) == 1
    keys = {r["key"] for r in s.iter_query("kind='contribution'")}
    assert keys == {"ga2|efile|2", "ga2|efile|3"}


def test_committee_and_nickname_matching():
    idx = ethics.candidate_index(CANDS + [{"id": "blake-tillery", "name": "Blake Tillery"}, {"id": "chris-carr", "name": "Chris Carr"}])
    r = ethics.record_of("efile", t(filerName="Friends of Blake Tillery", filerTypeCode="CC", filerTypeDesc="Candidate Committee",
                                   candidateFirstName="", candidateLastName=""), idx)
    assert r.meta["candidate_id"] == "blake-tillery"
    r = ethics.record_of("efile", t(filerName="Carr, Christopher M", candidateFirstName="Christopher", candidateLastName="Carr"), idx)
    assert r.meta["candidate_id"] == "chris-carr"
    r = ethics.record_of("efile", t(filerName="WBJ Leadership Committee, Inc.", filerTypeDesc="Leadership Committee",
                                   candidateFirstName="", candidateLastName=""), idx)
    assert "candidate_id" not in r.meta


def test_candidate_filer_with_full_legal_name():
    idx = ethics.candidate_index([{"id": "brad-raffensperger", "name": "Brad Raffensperger"}])
    r = ethics.record_of("efile", t(filerName="Raffensperger, Bradford Jay", filerTypeCode="X", filerTypeDesc="Candidate",
                                   candidateFirstName="", candidateLastName=""), idx)
    assert r.meta["candidate_id"] == "brad-raffensperger" and r.meta["line"] == "1"
