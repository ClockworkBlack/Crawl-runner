"""connections.json at Georgia's scale: one chain per path, one edge per donor and recipient, no private individuals'
gifts, each kind of pattern capped (Electo's repository refuses files over 100 MB). Fictional names throughout."""
import json

from gacrawl import connections
from gacrawl.entities import EntityIndex


def _graph():
    g = connections.Graph(EntityIndex(None))
    for i, k, n in (("o1", "org", "Acme Holdings"), ("m1", "committee", "Peach PAC"), ("c1", "candidate", "Pat Local"),
                    ("c2", "candidate", "Dana Reed"), ("p1", "person", "Jane Doe"), ("o2", "org", "Acme Builders")):
        g.node(i, kind=k, name=n)
    for d in range(1, 101):        # a hundred lines from the org to the PAC, a hundred from the PAC to each candidate
        g.edge("o1", "m1", "contributed", f"https://e.com/o1/{d}", amount=10.0, date=f"2026-01-{d % 28 + 1:02d}", line=d)
        g.edge("m1", "c1", "contributed", f"https://e.com/m1/{d}", amount=5.0 + (d == 7), date=f"2026-02-{d % 28 + 1:02d}", line=1000 + d)
        g.edge("m1", "c2", "contributed", f"https://e.com/m2/{d}", amount=1.0, date="2026-03-01", line=2000 + d)
    g.edge("p1", "c1", "contributed", "https://e.com/p1", amount=50.0, date="2026-04-01", line=9001)
    g.edge("p1", "o2", "employed_by", "https://e.com/p1")
    return g


def test_one_org_chain_per_path():
    g = _graph()
    chains = [p for p in connections.patterns(g) if p["kind"] == "org_chain"]
    assert len(chains) == 2                                    # o1 -> m1 -> c1 and o1 -> m1 -> c2 (not 20,000)
    c1 = next(p for p in chains if p["path"][2]["id"] == "c1")
    assert c1["amounts"] == [1000.0, 501.0] and c1["src"][1] == "https://e.com/m1/7"     # leg totals; largest line's filing


def test_the_file_is_the_graph_electo_reads(tmp_path):
    g = _graph()
    pats = connections.patterns(g)
    counts = connections.export_graph(g, pats, tmp_path / "c.json")
    doc = json.loads((tmp_path / "c.json").read_text())
    assert counts["edges"] == 302 and counts["file"]["edges"] == 3 == len(doc["edges"])
    e = next(x for x in doc["edges"] if (x["a"], x["b"]) == ("m1", "c1"))
    assert e["amount"] == 501.0 and e["n"] == 100 and e["first"] == "2026-02-01" and e["date"] == "2026-02-28"
    assert "p1" not in {n["id"] for n in doc["nodes"]} or any(p.get("donor") == "p1" for p in doc["patterns"])
    assert not [x for x in doc["edges"] if "p1" in (x["a"], x["b"])]      # a private person's gift: in donors.json, not here


def test_patterns_capped_per_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "PATTERN_CAP", 1)
    g = _graph()
    pats = connections.patterns(g)
    connections.export_graph(g, pats, tmp_path / "c.json")
    kept = [p for p in json.loads((tmp_path / "c.json").read_text())["patterns"] if p["kind"] == "org_chain"]
    assert len(kept) == 1 and kept[0]["path"][2]["id"] == "c1"            # the larger chain


def test_the_file_stays_under_the_size_cap(tmp_path, monkeypatch):
    """Over the cap (GitHub's 100 MB; Nov 9: 113 MB and Electo refused the push), the smallest gift edges go first."""
    g = _graph()
    pats = connections.patterns(g)
    full = connections.export_graph(g, pats, tmp_path / "full.json")["file"]["bytes"]
    monkeypatch.setenv("CONNECTIONS_MAX_MB", str((full - 50) / 1_000_000))
    counts = connections.export_graph(g, pats, tmp_path / "c.json")
    doc = json.loads((tmp_path / "c.json").read_text())
    assert counts["file"]["bytes"] <= full - 50 and doc["counts"]["trimmed"]["money_edges"] >= 1
    kept = {(e["a"], e["b"]) for e in doc["edges"]}
    assert ("m1", "c2") not in kept and ("o1", "m1") in kept           # the smallest total left out, the largest kept
    assert "largest of 3 gift and payment edges" in doc["note"]
    monkeypatch.delenv("CONNECTIONS_MAX_MB")
    connections.export_graph(g, pats, tmp_path / "d.json")
    assert "trimmed" not in json.loads((tmp_path / "d.json").read_text())["counts"]      # under the cap: nothing left out
