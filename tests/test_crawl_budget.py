"""The crawl's merge job: what its optional steps get from the job's clock, and what a missing key or a late file says.

The optional steps share the minutes left before the reserve (scripts/budget.sh) in their order. From Nov 14 to Nov 19,
2026 the Georgia money step stood last, behind the legislature, bill reasons, discovery, hearing video, the acts' Code
changes, federal bills and finance, and found no time left in 18 merge jobs in a row. These tests walk the steps the way
the job does, each using all of its minutes and its hard stop's margin, and check that it keeps its minutes."""
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
    for start in (20, 53, 100, 140):
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
