"""LegiScan as the backup source: datasets read only when changed, legislators and bills matched to legis.ga.gov,
roll calls stored in the legis shape, the key never written anywhere, both sources compared in the vote model, and
the nightly job's hand-off applied without undoing newer marks."""
import base64
import io
import json
import zipfile

import pytest

from gacrawl import legis_handoff
from gacrawl.sources import legis, legiscan
from gacrawl.store import Record, Store

KEY = "abc123SECRETkey"


def _zip(rolls, people, bills) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in people:
            z.writestr(f"GA/2025-2026_Regular_Session/people/{p['people_id']}.json", json.dumps({"person": p}))
        for b in bills:
            z.writestr(f"GA/2025-2026_Regular_Session/bill/{b['bill_number']}.json", json.dumps({"bill": b}))
        for r in rolls:
            z.writestr(f"GA/2025-2026_Regular_Session/vote/{r['roll_call_id']}.json", json.dumps({"roll_call": r}))
    return base64.b64encode(buf.getvalue()).decode()


class FakeResult:
    def __init__(self, d):
        self.status, self._d, self.content = 200, d, b"x"

    def json(self):
        return self._d


class FakeFetcher:
    def __init__(self, datasets, zips):
        self.datasets, self.zips, self.calls = datasets, zips, []

    def get(self, url, params=None, **kw):
        self.calls.append(dict(params))
        if params["op"] == "getDatasetList":
            return FakeResult({"status": "OK", "datasetlist": self.datasets})
        if params["op"] == "getDataset":
            return FakeResult({"status": "OK", "dataset": {"zip": self.zips[params["id"]]}})
        return FakeResult({"status": "ERROR", "alert": {"message": f"bad op, key={params['key']}"}})


PEOPLE = [{"people_id": 11, "name": "Pat Local", "last_name": "Local", "role": "Rep", "district": "HD-043", "party": "D"},
          {"people_id": 12, "name": "Sam Hill", "last_name": "Hill", "role": "Rep", "district": "HD-044", "party": "R"},
          {"people_id": 13, "name": "Jo New", "last_name": "New", "role": "Rep", "district": "HD-099", "party": "R"}]
BILL = {"bill_id": 501, "bill_number": "HB1", "title": "Safe storage", "state_link": "https://www.legis.ga.gov/legislation/69281",
        "session": {"session_id": 2100, "year_start": 2025},
        "votes": [{"roll_call_id": 9001, "url": "https://legiscan.com/GA/rollcall/HB1/id/9001"}]}
ROLL = {"roll_call_id": 9001, "bill_id": 501, "date": "2025-03-05", "desc": "House Vote #80 PASSAGE", "yea": 2, "nay": 1, "nv": 0,
        "absent": 0, "total": 3, "passed": 1, "chamber": "H",
        "votes": [{"people_id": 11, "vote_id": 1, "vote_text": "Yea"}, {"people_id": 12, "vote_id": 2, "vote_text": "Nay"},
                  {"people_id": 13, "vote_id": 1, "vote_text": "Yea"}]}


def _legislators(store):
    for mid, name, last, dist, party in ((5025, "Pat Local", "Local", 43, "D"), (77, "Sam Hill", "Hill", 44, "R")):
        store.add(Record(legis.SOURCE, "legislator", f"https://www.legis.ga.gov/members/house/{mid}", title=name, key=f"{legis.SOURCE}|legislator|{mid}",
                         meta={"member_id": mid, "name": name, "last": last, "chamber": "House", "district": dist, "party": party, "sessions": []}))
    store.flush()


def test_datasets_matching_records_and_no_key_anywhere(tmp_path):
    store = Store(tmp_path / "data")
    _legislators(store)
    ds = [{"session_id": 2100, "session_name": "2025-2026 Regular Session", "year_start": 2025, "dataset_hash": "h1", "access_key": "ak"},
          {"session_id": 900, "session_name": "2013-2014 Regular Session", "year_start": 2013, "dataset_hash": "old", "access_key": "ak0"}]
    f = FakeFetcher(ds, {2100: _zip([ROLL], PEOPLE, [BILL])})
    out = legiscan.update(f, store, log=lambda *_: None, key=KEY)
    assert out["changed"] == 1 and out["roll_calls"] == 1 and out["calls"] == 2           # the 2013 dataset is not followed
    s = out["sessions"][0]
    assert s["people_matched"] == 2 and s["bills_matched"] == 1
    v = store.query("kind='vote' AND source_id='ga:legiscan'")[0]
    m = v["meta"]
    assert v["url"] == "https://legiscan.com/GA/rollcall/HB1/id/9001" and m["legislation_id"] == 69281 and m["bill"] == "HB 1"
    by = {x["person"]: x for x in m["votes"]}
    assert by["Pat Local"]["person_id"] == 5025 and by["Sam Hill"]["vote"] == "Nay" and by["Jo New"]["person_id"] == "ls:13"
    assert m["check"]["ok"] and m["substantive"] and m["credit"].startswith("LegiScan")
    # the key is in no record, no mark, nothing stored
    dump = json.dumps(store.query("1")) + json.dumps({k: store.kv_get(k) for k in ("legiscan:dataset:2100", "legiscan:read:2100")})
    assert KEY not in dump
    # unchanged next night: one call (the list), nothing downloaded
    out2 = legiscan.update(f, store, log=lambda *_: None, key=KEY)
    assert out2["changed"] == 0 and out2["calls"] == 1


def test_errors_never_carry_the_key():
    assert KEY not in legiscan.redact(f"HTTPSConnectionPool: Max retries exceeded with url: /?key={KEY}&op=getDataset", KEY)
    assert "key=***" in legiscan.redact("url: /?key=zzz&op=x", "")

    class Boom:
        def get(self, url, params=None, **kw):
            raise ConnectionError(f"Max retries exceeded with url: /?key={params['key']}&op={params['op']}")

    out = legiscan.update(Boom(), Store.__new__(Store), log=lambda *_: None, key=KEY)
    assert "error" in out and KEY not in out["error"]


def test_no_key_is_said_plainly(tmp_path, monkeypatch):
    monkeypatch.delenv("LEGISCAN_API_KEY", raising=False)
    out = legiscan.update(FakeFetcher([], {}), Store(tmp_path / "data"), log=lambda *_: None)
    assert "LEGISCAN_API_KEY" in out["skipped"]


def _roll(source, key, lid, date, votes, yea=None, nay=None, check_ok=True, bill="HB 1"):
    y = sum(v["vote"] == "Yea" for v in votes) if yea is None else yea
    n = sum(v["vote"] == "Nay" for v in votes) if nay is None else nay
    return Record(source, "vote", f"https://x.example/{key}", title=key, key=key,
                  meta={"legislation_id": lid, "bill": bill, "chamber": "House", "date": date, "vote_caption": "PASSAGE",
                        "substantive": True, "yea": y, "nay": n, "votes": votes, "check": {"ok": check_ok}})


def test_vote_model_fills_replaces_and_compares(tmp_path):
    pytest.importorskip("numpy")
    from gacrawl import vote_model as vm

    store = Store(tmp_path / "data")
    mem = [{"person": f"M{i}", "person_id": i, "party": "R" if i < 7 else "D"} for i in range(12)]
    yes_r = [dict(m, vote="Yea" if m["party"] == "R" else "Nay") for m in mem]
    flip = [dict(v, vote="Nay") if v["person_id"] == 3 else v for v in yes_r]
    # 1. both have it, one member differs
    store.add(_roll(legis.SOURCE, "a1", 1, "2025-02-01", yes_r))
    store.add(_roll(legiscan.SOURCE, "b1", 1, "2025-02-01", flip, yea=7, nay=5))
    # 2. only LegiScan has it
    store.add(_roll(legiscan.SOURCE, "b2", 2, "2025-02-02", yes_r))
    # 3. legis.ga.gov's list failed its check: LegiScan's copy is used instead
    store.add(_roll(legis.SOURCE, "a3", 3, "2025-02-03", yes_r[:5], yea=7, nay=5, check_ok=False))
    store.add(_roll(legiscan.SOURCE, "b3", 3, "2025-02-03", yes_r))
    store.flush()
    picked, st = vm.pick_roll_calls(store)
    assert sorted(m["src"] + str(m["legislation_id"]) for m in picked) == ["LegiScan2", "LegiScan3", "legis.ga.gov1"]
    assert st["compared"] == 1 and st["member_votes_compared"] == 12 and st["member_votes_agree"] == 11
    assert st["filled_from_legiscan"] == 1 and st["replaced_by_legiscan"] == 1 and st["legis_failed_check"] == 1
    d = st["disagreements"][0]
    assert d["members"] == [{"name": "M3", "legis": "Yea", "legiscan": "Nay"}] and d["tally"] == [[7, 5], [7, 5]]
    out = vm.build(store, log=lambda *_: None)
    assert out["sources"]["used"] == {"legis.ga.gov": 1, "LegiScan": 2} and "CC BY 4.0" in out["sources"]["credit"]


def test_handoff_round_trip_keeps_newer_marks(tmp_path):
    side = Store(tmp_path / "side")
    side.kv_set("legis:votes:1", "2026-11-13"); side.kv_set("legis:votes_try:2", "2"); side.kv_set("legiscan:dataset:2100", "h2")
    side.kv_set("legis:legislation:1033", "2026-11-13")                    # not the job's to hand over
    side.flush()
    out = tmp_path / "kv.json"
    assert legis_handoff.export_kv(side, out, since="2026-11-13T04:13:00")["marks"] == 3
    d = json.loads(out.read_text())
    assert d["_since"] == "2026-11-13T04:13:00" and "legis:legislation:1033" not in d
    main = Store(tmp_path / "main")
    main.kv_set("legis:votes:1", "2026-11-20")                              # the crawl read it again since: kept
    main.kv_set("legis:votes_try:2", "3")
    main.flush()
    r = legis_handoff.apply(main, None, out)
    assert r["marks"] == 1 and main.kv_get("legis:votes:1") == "2026-11-20" and main.kv_get("legis:votes_try:2") == "3"
    assert main.kv_get("legiscan:dataset:2100") == "h2"


def test_bills_legiscan_gave_in_full_wait_behind_the_rest(tmp_path):
    store = Store(tmp_path / "data")
    mem = [{"person": f"M{i}", "person_id": i, "party": "R", "vote": "Yea"} for i in range(12)]
    for lid, num in ((11, "HB 1"), (12, "HB 2"), (13, "HB 3")):
        store.add(Record(legis.SOURCE, "bill", f"https://www.legis.ga.gov/legislation/{lid}", title=num,
                         key=f"{legis.SOURCE}|bill|{lid}",
                         meta={"legislation_id": lid, "session": 1029, "number": num, "enacted": True}))
    store.add(_roll(legiscan.SOURCE, "c1", 11, "2023-02-01", mem))                       # HB 1: LegiScan in full
    store.add(_roll(legiscan.SOURCE, "c2", 12, "2023-02-01", mem))                       # HB 2: one roll call adds up,
    store.add(_roll(legiscan.SOURCE, "c3", 12, "2023-02-02", mem[:5], yea=12, check_ok=False))  # one does not
    store.flush()
    assert legis.legiscan_covered(store) == {11}
    order = [b["meta"]["number"] for _, _, b in legis.votes_todo(store, {}, current=[1031], history=[1029])]
    assert order[-1] == "HB 1" and set(order) == {"HB 1", "HB 2", "HB 3"}
