"""School boards' Simbli pages from the Internet Archive's existing captures (gacrawl/simbli_archive.py). A made-up
archive: the capture index and the captured pages, laid out as the Archive's copies of Baldwin County's listing
(S=4007, Sept. 9, 2024) show them. Simbli itself is never asked, nor the Archive asked to capture anything."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from gacrawl import simbli_archive as sa
from gacrawl.store import Store

LISTING = "https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?S=4007"
POLICIES = "https://simbli.eboardsolutions.com/Policy/PolicyListing.aspx?S=4007"
VIEW = "https://simbli.eboardsolutions.com/SB_Meetings/ViewMeeting.aspx?S=4007&MID=88001"
# Simbli's own furniture, worded as on the captured pages (far more of it than of a meeting)
PAD = ("<div><b>How May We Help You Today?</b> Visit the Simbli Help Desk for all your training and support needs. "
       + "Learning Center: build skills with helpful training courses. Knowledge Base. Get Support. Want to Chat? " * 40
       + "</div><div>Session Timeout Notification. Are you still there? eBOARDsolutions</div>")


def listing_html():
    rows = [("09/10/2024 - 05:30 PM", "Board Agenda", "UnPublished", "A-I. Board of Education Meetings", 88002),
            ("07/23/2024 - 05:30 PM", "Board Agenda", "Published", "A-I. Board of Education Meetings", 88001),
            ("07/02/2024 - 05:30 PM", "Called Board Meeting", "UnPublished", "A-III. Called Board Meetings", None)]
    trs = "".join(
        f'<tr onclick="OpenMeeting(\'S=4007&MID={m}\')"><td>{d}</td><td>'
        + (f'<a href="/web/20240909160017/{VIEW.replace("88001", str(m))}">{t}</a>' if m else t)
        + f"</td><td>{pub}</td><td>{ty}</td></tr>" for d, t, pub, ty, m in rows)
    return (f"<html><head><title>Meetings Listing: eBoardsolutions</title></head><body>{PAD}"
            f"<table><tr><th>Date - Time</th><th>Meeting Title</th><th>Minutes</th><th>Meeting Type</th></tr>{trs}</table></body></html>")


def policies_html():
    return (f"<html><body>{PAD}<table>"
            '<tr><td>BCBI</td><td><a href="/web/20240806231215/https://simbli.eboardsolutions.com/Policy/ViewPolicy.aspx?S=4007&revid=1">'
            "Rules of Conduct for Board Meetings</a></td></tr>"
            '<tr><td><a href="/web/20240806231215/https://simbli.eboardsolutions.com/Policy/ViewPolicy.aspx?S=4007&revid=2">'
            "JCDAG - Weapons</a></td></tr></table></body></html>")


CHALLENGE = "<html><body>Request unsuccessful. Incapsula incident ID: 1234</body></html>"
MEETING_PAGE = f"<html><body>{PAD}<h1>Board Agenda July 23, 2024</h1><p>{'Motion to approve the budget, carried 5-0. ' * 20}</p></body></html>"


class Archive:
    """The capture index and captured pages; counts what was asked, and of whom."""

    def __init__(self, captures, pages, refuse_after=None):
        self.captures, self.pages, self.asked, self.refuse_after = captures, pages, [], refuse_after

    def get(self, url, params=None, **kw):
        self.asked.append(url)
        if self.refuse_after is not None and len(self.asked) > self.refuse_after:
            return SimpleNamespace(status=429, text="", content_type="text/plain", json=lambda: None)
        assert urlsplit(url).netloc == "web.archive.org", url          # never Simbli itself
        assert "/save/" not in url                                    # never Save Page Now
        if url == sa.CDX:
            q = dict(params)
            rows = [["timestamp", "original", "length"]] + [[ts, q["url"], str(n)] for ts, n in self.captures.get(q["url"], [])]
            return SimpleNamespace(status=200, text=json.dumps(rows), content_type="application/json", json=lambda: rows)
        body = self.pages.get(url)
        return SimpleNamespace(status=200 if body else 404, text=body or "", content_type="text/html")


def fixtures():
    caps = {LISTING: [("20240909160017", 459305), ("20250108144444", 1331)],     # the second: the challenge (1.3 KB)
            POLICIES: [("20240806231215", 434325)], VIEW: [("20240801010101", 52000)]}
    pages = {sa.RAW.format(ts="20240909160017", url=LISTING): listing_html(),
             sa.RAW.format(ts="20240806231215", url=POLICIES): policies_html(),
             sa.RAW.format(ts="20240801010101", url=VIEW): MEETING_PAGE}
    return caps, pages


def test_meetings_policies_and_a_meetings_page_from_the_archive_copies(tmp_path):
    caps, pages = fixtures()
    store = Store(tmp_path / "data")
    arc = Archive(caps, pages)
    out = sa.SimbliArchive(arc, store, log=lambda *a: None).run({"sd-baldwin": "4007"})
    assert out["districts"] == 1 and not out["archive_off"]
    ms = {r["title"] + " " + r["published"][:10]: r for r in store.read("sd-baldwin:simbli", "meeting")}
    assert set(ms) == {"Board Agenda 2024-09-10", "Board Agenda 2024-07-23", "Called Board Meeting 2024-07-02"}
    m = ms["Board Agenda 2024-07-23"]
    assert m["jurisdiction"] == "sd-baldwin" and m["url"].startswith("https://web.archive.org/web/20240909160017/")
    assert m["meta"]["archived"] and m["meta"]["captured"] == "2024-09-09" and m["meta"]["original"] == VIEW
    assert m["meta"]["MID"] == "88001" and m["meta"]["EventBodyName"] == "Board of Education" and m["meta"]["minutes_status"] == "Published"
    assert ms["Called Board Meeting 2024-07-02"]["meta"]["EventBodyName"] == "Called Board"
    pol = {r["meta"]["code"]: r for r in store.read("sd-baldwin:simbli", "policy")}
    assert set(pol) == {"BCBI", "JCDAG"} and pol["JCDAG"]["title"] == "Weapons"
    assert pol["BCBI"]["meta"]["original"] == "https://simbli.eboardsolutions.com/Policy/ViewPolicy.aspx?S=4007&revid=1"
    docs = store.read("sd-baldwin:simbli", "document")
    assert len(docs) == 1 and docs[0]["meta"]["doc_type"] == "minutes" and "approve the budget" in docs[0]["text"]
    # the challenge capture is never even read: its stored size says what it is
    assert sa.RAW.format(ts="20250108144444", url=LISTING) not in arc.asked
    # read again only after RECHECK_DAYS
    n = len(arc.asked)
    sa.SimbliArchive(arc, store, log=lambda *a: None).run({"sd-baldwin": "4007"})
    assert len(arc.asked) == n
    store.close()


def test_a_capture_that_is_the_challenge_page_is_not_content():
    assert not sa.is_real(CHALLENGE) and not sa.is_real(CHALLENGE + "x" * 5000)
    assert sa.is_real(listing_html())


def test_the_archive_refusing_stops_the_run_and_leaves_the_district_due(tmp_path):
    caps, pages = fixtures()
    store = Store(tmp_path / "data")
    arc = Archive(caps, pages, refuse_after=1)
    out = sa.SimbliArchive(arc, store, log=lambda *a: None).run({"sd-baldwin": "4007", "sd-banks": "4008"})
    assert out["archive_off"] and store.kv_get("simbli:archive:4007") is None
    assert len(arc.asked) == 2                       # nothing after the 429
    store.close()


def test_site_numbers_come_from_the_registrys_simbli_links():
    J = [{"id": "sd-baldwin", "kind": "school_district", "portals": {"simbli": "https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?S=4007"}},
         {"id": "sd-appling", "kind": "school_district", "portals": {"simbli": "https://simbli.eboardsolutions.com/index.aspx?s=4002"}},
         {"id": "co-x", "kind": "county", "portals": {"simbli": "https://simbli.eboardsolutions.com/index.aspx?s=9"}}]
    assert sa.site_ids(J) == {"sd-baldwin": "4007", "sd-appling": "4002"}
