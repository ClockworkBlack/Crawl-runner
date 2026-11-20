"""The format registry (formats/): every schema of the crawler's data is there, reads, is listed in its index, and is
what the files and the code hold to. A change to a format that does not change its schema fails here.

Why: on Nov. 20, 2026 the formats of 231 kinds of data were written down for the first time (formats/README.md). The
written word had fallen behind the code: 29 record kinds, seven channels and 361 keys of the bundle were in no
document. From now on a format is changed in its schema in the same change as the code (formats/README.md, rule 2)."""
import fnmatch
import glob
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FM = ROOT / "formats"
sys.path.insert(0, str(FM))
import check as C  # noqa: E402

REG = json.loads((FM / "registry.json").read_text())["formats"]
SCHEMAS = sorted(FM.rglob("*.schema.json"))


def tracked(*pats):
    try:
        out = subprocess.run(["git", "ls-files", *pats], capture_output=True, text=True, cwd=ROOT, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not at hand")
    if out.returncode:
        pytest.skip("not a git checkout")
    return out.stdout.split()


def test_every_schema_reads_and_uses_only_what_the_checker_checks():
    assert len(SCHEMAS) >= 200
    for p in SCHEMAS:
        s = C.load_schema(p)                                  # SchemaError on a keyword the checker does not read
        assert s.get("title") and s.get("$schema", "").startswith("https://json-schema.org/"), p.name


def test_the_index_is_what_the_schemas_say():
    import importlib.util

    spec = importlib.util.spec_from_file_location("formats_index", FM / "index.py")
    ix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ix)
    assert ix.main(["--check"]) == 0, "formats/README.md or registry.json is stale: python3 -I formats/index.py"
    assert {e["schema"] for e in REG} == {p.relative_to(FM).as_posix() for p in SCHEMAS}


def test_the_checker_is_the_same_in_every_repository_checked_out_beside():
    for other in ("Electo", "logview", "LogView"):
        for f in ("check.py", "index.py"):
            q = ROOT.parent / other / "formats" / f
            if q.exists():
                assert q.read_text() == (FM / f).read_text(), f"{other}/formats/{f} differs from Crawl's: copy the newer to both"


def test_every_tracked_data_file_has_a_format():
    files = tracked("*.json", "*.jsonl", "*.yaml", "*.yml")
    pats = [e["data"] for e in REG]
    missing = [f for f in files if not f.startswith("formats/") and not any(fnmatch.fnmatch(f, p) for p in pats)]
    assert not missing, f"{len(missing)} data files have no schema in formats/ (add one: formats/README.md): {missing[:8]}"


def test_the_files_in_this_repository_fit_their_formats():
    bad = []
    for e in REG:
        fs = [f for f in sorted(glob.glob(str(ROOT / e["data"]))) if Path(f).is_file() and f.endswith((".json", ".jsonl", ".yaml", ".yml"))]
        step = max(1, len(fs) // 12)                         # a format kept in thousands of files: a spread of them
        for f in fs[::step][:12]:
            errs = C.check_file(FM / e["schema"], f, limit=2)
            if errs:
                bad.append((Path(f).relative_to(ROOT).as_posix(), e["schema"], errs[0]))
    assert not bad, f"{len(bad)} files do not fit their formats: {bad[:5]}"


def test_every_bundle_file_the_export_writes_has_a_format():
    names = set(re.findall(r'["\']([a-z_]+\.json)["\']', (ROOT / "gacrawl" / "export.py").read_text()))
    titles = {e["title"] for e in REG}
    missing = sorted(n for n in names if f"dist/electo/{n}" not in titles and f"state/{n}" not in titles)
    assert not missing, f"bundle files with no schema in formats/bundle/: {missing}"


def test_every_record_kind_the_code_writes_has_a_format():
    kinds = set()
    for f in (ROOT / "gacrawl").rglob("*.py"):
        s = f.read_text()
        kinds |= set(re.findall(r'Record\(\s*[^,()]+,\s*"([a-z_]+)"', s)) | set(re.findall(r'Record\([^)]*?\bkind="([a-z_]+)"', s, re.S))
    have = {e["title"] for e in REG if e["area"] == "records"}
    assert len(kinds) >= 30 and not sorted(kinds - have), f"record kinds with no schema in formats/records/: {sorted(kinds - have)}"


def test_the_state_meetings_bundle_holds_to_the_codes_own_lists():
    from gacrawl import agencies as A

    s = C.load_schema(FM / "bundle" / "state_meetings.schema.json")
    b = s["properties"]["bodies"]["items"]["properties"]
    assert s["x-pinned"] and s["properties"]["format"]["const"] == A.FMT
    assert b["kind"]["enum"] == list(A.KINDS) and b["source"]["enum"] == ["live", "archive"]
    assert b["meetings"]["items"]["properties"]["st"]["enum"] == list(A.STATUSES)
    assert s["x-keys-in-order"] == {"bodies[]": list(A.BODY_KEYS), "bodies[].meetings[]": list(A.MEETING_KEYS)}


def test_a_file_that_does_not_fit_says_where_and_why(tmp_path):
    s = {"type": "object", "required": ["d"], "additionalProperties": False,
         "properties": {"d": {"type": "string", "format": "date"}, "st": {"enum": ["held", "scheduled"]}}}
    assert C.errors({"d": "2026-11-20", "st": "held"}, s) == []
    assert C.errors({"d": "2026-02-30", "st": "maybe", "z": 1}, s) == ["$.d: '2026-02-30' is no date",
                                                                       "$.st: 'maybe' is not one of ['held', 'scheduled']",
                                                                       "$: key 'z' is not in the format"]
    assert C.errors({"U.S. Senate": 1}, {"type": "object", "additionalProperties": {"type": "string"}}) == ['$["U.S. Senate"]: int where string belongs']
    p = tmp_path / "x.jsonl"
    p.write_text("// written by a test\n" + json.dumps({"d": "2026-11-20"}) + "\n")
    assert C.check_file({"type": "array", "items": s}, p) == []
    with pytest.raises(C.SchemaError):
        C.errors({}, {"type": "object", "maxProperties": 3})       # a keyword the checker does not read is never ignored
