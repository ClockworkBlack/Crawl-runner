"""The one strict format of state_meetings.json (gacrawl/agencies.py STATE_MEETINGS_SCHEMA): what the reader stores
is exported in it, the validator leaves out what does not fit and says so, a site read from the Internet Archive says
so with the copy's day, and the same file passes Electo's own importer where its code is at hand."""
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gacrawl import agencies as A
from gacrawl.store import Record, Store

FX = Path(__file__).parent / "fixtures" / "agencies"
ROOT = Path(__file__).resolve().parents[1]
PAGES = {"https://www.itsmarta.com/meeting-schedule.aspx": "marta_meeting-schedule.html",
         "https://www.itsmarta.com/board-documents.aspx": "marta_board-documents.html",
         "https://www.jekyllisland.com/authority/board-directors/": "jekyll_board.html",
         "https://gsfic.georgia.gov/board-meeting-minutes-0": "ghefa_minutes.html", "https://gsfic.georgia.gov/past-events": "gsfic_past.html",
         "https://gsfic.georgia.gov/events/2026-05-26/ghefa-board-meeting": "ev_2026-05-26_ghefa-board-meeting.html",
         "https://gtea.ga.gov/board/": "gtea_board.html", "https://gaports.com/organization/meeting-notice-agenda/": "ports.html"}
ARCHIVED = {"https://www.gwcca.org/about": ("gwcca_wayback.html", "20260501095954"),
            "https://atltransit.ga.gov/the-atl-board/": ("atl_board_wayback.html", "20260516101010")}


class Fetcher:
    def __init__(self):
        self.asked = []

    def get(self, url, **k):
        self.asked.append(url)
        f = PAGES.get(url)
        t = (FX / f).read_text() if f else ""
        return SimpleNamespace(status=200 if f else 404, text=t, url=url, content_type="text/html", content=t.encode())


@pytest.fixture()
def read(tmp_path, monkeypatch):
    """The reader run over the saved pages into a scratch store; the archive answers only from its saved copies."""
    import gacrawl.wayback as W

    monkeypatch.setattr(W, "Wayback", lambda *a, **k: SimpleNamespace(off=""))
    monkeypatch.setattr(A, "read_archived", lambda wb, u, copies=4: ((FX / ARCHIVED[u][0]).read_text(), ARCHIVED[u][1]) if u in ARCHIVED else (None, ""))
    store, f = Store(tmp_path / "data"), Fetcher()
    res = A.run(f, store, ROOT, only=["marta", "jekyll-island", "ghefa", "atl-transit", "ports", "gwcca"], log=lambda *a: None, minutes_cap=0)
    out = tmp_path / "state_meetings.json"
    counts = A.export_state_meetings(store, ROOT, out, today="2026-11-20", log=lambda *a: None)
    yield SimpleNamespace(store=store, fetcher=f, res=res, counts=counts, data=json.loads(out.read_text()), out=out, tmp=tmp_path)
    store.close()


def body(data, bid):
    return next(b for b in data["bodies"] if b["id"] == bid)


def test_the_export_is_the_strict_format(read):
    d = read.data
    assert list(d) == ["as_of", "format", "not_read", "note", "bodies", "legislature"] and d["format"] == 2
    assert [b["id"] for b in d["bodies"]] == [b["id"] for b in sorted(d["bodies"], key=lambda b: (A.KINDS.index(b["kind"]), b["name"].casefold(), b["id"]))]
    assert {b["id"] for b in d["bodies"]} == {a["id"] for a in A.load(ROOT)}             # every listed body, read or not
    assert read.counts["left_out"] == 0 and "warning" not in read.counts
    n = 0
    for b in d["bodies"]:
        assert A.validate_body(b) == [], (b["id"], A.validate_body(b))
        rows = b["meetings"]
        assert rows == sorted(rows, key=lambda r: (r["d"], r.get("t", ""), r.get("b", "")), reverse=True)
        assert len({(r.get("b", ""), r["d"], r.get("t", "")) for r in rows}) == len(rows)       # one row per body + committee + day + hour
        for r in rows:
            n += 1
            assert A.validate_meeting(r) == [], (b["id"], r, A.validate_meeting(r))
            assert list(r) == [k for k in A.MEETING_KEYS if k in r] and all(v != "" for v in r.values())
            assert r["upd"] >= r["seen"] and r["seen"].endswith("Z") and r["p"].startswith("http")
    assert n > 150


def test_what_each_body_exports(read):
    marta = body(read.data, "marta")
    assert marta["kind"] == "transit" and marta["source"] == "live" and "archive_as_of" not in marta
    rows = {(r["d"], r.get("b", ""), r.get("t", "")): r for r in marta["meetings"]}
    assert rows[("2026-12-03", "", "13:30")]["st"] == "scheduled" and rows[("2026-12-03", "", "12:00")]["n"] == "Board Work Session"
    aud = rows[("2026-11-20", "Audit Committee", "10:00")]
    assert aud["a"].endswith("Agenda%20Audit%2011202026.pdf") and " " not in aud["a"]
    assert rows[("2026-08-20", "Audit Committee", "10:00")]["st"] == "cancelled"
    oct8 = rows[("2026-10-08", "", "13:30")]
    assert "Approved%20Minutes" in oct8["m"] and "Summary" in oct8["s"] and oct8["st"] == "held"
    jek = {r["d"]: r for r in body(read.data, "jekyll-island")["meetings"]}
    assert jek["2026-12-15"]["st"] == "cancelled" and jek["2026-06-02"]["b"] == "Finance Committee" and jek["2026-10-20"]["m"].startswith("https://acrobat.adobe.com/")
    gh = body(read.data, "ghefa")
    assert [r["d"] for r in gh["meetings"]] == ["2026-05-26", "2025-12-17", "2025-10-30", "2025-09-19", "2025-06-30"]
    may = gh["meetings"][0]
    assert may["t"] == "16:30" and may["s"].endswith("summary-may-26-2026/download") and may["w"].startswith("270 Washington") and may["x"][0]["l"]
    assert gh["links"][0]["u"].startswith("https://simbli.eboardsolutions.com/") and list(gh["links"][0]) == ["l", "u"]
    assert "https://gsfic.georgia.gov/events/2026-12-09/gsfic-commission-meeting" not in read.out.read_text()
    gtea = body(read.data, "atl-transit")
    assert gtea["kind"] == "authority" and gtea["source"] == "live"
    assert any(r["st"] == "tentative" for r in gtea["meetings"]) and any(r.get("b") == "Transit Operations Committee" for r in gtea["meetings"])


def test_a_site_that_refuses_readers_is_read_from_the_archive_and_says_so(read):
    gw = body(read.data, "gwcca")
    assert gw["source"] == "archive" and gw["archive_as_of"] == "2026-05-01" and gw["pages"] == ["https://www.gwcca.org/about"]
    assert gw["meetings"] and all(r["p"] == "https://web.archive.org/web/20260501095954/https://www.gwcca.org/about" for r in gw["meetings"])
    assert not any("gwcca.org" in u or "web.archive.org/save" in u for u in read.fetcher.asked)       # the live site is never asked; nothing is saved
    # the ATL's dead page: its own rows carry the archive copy, the body stays live (the GTEA's page is)
    atl = [r for r in body(read.data, "atl-transit")["meetings"] if r["d"] == "2025-12-04" and "b" not in r][0]
    assert atl["p"].startswith("https://web.archive.org/web/20260516101010/") and "w" not in atl
    # looked for again only after recheck_days; once: true never again
    f2 = Fetcher()
    A.read_archived = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the archive was asked again the same day"))
    A.run(f2, read.store, ROOT, only=["gwcca", "atl-transit"], log=lambda *a: None, minutes_cap=0)
    out2 = read.tmp / "again.json"
    A.export_state_meetings(read.store, ROOT, out2, today="2026-11-21", log=lambda *a: None)
    again = json.loads(out2.read_text())
    assert body(again, "gwcca")["archive_as_of"] == "2026-05-01" and len(body(again, "gwcca")["meetings"]) == len(gw["meetings"])


def test_a_row_that_does_not_fit_is_left_out_and_counted(read, tmp_path):
    store = read.store
    store.add(Record("ga-agency:ports", "meeting", "https://gaports.com/x#1", title="x", text="x", published="2026-13-45", key="ga-agency:ports|meeting|bad-day",
                     meta={"fmt": 2, "EventDate": "2026-13-45", "found_on": "https://gaports.com/x"}))
    store.add(Record("ga-agency:ports", "meeting", "https://gaports.com/x#2", title="y", text="y", published="2026-10-01", key="ga-agency:ports|meeting|bad-hour",
                     meta={"fmt": 2, "EventDate": "2026-10-01", "EventTime": "9:30 am", "found_on": "https://gaports.com/x"}))
    store.add(Record("ga-agency:ports", "meeting", "https://gaports.com/x#3", title="z", text="z", published="2026-10-02", key="ga-agency:ports|meeting|no-page",
                     meta={"fmt": 2, "EventDate": "2026-10-02", "EventAgendaFile": "ftp://nowhere/agenda.pdf", "found_on": ""}))
    out = tmp_path / "s.json"
    said = []
    counts = A.export_state_meetings(store, ROOT, out, today="2026-11-20", log=said.append)
    assert counts["left_out"] == 3 and "3 rows did not fit" in counts["warning"] and said and "WARNING" in said[0]
    ports = body(json.loads(out.read_text()), "ports")
    assert len(ports["meetings"]) == 10 and all(A.validate_meeting(r) == [] for r in ports["meetings"])
    assert A.validate_meeting({"d": "2026-10-01", "st": "held", "p": "https://x.org/", "seen": "2026-10-01T00:00:00Z", "upd": "2026-10-01T00:00:00Z", "n": ""})
    assert A.validate_meeting({"st": "held", "d": "2026-10-01", "p": "https://x.org/", "seen": "2026-10-01T00:00:00Z", "upd": "2026-10-01T00:00:00Z"}) == ["keys out of order"]
    assert A.validate_meeting({"d": "2026-10-01", "st": "Held", "p": "https://x.org/", "seen": "2026-10-01T00:00:00Z", "upd": "2026-10-01T00:00:00Z"}) == ["st is not a status"]


def test_records_from_before_the_format_are_replaced_or_read_by_the_same_rules(read, tmp_path):
    store = read.store
    old = {"agency": "marta", "EventDate": "2026-11-19", "EventAgendaFile": "https://www.itsmarta.com/a.pdf", "EventMinutesFile": "", "docs": [], "what": "",
           "location": "", "found_on": "https://www.itsmarta.com/board-documents.aspx"}
    store.add(Record("ga-agency:marta", "meeting", "https://www.itsmarta.com/board-documents.aspx#meeting-2026-11-19", title="MARTA 2026-11-19", text="old",
                     published="2026-11-19", key="ga-agency:marta|meeting|2026-11-19", meta=old))
    store.add(Record("ga-agency:regents", "meeting", "https://www.usg.edu/regents/meetings/#meeting-2026-09-16", title="Regents", text="old", published="2026-09-16",
                     key="ga-agency:regents|meeting|2026-09-16",
                     meta={"EventDate": "2026-09-16", "EventAgendaFile": "https://www.usg.edu/a.pdf", "EventMinutesFile": "https://www.usg.edu/Action-Summary.pdf",
                           "docs": [{"t": "Packet", "u": "https://www.usg.edu/p.pdf"}], "what": "Board Meeting", "location": "Atlanta",
                           "found_on": "https://www.usg.edu/regents/meetings/"}))
    out = tmp_path / "s.json"
    counts = A.export_state_meetings(store, ROOT, out, today="2026-11-20", log=lambda *a: None)
    d = json.loads(out.read_text())
    assert counts["left_out"] == 0
    assert not [r for r in body(d, "marta")["meetings"] if r["d"] == "2026-11-19" and "b" not in r]     # the one-a-day record is not exported
    reg = body(d, "regents")["meetings"]
    assert reg == [{"d": "2026-09-16", "n": "Board Meeting", "w": "Atlanta", "st": "held", "a": "https://www.usg.edu/a.pdf", "s": "https://www.usg.edu/Action-Summary.pdf",
                    "x": [{"l": "Packet", "u": "https://www.usg.edu/p.pdf"}], "p": "https://www.usg.edu/regents/meetings/", "seen": reg[0]["seen"], "upd": reg[0]["upd"]}]


def test_a_coming_meeting_its_page_drops_is_withdrawn_and_a_reread_does_not_touch_upd(read):
    before = {(r["d"], r.get("b", ""), r.get("t", "")): r["upd"] for r in body(read.data, "ports")["meetings"]}
    pages = dict(PAGES)
    try:
        html = (FX / "ports.html").read_text()
        cut = re.sub(r"(?s)<tr><td></td><td>July 26, 2027.*?</tr>\s*<tr>.*?</tr>", "", html, count=1)
        assert cut != html
        (read.tmp / "ports_cut.html").write_text(cut)

        class F2(Fetcher):
            def get(self, url, **k):
                if "gaports.com" in url:
                    return SimpleNamespace(status=200, text=cut, url=url, content_type="text/html", content=cut.encode())
                return super().get(url, **k)

        res = A.run(F2(), read.store, ROOT, only=["ports"], log=lambda *a: None, minutes_cap=0)
        assert any("withdrawn" in n for n in res["notes"])
        out = read.tmp / "w.json"
        A.export_state_meetings(read.store, ROOT, out, today="2026-11-20", log=lambda *a: None)
        rows = body(json.loads(out.read_text()), "ports")["meetings"]
        assert "2027-07-26" not in {r["d"] for r in rows} and len(rows) == 9
        assert all(before[(r["d"], r.get("b", ""), r.get("t", ""))] == r["upd"] for r in rows)       # unchanged meetings keep their stamp
    finally:
        PAGES.clear()
        PAGES.update(pages)


def test_the_export_passes_electos_importer(read):
    """Electo's own check of the format (ga/crawl_import.py state_bodies, strict), when its checkout is beside this one."""
    ga = ROOT.parent / "Electo" / "ga"
    if not (ga / "crawl_import.py").exists():
        pytest.skip("Electo is not checked out beside Crawl")
    sys.path.insert(0, str(ga))
    try:
        import crawl_import as CI
    except Exception as exc:  # noqa: BLE001 — its imports need Electo's own environment
        pytest.skip(f"Electo's importer does not import here: {type(exc).__name__}")
    finally:
        sys.path.remove(str(ga))
    if not hasattr(CI, "state_bodies"):
        pytest.skip("this Electo checkout has no state_bodies")
    # a body Electo has never seen and that has no archive copy yet is the one entry it may refuse; here every archive body was read
    bodies, dropped = CI.state_bodies(read.data["bodies"], {}, "", True)
    assert dropped == {}, dropped
    assert {b["id"] for b in bodies} == {b["id"] for b in read.data["bodies"]}
    assert sum(len(b["meetings"]) for b in bodies) == sum(len(b["meetings"]) for b in read.data["bodies"])


def test_the_schema_is_written_into_the_bundles_schema_md():
    assert "state_meetings.json (format 2)" in A.STATE_MEETINGS_SCHEMA
    for k in A.MEETING_KEYS + ("archive_as_of", "source"):
        assert f"| {k} |" in A.STATE_MEETINGS_SCHEMA
    assert "STATE_MEETINGS_SCHEMA" in (ROOT / "gacrawl" / "export.py").read_text()
