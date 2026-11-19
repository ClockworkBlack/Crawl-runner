"""A part-read source stuck on the same pages backs off: no spare shares in the run, not resumed first, and a wait
between tries that grows. A reader that moves its cursor (Legistar's vote sweep, CivicClerk's marks inside a year)
is getting further even when everything it read was stored already, and keeps its shares."""
import datetime as dt
import time

import pytest
import requests

from gacrawl import channels
from gacrawl.fetch import Fetcher
from gacrawl.scheduler import STALL_SHARES, Progress, due, stall_after, stall_wait, stuck, run_sources
from gacrawl.store import Record, Store


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def _env(tmp_path):
    return Store(tmp_path / "data"), channels.ChannelCache(tmp_path / "c.json"), Progress(tmp_path / "p.json")


SRC = {"id": "duluth:site", "url": "https://duluth.gov/", "tier": "a", "channel": "html", "endpoint": "https://duluth.gov/"}


def _same_pages(calls, checkpoint=None):
    """A share that reads the same three listing pages every time, then runs out of its slice."""
    def crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        calls.append(dict(ctx.cursors))
        for i in range(3):
            yield Record(src["id"], "page", f"https://duluth.gov/{i}", title=str(i))
        if checkpoint:
            yield channels.Checkpoint("events", checkpoint(len(calls)))
        while True:
            time.sleep(0.05)
            f._check_time()
    return crawl


def test_a_source_rereading_the_same_pages_gets_no_more_shares(tmp_path, fetcher, monkeypatch):
    """Duluth's site, Nov. 14-19, 2026: '0 new / 41 seen' six shares a run, every run. The first share stores the
    pages; the next STALL_SHARES store nothing and see no more, and the run gives it no further share."""
    calls = []
    monkeypatch.setattr(channels, "crawl", _same_pages(calls))
    store, cache, prog = _env(tmp_path)
    lines = []
    out = run_sources({"schedule": {"windows": []}}, [SRC], fetcher, store, cache, prog, time.monotonic() + 600,
                      force=True, workers=1, slice_minutes=0.005, log=lines.append)
    assert len(calls) == 1 + STALL_SHARES, out          # it used to be given a share after every share (8 passes)
    p = prog.get("duluth:site")
    assert p["stalled"] == STALL_SHARES and p["stall_seen"] == 3 and stuck(p)
    assert any("stuck on the same pages" in ln and "duluth:site" in ln for ln in lines)

    # the next run: not resumed first, and it waits before its next try
    now = dt.datetime.fromisoformat(p["last_run"]) + dt.timedelta(minutes=30)
    other = {"id": "co-x:site", "url": "https://x.gov/", "tier": "c"}
    assert [s["id"] for s in due([SRC, other], prog, now=now)] == ["co-x:site"]
    later = now + dt.timedelta(hours=7)
    assert [s["id"] for s in due([SRC, other], prog, now=later)] == ["co-x:site", "duluth:site"]

    # a share that completes clears the count
    def done(f, src, chosen, limits, modified_since=None, ctx=None):
        yield Record(src["id"], "page", "https://duluth.gov/0", title="0")
    monkeypatch.setattr(channels, "crawl", done)
    run_sources({"schedule": {"windows": []}}, [SRC], fetcher, store, cache, prog, time.monotonic() + 600,
                force=True, workers=1, log=lambda *_: None)
    assert prog.get("duluth:site")["status"] == "ok" and prog.get("duluth:site")["stalled"] == 0
    store.close()


def test_a_reader_whose_cursor_moves_keeps_its_shares(tmp_path, fetcher, monkeypatch):
    """CivicClerk after Nov. 18, 2026 marks its place at each new day of a year: a share that re-reads stored meetings
    but gets further is progress. The old reader, back at January 1 every share, moved nothing and is stuck."""
    calls = []
    monkeypatch.setattr(channels, "crawl", _same_pages(calls, checkpoint=lambda n: f"2019-01-{n:02d}"))
    store, cache, prog = _env(tmp_path)
    src = {**SRC, "id": "macon-bibb:civicclerk", "channel": "api", "endpoint": "civicclerk:maconbibbcoga"}
    run_sources({"schedule": {"windows": []}}, [src], fetcher, store, cache, prog, time.monotonic() + 600,
                force=True, workers=1, slice_minutes=0.005, log=lambda *_: None)
    assert len(calls) == 8 and prog.get(src["id"])["stalled"] == 0       # every pass the run allows
    assert calls[-1] == {"events": "2019-01-07"}

    calls.clear()
    (tmp_path / "old").mkdir()
    store2, cache2, prog2 = _env(tmp_path / "old")
    monkeypatch.setattr(channels, "crawl", _same_pages(calls, checkpoint=lambda n: "2019-01-01"))
    store2.set_cursor(src["id"], "events", "2019-01-01")
    run_sources({"schedule": {"windows": []}}, [src], fetcher, store2, cache2, prog2, time.monotonic() + 600,
                force=True, workers=1, slice_minutes=0.005, log=lambda *_: None)
    assert len(calls) == 1 + STALL_SHARES and stuck(prog2.get(src["id"]))
    store.close()
    store2.close()


def test_stall_count_and_wait():
    p = {}
    p.update(stall_after(p, 0, 41, False, True))
    assert p == {"stalled": 1, "stall_seen": 41}
    p.update(stall_after(p, 0, 41, False, True))
    p.update(stall_after(p, 0, 33, False, True))      # a slower share that saw fewer: no further than before
    assert p["stalled"] == 3
    assert stall_after(p, 0, 50, False, True) == {"stalled": 1, "stall_seen": 50}     # it got further
    assert stall_after(p, 2, 41, False, True) == {"stalled": 0}                        # stored something
    assert stall_after(p, 0, 41, True, True) == {"stalled": 0}                         # moved a cursor
    assert stall_after(p, 0, 5, False, False) == {}             # cut short by the run's end: says nothing
    assert not stuck({**p, "status": "ok"}) and stuck({**p, "status": "partial (html full): 0 new / 41 seen (time slice)"})
    day, month = dt.timedelta(days=1), dt.timedelta(days=30)
    assert [stall_wait(day, n) / dt.timedelta(hours=1) for n in (3, 4, 5, 9)] == [6, 12, 24, 24]
    assert [stall_wait(month, n) / day for n in (3, 4, 5, 6)] == [7.5, 15, 30, 30]


def test_due_order_with_a_stuck_source(tmp_path):
    prog = Progress(tmp_path / "p.json")
    now = dt.datetime.now(dt.timezone.utc)
    t = lambda h: (now - dt.timedelta(hours=h)).isoformat()   # noqa: E731
    part = "partial (html full): 0 new / 41 seen (time slice)"
    prog.update("stuck-recent:site", last_run=t(1), status=part, stalled=3, stall_seen=41)
    prog.update("stuck-old:site", last_run=t(7), status=part, stalled=3, stall_seen=41)
    prog.update("stuck-longer:site", last_run=t(7), status=part, stalled=4, stall_seen=41)   # waits 12 hours now
    prog.update("slow:site", last_run=t(1), status=part, stalled=1, stall_seen=41)
    srcs = [{"id": i, "url": f"https://{i.split(':')[0]}.gov/", "tier": "a"}
            for i in ("stuck-recent:site", "stuck-old:site", "stuck-longer:site", "slow:site", "new:site")]
    assert [s["id"] for s in due(srcs, prog, now=now)] == ["slow:site", "new:site", "stuck-old:site"]
    assert len(due(srcs, prog, now=now, force=True)) == 5
