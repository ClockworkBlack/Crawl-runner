"""School boards' meetings from the districts' own calendars (gacrawl/district_calendars.py). The fixtures keep the
shapes the districts' sites served on Nov. 19, 2026 (Finalsite month grids, list views and buttons; Apptegy's pages and
its generated iCalendar file; Edlio's home page; a Google Calendar embed), cut down to a few events; no network."""
import datetime as dt
import json

from gacrawl import district_calendars as dc
from gacrawl.blocked import Blocked
from gacrawl.fetch import Disallowed
from gacrawl.store import Store

TODAY = dt.date(2026, 11, 19)
LO, HI = TODAY - dt.timedelta(days=800), TODAY + dt.timedelta(days=420)

# Gwinnett's /about-us/calendar: the month grid (event link with data-occur-id: event id, start and end in UTC), each
# event again in the day view (fsStateSoftHidden), a long holiday in several day boxes, days of the next month out of
# range, and the "Upcoming Events" list (article, time.fsDate)
FS_GRID = """<html><head><link rel="canonical" href="https://www.gcpsk12.org/about-us/calendar"></head><body>
<div id="fsEl_11739" class="fsElement fsCalendar fsGrid" data-calendar-ids="1">
 <div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="5" data-month="10" data-year="2026"></div>
  <div class="fsCalendarInfo fsCalendarDayViewEvent noBottomMargin fsCalendarEvent">
   <a class="fsCalendarTitle fsCalendarEventLink" href="#" data-occur-id="1985316746_2026-11-05T21:30:00Z_2026-11-05T23:30:00Z">Policy Committee Meeting</a>
   <div class="fsTimeRange"><time datetime="2026-11-05T16:30:00-05:00" class="fsStartTime">4:30 PM</time></div></div></div>
 <div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="17" data-month="10" data-year="2026"></div>
  <div class="fsCalendarInfo fsCalendarDayViewEvent noBottomMargin fsCalendarEvent">
   <a class="fsCalendarTitle fsCalendarEventLink" href="#" data-occur-id="2053840748_2026-11-18T00:30:00Z_2026-11-18T01:30:00Z">School Board Work Session</a>
   <div class="fsTimeRange"><time datetime="2026-11-17T19:30:00-05:00" class="fsStartTime">7:30 PM</time>
   <time datetime="2026-11-17T20:30:00-05:00" class="fsEndTime">8:30 PM</time></div></div>
  <div class="fsCalendarInfo fsCalendarDayViewEvent noBottomMargin fsStateSoftHidden">
   <a class="fsCalendarTitle fsCalendarEventLink" href="#" data-occur-id="2053840748_2026-11-18T00:30:00Z_2026-11-18T01:30:00Z">School Board Work Session</a></div></div>
 <div class="fsCalendarDaybox fsStateHasEvents fsCalendarToday"><div class="fsCalendarDate" data-day="19" data-month="10" data-year="2026"></div>
  <div class="fsCalendarInfo noBottomMargin"><a class="fsCalendarTitle fsCalendarEventLink" data-occur-id="1577243260_2026-11-19T00:00:00Z_2026-11-20T00:00:00Z">National Parental Involvement Day</a></div>
  <div class="fsCalendarInfo fsCalendarDayViewEvent noBottomMargin fsCalendarEvent">
   <a class="fsCalendarTitle fsCalendarEventLink" href="#" data-occur-id="1286056341_2026-11-20T00:00:00Z_2026-11-20T02:00:00Z">School Board Business Meeting</a>
   <div class="fsTimeRange"><time datetime="2026-11-19T19:00:00-05:00" class="fsStartTime">7:00 PM</time></div></div></div>
 <div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="23" data-month="10" data-year="2026"></div>
  <div class="fsCalendarInfo noBottomMargin"><a class="fsCalendarEventTitle fsCalendarEventLink fsCalendarLongEventTitle"
   href="https://www.gcpsk12.org/about-us/calendar" data-occur-id="1577222401_2026-11-23T00:00:00Z_2026-11-28T00:00:00Z">Thanksgiving Break (School Holiday)</a></div></div>
 <div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="30" data-month="10" data-year="2026"></div></div>
 <div class="fsCalendarDaybox fsCalendarOutOfRange"><div class="fsCalendarDate" data-day="1" data-month="11" data-year="2026"></div></div>
</div>
<div class="fsElement fsCalendar fsSlideshow" data-calendar-ids="1"><h2>Upcoming Events</h2>
 <article><time datetime="2026-11-19T19:00:00-05:00" class="fsDate">Nov 19 2026</time>
  <a class="fsCalendarEventLink" href="#" data-occur-id="1286056341_2026-11-20T00:00:00Z_2026-11-20T02:00:00Z">School Board Business Meeting</a></article>
 <article><time datetime="2026-11-20T00:00:00-05:00" class="fsDate">Nov 20 2026</time>
  <a class="fsCalendarEventLink" href="#" data-occur-id="1577243261_2026-11-20T00:00:00Z_2026-11-21T00:00:00Z">Substitute Educators Day</a></article>
</div><p>Powered by Finalsite</p></body></html>"""

# Fulton's /calendar01: older event numbers (data-occur-id="1426506"), the date only in the day box
FS_OLD = """<div class="fsElement fsCalendar" id="fsEl_217572" data-calendar-ids="820">
<div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="3" data-month="10" data-year="2026"></div>
 <div class="fsCalendarInfo"><a class="fsCalendarEventTitle fsCalendarEventLink" data-occur-id="702252">Election Day / Remote Learning</a></div></div>
<div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="10" data-month="10" data-year="2026"></div>
 <div class="fsCalendarInfo"><a class="fsCalendarEventTitle fsCalendarEventLink" data-occur-id="1426495">FCS Board Work Session</a>
 <div class="fsTimeRange"><time datetime="2026-11-10T11:00:00-05:00" class="fsStartTime">11:00 AM</time></div>
 <div class="fsLocation">North Learning Center</div></div></div>
<div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="19" data-month="10" data-year="2026"></div>
 <div class="fsCalendarInfo"><a class="fsCalendarEventTitle fsCalendarEventLink" data-occur-id="1426506">FCS Board Meeting</a>
 <div class="fsTimeRange"><time datetime="2026-11-19T16:00:00-05:00" class="fsStartTime">4:00 PM</time></div>
 <div class="fsLocation">South Learning Center</div></div></div>
<div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="30" data-month="10" data-year="2026"></div></div>
</div>"""

# Cherokee's /calendar: the title is a button, not a link
FS_BUTTON = """<div class="fsElement fsCalendar" data-calendar-ids="355,364">
<div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="19" data-month="10" data-year="2026"></div>
 <div class="fsCalendarInfo fsCalendarDayViewEvent noBottomMargin fsCalendarEvent">
  <button class="fsCalendarTitle fsCalendarEventLink fsButtonReset fsA11yButtonLink" data-occur-id="1324018">School Board Meeting - Work Session 5:30 pm, Business Meeting 7 pm</button>
  <div class="fsTimeRange"><time datetime="2026-11-19T17:30:00-05:00" class="fsStartTime">5:30 PM</time></div></div></div>
<div class="fsCalendarDaybox fsStateHasEvents"><div class="fsCalendarDate" data-day="23" data-month="10" data-year="2026"></div>
 <div class="fsCalendarInfo"><button class="fsCalendarTitle fsCalendarEventLink" data-occur-id="1324109">Holiday Break - Schools and District Offices closed</button></div></div>
</div>"""

# Savannah-Chatham's /board/calendar: a list view (articles), the calendar's name beside the title, a subscribe button
FS_LIST = """<div class="fsElement fsCalendar fsList" data-calendar-ids="350">
<article><div class="fsTitle"><span class="fsCalendarName">Regular Board Meeting</span>
 <a class="fsCalendarEventLink" href="#" data-occur-id="17442">Board of Public Education Meeting: Regular Session</a>
 <button class="fsCalendarAlertButton">Subscribe to Calendar Alerts</button></div>
 <time datetime="2026-12-09T14:00:00-05:00" class="fsDate">Dec 9</time>
 <time datetime="2026-12-09T14:00:00-05:00" class="fsStartTime">2:00 PM</time>
 <div class="fsLocation">Whitney Administrative Complex, Bldg. G; 2 Laura Avenue</div></article>
<article><div class="fsTitle"><span class="fsCalendarName">Board Calendar</span>
 <a class="fsCalendarEventLink" href="#" data-occur-id="22480">Academic Excellence Committee</a></div>
 <time datetime="2027-01-19T10:00:00-05:00" class="fsDate">Jan 19</time>
 <time datetime="2027-01-19T10:00:00-05:00" class="fsStartTime">10:00 AM</time></article>
</div>"""

# an Apptegy home page: the org's API addresses in __NUXT_DATA__, and its /events page's download link
APPTEGY_HOME = """<html><head><link rel="stylesheet" href="https://cmsv2-static-cdn-prod.apptegy.net/static_js/x/_nuxt/entry.css">
<link rel="canonical" href="https://www.crawfordschools.org/"></head><body>
<a href="https://www.crawfordschools.org/page/board-member-districts">Board Member Districts</a>
<a href="https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?S=4047">Schedule of Meetings- Simbli</a>
<script type="application/json" id="__NUXT_DATA__">[{"events":35},"https://thrillshare-cmsv2.services.thrillshare.com/api/v2/s/414393/events",
"https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/25003/cms/events","/api/v4/o/25003/cms/events?section_ids=414393"]</script>
<p>Powered By Apptegy</p></body></html>"""
APPTEGY_EVENTS = """<html><body><h2>Events</h2>
<a href="https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/25003/cms/events/generate_ical?filter_ids&amp;section_ids">Click to Download Calendar</a>
<p>Powered By Apptegy</p></body></html>"""
APPTEGY_FEED = "https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/25003/cms/events/generate_ical?filter_ids&section_ids"


def apptegy_ics(uids=("8d84463a-4bba-4200-8a42-824cfb0e9ade", "92924662-8aae-449d-a811-2a45c6a3f88a", "5a93a019-c43c-457b-8efc-f542ee76c520")):
    """Apptegy's generated file: floating local times, all-day DATE values, new random UIDs on every request."""
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:icalendar-ruby\r\nCALSCALE:GREGORIAN\r\nBEGIN:VTIMEZONE\r\nTZID:America/New_York\r\n"
            "BEGIN:DAYLIGHT\r\nDTSTART:20270314T030000\r\nTZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\nRRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3\r\n"
            "TZNAME:EDT\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
            f"BEGIN:VEVENT\r\nDTSTAMP:20261119T052945Z\r\nUID:{uids[0]}\r\nDTSTART:20261208T183000\r\nDTEND:20261208T200000\r\n"
            "DESCRIPTION:\r\nLOCATION:190 E Crusselle St\\, Roberta\\, GA 31078\\, USA\r\nSUMMARY:Board Meeting \r\nEND:VEVENT\r\n"
            f"BEGIN:VEVENT\r\nDTSTAMP:20261119T052945Z\r\nUID:{uids[1]}\r\nDTSTART:20261119T160000\r\nDTEND:20261119T180000\r\n"
            "LOCATION:HOME\r\nSUMMARY:Bryan County Middle School Basketball Game\r\nEND:VEVENT\r\n"
            f"BEGIN:VEVENT\r\nDTSTAMP:20261119T052945Z\r\nUID:{uids[2]}\r\nDTSTART;VALUE=DATE:20261123\r\nDTEND;VALUE=DATE:20261128\r\n"
            "SUMMARY:Thanksgiving Break\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


# Cook's home page: its calendar is a Google Calendar embed
GOOGLE_PAGE = """<html><body><a href="https://calendar.google.com/calendar/embed?src=cook.k12.ga.us_go6molpsl1oe4cakfj12lohif8%40group.calendar.google.com&amp;ctz=America%2FNew_York">Calendar</a>
<iframe src="https://calendar.google.com/calendar/embed?height=600&amp;wkst=1&amp;ctz=America%2FNew_York&amp;src=Ym9lQGV4YW1wbGUub3Jn&amp;src=en.usa%23holiday%40group.v.calendar.google.com"></iframe>
<a href="https://calendar.google.com/calendar/u/0?cid=Ym9hcmRAY29vay5rMTIuZ2EudXM">Add the board's calendar</a></body></html>"""
COOK_ICS = "https://calendar.google.com/calendar/ical/cook.k12.ga.us_go6molpsl1oe4cakfj12lohif8%40group.calendar.google.com/public/basic.ics"

# Bacon's Edlio home page: Upcoming Events
EDLIO_HOME = """<html><head><link rel="stylesheet" href="https://baccsdm.edlioschool.com/common_files/main.css"></head><body>
<h2><a href="https://www.bcraiders.com/apps/events/">Upcoming Events</a></h2><ul>
<li><div>Nov 19</div><h3><a href="https://www.bcraiders.com/apps/events/2026/11/19/60813983/?REC_ID=60813983&amp;id=0">BCMS Basketball vs Brantley Co.</a></h3>
 <p>Time: 4:30 PM – 6:30 PM</p><p>Location: Bacon County, GA, USA</p></li>
<li><div>Dec 8</div><h3><a href="/apps/events/2026/12/8/61000001/?REC_ID=61000001&amp;id=0">Board of Education Regular Meeting</a></h3>
 <p>Time: 6:00 PM – 7:00 PM</p></li></ul>
<a href="https://www.bcraiders.com/apps/events/ical/?id=0">iCal</a></body></html>"""

# iCalendar features: folded lines, TZID, UTC, all-day, weekly and monthly rules, exceptions, a moved and a cancelled
# occurrence, an alarm inside an event
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-CALNAME:Board of Education
X-WR-TIMEZONE:America/New_York
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260908T180000
DTEND;TZID=America/New_York:20260908T193000
RRULE:FREQ=MONTHLY;BYDAY=2TU
EXDATE;TZID=America/New_York:20261208T180000
UID:regular@google.com
SUMMARY:Regular Board Meeting\\, Central Office Board
  Room
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Reminder: Board Meeting tomorrow
TRIGGER:-P1D
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:regular@google.com
RECURRENCE-ID;TZID=America/New_York:20270112T180000
DTSTART;TZID=America/New_York:20270114T170000
SUMMARY:Regular Board Meeting (moved to Thursday)
END:VEVENT
BEGIN:VEVENT
UID:ws@google.com
DTSTART:20261103T220000Z
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;COUNT=4
SUMMARY:Board Work Session
END:VEVENT
BEGIN:VEVENT
UID:called@google.com
DTSTART:20261120T000000Z
SUMMARY:Called Board Meeting
END:VEVENT
BEGIN:VEVENT
UID:cancelled@google.com
DTSTART;TZID=America/New_York:20261201T180000
STATUS:CANCELLED
SUMMARY:Budget Hearing
END:VEVENT
BEGIN:VEVENT
UID:hol@google.com
DTSTART;VALUE=DATE:20261123
DTEND;VALUE=DATE:20261128
SUMMARY:Thanksgiving Break - No School
END:VEVENT
BEGIN:VEVENT
UID:old@google.com
DTSTART;TZID=America/New_York:20230110T180000
SUMMARY:Board Meeting
END:VEVENT
BEGIN:VEVENT
UID:far@google.com
DTSTART;TZID=America/New_York:20280210T180000
SUMMARY:Board Meeting
END:VEVENT
BEGIN:VEVENT
UID:until@google.com
DTSTART;VALUE=DATE:20261001
RRULE:FREQ=YEARLY;BYMONTH=6;BYMONTHDAY=15;UNTIL=20280101T000000Z
SUMMARY:Millage Rate Hearing
END:VEVENT
END:VCALENDAR
"""


# ----------------------------------------------------------------------------- Finalsite
def test_finalsite_month_grid():
    evs, rng = dc.parse_finalsite(FS_GRID)
    by = {(dc.day_of(e["start"]).isoformat(), e["title"]): e for e in evs}
    assert rng == (dt.date(2026, 11, 5), dt.date(2026, 11, 30))       # the month's day boxes, the next month's left out
    ws = by[("2026-11-17", "School Board Work Session")]
    assert ws["start"] == dt.datetime(2026, 11, 17, 19, 30) and ws["uid"] == "fs:2053840748"
    assert dc.fmt_time(by[("2026-11-19", "School Board Business Meeting")]["start"]) == "7:00pm"
    assert [t for (d, t) in by if t == "School Board Work Session"] == ["School Board Work Session"]   # day view twice: once
    assert by[("2026-11-19", "National Parental Involvement Day")]["all_day"]
    board = sorted((d, t) for (d, t), e in by.items() if dc.board_meeting(t))
    assert board == [("2026-11-17", "School Board Work Session"), ("2026-11-19", "School Board Business Meeting")]
    assert dc.board_meeting("Policy Committee Meeting") == ""                 # a committee, but not named the board's
    assert dc.board_meeting("Policy Committee Meeting", board_feed=True) == "committee"


def test_finalsite_older_numbers_buttons_and_list_view():
    evs, _ = dc.parse_finalsite(FS_OLD)
    m = next(e for e in evs if e["title"] == "FCS Board Meeting")
    assert m["start"] == dt.datetime(2026, 11, 19, 16, 0) and m["uid"] == "fs:1426506" and m["location"] == "South Learning Center"
    assert next(e for e in evs if e["title"].startswith("Election"))["start"] == dt.date(2026, 11, 3)   # from the day box
    b, _ = dc.parse_finalsite(FS_BUTTON)
    assert [(e["start"], dc.board_meeting(e["title"])) for e in b if dc.board_meeting(e["title"])] == \
        [(dt.datetime(2026, 11, 19, 17, 30), "work session")]
    lst, rng = dc.parse_finalsite(FS_LIST)
    assert rng is None                                                        # a list view: no month shown in full
    assert [e["title"] for e in lst] == ["Board of Public Education Meeting: Regular Session", "Academic Excellence Committee"]
    assert lst[0]["start"] == dt.datetime(2026, 12, 9, 14, 0) and lst[0]["location"].startswith("Whitney")
    assert dc.board_meeting(lst[1]["title"]) == "" and dc.board_meeting(lst[1]["title"], board_feed=True) == "committee"


# ----------------------------------------------------------------------------- discovery
def test_discovery_per_platform():
    fs = dc.find_feeds(FS_GRID + '<a href="/fs/calendar-manager/events.ics?calendar_ids[]=1">iCal</a>',
                       "https://www.gcpsk12.org/about-us/calendar?x=1")
    assert [(x["kind"], x["url"], x["platform"]) for x in fs] == [("finalsite_page", "https://www.gcpsk12.org/about-us/calendar", "finalsite")]
    home = dc.find_feeds(APPTEGY_HOME, "https://www.crawfordschools.org/")
    assert [(x["kind"], x["url"], x["platform"]) for x in home] == [("ics", APPTEGY_FEED, "apptegy")]
    assert [x["url"] for x in dc.find_feeds(APPTEGY_EVENTS, "https://www.crawfordschools.org/events")] == [APPTEGY_FEED]
    ed = dc.find_feeds(EDLIO_HOME, "https://www.bcraiders.com/")
    assert [(x["kind"], x["url"]) for x in ed] == [("edlio_page", "https://www.bcraiders.com/")]   # never /apps/events/ical
    assert dc.platform_of(EDLIO_HOME) == "edlio" and dc.platform_of(APPTEGY_HOME) == "apptegy" and dc.platform_of(FS_GRID) == "finalsite"
    bb = dc.find_feeds('<a href="webcal://www.district.org/site/handlers/icalfeed.ashx?MIID=1234">Subscribe</a>'
                       '<a href="/files/board-meetings-2026-27.ics">Board meetings (.ics)</a>'
                       '<a href="/events/?ical=1">Export Events</a>'
                       + "".join(f'<a href="https://cicmsapi.azurewebsites.net/cobb/_ci/calendar/ics/{n}.ics">Add</a>' for n in range(5))
                       + '<link rel="alternate" type="application/rss+xml" title="Board Calendar RSS" href="/site/RSS.aspx?ModuleInstanceID=9">'
                       '<a href="https://simbli.eboardsolutions.com/x.ics">Simbli</a>', "https://www.district.org/board/calendar")
    got = [(x["kind"], x["url"]) for x in bb]
    assert ("ics", "https://www.district.org/site/handlers/icalfeed.ashx?MIID=1234") in got          # webcal:// as https
    assert ("ics", "https://www.district.org/files/board-meetings-2026-27.ics") in got
    assert ("ics", "https://www.district.org/events/?ical=1") in got                                  # The Events Calendar
    assert ("rss", "https://www.district.org/site/RSS.aspx?ModuleInstanceID=9") in got
    assert not any("cicmsapi" in u or "simbli" in u for _, u in got)      # per-event links; Simbli never asked
    links = dc.calendar_links('<a href="/board/meeting-calendar">Meeting Calendar</a><a href="/calendar">Calendar</a>'
                              '<a href="/athletics/calendar">Athletics Calendar</a><a href="/fs/pages/2081">Full Calendar</a>'
                              '<a href="https://other.org/calendar">Calendar</a><a href="/news">News</a>',
                              "https://www.dekalbschoolsga.org/", {"dekalbschoolsga.org"}, "finalsite")
    assert links == ["https://www.dekalbschoolsga.org/board/meeting-calendar", "https://www.dekalbschoolsga.org/calendar"]


def test_google_embed_becomes_its_public_ics_address():
    feeds = dc.find_feeds(GOOGLE_PAGE, "https://www.cook.k12.ga.us/")
    urls = [x["url"] for x in feeds]
    assert urls[0] == COOK_ICS and all(x["platform"] == "google" and x["kind"] == "ics" for x in feeds)
    assert "https://calendar.google.com/calendar/ical/en.usa%23holiday%40group.v.calendar.google.com/public/basic.ics" in urls
    # cid= (base64 of the calendar's address)
    assert "https://calendar.google.com/calendar/ical/board%40cook.k12.ga.us/public/basic.ics" in urls
    assert dc.gcal_ics("a@group.calendar.google.com") == "https://calendar.google.com/calendar/ical/a%40group.calendar.google.com/public/basic.ics"


# ----------------------------------------------------------------------------- iCalendar
def test_ics_folding_zones_all_day_and_rules():
    name, evs = dc.ics_events(ICS, LO, HI, TODAY + dt.timedelta(days=dc.RRULE_AHEAD_DAYS))
    assert name == "Board of Education"
    got = sorted(((e["start"], e["title"]) for e in evs), key=lambda x: (str(x[0]), x[1]))
    reg = [s for s, t in got if t.startswith("Regular Board Meeting,")]
    assert reg[0] == dt.datetime(2026, 9, 8, 18, 0) and reg[1] == dt.datetime(2026, 10, 13, 18, 0)   # the second Tuesday
    assert dt.datetime(2026, 12, 8, 18, 0) not in reg                         # EXDATE
    assert dt.datetime(2027, 1, 12, 18, 0) not in reg                         # moved: RECURRENCE-ID
    assert (dt.datetime(2027, 1, 14, 17, 0), "Regular Board Meeting (moved to Thursday)") in got
    assert max(reg) <= dt.datetime.combine(TODAY + dt.timedelta(days=dc.RRULE_AHEAD_DAYS), dt.time(23))   # ~13 months
    assert "Regular Board Meeting, Central Office Board Room" in {t for _, t in got}              # a folded line
    # UTC: 22:00Z on Nov 3 is 5 PM in Georgia; every other Tuesday, four times
    assert [s for s, t in got if t == "Board Work Session"] == [dt.datetime(2026, 11, 3, 17, 0), dt.datetime(2026, 11, 17, 17, 0),
                                                                dt.datetime(2026, 12, 1, 17, 0), dt.datetime(2026, 12, 15, 17, 0)]
    assert (dt.datetime(2026, 11, 19, 19, 0), "Called Board Meeting") in got                     # 00:00Z the next day
    assert not any(t == "Budget Hearing" for _, t in got)                                        # cancelled
    hol = next(e for e in evs if e["title"].startswith("Thanksgiving"))
    assert hol["all_day"] and hol["start"] == dt.date(2026, 11, 23)
    assert [s for s, t in got if t == "Millage Rate Hearing"] == [dt.date(2027, 6, 15)]           # yearly, until 2028
    assert not any(t == "Reminder: Board Meeting tomorrow" for _, t in got)                       # the alarm is not an event
    assert not any(dc.day_of(s) < LO or dc.day_of(s) > HI for s, _ in got)                        # the window


def test_floating_times_and_eastern_time_without_a_zone_database(monkeypatch):
    name, evs = dc.ics_events(apptegy_ics(), LO, HI)
    assert [(e["start"], e["title"].strip()) for e in evs][0] == (dt.datetime(2026, 12, 8, 18, 30), "Board Meeting")
    monkeypatch.setattr(dc, "_ET", None)
    assert dc.to_local(dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)) == dt.datetime(2026, 11, 19, 19, 0)
    assert dc.to_local(dt.datetime(2026, 7, 15, 22, 0, tzinfo=dt.timezone.utc)) == dt.datetime(2026, 7, 15, 18, 0)


# ----------------------------------------------------------------------------- which entries are board meetings
def test_board_meeting_filter():
    yes = {"School Board Business Meeting": "regular meeting", "FCBOE Business Meeting": "regular meeting",
           "BOE Work Session and Meeting": "work session", "Called Work Session": "work session",
           "PCS School Board Meeting": "regular meeting", "Rockdale County Board of Education Meeting Regular Session": "regular meeting",
           "Board of Education - Combined Work Session-Regular Monthly Meeting": "work session",
           "Special Called Meeting": "called meeting", "Public Hearing on the FY2027 Budget": "budget or tax hearing",
           "Millage Rate Hearing": "budget or tax hearing", "Board Retreat": "retreat", "Executive Session": "executive session",
           "Budget Commission Meeting": "budget meeting", "BOE Committee Meeting": "committee",
           "Board of Education Meeting": "regular meeting", "Board Meeting - Student Recognition": "regular meeting"}
    for t, k in yes.items():
        assert dc.board_meeting(t) == k, t
    for t in ["Thanksgiving Break (School Holiday)", "BCMS Boys Basketball @ Legacy Knoll", "PJHS PTO Meeting in Auditorium",
              "Education Foundation Board Meeting", "Student Advisory Board Meeting", "Board Candidate Forum",
              "School Board Appreciation Month", "General Administrators' Meeting", "Teacher Leader Council",
              "EOC Lit & Comp II", "Board Meeting - CANCELLED", "Graduation", "Governance Team Development and Training",
              "Subscribe to Calendar Alerts", "Regular Meeting"]:
        assert dc.board_meeting(t) == "", t
    assert dc.board_meeting("Audit Committee Meeting", board_feed=True) == "committee"
    assert dc.board_meeting("Principals Meeting", board_feed=True) == ""


# ----------------------------------------------------------------------------- reading, keys, state
class FakeResult:
    def __init__(self, url, status, body, ctype):
        self.url, self.status, self.content_type = url, status, ctype
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class FakeFetcher:
    def __init__(self, pages: dict):
        self.pages, self.asked = pages, []

    def get(self, url, **kw):
        self.asked.append(url)
        x = self.pages.get(url)
        if x is None:
            return FakeResult(url, 404, "", "text/html")
        if isinstance(x, Exception):
            raise x
        body, ctype = x if isinstance(x, tuple) else (x, "text/html")
        return FakeResult(url, 200, body, ctype)


def _root(tmp_path, seed: dict):
    (tmp_path / "config").mkdir()
    (tmp_path / "state").mkdir()
    import yaml

    (tmp_path / "config" / dc.SEED_NAME).write_text(yaml.safe_dump({"districts": seed}), encoding="utf-8")
    return tmp_path


JUR = [{"id": "sd-crawford", "kind": "school_district", "website": "https://www.crawfordschools.org/"},
       {"id": "sd-gwinnett", "kind": "school_district", "website": "http://www.gwinnett.k12.ga.us/"},
       {"id": "sd-cook", "kind": "school_district", "website": "https://www.cook.k12.ga.us/"},
       {"id": "sd-bacon", "kind": "school_district", "website": "http://www.bcraiders.com/"},
       {"id": "co-cook", "kind": "county", "website": "https://cookcountyga.us/"}]


def pages(ics_uids=None, fs=FS_GRID):
    p = {"https://www.crawfordschools.org/": APPTEGY_HOME, "https://www.crawfordschools.org/events": APPTEGY_EVENTS,
         APPTEGY_FEED: (apptegy_ics(ics_uids) if ics_uids else apptegy_ics(), "text/calendar"),
         "https://www.gcpsk12.org/": '<html><a href="/about-us/calendar">Calendar</a> Powered by Finalsite fsElement</html>',
         "https://www.cook.k12.ga.us/": GOOGLE_PAGE, COOK_ICS: Disallowed(COOK_ICS, "robots.txt disallows this path"),
         "http://www.bcraiders.com/": EDLIO_HOME}
    for off in range(-24, 14):
        first = dc._month(TODAY, off)
        p[f"https://www.gcpsk12.org/about-us/calendar?cal_date={first.isoformat()}"] = fs if off == 0 else "<div class='fsCalendar'></div>"
    p["https://www.gcpsk12.org/about-us/calendar"] = fs
    return p


def test_reading_every_platform_stores_board_meetings(tmp_path):
    root = _root(tmp_path, {"sd-gwinnett": {"site": "https://www.gcpsk12.org/", "feeds": [
        {"url": "https://www.gcpsk12.org/about-us/calendar", "kind": "finalsite_page", "platform": "finalsite", "checked": "2026-11-19"}]}})
    store = Store(tmp_path / "data")
    f = FakeFetcher(pages())
    out = dc.Calendars(f, store, root, workers=2, archive=False, today=TODAY).run(JUR)
    recs = {(r["jurisdiction"], r["published"], r["title"].strip()): r for r in store.query("kind='meeting'")}
    assert set(recs) == {("sd-gwinnett", "2026-11-17", "School Board Work Session"), ("sd-gwinnett", "2026-11-19", "School Board Business Meeting"),
                         ("sd-crawford", "2026-12-08", "Board Meeting"), ("sd-bacon", "2026-12-08", "Board of Education Regular Meeting")}
    m = recs[("sd-gwinnett", "2026-11-19", "School Board Business Meeting")]
    meta = m["meta"] if isinstance(m["meta"], dict) else json.loads(m["meta"])
    assert m["source_id"] == "sd-gwinnett:calendar" and m["url"] == "https://www.gcpsk12.org/about-us/calendar?cal_date=2026-11-01"
    assert {k: meta[k] for k in ("EventDate", "EventBodyName", "time", "provider", "scheduled", "platform", "from_feed", "meeting_type")} == \
        {"EventDate": "2026-11-19", "EventBodyName": "Board of Education", "time": "7:00pm", "provider": "district calendar",
         "scheduled": True, "platform": "finalsite", "from_feed": "https://www.gcpsk12.org/about-us/calendar", "meeting_type": "regular meeting"}
    cr = recs[("sd-crawford", "2026-12-08", "Board Meeting")]
    assert cr["url"] == "https://www.crawfordschools.org/" and "uid" not in (cr["meta"] if isinstance(cr["meta"], dict) else json.loads(cr["meta"]))
    assert recs[("sd-bacon", "2026-12-08", "Board of Education Regular Meeting")]["url"].endswith("://www.bcraiders.com/apps/events/2026/12/8/61000001/?REC_ID=61000001&id=0")
    assert not any("/apps/events/ical" in u or "/fs/" in u or "simbli" in u for u in f.asked)    # never asked
    st = json.loads((root / "state" / dc.STATE_NAME).read_text())
    cook = st["sd-cook"]["feeds"]
    assert cook[0]["url"] == COOK_ICS and cook[0]["status"].startswith("refused: Disallowed")    # robots.txt: recorded, left
    assert st["sd-gwinnett"]["feeds"][0]["source"] == "seed" and st["sd-gwinnett"]["upcoming"] == 1
    assert st["sd-crawford"]["platform"] == "apptegy" and st["sd-crawford"]["checked"] == TODAY.isoformat()
    assert "co-cook" not in st and out["with_board_meetings"] == 3 and out["refused"] == 1
    sm = dc.summary(root)                                                       # the coverage report's line
    assert (sm["looked"], sm["with_feeds"], sm["with_board_meetings"], sm["with_upcoming"], sm["refused"]) == (4, 4, 3, 3, 1)
    assert sm["by_platform"] == {"apptegy": 1, "edlio": 1, "finalsite": 1, "google": 1}


def test_keys_are_stable_across_reads_and_moved_meetings_go(tmp_path):
    root = _root(tmp_path, {})
    store = Store(tmp_path / "data")
    jur = [JUR[0]]
    dc.Calendars(FakeFetcher(pages()), store, root, archive=False, today=TODAY).run(jur)
    k1 = sorted(r["key"] for r in store.query("kind='meeting'"))
    # Apptegy's UIDs are new on every request: the same meeting keeps its record
    dc.Calendars(FakeFetcher(pages(ics_uids=("a", "b", "c"))), store, root, archive=False, today=TODAY).run(jur)
    assert sorted(r["key"] for r in store.query("kind='meeting'")) == k1 == ["sd-crawford:calendar|meeting|cal|2026-12-08|board-meeting|6:30pm"]
    # the meeting moved to Dec 10: the Dec 8 record goes, the Dec 10 one comes
    p = pages()
    p[APPTEGY_FEED] = (apptegy_ics().replace("DTSTART:20261208T183000", "DTSTART:20261210T183000"), "text/calendar")
    dc.Calendars(FakeFetcher(p), store, root, archive=False, today=TODAY).run(jur)
    assert [r["published"] for r in store.query("kind='meeting'")] == ["2026-12-10"]
    # a Google UID names the event: the key is the UID and the date
    rec = dc.Calendars(FakeFetcher({}), store, root, archive=False, today=TODAY)
    feed = {"url": "https://calendar.google.com/calendar/ical/x/public/basic.ics", "kind": "ics", "platform": "google", "found_on": "https://d.org/cal"}
    _, evs = dc.ics_events(ICS, rec.lo, rec.hi, rec.rrule_hi)
    rec.store_events("sd-x", feed, evs, {}, [], {}, {})
    keys = {r["key"] for r in store.query("source_id='sd-x:calendar'")}
    assert "sd-x:calendar|meeting|cal|2026-11-19|called@google.com" in keys
    assert not any("old@" in k or "far@" in k for k in keys)                    # outside 800 days back / 420 ahead


def test_refused_feed_read_from_an_archive_copy(tmp_path):
    root = _root(tmp_path, {})
    store = Store(tmp_path / "data")

    class WB:
        off = ""

        def _get(self, url, params=None):
            if "cdx" in url:
                return FakeResult(url, 200, [["timestamp", "original"], ["20261115093000", COOK_ICS]], "application/json")
            return FakeResult(url, 200, ICS, "text/calendar")
    cal = dc.Calendars(FakeFetcher(pages()), store, root, archive=True, today=TODAY, wayback=WB())
    cal.run([JUR[2]])
    rows = store.query("source_id='sd-cook:calendar'")
    assert rows and all((r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"]))["archived"] for r in rows)
    assert {r["url"] for r in rows} == {"https://www.cook.k12.ga.us/"}          # the district's page that shows the calendar
    st = json.loads((root / "state" / dc.STATE_NAME).read_text())
    assert st["sd-cook"]["feeds"][0]["archive"] == {"checked": TODAY.isoformat(), "found": "2026-11-15"}


def test_a_blocked_site_and_a_district_without_a_site(tmp_path):
    root = _root(tmp_path, {})
    store = Store(tmp_path / "data")
    jur = [{"id": "sd-a", "kind": "school_district", "website": "https://a.k12.ga.us/"}, {"id": "sd-b", "kind": "school_district"}]
    out = dc.Calendars(FakeFetcher({"https://a.k12.ga.us/": Blocked("a.k12.ga.us", "challenge page")}), store, root,
                       archive=False, today=TODAY).run(jur)
    st = json.loads((root / "state" / dc.STATE_NAME).read_text())
    assert st["sd-a"]["note"].startswith("home page refused: Blocked") and st["sd-b"]["note"].startswith("no website")
    assert out["with_feeds"] == 0 and out["districts"] == 2
    # the next run does not look again within the week
    f = FakeFetcher({})
    dc.Calendars(f, store, root, archive=False, today=TODAY + dt.timedelta(days=3)).run(jur)
    assert f.asked == []


def test_rss_and_jsonld_events():
    rss = """<?xml version="1.0"?><rss xmlns:ev="http://purl.org/rss/1.0/modules/event/"><channel>
    <item><title>Board of Education Work Session</title><link>https://d.org/event/1</link><ev:startdate>2026-12-01T17:30:00-05:00</ev:startdate></item>
    <item><title>Regular Board Meeting</title><description>Date: December 14, 2026 6:00 PM, Central Office</description></item>
    <item><title>Winter Concert</title><pubDate>Thu, 19 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    evs = dc.parse_rss(rss)
    assert [(e["title"], e["start"]) for e in evs] == [("Board of Education Work Session", dt.datetime(2026, 12, 1, 17, 30)),
                                                       ("Regular Board Meeting", dt.datetime(2026, 12, 14, 18, 0))]
    ld = """<script type="application/ld+json">[{"@context":"http://schema.org","@type":"Event","name":"Board Meeting",
    "startDate":"2026-12-03T18:00:00-05:00","url":"https://d.org/event/board-meeting/","location":{"@type":"Place","name":"Board Room"}}]</script>"""
    e = dc.parse_jsonld(ld)[0]
    assert (e["title"], e["start"], e["location"], e["url"]) == ("Board Meeting", dt.datetime(2026, 12, 3, 18, 0), "Board Room",
                                                                "https://d.org/event/board-meeting/")
    j = dc.parse_apptegy_json({"events": [{"id": 51712352, "title": "Board Meeting ", "start_at": "2026-12-08T18:30:00.000-05:00",
                                           "all_day": False, "venue": "190 E Crusselle St, Roberta, GA 31078, USA"}]})
    assert j[0]["start"] == dt.datetime(2026, 12, 8, 18, 30) and j[0]["uid"] == "apptegy:51712352"


def test_seed_file_is_valid():
    from pathlib import Path

    from gacrawl.registry import jurisdictions

    root = Path(__file__).resolve().parents[1]
    seed = dc.load_seed(root)
    ids = {j["id"] for j in jurisdictions(root) if j.get("kind") == "school_district"}
    assert seed and set(seed) <= ids
    kinds = {"ics", "apptegy_json", "finalsite_page", "edlio_page", "jsonld_page", "rss"}
    for did, v in seed.items():
        for x in v.get("feeds") or []:
            assert x["url"].startswith("https://") and x.get("kind") in kinds and x.get("checked"), (did, x)
            assert "/fs/" not in x["url"] and "/apps/" not in x["url"], (did, x)


def test_export_lists_each_meeting_once():
    """The School Districts file (schools/<id>.json meeting_list, which Electo's Committee Meetings tab reads): a day the
    meeting system has stays its own, a calendar's meeting replaces a schedule page's, and two calendars' copies are one."""
    from gacrawl.schools import dedupe_meetings

    ms = [{"d": "2026-11-19", "title": "Regular Meeting", "u": "a", "via": "district website"},
          {"d": "2026-11-19", "title": "School Board Business Meeting", "u": "b", "time": "7:00pm", "via": "district calendar"},
          {"d": "2026-11-19", "title": "School Board Business  Meeting", "u": "c", "via": "district calendar"},
          {"d": "2024-05-14", "title": "Regular Meeting", "u": "d", "via": "simbli"},
          {"d": "2024-05-14", "title": "Board Meeting", "u": "e", "via": "district calendar"},
          {"d": "2026-12-10", "title": "Regular Meeting", "u": "f", "via": "district website"}]
    out = dedupe_meetings(ms)
    assert [m["u"] for m in out] == ["b", "d", "f"] and not any("via" in m for m in out) and out[0]["time"] == "7:00pm"
