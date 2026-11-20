"""A copy of a page kept by the Internet Archive that is its site's firewall challenge is a bad copy, never the Archive
refusing this crawler; and a state body whose own site refuses, with nothing on file, is read from the Archive's copy.

On Nov. 19, 2026 one copy (Simbli's policy list as saved on May 27, 2024: an Imperva shell, served with 200) was judged
as web.archive.org's own challenge. The host went on the block list for a week, and every reader of archived copies
found the Archive off: the Georgia World Congress Center Authority, read only from the Archive, reached the site with
no meetings and was left out of it; Macon-Bibb County Transit, whose site turned the crawler away the next night, had
nowhere to be read from. The earlier tests of these readers put a stand-in in place of read_archived and of Wayback,
so the fetcher was never in them. Here it is: a real Fetcher over a made-up session."""
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gacrawl import agencies as A
from gacrawl.blocked import Blocked, BlockList, challenge_copy, is_archive_copy, looks_blocked, not_a_refusal, replayed
from gacrawl.fetch import Fetcher
from gacrawl.store import Store
from gacrawl.wayback import CDX, RAW, Wayback

ROOT = Path(__file__).resolve().parents[1]
FX = Path(__file__).parent / "fixtures" / "agencies"
IMPERVA = (b'<html><head><META NAME="robots" CONTENT="noindex,nofollow"><script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3">'
           b'</script></head><body>Request unsuccessful. Incapsula incident ID: 1234000310012345678-9876543210</body></html>')
CLOUDFLARE = (b"<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'>Checking your browser before accessing "
              b"example.gov.</div><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script></body></html>")
PAGE = b"<html><head><title>Board meetings</title></head><body><h1>Board meetings</h1><p>" + b"The board met and adopted the budget. " * 40 + b"</p></body></html>"


class Resp:
    def __init__(self, status, body=b"", ctype="text/html"):
        self.status_code, self.content, self.headers = status, body, {"Content-Type": ctype}
        self.text, self.elapsed = body.decode("utf-8", "replace"), dt.timedelta(seconds=0.01)

    def iter_content(self, n):
        yield self.content

    def close(self):
        pass


class Session:
    """Answers by full address: {address: (status, body[, content type])}; robots.txt 404 (no rules); anything else 404."""
    headers: dict = {}

    def __init__(self, routes):
        self.routes, self.asked, self.headers = dict(routes), [], {}

    def mount(self, *a, **k):
        pass

    def get(self, url, params=None, **kw):
        if url.endswith("/robots.txt"):
            return Resp(404)
        self.asked.append(url)
        hit = self.routes.get(url.split("?")[0] if url.startswith(CDX) else url)
        if hit is None:
            return Resp(404, b"not found")
        return Resp(*hit)


def fetcher(tmp_path, routes, **kw):
    return Fetcher("GACivicCrawler/test", tmp_path / "state", min_interval=0, session=Session(routes), sleep=lambda _: None,
                   host_retries={"web.archive.org": 0}, api_endpoints=["web.archive.org/cdx/search/cdx"], **kw)


def cdx(*rows):
    return (200, json.dumps([["urlkey", "timestamp", "original"]] + [["k", ts, u] for ts, u in rows]).encode(), "application/json")


SIMBLI = "https://simbli.eboardsolutions.com/Policy/PolicyListing.aspx?S=4047"


def test_what_is_a_copy_and_what_is_a_challenge():
    assert is_archive_copy(RAW.format(ts="20240527070431", url=SIMBLI)) and is_archive_copy("https://web.archive.org/web/20240527070431/" + SIMBLI)
    assert is_archive_copy("http://web.archive.org/web/2024/https://x.gov/") and is_archive_copy("https://web.archive.org/web/20240527070431if_/https://x.gov/a")
    for u in (CDX, "https://web.archive.org/", "https://web.archive.org/robots.txt", "https://web.archive.org/web/", "https://archive.org/details/x",
              "https://web.archive.org/save/https://x.gov/", "https://www.gwcca.org/web/20240101/about", "https://evil.example/https://web.archive.org/web/2024/x", ""):
        assert not is_archive_copy(u), u
    # the same body: a refusal from the site itself, a bad copy from the Archive
    assert looks_blocked(200, IMPERVA, "https://www.gwcca.org/about") == looks_blocked(200, IMPERVA) == "HTTP 200 bot-protection challenge (Imperva)"
    assert looks_blocked(200, IMPERVA, RAW.format(ts="20240527070431", url=SIMBLI)) is None
    assert looks_blocked(403, CLOUDFLARE, "https://x.gov/") == "HTTP 403 bot-protection challenge"
    # a saved 403 replayed with its own status is the copy's when the Archive marks the answer a replay; without that
    # mark a 403 at a copy's address is the Archive's own answer, and is honoured as one
    copy = RAW.format(ts="20240527070431", url="https://x.gov/")
    assert looks_blocked(403, CLOUDFLARE, copy, {"memento-datetime": "Mon, 27 May 2024 07:04:31 GMT"}) is None
    assert looks_blocked(403, CLOUDFLARE, copy, {"X-Archive-Orig-Server": "cloudflare"}) is None
    assert looks_blocked(403, CLOUDFLARE, copy) == looks_blocked(403, CLOUDFLARE, copy, {"content-type": "text/html"}) == "HTTP 403 bot-protection challenge"
    assert looks_blocked(429, CLOUDFLARE, copy) == "HTTP 429 bot-protection challenge"
    assert replayed({"Memento-Datetime": "x"}) and replayed({"x-archive-orig-date": "x"}) and not replayed({"server": "nginx"}) and not replayed(None)
    assert looks_blocked(403, CLOUDFLARE, CDX) == looks_blocked(403, CLOUDFLARE, CDX, {"memento-datetime": "x"}) == "HTTP 403 bot-protection challenge"   # the index is the Archive
    assert challenge_copy(IMPERVA.decode()) and challenge_copy(CLOUDFLARE.decode()) and not challenge_copy(PAGE.decode()) and not challenge_copy("")
    # a real page that mentions the words, or loads Cloudflare's script as every page behind it does, is a page
    assert not challenge_copy("<html><body>" + "Minutes of the meeting. " * 2000 + "Just a moment... verify you are human</body></html>")
    assert not challenge_copy(PAGE.decode().replace("</body>", "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></body>"))
    # Imperva's script is on every page of a site behind it: a short real page that carries it is a page, a shell that
    # says nothing else is the challenge
    short = ("<html><head><script src='/_Incapsula_Resource?SWJIYLWA=719d'></script><title>Board meetings</title></head><body><h1>Board meetings</h1><p>"
             + "The board meets on the second Tuesday of each month at 4 p.m. at the authority's offices. " * 6 + "</p></body></html>")
    shell = "<html><head><script src='/_Incapsula_Resource?SWJIYLWA=719d'></script></head><body><iframe src='/_Incapsula_Resource?x=1'></iframe></body></html>"
    assert len(short) < 4000 and not challenge_copy(short) and challenge_copy(shell)
    # bytes as a file reader holds them; a PDF or a large file is never one
    assert challenge_copy(IMPERVA) and not challenge_copy(b"%PDF-1.7 " + b"x" * 3000) and not challenge_copy(IMPERVA + b" " * 30000)


def test_a_copy_that_is_a_challenge_page_does_not_block_the_archive(tmp_path):
    bad, good = RAW.format(ts="20240527070431", url=SIMBLI), RAW.format(ts="20231101000000", url=SIMBLI)
    f = fetcher(tmp_path, {bad: (200, IMPERVA), good: (200, PAGE), CDX: cdx(("20231101000000", SIMBLI), ("20240527070431", SIMBLI))})
    r = f.get(bad)
    assert r.status == 200 and b"Incapsula" in r.content                     # handed to the reader, which judges the copy
    assert f.blocklist.is_blocked(bad) is None and "web.archive.org" not in f.blocklist.data
    assert f.get(good).status == 200                                         # and the Archive is still asked
    # the readers pass the bad copy over and take the one before it
    logs = []
    wb = Wayback(f, log=logs.append)
    assert wb.page("20240527070431", SIMBLI) is None and wb.off == ""
    assert wb.page("20231101000000", SIMBLI)[0] == "Board meetings"
    html, ts = A.read_archived(wb, SIMBLI)
    assert ts == "20231101000000" and "adopted the budget" in html and wb.off == "" and not logs
    assert json.loads((tmp_path / "state" / "blocked.json").read_text() or "{}") == {} if (tmp_path / "state" / "blocked.json").exists() else True


def test_the_site_itself_answering_with_the_same_page_is_still_a_refusal(tmp_path):
    f = fetcher(tmp_path, {"https://www.gwcca.org/about": (200, IMPERVA)})
    with pytest.raises(Blocked):
        f.get("https://www.gwcca.org/about")
    assert f.blocklist.is_blocked("https://www.gwcca.org/meetings")["reason"] == "HTTP 200 bot-protection challenge (Imperva)"


def test_the_entry_filed_for_the_archive_on_nov_19_is_not_honoured(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    entry = lambda reason, url: {"first_seen": now, "last_seen": now, "count": 1, "urls": [url], "sources": [], "reason": reason}
    path = tmp_path / "blocked.json"
    path.write_text(json.dumps({
        "web.archive.org": entry("HTTP 200 bot-protection challenge (Imperva)", RAW.format(ts="20240527070431", url=SIMBLI)),
        "mbcta.net": entry("HTTP 403 bot-protection challenge", "https://mbcta.net/category/board-meetings/"),
        "dbhdd.georgia.gov": entry("HTTP 403 bot-protection challenge", "https://dbhdd.georgia.gov/x.pdf")}))
    bl = BlockList(path)
    assert bl.is_blocked(CDX) is None and bl.is_blocked(RAW.format(ts="20260501095954", url="https://www.gwcca.org/about")) is None
    assert bl.is_blocked("https://mbcta.net/board-meeting-minutes/") and bl.is_blocked("https://dbhdd.georgia.gov/past-meetings-archive")
    copy = RAW.format(ts="20240527070431", url=SIMBLI)
    assert not_a_refusal("HTTP 200 bot-protection challenge (Imperva)", "web.archive.org", [copy])
    assert not not_a_refusal("HTTP 200 bot-protection challenge (Imperva)", "www.gwcca.org", [copy]) and not not_a_refusal("HTTP 200 bot-protection challenge (Imperva)")
    assert not not_a_refusal("HTTP 403 refused", "web.archive.org", [copy])   # a plain refusal by the Archive itself stays one
    # only an entry made of copies is withdrawn: one that names the index, the front door or robots.txt is the Archive's own
    assert not not_a_refusal("HTTP 200 bot-protection challenge (Imperva)", "web.archive.org", [copy, CDX])
    assert not not_a_refusal("HTTP 403 bot-protection challenge", "web.archive.org", [copy, CDX])
    assert not not_a_refusal("HTTP 403 bot-protection challenge", "web.archive.org", [copy])     # the Archive's own 403, at a copy's address
    assert not not_a_refusal("HTTP 403 bot-protection challenge", "web.archive.org", ["https://web.archive.org/"])
    assert not not_a_refusal("robots.txt: HTTP 403 bot-protection challenge", "web.archive.org", ["https://web.archive.org/robots.txt"])
    assert not not_a_refusal("HTTP 200 bot-protection challenge (Imperva)", "web.archive.org", [])
    # with that file as the run's state, the Archive answers
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "blocked.json").write_text(path.read_text())
    gw = RAW.format(ts="20260501095954", url="https://www.gwcca.org/about")
    f = fetcher(tmp_path, {gw: (200, PAGE), CDX: cdx(("20260501095954", "https://www.gwcca.org/about"))})
    wb = Wayback(f, log=lambda *a: None)
    assert A.read_archived(wb, "https://www.gwcca.org/about")[1] == "20260501095954" and wb.off == ""
    with pytest.raises(Blocked):
        f.get("https://mbcta.net/category/board-meetings/")                    # the site that did refuse is still left alone


# ---------------------------------------------------------------- a state body whose own site refuses

# Macon-Bibb County Transit Authority's two pages, as saved in tests/fixtures/agencies: the body of Nov. 20, 2026
MB = {"https://mbcta.net/category/board-meetings/": "mbcta_posts.html", "https://mbcta.net/board-meeting-minutes/": "mbcta_minutes.html"}


def _mb_root() -> Path:
    """The config with the authority as it was set down on Nov. 20, 2026 (its two pages): these tests are of the rule
    that reads a refusing body from the Archive, not of the authority's pages today (tests/test_macon_transit.py)."""
    import re
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="mb-root-"))
    (root / "config").mkdir()
    cfg = (ROOT / "config" / "agencies.yaml").read_text()
    two = """  - id: macon-transit
    kind: transit
    name: Macon-Bibb County Transit Authority
    agency: Regional transit authority (Macon-Bibb County)
    pages:
      - {u: "https://mbcta.net/category/board-meetings/", reader: notice_posts}
      - {u: "https://mbcta.net/board-meeting-minutes/", reader: month_named}
"""
    cfg, n = re.subn(r"  - id: macon-transit\n.*?(?=\n  - id: |\n\S)", two.rstrip("\n"), cfg, count=1, flags=re.S)
    assert n == 1
    (root / "config" / "agencies.yaml").write_text(cfg)
    return root


MB_ROOT = _mb_root()
CHALLENGED = "HTTP 403 bot-protection challenge"


class BodyFetcher:
    """The body's own site: `pages` answer, `refuse` raise Blocked with that reason (as the real fetcher does for a
    challenge, and for every page of a host on the block list)."""

    def __init__(self, pages=(), refuse=None):
        self.pages, self.refuse, self.asked = set(pages), refuse or {}, []

    def get(self, url, **k):
        self.asked.append(url)
        if url in self.refuse:
            raise Blocked("mbcta.net", self.refuse[url])
        f = MB.get(url) if url in self.pages else None
        t = (FX / f).read_text() if f else ""
        return SimpleNamespace(status=200 if f else 404, text=t, url=url, content_type="text/html", content=t.encode())


def export(store, tmp_path, name, today="2026-11-20"):
    out = tmp_path / name
    A.export_state_meetings(store, MB_ROOT, out, today=today, log=lambda *a: None)
    return next(b for b in json.loads(out.read_text())["bodies"] if b["id"] == "macon-transit")


def read(fetcher, store):
    return A.run(fetcher, store, MB_ROOT, only=["macon-transit"], log=lambda *a: None, minutes_cap=0)


@pytest.fixture()
def archive(monkeypatch):
    """The Archive holds a copy of each of the body's pages, saved Oct. 30, 2026; `asked` lists what was looked for."""
    import gacrawl.wayback as W

    asked = []
    monkeypatch.setattr(W, "Wayback", lambda *a, **k: SimpleNamespace(off=""))
    monkeypatch.setattr(A, "read_archived", lambda wb, u, copies=4: asked.append(u) or (((FX / MB[u]).read_text(), "20261030120000")
                                                                                         if u in MB else (None, "")))
    return asked


def test_the_body_reads_from_its_own_site_when_it_answers(tmp_path, archive):
    store = Store(tmp_path / "data")
    res = read(BodyFetcher(pages=MB), store)
    b = export(store, tmp_path, "a.json")
    assert archive == [] and not [e for e in res["errors"] if "macon" in e]
    assert b["source"] == "live" and "archive_as_of" not in b and len(b["meetings"]) == 3 and A.validate_body(b) == []
    assert all(r["p"].startswith("https://mbcta.net/") for r in b["meetings"])
    store.close()


def test_a_body_whose_site_refuses_and_has_nothing_on_file_is_read_from_the_archives_copy(tmp_path, archive):
    store = Store(tmp_path / "data")
    live = Store(tmp_path / "live")
    read(BodyFetcher(pages=MB), live)
    want = len(export(live, tmp_path, "live.json")["meetings"])            # what the same pages give when the site answers
    live.close()
    refuse = {u: CHALLENGED for u in MB}
    res = read(BodyFetcher(refuse=refuse), store)
    assert sorted(archive) == sorted(MB) and res["by"]["macon-transit"]["archive_as_of"] == "2026-10-30"
    assert not [e for e in res["errors"] if "macon" in e]
    b = export(store, tmp_path, "a.json")
    assert b["source"] == "archive" and b["archive_as_of"] == "2026-10-30" and len(b["meetings"]) == want == 3
    # a meeting listed without a page of its own points at the Archive's copy of the list; one with its own notice
    # points at the notice, on the body's site (a person's browser opens it)
    copy = "https://web.archive.org/web/20261030120000/https://mbcta.net/board-meeting-minutes/"
    assert {r["d"]: r["p"] for r in b["meetings"]} == {"2026-10-27": "https://mbcta.net/mbcta-board-of-directors-meeting-october-27-2026/",
                                                         "2026-09-29": "https://mbcta.net/mbcta-board-of-directors-meeting-september-29-2026/",
                                                         "2024-01-30": copy}
    assert A.validate_body(b) == [] and b["pages"] == list(MB)             # the pages listed are the body's own
    # the next night the site still refuses: the copy was read this week and is not asked for again; nothing is lost
    archive.clear()
    read(BodyFetcher(refuse=refuse), store)
    b2 = export(store, tmp_path, "b.json")
    assert archive == [] and b2["source"] == "archive" and b2["archive_as_of"] == "2026-10-30" and len(b2["meetings"]) == want
    # the site answers again: read from it, and the body says live
    read(BodyFetcher(pages=MB), store)
    b3 = export(store, tmp_path, "c.json")
    assert b3["source"] == "live" and "archive_as_of" not in b3 and len(b3["meetings"]) >= want and A.validate_body(b3) == []
    store.close()


def test_a_body_with_meetings_on_file_keeps_them_and_no_older_copy_is_read_over_them(tmp_path, archive):
    store = Store(tmp_path / "data")
    read(BodyFetcher(pages=MB), store)
    before = export(store, tmp_path, "a.json")
    assert before["source"] == "live" and len(before["meetings"]) == 3 and archive == []
    res = read(BodyFetcher(refuse={u: CHALLENGED for u in MB}), store)
    assert archive == [] and sum("macon-transit" in e and "Blocked" in e for e in res["errors"]) == 2
    after = export(store, tmp_path, "b.json")
    assert after["source"] == "live" and "archive_as_of" not in after
    strip = lambda b: [{k: v for k, v in r.items() if k != "upd"} for r in b["meetings"]]
    assert strip(after) == strip(before)
    store.close()


def test_a_page_behind_a_login_is_never_read_from_a_copy(tmp_path, archive):
    store = Store(tmp_path / "data")
    res = read(BodyFetcher(refuse={u: "HTTP 401 refused" for u in MB}), store)
    assert archive == [] and res["by"]["macon-transit"]["meetings"] == 0 and sum("macon-transit" in e for e in res["errors"]) == 2
    b = export(store, tmp_path, "a.json")
    assert b["source"] == "live" and b["meetings"] == []
    store.close()


def test_a_refusing_site_with_no_copy_says_both(tmp_path, monkeypatch):
    import gacrawl.wayback as W

    monkeypatch.setattr(W, "Wayback", lambda *a, **k: SimpleNamespace(off=""))
    monkeypatch.setattr(A, "read_archived", lambda wb, u, copies=4: (None, ""))
    store = Store(tmp_path / "data")
    res = read(BodyFetcher(refuse={u: CHALLENGED for u in MB}), store)
    errs = [e for e in res["errors"] if "macon-transit" in e]
    assert len(errs) == 2 and all("refused (mbcta.net: HTTP 403 bot-protection challenge), and no readable Internet Archive copy" in e for e in errs)
    b = export(store, tmp_path, "a.json")
    assert b["source"] == "live" and b["meetings"] == [] and A.validate_body(b) == []      # nothing is claimed that was not read
    store.close()


def test_a_real_challenge_from_the_archive_itself_is_honoured(tmp_path):
    """The Archive's index answering with a challenge is the Archive refusing: asked once, then left alone, by this
    run and by the next (the entry it files is honoured, with the old entry of copies still in the file)."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "blocked.json").write_text(json.dumps({"web.archive.org": {
        "first_seen": now, "last_seen": now, "count": 1, "sources": [], "reason": "HTTP 200 bot-protection challenge (Imperva)",
        "urls": [RAW.format(ts="20240527070431", url=SIMBLI)]}}))
    routes = {CDX: (403, CLOUDFLARE)}
    f = fetcher(tmp_path, routes)
    asked = lambda fx: [u for u in fx.session.asked if "web.archive.org" in u]
    logs = []
    wb = Wayback(f, log=logs.append)
    assert wb.captures("https://www.gwcca.org/about") == [] and "Blocked" in wb.off and len(asked(f)) == 1
    assert wb.captures("https://mbcta.net/board-meeting-minutes/") == [] and len(asked(f)) == 1          # off for the run
    rec = f.blocklist.is_blocked(CDX)
    assert rec and rec["reason"] == "HTTP 403 bot-protection challenge" and CDX.split("?")[0] in " ".join(rec["urls"])
    # the next run: the entry is honoured, nothing is asked of the Archive at all
    f2 = fetcher(tmp_path, routes)
    wb2 = Wayback(f2, log=logs.append)
    assert wb2.captures("https://www.gwcca.org/about") == [] and asked(f2) == []
    with pytest.raises(Blocked):
        f2.get(RAW.format(ts="20260501095954", url="https://www.gwcca.org/about"))
    assert asked(f2) == []


def test_the_archives_own_refusal_at_a_copys_address_is_honoured_and_a_replayed_one_is_the_copys(tmp_path):
    copy = RAW.format(ts="20240527070431", url="https://x.gov/board")

    class Marked(Session):
        def get(self, url, params=None, **kw):
            r = super().get(url, params, **kw)
            if url == copy and self.routes.get("replay"):
                r.headers = {**r.headers, "Memento-Datetime": "Mon, 27 May 2024 07:04:31 GMT", "X-Archive-Orig-Server": "cloudflare"}
            return r

    def fx(replay):
        return Fetcher("GACivicCrawler/test", tmp_path / ("r" if replay else "o"), min_interval=0,
                       session=Marked({copy: (403, CLOUDFLARE), "replay": replay}), sleep=lambda _: None, host_retries={"web.archive.org": 0})
    # the Archive itself answering 403 with a challenge: one request, then the host is left alone
    own = fx(False)
    with pytest.raises(Blocked):
        own.get(copy)
    assert own.blocklist.is_blocked(copy)["reason"] == "HTTP 403 bot-protection challenge"
    with pytest.raises(Blocked):
        own.get(copy)
    assert own.session.asked.count(copy) == 1
    # a saved 403 the Archive replays (it says so in its headers): the copy's own, no block of the Archive
    rep = fx(True)
    try:
        r = rep.get(copy)
        assert r.status == 403
    except Blocked:
        # a plain 403 is still counted one by one (a file store's bar is twenty in a row): never a block on the first
        pytest.fail("a replayed copy's 403 blocked the Archive")
    assert rep.blocklist.is_blocked(copy) is None


def test_one_refusal_from_the_archive_stops_every_body_of_the_run_asking(tmp_path, monkeypatch):
    """One Wayback for the whole run: after the Archive's first 429 no later body asks it again."""
    import gacrawl.wayback as W

    made, asked = [], []

    class OneWayback:
        def __init__(self, *a, **k):
            made.append(self)
            self.off = ""

    def read_archived(wb, u, copies=4):
        if not wb.off:
            asked.append(u)
            wb.off = "HTTP 429 from web.archive.org"       # the Archive's first answer of the run
        return None, ""

    monkeypatch.setattr(W, "Wayback", OneWayback)
    monkeypatch.setattr(A, "read_archived", read_archived)
    store = Store(tmp_path / "data")

    class Refusing:
        def get(self, url, **k):
            raise Blocked("x", CHALLENGED)

    res = A.run(Refusing(), store, MB_ROOT, only=["gwcca", "macon-transit"], log=lambda *a: None, minutes_cap=0)
    assert len(made) == 1 and len(asked) == 1
    assert sum("no readable Internet Archive copy (HTTP 429 from web.archive.org)" in e for e in res["errors"]) == 3   # gwcca's page, Macon-Bibb's two
    store.close()


def test_readings_of_the_bodys_own_site_from_before_the_format_are_not_read_over(tmp_path, archive):
    """Records stored before format 2 carry no format mark. They are readings of the body's own site all the same: when
    the site refuses, they stay as they are and no older copy is read over them (their minutes links would be lost)."""
    from gacrawl.store import Record

    store = Store(tmp_path / "data")
    for d in ("2026-10-27", "2026-09-29"):
        store.add(Record("ga-agency:macon-transit", "meeting", f"https://mbcta.net/board-meeting-minutes/#meeting-{d}", title=f"MBCTA {d}", text=f"MBCTA {d}",
                         published=d, channel="html", jurisdiction="ga", evidence="primary", key=f"ga-agency:macon-transit:{d}",
                         meta={"agency": "macon-transit", "EventBodyName": "Macon-Bibb County Transit Authority", "EventDate": d,
                               "EventMinutesFile": f"https://mbcta.net/wp-content/uploads/minutes-{d}.pdf", "found_on": "https://mbcta.net/board-meeting-minutes/"}))
    res = read(BodyFetcher(refuse={u: CHALLENGED for u in MB}), store)
    assert archive == [] and sum("macon-transit" in e and "Blocked" in e for e in res["errors"]) == 2
    store.close()


def test_a_body_read_from_a_copy_is_looked_for_again_after_its_recheck_days(tmp_path, archive, monkeypatch):
    """A body read from the Archive because its site refuses is not frozen on that copy: while the site still refuses,
    the Archive is looked at again every recheck_days (7), as for a body set down as read from the Archive."""
    store = Store(tmp_path / "data")
    refuse = {u: CHALLENGED for u in MB}
    read(BodyFetcher(refuse=refuse), store)
    assert sorted(archive) == sorted(MB)
    archive.clear()
    read(BodyFetcher(refuse=refuse), store)
    assert archive == []                                        # the same week: not asked again
    for u in MB:                                                # eight days on
        stamp = json.loads(store.kv_get(f"agency_archive:macon-transit:{u}"))
        stamp["at"] = (dt.date.today() - dt.timedelta(days=8)).isoformat()
        store.kv_set(f"agency_archive:macon-transit:{u}", json.dumps(stamp))
    res = read(BodyFetcher(refuse=refuse), store)
    assert sorted(archive) == sorted(MB) and not [e for e in res["errors"] if "macon" in e]
    b = export(store, tmp_path, "a.json")
    assert b["source"] == "archive" and b["archive_as_of"] == "2026-10-30" and len(b["meetings"]) == 3
    store.close()


def test_file_readers_pass_a_challenge_copy_over(tmp_path):
    """The sheriffs' list and the history files are read from copies too: a copy that is a challenge page is not taken
    for the file; the copy before it is."""
    import gacrawl.sheriffs as SH

    src = (ROOT / "gacrawl" / "sheriffs.py").read_text() + (ROOT / "gacrawl" / "history" / "run.py").read_text()
    assert src.count("challenge_copy(") >= 2
    url = SH.URL
    bad, good = RAW.format(ts="20261101000000", url=url), RAW.format(ts="20261001000000", url=url)
    f = fetcher(tmp_path, {CDX: cdx(("20261001000000", url), ("20261101000000", url)), bad: (200, IMPERVA), good: (200, PAGE)})
    arch = [n for n in dir(SH) if "archiv" in n.lower() and callable(getattr(SH, n))]
    assert arch, "sheriffs.py has a reader of the Archive's copies"
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(SH, "ARCHIVE_DAYS", 100000, raising=False)
        got = getattr(SH, arch[0])(f, log=lambda *a: None)
    finally:
        monkey.undo()
    assert got[0] and "adopted the budget" in got[0] and got[1] == "2026-10-01"
