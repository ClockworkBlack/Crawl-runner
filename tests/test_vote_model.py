"""The vote model: which bills' roll calls are read, positions fitted to recorded votes, the forward test and the export."""
import json

import pytest

from gacrawl.sources import legis
from gacrawl.store import Record, Store


def test_reached_floor():
    f = legis.reached_floor
    assert f({"number": "HB 5", "chamber": "House", "status": "Senate Read and Referred"})          # crossed over
    assert f({"number": "SB 9", "chamber": "Senate", "status": "Senate Passed/Adopted By Substitute"})
    assert f({"number": "HB 7", "chamber": "House", "status": "House Third Reading Lost"})
    assert f({"number": "HB 8", "chamber": "House", "status": "House Second Readers", "enacted": True})
    assert f({"number": "HB 14", "chamber": "House", "status": "House Date Vetoed by Governor", "vetoed": True})
    assert not f({"number": "HB 6", "chamber": "House", "status": "House Second Readers"})
    assert not f({"number": "SB 2", "chamber": "Senate", "status": "Senate Read and Referred"})
    assert not f({"number": "HR 44", "chamber": "House", "status": "House Read and Adopted"})       # voice, no roll call
    assert not f({"number": "HB 3", "chamber": "House", "status": "House Committee Favorably Reported"})


def _legislature(store, years=(2019, 2020, 2021, 2022, 2023), per_year=30, seed=3):
    """A made-up chamber: 60 members on a left-right line, bills that divide it at different points, and a few members
    (the ones near the middle) who cross. Each bill gets a second roll call a week later."""
    import numpy as np

    rng = np.random.default_rng(seed)
    n = 60
    party = ["R" if i < 34 else "D" for i in range(n)]
    pos = np.array([rng.normal(1.0, 0.45) if p == "R" else rng.normal(-1.0, 0.45) for p in party])
    vid, lid = 0, 0
    for yr in years:
        for k in range(per_year):
            lid += 1
            cut = rng.choice([-3.0, -3.0, -0.2, 0.0, 0.3, 1.2])       # -3: nearly everyone votes Yea
            lead = int(rng.integers(0, 34))
            store.add(Record(legis.SOURCE, "bill", f"https://x.example/legislation/{lid}", title=f"HB {lid}", key=f"{legis.SOURCE}|bill|{lid}",
                             meta={"legislation_id": lid, "number": f"HB {lid}", "session": 1, "chamber": "House", "status": "House Passed/Adopted",
                                   "sponsors": [{"member_id": 1000 + lead, "sequence": 1}, {"member_id": 1000 + int(rng.integers(0, n)), "sequence": 2}],
                                   "house_committee": "Judiciary", "senate_committee": None}))
            for step, cap in enumerate(("PASSAGE", "AGREE TO SENATE SUBSTITUTE")):
                vid += 1
                votes = []
                for i in range(n):
                    p_yes = 1 / (1 + np.exp(-4 * (pos[i] - cut)))
                    votes.append({"person": f"Member {i}", "person_id": 1000 + i, "party": party[i], "district": i + 1,
                                  "vote": "Yea" if rng.random() < p_yes else "Nay"})
                yea = sum(v["vote"] == "Yea" for v in votes)
                store.add(Record(legis.SOURCE, "vote", f"https://x.example/legislation/{lid}", title=f"HB {lid} {cap}", key=f"{legis.SOURCE}|vote|{vid}",
                                 meta={"VoteId": vid, "legislation_id": lid, "bill": f"HB {lid}", "session": 1, "session_name": "made up",
                                       "chamber": "House", "date": f"{yr}-02-{1 + (k % 14) + step * 7:02d}", "vote_name": "House Vote",
                                       "vote_caption": cap, "yea": yea, "nay": n - yea, "substantive": True, "votes": votes}))
    store.flush()
    return pos, party


def test_vote_model_positions_test_and_macro(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    pytest.importorskip("sklearn")
    from gacrawl import vote_model as vm

    assert vm.MIN_ROLL_CALLS <= 60
    store = Store(tmp_path / "data")
    pos, party = _legislature(store)
    out_path = tmp_path / "vote_model.json"
    counts = vm.export_vote_model(store, out_path, log=lambda *_: None)
    out = json.loads(out_path.read_text())
    assert counts["built"] and out["status"] == "tested" and out["built_from"]["roll_calls"] == 300
    # positions: Republicans on the positive side, and in the order the made-up line put them
    m = {x["id"]: x for x in out["members"]}
    assert len(m) == 60 and all((x["position"] > 0) == (x["party"] == "R") for x in m.values() if abs(x["position"]) > 0.5)
    fitted = np.array([m[1000 + i]["position"] for i in range(60)])
    assert np.corrcoef(fitted, pos)[0, 1] > 0.9
    # the forward test: every year after the first, the model next to the plain rules, with its verdict
    t = out["test"]["all"]
    assert set(out["test"]["by_year"]) == {"2020", "2021", "2022", "2023"}
    assert t["questions"] == 60 * 60 * 4 and t["best_rule"] in vm.RULES and t["verdict"]
    assert t["model"]["right"] >= t[t["best_rule"]]["right"] - 0.02 and t["model"]["brier"] < t["always yes"]["brier"]
    later = out["test"]["later_vote_on_the_bill"]
    assert later["model"]["right"] > 0.85                      # the bill's first roll call shows where it divides the chamber
    assert out["test"]["bands"] and all(0 <= b["said"] <= 1 for b in out["test"]["bands"])
    # macro: expected Yea counts land near the recorded ones once the bill has been voted on before
    mac = out["macro"]
    assert mac["roll_calls"] == 240 and mac["mean_miss_share"] < 0.2 and len(mac["newest"]) == 40
    assert all({"bill", "yea", "expected_yea", "u"} <= set(r) for r in mac["newest"])


def test_too_few_roll_calls_gives_positions_only(tmp_path):
    pytest.importorskip("sklearn")
    from gacrawl import vote_model as vm

    store = Store(tmp_path / "data")
    _legislature(store, years=(2024, 2025), per_year=6)
    out = vm.build(store, log=lambda *_: None)
    assert out["test"] is None and out["status"].startswith("positions only") and out["members"]


def test_no_roll_calls(tmp_path):
    pytest.importorskip("sklearn")
    from gacrawl import vote_model as vm

    out = vm.build(Store(tmp_path / "data"), log=lambda *_: None)
    assert out["status"] == "no roll calls read yet" and out["members"] == []
