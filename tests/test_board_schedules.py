"""School boards' meeting schedules from their own websites (gacrawl/board_schedules.py): the Open Meetings Act has
every board post its regular meeting dates on its website; Simbli, where most boards keep their meetings, refuses
the crawler. Page texts as the crawl stores them (Markdown); the layouts are those district sites commonly use."""
from gacrawl import board_schedules as bs

LINES = """# Board of Education
The Board of Education meets monthly.
## 2025-2026 Board Meeting Schedule
All meetings are held at the Central Office, 6:00 PM.
- August 12, 2025 - Regular Meeting
- September 9 - Regular Meeting
- Tuesday, October 14 - Work Session, 5:30 p.m.
- November 3 - No School (Teacher Planning Day)
- January 13 - Regular Meeting
- June 9, 2026 - Budget Hearing and Regular Meeting
## Contact the Board
Call 555-0100 on March 3 to reach the clerk.
"""

TABLE = """Calendar of Board Meetings
| Date | Time | Meeting | Location |
| --- | --- | --- | --- |
| 1/13/2026 | 6:00 PM | Regular Board Meeting | Board Room |
| 2/10/2026 | 6:00 PM | Regular Board Meeting | Board Room |
| 3/2/2026 | 9:00 AM | Called Meeting | Board Room |
"""


def test_a_list_of_dates_under_a_schedule_heading():
    ms = {m["d"]: m for m in bs.schedule(LINES, "Board of Education")}
    assert set(ms) == {"2025-08-12", "2025-09-09", "2025-10-14", "2026-01-13", "2026-06-09"}      # no planning day
    assert ms["2025-10-14"]["what"] == "Work Session" and ms["2025-10-14"]["time"] == "5:30pm"
    assert ms["2026-01-13"]["what"] == "Regular Meeting"          # January: the school year's second year
    assert ms["2026-06-09"]["what"].startswith("Budget Hearing")
    assert "2026-03-03" not in ms                                  # a date in the next part of the page


def test_a_table_of_meetings():
    ms = {m["d"]: m for m in bs.schedule(TABLE)}
    assert set(ms) == {"2026-01-13", "2026-02-10", "2026-03-02"}
    assert ms["2026-03-02"]["what"].startswith("Called Meeting") and ms["2026-03-02"]["time"] == "9:00am"


def test_dates_that_are_not_a_schedule_are_not_meetings():
    news = "# News\nThe board meeting schedule changed. On August 12, 2025 the board met.\nStudents return in August."
    assert bs.schedule(news) == []
    cal = "# District Calendar 2025-2026\n- August 4 - First Day of School\n- September 1 - Labor Day Holiday\n- November 24 - Thanksgiving Break"
    assert bs.schedule(cal) == []


def test_the_derive_step_files_them_under_the_district(monkeypatch):
    import datetime as dt

    class D(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2025, 11, 19)

    monkeypatch.setattr(bs.dt, "date", D)
    src = {"source_id": "sd-baldwin:site", "jurisdiction": "sd-baldwin", "url": "https://www.baldwin.k12.ga.us/board", "title": "Board of Education",
           "text": LINES, "id": "x"}
    recs = bs._derive(src, {})
    assert {r.published for r in recs} == {"2025-08-12", "2025-09-09", "2025-10-14", "2026-01-13", "2026-06-09"}
    r = next(r for r in recs if r.published == "2026-01-13")
    assert r.kind == "meeting" and r.jurisdiction == "sd-baldwin" and r.meta["scheduled"] and r.meta["from_page"] == src["url"]
    assert bs._derive({**src, "title": "Lunch menu", "text": "- August 12, 2025 - Pizza\n- August 13, 2025 - Tacos"}, {}) == []
