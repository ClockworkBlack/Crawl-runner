"""Open-model jobs that had work and did none of it fail their step instead of ending green.

ai-said-by read no sentence from Nov 12 to 19, 2026, twice a day and green on every run: its model failed the exam on
all 8 runners, and every other way the AI jobs can do nothing (the model does not load, every call raises) also ended
in a ::warning at most (scripts/stopped.sh) or nothing at all (ai-outlook's continue-on-error). Electo's tools/ai
scripts now exit 75 (NOTHING_DONE) when work was due and no model call answered; stopped.sh fails the step on it."""
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
STOPPED = ROOT / "scripts" / "stopped.sh"
SHARD_JOBS = {"ai-said-by.yml": "said_by.py", "ai-infer.yml": "infer_ai.py", "ai-write.yml": "profile_write.py",
              "ai-profiles.yml": "profiles_ai.py", "ai-law-questions.yml": "law_questions.py"}


def stopped(rc, mode=None):
    r = subprocess.run(["bash", str(STOPPED), str(rc), "stopped after 5 minutes"] + ([mode] if mode else []),
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def test_stopped_tells_the_time_limit_from_a_crash_and_from_nothing_done():
    for rc in (124, 130, 137, 143):                       # the time limit: what was done is kept, the step stays green
        for mode in (None, "fatal"):
            code, out = stopped(rc, mode)
            assert code == 0 and "the time limit" in out and "::" not in out, (rc, mode)
    for mode in (None, "fatal"):                          # work was due and none of it was done: fails in either mode
        code, out = stopped(75, mode)
        assert code == 75 and out.startswith("::error::") and "none of it was done" in out, mode
    code, out = stopped(1)                                # a crash: a warning, unless the caller says "fatal"
    assert code == 0 and out.startswith("::warning::")
    code, out = stopped(1, "fatal")
    assert code == 1 and out.startswith("::error::")
    assert stopped(0) == (0, "")


def steps(name):
    W = yaml.safe_load((WORKFLOWS / name).read_text())
    return [(job, s) for job, J in W["jobs"].items() for s in J.get("steps") or []]


def test_each_model_shard_step_fails_on_a_crash_or_nothing_done_and_still_hands_off():
    for name, tool in SHARD_JOBS.items():
        S = steps(name)
        runs = [(i, s) for i, (_, s) in enumerate(S) if re.search(r"python3 -I tools/ai/" + re.escape(tool) + r" --shard", s.get("run") or "")]
        assert len(runs) == 1, name
        i, s = runs[0]
        assert re.search(r'scripts/stopped\.sh" \S+ "stopped" fatal', s["run"]), name
        assert not s.get("continue-on-error"), name
        hand = [st for _, st in S[i + 1:] if "ai_handoff.sh put" in (st.get("run") or "")]
        assert hand and hand[0].get("if") == "always()", name     # what was done before a crash is still passed on


def test_the_shard_steps_shell_runs_the_way_the_workflow_reads(tmp_path):
    # ai-infer's step with a stand-in for the tool: the shard and log are set aside first, then the exit code decides
    run = next(s["run"] for _, s in steps("ai-infer.yml") if "tools/ai/infer_ai.py --shard" in (s.get("run") or ""))
    body = re.sub(r"\$\{\{.*?\}\}", "1", run)
    body = body.replace('timeout -s INT -k 2m "$((M + 10))m" python3 -I tools/ai/infer_ai.py', "fake_tool")
    body = body.replace('"$GITHUB_WORKSPACE/scripts/stopped.sh"', str(STOPPED))
    (tmp_path / "electo" / "research" / "train").mkdir(parents=True)
    for rc, want in ((0, 0), (130, 0), (75, 75), (1, 1)):
        for f in ("shard.json", "log.jsonl"):
            (tmp_path / f).unlink(missing_ok=True)
        script = f"fake_tool() {{ return {rc}; }}\n" + body
        r = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True)
        assert r.returncode == want, (rc, r.stdout, r.stderr)
        assert (tmp_path / "shard.json").exists() and (tmp_path / "log.jsonl").exists(), rc


def test_the_said_by_combine_warns_when_the_exam_keeps_failing():
    W = yaml.safe_load((WORKFLOWS / "ai-said-by.yml").read_text())
    assert W[True]["workflow_dispatch"]["inputs"]["exam_again"]["type"] == "boolean"
    S = steps("ai-said-by.yml")
    need = next(s for _, s in S if s.get("id") == "need")
    assert "--list" in need["run"] and "$EXAM_AGAIN" in need["run"]
    combine = next(s for job, s in S if job == "combine" and "--combine" in (s.get("run") or ""))
    assert "said_by.py --stalled" in combine["run"] and "::warning title=Said by::" in combine["run"]
    # with no new readings the combine still runs (it records the exam), so the old early "nothing new" exit is gone
    assert "sys.exit(0 if n else 3)" not in combine["run"]


def test_the_outlook_fails_once_its_cards_are_over_30_hours_old(tmp_path):
    S = steps("ai-outlook.yml")
    assert not [s.get("name") for _, s in S if s.get("continue-on-error")]
    write = next(s for _, s in S if s.get("name") == "Write the outlook")
    assert "::warning title=Outlook::" in write["run"]            # a failed model run alone leaves the commit to happen
    names = [s.get("name") or "" for _, s in S]
    age = S[-1][1]
    assert names.index(age["name"]) > names.index("Commit to Electo and refresh the public site")
    script = age["run"].replace("/tmp/electo-ol/ga/outlook.json", str(tmp_path / "outlook.json"))
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    for hours, ok in ((5, True), (29, True), (31, False), (None, False)):
        (tmp_path / "outlook.json").write_text('{"at": "%s", "items": []}' % (iso(time.time() - hours * 3600) if hours else ""))
        r = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, env={**os.environ})
        assert (r.returncode == 0) == ok, (hours, r.stdout, r.stderr)
        assert ok or "::error title=Outlook::" in r.stdout
