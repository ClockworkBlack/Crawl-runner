"""`gacrawl finance` keeps to the minutes it is given. While old FEC lines await the re-read it takes an hour on its own,
but the crawl's merge job gives each optional step what the job has left: a forced hour made every run an hour longer
(Nov 9: an hour that re-read no FEC line at all, beside the parallel fec-reread job). Fictional data throughout."""
import time

from gacrawl import candidates, cli, finance
from gacrawl.store import Record, Store


def test_finance_keeps_to_the_minutes_it_is_given(tmp_path, monkeypatch):
    data = tmp_path / "data"
    store = Store(data)
    store.add(Record("cand:pat-test", "contribution", "https://example.test/fec/1", title="Contribution: Avery Quill",
                     meta={"filing_source": "FEC", "committee": "C00000001", "candidate_id": "pat-test",
                           "name": "Avery Quill"}))           # read before the FEC's own fields were kept: awaits the re-read
    store.close()

    seen = {}

    def run(self, cands, since="2016-01-01", deadline=None, org_limit=40, orgs=True):
        seen["minutes"] = round((deadline - time.monotonic()) / 60)
        seen["orgs"], seen["cands"] = orgs, [c["id"] for c in cands]
        return {}

    monkeypatch.setattr(candidates, "load_candidates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_fetcher", lambda cfg, deadline: None)
    monkeypatch.setattr(finance.Finance, "run", run)
    cfg = {"data_dir": str(data), "state_dir": str(tmp_path / "state")}
    for given, expect in ((15, 15), (40, 40), (None, 60)):
        argv = ["finance", "--reread-only"] + (["--max-minutes", str(given)] if given else [])
        cli.cmd_finance(cfg, cli.build_parser().parse_args(argv))
        assert seen.pop("minutes") == expect, argv

    assert seen.pop("orgs") is False                 # the re-read does no organization lookups (the model job's)


def test_the_reread_goes_smallest_first_and_is_left_to_its_job(tmp_path, monkeypatch):
    """Smallest campaigns first (largest first, one Senate campaign held every other off the site for days); with the
    fec-reread job's hand-off applied in the last two days, the crawl leaves those campaigns to it (--defer-reread)."""
    import datetime as dt

    data = tmp_path / "data"
    store = Store(data)
    for cid, n in (("big-senate", 30), ("small-house", 2), ("mid-house", 9)):
        for i in range(n):
            store.add(Record(f"cand:{cid}", "contribution", f"https://example.test/fec/{cid}/{i}", title=f"Contribution {i}",
                             meta={"filing_source": "FEC", "committee": f"C{cid}", "candidate_id": cid, "name": f"Donor {i}"}))
    store.close()
    seen = {}

    def run(self, cands, since="2016-01-01", deadline=None, org_limit=40, orgs=True):
        seen["cands"] = [c["id"] for c in cands]
        return {}

    cands = [{"id": x, "name": x} for x in ("big-senate", "mid-house", "small-house", "state-rep")]
    monkeypatch.setattr(candidates, "load_candidates", lambda *a, **k: cands)
    monkeypatch.setattr(cli, "_fetcher", lambda cfg, deadline: None)
    monkeypatch.setattr(finance.Finance, "run", run)
    monkeypatch.setattr(finance.Finance, "reread_orphans", lambda self, *a, **k: 0)
    cfg = {"data_dir": str(data), "state_dir": str(tmp_path / "state")}
    cli.cmd_finance(cfg, cli.build_parser().parse_args(["finance", "--reread-only", "--max-minutes", "5"]))
    assert seen.pop("cands") == ["small-house", "mid-house", "big-senate"]
    cli.cmd_finance(cfg, cli.build_parser().parse_args(["finance", "--max-minutes", "5"]))
    assert seen.pop("cands") == ["small-house", "mid-house", "big-senate", "state-rep"]
    # the re-read job's hand-off applied an hour ago: the crawl leaves the re-read to it
    s = Store(data)
    s.kv_set("fec_reread_applied", (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat())
    s.close()
    cli.cmd_finance(cfg, cli.build_parser().parse_args(["finance", "--reread-only", "--defer-reread", "--max-minutes", "5"]))
    assert "cands" not in seen                       # nothing to do here
    cli.cmd_finance(cfg, cli.build_parser().parse_args(["finance", "--defer-reread", "--max-minutes", "5"]))
    assert seen.pop("cands") == ["state-rep"]        # new filings of everyone else
    # three days without a hand-off: the crawl does the re-read itself again
    s = Store(data)
    s.kv_set("fec_reread_applied", (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat())
    s.close()
    cli.cmd_finance(cfg, cli.build_parser().parse_args(["finance", "--reread-only", "--defer-reread", "--max-minutes", "5"]))
    assert seen.pop("cands") == ["small-house", "mid-house", "big-senate"]
