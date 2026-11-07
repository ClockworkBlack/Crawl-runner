"""Electo's name registry (ga/pipeline_archive/names.json): people as the site shows them."""
import json
from pathlib import Path

from gacrawl.electo import import_electo, resolved_people

ROOT = Path(__file__).resolve().parents[1]
URL = "https://results.example/x"


def _row(eid, i, d, office, county, outcome="won", level="local"):
    return [eid, i, d, office, county, "", 100, 60.0, outcome, level, URL]


RAW = {
    "VINCENT WILLIAMS": {"n": "Vincent Williams", "r": [
        _row("2026May", 1, "2026-05-19", "State House District 117 (Democratic primary)", "", "", "legislative"),
        _row("2025Nov", 2, "2025-11-04", "Mayor - Brunswick", "Glynn", "")]},
    "PAT L LOCAL": {"n": "Pat L. Local", "r": [_row("2021Nov", 1, "2021-11-02", "Council Member Ward 1 - Abbeville", "Wilcox")]},
    "PAT LOCAL": {"n": "Pat Local", "r": [_row("2025Nov", 1, "2025-11-04", "Council Member Ward 1 - Abbeville", "Wilcox")]},
}
NAMES = {"people": {
    "VINCENT WILLIAMS": {"n": "Vincent Williams", "keys": ["VINCENT WILLIAMS"], "same_name": ["VINCENT WILLIAMS (BRUNSWICK)"]},
    "VINCENT WILLIAMS (BRUNSWICK)": {"n": "Vincent T. Williams (Brunswick)", "keys": ["VINCENT WILLIAMS (BRUNSWICK)"],
                                     "same_name": ["VINCENT WILLIAMS"],
                                     "moved_in": [{"from": "VINCENT WILLIAMS", "i": 0, "o": "Mayor - Brunswick", "c": "Glynn"}]},
    "PAT LOCAL": {"n": "Pat Local", "aka": ["Pat L. Local"], "keys": ["PAT L LOCAL", "PAT LOCAL"]},
}}


def test_resolved_people_splits_and_aliases():
    P = resolved_people(RAW, NAMES)
    assert [r[3] for r in P["VINCENT WILLIAMS"]["r"]] == ["State House District 117 (Democratic primary)"]
    assert [r[3] for r in P["VINCENT WILLIAMS (BRUNSWICK)"]["r"]] == ["Mayor - Brunswick"]
    assert sorted(r[2] for r in P["PAT LOCAL"]["r"]) == ["2021-11-02", "2025-11-04"] and P["PAT LOCAL"]["aka"] == ["Pat L. Local"]
    assert "PAT L LOCAL" not in P


def test_import_uses_the_registry(tmp_path):
    e = tmp_path / "electo"
    arc = e / "ga" / "pipeline_archive"
    arc.mkdir(parents=True)
    (arc / "people.json").write_text(json.dumps({"people": RAW}))
    (arc / "aliases.json").write_text(json.dumps({"PAT L LOCAL": "PAT LOCAL"}))
    (arc / "splits.json").write_text(json.dumps({"VINCENT WILLIAMS": [{"o": "Mayor - Brunswick", "c": "Glynn", "to": "VINCENT WILLIAMS (BRUNSWICK)"}]}))
    (arc / "names.json").write_text(json.dumps(NAMES))
    (e / "ga" / "councils.json").write_text(json.dumps({"counties": {"wilcox": {"Abbeville": {"k": "city", "c": [
        {"d": "2021-11-02", "t": "Council Member Ward 1 - Abbeville", "o": [["PAT L LOCAL", "Pat L. Local", 90, 55.0, "won"]]}]}}}}))
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    for f in ("jurisdictions.yaml", "outlets.yaml", "topics.yaml"):
        (root / "config" / f).write_text((ROOT / "config" / f).read_text())
    meta = import_electo(e, root, log=lambda *_: None)
    people = json.loads((root / "state" / "electo_people.json").read_text())["people"]
    assert meta["people"] == 3 and set(people) == {"VINCENT WILLIAMS", "VINCENT WILLIAMS (BRUNSWICK)", "PAT LOCAL"}
    vb = people["VINCENT WILLIAMS (BRUNSWICK)"]
    assert [r["office"] for r in vb["races"]] == ["Mayor - Brunswick"] and vb["split"] and vb["same_name"] == ["VINCENT WILLIAMS"]
    assert "co-glynn" in vb["jurisdictions"] and "co-glynn" not in people["VINCENT WILLIAMS"]["jurisdictions"]
    assert people["PAT LOCAL"]["n_races"] == 2 and "abbeville" in people["PAT LOCAL"]["jurisdictions"]
