"""The coverage report's pipeline checks: what each step that feeds donors, rumors, federal bills and Georgia money
has done so far (a number that stops moving is a step not working)."""
import json

from gacrawl import coverage
from gacrawl.store import Record, Store


def test_pipeline_checks(tmp_path):
    (tmp_path / "data" / "rumor_mill").mkdir(parents=True)
    (tmp_path / "data" / "rumor_mill" / "rumors.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"id": "r1", "candidate_id": "a", "url": "https://e.com/1", "status": "unverified", "meta": {"text": "x" * 300}, "ai": {"v": 1}},
        {"id": "r2", "candidate_id": "a", "url": "https://e.com/2", "status": "unverified", "meta": {"text_error": "Disallowed: robots"}},
        {"id": "r3", "candidate_id": "a", "url": "https://e.com/3", "status": "confirmed", "meta": {"text": "y" * 300}}]))
    s = Store(tmp_path / "data")
    s.add(Record("org", "organization", "u1", title="A PAC", key="org|o1", meta={"registries": ["fec"]}))
    s.add(Record("org", "organization", "u2", title="Acme LLC", key="org|o2", meta={"registries": []}))
    s.add(Record("ga-ethics", "contribution", "u3", title="t", key="ga2|efile|1", meta={"amount": 5.0}))
    s.kv_set("donor_about", json.dumps({"o1": {"t": "A PAC registered with the FEC."}, "o2": {"why": ["no source"]}}))
    s.kv_set("federal_bill_ai", json.dumps({"119|hr|1": {"t": "Two sentences."}, "119|hr|2": {"why": ["x"]}}))
    s.kv_set("ga_ethics:efile:all", json.dumps({"page": 300, "total": 2563329, "size": 100, "at": "2026-11-09T10:00:00+00:00"}))
    s.kv_set("legis_refusals", json.dumps([{"at": "2099-01-01T00:00", "refused": True}, {"at": "2099-01-01T06:00", "refused": False}]))
    s.flush()
    out = coverage.pipeline_checks(tmp_path, s)
    assert "looked up: **2**; found in a registry (FEC files, State Ethics Commission, IRS): **1**" in out
    assert "backgrounds the open model wrote: **1**" in out
    assert "2 unverified items: pages read for the model **1**; pages that could not be read **1**" in out
    assert "readings by the open model **1**" in out and "descriptions the open model wrote **1** (1 more" in out
    assert "**1** lines; full reads: efile:all page 300 of 25634" in out and "hand-off applied **never**" in out
    assert "refused the crawler in **1** of the last 7 days' 2 runs" in out


def _fec_line(s, i, committee, candidate):
    s.add(Record("fec", "contribution", f"https://fec.example/{committee}/{i}", title="t", key=f"fec|{committee}|{i}",
                 meta={"filing_source": "FEC", "committee": committee, "candidate_id": candidate, "amount": 10.0}))


def test_hand_checked_fec_ids_settle_the_three_the_name_lookup_missed(tmp_path):
    # Nov 17-19, 2026 the report listed barbara-seidman, philip-singleton and sarah-riggs-amico as federal candidates
    # with no FEC id (two FEC "people" for one person, twice; a two-word last name once): their ids are in
    # config/finance.yaml now, checked against the FEC's candidate search and Electo's record of their races
    from gacrawl.config import ROOT
    from gacrawl.registry import load_yaml

    hand = load_yaml(ROOT / "config" / "finance.yaml")["fec"]["candidate_ids"]
    assert hand["barbara-seidman"] == ["H8GA01071", "H0GA01052"]        # GA-1, 2018 and 2020
    assert hand["philip-singleton"] == ["H8GA03044", "H4GA03076"]       # GA-3, 2018 and 2024
    assert hand["sarah-riggs-amico"] == ["S0GA00401"]                   # Senate, 2020
    s = Store(tmp_path / "data")
    s.kv_set("fec_ids2", json.dumps({k: {"ids": [], "checked": "2026-11-17"} for k in
                                     ("barbara-seidman", "philip-singleton", "sarah-riggs-amico", "someone-unmatched")}))
    s.flush()
    out = coverage.money_checks(s, by=coverage.writer())
    assert "could not match: **1**: someone-unmatched (" in out
    s.close()


def test_report_names_the_job_and_database_that_wrote_it(tmp_path, monkeypatch):
    # the crawl's merge job and fec-reread both write data/coverage.md, from the crawl database and from fec-reread's
    # own copy: the FEC lines awaiting the re-read read 9,192 in one and 0 in the other, turn about, with nothing to say
    # which was which. Each report now names its writer, and a copy's shows the crawl database's count beside its own
    (tmp_path / "state").mkdir()
    s = Store(tmp_path / "data")
    for i in range(3):
        _fec_line(s, i, "C00607838", "drew-ferguson")
    _fec_line(s, 9, "C00493783", "austin-scott")
    s.kv_set("fec_reread_applied", "2026-11-19T07:20:01+00:00")
    s.flush()
    state, md = tmp_path / "state" / "coverage.json", tmp_path / "data" / "coverage.md"

    monkeypatch.setenv("GITHUB_WORKFLOW", "crawl")
    monkeypatch.setenv("GITHUB_RUN_ID", "111")
    coverage.run(tmp_path, s)
    text = md.read_text()
    assert "Written by the **crawl** workflow (run 111) from the crawl database." in text
    assert "Counted in the crawl database." in text
    assert ("awaiting the re-read (not counted): **4**, most in C00607838 of drew-ferguson (3), "
            "C00493783 of austin-scott (1); the fec-reread job's last hand-off was applied 2026-11-19 07:20 UTC") in text
    assert json.loads(state.read_text())["crawl_db"]["fec_legacy"] == 4

    # fec-reread's copy: its re-read removed the lines there; the crawl database still holds them
    s.delete_where("kind='contribution' AND json_extract(meta,'$.filing_source')='FEC'")
    s.flush()
    monkeypatch.setenv("GITHUB_WORKFLOW", "fec-reread")
    monkeypatch.setenv("GITHUB_RUN_ID", "222")
    coverage.run(tmp_path, s)
    text = md.read_text()
    assert "Written by the **fec-reread** workflow (run 222) from fec-reread's copy of the crawl database" in text
    assert "Counted in fec-reread's copy of the crawl database" in text
    line = next(ln for ln in text.splitlines() if "awaiting the re-read" in ln)
    assert "(not counted): **0**; in the crawl database at the crawl's last report (" in line and "UTC): **4**" in line
    assert "hand-off was applied" not in line
    # a copy's report never stands in for the crawl database's own figure
    assert json.loads(state.read_text())["crawl_db"]["fec_legacy"] == 4 and json.loads(state.read_text())["crawl_db"]["run"] == "111"

    # outside GitHub Actions the report says so
    monkeypatch.delenv("GITHUB_WORKFLOW")
    monkeypatch.delenv("GITHUB_RUN_ID")
    coverage.run(tmp_path, s)
    assert "Written outside GitHub Actions, from the database this checkout's config names." in md.read_text()
    s.close()
