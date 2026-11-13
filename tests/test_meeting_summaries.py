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
    st = MS.minutes_run(s, tmp_path, time.monotonic() + 60, llm=llm, log=lambda *a: None)
    assert st["minutes"] == 1 and st["read"] == 1 and st["summaries"] == 1          # the agenda and the unread scan are not minutes to read
    rec = s.read("example:site", "meeting_summary")[0]
    texts = [it["text"] for it in rec["meta"]["items"]]
    assert "The council voted to raise taxes by ten percent." not in texts and len(texts) == 3
    assert rec["meta"]["of_url"].endswith("2026-10-05.pdf") and rec["meta"]["summary"].startswith("The council approved")
    calls = llm.calls
    assert MS.minutes_run(s, tmp_path, time.monotonic() + 60, llm=llm, log=lambda *a: None)["minutes"] == 0 and llm.calls == calls
    s.close()


def test_the_bundle_file_carries_checked_lines_and_leaves_statements_and_routine_business_out(tmp_path):
    s = _store(tmp_path)
    MS.minutes_run(s, tmp_path, time.monotonic() + 60, llm=FakeLLM(summary="Something the lines do not say."), log=lambda *a: None)
    s.add(Record("example:youtube", "transcript", "https://www.youtube.com/watch?v=abc", title="Council Meeting — transcript", text="x",
                 published="2026-10-20", jurisdiction="example", evidence="primary",
                 meta={"transcript_source": "youtube automatic captions", "video_page": "https://www.youtube.com/watch?v=abc",
                       "ai": {"v": 1, "m": "Fake-1", "at": "2026-10-21", "summary": "The council adopted the budget.",
                              "items": [{"type": "statement", "who": "Lee", "text": "i think this is wrong", "needs_review": True},
                                        {"type": "vote", "who": "", "text": "the budget is adopted five to zero"}]}}))
    s.flush()
    counts = MS.export_summaries(s, tmp_path / "meeting_summaries.json")
    d = json.loads((tmp_path / "meeting_summaries.json").read_text())
    assert counts == {"summaries": 2, "from_minutes": 1, "from_recordings": 1, "with_summary": 1}
    rec, mins = d["summaries"]
    assert rec["k"] == "recording" and rec["i"] == [["vote", "", "the budget is adopted five to zero"]] and rec["u"].endswith("v=abc")
    assert mins["k"] == "minutes" and mins["s"] == "" and [x[0] for x in mins["i"]] == ["vote", "motion"]     # the call to order is routine
    s.close()
