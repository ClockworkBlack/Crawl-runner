"""Press releases -> appointments; newsroom sources; page dates. Fictional names."""
from pathlib import Path

from gacrawl import derive, extract, press, registry
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent
RELEASE = """ATLANTA – October 2, 2026 – Governor Brian P. Kemp today announced the appointment of Jane Q. Public to the State Board of Education, representing the 7th Congressional District. Public is a Marietta attorney. The Governor also appointed John Doe as judge of the Superior Court of the Blue Ridge Judicial Circuit, filling the vacancy created by the retirement of Judge Ann Lee. Kemp also announced appointments to the Board of Natural Resources: Sam Third of Macon; Lee Ward of Rome; and Kim Post of Dalton, with terms expiring January 1, 2032."""
CITY = """The Canton City Council on Thursday appointed Pat Local to the Planning Commission for a term ending December 31, 2028. Mayor Dana Reed said Local brings experience. The council also discussed the parks plan."""


def test_parse_appointments():
    a = {x["person"]: x for x in press.parse_appointments(RELEASE)}
    assert a["Jane Q. Public"]["body"] == "State Board of Education" and a["Jane Q. Public"]["by"] == "Brian P. Kemp"
    assert a["John Doe"]["role"] == "judge" and a["John Doe"]["body"].startswith("Superior Court")
    assert {"Sam Third", "Lee Ward", "Kim Post"} <= set(a) and a["Kim Post"]["term"] == "January 1, 2032" and a["Sam Third"]["of"] == "of Macon"
    assert "Ann Lee" not in a and "Brian P. Kemp" not in a
    c = press.parse_appointments(CITY)
    assert len(c) == 1 and c[0]["person"] == "Pat Local" and c[0]["body"] == "Planning Commission" and c[0]["term"] == "December 31, 2028"


def test_press_deriver_and_sources(tmp_path):
    store = Store(tmp_path / "data")
    store.add(Record("press:governor", "press_release", "https://gov.example/press-releases/appointments-oct-2", title="Gov. Kemp Announces Appointments",
                     text=RELEASE, published="2026-10-02", channel="html", evidence="primary", meta={"opened": True}))
    store.add(Record("canton:site", "page", "https://canton.example/news/council-appoints", title="Council appoints planning commissioner",
                     text=CITY, published="2026-10-09", jurisdiction="canton", channel="html", evidence="primary", meta={"opened": True}))
    store.add(Record("canton:site", "page", "https://canton.example/parks", title="Parks", text="Fun for all. " * 30, jurisdiction="canton"))
    store.flush()
    out = derive.run(store, only={"press"}, log=lambda *_: None)
    assert out["press"] == {"scanned": 2, "derived": 6, "partial": False}
    apps = {r["meta"]["person"]: r for r in store.query("kind='appointment'")}
    assert apps["Jane Q. Public"]["meta"]["level"] == "ga" and apps["Jane Q. Public"]["published"] == "2026-10-02"
    assert apps["Pat Local"]["jurisdiction"] == "canton" and apps["Pat Local"]["meta"]["level"] == "canton" and apps["Pat Local"]["meta"]["by"] == "Dana Reed"
    assert derive.run(store, only={"press"}, log=lambda *_: None)["press"]["scanned"] == 0
    store.close()
    ids = {s["id"]: s for s in registry.press_sources(ROOT)}
    assert "press:governor" in ids and ids["press:governor"]["kind"] == "press_release" and ids["press:governor"]["level"] == "state"
    assert any(s["id"] == "press:sos" for s in registry.all_sources(ROOT))


def test_page_date():
    assert extract.page_date('<html><head><meta property="article:published_time" content="2026-10-02T14:00:00-04:00"></head><body>x</body></html>') == "2026-10-02"
    assert extract.page_date("<html><body><p>ATLANTA – October 2, 2026 – Governor Brian P. Kemp today announced</p></body></html>") == "2026-10-02"
    assert extract.page_date('<html><body><time datetime="2026-09-30">Sept. 30</time></body></html>') == "2026-09-30"
    assert extract.page_date("<html><body>nothing</body></html>") is None


def test_derive_keeps_its_place_when_out_of_time(tmp_path, monkeypatch):
    """A pass that runs out of time keeps the last change it reached as its cursor, so the next run carries on
    from there rather than re-reading the same records every night (the old behaviour starved a big backlog)."""
    import time as _time

    store = Store(tmp_path / "data")
    for i in range(6):
        store.add(Record("press:governor", "press_release", f"https://gov.example/press-releases/r{i}", title=f"Release {i}",
                         text=RELEASE, published=f"2026-10-0{i + 1}", channel="html", evidence="primary", meta={"opened": True}))
    store.flush()
    # time stands at 100 until two records have been derived, then jumps past the deadline
    clock = {"t": 100.0, "n": 0}
    derive.run(store, only={"nothing"}, log=lambda *_: None)      # registers the derivers
    d = next(x for x in derive.REGISTRY if x.name == "press")
    real_fn = d.fn

    def slow(src, ctx):
        clock["n"] += 1
        if clock["n"] >= 2:
            clock["t"] = 1000.0
        return real_fn(src, ctx)

    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(d, "fn", slow)
    out = derive.run(store, only={"press"}, deadline=500.0, log=lambda *_: None)
    assert out["press"]["partial"] and 1 <= out["press"]["scanned"] <= 3
    cursor = store.kv_get("derive:press")
    assert cursor and cursor < "2100"          # a real timestamp, one second before the last change reached
    monkeypatch.setattr(d, "fn", real_fn)
    clock["t"] = 100.0
    out2 = derive.run(store, only={"press"}, deadline=500.0, log=lambda *_: None)
    assert not out2["press"]["partial"] and out2["press"]["scanned"] >= 6 - out["press"]["scanned"]
    assert derive.run(store, only={"press"}, log=lambda *_: None)["press"]["scanned"] == 0
    assert len(list(store.query("kind='appointment'"))) == 30    # five appointments per release, each derived once (natural keys)
    store.close()
