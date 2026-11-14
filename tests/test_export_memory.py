"""The export's memory (Nov 13, 2026: two merge jobs lost, "lost communication with the server", in the bundle step):
profiles written one at a time give the same files as all at once, the graph's edge keys go once it is built, and the
export's log says how much memory it holds."""
import json

import pytest

from gacrawl.connections import Graph
from gacrawl.entities import EntityIndex
from gacrawl.export import _memory
from gacrawl.profiles import ProfileWriter, write_profiles


def profile(cid, name, inputs, has_new=True, raised=0.0):
    return {"key": f"ga-{cid}", "candidate_id": cid, "name": name, "depth": "full" if inputs > 1 else "thin",
            "inference_inputs": inputs, "positions": [{"t": "x"}] * inputs, "sources": [{"u": "https://example.org/a"}],
            "money": {"total_raised": raised, "donors": [{"id": "d1"}]}, "has_new": has_new}


def test_profiles_one_at_a_time_match_all_at_once(tmp_path):
    ps = [profile("b", "Bea Bell", 3, raised=10.5), profile("a", "Al Abel", 0), profile("c", "Cy Cole", 3, has_new=False),
          profile("d", "Di Dale", 1)]
    n1 = write_profiles([dict(p) for p in ps], tmp_path / "batch", log=lambda *_: None)
    w = ProfileWriter(tmp_path / "stream", log=lambda *_: None)
    for p in ps:
        w.add(dict(p))
    n2 = w.finish()
    assert n1 == n2 == 3                                              # Cy Cole has nothing new: indexed, no file
    names = sorted(f.name for f in (tmp_path / "stream").iterdir())
    assert names == sorted(f.name for f in (tmp_path / "batch").iterdir()) == ["ga-a.json", "ga-b.json", "ga-d.json", "index.json"]
    for f in (tmp_path / "batch").iterdir():
        assert f.read_bytes() == (tmp_path / "stream" / f.name).read_bytes(), f.name
    idx = json.loads((tmp_path / "stream" / "index.json").read_text())
    assert [x["candidate_id"] for x in idx["profiles"]] == ["a", "d", "b", "c"]   # thinnest first, then by name
    assert idx["written"] == 3 and idx["thin"] == 2


def test_graph_is_sealed_once_built():
    g = Graph(EntityIndex())
    assert g.edge("x", "y", "contributed", "https://example.org/f", amount=5.0, line="r1")
    assert g.edge("x", "y", "contributed", "https://example.org/f", amount=5.0, line="r1") is None   # the same line once
    g.seal()
    assert g._edge_keys is None and len(g.edges) == 1 and g.out["x"] and g.inn["y"]
    with pytest.raises(RuntimeError):
        g.edge("x", "z", "contributed", "https://example.org/f", amount=1.0, line="r2")


def test_memory_note_reads_the_process():
    note = _memory()
    assert note == "" or (note.startswith("(memory ") and "GB, at most " in note and note.endswith(" GB so far)"))
