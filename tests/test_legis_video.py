"""Committee hearing video: dated committee steps from a bill's detail, an official Vimeo channel's videos, the link."""
import json
from pathlib import Path

from gacrawl import legis_video as lv
from gacrawl.fetch import FetchResult
from gacrawl.store import Record, Store

DETAIL = {"id": 1, "statusHistory": [
    {"date": "2026-02-10T00:00:00", "name": "House First Readers"},
    {"date": "2026-02-19T00:00:00", "name": "House Committee Favorably Reported By Substitute"},
    {"date": "2026-03-05T00:00:00", "name": "Senate Committee Favorably Reported"}]}

PAGE = ('<html><head><link rel="alternate" type="application/rss+xml" href="https://vimeo.com/georgiahouse/videos/rss">'
        '</head><body><a href="/111111111" title="2.19.26 Governmental Affairs">x</a></body></html>')
FEED = (b'<?xml version="1.0"?><rss><channel><item><title>2.18.26 Ways and Means</title>'
        b'<link>https://vimeo.com/222222222</link><pubDate>Wed, 18 Feb 2026 20:00:00 +0000</pubDate></item></channel></rss>')


def test_committee_steps_reads_any_shape():
    st = lv.committee_steps(DETAIL)
    assert [s["date"] for s in st] == ["2026-02-19", "2026-03-05"]
    assert "Favorably Reported" in st[0]["step"]


def test_links_the_hearing_on_that_day(tmp_path):
    pages = {"https://vimeo.com/georgiahouse/videos": PAGE.encode(), "https://vimeo.com/georgiahouse/videos/rss": FEED}

    class F:
        def get(self, url, **kw):
            body = pages.get(url)
            return FetchResult(url, 200 if body else 404, {"content-type": "text/html"}, body or b"", 0.0)

    store = Store(tmp_path / "db")
    store.add(Record(lv.LEGIS, "bill", "https://www.legis.ga.gov/legislation/1", title="HB 9",
                     meta={"number": "HB 9", "session": 1033, "legislation_id": 1, "house_committee": "Governmental Affairs",
                           "senate_committee": "Ethics"}))
    store.flush()
    store.kv_set("bill_history:1", json.dumps(lv.committee_steps(DETAIL)))
    root = tmp_path / "root"; (root / "config").mkdir(parents=True)
    (root / "config" / "legislature.yaml").write_text(
        "video:\n  since: '2019-01-01'\n  channels:\n    - name: House\n      url: https://vimeo.com/georgiahouse\n")
    out = lv.link_videos(F(), store, root, log=lambda *a: None)
    assert out["links"] == 1
    v = list(store.iter_query("kind='bill_video'"))
    assert v[0]["url"] == "https://vimeo.com/111111111" and v[0]["meta"]["step_date"] == "2026-02-19"


def test_hearing_captions_become_time_coded_reasons(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMEO_TOKEN", "t")
    vtt = ("WEBVTT\n\n00:10:00.000 --> 00:10:05.000\nNext is House Bill 9 by Representative Smith.\n\n"
           "00:10:05.000 --> 00:10:10.000\nThis bill would cost counties two million dollars.\n\n"
           "00:10:10.000 --> 00:10:15.000\nOpponents say it is an unfunded mandate.\n")
    pages = {"https://api.vimeo.com/videos/111111111/texttracks": json.dumps({"data": [{"link": "https://captions.example/1.vtt", "language": "en", "active": True}]}).encode(),
             "https://captions.example/1.vtt": vtt.encode()}

    class F:
        def get(self, url, **kw):
            body = pages.get(url)
            return FetchResult(url, 200 if body else 404, {"content-type": "text/plain"}, body or b"", 0.0)

    store = Store(tmp_path / "db")
    store.add(Record(lv.SOURCE, "bill_video", "https://vimeo.com/111111111", title="2.19.26 Governmental Affairs",
                     meta={"legislation_id": 1, "number": "HB 9", "session": 1033, "committee": "Governmental Affairs",
                           "video_date": "2026-02-19", "platform": "vimeo", "video_id": "111111111"}))
    store.flush()
    out = lv.hearings(F(), store, {}, log=lambda *a: None)
    assert out["captions"] == 1 and out["moments"] == 1
    r = list(store.iter_query("kind='bill_reason' AND source_id=?", (lv.HEARING_SOURCE,)))[0]
    assert r["meta"]["at"] == "0:10:00" and "unfunded" in r["text"] and r["url"].endswith("#t=600s")
    assert lv.hearings(F(), store, {}, log=lambda *a: None)["waiting"] == 0
