from pathlib import Path

from gacrawl.candidates import load_candidates


def test_priority_field_kept(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidates.yaml").write_text(
        "candidates:\n- {id: a, name: Ann Able, priority: true, websites: ['https://example.org']}\n- {id: b, name: Bob Baker}\n")
    c = {x["id"]: x for x in load_candidates(tmp_path, include_electo=False)}
    assert c["a"]["priority"] is True and not c["b"].get("priority")
    assert c["a"]["websites"] == ["https://example.org"]


def test_repo_candidates_file_loads():
    root = Path(__file__).resolve().parents[1]
    cs = load_candidates(root, include_electo=False)
    assert cs and all(c.get("name") for c in cs)
    assert any(c.get("priority") for c in cs)
