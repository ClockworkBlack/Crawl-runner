"""The calendar subscription client (gacrawl/calendar_subscriber.py) for the public Google Calendars districts publish:
only published public iCal addresses a district linked, an honest User-Agent, a refresh at most every 12 hours with
ETag / Last-Modified, a day's pause on any refusal and none after three. A made-up service; nothing is asked of Google."""
import datetime as dt
from pathlib import Path

from gacrawl import calendar_subscriber as cs
from gacrawl import district_calendars as dc

URL = "https://calendar.google.com/calendar/ical/boe%40example.k12.ga.us/public/basic.ics"
ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Board of Education\r\nBEGIN:VEVENT\r\nUID:a1@google.com\r\n"
       "DTSTART;TZID=America/New_York:{d}T180000\r\nSUMMARY:Regular Board Meeting\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


class Raw:
    def __init__(self, b):
        self.b = b

    def read(self, n, decode_content=True):
        return self.b[:n]


class Session:
    def __init__(self, answers):
        self.answers, self.asked, self.headers = list(answers), [], {}

    def get(self, url, headers=None, **kw):
        self.asked.append((url, dict(headers or {}), dict(self.headers)))
        status, body, hdrs = self.answers.pop(0)
        return type("R", (), {"status_code": status, "raw": Raw(body.encode()), "headers": hdrs, "encoding": "utf-8"})()


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 19, 6, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t


def sub(answers, clock=None):
    s = Session(answers)
    return cs.Subscriber(session=s, sleep=lambda x: None, clock=clock or Clock()), s


def test_only_published_public_addresses_a_district_linked():
    assert cs.Subscriber.handles(URL)
    for bad in ("https://calendar.google.com/calendar/embed?src=x", "https://calendar.google.com/calendar/ical/x/private-abc/basic.ics",
                "https://calendar.google.com/calendar/u/0/r", "http://calendar.google.com/calendar/ical/x/public/basic.ics"):
        assert not cs.Subscriber.handles(bad), bad
    assert cs.Subscriber.published_by_district({"found_on": "https://www.cook.k12.ga.us/board"})
    assert cs.Subscriber.published_by_district({"source": "seed"})
    assert not cs.Subscriber.published_by_district({"found_on": "https://calendar.google.com/calendar/embed?src=x"})
    s, sess = sub([])
    assert s.get(URL, {"found_on": "https://www.google.com/search?q=x"}) is None and not sess.asked


def test_it_says_what_it_is_and_refreshes_at_most_every_twelve_hours_asking_if_changed():
    clock = Clock()
    s, sess = sub([(200, ICS.format(d="20261208"), {"ETag": '"v1"', "Last-Modified": "Wed, 18 Nov 2026 10:00:00 GMT"}),
                   (304, "", {})], clock)
    feed = {"found_on": "https://www.cook.k12.ga.us/board"}
    r = s.get(URL, feed)
    assert r.status == 200 and "Regular Board Meeting" in r.text
    ua = sess.headers["User-Agent"]
    assert ua.startswith("ElectoCalendar/") and "calendar subscription" in ua and "@" not in ua
    assert s.get(URL, feed) is None and len(sess.asked) == 1               # not due again for 12 hours
    clock.t += dt.timedelta(hours=12, minutes=1)
    r = s.get(URL, feed)
    assert r.unchanged and sess.asked[1][1] == {"If-None-Match": '"v1"', "If-Modified-Since": "Wed, 18 Nov 2026 10:00:00 GMT"}


def test_a_refusal_pauses_a_day_and_three_end_it():
    clock = Clock()
    s, sess = sub([(429, "", {"Retry-After": "172800"}), (403, "", {}), (403, "", {})], clock)
    feed = {"source": "seed"}
    assert s.get(URL, feed).status == 429
    clock.t += dt.timedelta(hours=30)
    assert s.get(URL, feed) is None                                         # Retry-After: two days
    clock.t += dt.timedelta(hours=20)
    assert s.get(URL, feed).status == 403
    clock.t += dt.timedelta(hours=25)
    assert s.get(URL, feed).status == 403 and feed["sub"]["gave_up"]
    clock.t += dt.timedelta(days=30)
    assert s.get(URL, feed) is None and len(sess.asked) == 3


def test_the_calendar_reader_uses_it_for_a_districts_google_calendar(tmp_path):
    from gacrawl.store import Store

    today = dt.date(2026, 11, 19)
    store = Store(tmp_path / "data")
    (tmp_path / "state").mkdir()
    s, sess = sub([(200, ICS.format(d="20261208"), {"ETag": '"v1"'})])

    class NoFetch:
        def get(self, url, **kw):
            raise AssertionError("the crawler's own fetcher is never used for a subscribed calendar: " + url)

    c = dc.Calendars(NoFetch(), store, tmp_path, log=lambda *a: None, archive=False, today=today, subscriber=s)
    feed = {"url": URL, "kind": "ics", "platform": "google", "found_on": "https://www.cook.k12.ga.us/board", "board": True}
    n = c.read_feed("sd-cook", feed, {})
    store.flush()
    assert n == 1 and feed["status"] == "ok" and len(sess.asked) == 1
    rec = store.query("source_id='sd-cook:calendar'")
    assert rec and rec[0]["published"] == "2026-12-08"
    assert c.read_feed("sd-cook", feed, {}) == 0 and len(sess.asked) == 1   # not due: the stored meeting stays
    store.flush()
    assert store.query("source_id='sd-cook:calendar'")
