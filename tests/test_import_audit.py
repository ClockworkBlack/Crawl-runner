"""The database audit at the end of the Electo import (scripts/import_audit.sh, called by crawl.yml after ga/names.py).

Electo's importer audits at its own end, before ga/names.py has put this import's record-only people into the name
registry the audit reads, so the audit file the import committed called them all unknown: on Nov 20, 2026 the file
listed 4 errors, and the same audit on the same commit, run by grand-cross, found 1. The script runs the audit again
after the registry is written. It never stops the import, and its lines in the public runner's log hold counts only."""
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "import_audit.sh"

AUDIT_OK = '''import json, os, sys
os.makedirs("ga/crawl", exist_ok=True)
json.dump({"verdict": "fail", "errors": 1}, open("ga/crawl/data_audit.json", "w"))
print("  WARNING [councils] every roll call has a date: 14 of 11204 e.g. [['co-elbert', 'Adopt the agenda']]")
print("  ERROR   [city results] no reported contest carries another office's candidates: 1 of 228 e.g. [['xcstmarys20261103', 'Governor - St. Marys', 'Rick Jackson ran for Governor']]")
print("data audit: fail, 67 checks, 1 errors, 4 warnings (7.0s)")
sys.exit(1)
'''


def electo(tmp_path: Path, audit: str | None) -> Path:
    e = tmp_path / "electo"
    (e / "tools").mkdir(parents=True)
    (e / "ga" / "crawl").mkdir(parents=True)
    (e / "ga" / "crawl" / "data_audit.json").write_text(json.dumps({"verdict": "fail", "errors": 4}))
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "main"]):
        subprocess.run(["git", *cmd], cwd=e, check=True)
    if audit is not None:
        (e / "tools" / "data_audit.py").write_text(audit)
    return e


def run(e: Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], cwd=e, capture_output=True, text=True, env={**os.environ, **env})


def test_the_file_the_import_commits_is_the_audit_after_the_registry_and_the_log_holds_counts_only(tmp_path):
    e = electo(tmp_path, AUDIT_OK)
    p = run(e)
    assert p.returncode == 0                       # errors found: reported, the import goes on
    assert json.loads((e / "ga/crawl/data_audit.json").read_text()) == {"verdict": "fail", "errors": 1}
    assert p.stdout.splitlines() == [
        "  ERROR   [city results] no reported contest carries another office's candidates: 1 of 228",
        "data audit: fail, 67 checks, 1 errors, 4 warnings (7.0s)"]
    assert "St. Marys" not in p.stdout + p.stderr and "Rick Jackson" not in p.stdout + p.stderr and "WARNING" not in p.stdout


def test_a_passing_audit_prints_its_verdict(tmp_path):
    e = electo(tmp_path, 'print("data audit: pass, 67 checks, 0 errors, 0 warnings (6.1s)")\n')
    p = run(e)
    assert p.returncode == 0 and p.stdout.strip() == "data audit: pass, 67 checks, 0 errors, 0 warnings (6.1s)"


def test_an_audit_that_breaks_leaves_the_importers_file_and_warns(tmp_path):
    e = electo(tmp_path, 'import sys\nprint("reading")\nraise KeyError("runs")\n')
    p = run(e)
    assert p.returncode == 0
    assert "::warning title=Electo import::the database audit did not run to its end after the name registry (exit 1): KeyError: 'runs'" in p.stdout
    assert json.loads((e / "ga/crawl/data_audit.json").read_text())["errors"] == 4          # the importer's own, untouched


def test_an_audit_stopped_while_writing_does_not_leave_half_a_file(tmp_path):
    e = electo(tmp_path, 'import time\nopen("ga/crawl/data_audit.json", "w").write(\'{"verdict": "fa\')\ntime.sleep(30)\n')
    p = run(e, IMPORT_AUDIT_TIMEOUT="2")
    assert p.returncode == 0 and "did not run to its end" in p.stdout and "(exit 124)" in p.stdout
    assert json.loads((e / "ga/crawl/data_audit.json").read_text()) == {"verdict": "fail", "errors": 4}   # as main has it


def test_an_electo_without_the_audit_is_left_alone(tmp_path):
    e = electo(tmp_path, None)
    p = run(e)
    assert p.returncode == 0 and p.stdout.strip() == "no database audit in this Electo"


def test_the_import_runs_it_after_the_name_registry_and_before_it_commits():
    steps = yaml.safe_load((ROOT / ".github/workflows/crawl.yml").read_text())["jobs"]["merge"]["steps"]
    step = [s for s in steps if (s.get("name") or "").startswith("Import into Electo")]
    assert len(step) == 1
    sh = step[0]["run"]
    names, audit, add = sh.index('run_step "ga/names.py"'), sh.index('bash "$AUDIT" || true'), sh.index("git add -A ga/crawl")
    assert names < audit < add
    # the script's path is taken while the step is still in Crawl's checkout (the import works in /tmp/electo-full)
    assert sh.index('AUDIT="$PWD/scripts/import_audit.sh"') < sh.index("cd /tmp/electo-full")
    assert sh.count('bash "$AUDIT"') == 1 and os.access(SCRIPT, os.X_OK)
