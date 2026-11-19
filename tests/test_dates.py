"""Where a record's date came from (meta.date_from) and dates of a page's own words that are not its publication
(meta.date_set_aside): gacrawl/extract.py page_date_info, the page readers, the once-only repair of stored pages
(Store.set_aside_text_dates) and the merge's count of each for the run's log."""
import json
import sqlite3

from gacrawl import extract
from gacrawl.channels import date_meta
from gacrawl.shard import merge_delta
from gacrawl.store import Record, Store


def test_each_date_says_where_it_was_read():
    assert extract.page_date_info('<meta property="article:published_time" content="2026-01-02T10:00:00Z">', "")["from"] == "meta"
    assert extract.page_date_info('<time datetime="2025-03-04">March 4</time>', "")["from"] == "time"
    assert extract.page_date_info('<script type="application/ld+json">{"datePublished": "2024-05-06"}</script>', "")["from"] == "jsonld"
    i = extract.page_date_info("", "Posted November 3, 2026. The council met.", "2026-11-05")
    assert i == {"date": "2026-11-03", "from": "text", "set_aside": None}
    i = extract.page_date_info("", "On September 11, 2001 the county remembers.", "2026-11-05")
    assert i == {"date": None, "from": "", "set_aside": "2001-09-11"}
    assert date_meta(i) == {"date_set_aside": "2001-09-11"} and date_meta({"from": "text", "set_aside": None}) == {"date_from": "text"}
    assert extract.page_date("", "On September 11, 2001 the county remembers.", "2026-11-05") is None


def test_stored_pages_dated_by_a_date_their_words_mention_are_set_aside(tmp_path):
    store = Store(tmp_path / "data")
    words = "Al Bartell was born January 12, 1956 in Macon and ran for governor."
    store.add(Record("x:web", "article", "https://news.example/a", title="A", text=words, published="1956-01-12", channel="html"))
    store.add(Record("x:web", "article", "https://news.example/b", title="B", text=words, published="1956-01-12", channel="html",
                     meta={"date_from": "meta"}))                                 # the page's markup said so
    store.add(Record("x:web", "document", "https://x.gov/1956.pdf", title="C", text=words, published="1956-01-12", channel="html"))
    store.add(Record("x:web", "article", "https://news.example/d", title="D", text="Founded in 1890. " + words,
                     published="1990-01-01", channel="html"))                    # not the first date of its words
    store.add(Record("x:web", "article", "https://news.example/e", title="E", text="Posted May 1, 2026.", published="2026-05-01", channel="html"))
    store.flush()
    out = store.set_aside_text_dates()
    assert out["set_aside"] == 1, out
    recs = {r["url"].rsplit("/", 1)[1].replace("1956.pdf", "c"): r for r in store.query("source_id='x:web'")}
    assert recs["a"]["published"] is None and recs["a"]["meta"]["date_set_aside"] == "1956-01-12" and recs["a"]["meta"]["date_from"] == "text"
    assert recs["b"]["published"] == "1956-01-12" and recs["c"]["published"] == "1956-01-12"
    assert recs["d"]["published"] == "1990-01-01" and recs["e"]["published"] == "2026-05-01"
    assert store.set_aside_text_dates()["set_aside"] == 0                       # once


def test_the_merge_counts_where_the_runs_dates_came_from(tmp_path):
    main = Store(tmp_path / "main")
    shard = Store(tmp_path / "shard")
    shard.add(Record("x:web", "page", "https://x.gov/1", title="1", text="t", published="2026-11-01", channel="html", meta={"date_from": "text"}))
    shard.add(Record("x:web", "page", "https://x.gov/2", title="2", text="t", published="2026-11-02", channel="feed", meta={"date_from": "feed"}))
    shard.add(Record("x:legistar", "meeting", "https://x.gov/3", title="3", text="t", published="2026-11-03", channel="api"))
    shard.add(Record("x:web", "page", "https://x.gov/4", title="4", text="t", channel="html", meta={"date_set_aside": "2001-09-11"}))
    shard.flush()
    shard.close()
    db_file = next(p for p in (tmp_path / "shard").rglob("*.db"))
    res = merge_delta(main, db_file)
    assert res["records"] == 4 and res["dates"] == {"text": 1, "feed": 1, "source": 1, "set_aside": 1}, res
