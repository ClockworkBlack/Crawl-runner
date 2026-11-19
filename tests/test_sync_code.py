"""sync-code's "Move finished runs' logs" step (sync-code.yml), run in bash against a fake gh and a fake GitHub API.

Until Nov 19, 2026 the step read the run list with an unquoted for-loop, so a run whose name holds spaces ("pages build
and deployment") was split into words: deleted with no line in the archive, and its words sent to DELETE as run ids. A
run that never started (startup_failure) has no logs; the step called that "not ready yet" at every sync and never
recorded or deleted it (crawl run 43097447450, Nov 11 to 19). And LogView, built from the archive, waited for its own
hourly run, which GitHub drops at times."""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "sync-code.yml"
REPO = "ClockworkBlack/Crawl-runner"
NOW = dt.datetime.now(dt.timezone.utc)

pytestmark = pytest.mark.skipif(not (shutil.which("jq") and shutil.which("git") and shutil.which("bash")),
                                reason="needs jq (for gh --jq), git and bash")


def iso(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def step(start: str) -> str:
    steps = yaml.safe_load(WF.read_text())["jobs"]["sync"]["steps"]
    return next(s["run"] for s in steps if s["name"].startswith(start))


FAKE_GH = r'''#!/usr/bin/env python3
"""gh for the test: the run list from runs.json (through jq, as gh --jq does), logs for the runs in logs.json,
a release download that writes status.json when there is one; every call is logged to calls.jsonl."""
import json, os, subprocess, sys
D = os.environ["FAKE_DIR"]
a = sys.argv[1:]
open(os.path.join(D, "calls.jsonl"), "a").write(json.dumps(a) + "\n")
def out(obj):
    if "--jq" in a:
        r = subprocess.run(["jq", "-r", a[a.index("--jq") + 1]], input=json.dumps(obj), capture_output=True, text=True)
        sys.stdout.write(r.stdout); sys.exit(r.returncode)
    print(json.dumps(obj))
if a[:2] == ["release", "download"]:
    src = os.path.join(D, "status.json")
    if not os.path.exists(src):
        sys.exit("release asset not found")
    dest = a[a.index("-D") + 1]; os.makedirs(dest, exist_ok=True)
    open(os.path.join(dest, "runner-status.json"), "w").write(open(src).read()); sys.exit(0)
if a[:2] == ["release", "upload"]:
    sys.exit(0)
if a[:2] == ["workflow", "run"]:
    sys.exit(0)
if a[0] == "api":
    if "-X" in a:                                    # DELETE a run
        sys.exit(0)
    path = [x for x in a[1:] if not x.startswith("-") and a[a.index(x) - 1] not in ("--jq", "-H")][0]
    if path.startswith(f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs?"):
        out(json.load(open(os.path.join(D, "runs.json"))))
    elif "/actions/workflows/logview.yml/runs" in path:
        out({"workflow_runs": []})
    elif path.endswith("/logs"):
        rid = path.split("/")[-2]
        logs = json.load(open(os.path.join(D, "logs.json")))
        if rid not in logs:
            sys.exit("gh: Not Found (HTTP 404)")
        sys.stdout.buffer.write(bytes.fromhex(logs[rid]))
    else:
        sys.exit("gh: Not Found (HTTP 404)")
    sys.exit(0)
sys.exit("fake gh: " + " ".join(a))
'''

FAKE_GIT = r'''#!/bin/bash
# git for the test: a clone is an empty repository of its own, a pull or push does nothing; the rest is git
case "$1" in
  clone) d="${@: -1}"; "$REAL_GIT" init -q "$d"; if [ -n "${CLONE_FROM:-}" ]; then cp -r "$CLONE_FROM"/. "$d"/; fi; exit 0 ;;
  pull) exit 0 ;;
  push) if [ -n "${PUSH_LOG:-}" ]; then "$REAL_GIT" show --name-only --format= HEAD >> "$PUSH_LOG"; fi; exit 0 ;;
esac
if [ "$1" = "-C" ] && [ "$3" = "sparse-checkout" ]; then exit 0; fi
exec "$REAL_GIT" "$@"
'''


class Jobs(BaseHTTPRequestHandler):
    """The two calls of the step's jobs script: a run's jobs and a failed job's error notes."""

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.endswith("/jobs"):
            rid = p.split("/")[-2]
            jobs = [] if rid == "102" else [{"id": 9103, "name": "check", "conclusion": "failure", "steps": []}]
            body = {"jobs": jobs}
        elif p.endswith("/annotations"):
            body = [{"annotation_level": "failure", "message": "Process completed with exit code 1."}]
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def a_zip() -> str:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("check/1_Run.txt", "2026-11-19T10:00:00.0000000Z ##[error]boom\n")
    return b.getvalue().hex()


def test_names_with_spaces_startup_failures_and_logview(tmp_path):
    fake, bin_, work, rt = tmp_path / "fake", tmp_path / "bin", tmp_path / "work", tmp_path / "rt"
    for d in (fake, bin_, work, rt):
        d.mkdir()
    old = NOW - dt.timedelta(days=8)
    runs = [
        {"id": 101, "name": "pages build and deployment", "created_at": iso(2), "updated_at": iso(1.9), "conclusion": "success"},
        {"id": 102, "name": "crawl", "created_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "updated_at": (old + dt.timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ"), "conclusion": "startup_failure"},
        {"id": 103, "name": "results", "created_at": iso(1), "updated_at": iso(0.9), "conclusion": "failure"},
        {"id": 104, "name": "police", "created_at": iso(0.3), "updated_at": iso(0.1), "conclusion": "success"},
        {"id": 999, "name": "sync-code", "created_at": iso(0), "updated_at": iso(0), "conclusion": None},
    ]
    (fake / "runs.json").write_text(json.dumps({"workflow_runs": runs}))
    (fake / "logs.json").write_text(json.dumps({"103": a_zip()}))     # 104's logs are not ready; 102 has none
    # crawl's last outcome as recorded before: a success yesterday; the run of eight days ago must not replace it
    (fake / "status.json").write_text(json.dumps({"crawl": {"fails_in_row": 0, "recent": [5],
                                                            "last": {"id": 5, "at": iso(20), "conclusion": "success"},
                                                            "last_success": {"id": 5, "at": iso(20)}}}))
    (bin_ / "gh").write_text(FAKE_GH)
    (bin_ / "git").write_text(FAKE_GIT)
    for f in ("gh", "git"):
        (bin_ / f).chmod(0o755)
    server = HTTPServer(("127.0.0.1", 0), Jobs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.update({"PATH": f"{bin_}:{Path(sys.executable).parent}:{env.get('PATH', '/usr/bin:/bin')}",
                "REAL_GIT": shutil.which("git"), "FAKE_DIR": str(fake), "GITHUB_REPOSITORY": REPO,
                "GITHUB_RUN_ID": "999", "RUNNER_TEMP": str(rt), "DATA_TOKEN": "data", "GH_TOKEN": "runner",
                "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}", "NO_PROXY": "127.0.0.1",
                "HOME": str(tmp_path)})
    script = tmp_path / "step.sh"
    script.write_text(step("Move finished runs' logs"))
    try:
        p = subprocess.run(["bash", "-e", str(script)], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    finally:
        server.shutdown()
    assert p.returncode == 0, p.stdout + p.stderr
    calls = [json.loads(x) for x in (fake / "calls.jsonl").read_text().splitlines()]
    deleted = sorted(c[-1].rsplit("/", 1)[-1] for c in calls if c[:3] == ["api", "-X", "DELETE"])
    assert deleted == ["101", "102", "103"], p.stdout          # not 104 (logs not ready), not "build", "and"...
    assert "logs for 104 not ready yet" in p.stdout
    assert "run 102 (crawl, startup_failure) has no logs: recorded without them" in p.stdout

    logs = work / "logs-repo" / "logs" / "runner"
    lines = {}
    for f in logs.glob("*/runs-*.jsonl"):
        for x in f.read_text().splitlines():
            lines[json.loads(x)["id"]] = json.loads(x)
    assert set(lines) == {101, 102, 103}
    assert lines[101]["workflow"] == "pages build and deployment" and lines[101]["logs"] is None
    assert lines[102]["conclusion"] == "startup_failure" and lines[102]["logs"] is None
    assert (logs / old.strftime("%Y-%m") / f"runs-{old.strftime('%Y-%m-%d')}.jsonl").exists()
    assert lines[103]["logs"] == f"{runs[2]['created_at'][:10]}_results_103.zip"
    jj = json.loads((logs / old.strftime("%Y-%m") / f"{old.strftime('%Y-%m-%d')}_crawl_102.jobs.json").read_text())
    assert jj["jobs"] == [] and jj["conclusion"] == "startup_failure" and "never started" in jj["note"]

    st = json.loads((work / "status" / "runner-status.json").read_text())
    assert st["crawl"]["last"]["id"] == 5 and st["crawl"]["fails_in_row"] == 0     # the old run came late: not the last
    assert 102 in st["crawl"]["recent"]
    assert st["pages build and deployment"]["last"]["conclusion"] == "success"
    assert st["results"]["fails_in_row"] == 1

    # LogView collects what was just saved, in this repository (-R: there is no checkout here)
    assert ["workflow", "run", "logview.yml", "-R", REPO, "--ref", "main"] in calls
    assert "saved 1 run log(s)" in p.stdout


COLLECTOR = r"""import json, os, pathlib, sys
pathlib.Path("electo/2026-11-19").mkdir(parents=True, exist_ok=True)
pathlib.Path("electo/2026-11-19/index.json").write_text("{}")
pathlib.Path("collect-status.json").write_text(json.dumps({"could_not_read": {"electo-data": {"runs": 2}}}))
print("::warning title=LogView::electo-data: gh: Bad credentials (HTTP 401)")
print("collected 1 run(s); could not read electo-data (nor in the run before: electo-data)")
sys.exit(int(os.environ.get("COLLECT_EXIT", "3")))
"""


@pytest.mark.parametrize("code", [3, 0, 1])
def test_logview_saves_what_it_collected_then_fails_on_a_project_unread_twice(tmp_path, code):
    """logview.yml: collect.py's exit 3 (a project not read in this run and the one before) is a failed run, after
    what the others gave is committed and pushed. The step's shell (bash -e) stopped at the collector's failed exit
    before "rc=$?" was read, so until Nov 19 no exit of the collector reached its error message."""
    bin_, stub, rt = tmp_path / "bin", tmp_path / "stub", tmp_path / "rt"
    for d in (bin_, stub / "tools", rt):
        d.mkdir(parents=True)
    (stub / "tools" / "collect.py").write_text(COLLECTOR)
    (bin_ / "git").write_text(FAKE_GIT)
    (bin_ / "git").chmod(0o755)
    run = yaml.safe_load((ROOT / ".github" / "workflows" / "logview.yml").read_text())["jobs"]["collect"]["steps"][0]["run"]
    (tmp_path / "step.sh").write_text(run)
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.update({"PATH": f"{bin_}:{Path(sys.executable).parent}:{env.get('PATH', '/usr/bin:/bin')}",
                "REAL_GIT": shutil.which("git"), "CLONE_FROM": str(stub), "GH_TOKEN": "read", "WRITE": "write",
                "DAYS": "3", "RUNNER_TEMP": str(rt), "COLLECT_EXIT": str(code), "HOME": str(tmp_path),
                "PUSH_LOG": str(tmp_path / "pushed.txt")})    # what the pushed commit holds (the clone is removed at the end)
    p = subprocess.run(["bash", "-e", str(tmp_path / "step.sh")], cwd=tmp_path, env=env, capture_output=True, text=True,
                       timeout=60)
    out = p.stdout + p.stderr
    if code == 1:                    # every project unread: stops at once, as before
        assert p.returncode == 1 and "the collector stopped (exit 1)" in out and "pushed" not in out
        return
    assert "pushed to LogView" in out, out
    assert set((tmp_path / "pushed.txt").read_text().split()) == {"collect-status.json", "electo/2026-11-19/index.json"}
    assert "some projects could not be read" in out
    if code == 3:
        assert p.returncode == 1 and "could not be read in two runs in a row" in out
    else:
        assert p.returncode == 0 and "two runs in a row" not in out
