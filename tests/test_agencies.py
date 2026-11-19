"""State boards' meeting pages (gacrawl/agencies.py), in the layouts the boards use: a table of dates with agenda
and minutes links (Board of Regents), a date heading over its files (DBHDD), minutes named by day under a year
heading (TCSG), a list of minutes with the date in the link (Corrections), an event page with the day in its
address (georgia.gov sites). A date in a paragraph is not a meeting."""
from types import SimpleNamespace

from gacrawl import agencies
from gacrawl.store import Store

REGENTS = """<html><head><title>2026 Board Meetings</title></head><body><p>The Board was created on January 1, 1932.</p>
<table><tr><th>Date</th><th>Location</th><th>Agenda</th><th>Minutes</th></tr>
<tr><td>October 6-7, 2026</td><td>University of Georgia</td><td><a href="/media/BoR-Oct-agenda.pdf">agenda</a></td><td></td></tr>
<tr><td>September 16, 2026</td><td>Virtual</td><td><a href="/media/a916.pdf">agenda</a></td><td><a href="/media/m916.pdf">minutes</a></td></tr>
<tr><td>November 17, 2026</td><td>Boardroom, Atlanta</td><td></td><td></td></tr></table></body></html>"""
DBHDD = """<html><body><h2>2026 DBHDD Board Meetings</h2>
<h3>Thursday, February 26, 2026</h3><ul><li><a href="/document/agenda/download">Agenda (PDF, 188.33 KB)</a></li>
<li><a href="/files/pres.pdf">Presentation (PDF, 7.18 MB)</a></li><li><a href="/files/feb-min.pdf">Minutes (PDF, 382 KB)</a></li></ul>
<h3>Thursday, April 23, 2026</h3><ul><li><a href="/files/apr-agenda.pdf">Agenda (PDF)</a></li></ul></body></html>"""
TCSG = """<html><body><h2>2026 Meeting Minutes</h2><p><a href="/wp/2026/02/Minutes.pdf">February 5</a> <a href="/wp/2026/03/Min2.pdf">March 11</a></p>
<h2>2025 Meeting Minutes</h2><p><a href="/wp/2025/12/Min3.pdf">December 4</a></p></body></html>"""
GDC = """<html><body><ul><li><a href="/document/x/board-meeting-minutes-september-3-2026/download">Board Meeting Minutes September 3, 2026 (PDF, 120 KB)</a></li></ul>
<a href="/events/2026-11-19/dca-board-meeting-november-2026">DCA Board Meeting - November 2026</a>
<a href="/events/2026-11-20/holiday-closing">Offices closed</a></body></html>"""


def test_layouts():
    r = {m["date"]: m for m in agencies.meetings_on(REGENTS, "https://www.usg.edu/regents/meetings/date/2026/")}
    assert set(r) == {"2026-10-06", "2026-09-16", "2026-11-17"}                     # not the 1932 date in the paragraph
    assert r["2026-09-16"]["agenda"].endswith("/media/a916.pdf") and r["2026-09-16"]["minutes"].endswith("/media/m916.pdf")
    assert r["2026-11-17"]["where"] == "Boardroom, Atlanta" and not r["2026-11-17"]["agenda"]
    d = {m["date"]: m for m in agencies.meetings_on(DBHDD, "https://dbhdd.georgia.gov/board")}
    assert d["2026-02-26"]["agenda"].endswith("/document/agenda/download") and d["2026-02-26"]["minutes"].endswith("feb-min.pdf")
    assert [x["t"] for x in d["2026-02-26"]["docs"]] == ["Presentation (PDF, 7.18 MB)"] and d["2026-04-23"]["agenda"]
    t = {m["date"]: m for m in agencies.meetings_on(TCSG, "https://www.tcsg.edu/about-tcsg/state-board/approved-meeting-minutes/")}
    assert set(t) == {"2026-02-05", "2026-03-11", "2025-12-04"}
    g = {m["date"]: m for m in agencies.meetings_on(GDC, "https://gdc.georgia.gov/minutes")}
    assert g["2026-09-03"]["minutes"] and "2026-11-19" in g and "2026-11-20" not in g


def test_run_stores_one_meeting_a_day_across_pages(tmp_path, monkeypatch):
    pages = {"https://www.usg.edu/regents/meetings/": '<a href="/regents/meetings/date/2026/">2026</a>' + REGENTS.replace("m916.pdf", "none.txt"),
             "https://www.usg.edu/regents/meetings/date/2026/": REGENTS}

    class F:
        def get(self, url, **k):
            return SimpleNamespace(status=200 if url in pages else 404, text=pages.get(url, ""), url=url, content_type="text/html")

    monkeypatch.setattr(agencies, "load", lambda root: [{"id": "regents", "name": "Board of Regents", "agency": "University System of Georgia",
                                                        "pages": ["https://www.usg.edu/regents/meetings/"], "follow": ["/regents/meetings/date/"]}])
    store = Store(tmp_path / "data")
    out = agencies.run(F(), store, tmp_path, log=lambda *a: None)
    assert out["pages"] == 2 and out["by"]["regents"]["meetings"] == 3 and out["by"]["regents"]["with_minutes"] == 1
    recs = {r["meta"]["EventDate"]: r for r in store.read("ga-agency:regents", "meeting")}
    assert len(recs) == 3 and recs["2026-09-16"]["meta"]["EventMinutesFile"].endswith("m916.pdf") and recs["2026-09-16"]["jurisdiction"] == "ga"
    agencies.run(F(), store, tmp_path, log=lambda *a: None)
    assert len(store.read("ga-agency:regents", "meeting")) == 3                     # the same meetings, not twice
    store.close()


def test_a_schedule_written_as_lines_takes_its_year_from_the_heading():
    html = ('<html><head><title>2026 State Board Meeting Schedule</title></head><body><h1>2026 State Board Meeting Schedule</h1>'
            '<h3>January</h3><p>Tuesday, January 14: Committee Meetings</p><h3>May</h3><p>No Meeting</p>'
            '<p>The board meets at 2070 Twin Towers East.</p></body></html>')
    ms = agencies.meetings_on(html, "https://gadoe.org/state-board/board-schedule/")
    assert [(m["date"], m["what"]) for m in ms] == [("2026-01-14", "Committee Meetings")]


def test_a_calendar_that_names_each_session_in_a_link():
    html = ('<table><tr><td>3 <a href="/agendas-calendars/commission-calendar/administrative-session-11-3-2026/">'
            'Administrative Session 11-3-2026</a></td><td><a href="/about/">About 1-2-2026</a></td></tr></table>')
    ms = agencies.meetings_on(html, "https://psc.ga.gov/agendas-calendars/commission-calendar/", "/agendas-calendars/commission-calendar/")
    assert len(ms) == 1 and ms[0]["date"] == "2026-11-03" and ms[0]["what"] == "Administrative Session"
    assert ms[0]["docs"][0]["u"].endswith("/administrative-session-11-3-2026/")


def test_a_years_meeting_dates_file_is_found_and_a_second_date_is_not_a_place():
    files = agencies.schedule_files('<a href="/document/document/2026-bps-meeting-dates/download">2026 Board of Public Safety Meeting Dates</a>'
                                    '<a href="/x.pdf">Annual Report 2026</a>', "https://dps.georgia.gov/meeting-dates")
    assert [(f["year"], f["u"]) for f in files] == [(2026, "https://dps.georgia.gov/document/document/2026-bps-meeting-dates/download")]
    ms = agencies.meetings_on("<table><tr><td>December 9, 2026</td><td>12/10/2026</td></tr><tr><td>November 18, 2026</td>"
                              "<td>Committee Meeting</td></tr></table>", "https://www.dot.ga.gov/GDOT/Pages/BoardMeetings.aspx")
    assert {m["date"]: (m["what"], m["where"]) for m in ms} == {"2026-12-09": ("", ""), "2026-11-18": ("Committee Meeting", "")}


def test_a_page_drawn_in_the_browser_is_read_from_its_data_script():
    import json

    body = ('<p>Meeting times are on Simbli.</p><h3 class="wp-block-heading">January</h3><p class="wp-block-paragraph">Tuesday, January 14: '
            'Committee Meetings<br>Wednesday, January 15: Committee of the Whole and Regular Meeting</p><h3>May</h3><p>No Meeting</p>')
    html = ('<html><head><title>2026 State Board Meeting Schedule</title></head><body><div id="__next"></div>'
            '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"page": {"content": body}}}}) +
            '</script></body></html>')
    ms = agencies.meetings_on(html, "https://gadoe.org/state-board/board-schedule/")
    assert [(m["date"], m["what"]) for m in ms] == [("2026-01-15", "Committee of the Whole and Regular Meeting"), ("2026-01-14", "Committee Meetings")]


def test_a_boards_minutes_files_are_opened_once_newest_first(tmp_path, monkeypatch):
    from gacrawl import ingest
    from gacrawl.store import Record, Store

    class R:
        status, content, content_type, url = 200, b"%PDF-1.4 fake", "application/pdf", ""

    class F:
        got = []

        def get(self, u, use_cache=False):
            self.got.append(u)
            return R()

    monkeypatch.setattr(ingest, "pdf_to_record", lambda content, **k: Record(k["source_id"], k["kind"], k["url"], title=k["title"],
                                                                             text="The board approved the budget. " * 40, meta=k["meta"]))
    store = Store(tmp_path / "data")
    found = {"2026-10-01": {"minutes": "https://b.example/m-oct.pdf", "page": "https://b.example/"},
             "2026-09-01": {"minutes": "https://b.example/m-sep.pdf"}, "2026-11-01": {"minutes": ""}}
    ag = {"id": "regents", "name": "Board of Regents"}
    assert agencies.open_minutes(F(), store, ag, found, None, cap=1) == 1 and F.got == ["https://b.example/m-oct.pdf"]
    assert agencies.open_minutes(F(), store, ag, found, None, cap=5) == 1 and F.got[-1] == "https://b.example/m-sep.pdf"
    assert agencies.open_minutes(F(), store, ag, found, None, cap=5) == 0
    docs = store.read("ga-agency:regents", "document")
    assert len(docs) == 2 and docs[0]["meta"]["doc_type"] == "minutes" and docs[0]["jurisdiction"] == "ga" and docs[0]["published"][:10] == "2026-10-01"
    store.close()


def test_every_listed_body_exports_its_kind(tmp_path):
    """Committee Meetings shows state authorities apart from the boards: each listed body carries kind in the export,
    and the authorities in config/agencies.yaml say so."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    listed = agencies.load(root)
    assert any(a.get("kind") == "authority" for a in listed)
    assert all((a.get("kind") or "board") in ("board", "authority") for a in listed)
    out = tmp_path / "state_meetings.json"
    agencies.export_state_meetings(Store(tmp_path / "data"), root, out, today="2026-11-13")
    kinds = {a["id"]: a["kind"] for a in json.loads(out.read_text())["agencies"]}
    assert kinds["regents"] == "board" and kinds["gefa"] == "authority"


GTEA = """<html><head><title>Board – Georgia Transportation Efficiency Authority</title></head><body>
<div class="elementor"><h2>Board Meetings</h2>
<h2>Upcoming Board Meeting Remote Access Info</h2><h3>Agendas</h3>
<div class="e-text"><p><strong>10:30 AM –</strong> GTEA Committee Meetings</p><ul>
<li><a href="https://gtea.ga.gov/wp-content/uploads/2026/11/Administration-Committee-Meeting-Draft-Agenda.pdf">Administrative Committee Agenda (PDF)</a></li>
<li><a href="https://gtea.ga.gov/wp-content/uploads/2026/11/Progams-and-Projects-Committee-Meeting-Draft-Agenda.pdf">Programs and Projects Committee Agenda (PDF)</a></li></ul>
<p><strong>11:15 AM –</strong> GTEA Board Meeting</p><ul>
<li><a href="https://gtea.ga.gov/wp-content/uploads/2026/11/GTEA-Board-of-Directors-Meeting-Draft-Agenda.pdf">Agenda (PDF)</a></li></ul></div>
<h3>Location</h3><div class="e-info"><div>Date</div><div>Tuesday, November 18, 2026</div><div>Time</div><div>10:30 a.m. – 12:00 p.m.</div></div>
<h2>FY27 GTEA Board and Committee Meeting Schedule</h2>
<div class="e-text"><p>The Board and Committees will hold regular meetings on the following dates</p>
<p>November 18, 2026</p><p>February 16, 2027 *</p><p>April 21, 2027 *</p><p>June 16, 2027 *</p>
<p>*Meeting dates may be adjusted, pending ARC’s TIP Amendment schedule</p></div>
<h2>Agendas from Past GTEA Board Meetings</h2>
<div class="e-acc"><div class="e-title">October 14th, 2026</div>
 <div class="e-body"><div>GTEA Board of Directors</div>
  <div><a href="https://gtea.ga.gov/wp-content/uploads/2026/10/GTEA-Board-of-Directors-Meeting-Agenda.pdf">View Agenda</a></div>
  <div><a href="https://gtea.ga.gov/wp-content/uploads/2026/10/Signed-GTEA-Board-Meeting-Minutes.pdf">View Minutes</a></div>
  <div><a href="https://gtea.ga.gov/wp-content/uploads/2026/10/GTEA-Board-Meeting-Presentation.pdf">View Presentation</a></div>
  <div>Programs and Projects Committee</div>
  <div><a href="https://gtea.ga.gov/wp-content/uploads/2026/10/Progams-and-Projects-Committee-Meeting-Agenda.pdf">View Agenda</a></div></div>
 <div class="e-title">August 19th, 2026</div>
 <div class="e-body"><div>GTEA Board of Directors</div>
  <div><a href="http://atltransit.ga.gov/wp-content/uploads/2026/08/GTEA-Board-of-Directors-Meeting-Draft-Agenda.pdf">View Agenda</a></div>
  <div><a href="http://atltransit.ga.gov/wp-content/uploads/2026/08/8.19.26-GTEA-Board-Meeting-Presentation.pdf">View Presentation</a></div></div>
 <div class="e-title">June 16th, 2026</div>
 <div class="e-body"><div>GTEA Board of Directors</div>
  <div><a href="http://atltransit.ga.gov/wp-content/uploads/2026/06/GTEA-Board-of-Directors-Meeting-Agenda.pdf">View Agenda</a></div></div></div>
</div></body></html>"""


def test_a_page_built_with_dates_as_plain_lines_and_a_schedule_of_dates_alone():
    """The GTEA's board page (the ATL and GRTA since May 2026): the ATL's old page answered 404, and the site showed
    no meeting of the authority at all. Dates are lines of their own, not headings; the coming meetings are dates
    with no files; the current agendas come before their date."""
    ms = {m["date"]: m for m in agencies.meetings_on(GTEA, "https://gtea.ga.gov/board/")}
    assert set(ms) == {"2026-11-18", "2027-02-16", "2027-04-21", "2027-06-16", "2026-10-14", "2026-08-19", "2026-06-16"}
    assert ms["2026-10-14"]["agenda"].endswith("GTEA-Board-of-Directors-Meeting-Agenda.pdf")
    assert ms["2026-10-14"]["minutes"].endswith("Signed-GTEA-Board-Meeting-Minutes.pdf")
    assert any(d["u"].endswith("Progams-and-Projects-Committee-Meeting-Agenda.pdf") for d in ms["2026-10-14"]["docs"])
    assert ms["2026-08-19"]["agenda"].endswith("2026/08/GTEA-Board-of-Directors-Meeting-Draft-Agenda.pdf")
    assert ms["2026-11-18"]["agenda"].endswith("Administration-Committee-Meeting-Draft-Agenda.pdf")     # dated by the line after
    assert not ms["2027-02-16"]["agenda"] and not ms["2027-02-16"]["docs"]                             # a date on the schedule


def test_a_date_standing_alone_away_from_any_schedule_is_not_a_meeting():
    html = "<html><body><h2>News</h2><p>March 3, 2026</p><p>The board thanked its volunteers.</p></body></html>"
    assert agencies.meetings_on(html, "https://x.georgia.gov/news") == []
