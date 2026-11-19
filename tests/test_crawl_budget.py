"""The crawl's merge job: what its optional steps get from the job's clock, and what a missing key or a late file says.

The optional steps share the minutes left before the reserve (scripts/budget.sh) in their order. From Nov 14 to Nov 19,
2026 the Georgia money step stood last, behind the legislature, bill reasons, discovery, hearing video, the acts' Code
changes, federal bills and finance, and found no time left in 18 merge jobs in a row. These tests walk the steps the way
the job does, each using all of its minutes and its hard stop's margin, and check that it keeps its minutes. The records
contacts come before research for the same reason: behind the open model's steps they would have found no time left
in 19 of the 21 merge jobs from Nov 14 to 19.

The saving steps keep the reserve: on Nov 19, 2026 (run 44497934401) they began at minute 256, the late money took 23
minutes, the export stopped at its own 45-minute limit and nothing was imported into Electo. The tests below hold each
saving step at the most it took in the 32 merge jobs with an export from Nov 10 to 19, check that the job still ends
before its limit, and that an export or import that did not finish queues an export-only run (never from one)."""
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def job(workflow: str, name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][name]


def merge_steps() -> list[dict]:
    return job("crawl.yml", "merge")["steps"]


def find(steps: list[dict], prefix: str) -> int:
    hits = [i for i, s in enumerate(steps) if (s.get("name") or "").startswith(prefix)]
    assert len(hits) == 1, f"{prefix!r}: {len(hits)} steps"
    return hits[0]


def script(step: dict) -> str:
    """A step's shell as a main run on the public runner sees it: a `${{ ... || 'N' }}` default is N, any other
    expression empty (the prepare job's outputs, inputs)."""
    def sub(m: re.Match) -> str:
        d = re.search(r"\|\|\s*'([^']*)'\s*$", m.group(1).strip())
        return d.group(1) if d else ""
    return re.sub(r"\$\{\{(.*?)\}\}", sub, step["run"], flags=re.S)


def budget(want: int, keep: int, used: int, limit: int) -> int:
    env = {**os.environ, "MERGE_LIMIT": str(limit), "MERGE_T0": str(int(time.time()) - used * 60)}
    env.pop("EXPORT_ONLY", None)
    env.pop("MERGE_RESERVE", None)
    out = subprocess.run(["bash", str(ROOT / "scripts" / "budget.sh"), str(want), str(keep)], env=env,
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def worst(step: dict, used: int, limit: int) -> tuple[int, int]:
    """(minutes the step is given, minutes it may take at most) at `used` minutes into the job: all of what it is
    given, plus its hard stop's margin and kill time. A step with no clock counts its own time cap."""
    if "run" not in step:
        return 0, 1                                        # an action (the model cache): seconds
    s = script(step)
    hard = re.search(r'-k (\d+)m "\$\(\(M \+ (\d+)\)\)m"', s)
    margin = int(hard.group(1)) + int(hard.group(2)) if hard else 0
    if "budget.sh" not in s:
        if "timeout-minutes" in step:
            return 0, int(step["timeout-minutes"])
        base = re.search(r"\bM=(\d+)", s)
        assert base and hard, f"{step['name']!r}: no time cap before the money steps"
        return 0, int(base.group(1)) + margin
    call = re.search(r'budget\.sh (?:"\$M"|(\d+))(?: "(\d+)")?', s)
    assert call, step["name"]
    if call.group(1):
        want = int(call.group(1))
    elif "CAP=" in s:
        want = int(re.search(r"\bCAP=(\d+)", s).group(1))  # research: the public runner's block asks for more than the cap
    else:
        want = int(re.search(r"\bM=(\d+)", s).group(1))
        if (m := re.search(r'\[ "\$PUBLIC_RUNNER" = "true" \] && M=(\d+)', s)):
            want = int(m.group(1))
        if (m := re.search(r'\[ "\$PUBLIC_RUNNER" = "true" \] && M=\$\(\( M \* (\d+) \)\)', s)):
            want *= int(m.group(1))
        if (m := re.search(r'\[ "\$M" -gt (\d+) \] && M=(\d+)', s)):
            want = min(want, int(m.group(2)))
    given = budget(want, int(call.group(2) or 0), used, limit)
    return given, (given + margin if given else 0)


def job_limit() -> int:
    return int(re.search(r"MERGE_LIMIT=(\d+)", merge_steps()[0]["run"]).group(1))


def wanted(step: dict) -> int:
    return int(re.search(r"\bM=(\d+)", script(step)).group(1))


def test_money_steps_keep_their_minutes_after_research():
    steps, limit = merge_steps(), job_limit()
    r = find(steps, "Candidate research")
    g = find(steps, "Georgia state and local money")
    o = find(steps, "What the organizations that gave money are")
    assert r < find(steps, "Campaign finance and organizations") < g < o   # donor ids for both kinds of money first
    # minutes into the job when research starts: Nov 19's 53, a quick start, a slow one, and one with research left 0
    # (from minute 95 with the reserve at 135; the latest start in the merge jobs from Nov 10 to 19 was minute 125)
    for start in (20, 53, 100, 130):
        used = start
        for st in steps[r:g]:
            used += worst(st, used, limit)[1]
        got, took = worst(steps[g], used, limit)
        assert got == wanted(steps[g]), f"research at minute {start}: the Georgia money step gets {got} minutes at minute {used}"
        used += took
        for st in steps[g + 1:o]:
            used += worst(st, used, limit)[1]
        assert worst(steps[o], used, limit)[0] == wanted(steps[o]), f"research at minute {start}: the organizations"


def test_federal_bills_before_the_code_changes():
    steps = merge_steps()
    # a minute's reading must not wait behind a step that uses all of its 45 minutes while its backlog lasts
    assert find(steps, "Federal bills") < find(steps, "Code changes of enacted acts")


def stub(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def run_step(step: dict, tmp_path: Path, env: dict, cwd: Path = ROOT) -> str:
    f = tmp_path / "step.sh"
    f.write_text(script(step))
    e = {**os.environ, "IS_MAIN": "true", "GITHUB_REPOSITORY": "ClockworkBlack/Crawl-runner", "GITHUB_WORKSPACE": str(ROOT),
         **env}
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(f)], cwd=cwd, env=e,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_youtube_without_a_key_warns_on_the_run_page(tmp_path):
    calls = tmp_path / "calls"
    bin_ = stub(tmp_path, "gacrawl", f'echo "$*" >> "{calls}"\n')
    st = merge_steps()[find(merge_steps(), "YouTube channels")]
    out = run_step(st, tmp_path, {"PATH": f"{bin_}:{os.environ['PATH']}", "YOUTUBE_API_KEY": ""})
    warn = [ln for ln in out.splitlines() if ln.startswith("::warning title=YouTube::")]
    assert warn and "ClockworkBlack/Crawl-runner" in warn[0] and "meeting videos are not being listed" in warn[0]
    assert "run --kind video" not in calls.read_text()
    calls.unlink()
    out = run_step(st, tmp_path, {"PATH": f"{bin_}:{os.environ['PATH']}", "YOUTUBE_API_KEY": "k"})
    assert "::warning" not in out and "run --kind video" in calls.read_text()


def test_missing_x_token_is_a_notice(tmp_path):
    bin_ = stub(tmp_path, "gacrawl", "exit 0\n")
    st = merge_steps()[find(merge_steps(), "Candidate research")]
    # EXPORT_ONLY: the clock gives research 0 minutes, so the step ends right after the keys are reported
    out = run_step(st, tmp_path, {"PATH": f"{bin_}:{os.environ['PATH']}", "EXPORT_ONLY": "true", "X_BEARER_TOKEN": "",
                                  "FEC_API_KEY": "k", "YOUTUBE_API_KEY": "k"})
    assert any(ln.startswith("::notice title=X posts::") and "ClockworkBlack/Crawl-runner" in ln for ln in out.splitlines())
    assert "::warning" not in out
    out = run_step(st, tmp_path, {"PATH": f"{bin_}:{os.environ['PATH']}", "EXPORT_ONLY": "true", "X_BEARER_TOKEN": "t",
                                  "FEC_API_KEY": "k", "YOUTUBE_API_KEY": "k"})
    assert "::notice" not in out


def test_legiscan_without_a_key_names_the_repository_that_needs_it(tmp_path):
    steps = job("legis-votes.yml", "votes")["steps"]
    st = steps[find(steps, "LegiScan")]
    out = run_step(st, tmp_path, {"LEGISCAN_API_KEY": ""})
    line = out.strip().splitlines()[0]
    assert line.startswith("::warning title=LegiScan::") and "ClockworkBlack/Crawl-runner" in line


def test_the_export_reads_the_police_jobs_newest_sheriffs(tmp_path):
    files = job("crawl.yml", "merge")["env"]["POLICE_STATE"].split()
    assert "state/sheriffs.json" in files and "state/police.json" in files
    steps = merge_steps()
    early = steps[find(steps, "Police incidents in the news")]
    late_i = find(steps, "The police job's newest departments and sheriffs, for the export")
    assert "for f in $POLICE_STATE" in early["run"]
    assert late_i < find(steps, "Task list, status, Electo bundle")
    # the private repository holds a newer sheriffs file; its police.json does not answer (kept as this job has it)
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    (work / "state" / "sheriffs.json").write_text(json.dumps({"sheriffs": {}}))
    (work / "state" / "police.json").write_text(json.dumps({"old": True}))
    newest = {"as_of": "2026-11-19", "sheriffs": {"appling": {"name": "A Sheriff"}}}
    bin_ = stub(tmp_path, "gh", 'case "$*" in *contents/state/sheriffs.json*) echo \'' + json.dumps(newest)
                + '\' ;; *contents/state/media.json*) echo "not json" ;; *) exit 1 ;; esac\n')
    out = run_step(steps[late_i], tmp_path, {"PATH": f"{bin_}:{os.environ['PATH']}", "DATA_REPO": "ClockworkBlack/Crawl",
                                             "POLICE_STATE": " ".join(files)}, cwd=work)
    assert json.loads((work / "state" / "sheriffs.json").read_text()) == newest
    assert json.loads((work / "state" / "police.json").read_text()) == {"old": True}
    assert not (work / "state" / "media.json").exists() and not list((work / "state").glob("*.new"))
    assert "state/sheriffs.json: the police job's newest" in out


# ---- the saving steps: the most each took, and the job's clock that keeps them their minutes

# minutes, the most each saving step took in the 32 merge jobs with an export from Nov 10 to Nov 19, 2026 (the archived
# logs in logs/runner/2026-11: each step from its first line to the next step's, or the run's jobs.json)
SLOWEST = {
    "Store the database now": 3.7,                       # Nov 19: 1.9 GB in two parts
    "Temp save of a stopped run's data": 0.0,            # a cancelled run only
    "The police job's newest departments and sheriffs": 0.2,   # new on Nov 19: five file reads
    "Memory to spare for the export": 0.1,
    "Task list, status, Electo bundle": 46.0,            # Nov 19: stopped at its limit, 45.2, about a minute short
    "Store database and bundle": 5.5,
    "Run summary": 0.1,
    "Backfill (this run's minutes": 0.2,
    "Coverage (what the data holds": 5.6,
    "The owner's latest review decisions": 0.1,
    "Commit state": 1.3,
    "Electo is fed from what Crawl holds": 2.2,
    "Push research packets to Electo": 10.4,
    "Import into Electo": 28.1,                          # three tries, Nov 16
    "Temp saves this run's data covers are deleted": 0.1,
    "Site data for the page's Check for updates": 6.6,
    "Remove this run's attachments": 0.9,
}
LATE_MONEY = "Money read while this run worked"
AT_THE_END = ("The export or the Electo import did not finish", "The Electo import failed")   # a few API calls
OVERRUN = 10       # the last optional step past the clock: 16 on Nov 19, 10 of them the records contacts (before research,
                   # on the clock, since)


def saving(want: int, name: str, used: int, limit: int, export_only: bool = False) -> int:
    env = {**os.environ, "MERGE_LIMIT": str(limit), "MERGE_T0": str(int(time.time()) - used * 60)}
    env.pop("MERGE_RESERVE", None)
    env["EXPORT_ONLY"] = "true" if export_only else "false"
    out = subprocess.run(["bash", str(ROOT / "scripts" / "budget.sh"), "--saving", str(want), name], env=env,
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def saving_call(step: dict) -> tuple[int, str]:
    m = re.search(r"budget\.sh --saving (\d+) (\w+)", script(step))
    assert m, step["name"]
    return int(m.group(1)), m.group(2)


def test_every_saving_step_is_measured():
    steps = merge_steps()
    names = [s.get("name") or s.get("uses") or "" for s in steps[find(steps, "Store the database now"):]]
    for n in names:
        known = [k for k in SLOWEST if n.startswith(k)] + [k for k in (LATE_MONEY, *AT_THE_END) if n.startswith(k)]
        assert len(known) == 1, f"{n!r}: a saving step not in the table (how long does it take at the most?)"
    for k in SLOWEST:
        find(steps, k)


def test_the_saving_steps_fit_the_job_at_their_slowest():
    steps, limit = merge_steps(), job_limit()
    reserve = limit - budget(10 ** 4, 0, 0, limit)
    assert reserve >= OVERRUN + sum(SLOWEST.values()), "the reserve no longer holds every saving step at its slowest"
    first = find(steps, "Store the database now")
    money_want, money = saving_call(steps[find(steps, LATE_MONEY)])
    export_want, export = saving_call(steps[find(steps, "Task list, status, Electo bundle")])
    # the saving steps begin when the clock runs out, OVERRUN minutes later, and later still (a step past its margin)
    for late in (0, OVERRUN, OVERRUN + 25):
        used, got = limit - reserve + late, {}
        for st in steps[first:]:
            name = st.get("name") or ""
            if name.startswith(LATE_MONEY):
                got["money"] = g = saving(money_want, money, int(used), limit)
                used += g + 0.5 if g >= 3 else 0                 # all of it, and the half minute a stop may take
            elif name.startswith("Task list, status, Electo bundle"):
                got["export"] = g = saving(export_want, export, int(used), limit)
                used += min(SLOWEST["Task list, status, Electo bundle"], g + 1)   # done, or stopped by the clock
            else:
                used += next((v for k, v in SLOWEST.items() if name.startswith(k)), 0.1)
        assert used <= limit, f"saving steps {late} minutes late: the job needs {used:.1f} of its {limit} minutes"
        if late <= OVERRUN:
            # Nov 19's export finishes, and the late money has its minutes
            assert got["export"] >= SLOWEST["Task list, status, Electo bundle"], (late, got)
            assert got["money"] >= 5, (late, got)
    # an export-only run: the clock's optional minutes are 0, the saving steps' are not
    assert saving(export_want, export, 40, limit, export_only=True) == export_want
    assert budget(25, 0, 40, limit) > 0 and saving(money_want, money, 40, limit, export_only=True) == money_want


def test_the_export_has_room_beyond_its_slowest():
    steps = merge_steps()
    st = steps[find(steps, "Task list, status, Electo bundle")]
    want, _ = saving_call(st)
    assert want >= 1.5 * SLOWEST["Task list, status, Electo bundle"]
    assert int(st["timeout-minutes"]) >= want + 5        # the reports and packages before it, a stop's grace
    late = steps[find(steps, LATE_MONEY)]
    assert int(late["timeout-minutes"]) >= saving_call(late)[0] + 2


def run_script(step: dict, tmp_path: Path, env: dict, cwd: Path = ROOT) -> tuple[int, str]:
    f = tmp_path / "step.sh"
    f.write_text(script(step))
    e = {**os.environ, "IS_MAIN": "true", "GITHUB_REPOSITORY": "ClockworkBlack/Crawl-runner", "GITHUB_WORKSPACE": str(ROOT),
         **env}
    e.pop("MERGE_RESERVE", None)
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(f)], cwd=cwd, env=e,
                       capture_output=True, text=True, check=False)
    return p.returncode, p.stdout + p.stderr


def workdir(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    w.mkdir(exist_ok=True)
    if not (w / "scripts").exists():
        (w / "scripts").symlink_to(ROOT / "scripts")
    return w


TIMEOUT = 'echo "$*" >> "$TLOG"\ncase "$*" in *gacrawl*) [ -n "${TIMEOUT_RC:-}" ] && exit "$TIMEOUT_RC" ;; esac\n' \
          'while [ $# -gt 0 ]; do case "$1" in -s|-k) shift 2 ;; -*) shift ;; *) shift; break ;; esac; done\n"$@"\n'


def test_the_late_money_fits_the_clock(tmp_path):
    calls, tlog = tmp_path / "calls", tmp_path / "tlog"
    stub(tmp_path, "gacrawl", f'echo "$*" >> "{calls}"\n')
    stub(tmp_path, "zstd", 'p=""; for a; do [ "$p" = "-o" ] && echo db > "$a"; p=$a; done\n')
    stub(tmp_path, "timeout", TIMEOUT)
    bin_ = stub(tmp_path, "gh", 'case "$*" in\n  *releases/tags/*) for a in $HAVE; do case "$*" in *"\\"$a\\""*) echo 7 ;; esac; done ;;\n'
                                '  *releases/assets/*) echo bytes ;;\nesac\n')
    st = merge_steps()[find(merge_steps(), LATE_MONEY)]
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "DATA_TAG": "crawl-data", "TLOG": str(tlog), "MERGE_LIMIT": "350",
           "HAVE": "fec-delta.db.zst fec-kv.json"}
    # the export and everything after it need 116 minutes at their slowest: with 118 left this step has 2, and skips
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": str(int(time.time()) - (350 - 118) * 60)}, cwd=workdir(tmp_path))
    assert rc == 0 and "no time left in this run for the money" in out and not calls.exists() and not tlog.exists()
    # with time: at most 12 minutes, the apply and the linking inside them, stopped with SIGINT
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": str(int(time.time()) - 200 * 60)}, cwd=workdir(tmp_path))
    assert rc == 0, out
    assert calls.read_text().splitlines() == ["finance apply --delta late/fec.db --kv late/fec-kv.json", "finance link"]
    stops = [ln.split() for ln in tlog.read_text().splitlines()]
    assert all(s[:4] == ["-s", "INT", "-k", "30"] and 600 < int(s[4]) <= 720 for s in stops), stops
    assert [s[5:8] for s in stops if s[5] == "gacrawl"] == [["gacrawl", "finance", "apply"], ["gacrawl", "finance", "link"]]
    # a stopped apply is the time limit, not an error: the next run applies the hand-off at its start
    calls.unlink()
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": str(int(time.time()) - 200 * 60), "HAVE": "ga-delta.db.zst",
                                        "TIMEOUT_RC": "130"}, cwd=workdir(tmp_path))
    assert rc == 0 and "the next run applies it at its start (exit 130: the time limit)" in out and "::warning" not in out


def test_the_export_fits_the_clock_and_says_when_it_was_stopped(tmp_path):
    calls, tlog = tmp_path / "calls", tmp_path / "tlog"
    stub(tmp_path, "gacrawl", f'echo "$*" >> "{calls}"\n')
    stub(tmp_path, "pip", "exit 0\n")
    bin_ = stub(tmp_path, "timeout", TIMEOUT)
    st = merge_steps()[find(merge_steps(), "Task list, status, Electo bundle")]
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "TLOG": str(tlog), "MERGE_LIMIT": "350"}

    def ago(m: int) -> str:
        return str(int(time.time()) - m * 60)

    # 10 minutes left once the steps after it keep their slowest 65: not started, and said
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": ago(350 - 75)}, cwd=workdir(tmp_path))
    assert rc == 1 and "::error title=Export::only 10 minutes left" in out and "export" not in calls.read_text()
    calls.unlink()
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": ago(100)}, cwd=workdir(tmp_path))
    assert rc == 0 and "export --out dist/electo" in calls.read_text()
    assert tlog.read_text().split()[:5] == ["-k", "1m", "70m", "gacrawl", "export"]
    # stopped by the clock (or killed after it), and stopped for memory before its minutes were up
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": ago(100), "TIMEOUT_RC": "124"}, cwd=workdir(tmp_path))
    assert rc == 124 and "::error title=Export::the export was stopped after 70 minutes" in out
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": ago(100), "TIMEOUT_RC": "137"}, cwd=workdir(tmp_path))
    assert rc == 137 and "stopped for memory (exit 137)" in out and "stopped after 70" not in out


RECOVERY = "The export or the Electo import did not finish"


def test_an_unfinished_export_or_import_queues_an_export_only_run(tmp_path):
    calls, runs = tmp_path / "calls", tmp_path / "runs"
    runs.mkdir()
    bin_ = stub(tmp_path, "gh", f'echo "GH_TOKEN=$GH_TOKEN $*" >> "{calls}"\n'
                                'case "$*" in\n  "api "*/runs\\?*) [ -n "${FAIL_LIST:-}" ] && exit 1\n'
                                f'    for s in queued in_progress waiting pending requested; do case "$*" in *"status=$s&"*) cat "{runs}/$s" 2>/dev/null || true ;; esac; done ;;\n'
                                'esac\n')
    st = merge_steps()[find(merge_steps(), RECOVERY)]
    summary = tmp_path / "summary.md"
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "RUN_TOKEN": "run-token", "GH_TOKEN": "data-token",
           "GITHUB_REF_NAME": "main", "GITHUB_RUN_ID": "999", "GITHUB_STEP_SUMMARY": str(summary),
           "EXPORTED": "failure", "STORED": "skipped", "IMPORTED": "skipped", "EXPORT_ONLY": "false"}
    dispatch = "GH_TOKEN=run-token workflow run -R ClockworkBlack/Crawl-runner crawl.yml --ref main -f job=export-only"

    rc, out = run_script(st, tmp_path, env)                      # nothing waiting: queued
    assert rc == 0 and dispatch in calls.read_text().splitlines(), calls.read_text()
    assert "::error title=Electo not updated::the export (failure)" in out and "An export-only run is queued" in out
    assert "### Electo was not updated by this run" in summary.read_text()
    assert all(ln.startswith("GH_TOKEN=run-token ") for ln in calls.read_text().splitlines())   # this repository's token

    calls.unlink()                                               # the next backfill link waiting: it gives way
    (runs / "pending").write_text("456 pending crawl\n")
    rc, out = run_script(st, tmp_path, env)
    assert rc == 0 and dispatch in calls.read_text() and "takes the place of waiting run 456" in out

    calls.unlink()                                               # an export-only run waiting already: none more
    (runs / "queued").write_text("123 queued crawl (export-only)\n")
    rc, out = run_script(st, tmp_path, env)
    assert rc == 0 and "workflow run" not in calls.read_text() and "already waiting or running (123)" in out

    calls.unlink()                                               # the runs cannot be listed: none queued, said
    rc, out = run_script(st, tmp_path, {**env, "FAIL_LIST": "1"})
    assert rc == 1 and "workflow run" not in calls.read_text() and "start one by hand" in out

    calls.unlink()                                               # an export-only run never queues another (no loop)
    rc, out = run_script(st, tmp_path, {**env, "EXPORT_ONLY": "true"})
    assert rc == 0 and not calls.exists() and "This export-only run queues no other one" in out


def test_the_export_only_run_is_queued_only_when_it_can_help():
    wf = yaml.safe_load((WORKFLOWS / "crawl.yml").read_text())
    assert wf["permissions"]["actions"] == "write"
    # its name is what the step looks for among the waiting runs
    assert "inputs.job == 'export-only' && 'crawl (export-only)'" in wf["run-name"]
    steps = merge_steps()
    st = steps[find(steps, RECOVERY)]
    assert 'grep -q " crawl (export-only)$"' in st["run"]
    cond = st["if"]
    for need in ("!cancelled()", "env.IS_MAIN == 'true'", "env.HAS_ELECTO_TOKEN == 'true'",
                 "steps.dbnow.outcome == 'success' || steps.stored.outcome == 'success'",   # this run stored its database
                 "steps.imported.outcome != 'success'", "steps.imported.outputs.stopped != 'step'"):
        assert need in cond, need
    ids = {s.get("id"): i for i, s in enumerate(steps) if s.get("id")}
    assert ids["dbnow"] == find(steps, "Store the database now") and ids["bundle"] < ids["stored"] < ids["imported"]
    assert find(steps, "Site data for the page's") < find(steps, RECOVERY) < find(steps, "The Electo import failed")
    # a step of Electo's importer that stopped would stop an export-only run in the same place: said so to this step
    imp = steps[ids["imported"]]["run"]
    assert re.search(r'if \[ "\$rc" = 2 \]; then.*?echo "stopped=step" >> "\$GITHUB_OUTPUT"\s+exit 1', imp, re.DOTALL)


def test_an_export_only_run_does_no_nightly_or_weekly_work(tmp_path):
    steps = job("crawl.yml", "prepare")["steps"]
    st = steps[find(steps, "Nightly and weekly work due")]
    genv, gout = tmp_path / "env", tmp_path / "out"
    w = workdir(tmp_path)                       # no state/cadence.json: both are due in a full run
    rc, _ = run_script(st, tmp_path, {"GITHUB_ENV": str(genv), "GITHUB_OUTPUT": str(gout), "EXPORT_ONLY": "true"}, cwd=w)
    assert rc == 0 and genv.read_text().split() == ["NIGHTLY=false", "WEEKLY=false", "OPTIMIZE_FTS=false"]
    assert gout.read_text().split() == ["nightly=false", "weekly=false", "optimize_fts=false"]
    genv.unlink(); gout.unlink()
    rc, _ = run_script(st, tmp_path, {"GITHUB_ENV": str(genv), "GITHUB_OUTPUT": str(gout), "EXPORT_ONLY": "false"}, cwd=w)
    assert rc == 0 and "NIGHTLY=true" in genv.read_text() and "weekly=true" in gout.read_text()


CONTACTS = "Records contacts"
# the latest minute research started in the 35 merge jobs from Nov 10 to 19, 2026 (124.6, run 44497934401, whose first
# FEC apply took 18.6 minutes), from the archived logs in logs/runner/2026-11
LATEST_RESEARCH_START = 125


def test_the_records_contacts_keep_their_minutes_before_research():
    # On the clock behind the open model's steps they would have found 0 minutes in 19 of the 21 merge jobs from Nov 14
    # to 19 (all but two reached them after minute 240, and the clock leaves optional steps nothing from minute 215).
    # Before research they have their minutes wherever research started, and far later
    steps, limit = merge_steps(), job_limit()
    c, r = find(steps, CONTACTS), find(steps, "Candidate research")
    assert c < r
    looks_up = [s.get("name") for s in steps if "gacrawl records contacts" in (s.get("run") or "")]
    assert looks_up == [steps[c]["name"]], looks_up        # nowhere else, the open records step at the end included
    want = wanted(steps[c])
    assert want == 10
    for used in (20, 53, LATEST_RESEARCH_START, LATEST_RESEARCH_START + 80):
        assert worst(steps[c], used, limit) == (want, want + 3), used     # its 10, and at most 13 with its hard stop
    clock = budget(10 ** 4, 0, 0, limit)                      # 215: from here an optional step gets 0 minutes
    assert budget(want, 0, clock, limit) == 0 and clock < 240     # where the open records step stood: 240 to 248


def test_the_records_contacts_fit_the_clock(tmp_path):
    calls, tlog = tmp_path / "calls", tmp_path / "tlog"
    stub(tmp_path, "gacrawl", f'echo "$*" >> "{calls}"\n')
    bin_ = stub(tmp_path, "timeout", TIMEOUT)
    st = merge_steps()[find(merge_steps(), CONTACTS)]
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "PUBLIC_RUNNER": "true", "MERGE_LIMIT": "350", "TLOG": str(tlog)}
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": str(int(time.time()) - 230 * 60)}, cwd=workdir(tmp_path))
    assert rc == 0 and "no time left in this run for the records contacts" in out and not calls.exists()
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": str(int(time.time()) - 100 * 60)}, cwd=workdir(tmp_path))
    assert rc == 0 and calls.read_text() == "records contacts --max-minutes 10\n"
    assert tlog.read_text().split()[:5] == ["-s", "INT", "-k", "1m", "12m"]       # a hard stop, as on the other steps
    # stopped by it: the time limit, said, and the step stays green; the next run looks up the rest
    rc, out = run_script(st, tmp_path, {**env, "MERGE_T0": str(int(time.time()) - 100 * 60), "TIMEOUT_RC": "130"},
                         cwd=workdir(tmp_path))
    assert rc == 0 and "the records contacts stopped after 12 minutes (exit 130: the time limit)" in out
    # the open records step near the end: the mailbox (none on the public runner) and the tracker's status, no lookups
    calls.unlink()
    st = merge_steps()[find(merge_steps(), "Open records requests")]
    assert "budget.sh" not in script(st)
    rc, out = run_script(st, tmp_path, {**env, "ORR_USER": "", "MERGE_T0": str(int(time.time()) - 230 * 60)},
                         cwd=workdir(tmp_path))
    assert rc == 0 and calls.read_text().splitlines() == ["records status"], calls.read_text()
