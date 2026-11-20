"""Every record says when its source published it (only when the source states it), when it first arrived here
(first_seen, never rewritten) and when it last changed (updated); the bundle carries them under those names."""
import datetime as dt
import email.utils
import http.server
import json
import threading
import time

import requests

from gacrawl import channels, export
from gacrawl.fetch import Fetcher, FetchResult
from gacrawl.shard import merge_delta, write_delta
from gacrawl.store import Record, Store

TEXT = "<p>" + "The council will hold a public hearing on December 15, 2026 about the budget. " * 5 + "</p>"
OLD = email.utils.format_datetime(dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.timezone.utc), usegmt=True)


def test_first_seen_stays_and_updated_moves(tmp_path, monkeypatch):
    import gacrawl.store as st

    clock = iter(["2026-11-01T00:00:00+00:00", "2026-11-05T00:00:00+00:00", "2026-11-09T00:00:00+00:00", "2026-11-12T00:00:00+00:00"])
    monkeypatch.setattr(st, "utcnow", lambda: next(clock))
    s = Store(tmp_path / "data")
    s.add(Record("x:site", "page", "http://x/a", title="A", text="one", fetched_at="2026-11-01T00:00:00+00:00"))
    row = s.read("x:site", "page")[0]
    assert row["first_seen"] == row["changed_at"] == "2026-11-01T00:00:00+00:00"
    s.add(Record("x:site", "page", "http://x/a", title="A", text="two", fetched_at="2026-11-05T00:00:00+00:00"))     # new text
    s.add(Record("x:site", "page", "http://x/a", title="A", text="two", meta={"k": 1}, fetched_at="2026-11-09T00:00:00+00:00"))  # new meta
    s.add(Record("x:site", "page", "http://x/a", title="A", text="two", meta={"k": 1}, published="Tue, 27 Oct 2026 15:05:21 -0500"))  # a date
    row = s.read("x:site", "page")[0]
    assert row["first_seen"] == "2026-11-01T00:00:00+00:00" and row["changed_at"] == "2026-11-12T00:00:00+00:00"
    assert row["published"] == "2026-10-27T15:05:21-05:00" and row["version"] == 4
    raw = s.db.execute("SELECT published FROM records").fetchone()[0]
    assert raw == "2026-10-27T15:05:21-05:00"                           # stored in the one form, not only read so
    s.close()


def test_a_date_set_after_the_record_was_made_is_stored_as_iso(tmp_path):
    s = Store(tmp_path / "data")
    r = Record("x:site", "page", "http://x/a", title="A", text="one")
    r.published = "Mon, 02 Nov 2026 19:00:00 GMT"                       # a reader taking the feed's date over the page's
    s.add(r)
    assert s.db.execute("SELECT published FROM records").fetchone()[0] == "2026-11-02T19:00:00+00:00"
    s.close()


def test_a_merge_keeps_the_earliest_first_seen(tmp_path):
    main, shard = Store(tmp_path / "main"), Store(tmp_path / "shard")
    for s, seen in ((main, "2026-11-01T00:00:00+00:00"), (shard, "2026-11-18T00:00:00+00:00")):
        s.add(Record("x:site", "page", "http://x/a", title="A", text="old" if s is main else "new"))
        s.db.execute("UPDATE records SET first_seen=?, changed_at=?", (seen, seen))
    write_delta(shard, "2026-11-10T00:00:00+00:00", tmp_path / "d.db")
    merge_delta(main, tmp_path / "d.db")
    row = main.read("x:site", "page")[0]
    assert row["text"] == "new" and row["first_seen"] == "2026-11-01T00:00:00+00:00" and row["changed_at"] == "2026-11-18T00:00:00+00:00"
    main.close(); shard.close()


def test_last_modified_is_kept_when_the_server_states_it():
    def res(value):
        return FetchResult("http://x/a", 200, {"last-modified": value} if value else {}, b"", 0.0)
    assert channels.http_modified(res(OLD)) == "2026-03-02T14:30:00+00:00"
    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc), usegmt=True)
    assert channels.http_modified(res(now)) is None                     # a page built on request: stamped "now"
    assert channels.http_modified(res(now), min_age_hours=0) is not None  # a file just posted: its stamp counts
    assert channels.http_modified(res("yesterday-ish")) is None and channels.http_modified(res(None)) is None
    ahead = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2), usegmt=True)
    assert channels.http_modified(res(ahead), min_age_hours=0) is None


def site():
    pages = {
        "/": ("text/html", f'<html><title>Home</title><body><a href="/news/hearing">Hearing</a> <a href="/council">Council</a>'
                           f'<a href="/files/minutes.txt">Minutes</a>{TEXT}</body></html>'.encode(), None),
        "/news/hearing": ("text/html", b'<html><head><title>Hearing</title><meta property="article:published_time" '
                                       b'content="2026-11-17T09:00:00-05:00"></head><body>' + TEXT.encode() + b"</body></html>", None),
        "/council": ("text/html", f"<html><title>Council</title><body>{TEXT}</body></html>".encode(), OLD),
        "/files/minutes.txt": ("text/plain", b"MINUTES\nThe council met on October 5, 2026.\n", OLD),
    }
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path not in pages:
                self.send_response(404); self.end_headers(); return
            ctype, body, modified = pages[self.path]
            if modified and self.headers.get("If-Modified-Since") == modified:
                self.send_response(304); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if modified:
                self.send_header("Last-Modified", modified)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", hits


def test_the_page_crawl_dates_a_page_by_its_markup_and_never_by_its_words(tmp_path):
    srv, base, hits = site()
    try:
        s = requests.Session(); s.trust_env = False
        f = Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)
        src = {"id": "x:site", "url": base + "/", "kind": "page", "depth": 2}
        recs = {r.url[len(base):]: r for r in channels._html_crawl(f, src, 20, channels.CrawlContext())}
        hearing = recs["/news/hearing"]
        assert hearing.published == "2026-11-17" and hearing.meta["date_from"] == "meta"
        council = recs["/council"]                                      # its words name Dec. 15: the hearing's date, not its own
        assert council.published is None and council.meta["http_last_modified"] == "2026-03-02T14:30:00+00:00"
        assert recs["/"].published is None and "http_last_modified" not in recs["/"].meta
        doc = recs["/files/minutes.txt"]
        assert doc.published is None and doc.meta["http_last_modified"] == "2026-03-02T14:30:00+00:00"
        # read again through the conditional-request cache (304): the stamp is still there, the record unchanged
        f._memo.clear()
        again = {r.url[len(base):]: r for r in channels._html_crawl(f, src, 20, channels.CrawlContext())}
        assert again["/council"].meta == council.meta and again["/council"].content_hash == council.content_hash
    finally:
        srv.shutdown()


def test_the_bundle_carries_published_first_seen_updated_and_modified(tmp_path):
    s = Store(tmp_path / "data")
    s.add(Record("x:site", "page", "http://x/a", title="A", text="one", published="2026-11-17", jurisdiction="x",
                 meta={"date_from": "meta"}))
    s.add(Record("x:site", "document", "http://x/m.pdf", title="Minutes", text="two", jurisdiction="x",
                 meta={"http_last_modified": "2026-03-02T14:30:00+00:00"}))
    s.db.execute("UPDATE records SET first_seen='2026-11-01T00:00:00+00:00'")
    s.flush()
    out = tmp_path / "x.json"
    export._write_jurisdiction(out, {"id": "x", "name": "X"}, s, s.counts("kind", "jurisdiction=?", ("x",)))
    d = json.loads(out.read_text())["records"]
    page, doc = d["page"][0], d["document"][0]
    assert page["published"] == "2026-11-17" and page["first_seen"] == "2026-11-01T00:00:00+00:00"
    assert page["updated"] == page["changed_at"] and page["updated"].endswith("+00:00") and "modified" not in page
    assert doc["published"] is None and doc["modified"] == "2026-03-02T14:30:00+00:00" and doc["first_seen"] and doc["updated"]
    slim = export._slim(s.read("x:site", "document")[0])
    assert slim["first_seen"] == "2026-11-01T00:00:00+00:00" and slim["updated"] == slim["changed_at"] and slim["modified"]
    for word in ("| first_seen |", "| updated |", "| modified |", "| published |"):
        assert word in export.SCHEMA_MD
    s.close()
