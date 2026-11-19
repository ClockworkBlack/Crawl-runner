"""The FEC re-read's removals reach the crawl database, and every candidate with FEC ids has their FEC money read.

fec-reread works on a copy of the crawl database and hands over only the lines it added or changed. The lines of the
committees it drops as not a candidate's money (joint fundraising, unauthorized) went only in its copy: from Nov 17 to
19, 2026 every fec-reread run began with the same 9,193 old lines "left" and removed them again, and the crawl's report
counted 9,192. Fictional data throughout."""
import datetime as dt
import json

from gacrawl import cli
from gacrawl.finance import Finance, apply_drops, given_fec_ids
from gacrawl.store import Record, Store


class _Fec:
    """A stand-in for the FEC API: a candidate's committees, committee lookups, schedule A pages (one page each)."""

    def __init__(self, coms, pages, others=()):
        self.coms, self.pages, self.others = coms, pages, list(others)

    def __call__(self, path, params=None):
        if path == "/committees/":
            return {"results": self.coms}
        if path.startswith("/committee/") and path.endswith("/totals/"):
            return {"results": []}
        if path.startswith("/committee/"):
            cid = path.split("/")[2]
            return {"results": [c for c in self.coms + self.others if c["committee_id"] == cid]}
        if path == "/schedules/schedule_a/":
            return {"results": self.pages.get(params["committee_id"], []), "pagination": {"last_indexes": {}}}
        return {"results": []}


def _old(i, committee, cand, **meta):
    """An FEC line kept before the FEC's own fields were (no fec_v): awaits the re-read."""
    return Record(f"cand:{cand}", "contribution", f"https://docquery.fec.example/{committee}/{i}", title=f"Contribution {i}",
                  key=f"fec|{committee}|{i}", meta={"type": "contribution", "filing_source": "FEC", "committee": committee,
                                                     "candidate_id": cand, "recipient": cand, "name": f"Donor {i}",
                                                     "amount": 10.0, **meta})


def _line(i):
    return {"sub_id": f"S{i}", "transaction_id": f"T{i}", "contributor_name": f"DONOR {i}", "contribution_receipt_amount": 50.0,
            "contribution_receipt_date": "2026-03-01", "line_number": "11AI", "entity_type": "IND"}


def _fill(d):
    s = Store(d)
    for i in range(5):
        s.add(_old(i, "C00000011", "dana-test"))           # unauthorized: not the candidate's money
    for i in range(2):
        s.add(_old(i, "C00000022", "orphan-test"))         # joint fundraising, of a person no longer researched
    for i in range(3):
        s.add(_old(i, "C00000033", "dana-test"))           # the principal campaign committee
    for i in range(2):                                     # someone else's campaign, already re-read: stays
        r = _old(i, "C00000044", "other-test", fec_v=2)
        r.key = f"fec2|C00000044|T{i}"
        s.add(r)
    s.add(Record("ga-ethics", "contribution", "https://ethics.example/1", title="t", key="ga2|x|1",
                 meta={"filing_source": "Georgia State Ethics Commission", "committee": "C00000011", "amount": 5.0}))
    s.close()


def _fin(store, fake):
    f = Finance.__new__(Finance)
    f.root, f.cfg, f.f, f.store, f.log, f.opts = None, {}, object(), store, (lambda *a: None), {}
    f._fec = fake
    return f


def _count(d, committee):
    s = Store(d)
    try:
        return s.db.execute("SELECT count(*) FROM records WHERE kind='contribution' AND json_extract(meta,'$.committee')=? "
                            "AND json_extract(meta,'$.filing_source')='FEC'", (committee,)).fetchone()[0]
    finally:
        s.close()


def _kv(d, k):
    s = Store(d)
    try:
        return s.kv_get(k)
    finally:
        s.close()


def _cli(d, tmp_path, *argv):
    cli.cmd_finance({"data_dir": str(d), "state_dir": str(tmp_path / "state")}, cli.build_parser().parse_args(["finance", *argv]))


def test_the_reread_drops_reach_the_crawl_database(tmp_path, capsys):
    crawl, copy, kvp = tmp_path / "crawl", tmp_path / "copy", tmp_path / "fec-kv.json"
    _fill(crawl)
    _fill(copy)                     # fec-reread's copy of the crawl database, pulled when its run began
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    coms = [{"committee_id": "C00000033", "name": "DANA TEST FOR CONGRESS", "designation": "P"},
            {"committee_id": "C00000011", "name": "FRIENDS OF DANA TEST", "designation": "U"}]
    fake = _Fec(coms, {"C00000033": [_line(1), _line(2)]},
                others=[{"committee_id": "C00000022", "name": "TEST VICTORY FUND", "designation": "J"}])

    # fec-reread: the candidate's own pass, then the orphans pass, in its copy
    s = Store(copy)
    f = _fin(s, fake)
    f.fec({"id": "dana-test", "name": "Dana Test", "fec_id": "H0GA00001"}, "2016-01-01")
    f.reread_orphans(set(), "2016-01-01", None)
    s.close()
    assert _count(copy, "C00000011") == 0 and _count(copy, "C00000022") == 0
    assert _kv(copy, "fec:drop:C00000011") == today and _kv(copy, "fec:drop:C00000022") == today
    _cli(copy, tmp_path, "export-kv", "--kv", str(kvp))
    kv = json.loads(kvp.read_text())
    assert kv["fec:drop:C00000011"] == today and kv["fec:drop:C00000022"] == today and kv["fec:a2:C00000033"]

    # the crawl's merge applies the hand-off: the dropped committees' lines go there too, the drops are kept
    capsys.readouterr()
    _cli(crawl, tmp_path, "apply", "--kv", str(kvp))
    out = capsys.readouterr().out
    assert ("finance: the FEC re-read applied; 10 old lines of re-read committees removed "
            "(7 of them from committees that are not a candidate's money)") in out
    assert _count(crawl, "C00000011") == 0 and _count(crawl, "C00000022") == 0 and _count(crawl, "C00000033") == 0
    assert _count(crawl, "C00000044") == 2                                   # another committee's lines stay
    s = Store(crawl)
    assert s.db.execute("SELECT count(*) FROM records WHERE key='ga2|x|1'").fetchone()[0] == 1   # not an FEC line
    s.close()
    assert _kv(crawl, "fec:drop:C00000011") == today and _kv(crawl, "fec:drop:C00000022") == today

    # the next hand-off carries the same drops (the copy is pulled from the crawl database): nothing left to remove
    _cli(crawl, tmp_path, "apply", "--kv", str(kvp))
    assert "applied; 0 old lines of re-read committees removed\n" in capsys.readouterr().out


def test_a_committee_listed_as_the_candidates_again_is_no_longer_dropped(tmp_path, capsys):
    crawl, copy, kvp = tmp_path / "crawl", tmp_path / "copy", tmp_path / "fec-kv.json"
    for d in (crawl, copy):
        s = Store(d)
        s.kv_set("fec:drop:C00000011", "2026-11-19")
        s.close()
    # the FEC now lists the committee as authorized by the candidate: what is read from it is the candidate's money
    fake = _Fec([{"committee_id": "C00000011", "name": "FRIENDS OF DANA TEST", "designation": "A"}], {"C00000011": [_line(7)]})
    s = Store(copy)
    _fin(s, fake).fec({"id": "dana-test", "name": "Dana Test", "fec_id": "H0GA00001"}, "2016-01-01")
    s.close()
    assert _kv(copy, "fec:drop:C00000011") == "" and _count(copy, "C00000011") == 1
    _cli(copy, tmp_path, "export-kv", "--kv", str(kvp))
    assert json.loads(kvp.read_text())["fec:drop:C00000011"] == ""
    s = Store(crawl)                # the line as the hand-off's delta brings it
    r = _old(7, "C00000011", "dana-test", fec_v=2)
    r.key = "fec2|C00000011|T7"
    s.add(r)
    s.close()
    _cli(crawl, tmp_path, "apply", "--kv", str(kvp))
    assert _count(crawl, "C00000011") == 1 and _kv(crawl, "fec:drop:C00000011") == ""


def test_apply_drops_removes_every_dropped_committee_in_one_pass(tmp_path):
    s = Store(tmp_path / "db")
    for c in ("C1", "C2", "C3"):
        for i in range(2):
            s.add(_old(i, c, "x"))
    s.flush()
    kv = {"fec:drop:C1": "2026-11-19", "fec:drop:C2": "2026-11-18", "fec:drop:C3": "", "fec:a2:C3": "2026-11-01",
          "_since": "2026-11-19T00:00:00"}
    assert apply_drops(s, kv) == 4
    left = s.db.execute("SELECT json_extract(meta,'$.committee'), count(*) FROM records GROUP BY 1").fetchall()
    assert [tuple(r) for r in left] == [("C3", 2)]
    assert s.kv_get("fec:drop:C1") == "2026-11-19" and not s.kv_get("fec:drop:C3") and s.kv_get("fec:a2:C3") is None
    s.close()


def test_hand_checked_ids_and_us_representatives_get_their_fec_money_read(tmp_path):
    # Finance.run read FEC money only for a candidate with fec_id or an office matching "U.S. House/Senate" or
    # "congress": ids checked by hand in config/finance.yaml did not count, and "U.S. Representative, District 11"
    # (five sitting members of Congress in config/candidates.yaml) did not match, so their money was never read
    f = Finance.__new__(Finance)
    f.root, f.cfg, f.f, f.store, f.log = tmp_path, {}, object(), Store(tmp_path / "db"), (lambda *a: None)
    f.opts = {"fec": {"candidate_ids": {"sam-hand": ["H8GA99001"], "una-one": "H8GA99003"}}}
    read = []
    f.fec = lambda c, since, deadline=None: read.append(c["id"]) or 0
    f.from_imports = f.from_documents = lambda cands: 0
    f.fec_totals_all = lambda deadline=None: 0
    f.resolve_all = lambda cands: {}
    f.flush = lambda: None
    cands = [{"id": "sam-hand", "name": "Sam Hand", "office": "State Representative, District 71"},   # a federal race before
             {"id": "rita-rep", "name": "Rita Rep", "office": "U.S. Representative, District 11"},
             {"id": "hal-house", "name": "Hal House", "office": "U.S. House District 9"},
             {"id": "fay-ids", "name": "Fay Ids", "office": "", "fec_ids": ["S0GA99002"]},
             {"id": "una-one", "name": "Una One", "office": "Mayor"},
             {"id": "stan-state", "name": "Stan State", "office": "State Senate District 4"}]
    f.run(cands, orgs=False)
    assert read == ["sam-hand", "rita-rep", "hal-house", "fay-ids", "una-one"]
    assert given_fec_ids({"id": "una-one"}, f.opts) == ["H8GA99003"]   # one id written without a list is one id
    assert f.fec_candidate_ids({"id": "sam-hand", "fec_id": "H8GA99001"}) == ["H8GA99001"]
    f.store.close()
