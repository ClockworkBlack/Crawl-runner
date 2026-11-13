"""Meeting summaries from minutes and recordings (gacrawl/meeting_summaries.py)."""
import json
import time

from gacrawl import meeting_summaries as MS
from gacrawl.store import Record, Store

MINUTES = ("CITY OF EXAMPLE REGULAR MEETING. The meeting was called to order at 6:00 p.m. "
           "Councilmember Lee moved to approve the rezoning of 12 Oak Street from R-1 to C-2. "
           "The motion carried 4-1 with Councilmember Park opposed. "
           "The council heard a report on the water plant expansion. " + "Filler sentence about nothing in particular. " * 20)


class FakeLLM:
    def __init__(self, summary="The council approved a rezoning of 12 Oak Street and heard a report on the water plant."):
        self.summary, self.calls = summary, 0

    def model_name(self):
        return "Fake-1 (test)"

    def chat(self, msgs, max_tokens=400):
        self.calls += 1
        if "one JSON object per line" in msgs[-1]["content"]:
            return "\n".join(json.dumps(x) for x in [
                {"type": "motion", "who": "Lee", "text": "Councilmember Lee moved to approve the rezoning of 12 Oak Street from R-1 to C-2."},
                {"type": "vote", "who": "", "text": "The motion carried 4-1 with Councilmember Park opposed."},
                {"type": "motion", "who": "", "text": "The council voted to raise taxes by ten percent."},      # not in the minutes
                {"type": "agenda", "who": "", "text": "The meeting was called to order at 6:00 p.m."}])
        return self.summary

    def check(self, line, source, max_words=60, new_share=0.3, allow=()):
        return [] if "rezoning" in line else ["says more than the lines"]

    def log(self, path, rec):
        pass


def _store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add(Record("example:site", "document", "https://example.gov/minutes/2026-10-05.pdf", title="Regular Meeting Minutes", text=MINUTES,
                 published="2026-10-05", jurisdiction="example", evidence="primary", meta={"doc_type": "minutes", "opened": True}))
    s.add(Record("example:site", "document", "https://example.gov/agenda.pdf", title="Agenda", text=MINUTES, published="2026-10-05",
                 jurisdiction="example", evidence="primary", meta={"doc_type": "agenda"}))
    s.add(Record("example:site", "document", "https://example.gov/minutes/scan.pdf", title="Minutes", text="short", published="2026-09-01",
                 jurisdiction="example", evidence="primary", meta={"doc_type": "minutes"}))
    s.flush()
    return s


def test_minutes_are_read_once_and_only_lines_in_the_minutes_are_kept(tmp_path):
    s, llm = _store(tmp_path), FakeLLM()
    st = MS.minutes_run(s, tmp_path, time.monotonic() + 3600, llm=llm, log=lambda *a: None)
    assert st["waiting"] == 1 and st["full_readings"] == 1 and st["summaries"] == 1   # the agenda and the unread scan are not minutes to read
    rec = s.read("example:site", "meeting_summary")[0]
    texts = [it["text"] for it in rec["meta"]["items"]]
    assert "The council voted to raise taxes by ten percent." not in texts and len(texts) == 3
    assert rec["meta"]["of_url"].endswith("2026-10-05.pdf") and rec["meta"]["summary"].startswith("The council approved") and rec["meta"]["by"] == "model"
    calls = llm.calls
    assert MS.minutes_run(s, tmp_path, time.monotonic() + 3600, llm=llm, log=lambda *a: None)["waiting"] == 0 and llm.calls == calls
    s.close()


def _motions(s, url):
    for i, (what, meta) in enumerate([("approve the rezoning of 12 Oak Street from R-1 to C-2", {"result": "passed", "tally": "4-1", "mover": "Lee"}),
                                      ("table the sign ordinance", {"result": "", "tally": ""}),
                                      ("adjourn", {"result": "passed", "procedural": True})]):
        s.add(Record("example:site", "agenda_item", url, title=what, text=what, published="2026-10-05", channel="derived",
                     jurisdiction="example", key=f"example:site|minutes|{url}|{i}|item", meta={"from": "minutes", "procedural": False, **meta}))
    s.flush()


def test_minutes_the_parser_already_read_take_one_short_call_and_show_before_any_model_runs(tmp_path):
    s, llm = _store(tmp_path), FakeLLM()
    url = "https://example.gov/minutes/2026-10-05.pdf"
    _motions(s, url)
    before = MS.export_summaries(s, tmp_path / "m.json")                      # no model has run: the parser's lines are there already
    row = json.loads((tmp_path / "m.json").read_text())["summaries"][0]
    assert before["lines_by_parser"] == 1 and row["s"] == "" and row["by"] == "parser"
    assert row["i"] == [["vote", "Lee", "approve the rezoning of 12 Oak Street from R-1 to C-2 (passed 4-1)"], ["motion", "", "table the sign ordinance"]]
    st = MS.minutes_run(s, tmp_path, time.monotonic() + 3600, llm=llm, log=lambda *a: None)
    assert st["from_parsed_lines"] == 1 and st["full_readings"] == 0 and llm.calls == 1 and st["summaries"] == 1
    after = MS.export_summaries(s, tmp_path / "m.json")
    row = json.loads((tmp_path / "m.json").read_text())["summaries"][0]
    assert after["summaries"] == 1 and row["s"].startswith("The council approved") and row["by"] == "parser"
    s.close()


def test_a_committee_hearing_is_read_from_parts_spread_over_its_whole_length(tmp_path):
    s, llm = _store(tmp_path), FakeLLM()
    long = (MINUTES + " ") * 60
    s.add(Record(MS.HEARINGS, "transcript", "https://vimeo.com/123", title="02.10.26 Judiciary", text=long, published="2026-02-10",
                 jurisdiction="ga", evidence="primary", meta={"committee": "Judiciary", "platform": "vimeo"}))
    s.flush()
    text, cover = MS._spread(long, 6)
    assert len(text) == 36000 and 0 < cover < 1 and text.endswith(long[-200:])
    st = MS.minutes_run(s, tmp_path, time.monotonic() + 3600, llm=llm, log=lambda *a: None)
    assert st["hearings"] == 1
    MS.export_summaries(s, tmp_path / "m.json")
    h = [x for x in json.loads((tmp_path / "m.json").read_text())["summaries"] if x["k"] == "hearing"][0]
    assert h["u"] == "https://vimeo.com/123" and h["c"] == "Judiciary" and h["cov"] == cover and h["s"]
    s.close()


def test_the_bundle_file_carries_checked_lines_and_leaves_statements_and_routine_business_out(tmp_path):
    s = _store(tmp_path)
    MS.minutes_run(s, tmp_path, time.monotonic() + 3600, llm=FakeLLM(summary="Something the lines do not say."), log=lambda *a: None)
    s.add(Record("example:youtube", "transcript", "https://www.youtube.com/watch?v=abc", title="Council Meeting — transcript", text="x",
                 published="2026-10-20", jurisdiction="example", evidence="primary",
                 meta={"transcript_source": "youtube automatic captions", "video_page": "https://www.youtube.com/watch?v=abc",
                       "ai": {"v": 1, "m": "Fake-1", "at": "2026-10-21", "summary": "The council adopted the budget.",
                              "items": [{"type": "statement", "who": "Lee", "text": "i think this is wrong", "needs_review": True},
                                        {"type": "vote", "who": "", "text": "the budget is adopted five to zero"}]}}))
    s.flush()
    counts = MS.export_summaries(s, tmp_path / "meeting_summaries.json")
    d = json.loads((tmp_path / "meeting_summaries.json").read_text())
    assert (counts["summaries"], counts["from_minutes"], counts["from_recordings"], counts["with_summary"]) == (2, 1, 1, 1)
    rec, mins = d["summaries"]
    assert rec["k"] == "recording" and rec["i"] == [["vote", "", "the budget is adopted five to zero"]] and rec["u"].endswith("v=abc")
    assert mins["k"] == "minutes" and mins["s"] == "" and [x[0] for x in mins["i"]] == ["vote", "motion"]     # the call to order is routine
    s.close()


def test_the_jobs_hand_off_carries_its_readings_and_nothing_it_did_not_write(tmp_path):
    from gacrawl import model_handoff as mh

    s = _store(tmp_path)
    start = mh.mark(s, tmp_path)
    s.add(Record("x", "organization", "https://example.org/o", title="An organization", key="x|organization|o"))
    MS.minutes_run(s, tmp_path, time.monotonic() + 3600, llm=FakeLLM(), log=lambda *a: None)
    h = mh.export(s, tmp_path, start, only=("records", "train"), kinds=("meeting_summary",))
    assert [d["kind"] for d in h["records"]] == ["meeting_summary"] and not h["rumors"] and not any(h["kv"].values())
    other = Store(tmp_path / "other.db")
    assert mh.apply(other, tmp_path, h)["records"] == 1 and len(other.read("example:site", "meeting_summary")) == 1
    other.close()
    s.close()


def test_shards_split_the_meetings_with_none_read_twice_and_none_left_out(tmp_path):
    s = _store(tmp_path)
    for k in range(40):
        s.add(Record("example:site", "document", f"https://example.gov/minutes/{k}.pdf", title=f"Minutes {k}", text=MINUTES,
                     published=f"2026-0{1 + k % 9}-10", jurisdiction="example", evidence="primary", meta={"doc_type": "minutes"}))
    s.flush()
    got = []
    for i in range(4):
        st = MS.minutes_run(s, tmp_path, time.monotonic() + 3600, llm=FakeLLM(), log=lambda *a: None, shard=(i, 4))
        got.append(st["waiting"])
        assert st["shard"] == f"{i}/4" and st["full_readings"] == st["waiting"]
    assert sum(got) == 41 and all(n > 0 for n in got)          # 40 here and the one in _store, each in exactly one shard
    assert len(s.read("example:site", "meeting_summary")) == 41
    s.close()


def test_a_long_reading_is_not_started_when_it_could_not_finish(tmp_path):
    s, llm = _store(tmp_path), FakeLLM()
    s.add(Record(MS.HEARINGS, "transcript", "https://vimeo.com/9", title="Hearing", text=(MINUTES + " ") * 60, published="2026-02-10",
                 jurisdiction="ga", evidence="primary", meta={"committee": "Rules"}))
    s.flush()
    st = MS.minutes_run(s, tmp_path, time.monotonic() + 120, llm=llm, log=lambda *a: None)   # 2 minutes: the minutes fit, a 6-part hearing does not
    assert st["full_readings"] == 1 and st["hearings"] == 0 and st["left"] == 1
    s.close()
