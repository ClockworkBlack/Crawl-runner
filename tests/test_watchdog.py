"""The watchdog's check (watchdog.yml, "Check the chain and repair what can be repaired"), run against a fake GitHub.

It covered only this chain's runner jobs until Nov 19, 2026: records-send in the private Crawl repository failed three
weekdays in a row and no issue was opened, and jobs that ended green with nothing written (ai-said-by's said_by.json
stayed at Nov 12) or with an API key missing on the runner (YouTube from Nov 7, LegiScan never read) were not seen."""
import base64
import datetime as dt
import io
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "watchdog.yml"
NOW = dt.datetime.now(dt.timezone.utc)
CRAWL, RUNNER, ELECTO, SITE = "ClockworkBlack/Crawl", "ClockworkBlack/Crawl-runner", "ClockworkBlack/Electo", "ClockworkBlack/electo-data"


def iso(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_script() -> str:
    steps = yaml.safe_load(WF.read_text())["jobs"]["watch"]["steps"]
    run = next(s["run"] for s in steps if s.get("name", "").startswith("Check the chain"))
    return re.search(r"<<'PY'\n(.*)\nPY\s*$", run, re.S).group(1)


def run_(rid, conclusion, hours_ago):
    return {"id": rid, "conclusion": conclusion, "status": "completed", "updated_at": iso(hours_ago),
            "html_url": f"https://github.com/x/actions/runs/{rid}"}


class FakeGitHub:
    """Answers the watchdog's calls. `actions` maps (repository, token) to whether that token reads its Actions;
    `wf_runs` maps (repository, workflow file) to its runs, newest first; `files` maps an Electo path to its JSON."""

    def __init__(self, actions, wf_runs, files, open_issue=None):
        self.actions, self.wf_runs, self.files, self.open_issue = actions, wf_runs, files, open_issue
        self.sent = []          # (method, path, body) of every write
        self.read = []          # the path of every GET

    def answer(self, method, path, token, accept):
        if method != "GET":
            return 200, {}
        if path == "rate_limit":
            return 200, {}
        if path == f"repos/{CRAWL}/commits?per_page=100":
            return 200, [{"commit": {"message": m, "committer": {"date": iso(1)}}} for m in ("crawl: x", "police: x")]
        if path.startswith(f"repos/{RUNNER}/actions/"):
            return 200, {"workflow_runs": []}
        if path == f"repos/{SITE}/commits?per_page=1":
            return 200, [{"commit": {"committer": {"date": iso(1)}}}]
        if path == f"repos/{ELECTO}/commits/main":
            return 200, {"sha": "abc1234def", "commit": {"committer": {"date": iso(1)}}}
        if path == f"repos/{SITE}/contents/current.json":
            return 200, {"content": base64.b64encode(json.dumps({"commit": "abc1234"}).encode()).decode()}
        if path.startswith(f"repos/{CRAWL}/issues?"):
            return 200, [self.open_issue] if self.open_issue else []
        m = re.fullmatch(r"repos/(\w+/[\w.-]+)/actions/runs\?per_page=1", path)
        if m:
            return (200, {"workflow_runs": []}) if self.actions.get((m.group(1), token)) else (403, {"message": "no"})
        m = re.fullmatch(r"repos/(\w+/[\w.-]+)/actions/workflows/([\w.-]+)/runs\?branch=main&status=completed&per_page=10", path)
        if m:
            if not self.actions.get((m.group(1), token)):
                return 403, {"message": "Resource not accessible by personal access token"}
            runs = self.wf_runs.get((m.group(1), m.group(2)))
            return (404, {"message": "Not Found"}) if runs is None else (200, {"workflow_runs": runs})
        m = re.fullmatch(rf"repos/{ELECTO}/contents/(.+)", path)
        if m and accept == "application/vnd.github.raw" and m.group(1) in self.files:
            return 200, self.files[m.group(1)]
        return 404, {"message": "Not Found"}

    def urlopen(self, req, timeout=None):
        path = req.full_url.split("https://api.github.com/", 1)[1]
        method = req.get_method()
        if method != "GET":
            self.sent.append((method, path, json.loads(req.data) if req.data else None))
        else:
            self.read.append(path)
        code, obj = self.answer(method, path, req.get_header("Authorization").split()[-1], req.get_header("Accept"))
        data = json.dumps(obj).encode()
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "error", {}, io.BytesIO(data))

        class Resp(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp(data)


def watch(monkeypatch, tmp_path, hub, keys=False, status=None):
    for k, v in {"DATA_TOKEN": "data", "ELECTO_TOKEN": "electo", "SITE_TOKEN": "site", "LOGVIEW_READ": "lvread",
                 "RUNNER_TOKEN": "runner", "RUNNER_REPO": RUNNER, "DATA_REPO": CRAWL, "ELECTO_REPO": ELECTO,
                 "SITE_DATA_REPO": SITE, "MAX_CRAWL_H": "10", "MAX_POLICE_H": "30", "MAX_NEWS_H": "8",
                 "MAX_SITE_H": "8", "MAX_STEP_H": "4", "HAS_LOGVIEW": "true",
                 "HAS_YOUTUBE_KEY": str(keys).lower(), "HAS_LEGISCAN_KEY": str(keys).lower()}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BACKFILL_GO", raising=False)
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name in ("ai-said-by.yml", "ai-outlook.yml"):
        (wf / name).write_text('on:\n  schedule:\n    - cron: "7 5,17 * * *"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", hub.urlopen)
    # runner-status.json (sync-code's record of this repository's runs), or none yet
    done = (subprocess.CompletedProcess((), 0, json.dumps(status), "") if status is not None
            else subprocess.CompletedProcess((), 1, "", "release asset not found"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    monkeypatch.setattr("time.sleep", lambda s: None)
    exec(compile(check_script(), "watchdog-check", "exec"), {"__name__": "__main__"})


def files(said_by_h=2, outlook_h=2, video_days=1, legiscan=5000, said_by=None):
    return {"ga/crawl/said_by.json": {"at": iso(said_by_h), "exam": {"ok": True}, **(said_by or {})},
            "ga/outlook.json": {"at": iso(outlook_h), "items": []},
            "ga/crawl/meeting_videos.json": {"as_of": "x", "videos": [
                {"t": "a", "d": (NOW - dt.timedelta(days=video_days)).strftime("%Y-%m-%d")}, {"t": "b", "d": "2016-01-05"}]},
            "data/crawl/vote_model.json": {"sources": {"legiscan_roll_calls": legiscan, "legis_roll_calls": 8414}}}


def healthy_runs():
    return {(CRAWL, "records-send.yml"): [run_(1, "success", 20)], (CRAWL, "code-finder.yml"): [run_(2, "success", 5)],
            (CRAWL, "ordinances.yml"): [run_(3, "success", 30)],
            (ELECTO, "bill-details.yml"): [run_(4, "success", 3)], (ELECTO, "incidents.yml"): [run_(5, "success", 3)],
            (SITE, "pages.yml"): [run_(6, "success", 1)], (SITE, "apps.yml"): [run_(7, "success", 72)]}


def issue_body(hub):
    posted = [b for m, p, b in hub.sent if m in ("POST", "PATCH") and re.fullmatch(rf"repos/{CRAWL}/issues(/\d+)?", p)
              and b and "body" in b]
    return posted[-1]["body"] if posted else None


def test_private_jobs_outputs_and_keys_reach_the_issue_not_the_public_log(monkeypatch, tmp_path, capsys):
    runs = healthy_runs()
    # Nov 16-18: three weekdays in a row failed, the last success five days before
    runs[(CRAWL, "records-send.yml")] = [run_(13, "failure", 20), run_(12, "failure", 44), run_(11, "failure", 68),
                                         run_(10, "success", 140)]
    # Nov 10-15: each run cut at its time limit ends "cancelled"
    runs[(CRAWL, "ordinances.yml")] = [run_(23, "cancelled", 30), run_(22, "cancelled", 54), run_(21, "success", 200)]
    # a newer site build cancels a deploy in progress: not a failure
    runs[(SITE, "pages.yml")] = [run_(33, "cancelled", 0.5), run_(32, "cancelled", 0.7), run_(31, "success", 1)]
    hub = FakeGitHub(
        # DATA_TOKEN has no Actions: read on Crawl (LOGVIEW_READ has); no token here reads Electo's Actions
        actions={(CRAWL, "lvread"): True, (SITE, "site"): True},
        wf_runs=runs, files=files(said_by_h=24 * 7, video_days=15, legiscan=0))
    watch(monkeypatch, tmp_path, hub)
    out = capsys.readouterr().out
    body = issue_body(hub)
    assert body, "no watchdog issue opened"
    assert "**records-send.yml** in ClockworkBlack/Crawl: 3 runs in a row did not succeed" in body
    assert "no success in 140 hours" in body
    # ordinances runs on the runner since Nov 19 (3c, from runner-status.json): its private runs are not read
    assert "ordinances.yml" not in body and not any("workflows/ordinances.yml" in p for p in hub.read if CRAWL in p)
    assert "pages.yml" not in body
    assert "said_by.json** in Electo was last written 7 days ago" in body
    assert "ga/outlook.json" not in body                         # written two hours ago
    assert "newest meeting video" in body and "YOUTUBE_API_KEY is not set" in body
    assert "legiscan_roll_calls is 0" in body and "LEGISCAN_API_KEY is not set" in body
    # Electo's runs could not be read: said so in a notice, nothing claimed about its jobs
    assert "::notice title=Watchdog::the jobs of ClockworkBlack/Electo are not watched" in out
    assert "bill-details" not in body
    # the private repositories' jobs and Electo's files are not named in this public log
    for private in ("records-send", "ordinances.yml", "said_by.json", "meeting_videos", "vote_model"):
        assert private not in out, private
    assert "private repositories' jobs and outputs: 4 problem(s)" in out


def test_all_healthy_opens_nothing_and_closes_the_issue(monkeypatch, tmp_path, capsys):
    hub = FakeGitHub(actions={(CRAWL, "data"): True, (ELECTO, "electo"): True, (SITE, "site"): True},
                     wf_runs=healthy_runs(), files=files(), open_issue={"number": 16})
    watch(monkeypatch, tmp_path, hub, keys=True)
    assert issue_body(hub) is None
    assert ("PATCH", f"repos/{CRAWL}/issues/16", {"state": "closed"}) in hub.sent
    assert any(p == f"repos/{CRAWL}/issues/16/comments" and b["body"].startswith("Healthy again") for m, p, b in hub.sent)
    assert "::notice" not in capsys.readouterr().out


def test_two_failures_in_a_row_and_a_stale_output_with_the_keys_set(monkeypatch, tmp_path):
    runs = healthy_runs()
    runs[(ELECTO, "incidents.yml")] = [run_(42, "failure", 3), run_(41, "timed_out", 27), run_(40, "success", 51)]
    runs[(SITE, "apps.yml")] = [run_(52, "failure", 10)]       # one failure: not yet
    hub = FakeGitHub(actions={(CRAWL, "data"): True, (ELECTO, "electo"): True, (SITE, "site"): True},
                     wf_runs=runs, files=files(outlook_h=40, legiscan=0))
    watch(monkeypatch, tmp_path, hub, keys=True)
    body = issue_body(hub)
    assert "**incidents.yml** in ClockworkBlack/Electo: 2 runs in a row did not succeed (last: failure" in body
    assert "apps.yml" not in body
    assert "**ga/outlook.json** in Electo is 40 hours old" in body
    assert "the key is set, so see legis-votes' LegiScan step" in body
    assert "YOUTUBE_API_KEY" not in body and "LEGISCAN_API_KEY is not set" not in body


def said_by_line(body):
    return next((line for line in (body or "").splitlines() if "ga/crawl/said_by.json" in line), None)


def test_said_by_is_quiet_when_nothing_waits_and_names_a_failed_exam(monkeypatch, tmp_path):
    # "at" moves only when readings come in: a week old with nothing waiting is no problem
    hub = FakeGitHub(actions={(CRAWL, "data"): True, (ELECTO, "electo"): True, (SITE, "site"): True},
                     wf_runs=healthy_runs(), files=files(said_by_h=24 * 7, said_by={"waiting": 0}))
    watch(monkeypatch, tmp_path, hub, keys=True)
    assert said_by_line(issue_body(hub)) is None


def test_said_by_names_the_exam_that_keeps_failing(monkeypatch, tmp_path):
    stalled = {"waiting": 1234, "stalled_since": "2026-11-19T17:20:00Z",
               "exam": {"ok": False, "m": "phi4", "own_held": 2, "other_held": 12}}
    hub = FakeGitHub(actions={(CRAWL, "data"): True, (ELECTO, "electo"): True, (SITE, "site"): True},
                     wf_runs=healthy_runs(), files=files(said_by_h=24 * 7, said_by=stalled))
    watch(monkeypatch, tmp_path, hub, keys=True)
    line = said_by_line(issue_body(hub))
    assert "last written 7 days ago" in line and "with 1,234 sentences waiting" in line
    assert "its model (phi4) has failed its exam on every runner since 2026-11-19T17:20:00Z, so it reads nothing" in line
    assert "a run of it that ended green wrote nothing" not in line


def test_said_by_with_sentences_waiting_and_no_failed_exam(monkeypatch, tmp_path, capsys):
    hub = FakeGitHub(actions={(CRAWL, "data"): True, (ELECTO, "electo"): True, (SITE, "site"): True},
                     wf_runs=healthy_runs(), files=files(said_by_h=24 * 5, said_by={"waiting": 40}))
    watch(monkeypatch, tmp_path, hub, keys=True)
    line = said_by_line(issue_body(hub))
    assert "last written 5 days ago" in line and "with 40 sentences waiting" in line
    assert "a run of it that ended green wrote nothing (its logs are in logs/runner/)" in line
    assert "said_by" not in capsys.readouterr().out                 # the issue only


def test_ordinances_is_watched_from_the_runners_record(monkeypatch, tmp_path):
    # since Nov 19 it runs here every 6 hours: no success in 50 hours is a restart and, past twice its gap, a problem
    status = {"ordinances": {"fails_in_row": 0, "recent": [7], "last": {"id": 7, "at": iso(50), "conclusion": "success"},
                             "last_success": {"id": 7, "at": iso(50)}}}
    hub = FakeGitHub(actions={(CRAWL, "data"): True, (ELECTO, "electo"): True, (SITE, "site"): True},
                     wf_runs=healthy_runs(), files=files())
    watch(monkeypatch, tmp_path, hub, keys=True, status=status)
    assert ("POST", f"repos/{RUNNER}/actions/workflows/ordinances.yml/dispatches", {"ref": "main"}) in hub.sent
    assert "**ordinances.yml** hasn't succeeded in 50 hours, even after restarts." in issue_body(hub)
    # a success 10 hours ago: nothing done
    status["ordinances"]["last_success"]["at"] = status["ordinances"]["last"]["at"] = iso(10)
    hub = FakeGitHub(actions={(CRAWL, "data"): True, (ELECTO, "electo"): True, (SITE, "site"): True},
                     wf_runs=healthy_runs(), files=files())
    watch(monkeypatch, tmp_path / "again", hub, keys=True, status=status)
    assert not any("ordinances.yml" in p for _, p, _ in hub.sent)


def test_the_pass_rate_watch_reads_said_by_and_outlook_but_not_the_people_file():
    steps = yaml.safe_load(WF.read_text())["jobs"]["watch"]["steps"]
    run = next(s["run"] for s in steps if s.get("name", "").startswith("The open models' pass rates"))
    sparse = re.search(r"git sparse-checkout set --no-cone ((?:[^\n]*\\\n)*[^\n]*)", run).group(1).split()
    for p in ("/ga/crawl/said_by.json", "/ga/outlook.json", "/research/train/outlook_ai_log.jsonl", "/tools/ai/"):
        assert p in sparse, p
    assert not any("people.json" in p for p in sparse)


@pytest.mark.parametrize("yml", ["watchdog.yml", "sync-code.yml", "logview.yml"])
def test_workflow_parses(yml):
    yaml.safe_load((ROOT / ".github" / "workflows" / yml).read_text())
