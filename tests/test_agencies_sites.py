"""Each authority's own page layout (gacrawl/agencies.py READERS), on saved pieces of the pages
(tests/fixtures/agencies). Every test here states a finding of the Nov. 19, 2026 audit of the Georgia Authorities
list: what the site showed wrong, and what the reader must give instead."""
from pathlib import Path

from gacrawl import agencies as A

FX = Path(__file__).parent / "fixtures" / "agencies"


def page(name: str) -> str:
    return (FX / name).read_text()


def by(ms, date, body="", mk=""):
    got = [m for m in ms if m["date"] == date and m["body"] == body and m["mk"] == mk]
    assert len(got) == 1, (date, body, mk, [(m["date"], m["body"], m["mk"]) for m in ms if m["date"] == date])
    return got[0]


# ------------------------------------------------------------------------------------------------ MARTA
def marta():
    ag = {}
    sch = A.marta_schedule(page("marta_meeting-schedule.html"), "https://www.itsmarta.com/meeting-schedule.aspx", ag, {})
    known = {}
    for m in sch:
        known.setdefault((A.slug(m["body"]), m["mk"]), set()).add(m["date"])
    ag["_schedule"] = known
    return sch, A.marta_documents(page("marta_board-documents.html"), "https://www.itsmarta.com/board-documents.aspx", ag, {}), ag


def test_marta_schedule_gives_the_coming_meetings_their_hours_and_the_remote_place():
    sch, _, _ = marta()
    ws, bd = by(sch, "2026-12-03", mk="work-session"), by(sch, "2026-12-03")
    assert (ws["time"], ws["name"], bd["time"], bd["name"]) == ("12:00", "Board Work Session", "13:30", "Board Meeting")
    assert by(sch, "2026-11-20", "Audit Committee")["time"] == "10:00" and "bit.ly/MARTAPublicMeeting" in bd["where"]
    assert not any(m["date"].startswith("2026-12") and m["body"] for m in sch)          # "December - No Meeting"


def test_marta_each_committee_is_its_own_body_with_its_own_files():
    _, docs, _ = marta()
    assert len(docs) == 73                                                              # 27 days, 73 meetings in 2026
    day = [m for m in docs if m["date"] == "2026-11-19"]
    assert sorted(m["body"] for m in day) == ["Business Management Committee", "External Relations Committee",
                                              "Operations & Safety Committee", "Planning & Capital Programs Committee"]
    assert all(A.file_name(m["agenda"]).split()[1] in ("PCP", "OPSS", "BMC", "ER") for m in day)
    assert by(docs, "2026-08-27", mk="special")["name"] == "Special Called Board Meeting"
    assert all(len(m["docs"]) <= 2 for m in docs)                                       # nothing left to an 8-file cap


def test_marta_audit_agenda_labelled_the_19th_is_the_20ths_meeting():
    _, docs, _ = marta()
    assert A.file_name(by(docs, "2026-11-20", "Audit Committee")["agenda"]) == "Agenda Audit 11202026.pdf"
    assert not [m for m in docs if m["date"] == "2026-11-19" and m["body"] == "Audit Committee"]
    assert by(docs, "2026-06-25", "GM/CEO Search Committee")["agenda"]                  # a file name a day off, no schedule: the label stands


def test_marta_approved_minutes_and_summaries_are_kept_apart_and_cancelled_is_cancelled():
    _, docs, ag = marta()
    b = by(docs, "2026-10-08")
    assert "Approved Minutes Board 10082026" in A.file_name(b["minutes"]) and "Summary Board 10082026" in A.file_name(b["summary"])
    assert not by(docs, "2026-11-12")["minutes"] and by(docs, "2026-11-12")["summary"]  # a summary is never the minutes
    for d, body in (("2026-03-19", "Audit Committee"), ("2026-08-20", "Audit Committee"), ("2026-08-27", "Business Management Committee"),
                    ("2026-02-26", "External Relations Committee")):
        m = by(docs, d, body)
        assert m["status"] == "cancelled" and any("No Meeting" in A.file_name(x["u"]) for x in m["docs"])
    assert "mislinked" in ag["_notes"][0]                                               # the Jan. 8 minutes linked under Feb. 12
    assert not any("01082026" in A.file_name(m[s]) for m in docs if m["date"] == "2026-02-12" for s in A.SLOTS)


def test_marta_a_link_with_no_words_and_another_years_label_are_read_around():
    html = ('<h2>Meeting Agendas</h2><p><strong>Operations & Safety Committee</strong></p><ul>'
            '<li><a href="uploadedFiles/More/Board_of_Directors/Agenda OPSS 11192026.pdf">OSC - November 19, 2026</a>'
            '<a href="uploadedFiles/More/Board_of_Directors/Agenda OPSS 11172022.pdf"></a></li></ul>'
            '<h1>Meeting Summaries</h1><p><strong>Business Management Committee</strong></p><ul>'
            '<li><a href="uploadedFiles/More/Board_of_Directors/Summary BMC 02272025.pdf">BMC - February 27, 2024</a></li>'
            '<li><a href="uploadedFiles/More/Board_of_Directors/No Meeting Audit 08202026.pdf">AC - August, 20 2026</a></li></ul>')
    ms = A.marta_documents(html, "https://www.itsmarta.com/board-documents-2025.aspx", {}, {})
    assert {(m["date"], m["body"]) for m in ms} == {("2026-11-19", "Operations & Safety Committee"), ("2025-02-27", "Business Management Committee"),
                                                     ("2026-08-20", "Audit Committee")}
    assert by(ms, "2026-08-20", "Audit Committee")["status"] == "cancelled"
    # one spelling of an address everywhere (the meeting, the opened minutes, the summaries' key): a space is %20
    assert by(ms, "2026-11-19", "Operations & Safety Committee")["agenda"].endswith("/Board_of_Directors/Agenda%20OPSS%2011192026.pdf")


# ------------------------------------------------------------------------------------------------ GTEA and the ATL
def test_gtea_board_and_each_committee_have_their_own_agenda_and_hour():
    ms = A.gtea_board(page("gtea_board.html"), "https://gtea.ga.gov/board/", {}, {})
    board = by(ms, "2026-11-18")
    assert board["agenda"].endswith("GTEA-Board-of-Directors-Meeting-Draft-Agenda.pdf") and board["time"] == "11:15"
    adm = by(ms, "2026-11-18", "Administrative Committee")
    assert adm["agenda"].endswith("Administration-Committee-Meeting-Draft-Agenda.pdf") and adm["time"] == "10:30"
    assert by(ms, "2026-10-14", "Transit Operations Committee")["agenda"].endswith("Transit-Operations-Committee-Meeting-Agenda.pdf")
    assert by(ms, "2027-02-16")["status"] == "tentative" and not board["where"]         # "Virtual" is not copied as the place


def test_gtea_minutes_in_a_row_are_the_meeting_before():
    ms = A.gtea_board(page("gtea_board.html"), "https://gtea.ga.gov/board/", {}, {})
    assert by(ms, "2026-06-16")["minutes"].endswith("6.16.26-Approved-BOD-Meeting-Minutes.pdf")      # linked in the Aug. 19 row
    assert by(ms, "2026-08-19")["minutes"].endswith("2026/10/Signed-GTEA-Board-Meeting-Minutes.pdf")  # linked in the Oct. 14 row
    assert not by(ms, "2026-10-14")["minutes"]


def test_gtea_work_session_comes_from_its_notice():
    html = ('<h1>Uncategorized</h1><h2><a href="https://gtea.ga.gov/georgia-transportation-efficiency-authority-gtea-to-hold-a-board-work-session-'
            'scheduled-for-november-10-2026-at-10-a-m/">Georgia Transportation Efficiency Authority (GTEA) to Hold a Board Work Session '
            'Scheduled for November 10, 2026 at 10 a.m.</a></h2>')
    m = by(A.notice_posts(html, "https://gtea.ga.gov/category/uncategorized/", {}, {}), "2026-11-10", mk="work-session")
    assert (m["name"], m["time"]) == ("Board Work Session", "10:00") and "work-session" in m["page"]


def test_atl_history_the_title_is_who_met_not_where():
    ms = A.title_table(page("atl_board_wayback.html"), "https://atltransit.ga.gov/the-atl-board/", {}, {})
    b = by(ms, "2025-12-04")
    assert b["minutes"].endswith("12.4.25-BOD-Meeting-Minutes-Website.pdf") and not b["where"] and b["name"] == "ATL Board of Directors"
    assert by(ms, "2025-10-02", "Regional Technology Committee")["agenda"] and not by(ms, "2026-02-05")["minutes"]     # "NA"
    assert len({m["date"] for m in ms}) == 8


# ------------------------------------------------------------------------------------------------ Jekyll Island
def test_jekyll_adobe_hosted_files_cancelled_meetings_committee_and_summary():
    ms = A.jekyll_board(page("jekyll_board.html"), "https://www.jekyllisland.com/authority/board-directors/", {}, {})
    oct_ = by(ms, "2026-10-20")
    assert oct_["agenda"].startswith("https://acrobat.adobe.com/id/urn:aaid:sc:VA6C2:73b190e8")
    assert oct_["minutes"].endswith("786709ca-0f71-4c1e-98f6-5b69599f0b18") and oct_["summary"].endswith("9a9b98b2-f57b-48a9-92f2-f4edc50fd290")
    nov = by(ms, "2026-11-17")
    assert nov["summary"] and not nov["minutes"]                                         # "Minutes" is not a link yet
    assert by(ms, "2026-12-15")["status"] == "cancelled" and by(ms, "2026-07-21")["status"] == "cancelled"
    fin = by(ms, "2026-06-02", "Finance Committee")
    assert "FY2027 Budget" in fin["name"] and by(ms, "2026-08-13")["name"] == "Annual Meeting"
    mar = by(ms, "2024-03-19")
    assert mar["minutes"].endswith("20240319-Approved-Minutes.pdf") and mar["summary"].endswith("March_JIA-Board-Meeting-Summary.pdf")
    assert not any(m["video"] for m in ms) and any("Audio" in x["t"] for x in by(ms, "2024-03-19")["docs"])


# ------------------------------------------------------------------------------------------------ GHEFA, GEFA, GSFC
def test_ghefa_its_own_summaries_and_never_gsfics_banner():
    ms = A.meetings_on(page("ghefa_minutes.html"), "https://gsfic.georgia.gov/board-meeting-minutes-0", match="ghefa|higher[- ]education[- ]facilities")
    assert [m["date"] for m in ms] == ["2026-05-26", "2025-12-17", "2025-10-30", "2025-09-19"]          # not 2026-12-09
    assert all(m["summary"] and not m["minutes"] for m in ms)
    past = A.meetings_on(page("gsfic_past.html"), "https://gsfic.georgia.gov/past-events", match="ghefa|higher[- ]education[- ]facilities")
    assert [m["date"] for m in past] == ["2026-05-26", "2025-12-17", "2025-09-19", "2025-06-30"] and past[0]["time"] == "16:30"
    assert past[0]["name"] == "GHEFA Board Meeting" and past[0]["event"].endswith("/events/2026-05-26/ghefa-board-meeting")


def test_a_georgia_gov_event_page_gives_place_hour_agenda_and_files():
    u = "https://gsfic.georgia.gov/events/2026-05-26/ghefa-board-meeting"
    m = A.govhub_event(page("ev_2026-05-26_ghefa-board-meeting.html"), u, {"match": "ghefa"}, {})[0]
    assert (m["date"], m["time"], m["name"]) == ("2026-05-26", "16:30", "GHEFA Board Meeting")           # "4:30 - 5 p.m."
    assert m["where"].startswith("270 Washington Street Southwest") and m["docs"][0]["u"].endswith("ghefa-board-meeting-may-26-2026/download")
    assert A.govhub_event(page("ev_2026-05-26_ghefa-board-meeting.html"), u, {"match": "gsfic commission"}, {}) == []
    u = "https://gefa.georgia.gov/events/2026-08-20/gefa-board-meeting"
    g = A.govhub_event(page("ev_2026-08-20_gefa-board-meeting.html"), u, {}, {})[0]
    assert g["agenda"] == u and g["where"] == "47 Trinity Ave SW, Fifth Floor, Atlanta, GA 30334" and g["time"] == "10:00"
    assert g["docs"] == [{"t": "August 2026 Loan List", "u": "https://gefa.georgia.gov/document/document/august-2026-loan-list/download"}]


def test_gefa_listing_names_hour_and_town_and_leaves_webinars_out():
    ms = A.meetings_on(page("gefa_past0.html"), "https://gefa.georgia.gov/past-events")
    assert len(ms) == 6 and not any("Webinar" in m["name"] for m in ms)
    m = by(ms, "2026-05-12")
    assert (m["name"], m["time"], m["where"]) == ("GEFA Board Meeting", "09:00", "Young Harris, GA")
    assert by(ms, "2026-08-18")["name"].endswith("CommunityWorkplans")                   # the whole name, not cut at 90


def test_gsfc_year_page_brief_actions_is_the_summary():
    ms = A.meetings_on(page("gsfc_2026-board-meetings.html"), "https://gsfc.georgia.gov/2026-board-meetings")
    nov = by(ms, "2026-11-06")
    assert nov["summary"].endswith("board-brief-november-6-2026/download") and nov["agenda"] and not nov["minutes"]
    assert by(ms, "2026-05-07")["where"].startswith("University of North Georgia") and len(ms) == 4


# ------------------------------------------------------------------------------------------------ Stone Mountain
def test_smma_board_and_committee_minutes_are_separate_and_no_file_slides_to_the_row_above():
    ms = A.smma_minutes(page("smma_minutes.html"), "https://stonemountainpark.org/about-us/board-of-directors/meeting-minutes/", {}, {})
    assert by(ms, "2026-09-21")["minutes"].endswith("12026-Board_Minutes_-9-21-2026.doc")              # "1September 21, 2026"
    assert by(ms, "2026-08-17")["minutes"].endswith("Board_Minutes_-8-17-2026.doc") and not by(ms, "2026-08-17")["docs"]
    assert by(ms, "2025-04-28")["minutes"].endswith("2025-Board_Minutes_-4-28-2025.doc")
    assert by(ms, "2025-04-28", "Finance & Development Committee")["minutes"].endswith("Fin-Dev-April-28-2025-minutes.doc")
    assert by(ms, "2026-05-12", mk="special")["name"] == "Special Called Board Meeting"
    assert by(ms, "2020-01-27")["minutes"] and not [m for m in ms if m["date"] == "2020-01-17"]         # the page's "January 17"
    # the general reader, too, no longer takes a neighbouring list item for a heading
    html = '<ul><li><a href="/a/2026-Board_Minutes_-8-17-2026.doc">August 17, 2026</a></li><li><a href="/a/minutes-x.doc">Minutes</a></li></ul>'
    assert [m["date"] for m in A.meetings_on(html, "https://x.org/m")] == ["2026-08-17"] and not A.meetings_on(html, "https://x.org/m")[0]["docs"]


def test_smma_notices_give_the_current_meetings_their_agenda_place_and_hour():
    ms = A.notice_posts(page("smma_feed.xml"), "https://stonemountainpark.org/feed/", {}, {})
    nov = by(ms, "2026-11-16")
    assert (nov["time"], nov["where"], nov["agenda"], nov["pub"]) == ("14:00", "Evergreen Conference Center Rotunda",
                                                                      "https://stonemountainpark.org/july-smma-board-meeting/", "2026-11-08")
    sp = by(ms, "2026-11-09", mk="special")
    assert (sp["time"], sp["where"], sp["name"]) == ("09:00", "Confederate Hall (Main Floor Classroom)", "Special Called Board Meeting")
    assert {m["date"] for m in ms} == {"2026-11-16", "2026-11-09", "2024-12-04"}        # not the ordinance, not the undated notice


# ------------------------------------------------------------------------------------------------ the bodies added
def test_ports_schedule_rows_without_files_and_the_place_on_the_next_row():
    ms = A.meetings_on(page("ports.html"), "https://gaports.com/organization/meeting-notice-agenda/")
    assert len(ms) == 10
    a = by(ms, "2026-11-17")
    assert (a["name"], a["time"]) == ("Authority Meeting", "08:30") and a["agenda"].endswith("Agenda-Authority-Meeting-MEDIA.pdf")
    assert a["where"] == "Georgia Ports Authority, Administration Building 2 Main St., Garden City, GA"
    c = by(ms, "2026-11-16", "Committees")
    assert (c["name"], c["time"]) == ("Committee Meetings", "14:00") and by(ms, "2027-03-23")["where"] == "The Westin, Jekyll Island, GA"


def test_gpb_only_the_commissions_dates_with_hours_and_cancellations():
    ms = A.dated_lines(page("gpb.html"), "https://www.gpb.org/about/board", {}, {"section": "Telecommunications Commission"})
    assert all(m["time"] != "10:00" for m in ms) and not any("foundation" in (m["agenda"] or "").lower() for m in ms)
    assert by(ms, "2026-09-23")["status"] == "cancelled" and by(ms, "2025-08-27")["status"] == "cancelled"
    assert by(ms, "2025-10-22")["agenda"].endswith("Commission_Board_Agenda_October_22_2025.pdf") and by(ms, "2026-12-16")["time"] == "11:00"
    assert by(ms, "2024-05-29")["time"] == "13:00" and by(ms, "2023-04-26")["agenda"] and len(ms) == 14


def test_ngma_minutes_are_its_own_and_the_dnr_boards_are_not_mixed_with_them():
    u = "http://gadnr.org/minutes"
    ng = A.dated_lines(page("gadnr_minutes.html"), u, {}, {"section": "North Georgia Mountain"})
    assert len(ng) == 16 and all("NGMA" in (m["minutes"] or m["summary"]) for m in ng) and by(ng, "2018-10-24")["summary"]
    dnr = A.dated_lines(page("gadnr_minutes.html"), u, {}, {"skip_section": "North Georgia Mountain"})
    assert not any("NGMA" in m["minutes"] + m["summary"] for m in dnr)
    assert by(dnr, "2026-08-25")["minutes"] and by(dnr, "2026-08-25")["summary"] and by(dnr, "2014-08-26", "Coastal Committee")["minutes"]


def test_savannah_schedule_committee_files_and_files_named_by_month():
    ms = A.month_named(page("savannah.html"), "https://www.savconventioncenter.com/venue-info/governance/", {}, {})
    nov = by(ms, "2026-11-11")
    assert nov["time"] == "11:00" and nov["agenda"].endswith("SGCCA-2026-November-Preliminary-Agenda.pdf") and "Savannah Convention Center" in nov["where"]
    assert by(ms, "2026-10-14")["minutes"].endswith("SGCCA-14-October-2026-Board-Meeting-Minutes-signed.pdf")
    assert by(ms, "2026-07-08")["minutes"] and by(ms, "2026-06-10")["minutes"]           # the day is in the file's name only
    assert by(ms, "2026-11-10", "Hotel Development Committee")["agenda"] and by(ms, "2026-04-15")["name"].endswith("(budget workshop)")


def test_srta_a_year_on_its_own_line_dates_the_entries_under_it():
    ms = A.meetings_on(page("srta.html"), "https://srta.ga.gov/srta-board-of-directors-and-leadership/")
    assert by(ms, "2026-06-09")["agenda"].endswith("AgendaDraft-v.3.pdf") and by(ms, "2024-06-06")["agenda"] and by(ms, "2023-01-04")["agenda"]
    assert len(ms) == 19 and not any("Org-Chart" in m["agenda"] for m in ms)


def test_gwcca_schedule_and_files_by_who_met():
    ms = A.gwcca_about(page("gwcca_wayback.html"), "https://www.gwcca.org/about", {}, {})
    assert by(ms, "2026-12-08")["name"] == "Board of Governors Meeting" and "retreat" in by(ms, "2026-09-16", mk="retreat")["name"].lower()
    b = by(ms, "2022-10-25")
    assert b["minutes"].endswith("Board-of-Governors-Meeting-Minutes-FINAL-.pdf") and b["agenda"]
    assert by(ms, "2022-10-25", "Executive Committee")["minutes"] and by(ms, "2021-03-25", mk="special")["minutes"]
    assert by(ms, "2020-01-28", "Personnel Committee")["minutes"] and by(ms, "2025-05-20")["minutes"]


def test_gta_presentations_listed_by_day_under_a_year():
    ms = A.meetings_on(page("gta_pres.html"), "https://gta.georgia.gov/about-us/who-we-are/board-directors/board-meeting-presentations")
    assert len(ms) == 7 and by(ms, "2025-06-05")["docs"][0]["t"] == "Board Meeting Presentation"


def test_transit_authorities_notices():
    cat = A.notice_posts(page("cat_news.html"), "https://catchacat.org/category/news/", {}, {})
    assert [(m["date"], m["time"], m["where"], m["pub"]) for m in cat] == [
        ("2026-09-22", "16:00", "CAT Central, located at 900 E. Gwinnett St.", "2026-09-18"),
        ("2026-08-25", "16:00", "CAT Central, located at 900 E. Gwinnett St.", "2026-08-21")]
    ag = {}
    mb = A.notice_posts(page("mbcta_posts.html"), "https://mbcta.net/category/board-meetings/", ag, {})
    assert by(mb, "2026-10-27")["time"] == "17:30" and by(mb, "2026-10-27")["where"].startswith("Terminal Station")
    ag["_known"] = [(m["date"], m["body"], m["mk"]) for m in mb] + [("2026-08-25", "", "")]
    mins = A.month_named(page("mbcta_minutes.html"), "https://mbcta.net/board-meeting-minutes/", ag, {})
    assert by(mins, "2026-08-25")["minutes"].endswith("MBCTA-Board-August-2026-Minutes.pdf") and by(mins, "2024-01-30")["minutes"]
    assert "named by month alone" in ag["_notes"][0]                                    # months with no known day are said, not guessed


def test_every_reader_named_in_the_config_exists_and_transit_is_authorities_only():
    root = Path(__file__).resolve().parents[1]
    listed = A.load(root)
    names = {e.get("reader") or ag.get("reader") or "general" for ag in listed for e in A.page_entries(ag)}
    assert names <= set(A.READERS), names - set(A.READERS)
    ids = {a["id"]: a for a in listed}
    for new in ("ports", "lottery", "srta", "gta", "savannah-convention", "llida", "gba", "gpb", "ngma", "gwcca"):
        assert ids[new]["kind"] == "authority"
    assert {a["id"] for a in listed if a.get("kind") == "transit"} == {"marta", "chatham-transit", "macon-transit"}
    assert ids["atl-transit"]["kind"] == "authority" and ids["gwcca"]["source"] == "archive"


def test_clock_and_dates_the_pages_write():
    assert [A.clock(t) for t in ("9:30 a.m.", "Noon", "4:30 - 5 p.m.", "1:30 p.m", "2:00 PM", "no hour")] == ["09:30", "12:00", "16:30", "13:30", "14:00", ""]
    assert A.dates_in("AC - August, 20 2026") == ["2026-08-20"] and A.dates_in("1September 21, 2026") == ["2026-09-21"]
    assert A.dates_in("Agenda – November 16,2026") == ["2026-11-16"] and A.dates_in("SGCCA 14 October 2026 Board") == ["2026-10-14"]
    assert A.name_dates("https://x.org/2026-Board_Minutes_-9-21-2026.doc") == ["2026-09-21"]
    assert A.name_dates("https://x.org/Agenda%20Audit%2011202026.pdf") == ["2026-11-20"]
