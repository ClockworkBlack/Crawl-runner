"""Reader defects from the Nov 2026 audits (the crawl core's and the Electo import's), one test each:
WebForms pages, minutes motions keyed by paragraph, feed pages stored twice, oversized files downloaded every run,
election reports read with their headings shifted, motions cut at an abbreviation, page phrases taken as people,
unusable e-mail addresses, and dates read from a page's words decades before it was written."""
import datetime as dt
import json
import re

from gacrawl import derive, extract, minutes
from gacrawl.elections import parse_results, parse_summary_report
from gacrawl.personname import clean_email, own_page, plausible_person
from gacrawl.store import Record, Store

from test_ingest_audit import Site, env, page, rss, site  # noqa: F401 — the fixtures


# 1 ------------------------------------------------------------------ an ASP.NET WebForms page is read, not emptied
WEBFORMS = """<html><head><title>City Council - Meeting Details</title></head><body>
<form method="post" action="./MeetingDetail.aspx" id="aspnetForm">
<input type="hidden" name="__VIEWSTATE" value="/wEPDwUKMTY1NDU2MTA1Mg9kFgJmD2QWAgIDD2QWAg==" />
<header><a href="/">Home</a> <a href="/calendar">Calendar</a></header>
<nav><ul><li><a href="/a">Agendas</a></li><li><a href="/b">Departments</a></li></ul></nav>
<div id="content"><h1>Regular Council Meeting</h1>
<p>The council approved the annual budget for fiscal year 2027 after a public hearing, and adopted the millage rate.</p>
<p>Council also heard a presentation on the downtown streetscape project and approved a contract for paving.</p>
<label for="s">Search</label><select name="s"><option>All years</option><option>2026</option></select>
<textarea name="c">type a comment</textarea><button type="submit">Go</button></div>
<footer>Copyright 2026 City of Example | Privacy policy</footer>
</form></body></html>"""


def test_a_webforms_page_inside_one_form_is_read_and_its_noise_still_dropped():
    title, md = extract.html_to_markdown(WEBFORMS, "https://example.test/MeetingDetail.aspx")
    assert "approved the annual budget" in md and "# Regular Council Meeting" in md
    for noise in ("Copyright", "Departments", "Calendar", "All years", "type a comment", "VIEWSTATE"):
        assert noise not in md, noise
    assert "approved a contract" in extract.main_text(WEBFORMS)


# 2 and 6 ------------------------------------------------------------- minutes: keys, re-derives, abbreviations, votes
MINUTES_TEXT = """City of Example Council Minutes, Monday, May 11th, 2026

Present: Mayor Jane Doe, Councilmember Bob Smith, Dr. Ann Lee and Mary Jones.

Councilmember Smith made a motion to approve Mr. Johnson's request for a variance at 12 St. Marys Rd. Councilmember Lee seconded. Motion carried unanimously.

Motion by Councilmember Lee to approve Ordinance No. 2026-15 for ABC Inc. and Dr. Phil Q. Jones, seconded by Councilmember Smith. Ayes: Smith, Lee, Jones. Nays: None. Public Comment - no.

Councilmember Jones moved to adjourn at 10:16 a.m. Councilmember Lee seconded. Motion carried."""


def test_motion_texts_are_not_cut_at_an_abbreviation():
    ms = minutes.parse_motions(MINUTES_TEXT)
    got = [m["motion"] for m in ms]
    assert got[0] == "approve Mr. Johnson's request for a variance at 12 St. Marys Rd"
    assert got[1] == "approve Ordinance No. 2026-15 for ABC Inc. and Dr. Phil Q. Jones"
    assert got[2] == "adjourn at 10:16 a.m."
    assert ms[1]["ordinance_ref"] == "Ordinance 2026-15"
    assert "Ann Lee" in ms[0]["present"]                                   # "Dr. Ann Lee" not cut at "Dr"
    for m in ms:                                                            # no agenda heading voted
        assert not any(re.search(r"comment|discussion|roll|call|public|safety", v["person"], re.I) for v in m["votes"])


def test_vote_words_and_agenda_headings_are_not_members_or_no_votes():
    text = ("Present: Mayor Jane Doe, Bob Smith, Ann Lee and Mary Jones.\n\nMotion by Smith to approve the budget, second by Lee. "
            "Roll call: Smith - yes, Lee - yes, Jones - Not Present, Doe - None. Public Comment - no. Discussion - no. "
            "Public Safety - no. Motion carried.")
    (m,) = minutes.parse_motions(text)
    votes = {v["person"]: v["vote"] for v in m["votes"]}
    assert votes.get("Bob Smith") == "yes" and votes.get("Ann Lee") == "yes"
    assert "no" not in votes.values()                                       # "Not Present" and "None" are not No
    assert not any(re.search(r"comment|discussion|safety|roll", p, re.I) for p in votes)


def _minutes_doc(text):
    return Record("town:site", "document", "https://example.test/minutes-2026-05-11.pdf", title="Council minutes May 11, 2026",
                  text=text, channel="pdf", meta={"doc_type": "minutes"})


def _derived(store):
    return store.query("source_id='town:site' AND channel='derived'")


def test_a_re_derive_after_ocr_shifted_the_paragraphs_leaves_no_copies(tmp_path):
    store = Store(tmp_path / "data")
    store.add(_minutes_doc(MINUTES_TEXT))
    # a record keyed by paragraph index, as version 2 stored them
    store.add(Record("town:site", "agenda_item", "https://example.test/minutes-2026-05-11.pdf", title="approve Mr", text="x",
                     channel="derived", key="town:site|minutes|https://example.test/minutes-2026-05-11.pdf|3|item"))
    derive.run(store, only={"minutes"}, log=lambda *a: None)
    first = sorted(r["key"] for r in _derived(store))
    assert len(first) == 6 and not any(k.endswith("|3|item") for k in first)        # 3 motions, item + vote each
    # OCR replaces the text: two more paragraphs on top shift every index; one motion read differently
    ocr = "Page 1\n\nCALL TO ORDER\n\n" + MINUTES_TEXT.replace("12 St. Marys Rd", "12 St. Marys Road")
    store.add(_minutes_doc(ocr))
    derive.run(store, only={"minutes"}, log=lambda *a: None, full=True)
    rows = _derived(store)
    assert len(rows) == 6
    assert len({r["title"] for r in rows if r["kind"] == "agenda_item"}) == 3
    assert any("Marys Road" in r["title"] for r in rows)
    assert len({r["key"] for r in rows} & set(first)) == 4                 # the two unchanged motions kept their keys
    store.close()


def test_a_motion_repeated_in_one_document_keeps_both():
    text = ("Present: Bob Smith, Ann Lee and Mary Jones.\n\nMotion by Smith to approve the minutes, second by Lee. Motion carried.\n\n"
            "Motion by Jones to approve the minutes, second by Lee. Motion carried.")
    ks = [m["mkey"] for m in minutes.parse_motions(text)]
    assert len(ks) == 2 and len(set(ks)) == 2 and ks[1] == ks[0] + "~2"


# 3 ------------------------------------------------------------------ a page from a feed and from the coverage crawl
def test_a_page_read_through_the_feed_and_the_coverage_crawl_is_one_record(site, env):
    now = dt.datetime.now(dt.timezone.utc)
    from email.utils import format_datetime

    site.routes["/news.rss"] = rss([("budget-hearing", "Budget hearing", format_datetime(now - dt.timedelta(days=1)))])
    site.routes["/n/budget-hearing"] = page
    site.routes["/"] = (200, "text/html", b'<html><body><main><p>' + b"Welcome to town. " * 30 +
                        b'</p><a href="/n/budget-hearing">Budget hearing</a></main></body></html>')
    src = {"id": "town:site", "url": site.base + "/", "channel": "feed", "endpoint": site.base + "/news.rss", "coverage": True,
           "tier": "a"}
    env.run(src)
    env.run(src, force=True)
    rows = [r for r in env.store.query("source_id='town:site' AND url LIKE '%budget-hearing'") if r["kind"] != "feed_item"]
    assert len(rows) == 1, [r["channel"] for r in rows]
    assert rows[0]["meta"].get("feed_guid") == "budget-hearing" or rows[0]["channel"] == "html"


def test_pages_stored_under_a_guid_are_filed_under_their_address_once(tmp_path):
    store = Store(tmp_path / "data")
    u = "https://town.test/news/budget-hearing"
    a = Record("town:site", "page", u, title="Budget hearing", text="feed copy", channel="feed", meta={"guid": "g-1"})
    b = Record("town:site", "page", u, title="Budget hearing", text="crawl copy", channel="html", meta={"opened": True})
    c = Record("town:site", "page", "https://town.test/news/other", title="Other", text="only via the feed", channel="feed",
               meta={"guid": "g-2"})
    w = Record("town:site", "page", "https://town.test/?p=5", title="Post", text="post", channel="wp_json", meta={"wp_id": 5})
    for r in (a, b, c, w):
        store.add(r)
    assert a.natural_key().endswith("|g-1") and b.natural_key().endswith("|")
    out = store.rekey_feed_pages()
    assert out == {"rekeyed": 2, "dropped": 1}
    rows = store.query("source_id='town:site'")
    assert sorted(r["url"] for r in rows) == sorted([u, "https://town.test/news/other", "https://town.test/?p=5"])
    assert all(not r["meta"].get("guid") and not r["meta"].get("wp_id") for r in rows)
    other = next(r for r in rows if r["url"].endswith("other"))
    assert other["id"] == Record("town:site", "page", "https://town.test/news/other").stable_id
    assert store.rekey_feed_pages() == {"rekeyed": 0, "dropped": 0}
    store.close()


# 4 ------------------------------------------------------------------ an oversized file is not fetched every run
def test_a_file_too_large_is_fresh_for_a_month(tmp_path):
    from gacrawl import store as store_mod

    store = Store(tmp_path / "data")
    u = "https://town.test/huge.pdf"
    store.mark_url(u, 413, "town:site", "too large")
    assert store.fresh(u, 3650)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=store_mod.TOO_LARGE_RETRY_DAYS + 1)).isoformat()
    store.db.execute("UPDATE urls SET fetched_at=? WHERE url=?", (old, u))
    assert not store.fresh(u, 3650)                                         # asked again after the retry period
    store.mark_url(u, 404, "town:site")
    assert not store.fresh(u, 3650)
    store.close()


# 5 ------------------------------------------------------------------ an election report whose headings follow their tables
# South Fulton's Dec. 2, 2025 runoff report as its PDF text comes out: each contest's table, its "Times Cast",
# "Undervotes" and "Precincts Reported" lines, and only then its heading. Read headings-first, Gumbs and Baker (the
# mayoral runoff) were filed under "Sandy Springs City Council D4" and the next contest's pair under "South Fulton Mayor".
TRAILING = """Election Summary Report
Runoff Election
December 02, 2025
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Dontaye Carter 2,582 1,959 27 0 4,568 30.73%
Rusty Paul (I) 4,997 5,263 38 0 10,298 69.27%
Total Votes 7,579 7,222 65 0 14,866
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 7,585 7,228 65 0 14,878 / 67,044 22.19%
Undervotes 6 6 0 0 12
Precincts Reported: 46 of 46 (100.00%)
Sandy Springs Mayor (Vote for 1)
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Mark Baker 2,803 2,578 13 0 5,394 40.25%
Carmalitha L. Gumbs 3,996 3,997 14 0 8,007 59.75%
Total Votes 6,799 6,575 27 0 13,401
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 6,827 6,590 27 0 13,444 / 81,436 16.51%
Undervotes 28 15 0 0 43
Precincts Reported: 65 of 65 (100.00%)
South Fulton Mayor (Vote for 1)
12/2/2025 11:46:45 PM Page: 4 of 5
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Aaron Johnson 756 879 3 0 1,638 55.28%
Stephanie G. Johnson 675 650 0 0 1,325 44.72%
Total Votes 1,431 1,529 3 0 2,963
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1,448 1,542 3 0 2,993 / 12,799 23.38%
Undervotes 17 13 0 0 30
Precincts Reported: 6 of 6 (100.00%)
South Fulton City Council D2 (Vote for 1)
"""


def _contests(text):
    return {c["office"]: [x["name"] for x in c["candidates"]] for c in parse_results(text)}


def test_a_report_that_prints_each_heading_after_its_table_is_read_in_that_order():
    want = {"Sandy Springs Mayor": ["Dontaye Carter", "Rusty Paul"], "South Fulton Mayor": ["Mark Baker", "Carmalitha L. Gumbs"],
            "South Fulton City Council D2": ["Aaron Johnson", "Stephanie G. Johnson"]}
    assert _contests(TRAILING) == want
    assert _contests(re.sub(r"\s+", " ", TRAILING)) == want                 # as the store keeps it: one long line
    sf = next(c for c in parse_results(TRAILING) if c["office"] == "South Fulton Mayor")
    assert sf["total"] == 13401 and {x["name"]: x["outcome"] for x in sf["candidates"]}["Carmalitha L. Gumbs"] == "won"


def test_a_report_whose_alignment_cannot_be_told_is_not_read():
    # half its contests print the heading first (its block lines after it), half after: neither order can be trusted
    lead = ("Clerk of Superior Court - Rep (Vote for 1)\nPrecincts Reported: 33 of 33 (100.00%)\nTimes Cast 100 100 0 0 200 / 900 22.22%\n"
            "Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total\n"
            "Charles Baker 60 50 0 0 110 55.00%\nJane Roe 40 50 0 0 90 45.00%\nTotal Votes 100 100 0 0 200\n")
    trail = ("Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total\n"
             "Gerald Couch (I) 70 60 0 0 130 65.00%\nJim Hammock, Jr. 30 40 0 0 70 35.00%\nTotal Votes 100 100 0 0 200\n"
             "Times Cast 100 100 0 0 200 / 900 22.22%\nPrecincts Reported: 33 of 33 (100.00%)\nSheriff - Rep (Vote for 1)\n")
    text = "Election Summary Report\nGeneral Primary\nMay 21, 2024\n" + lead + trail + lead.replace("Clerk of Superior Court", "Coroner") + \
        trail.replace("Sheriff", "Tax Commissioner")
    assert parse_summary_report(text) == []


def test_a_report_whose_parties_contradict_its_headings_is_not_read():
    text = "\n".join(["Election Summary Report", "General Primary", "May 21, 2024",
                      "Sheriff - Dem (Vote for 1)", "Ann Able (Rep) 10 10 0 0 20 100.00%", "Total Votes 10 10 0 0 20",
                      "Coroner - Dem (Vote for 1)", "Bob Baker (Rep) 10 10 0 0 20 100.00%", "Total Votes 10 10 0 0 20",
                      "Clerk - Rep (Vote for 1)", "Cy Cole (Dem) 10 10 0 0 20 100.00%", "Total Votes 10 10 0 0 20"])
    assert parse_summary_report(text) == []
    assert len(parse_summary_report(text.replace("(Rep)", "(Dem)").replace("Clerk - Rep", "Clerk - Dem"))) == 3


def test_a_document_whose_contests_are_no_longer_read_loses_them(tmp_path):
    store = Store(tmp_path / "data")
    url = "https://town.test/DocumentCenter/View/14916"
    store.add(Record("co-fulton:elections", "document", url, title="Election results", text=TRAILING, channel="pdf"))
    store.add(Record("co-fulton:elections", "election_result", url, title="Sandy Springs City Council D4 — 2025-12-02",
                     channel="derived", key=f"co-fulton:elections|election_result|{url}|2025-12-02|sandy springs city council d4"))
    derive.run(store, only={"elections"}, context={"jurisdictions": []}, log=lambda *a: None)
    offices = sorted(r["meta"]["office"] for r in store.query("kind='election_result'"))
    assert offices == ["Sandy Springs Mayor", "South Fulton City Council D2", "South Fulton Mayor"]
    store.close()


# 7 ------------------------------------------------------------------ people's names and e-mail addresses
NOT_PEOPLE = ["Dear Hornet Family", "Valdosta State University", "Administrative Assistant", "REQUEST TO APPEAR BEFORE",
              "Woods to share their", "Mike Blackerby is in", "Mychele Rhodes was recognized", "Robert Griffin and Administrative",
              "Atlanta Children", "To Date With Your", "To Citizen", "Quick Links", "Hiring New"]
PEOPLE = ["Gerald W. Couch", "Jane Q. Doe", "Bárbara Rivera Holmes", "D'Jaris Glasco", "Karen McNeal", "Leslie Manahan",
          "JOHN SMITH", "Maria de la Cruz", "Robert Griffin, Jr.", "Dr. Ann Lee", "Will Read"]


def test_page_phrases_and_the_places_own_name_are_not_people():
    assert [n for n in NOT_PEOPLE if plausible_person(n)] == []
    assert [n for n in PEOPLE if not plausible_person(n)] == []
    assert not plausible_person("West Point", "West Point") and not plausible_person("Sugar Hill", "City of Sugar Hill")
    assert plausible_person("Tom Hill", "Sugar Hill")


def test_school_and_police_readers_use_the_name_check():
    from gacrawl import police, schools

    assert schools.superintendent_from_text("Dear Hornet Family,\nSuperintendent Valdosta State University graduate") is None
    assert schools.superintendent_from_text("Superintendent: Dr. Jane Doe") == "Jane Doe"
    assert police.find_chief(["Sheriff", "To Date With Your"]) is None
    assert police.find_chief(["Chief of Police Quick Links"]) is None
    assert police.find_chief(["Sheriff Mike Jolley"])["name"] == "Mike Jolley"
    assert not police._name_ok("West Point", "West Point")


def test_names_from_an_archive_copy_of_another_site_are_not_believed():
    assert own_page("https://www.dekalbschoolsga.org/superintendent/", "https://www.dekalbschoolsga.org/")
    assert own_page("https://web.archive.org/web/2024/https://www.dekalbschoolsga.org/superintendent/", "https://dekalbschoolsga.org")
    assert not own_page("https://web.archive.org/web/2024/https://www.ajc.com/news/atlanta-children/", "https://dekalbschoolsga.org")


def test_school_people_from_an_archived_news_page_are_left_out(tmp_path, monkeypatch):
    from gacrawl import schools

    monkeypatch.setattr(schools, "school_districts", lambda root: [{"id": "sd-dekalb", "name": "DeKalb County Schools",
                                                                    "website": "https://www.dekalbschoolsga.org/"}])
    monkeypatch.setattr(schools, "load_registry", lambda root: {"districts": {}, "schools": []})
    monkeypatch.setattr(schools, "_link_electo", lambda root, people: None)
    store = Store(tmp_path / "data")
    text = "Our district\nSuperintendent: Dr. Devon Horton\nWelcome to the district office and its staff pages."
    store.add(Record("sd-dekalb:site", "page", "https://www.dekalbschoolsga.org/superintendent/", title="Superintendent", text=text))
    store.add(Record("sd-dekalb:site", "page", "https://web.archive.org/web/2023/https://www.ajc.com/superintendent-news/",
                     title="Superintendent news", text="Superintendent news\nSuperintendent: Mary Wrong\nAs reported by the newspaper on the day."))
    schools.people(tmp_path, store, cfg={}, log=lambda *a: None)
    people = json.loads((tmp_path / "state" / "school_people.json").read_text())["people"]
    assert [p["name"] for p in people] == ["Devon Horton"]
    store.close()


def test_contact_addresses_are_cleaned_and_placeholders_dropped():
    assert clean_email("%20jastevens@fayettecountyga.gov") == "jastevens@fayettecountyga.gov"
    assert clean_email("//openrecords@carrollcountyga.gov") == "openrecords@carrollcountyga.gov"
    assert clean_email("u003eclerkofsuperior@glynncounty-ga.gov") == "clerkofsuperior@glynncounty-ga.gov"
    assert clean_email("mailto:Clerk@CityOfX.ga.us?subject=hi") == "clerk@cityofx.ga.us"
    for bad in ("test@test.gov", "citymanager@test.gov", "noreply@co.walla-walla.wa.us", "records@county.fl.us", "not an address"):
        assert clean_email(bad) == "", bad


def test_the_records_contact_reader_cleans_addresses():
    from types import SimpleNamespace

    from gacrawl.seed import find_records_contact

    home = '<html><body><a href="/open-records">Open Records Request</a></body></html>'
    rec_page = ('<html><body><a href="mailto:%20openrecords@townga.gov">Records</a> <a href="mailto:test@test.gov">x</a> '
                "<p>Write //clerk@townga.gov or noreply@co.walla-walla.wa.us</p></body></html>")

    class F:
        def get(self, url, **k):
            return SimpleNamespace(status=200, content_type="text/html", text=rec_page, url=url)

    rc = find_records_contact(F(), home, "https://www.townga.gov/")
    assert rc["emails"] == ["clerk@townga.gov", "openrecords@townga.gov"] or set(rc["emails"]) == {"clerk@townga.gov", "openrecords@townga.gov"}


# 8 ------------------------------------------------------------------ a date from the page's words decades before it
def test_a_date_read_from_the_text_long_before_the_fetch_is_not_the_pages():
    html = "<html><body><p>On September 11, 2001 the nation changed. Today the senator spoke.</p></body></html>"
    assert extract.page_date(html, fetched="2026-11-01") is None
    assert extract.page_date(html, fetched="2012-01-01") == "2001-09-11"
    meta = '<html><head><meta property="article:published_time" content="1956-01-12"></head><body>x</body></html>'
    assert extract.page_date(meta) == "1956-01-12"                         # from the page's metadata: believed
    assert extract.page_date("<p>Posted October 3, 2026</p>", fetched="2026-11-01") == "2026-10-03"


# Hall County's runoff of June 18, 2024 and South Fulton's results page of Nov. 5, 2025 (pages 28-29 of 30), checked
# against their PDFs on Nov 19, 2026: the same layout (each heading after its table), Hall's with the party code on the
# heading's line. Read headings-first they put Higgins and Gibbs under "State Court Judge", and Vince R. Williams (Union
# City's mayor) under "South Fulton City Council D6".
HALL_RUNOFF = """Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
David Gibbs 2,890 1,463 258 2 4,613 53.54%
Richard P. Higgins (I) 2,080 1,529 394 0 4,003 46.46%
Total Votes 4,970 2,992 652 2 8,616
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1 1 1 1 1 / 0 N/A
Precincts Reported: 31 of 31 (100.00%)
Hall County Board of Commissioners Chair - Rep (Vote for 1) REP
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Andy Maddox 2,042 1,426 487 1 3,956 39.92%
John "Tripp" Wingate, III 3,462 2,049 442 1 5,954 60.08%
Total Votes 5,504 3,475 929 2 9,910
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1 1 1 1 1 / 0 N/A
Precincts Reported: 31 of 31 (100.00%)
State Court Judge (Breakfield) (Vote for 1) NP
Precincts Reported: 31 of 31 (100.00%)
Registered Voters: 9,983 of 0 (N/A)
Ballots Cast: 9,983
Election Summary Report
Hall
General Primary and Nonpartisan General Runoff
Official & Complete
June 18, 2024
Closed Primary
6/24/2024 12:02:28 PM Page: 1 of 1"""

SOUTH_FULTON_GENERAL = """Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Joseph Adeyemi 795 967 15 0 1,777 6.63%
Mark Baker 2,874 2,291 12 0 5,177 19.32%
Kelvin Davis 429 339 2 0 770 2.87%
William "Bill" Edwards 1,736 2,679 28 0 4,443 16.58%
Carmalitha L. Gumbs 4,566 5,918 49 0 10,533 39.30%
Jewel A. Johnson 490 650 6 0 1,146 4.28%
Khalid "Kobi" Kamau (I) 665 599 4 0 1,268 4.73%
Ray Mills 867 465 10 0 1,342 5.01%
Ryan Olson 172 173 1 0 346 1.29%
Total Votes 12,594 14,081 127 0 26,802
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1 1 1 1 1 / 0 N/A
Precincts Reported: 31 of 31 (100.00%)
South Fulton Mayor (Vote for 1)
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Ken Austin 95 127 1 0 223 4.36%
Araba Dowell 257 253 0 0 510 9.98%
D'Jaris Glasco 93 96 1 0 190 3.72%
Aaron Johnson 668 969 10 0 1,647 32.24%
Stephanie G. Johnson 650 670 6 0 1,326 25.95%
Peni Webster Lewis 162 289 1 0 452 8.85%
Connie Robinson 115 214 0 0 329 6.44%
Joy Treadwell 200 229 3 0 432 8.46%
Total Votes 2,240 2,847 22 0 5,109
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1 1 1 1 1 / 0 N/A
Precincts Reported: 31 of 31 (100.00%)
South Fulton City Council D2 (Vote for 1)
11/5/2025 12:15:11 AM Page: 28 of 30
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Mike "MJ" Johnson 258 209 3 0 470 11.21%
Mel Keyton 218 311 0 0 529 12.61%
Kenya Jordan Moore 824 680 2 0 1,506 35.91%
Jaceey Sebastian (I) 648 1,027 14 0 1,689 40.27%
Total Votes 1,948 2,227 19 0 4,194
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1 1 1 1 1 / 0 N/A
Precincts Reported: 31 of 31 (100.00%)
South Fulton City Council D4 (Vote for 1)
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Tashima Freeman 828 511 6 0 1,345 42.98%
Natasha Williams (I) 798 972 14 0 1,784 57.02%
Total Votes 1,626 1,483 20 0 3,129
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1 1 1 1 1 / 0 N/A
Precincts Reported: 31 of 31 (100.00%)
South Fulton City Council D6 (Vote for 1)
Candidate Party Election Day Advance Voting Absentee by Mail Provisional Total
Tom Anderson, III 217 192 3 0 412 10.60%
Keith A. Skippings 550 489 4 0 1,043 26.85%
Vince R. Williams (I) 1,182 1,235 13 0 2,430 62.55%
Total Votes 1,949 1,916 20 0 3,885
Election Day Advance Voting Absentee by Mail Provisional Total
Unresolved Write-In 0 0 0 0 0
Election Day Advance Voti Absentee by Provisional Total
Times Cast 1 1 1 1 1 / 0 N/A
Precincts Reported: 31 of 31 (100.00%)
Union City Mayor (Vote for 1)
11/5/2025 12:15:11 AM Page: 29 of 30"""


def test_the_hall_and_south_fulton_reports_as_their_pdfs_print_them():
    for text in (HALL_RUNOFF, re.sub(r"\s+", " ", HALL_RUNOFF)):
        assert _contests(text) == {"Hall County Board of Commissioners Chair - Rep": ["David Gibbs", "Richard P. Higgins"],
                                   "State Court Judge (Breakfield)": ["Andy Maddox", 'John "Tripp" Wingate, III']}
    for text in (SOUTH_FULTON_GENERAL, re.sub(r"\s+", " ", SOUTH_FULTON_GENERAL)):
        got = _contests(text)
        assert got["Union City Mayor"] == ["Tom Anderson, III", "Keith A. Skippings", "Vince R. Williams"]
        assert got["South Fulton City Council D6"] == ["Tashima Freeman", "Natasha Williams"]
        assert "Carmalitha L. Gumbs" in got["South Fulton Mayor"] and "Aaron Johnson" in got["South Fulton City Council D2"]
