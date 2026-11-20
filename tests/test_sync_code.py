"""sync-code's log steps (sync-code.yml) and logview's step, run in bash against a fake gh and a fake git.

Every finished run of the public runner has its logs stored in the private LogView repository (crawl-runner/<day>/)
by LogView's own collector, and is deleted from the runner only when LogView's pushed commit holds it. Until late
November 2026 the logs went to the private Crawl repository (logs/runner), some jobs' only; one step moves that
archive into LogView, once, and removes it from Crawl.

Kept from the earlier step: until Nov 19, 2026 it read the run list with an unquoted for-loop, so a run whose name
holds spaces ("pages build and deployment") was split into words: deleted with no record, and its words sent to DELETE
as run ids. A run that never started (startup_failure) has no logs; the step called that "not ready yet" at every sync
and never recorded or deleted it (crawl run 43097447450, Nov 11 to 19)."""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "sync-code.yml"
REPO = "ClockworkBlack/Crawl-runner"
NOW = dt.datetime.now(dt.timezone.utc)
STORE, MOVE = "Store finished runs' logs in LogView", "Move the old log archive"
# LogView's own collector, when a checkout of that repository is at hand (LOGVIEW_REPO, or beside this one)
LOGVIEW = Path(os.environ.get("LOGVIEW_REPO") or ROOT.parent / "logview")

pytestmark = pytest.mark.skipif(not (shutil.which("jq") and shutil.which("git") and shutil.which("bash")),
                                reason="needs jq (for gh --jq), git and bash")


def iso(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def step(start: str) -> str:
    steps = yaml.safe_load(WF.read_text())["jobs"]["sync"]["steps"]
    return next(s["run"] for s in steps if s["name"].startswith(start))


FAKE_GH = r'''#!/usr/bin/env python3
"""gh for the test. For the step: the run list from runs.json (through jq, as gh --jq does), a release download that
writes status.json when there is one, uploads and deletions that do nothing, the private repository's logs/runner
(there when the file "archive" is). For LogView's collector: a day's finished runs, a run's jobs (jobs.json, or one
job that ended as the run did), a job's notes (notes.json) and a run's logs (logs.json: hex of a zip, else 404).
Every call is a line of calls.jsonl, with the token it was made with."""
import json, os, re, subprocess, sys
D = os.environ["FAKE_DIR"]
a = sys.argv[1:]
open(os.path.join(D, "calls.jsonl"), "a").write(json.dumps(a + ["token=" + os.environ.get("GH_TOKEN", "")]) + "\n")
def load(name, default):
    try:
        return json.load(open(os.path.join(D, name)))
    except OSError:
        return default
def out(obj):
    if "--jq" in a:
        r = subprocess.run(["jq", "-r", a[a.index("--jq") + 1]], input=json.dumps(obj), capture_output=True, text=True)
        sys.stdout.write(r.stdout); sys.exit(r.returncode)
    print(json.dumps(obj)); sys.exit(0)
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
    repo = os.environ.get("GITHUB_REPOSITORY", "ClockworkBlack/Crawl-runner")
    runs = load("runs.json", {"workflow_runs": []})
    if path.endswith("/contents/logs/runner"):
        if os.path.exists(os.path.join(D, "archive")):
            out([{"name": "2026-11", "type": "dir"}])
        sys.exit("gh: Not Found (HTTP 404)")
    m = re.match(rf"repos/{repo}/actions/runs\?(.*)", path)
    if m and "created=" in m.group(1):               # the collector: the runs of one day that have ended
        day = re.search(r"created=([\d-]+)", m.group(1)).group(1)
        page = re.search(r"[?&]page=(\d+)", path)
        got = [r for r in runs["workflow_runs"] if r["created_at"][:10] == day and r.get("conclusion")]
        out({"workflow_runs": got if not page or page.group(1) == "1" else []})
    if m:
        out(runs)
    m = re.match(rf"repos/{repo}/actions/runs/(\d+)/attempts/\d+/jobs", path)
    if m:
        run = next(r for r in runs["workflow_runs"] if str(r["id"]) == m.group(1))
        jobs = load("jobs.json", {}).get(m.group(1))
        if jobs is None:
            jobs = [{"id": 9000 + run["id"], "name": "job", "conclusion": run["conclusion"], "runner_name": "r",
                     "started_at": run["created_at"], "completed_at": run["updated_at"],
                     "steps": [{"name": "Run", "conclusion": run["conclusion"]}]}]
        out({"jobs": jobs})
    m = re.match(rf"repos/{repo}/check-runs/(\d+)/annotations", path)
    if m:
        out(load("notes.json", {}).get(m.group(1), []))
    m = re.match(rf"repos/{repo}/actions/runs/(\d+)/logs", path)
    if m:
        logs = load("logs.json", {})
        if m.group(1) not in logs:
            sys.exit("gh: Not Found (HTTP 404)")
        sys.stdout.buffer.write(bytes.fromhex(logs[m.group(1)])); sys.exit(0)
    sys.exit("gh: Not Found (HTTP 404)")
sys.exit("fake gh: " + " ".join(a))
'''

FAKE_GIT = r'''#!/bin/bash
# git for the test. A clone is a repository of its own, holding a copy of the folder given for that repository
# (CLONE_FROM_LOGVIEW, CLONE_FROM_CRAWL, else CLONE_FROM) as its first commit; it fails for a repository named in
# CLONE_FAIL. A pull does nothing; a push fails for a repository named in PUSH_FAIL, and is else noted: the repository
# in PUSH_ORDER, the files of the pushed commit in PUSH_LOG. The rest is git.
case "$1" in
  clone)
    d="${@: -1}"; url="${@: -2:1}"; name=$(basename "$url" .git); from="${CLONE_FROM:-}"
    case "$name" in LogView) from="${CLONE_FROM_LOGVIEW:-$from}" ;; Crawl) from="${CLONE_FROM_CRAWL:-$from}" ;; esac
    if [[ " ${CLONE_FAIL:-} " == *" $name "* ]]; then echo "fatal: Authentication failed" >&2; exit 128; fi
    if [ -n "${CLONES:-}" ]; then echo "$name" >> "$CLONES"; fi
    "$REAL_GIT" init -q "$d"; "$REAL_GIT" -C "$d" config fake.name "$name"
    if [ -n "$from" ]; then cp -r "$from"/. "$d"/; fi
    "$REAL_GIT" -C "$d" add -A
    "$REAL_GIT" -C "$d" -c user.name=seed -c user.email=seed@example.invalid commit -q --allow-empty -m seed
    exit 0 ;;
  pull) exit 0 ;;
  push)
    name=$("$REAL_GIT" config fake.name)
    if [[ " ${PUSH_FAIL:-} " == *" $name "* ]]; then echo "remote: Permission denied" >&2; exit 1; fi
    if [ -n "${PUSH_ORDER:-}" ]; then echo "$name" >> "$PUSH_ORDER"; fi
    if [ -n "${PUSH_LOG:-}" ]; then "$REAL_GIT" show --name-only --format= HEAD >> "$PUSH_LOG"; fi
    exit 0 ;;
esac
if [ "$1" = "-C" ] && [ "$3" = "sparse-checkout" ]; then exit 0; fi
exec "$REAL_GIT" "$@"
'''

# LogView's collector, stood in for: it writes what collect.json tells it into crawl-runner/ (rows of each day's
# index.json, files), prints lines the public log must not show, then its count
STUB_COLLECTOR = r"""import json, os, pathlib, sys
plan = json.load(open(os.path.join(os.environ["FAKE_DIR"], "collect.json")))
open(os.path.join(os.environ["FAKE_DIR"], "collector-args.json"), "w").write(json.dumps(
    {"argv": sys.argv[1:], "token": os.environ.get("GH_TOKEN"), "isolated": bool(sys.flags.isolated)}))
for day, rows in plan.get("rows", {}).items():
    d = pathlib.Path("crawl-runner") / day
    d.mkdir(parents=True, exist_ok=True)
    idx = json.loads((d / "index.json").read_text()) if (d / "index.json").exists() else {"runs": {}}
    idx["runs"].update(rows)
    (d / "index.json").write_text(json.dumps(idx, indent=1, sort_keys=True))
for path, text in plan.get("files", {}).items():
    p = pathlib.Path("crawl-runner") / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
print("crawl-runner: a clerk's address, clerk@example.gov, was in a log")
print("::warning title=LogView::crawl-runner: PRIVATE-LINE")
if plan.get("exit"):
    print("Traceback: PRIVATE-TRACE")
    sys.exit(plan["exit"])
print(f"collected {plan.get('count', 0)} run(s)")
"""

# LogView's tools/move_runner_archive.py, stood in for: it copies each zip of the archive into crawl-runner/moved/,
# and with --check exits as MOVE_CHECK says
STUB_MOVER = r"""import os, pathlib, shutil, sys
archive = pathlib.Path(sys.argv[sys.argv.index("--archive") + 1])
if "--check" in sys.argv:
    print("a line of a log: PRIVATE-LINE")
    print("checked 2 run(s) of the archive: %s not in LogView's last commit" % os.environ.get("MOVE_CHECK", "0"))
    sys.exit(int(os.environ.get("MOVE_CHECK", "0")))
d = pathlib.Path("crawl-runner/moved"); d.mkdir(parents=True, exist_ok=True)
n = 0
for f in sorted(archive.rglob("*.zip")):
    shutil.copy(f, d / f.name); n += 1
print("a line of a log: PRIVATE-LINE")
if os.environ.get("MOVE_EXIT"):
    sys.exit(int(os.environ["MOVE_EXIT"]))
print(f"moved {n} run log(s) into crawl-runner/ (0 already here; 0 run(s) listed without logs)")
"""


def a_zip(text: str = "2026-11-19T10:00:00.0000000Z ##[error]boom\n") -> str:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("check/1_Run.txt", text)
    return b.getvalue().hex()


def run_(rid, name, concl, started_h, ended_h, attempt=1):
    return {"id": rid, "name": name, "created_at": iso(started_h), "run_started_at": iso(started_h),
            "updated_at": iso(ended_h), "conclusion": concl, "run_attempt": attempt, "event": "schedule",
            "head_branch": "main", "head_sha": "abcdef0123456789", "html_url": f"https://github.com/{REPO}/actions/runs/{rid}"}


def kept(run, **more):
    """The run's row in LogView's index.json as the collector writes it, and its place there (day, base name)."""
    day = run["created_at"][:10]
    base = f"{run['created_at'][11:19].replace(':', '')}-{run['name'].replace(' ', '-')}-{run['id']}"
    row = {"workflow": run["name"], "conclusion": run["conclusion"], "attempt": run["run_attempt"],
           "created": run["created_at"], "ended": run["updated_at"], "details": f"{base}.json"}
    return day, base, row | more


class Runner:
    """A step of sync-code, run with the fakes in a folder of its own."""

    def __init__(self, tmp_path, runs, status=None):
        self.tmp = tmp_path
        self.fake, self.bin, self.work, self.rt, self.lv, self.crawl = (tmp_path / n for n in ("fake", "bin", "work", "rt", "lv", "crawl"))
        for d in (self.fake, self.bin, self.work, self.rt, self.lv / "tools", self.crawl):
            d.mkdir(parents=True)
        (self.fake / "runs.json").write_text(json.dumps({"workflow_runs": runs + [
            {"id": 999, "name": "sync-code", "created_at": iso(0), "updated_at": iso(0), "conclusion": None}]}))
        if status is not None:
            (self.fake / "status.json").write_text(json.dumps(status))
        (self.bin / "gh").write_text(FAKE_GH)
        (self.bin / "git").write_text(FAKE_GIT)
        (self.bin / "sleep").write_text("#!/bin/bash\nexit 0\n")        # the steps wait between tries at a push
        for f in ("gh", "git", "sleep"):
            (self.bin / f).chmod(0o755)
        self.plan = {"rows": {}, "files": {}, "count": 0}

    def collected(self, run, zip_=True, notes=(), **more):
        """The stub collector will store this run: its row, its details and (zip_) its logs."""
        day, base, row = kept(run, **more)
        if zip_:
            row["logs"] = f"{base}.zip"
            self.plan["files"][f"{day}/{base}.zip"] = "zip"
        self.plan["files"][f"{day}/{base}.json"] = json.dumps({"notes": list(notes)})
        self.plan["rows"].setdefault(day, {})[str(run["id"])] = row
        self.plan["count"] += 1
        return row

    def run(self, name, collector=STUB_COLLECTOR, **env_more):
        (self.fake / "collect.json").write_text(json.dumps(self.plan))
        if collector is not None:
            (self.lv / "tools" / "collect.py").write_text(collector)
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update({"PATH": f"{self.bin}:{Path(sys.executable).parent}:{env.get('PATH', '/usr/bin:/bin')}",
                    "REAL_GIT": shutil.which("git"), "FAKE_DIR": str(self.fake), "GITHUB_REPOSITORY": REPO,
                    "GITHUB_RUN_ID": "999", "RUNNER_TEMP": str(self.rt), "DATA_TOKEN": "data", "GH_TOKEN": "runner",
                    "LOGVIEW_WRITE": "lvwrite", "HOME": str(self.tmp), "CLONE_FROM_LOGVIEW": str(self.lv),
                    "CLONE_FROM_CRAWL": str(self.crawl), "CLONES": str(self.tmp / "clones.txt"),
                    "PUSH_ORDER": str(self.tmp / "push-order.txt"), "PUSH_LOG": str(self.tmp / "pushed.txt")})
        env.update(env_more)
        script = self.tmp / "step.sh"
        script.write_text(step(name))
        self.p = subprocess.run(["bash", "-e", str(script)], cwd=self.work, env=env, capture_output=True, text=True, timeout=180)
        self.out = self.p.stdout + self.p.stderr
        return self.p

    def names(self, f):
        p = self.tmp / f
        return p.read_text().split() if p.exists() else []

    @property
    def calls(self):
        p = self.fake / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    @property
    def deleted(self):
        return sorted(c[3].rsplit("/", 1)[-1] for c in self.calls if c[:3] == ["api", "-X", "DELETE"])

    @property
    def status(self):
        return json.loads((self.work / "status" / "runner-status.json").read_text())


def test_runs_are_stored_in_logview_then_deleted(tmp_path):
    runs = [run_(101, "pages build and deployment", "success", 2, 1.9),
            run_(102, "crawl", "startup_failure", 24 * 8, 24 * 8 - 0.1),
            run_(103, "results", "failure", 1, 0.9),
            run_(104, "police", "success", 0.3, 0.1),
            run_(105, "fec-reread", "cancelled", 7, 1.2),
            run_(106, "grand-cross", "skipped", 0.5, 0.5)]
    # crawl's last outcome as recorded before: a success yesterday; the run of eight days ago must not replace it
    r = Runner(tmp_path, runs, status={"crawl": {"fails_in_row": 0, "recent": [5],
                                                 "last": {"id": 5, "at": iso(20), "conclusion": "success"},
                                                 "last_success": {"id": 5, "at": iso(20)}}})
    r.collected(runs[0])
    r.collected(runs[1], zip_=False)                            # never started: no logs exist, the row is final
    r.collected(runs[2])
    r.collected(runs[3], zip_=False, pending=True)              # its logs could not be read yet: asked again next time
    r.collected(runs[4], notes=[{"job": "reread", "level": "failure",
                                 "message": "The job has exceeded the maximum execution time of 5h45m0s"}])
    day, _, row = kept(runs[5])                                 # skipped by GitHub: only counted (a row, no files)
    del row["details"]
    r.plan["rows"].setdefault(day, {})["106"] = row
    assert r.run(STORE).returncode == 0, r.out

    assert r.deleted == ["101", "102", "103", "105", "106"], r.out      # not 104, and not "build", "and"...
    assert "6 finished run(s) here: 5 stored in LogView, 1 waiting for their logs, 0 not stored" in r.out
    # the collector: LogView's own, isolated, for this repository only, with this job's token, as far back as the oldest run
    args = json.loads((r.fake / "collector-args.json").read_text())
    back = (NOW.date() - dt.datetime.fromisoformat(runs[1]["created_at"].replace("Z", "+00:00")).date()).days + 1
    assert args == {"argv": ["--only", "crawl-runner", "--days", str(back)], "token": "runner", "isolated": True}
    # LogView was written (the runs' logs, details and index), and nothing else: not the private Crawl repository
    assert r.names("clones.txt") == ["LogView"] and r.names("push-order.txt") == ["LogView"]
    pushed = set(r.names("pushed.txt"))
    d = runs[2]["created_at"][:10]
    assert f"crawl-runner/{d}/index.json" in pushed and any(f.endswith("-results-103.zip") for f in pushed)
    assert all(f.startswith("crawl-runner/") for f in pushed)
    assert not any(c[:2] == ["workflow", "run"] for c in r.calls)       # logview is no longer started from here
    # this log is public: the collector's count and no other line of it
    assert "collected 5 run(s)" in r.out and "PRIVATE" not in r.out and "clerk@" not in r.out
    assert not (r.rt / "logview").exists() and not (r.rt / "collect.out").exists()   # nothing of LogView left behind

    st = r.status
    assert st["crawl"]["last"]["id"] == 5 and st["crawl"]["fails_in_row"] == 0     # the old run came late: not the last
    assert 102 in st["crawl"]["recent"]
    assert st["pages build and deployment"]["last"]["conclusion"] == "success"
    assert st["results"]["fails_in_row"] == 1
    assert st["police"]["last_success"]["id"] == 104               # recorded when first seen, deleted when stored
    # a job cut at its time limit ends "cancelled": GitHub's note, in the details the collector kept, makes it a failure
    assert st["fec-reread"]["fails_in_row"] == 1 and st["fec-reread"]["last"]["conclusion"] == "cancelled (time limit)"


def test_a_run_logview_does_not_hold_stays_and_the_step_fails(tmp_path):
    runs = [run_(201, "news", "success", 3, 2.5), run_(202, "news", "success", 2, 1.5),
            run_(203, "results", "failure", 1, 0.9, attempt=2), run_(204, "ga-money", "failure", 1, 0.8),
            run_(205, "history", "success", 1, 0.7)]
    r = Runner(tmp_path, runs)
    r.collected(runs[0])
    day, base, row = kept(runs[1])
    r.plan["rows"].setdefault(day, {})["202"] = row | {"logs": f"{base}.zip"}    # a row naming logs that were not stored
    r.collected(runs[2], attempt=1)                              # LogView holds its first attempt, not the one that ended
    r.collected(runs[3], conclusion="cancelled")                 # not as it ended
    #                                                              205: not there at all (an older collector, say)
    assert r.run(STORE).returncode == 1
    assert r.deleted == ["201"], r.out
    assert "5 finished run(s) here: 1 stored in LogView, 0 waiting for their logs, 4 not stored" in r.out
    assert "::error title=Run logs::4 finished run(s) are not in LogView" in r.out
    assert r.status["ga-money"]["fails_in_row"] == 1             # the watchdog still sees them


@pytest.mark.parametrize("why, env, said", [
    ("no secret", {"LOGVIEW_WRITE": ""}, "the LOGVIEW_WRITE secret is not set here"),
    ("token refused", {"CLONE_FAIL": "LogView"}, "could not read LogView with LOGVIEW_WRITE"),
    ("collector stopped", {}, "LogView's collector could not read this repository's runs (exit 2)"),
    ("push refused", {"PUSH_FAIL": "LogView"}, "could not push to LogView"),
])
def test_nothing_is_deleted_when_logview_cannot_be_written(tmp_path, why, env, said):
    runs = [run_(301, "results", "failure", 1, 0.9), run_(302, "news", "success", 2, 1.5)]
    r = Runner(tmp_path, runs)
    r.collected(runs[0])
    r.collected(runs[1])
    if why == "collector stopped":
        r.plan["exit"] = 2
    assert r.run(STORE, **env).returncode == 1, r.out            # a failed run: sync-code's own status and the watchdog say so
    assert f"::error title=Run logs::{said}" in r.out and "no run is deleted" in r.out
    assert r.deleted == [] and "removed run" not in r.out
    assert "2 finished run(s) here: none deleted" in r.out and "PRIVATE" not in r.out
    assert r.names("push-order.txt") == []
    st = r.status                                                # the watchdog still sees the jobs
    assert st["results"]["fails_in_row"] == 1 and st["news"]["last_success"]["id"] == 302
    # the same runs at the next sync, LogView back: recorded once, deleted now
    (r.fake / "status.json").write_text(json.dumps(st))
    (r.fake / "calls.jsonl").unlink()
    r.plan["exit"] = 0
    assert r.run(STORE).returncode == 0, r.out
    assert r.deleted == ["301", "302"] and r.status["results"]["fails_in_row"] == 1


def test_no_step_keeps_logs_in_the_private_crawl_repository():
    steps = yaml.safe_load(WF.read_text())["jobs"]["sync"]["steps"]
    store = step(STORE)
    assert "Crawl.git" not in store and "logs/runner" not in store
    # the only step that still names logs/runner is the one that moves it away
    assert [s["name"] for s in steps if "logs/runner" in s["run"]] == [s["name"] for s in steps if s["name"].startswith(MOVE)]
    assert "git rm -rq logs/runner" in step(MOVE)
    # the code copy still leaves logs/ out of the public repository
    assert "--exclude logs/" in steps[0]["run"] and "pub/logs" in steps[0]["run"]


def archive(r):
    d = r.crawl / "logs" / "runner" / "2026-11"
    d.mkdir(parents=True)
    (d / "2026-11-10_crawl_1.zip").write_bytes(bytes.fromhex(a_zip()))
    (d / "2026-11-11_police_2.zip").write_bytes(bytes.fromhex(a_zip("x\n")))
    (r.crawl / "state").mkdir()
    (r.crawl / "state" / "kept.json").write_text("{}")
    (r.fake / "archive").write_text("")
    (r.lv / "tools" / "move_runner_archive.py").write_text(STUB_MOVER)


def test_the_old_archive_is_moved_to_logview_then_removed_from_crawl(tmp_path):
    r = Runner(tmp_path, [])
    archive(r)
    assert r.run(MOVE, collector=None).returncode == 0, r.out
    assert r.names("push-order.txt") == ["LogView", "Crawl"]                 # LogView first; Crawl only after
    assert set(r.names("pushed.txt")) == {
        "crawl-runner/moved/2026-11-10_crawl_1.zip", "crawl-runner/moved/2026-11-11_police_2.zip",    # added to LogView
        "logs/runner/2026-11/2026-11-10_crawl_1.zip", "logs/runner/2026-11/2026-11-11_police_2.zip"}  # removed from Crawl
    assert "moved 2 run log(s)" in r.out and "checked 2 run(s) of the archive: 0 not in LogView's last commit" in r.out
    assert "logs/runner removed from the private repository" in r.out and "PRIVATE" not in r.out
    assert not (r.rt / "old-logs").exists() and not (r.rt / "logview-move").exists()


@pytest.mark.parametrize("env, said", [
    ({"PUSH_FAIL": "LogView"}, "could not push the archive to LogView"),
    ({"MOVE_CHECK": "1"}, "LogView does not hold the whole archive"),
    ({"MOVE_EXIT": "1"}, "the archive was not moved"),
    ({"CLONE_FAIL": "LogView"}, "could not read LogView with LOGVIEW_WRITE"),
])
def test_the_archive_stays_in_crawl_unless_logview_holds_it(tmp_path, env, said):
    r = Runner(tmp_path, [])
    archive(r)
    assert r.run(MOVE, collector=None, **env).returncode == 1, r.out
    assert f"::error title=Old archive::{said}" in r.out and "PRIVATE" not in r.out
    assert "Crawl" not in r.names("push-order.txt")
    assert not any(f.startswith("logs/runner") for f in r.names("pushed.txt"))


def test_the_move_does_nothing_once_the_archive_is_gone_or_before_logview_has_the_mover(tmp_path):
    r = Runner(tmp_path / "gone", [])
    assert r.run(MOVE, collector=None).returncode == 0, r.out            # no logs/runner in Crawl
    assert "nothing to move" in r.out and r.names("clones.txt") == []
    r = Runner(tmp_path / "early", [])
    archive(r)
    (r.lv / "tools" / "move_runner_archive.py").unlink()                 # Crawl merged before LogView
    assert r.run(MOVE, collector=None).returncode == 0, r.out
    assert "LogView has no tools/move_runner_archive.py yet" in r.out and r.names("push-order.txt") == []


@pytest.mark.skipif(not (LOGVIEW / "tools" / "collect.py").exists(), reason="needs a checkout of LogView (LOGVIEW_REPO)")
def test_with_logviews_own_collector(tmp_path):
    """The two repositories agree on the shape: LogView's real collector stores the runs, and the step finds them."""
    runs = [run_(401, "pages build and deployment", "success", 2, 1.9), run_(402, "results", "failure", 1, 0.9),
            run_(403, "police", "success", 0.3, 0.1), run_(404, "crawl", "startup_failure", 30, 29.9),
            run_(405, "grand-cross", "skipped", 0.5, 0.5), run_(406, "fec-reread", "cancelled", 7, 1.2)]
    r = Runner(tmp_path, runs)
    (r.fake / "logs.json").write_text(json.dumps({"401": a_zip("ok\n"), "402": a_zip(), "406": a_zip("cut\n")}))   # 403's: not yet
    (r.fake / "jobs.json").write_text(json.dumps({"404": []}))
    (r.fake / "notes.json").write_text(json.dumps({"9406": [
        {"annotation_level": "failure", "message": "The job has exceeded the maximum execution time of 5h45m0s"}]}))
    real = (LOGVIEW / "tools" / "collect.py").read_text()
    assert r.run(STORE, collector=real).returncode == 0, r.out
    assert r.deleted == ["401", "402", "404", "405", "406"], r.out          # not 403: its logs are asked for again
    assert "6 finished run(s) here: 5 stored in LogView, 1 waiting for their logs, 0 not stored" in r.out
    pushed = set(r.names("pushed.txt"))
    day = runs[1]["created_at"][:10]
    assert {f"crawl-runner/{day}/{runs[1]['created_at'][11:19].replace(':', '')}-results-402.{x}" for x in ("zip", "json")} <= pushed
    assert "crawl-runner/README.md" in pushed and f"crawl-runner/{day}/README.md" in pushed
    assert not any("-405." in f for f in pushed)                           # a skipped run is only counted
    assert r.status["fec-reread"]["last"]["conclusion"] == "cancelled (time limit)"
    assert r.status["results"]["fails_in_row"] == 1
    # every call for the runs' logs was made with this job's own token (free, and it reads nothing but this repository)
    assert {c[-1] for c in r.calls if c[0] == "api" and "/logs" in " ".join(c)} == {"token=runner"}


COLLECTOR = r"""import json, os, pathlib, sys
pathlib.Path(os.environ["RUNNER_TEMP"], "args.json").write_text(json.dumps(sys.argv[1:]))
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
    # the private repositories only: this repository's own runs are stored by sync-code, the one writer of crawl-runner/
    assert json.loads((rt / "args.json").read_text()) == ["--days", "3", "--only", "electo,electo-data,crawl"]
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
