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

    def run(self, cands, since="2016-01-01", deadline=None, org_limit=40):
        seen["minutes"] = round((deadline - time.monotonic()) / 60)
        return {}

    monkeypatch.setattr(candidates, "load_candidates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_fetcher", lambda cfg, deadline: None)
    monkeypatch.setattr(finance.Finance, "run", run)
    cfg = {"data_dir": str(data), "state_dir": str(tmp_path / "state")}
    for given, expect in ((15, 15), (40, 40), (None, 60)):
        argv = ["finance", "--reread-only"] + (["--max-minutes", str(given)] if given else [])
        cli.cmd_finance(cfg, cli.build_parser().parse_args(argv))
        assert seen.pop("minutes") == expect, argv
