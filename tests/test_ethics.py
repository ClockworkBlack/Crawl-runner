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


def test_store_ignores_training_logs(tmp_path):
    (tmp_path / "train").mkdir()
    (tmp_path / "train" / "discover_log.jsonl").write_text('{"task": "x", "input": []}\n')
    s = Store(tmp_path)
    assert list(s.iter_query("1=1")) == []


# ---------------------------------------------------------------- the full reads, the side job and the hand-off
import datetime as dt  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402


class _Resp:
    def __init__(self, data, status=200):
        self.status, self.text = status, json.dumps(data)
        self.content = self.text.encode()


class FakeApi:
    """The Commission's API for both systems: election years, and pages of lines by transaction date."""

    def __init__(self, lines):          # {system: {year: [line, ...] oldest first}}
        self.lines, self.pages = lines, []

    def post(self, url, json_body=None, headers=None, **kw):
        system = "efile" if "recordsearch" in url else "peachfile"
        path = url.split("/api/", 1)[1]
        if path.startswith("PublicLookup/"):
            return _Resp({"data": [{"code": y} for y in self.lines[system]]})
        b = json_body
        rows = list(self.lines[system].get(b["electionYear"], []))
        if b["sortType"] == "desc":
            rows.reverse()
        size, page = b["pageSize"], b["pageNumber"]
        self.pages.append((system, b["electionYear"], page, b["sortType"]))
        return _Resp({"data": {"items": rows[(page - 1) * size: page * size], "totalItems": len(rows)}})


def _lines(system, year, n, start=1):
    return [t(transactionId=int(f"{year}{i:05d}") if system == "efile" else int(f"9{year}{i:05d}"),
              sourceName=f"Donor{i}, Pat", transactionDate=f"0{1 + i % 9}/15/{year}") for i in range(start, start + n)]


def _api_lines():
    return {"efile": {"2024": _lines("efile", "2024", 250)}, "peachfile": {"2026": _lines("peachfile", "2026", 120)}}


def _run(api, s, tmp_path, mode):
    return ethics.run(api, s, tmp_path, CANDS, time.monotonic() + 60, log=lambda *_: None, mode=mode)


def test_record_is_slim_and_unquotes_apostrophes():
    r = ethics.record_of("peachfile", t(sourceName="It''s Time Georgia", payeeEmployer="", payeeOccupation=""), ethics.candidate_index(CANDS))
    assert r.meta["name"] == "It's Time Georgia" and r.text == ""
    assert not {"report", "purpose", "needs_review", "employer", "occupation", "election"} & set(r.meta)
    assert r.meta["line"] == "1" and r.title          # the line id stays (money dedup); the title is what search reads


def test_full_reads_resume_complete_and_are_read_again_monthly(tmp_path):
    s, api = Store(tmp_path / "x.db"), FakeApi(_api_lines())
    st = _run(api, s, tmp_path, "full")
    assert st["pages"] == 3 + 2 and st["added"] == 370
    prog = ethics.progress(s)
    assert prog["efile:2024"]["complete"] and not prog["efile:2024"]["due"] and prog["peachfile:2026"]["complete"]
    api.pages.clear()
    assert _run(api, s, tmp_path, "full")["pages"] == 0      # nothing due: the side job's run ends at once
    # the crawl gave a line its donor ids; a month later the year is read in full again, and the line keeps them
    k = "ga2|efile|202400001"
    m = s.get_meta(k)
    rec = ethics.record_of("efile", api.lines["efile"]["2024"][0], ethics.candidate_index(CANDS))
    rec.meta.update({"donor_entity": "p1", "recipient_entity": "c1"})      # as finance.resolve_all writes them
    assert rec.key == k and s.add(rec)
    old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
    s.kv_set("ga_ethics:efile:2024", json.dumps({"complete": True, "total": 250, "size": 100, "at": old}))
    assert ethics.progress(s)["efile:2024"]["due"]
    st = _run(api, s, tmp_path, "full")
    assert st["pages"] == 3 and st["added"] == 0 and set(st["years"]) == {"efile:2024"}      # only the year due
    assert s.get_meta(k)["donor_entity"] == "p1" and m["name"] == s.get_meta(k)["name"]
    assert not ethics.progress(s)["efile:2024"]["due"]


def test_refresh_reads_the_newest_lines_and_leaves_the_full_reads_alone(tmp_path):
    s, lines = Store(tmp_path / "x.db"), _api_lines()
    api = FakeApi(lines)
    s.kv_set("ga_ethics:efile:2024", json.dumps({"page": 2, "total": 250, "size": 100, "at": "2026-11-08T00:00:00+00:00"}))
    st = _run(api, s, tmp_path, "refresh")
    assert s.kv_get("ga_ethics:efile:2024") == json.dumps({"page": 2, "total": 250, "size": 100, "at": "2026-11-08T00:00:00+00:00"})
    assert ("efile", "2024", 1, "desc") in api.pages and st["added"] > 0
    lines["peachfile"]["2026"] += _lines("peachfile", "2026", 3, start=500)
    api.pages.clear()
    st = _run(api, s, tmp_path, "refresh")
    assert st["added"] == 3 and api.pages.count(("peachfile", "2026", 1, "desc")) == 1


def test_auto_mode_follows_the_side_jobs_hand_offs(tmp_path):
    s = Store(tmp_path / "x.db")
    assert ethics.resolve_mode(s, "auto") == "all" and ethics.resolve_mode(s, "full") == "full"
    p = tmp_path / "ga-kv.json"
    p.write_text(json.dumps({}))
    ethics.apply_kv(s, p)
    assert ethics.resolve_mode(s, "auto") == "refresh"
    s.kv_set("ga_money_applied", (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat(timespec="seconds"))
    assert ethics.resolve_mode(s, "auto") == "all"          # the side job stopped handing over: the crawl reads in full


def test_apply_kv_only_moves_forward(tmp_path):
    s, p = Store(tmp_path / "x.db"), tmp_path / "ga-kv.json"
    old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
    s.kv_set("ga_ethics:efile:2022", json.dumps({"page": 50, "at": "2026-11-08T00:00:00+00:00"}))
    s.kv_set("ga_ethics:efile:2023", json.dumps({"page": 50, "at": "2026-11-08T00:00:00+00:00"}))
    s.kv_set("ga_ethics:efile:2024", json.dumps({"complete": True, "at": "2026-11-08"}))
    s.kv_set("ga_ethics:efile:2025", json.dumps({"complete": True, "at": old}))
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    p.write_text(json.dumps({
        "ga_ethics:efile:2022": json.dumps({"page": 80, "at": now}),             # further along: taken
        "ga_ethics:efile:2023": json.dumps({"page": 10, "at": now}),             # behind: not taken
        "ga_ethics:efile:2024": json.dumps({"page": 3, "at": now}),              # a completed read is not undone
        "ga_ethics:efile:2025": json.dumps({"page": 3, "at": now}),              # its monthly read in full again: taken
        "entities": "{}"}))                                                      # nothing but the reads' places
    assert ethics.apply_kv(s, p) == 2
    assert json.loads(s.kv_get("ga_ethics:efile:2022"))["page"] == 80 and json.loads(s.kv_get("ga_ethics:efile:2023"))["page"] == 50
    assert json.loads(s.kv_get("ga_ethics:efile:2024"))["complete"] and json.loads(s.kv_get("ga_ethics:efile:2025"))["page"] == 3
    assert s.kv_get("entities") is None


def test_keep_links_only_while_the_line_is_unchanged():
    old = {"name": "Doe, Jane", "city": "Macon", "donor_entity": "p1", "recipient_entity": "c1"}
    assert ethics.keep_links({"name": "Doe, Jane", "city": "Macon"}, old)["donor_entity"] == "p1"
    assert "donor_entity" not in ethics.keep_links({"name": "Doe, Jane", "city": "Atlanta"}, old)   # amended: given again
