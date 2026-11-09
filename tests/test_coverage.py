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
