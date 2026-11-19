"""data/coverage.md lists the sources the scheduler holds back as stuck on the same pages (scheduler.stuck): from Nov 14
to 19, 2026 Duluth's site read "0 new / 41 seen" on every share and 17 other sites much the same, which only the shard
logs showed. Fictional progress throughout."""
import json

from gacrawl import coverage
from gacrawl.store import Store

STUCK = "partial (html full): 0 new / 41 seen (time slice)"


def test_sources_stuck_on_the_same_pages_are_listed_under_needs_attention(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "progress.json").write_text(json.dumps({
        "duluth:site": {"last_run": "2026-11-19T02:41:11+00:00", "status": STUCK, "new": 0, "seen": 41,
                        "stalled": 4, "stall_seen": 41},
        "valdosta:site": {"last_run": "2026-11-19T02:10:00+00:00", "status": "partial (feed full): 0 new / 40 seen (time slice)",
                          "stalled": 3, "stall_seen": 60, "last_ok": "2026-11-01T05:00:00+00:00"},
        "rome:site": {"status": "partial (html full): 0 new / 1 seen (time slice)", "stalled": 2, "stall_seen": 1},  # not yet
        "winder:site": {"status": "ok", "last_ok": "2026-11-19T03:00:00+00:00", "stalled": 5},      # read in full since
        "macon-bibb:civicclerk": {"status": "partial (api full): 0 new / 92 seen (time slice)", "stalled": 0}}))
    store = Store(tmp_path / "data")
    out = coverage.run(tmp_path, store)
    store.close()
    md = (tmp_path / "data" / "coverage.md").read_text()
    assert out["stuck"] == ["duluth:site", "valdosta:site"]
    assert "**2** sources are stuck on the same pages." in md
    attention = md.split("## Needs attention", 1)[1].split("\n## ", 1)[0]
    assert "- **2 sources stuck on the same pages** (Crawl health)" in attention
    assert (f"  - `duluth:site`: {STUCK}; 4 shares in a row with nothing new, none seeing more than 41 records; "
            "last read in full: never; last tried 2026-11-19 02:41 UTC") in attention
    assert "`valdosta:site`" in attention and "last read in full: 2026-11-01 05:00 UTC" in attention
    assert attention.index("duluth:site") < attention.index("valdosta:site")          # most stalled first
    assert not any(s in md for s in ("rome:site", "winder:site", "macon-bibb"))
    assert json.loads((tmp_path / "state" / "coverage.json").read_text())["counts"]["sources_stuck"] == 2
    assert "| Sources stuck on the same pages, held back by the scheduler (listed under Needs attention) | 2 |" in md


def test_a_long_list_of_stuck_sources_is_cut_short():
    held = [{"id": f"town-{i:02d}:site", "status": STUCK, "stalled": 3, "stall_seen": 41, "last_ok": "", "last_run": ""}
            for i in range(coverage.STUCK_SHOWN + 4)]
    md = coverage.report([], "2026-11-19T08:00:00+00:00", None, {"job": "", "run": "", "crawl": False, "db": "a test"}, held)
    assert md.count("  - `town-") == coverage.STUCK_SHOWN and "  - and 4 more (state/progress.json" in md
