"""Daytime backfill: the switch, the day window, the month's minute cap, the stop rule, one count per run."""
import datetime as dt
import json

from gacrawl import backfill

ET_NOON = dt.datetime(2026, 11, 6, 17, 0, tzinfo=dt.timezone.utc)      # 12:00 EST
ET_NIGHT = dt.datetime(2026, 11, 7, 4, 0, tzinfo=dt.timezone.utc)      # 23:00 EST


def setup(tmp_path, **cfg):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "backfill.json").write_text(json.dumps(dict({"enabled": True, "month_minutes_cap": 1000}, **cfg)))
    return tmp_path


def test_plan_window_and_switch(tmp_path):
    root = setup(tmp_path)
    assert backfill.plan(root, ET_NOON)["go"] is True
    p = backfill.plan(root, ET_NIGHT)
    assert p["active"] is True and p["go"] is False and p["why"] == "outside the day window"
    (root / "config" / "backfill.json").write_text(json.dumps({"enabled": False}))
    assert backfill.plan(root, ET_NOON)["active"] is False


def test_cap_and_stop_rule(tmp_path):
    root = setup(tmp_path)
    r = backfill.next_run(root, 150, True, {"added": {"statement": 400}}, 2000, run_id="1", now=ET_NOON)
    assert r["go"] and r["month_minutes"] == 150 and r["quiet"] == 0
    assert backfill.next_run(root, 150, True, {"added": {}}, 2000, run_id="1", now=ET_NOON)["why"] == "this run was already counted"
    # nightly runs count toward the cap but never chain
    r = backfill.next_run(root, 80, False, None, None, run_id="2", now=ET_NIGHT)
    assert not r["go"] and r["month_minutes"] == 230
    # every candidate researched, then two quiet runs: done
    backfill.next_run(root, 150, True, {"added": {"statement": 10}}, 0, run_id="3", now=ET_NOON)
    r = backfill.next_run(root, 150, True, {"added": {"statement": 5}}, 0, run_id="4", now=ET_NOON)
    assert r["done"] and not r["go"] and "every candidate researched" in backfill.load_state(root)["done_reason"]
    assert backfill.plan(root, ET_NOON)["active"] is False


def test_month_cap_pauses(tmp_path):
    root = setup(tmp_path, month_minutes_cap=300)
    backfill.next_run(root, 200, True, {"added": {"x": 500}}, 900, run_id="1", now=ET_NOON)
    r = backfill.next_run(root, 150, True, {"added": {"x": 500}}, 800, run_id="2", now=ET_NOON)
    assert not r["go"] and "cap" in r["why"]
    # a new month starts fresh
    assert backfill.plan(root, dt.datetime(2026, 12, 1, 17, 0, tzinfo=dt.timezone.utc))["go"] is True


def test_month_minutes_counted_from_the_actions_api(tmp_path):
    """Every run in the repository counts (tests, probes, cancelled runs), as GitHub bills it: each job that ran,
    rounded up to the minute; skipped jobs and self-hosted runners cost nothing; finished runs are cached."""
    import datetime as dt

    from gacrawl import backfill

    now = dt.datetime(2026, 11, 6, 21, 0, tzinfo=dt.timezone.utc)
    runs = {"workflow_runs": [{"id": 1, "status": "completed"}, {"id": 2, "status": "in_progress"}, {"id": 3, "status": "completed"}]}
    jobs = {"1": [{"started_at": "2026-11-06T10:00:00Z", "completed_at": "2026-11-06T10:10:30Z", "conclusion": "success", "labels": ["ubuntu-latest"]},
                  {"started_at": "2026-11-06T10:00:00Z", "completed_at": "2026-11-06T10:00:00Z", "conclusion": "skipped", "labels": []}],
            "2": [{"started_at": "2026-11-06T20:30:00Z", "completed_at": None, "conclusion": None, "labels": ["ubuntu-latest"]}],
            "3": [{"started_at": "2026-11-06T09:00:00Z", "completed_at": "2026-11-06T11:00:00Z", "conclusion": "success", "labels": ["self-hosted", "Linux"]}]}
    calls = []

    class R:
        def __init__(self, d):
            self.d = d

        def raise_for_status(self):
            pass

        def json(self):
            return self.d

    class S:
        headers = {}

        def get(self, url, params=None, timeout=None):
            calls.append(url)
            if url.endswith("/actions/runs"):
                return R(runs)
            return R({"jobs": jobs[url.split("/runs/")[1].split("/")[0]]})
    total, cache = backfill.actions_minutes("o/r", "t", now.replace(day=1), now=now, session=S())
    assert total == 11 + 30 + 0 and cache == {"1": 11, "3": 0}          # the running one is not cached
    calls.clear()
    total2, _ = backfill.actions_minutes("o/r", "t", now.replace(day=1), cache=cache, now=now, session=S())
    assert total2 == total and not any("/runs/1/" in c for c in calls)  # finished runs are not asked again


def test_month_minutes_prefers_the_larger_count(tmp_path):
    import datetime as dt

    from gacrawl import backfill

    now = dt.datetime(2026, 11, 6, 21, 0, tzinfo=dt.timezone.utc)
    st = {"months": {"2026-11": 217}, "months_api": {"2026-11": 1450}}
    cfg = {"month_minutes_before": {"2026-11": 511}}
    assert backfill.month_minutes(st, now, cfg) == 1450
    assert backfill.month_minutes({"months": {"2026-11": 217}}, now, cfg) == 728
