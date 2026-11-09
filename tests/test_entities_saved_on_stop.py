"""Entity resolution saves the entity registry even when it stops partway. The store commits the lines it updates as
it goes, and those lines name the new entity ids; an unsaved registry hands the same ids to other names next run
(new ids are a counter). Nov 9: the Georgia money pass crashed partway and the next run reused its ids.
Fictional donors throughout."""
import json

import pytest

from gacrawl import finance
from gacrawl.store import Store

DONORS = ["Avery Quill", "Blake Ormond", "Casey Thistle", "Drew Pellam"]


def _line(i, name):
    row = {"name": name, "amount": 100.0 + i, "date": "2026-10-01", "line": i}
    return finance.contribution_record("cand:pat-test", f"https://example.test/report/{i}", row, "Pat Test", "candidate",
                                       "Test filing source")


def _store(tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    store = Store(root / "data")
    for i, name in enumerate(DONORS):
        store.add(_line(i, name))
    store.flush()
    return root, store


def _donor_ids(store):
    return {c["meta"]["name"]: c["meta"].get("donor_entity") for c in store.query("kind='contribution'")}


@pytest.mark.parametrize("stop", [IndexError("a donor name the resolver cannot handle"), KeyboardInterrupt()])
def test_registry_saved_when_resolution_stops_partway(tmp_path, monkeypatch, stop):
    root, store = _store(tmp_path)
    fin = finance.Finance(root, {}, None, store, log=lambda *_: None)
    real = fin.entities.resolve

    def resolve(name, kind=None, attrs=None, url=None):
        if name == "Casey Thistle":           # a crash, or the step's watchdog SIGINT, in the middle of the pass
            raise stop
        return real(name, kind, attrs, url=url)

    monkeypatch.setattr(fin.entities, "resolve", resolve)
    with pytest.raises(type(stop)):
        fin.resolve_all([])
    store.flush()                            # what the command's own `finally: store.close()` does

    named = {v for v in _donor_ids(store).values() if v}
    assert named, "the lines before the stop were resolved and committed"
    saved = json.loads(store.kv_get(fin.entities.KV) or '{"entities": {}}')
    assert named <= set(saved["entities"]), "every id a stored line names is in the saved registry"

    # the next run resolves the rest without handing out an id already in use
    finance.Finance(root, {}, None, store, log=lambda *_: None).resolve_all([])
    ids = _donor_ids(store)
    assert all(ids.values()) and len(set(ids.values())) == len(DONORS)


def test_lines_naming_ids_this_database_does_not_hold_are_linked_again(tmp_path):
    """The repair for what a stopped pass left behind before the fix above, and for lines linked on another copy of
    the database (the parallel FEC re-read's hand-off): an id the registry lacks, or holds for someone else."""
    root, store = _store(tmp_path)
    fin = finance.Finance(root, {}, None, store, log=lambda *_: None)
    fin.resolve_all([])
    good = _donor_ids(store)
    assert fin.misplaced_links() == []

    # Avery's line names an id no registry entry has; Blake's names Casey's entity; Drew's employer id is foreign
    for r in store.query("kind='contribution'"):
        m = dict(r["meta"])
        if m["name"] == "Avery Quill":
            m["donor_entity"] = "p99999"
        elif m["name"] == "Blake Ormond":
            m["donor_entity"] = good["Casey Thistle"]
        elif m["name"] == "Drew Pellam":
            m["employer"], m["employer_entity"] = "Pellam Tile Works", "o88888"
        else:
            continue
        store.add(finance.Record(r["source_id"], r["kind"], r["url"], title=r["title"], text=r["text"],
                                 published=r.get("published"), channel=r.get("channel") or "", evidence=r.get("evidence") or "",
                                 meta=m, key=r["key"]))
    store.flush()

    fin = finance.Finance(root, {}, None, store, log=lambda *_: None)
    assert len(fin.misplaced_links()) == 3
    out = fin.resolve_all([])
    assert out["relinked"] == 3
    ids = _donor_ids(store)
    assert ids["Avery Quill"] == good["Avery Quill"] and ids["Blake Ormond"] == good["Blake Ormond"]
    assert ids["Casey Thistle"] == good["Casey Thistle"]
    drew = next(c for c in store.query("kind='contribution'") if c["meta"]["name"] == "Drew Pellam")
    assert drew["meta"]["employer_entity"] in fin.entities.entities
    assert fin.misplaced_links() == []
    # a clean database costs nothing: nothing to link again on the next pass
    assert finance.Finance(root, {}, None, store, log=lambda *_: None).resolve_all([])["resolved"] == 0
