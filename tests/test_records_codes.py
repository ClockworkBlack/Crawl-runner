"""records-send without the 15 GB database: which places already have a code comes from state/ordinance_codes.json,
written by the crawl's merge job (`gacrawl records codes`)."""
import json

from gacrawl import openrecords as orr
from gacrawl.store import Record, Store


def test_counts_from_the_database_and_from_the_file(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    for i in range(30):
        store.add(Record("macon:ord", "ordinance", f"https://x.gov/s{i}", title=f"Sec. {i}", text="t", jurisdiction="macon"))
    store.add(Record("ty:ord", "ordinance", "https://y.gov/1", title="one", text="t", jurisdiction="ty"))
    store.flush()
    counts = orr.ordinance_counts(store)
    assert counts == {"macon": 30, "ty": 1}
    assert orr.crawlable_code({"id": "macon"}, store) and not orr.crawlable_code({"id": "ty"}, store)
    (tmp_path / "state").mkdir()
    (tmp_path / orr.CODES_FILE).write_text(json.dumps({"counts": counts}))
    monkeypatch.setattr(orr, "_CODES", None)
    monkeypatch.setattr(orr, "_codes_on_file", lambda root=None, _f=orr._codes_on_file: _f(tmp_path))
    assert orr.crawlable_code({"id": "macon"}) and not orr.crawlable_code({"id": "ty"}) and not orr.crawlable_code({"id": "nowhere"})
    assert orr.crawlable_code({"id": "z", "portals": {"ecode360": "https://ecode360.com/Z"}})
