"""A calendar subscription client for the public calendars Georgia's school districts publish (gacrawl/district_calendars.py).

Some districts publish their board's calendar as a public Google Calendar and put it on their own website (an embed,
an "add to calendar" link). Google gives such a calendar a public iCal address,
https://calendar.google.com/calendar/ical/<id>/public/basic.ics, for "other applications" to subscribe to: that is
what Apple Calendar, Outlook and Thunderbird do with it. calendar.google.com's robots.txt (`Disallow: /`) is written for
crawlers indexing its pages, and the crawler obeys it there. This client is not a crawler: it subscribes, as a
calendar application does, to the feed addresses a district itself published, and to nothing else.

What it does, and no more (the owner's decision, Nov 19, 2026):
- only the public iCal address of a calendar a district links on its own site (found on a page of the district's
  host, or checked by hand in config/district_calendars.yaml); never a page of calendar.google.com, never a link in a
  feed, never a private ("/private-...") address;
- says what it is in its User-Agent (Electo's calendar subscription, with the project's address), never another
  program's name;
- refreshes each calendar at most every REFRESH_HOURS (calendar applications refresh subscriptions every few hours),
  asks with the answer's ETag and Last-Modified so an unchanged calendar costs Google nothing, one request at a time
  with SPACING seconds between them;
- a 403, 404, 429 or 5xx is not asked again for BACKOFF_HOURS (a Retry-After longer than that is honoured), and three
  refusals in a row end its use of that calendar until a person looks (state: feed["sub"]["gave_up"]).
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from types import SimpleNamespace

import requests

USER_AGENT = ("ElectoCalendar/1.0 (calendar subscription: the public school-board calendars Georgia districts publish, "
              "for Electo's meeting listings; +https://github.com/ClockworkBlack/Crawl)")
REFRESH_HOURS = 12
BACKOFF_HOURS = 24
SPACING = 10.0                 # seconds between two requests to the calendar service
MAX_BYTES = 5_000_000
GIVE_UP_AFTER = 3
PUBLIC_ICS = re.compile(r"^https://calendar\.google\.com/calendar/ical/[^/?#]+/public/basic\.ics$", re.I)
SUBSCRIBED_HOSTS = ("calendar.google.com",)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _when(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _host(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/?#]+)", url or "", re.I)
    return (m.group(1).lower().removeprefix("www.") if m else "")


class Subscriber:
    def __init__(self, session: requests.Session | None = None, sleep=time.sleep, clock=_now, user_agent: str = USER_AGENT):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/calendar, */*;q=0.5"})
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._last = 0.0
        self.stats = {"asked": 0, "unchanged": 0, "read": 0, "not_due": 0, "backed_off": 0, "refused": 0}

    # ------------------------------------------------------------------ which feeds
    @staticmethod
    def handles(url: str) -> bool:
        """A published public iCal address of the calendar service (the only addresses this client asks for)."""
        return bool(PUBLIC_ICS.match(url or ""))

    @staticmethod
    def published_by_district(feed: dict) -> bool:
        """The district linked this calendar on its own site (or a person checked it): never one found elsewhere."""
        if feed.get("checked_by_hand") or feed.get("source") == "seed":
            return True
        h = _host(feed.get("found_on") or "")
        return bool(h) and not any(h == s or h.endswith("." + s) for s in SUBSCRIBED_HOSTS + ("google.com",))

    # ------------------------------------------------------------------ one refresh
    def due(self, feed: dict) -> bool:
        s = feed.get("sub") or {}
        if s.get("gave_up"):
            return False
        nxt = _when(s.get("next"))
        return nxt is None or self._clock() >= nxt

    def get(self, url: str, feed: dict):
        """The calendar's answer (SimpleNamespace(status, text, content_type, unchanged)), or None when it is not due,
        not a published public address, or no longer asked for. Updates feed["sub"]."""
        if not self.handles(url) or not self.published_by_district(feed):
            return None
        s = feed.setdefault("sub", {})
        if not self.due(feed):
            self.stats["not_due" if not s.get("gave_up") else "backed_off"] += 1
            return None
        with self._lock:                                   # one request at a time, SPACING apart
            wait = self._last + SPACING - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
        headers = {}
        if s.get("etag"):
            headers["If-None-Match"] = s["etag"]
        if s.get("last_modified"):
            headers["If-Modified-Since"] = s["last_modified"]
        now = self._clock()
        self.stats["asked"] += 1
        try:
            r = self.session.get(url, headers=headers, timeout=(10, 30), stream=True, allow_redirects=True)
            body = r.raw.read(MAX_BYTES + 1, decode_content=True) if r.status_code == 200 else b""
        except requests.RequestException as exc:
            s.update(next=_iso(now + dt.timedelta(hours=REFRESH_HOURS)), last=f"error: {type(exc).__name__}")
            return SimpleNamespace(status=0, text="", content_type="", unchanged=False)
        s["asked_at"] = _iso(now)
        if r.status_code == 304:
            self.stats["unchanged"] += 1
            s.update(next=_iso(now + dt.timedelta(hours=REFRESH_HOURS)), last="unchanged", refusals=0)
            return SimpleNamespace(status=304, text="", content_type="text/calendar", unchanged=True)
        if r.status_code == 200 and len(body) <= MAX_BYTES:
            self.stats["read"] += 1
            s.update(next=_iso(now + dt.timedelta(hours=REFRESH_HOURS)), last="read", refusals=0,
                     etag=r.headers.get("ETag") or "", last_modified=r.headers.get("Last-Modified") or "")
            return SimpleNamespace(status=200, text=body.decode(r.encoding or "utf-8", "replace"),
                                   content_type=r.headers.get("Content-Type") or "text/calendar", unchanged=False)
        # refused, gone, rate-limited, failing or too large: left for a day (or as long as the service asks)
        hours = BACKOFF_HOURS
        try:
            hours = max(hours, int(r.headers.get("Retry-After") or 0) / 3600)
        except ValueError:
            pass
        n = int(s.get("refusals") or 0) + 1
        s.update(next=_iso(now + dt.timedelta(hours=hours)), last=f"HTTP {r.status_code}", refusals=n)
        if n >= GIVE_UP_AFTER:
            s["gave_up"] = f"{_iso(now)}: HTTP {r.status_code} {n} times in a row; not asked again until a person looks"
        self.stats["refused"] += 1
        return SimpleNamespace(status=r.status_code, text="", content_type="", unchanged=False)
