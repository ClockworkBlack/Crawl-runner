import json

from gacrawl import model_handoff as mh
from gacrawl.candidates import RumorMill
from gacrawl.store import Record, Store


def _st(**kw):
    return {k: kw.get(k, 0) for k in mh.PARTS}


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
    assert h["kv"]["donor_about"] == {"e1": {"v": 2, "t": "A PAC."}} and h["rumors"] == {"k:a|https://e.com/1": {"v": 1, "named": True}}
    assert h["train"] == {"data/train/donor_about_log.jsonl": ['{"new": 1}']}
    # the crawl applies it, twice: the second time changes nothing
    cs = Store(crawl / "data")
    assert mh.apply(cs, crawl, h) == _st(kv=1, rumors=1, train=1)
    assert mh.apply(cs, crawl, h) == _st()
    assert json.loads(cs.kv_get("donor_about"))["e1"]["t"] == "A PAC."
    assert RumorMill(crawl).all()[0]["ai"]["named"] is True
    assert (crawl / "data" / "train" / "donor_about_log.jsonl").read_text().splitlines() == ['{"old": 1}', '{"new": 1}']


def test_apply_writes_only_training_logs(tmp_path):
    (tmp_path / "data" / "rumor_mill").mkdir(parents=True)
    s = Store(tmp_path / "data")
    mh.apply(s, tmp_path, {"train": {"../../evil.jsonl": ["x"], "data/train/../../config/x.jsonl": ["y"]}})
    assert not (tmp_path.parent / "evil.jsonl").exists() and not (tmp_path / "config" / "x.jsonl").exists()


def _roots(tmp_path):
    job, crawl = tmp_path / "job", tmp_path / "crawl"
    for root in (job, crawl):
        (root / "data" / "rumor_mill").mkdir(parents=True)
        (root / "data" / "rumor_mill" / "rumors.jsonl").write_text(json.dumps(
            {"id": "r1", "candidate_id": "a", "claim": "x", "url": "https://e.com/1", "domain": "e.com", "status": "unverified",
             "meta": {"text": "headline only"}}) + "\n")
    return job, crawl


def test_organization_records_and_rumor_pages_travel(tmp_path):
    """What the organization lookups wrote (organization records) and the Rumor Mill pages the model job read go to
    the crawl; nothing else the job's copy of the database holds does."""
    job, crawl = _roots(tmp_path)
    js = Store(job / "data")
    js.add(Record("org", "organization", "https://www.fec.gov/files/bulk-downloads/2026/cm26.zip", title="Old PAC",
                  text="old", key="org|o0", meta={"v": 0}))
    js.flush()
    js.db.execute("UPDATE records SET changed_at='2026-11-01T00:00:00+00:00'")    # an earlier run's (autocommit)
    start = mh.mark(js, job)
    js.add(Record("org", "organization", "https://www.fec.gov/files/bulk-downloads/2026/cm26.zip", title="A PAC",
                  text="Registered with the FEC", key="org|o1", meta={"registries": ["fec"], "given": 500.0}))
    js.add(Record("x", "page", "https://e.com/p", title="a page the job's copy happens to hold", text="t"))
    js.flush()
    rm = RumorMill(job); r = rm.all()[0]
    r["meta"].update({"text": "Mayor Doe said the vote was close. " * 10, "text_url": "https://e.com/1", "text_tried": "2026-11-09"})
    rm._dirty = True; rm.flush()
    h = mh.export(js, job, start)
    assert [d["key"] for d in h["records"]] == ["org|o1"] and h["records"][0]["meta"]["given"] == 500.0
    assert h["rumor_text"]["k:a|https://e.com/1"]["text_url"] == "https://e.com/1" and mh.has_work(h)
    cs = Store(crawl / "data")
    h["records"].append({"key": "c|1", "source_id": "x", "kind": "contribution", "url": "https://e.com/c", "meta": {}})
    assert mh.apply(cs, crawl, h) == _st(records=1, rumor_text=1)
    assert cs.get_meta("org|o1")["registries"] == ["fec"] and cs.get_meta("c|1") is None   # only organization records
    assert RumorMill(crawl).all()[0]["meta"]["text"].startswith("Mayor Doe")
    assert mh.apply(cs, crawl, h) == _st()
    # the crawl looked the organization up again after the job did: a carried older copy does not replace it
    cs.add(Record("org", "organization", "u", title="A PAC", text="newer", key="org|o1", meta={"registries": ["fec", "irs"]}))
    old = dict(h["records"][0], changed_at="2026-11-01T00:00:00+00:00")
    assert mh.apply(cs, crawl, {"records": [old]})["records"] == 0 and cs.get_meta("org|o1")["registries"] == ["fec", "irs"]
    # a page the crawl read later (longer) is never replaced by a shorter one
    rc = RumorMill(crawl); rc.all()[0]["meta"]["text"] = "x" * 2000; rc._dirty = True; rc.flush()
    assert mh.apply(cs, crawl, h)["rumor_text"] == 0 and len(RumorMill(crawl).all()[0]["meta"]["text"]) == 2000


def test_a_waiting_hand_off_is_carried(tmp_path):
    """A model job that starts while the last hand-off still waits applies it to its copy first and hands over both."""
    job, crawl = _roots(tmp_path)
    waiting = {"kv": {"donor_about": {"e1": {"v": 2, "t": "first"}, "e2": {"v": 2, "t": "kept"}}},
               "records": [{"key": "org|o1", "source_id": "org", "kind": "organization", "url": "u", "title": "A", "meta": {"n": 1}}],
               "rumors": {"r1": {"v": 1, "named": False}}, "rumor_text": {}, "train": {"data/train/x.jsonl": ["a"]}}
    js = Store(job / "data")
    mh.apply(js, job, waiting)
    start = mh.mark(js, job)
    js.kv_set("donor_about", json.dumps({**json.loads(js.kv_get("donor_about")), "e1": {"v": 2, "t": "second"}}))
    h = mh.export(js, job, start, carry=waiting)
    assert h["kv"]["donor_about"] == {"e1": {"v": 2, "t": "second"}, "e2": {"v": 2, "t": "kept"}}   # this job's wins
    # the waiting one's reading (by id, as hand-offs were before Nov 9) and this copy's (by candidate and url)
    assert [d["key"] for d in h["records"]] == ["org|o1"] and h["rumors"] == {"r1": {"v": 1, "named": False}, "k:a|https://e.com/1": {"v": 1, "named": False}}
    assert h["train"] == {"data/train/x.jsonl": ["a"]}
    assert not mh.has_work({"kv": {"donor_about": {}}, "records": [], "rumors": {}, "rumor_text": {}, "train": {}})


def test_news_found_on_its_own_runner_reaches_the_crawl(tmp_path):
    """The candidate-news job's articles, leads, Rumor Mill items and news progress go to the crawl. Rumor Mill items
    travel by candidate and url: the crawl added its own item (the same next id) while the job ran."""
    from gacrawl.scheduler import Progress

    job, crawl = _roots(tmp_path)
    js = Store(job / "data")
    start = mh.mark(js, job)
    js.add(Record("cand:a", "article", "https://news.example/1", title="Doe wins", text="Doe won", key="art|1"))
    js.add(Record("cand:a", "lead", "https://news.example/2", title="Doe", text="", key="lead|2"))
    js.add(Record("x", "article", "https://news.example/3", title="not a candidate's", text="t", key="art|3"))
    js.flush()
    rj = RumorMill(job)
    new = rj.add("a", "https://blog.example/rumor", "Doe to resign?", origin="article", meta={"text": "Doe may resign."})
    new["ai"] = {"v": 1, "named": True}; rj._dirty = True; rj.flush()
    assert new["id"] == "r00002"
    prog = {"news:a": {"last_ok": "2099-01-01T00:00:00+00:00", "counts": {"rumor_mill": 1}},
            "news:old": {"last_ok": "2000-01-01T00:00:00+00:00"}, "cand:a": {"last_ok": "2099-01-01T00:00:00+00:00"}}
    h = mh.export(js, job, start, progress=prog)
    assert sorted(d["key"] for d in h["records"]) == ["art|1", "lead|2"]
    assert list(h["rumor_rows"]) == ["a|https://blog.example/rumor"] and h["rumor_rows"]["a|https://blog.example/rumor"]["ai"]["named"]
    assert h["rumors"] == {} and list(h["progress"]) == ["news:a"] and mh.has_work(h)
    # meanwhile the crawl found an item of its own (r00002 there too)
    cs = Store(crawl / "data")
    rc = RumorMill(crawl); rc.add("b", "https://other.example/x", "other", origin="article"); rc.flush()
    pp = crawl / "state" / "candidate_progress.json"
    st = mh.apply(cs, crawl, h, progress_path=pp)
    assert st == _st(records=2, rumor_rows=1, progress=1)
    rows = {r["url"]: r for r in RumorMill(crawl).all()}
    assert rows["https://other.example/x"]["candidate_id"] == "b" and not rows["https://other.example/x"].get("ai")
    got = rows["https://blog.example/rumor"]
    assert got["id"] == "r00003" and got["ai"]["named"] and got["meta"]["text"] == "Doe may resign." and got["claim"] == "Doe to resign?"
    assert cs.get_meta("art|1") is not None and Progress(pp).get("news:a")["counts"] == {"rumor_mill": 1}
    assert mh.apply(cs, crawl, h, progress_path=pp) == _st()     # twice: nothing changes
    # carried into the next job's hand-off with the next job's own
    later = {"rumor_rows": {"a|https://blog.example/rumor": {"candidate_id": "a", "url": "https://blog.example/rumor",
                                                              "meta": {"text_url": "https://blog.example/rumor"}}},
             "progress": {"news:a": {"last_ok": "2099-02-01T00:00:00+00:00"}}}
    m = mh.merge(h, later)
    assert m["rumor_rows"]["a|https://blog.example/rumor"]["meta"] == {"text": "Doe may resign.", "text_url": "https://blog.example/rumor"}
    assert m["progress"]["news:a"]["last_ok"].startswith("2099-02") and len(m["records"]) == 2
