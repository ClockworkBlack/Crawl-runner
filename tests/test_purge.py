"""config/purge.yaml: a portal that belongs to another place is taken out of the database, with what cited it."""
from gacrawl.store import Record, Store


def test_purge_source(tmp_path):
    store = Store(tmp_path / "data")
    for i in range(5):
        store.add(Record("columbus-muscogee:legistar", "ordinance", f"https://webapi.legistar.com/v1/columbus/matters/{i}",
                         title=f"Ordinance {i}", text="Franklin County, Ohio", jurisdiction="columbus-muscogee"))
    store.add(Record("columbus-muscogee:youtube", "video", "https://www.youtube.com/watch?v=a", title="Council Meeting",
                     text="Columbus, Georgia", jurisdiction="columbus-muscogee"))
    store.add(Record("cand:someone", "mention", "https://webapi.legistar.com/v1/columbus/matters/1", title="Ordinance 1",
                     text="...", meta={"source": "columbus-muscogee:legistar"}))
    store.set_cursor("columbus-muscogee:legistar", "matters", "2026-11-07")
    store.mark_url("https://webapi.legistar.com/v1/columbus/matters/1", 200, "columbus-muscogee:legistar")
    store.flush()
    assert list(store.search(Store.fts_phrase("Franklin County"), limit=10))
    out = store.purge_source("columbus-muscogee:legistar")
    assert out == {"records": 5, "citing_mentions": 1}
    assert store.counts("source_id") == {"columbus-muscogee:youtube": 1}
    assert not store.cursors("columbus-muscogee:legistar") and not store.url_seen("https://webapi.legistar.com/v1/columbus/matters/1")
    assert not list(store.search(Store.fts_phrase("Franklin County"), limit=10))      # gone from search too
    assert store.purge_source("columbus-muscogee:legistar") == {"records": 0, "citing_mentions": 0}
    store.close()


def test_purge_list_parses():
    from pathlib import Path

    from gacrawl.registry import load_yaml

    entries = load_yaml(Path(__file__).resolve().parent.parent / "config" / "purge.yaml")
    assert entries and all(e.get("source") and e.get("reason") for e in entries)
