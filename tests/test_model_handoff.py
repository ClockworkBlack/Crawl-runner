import json

from gacrawl import model_handoff as mh
from gacrawl.candidates import RumorMill
from gacrawl.store import Store


def test_round_trip(tmp_path):
    job, crawl = tmp_path / "job", tmp_path / "crawl"
    for root in (job, crawl):
        (root / "data" / "rumor_mill").mkdir(parents=True)
        (root / "data" / "train").mkdir(parents=True)
        (root / "data" / "rumor_mill" / "rumors.jsonl").write_text(json.dumps(
            {"id": "r1", "candidate_id": "a", "claim": "x", "url": "https://e.com/1", "domain": "e.com", "status": "unverified"}) + "\n")
        (root / "data" / "train" / "donor_about_log.jsonl").write_text('{"old": 1}\n')
    js = Store(job / "data"); js.kv_set("donor_about", json.dumps({"e0": {"v": 2, "t": "old"}}))
    start = mh.mark(js, job)
    # the model job writes
    js.kv_set("donor_about", json.dumps({"e0": {"v": 2, "t": "old"}, "e1": {"v": 2, "t": "A PAC."}}))
    rm = RumorMill(job); r = rm.all()[0]; r["ai"] = {"v": 1, "named": True}; rm._dirty = True; rm.flush()
    with (job / "data" / "train" / "donor_about_log.jsonl").open("a") as fh:
        fh.write('{"new": 1}\n')
    h = mh.export(js, job, start)
    assert h["kv"]["donor_about"] == {"e1": {"v": 2, "t": "A PAC."}} and h["rumors"] == {"r1": {"v": 1, "named": True}}
    assert h["train"] == {"data/train/donor_about_log.jsonl": ['{"new": 1}']}
    # the crawl applies it, twice: the second time changes nothing
    cs = Store(crawl / "data")
    assert mh.apply(cs, crawl, h) == {"kv": 1, "rumors": 1, "train": 1}
    assert mh.apply(cs, crawl, h) == {"kv": 0, "rumors": 0, "train": 0}
    assert json.loads(cs.kv_get("donor_about"))["e1"]["t"] == "A PAC."
    assert RumorMill(crawl).all()[0]["ai"]["named"] is True
    assert (crawl / "data" / "train" / "donor_about_log.jsonl").read_text().splitlines() == ['{"old": 1}', '{"new": 1}']


def test_apply_writes_only_training_logs(tmp_path):
    (tmp_path / "data" / "rumor_mill").mkdir(parents=True)
    s = Store(tmp_path / "data")
    mh.apply(s, tmp_path, {"train": {"../../evil.jsonl": ["x"], "data/train/../../config/x.jsonl": ["y"]}})
    assert not (tmp_path.parent / "evil.jsonl").exists() and not (tmp_path / "config" / "x.jsonl").exists()
