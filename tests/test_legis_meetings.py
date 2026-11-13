"""The General Assembly's committee meeting schedule (gacrawl/legis_meetings.py)."""
import datetime as dt

from gacrawl import legis_meetings as L


def test_a_meeting_of_the_schedule_in_this_crawlers_terms():
    s = L.shape({"id": 7, "start": "2026-11-17T10:00:00", "subject": "House Ways &amp; Means", "chamber": 1, "location": "406 CLOB",
                 "agendaUri": "/api/x/agenda.pdf", "livestreamUrl": "https://example.org/live"})
    assert s["date"] == "2026-11-17" and s["time"] == "10:00" and s["chamber"] == "House"
    assert s["agenda"] == "https://www.legis.ga.gov/api/x/agenda.pdf" and s["video"] == "https://example.org/live"
    assert L.shape({"subject": "no start"}) is None


def test_an_unanswered_month_does_not_lose_the_others(monkeypatch):
    calls = []

    class Fake:
        def __init__(self, fetcher, log=print):
            pass

        def call(self, path):
            calls.append(path)
            if len(calls) == 2:
                raise ConnectionError("read timed out")
            if "startDate" not in path:
                return [{"id": 1, "start": "2026-11-20T09:00:00", "subject": "Senate Rules", "chamber": 2}]
            return []

    monkeypatch.setattr(L, "Legis", Fake)
    out = L.run(None, None, dry_run=True, today=dt.date(2026, 11, 12), log=lambda *a: None)
    assert "startDate" not in calls[0] and len(calls) > 3           # what is coming up first, then back a month at a time
    assert out["meetings"] == 1 and out["upcoming"] == 1 and len(out["errors"]) == 1 and "stopped" not in out
