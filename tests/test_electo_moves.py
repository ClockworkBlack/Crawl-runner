"""Electo's own scheduled and push-started jobs, moved to the public runner (the electo-*.yml workflows).

Until Nov 19, 2026 the private Electo repository ran them on paid minutes: bill-details and incidents every day (and
incidents on every issue event), lawsearch-zip, ordinances and crawl-policing on pushes the runner itself makes, geo on
a change to its scripts. Each one now runs here, in the public runner repository only, with Electo's own scripts on a
partial checkout of Electo. These tests read the workflows and run each step's shell in bash the way the workflow reads
it, against a local git repository standing in for Electo and stand-ins for gh and Electo's scripts:

  - each workflow loads, is guarded to the public runner, has a schedule and a by-hand start;
  - each clones Electo with the token and neither the workflow nor a run prints the token;
  - each `gh workflow run` names its repository (-R) and follows a real push;
  - electo-incidents prints nothing of an issue, whatever Electo's importer prints, and finds a token that may read
    issues;
  - the check that stands in for Electo's push triggers builds when the files changed and not otherwise.
"""
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RUNNER = "ClockworkBlack/Crawl-runner"
TOKEN = "electo-s3cr3t-0001"          # stands in for ELECTO_TOKEN: never to be seen in a step's output
SITE = "site-s3cr3t-0002"
DATA = "data-s3cr3t-0003"
MOVED = ["electo-bill-details.yml", "electo-incidents.yml", "electo-crawl-policing.yml", "electo-geo.yml",
         "electo-ordinances.yml", "electo-lawsearch-zip.yml"]
PUSHERS = {"electo-bill-details.yml": "steps.commit.outputs.pushed", "electo-incidents.yml": "steps.import.outputs.pushed",
           "electo-crawl-policing.yml": "steps.import.outputs.pushed", "electo-geo.yml": "steps.build.outputs.pushed"}
STEP_TIMEOUT = 180
CLONE = 'git clone -q --depth 1 --filter=blob:none --sparse "https://x-access-token:${ELECTO_TOKEN}@github.com/${ELECTO_REPO}.git"'

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def steps(name):
    return [s for j in workflow(name)["jobs"].values() for s in j.get("steps") or []]


def step(name, test):
    hits = [s for s in steps(name) if test(s)]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def shell(run):
    """A step's shell as a main run sees it: a `${{ ... || 'N' }}` default is N, any other expression empty."""
    def sub(m):
        d = re.search(r"\|\|\s*'([^']*)'\s*$", m.group(1).strip())
        return d.group(1) if d else ""
    return re.sub(r"\$\{\{(.*?)\}\}", sub, run, flags=re.S)


def tool(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


GH = r'''echo "GH_TOKEN=$GH_TOKEN gh $*" >> "$FAKE_LOG"
case "$1 $2" in
  "workflow run") exit "${GH_RUN_RC:-0}" ;;
  "api "*)        # the issues probe: refused for the tokens named in GH_NO_ISSUES
    case " ${GH_NO_ISSUES:-} " in *" $GH_TOKEN "*) echo '{"message":"Resource not accessible by personal access token"}'; echo "gh: HTTP 403" >&2; exit 1 ;; esac
    echo '[{"number": 7, "body": "ISSUE-BODY-TEXT"}]'; exit 0 ;;
  "issue list")   cat "$FAKE_ISSUES"; exit 0 ;;
  "release view") case "$*" in *"--json body"*) cat "$FAKE_NOTES" 2>/dev/null; exit 0 ;; esac; exit "${GH_NO_RELEASE:-0}" ;;
  "release upload") exit "${GH_UPLOAD_RC:-0}" ;;
esac
exit 0
'''


def fakes(tmp):
    """gh, sleep and pip stand-ins on PATH; gh writes each call (and the token it was given) to log.txt."""
    b = tmp / "bin"
    tool(b / "gh", GH)
    tool(b / "sleep", "exit 0\n")
    tool(b / "pip", 'echo "pip $*" >> "$FAKE_LOG"\nexit 0\n')
    return b


def electo(tmp, files, refuse=0):
    """A bare repository standing in for Electo, and the git settings that send the workflow's clone URL to it.
    refuse: how many pushes it turns away before taking one (-1: every push)."""
    src = tmp / "electo-src"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    commit(src, "start")
    bare = tmp / "electo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    subprocess.run(["git", "-C", str(src), "remote", "add", "origin", str(bare)], check=True)
    if refuse:
        hook = bare / "hooks" / "pre-receive"
        hook.write_text('#!/bin/sh\nc="$(dirname "$0")/count"; n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$c"\n'
                        f'R={refuse}; if [ "$R" -lt 0 ] || [ "$n" -le "$R" ]; then echo refused >&2; exit 1; fi\n')
        hook.chmod(0o755)
    return bare, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{bare}.insteadOf",
                  "GIT_CONFIG_VALUE_0": f"https://x-access-token:{TOKEN}@github.com/O/Electo.git",
                  "ELECTO_TOKEN": TOKEN, "ELECTO_REPO": "O/Electo"}


def commit(src, msg, files=None):
    """Another commit on the stand-in's main (a push made by someone else), on top of whatever the steps pushed."""
    g = ["git", "-c", "user.name=test", "-c", "user.email=gacrawl-bot@users.noreply.github.com", "-C", str(src)]
    if files is not None:
        subprocess.run(g + ["fetch", "-q", "origin", "main"], check=True)
        subprocess.run(g + ["reset", "-q", "--hard", "FETCH_HEAD"], check=True)
    for rel, text in (files or {}).items():
        p = Path(src) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", msg], check=True)
    if files is not None:
        subprocess.run(g + ["push", "-q", "origin", "HEAD:main"], check=True)


def commits(bare):
    return subprocess.run(["git", "-C", str(bare), "log", "--format=%s", "main"], capture_output=True, text=True,
                          check=True).stdout.splitlines()


def at_main(bare, path):
    r = subprocess.run(["git", "-C", str(bare), "show", f"main:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def env(tmp, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "RUNNER_", "GIT_"))}
    e.update(PATH=f"{tmp / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(tmp / "log.txt"), GITHUB_REPOSITORY=RUNNER,
             GITHUB_REF_NAME="main", GITHUB_OUTPUT=str(tmp / "output.txt"), GITHUB_STEP_SUMMARY=str(tmp / "summary.md"),
             GITHUB_WORKSPACE=str(tmp), RUNNER_TEMP=str(tmp / "rt"), HOME=str(tmp),
             FAKE_ISSUES=str(tmp / "issues.json"), FAKE_NOTES=str(tmp / "notes.txt"))
    (tmp / "rt").mkdir(exist_ok=True)
    e.update({k: str(v) for k, v in kw.items()})
    return e


def bash(script, tmp, e):
    """One step, as GitHub runs it (bash -e), in the workspace. Returns the result, gh's calls and the step's outputs."""
    for f in ("log.txt", "output.txt"):
        (tmp / f).unlink(missing_ok=True)
        (tmp / f).touch()
    r = subprocess.run(["bash", "-e", "-c", script], cwd=tmp, env=e, capture_output=True, text=True, timeout=STEP_TIMEOUT)
    return r, (tmp / "log.txt").read_text(), dict(line.split("=", 1) for line in (tmp / "output.txt").read_text().splitlines())


def run_of(name, test):
    return shell(step(name, test)["run"])


def said(r):
    return r.stdout + r.stderr


# --- what every moved workflow has ----------------------------------------------------------------------------------


def test_each_moved_workflow_runs_only_in_the_public_runner_on_a_schedule_and_by_hand():
    for name in MOVED:
        W = workflow(name)
        assert W["name"] == name[:-4], name
        on = W[True]                                    # PyYAML reads the key `on` as true
        assert "workflow_dispatch" in on and on["schedule"] and all(c["cron"] for c in on["schedule"]), name
        assert not {"push", "pull_request", "issues"} & set(on), name      # nothing in Electo can start a run here
        for job in W["jobs"].values():
            assert job["if"].startswith("github.event.repository.private == false"), name
            assert job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] <= 100, name
        assert W["concurrency"] == {"group": name[:-4], "cancel-in-progress": False}, name
        assert W["permissions"]["contents"] == "read", name


def test_the_starts_after_this_repositorys_own_pushes_name_workflows_that_exist():
    names = {yaml.safe_load(p.read_text())["name"] for p in WORKFLOWS.glob("*.yml")}
    for name in ("electo-ordinances.yml", "electo-lawsearch-zip.yml"):
        wr = workflow(name)[True]["workflow_run"]
        # the two jobs here that push data/ordinances and data/ord_lay.runner.json.gz to Electo
        assert wr == {"workflows": ["ordinances", "ai-law-questions"], "types": ["completed"]}, name
        assert set(wr["workflows"]) <= names
    for pusher, path in (("ordinances.yml", "data/ordinances"), ("ai-law-questions.yml", "data/ord_lay.runner.json.gz")):
        assert path in (WORKFLOWS / pusher).read_text(), pusher


def test_every_run_block_of_the_moved_workflows_parses():
    for name in MOVED:
        for s in steps(name):
            if "run" in s:
                r = subprocess.run(["bash", "-n", "-c", shell(s["run"])], capture_output=True, text=True)
                assert r.returncode == 0, f"{name}: {s.get('name')}: {r.stderr}"


def test_each_clones_electo_with_the_token_and_never_prints_it():
    for name in MOVED:
        text = (WORKFLOWS / name).read_text()
        job = next(iter(workflow(name)["jobs"].values()))
        assert job["env"]["ELECTO_TOKEN"] == "${{ secrets.ELECTO_TOKEN || secrets.DATA_TOKEN }}", name
        assert job["env"]["ELECTO_REPO"] == "${{ vars.ELECTO_REPO || 'ClockworkBlack/Electo' }}", name
        runs = [s["run"] for s in steps(name) if "run" in s]
        assert sum(r.count(CLONE) for r in runs) == 1, name                 # one partial clone, with the token
        assert any('[ -n "$ELECTO_TOKEN" ] ||' in r for r in runs), name   # no token: said, and nothing is tried
        assert not re.search(r"actions/checkout|upload-artifact", text), name      # no attachment, no checkout to leak into
        for r in runs:
            assert not re.search(r"set -[a-z]*x|xtrace|git remote (?:-v|get-url|show)|git config (?:-l|--list|--get)|\benv\b\s*$|printenv", r, re.M), name
            for line in r.splitlines():
                # what a line prints (from its echo, printf or tee on) never names a token: a secret is only ever
                # given to git, gh or a script
                for printed in re.findall(r"\b(?:echo|printf|tee)\b.*", line.split(" # ")[0]):
                    assert not re.search(r"\$\{?(?:\w*TOKEN|T|t)\b", printed), (name, line)
        # a secret reaches a step through env, never written into the script itself
        assert not any("secrets." in r for r in runs), name


def test_each_push_to_electo_is_followed_by_publish_site_with_this_repositorys_token():
    for name, out in PUSHERS.items():
        assert workflow(name)["permissions"].get("actions") == "write", name
        pub = step(name, lambda s: "gh workflow run" in (s.get("run") or ""))
        assert pub["if"] == "${{ !cancelled() && " + out + " == 'true' }}", name
        assert pub["env"] == {"GH_TOKEN": "${{ github.token }}"}, name
        assert re.fullmatch(r'gh workflow run publish-site\.yml -R "\$\{\{ github\.repository \}\}" --ref "\$\{\{ github\.ref_name \}\}" \|\| echo "[^"]+"',
                            pub["run"].strip()), name
        runs = "\n".join(s.get("run") or "" for s in steps(name))
        assert 'git push -q origin HEAD:main' in runs and 'echo "pushed=true" >> "$GITHUB_OUTPUT"' in runs, name
        assert "gacrawl-bot@users.noreply.github.com" in runs, name
    # the two that push nothing to Electo start nothing and need no right to
    for name in ("electo-ordinances.yml", "electo-lawsearch-zip.yml"):
        assert "actions" not in workflow(name)["permissions"], name
        assert not any("gh workflow run" in (s.get("run") or "") or re.search(r"\bgit (?:-C \S+ )?push\b", s.get("run") or "")
                       for s in steps(name)), name


# --- electo-bill-details --------------------------------------------------------------------------------------------

READER = '''import json, os, pathlib, sys
pathlib.Path(os.environ["ARGS_OUT"]).write_text(" ".join(sys.argv[1:]))
p = pathlib.Path("civics/legis/bill_detail.json")
d = json.loads(p.read_text()); d.update(json.loads(os.environ.get("NEW", "{}"))); p.write_text(json.dumps(d, sort_keys=True))
print(f"bill details: {len(d)} on file")
sys.exit(int(os.environ.get("READER_RC", "0")))
'''
BILLS = {"civics/bill_details.py": READER, "civics/fetch_legis.py": "", "civics/legis/bill_detail.json": "{}",
         "ga/pipeline_civics/acts.json": '{"items": []}', "ga/profiles.json": "PRIVATE", "civics/other.py": "PRIVATE"}


def bills_run(tmp, g, **kw):
    """The three shell steps of electo-bill-details in order; a failed read still reaches the commit (`!cancelled()`)."""
    shutil.rmtree(tmp / "rt" / "electo-bills", ignore_errors=True)
    name = "electo-bill-details.yml"
    e = env(tmp, LIMIT=kw.pop("LIMIT", "1500"), ARGS_OUT=tmp / "args.txt", **g, **kw)
    r1, _, _ = bash(run_of(name, lambda s: (s.get("name") or "").startswith("Electo's bill reader")), tmp, e)
    assert r1.returncode == 0, said(r1)
    r2, _, _ = bash(run_of(name, lambda s: (s.get("name") or "").startswith("Read new and changed bills")), tmp, e)
    r3, _, out = bash(run_of(name, lambda s: s.get("id") == "commit"), tmp, e)
    return r1, r2, r3, out


def test_bill_details_reads_on_a_partial_checkout_and_commits_what_was_read(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, BILLS)
    r1, r2, r3, out = bills_run(tmp_path, g, NEW='{"70072": {"fr": "A BILL"}}')
    assert r2.returncode == 0 and r3.returncode == 0, said(r2) + said(r3)
    assert out == {"pushed": "true"} and commits(bare)[0].startswith("Bill records: official summaries, history and votes (")
    assert json.loads(at_main(bare, "civics/legis/bill_detail.json")) == {"70072": {"fr": "A BILL"}}
    assert (tmp_path / "args.txt").read_text() == "--limit 1500 --workers 4"
    # only the four files the reader needs are checked out
    E = tmp_path / "rt" / "electo-bills"
    assert not (E / "ga" / "profiles.json").exists() and not (E / "civics" / "other.py").exists()
    assert TOKEN not in said(r1) + said(r2) + said(r3)
    # nothing new: nothing committed, publish-site not started
    r1, r2, r3, out = bills_run(tmp_path, g, NEW='{"70072": {"fr": "A BILL"}}')
    assert r3.returncode == 0 and out == {} and "No new bill records" in r3.stdout and len(commits(bare)) == 2


def test_bill_details_keeps_what_a_failed_or_timed_out_read_wrote_and_says_which(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, BILLS)
    assert step("electo-bill-details.yml", lambda s: s.get("id") == "commit")["if"] == "${{ !cancelled() }}"
    r1, r2, r3, out = bills_run(tmp_path, g, NEW='{"1": {}}', READER_RC=3)
    assert r2.returncode == 1 and "::error title=Bill records::civics/bill_details.py ended with exit 3" in r2.stdout
    assert out == {"pushed": "true"} and json.loads(at_main(bare, "civics/legis/bill_detail.json")) == {"1": {}}
    r1, r2, r3, out = bills_run(tmp_path, g, NEW='{"2": {}}', READER_RC=124)
    assert r2.returncode == 0 and "::notice::the read stopped at its 80 minutes" in r2.stdout and out == {"pushed": "true"}
    # a limit that is not a number never reaches the command line
    r1, r2, r3, out = bills_run(tmp_path, g, LIMIT="5; touch pwned")
    assert (tmp_path / "args.txt").read_text() == "--limit 1500 --workers 4" and not list(tmp_path.rglob("pwned"))


def test_bill_details_commits_again_on_electos_newest_main_and_fails_when_every_push_is_refused(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, BILLS, refuse=2)
    r1, r2, r3, out = bills_run(tmp_path, g, NEW='{"9": {}}')
    assert r3.returncode == 0 and out == {"pushed": "true"} and r3.stdout.count("the push was refused") == 2
    assert json.loads(at_main(bare, "civics/legis/bill_detail.json")) == {"9": {}}
    (bare / "hooks" / "pre-receive").write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    r1, r2, r3, out = bills_run(tmp_path, g, NEW='{"10": {}}')
    assert r3.returncode == 1 and out == {} and "::error title=Bill records::Electo refused the records five times" in r3.stdout


# --- electo-incidents -----------------------------------------------------------------------------------------------

# Electo's importer, at its most talkative: it prints the whole issue (title and body) on both streams, which the real
# one never does. Nothing of that may reach the step's output.
IMPORTER = '''import json, os, subprocess, sys
repo = sys.argv[sys.argv.index("--repo") + 1]
raw = subprocess.run(["gh", "issue", "list", "-R", repo, "--label", "incident", "--state", "all", "--limit", "1000",
                      "--json", "number,title,body,state,createdAt,updatedAt,url"], capture_output=True, text=True, check=True).stdout
items = [{"number": i["number"], "what": i["body"]} for i in json.loads(raw) if i["state"] == "OPEN"]
print("debug:", raw); print("debug:", raw, file=sys.stderr)
if os.environ.get("IMPORTER_RC"):
    raise ValueError("could not read: " + raw)
old = json.load(open("ga/incidents.json"))
if old.get("items") == items:
    print(f"incidents: unchanged ({len(items)} open reports)"); sys.exit(0)
json.dump({"repo": repo, "items": items}, open("ga/incidents.json", "w"))
print(f"incidents: {len(items)} open reports written to {os.path.abspath('ga/incidents.json')}")
'''
INCIDENTS = {"tools/incidents_import.py": IMPORTER, "ga/incidents.json": '{"items": []}', "ga/profiles.json": "PRIVATE"}
REPORT = "WITNESS-SAW-OFFICER-4417-AT-PONCE"       # stands in for an issue's text


def incidents_run(tmp, g, issues, **kw):
    (tmp / "issues.json").write_text(json.dumps(issues))
    return bash(run_of("electo-incidents.yml", lambda s: s.get("id") == "import"), tmp, env(tmp, DATA_TOKEN=DATA, **g, **kw))


def issue(n, state="OPEN", body=REPORT):
    return {"number": n, "title": "TITLE-" + body, "body": body, "state": state, "url": f"https://github.com/O/Electo/issues/{n}"}


def test_incidents_commits_the_log_and_prints_nothing_of_an_issue(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, INCIDENTS)
    r, log, out = incidents_run(tmp_path, g, [issue(7), issue(8, "CLOSED", "WITHDRAWN-REPORT")])
    assert r.returncode == 0, said(r)
    assert out == {"pushed": "true"} and commits(bare)[0] == "Shared incident log from issues"
    assert json.loads(at_main(bare, "ga/incidents.json")) == {"repo": "O/Electo", "items": [{"number": 7, "what": REPORT}]}
    # the public log: the count, and no word of the issues, their titles, the importer's own lines or a token
    assert "incidents: 1 open reports written" in r.stdout
    for private in (REPORT, "WITHDRAWN", "TITLE-", "debug:", "ISSUE-BODY-TEXT", "issues/7", TOKEN, DATA, str(tmp_path / "rt")):
        assert private not in said(r), private
    # the importer asks for Electo's issues, by name, with the token that may read them
    asked = [line for line in log.splitlines() if " gh issue list " in line]
    assert asked == [f"GH_TOKEN={TOKEN} gh issue list -R O/Electo --label incident --state all --limit 1000 "
                     "--json number,title,body,state,createdAt,updatedAt,url"]
    # only the importer and the log are checked out; nothing left behind holds the importer's output
    assert not (tmp_path / "rt" / "electo-incidents" / "ga" / "profiles.json").exists()
    assert not (tmp_path / "rt" / "incidents.out").exists() and not (tmp_path / "rt" / "incidents.err").exists()
    # the same issues again: nothing committed, nothing published
    r, _, out = incidents_run(tmp_path, g, [issue(7), issue(8, "CLOSED", "WITHDRAWN-REPORT")])
    assert r.returncode == 0 and out == {} and "the shared log is unchanged" in r.stdout and len(commits(bare)) == 2
    # a withdrawn report (its issue closed) leaves the log at the next poll
    r, _, out = incidents_run(tmp_path, g, [issue(7, "CLOSED")])
    assert r.returncode == 0 and out == {"pushed": "true"} and json.loads(at_main(bare, "ga/incidents.json"))["items"] == []
    assert REPORT not in said(r)


def test_incidents_says_only_the_kind_of_error_when_the_importer_fails(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, INCIDENTS)
    r, _, out = incidents_run(tmp_path, g, [issue(7)], IMPORTER_RC=1)
    assert r.returncode == 1 and out == {} and len(commits(bare)) == 1
    assert "::error title=Incidents::tools/incidents_import.py ended with exit 1 (ValueError); ga/incidents.json is kept as it was" in r.stdout
    assert REPORT not in said(r) and "could not read" not in said(r) and "Traceback" not in said(r)


def test_incidents_uses_the_first_token_that_may_read_issues_and_stops_when_none_may(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, INCIDENTS)
    # ELECTO_TOKEN has Contents only: DATA_TOKEN reads the issues, ELECTO_TOKEN still clones and pushes
    r, log, out = incidents_run(tmp_path, g, [issue(7)], GH_NO_ISSUES=TOKEN)
    assert r.returncode == 0 and out == {"pushed": "true"}, said(r)
    probes = [line for line in log.splitlines() if " gh api " in line]
    assert [p.split(" ")[0] for p in probes] == [f"GH_TOKEN={TOKEN}", f"GH_TOKEN={DATA}"]
    assert all("repos/O/Electo/issues?labels=incident&state=all&per_page=1 --silent" in p for p in probes)
    assert [line.split(" ")[0] for line in log.splitlines() if " gh issue list " in line] == [f"GH_TOKEN={DATA}"]
    assert "ISSUE-BODY-TEXT" not in said(r) and "Resource not accessible" not in said(r)
    # neither may: said, with what to grant, and nothing is cloned, imported or pushed
    r, log, out = incidents_run(tmp_path, g, [issue(7, body="OTHER")], GH_NO_ISSUES=f"{TOKEN} {DATA}")
    assert r.returncode == 1 and out == {} and " gh issue list " not in log and len(commits(bare)) == 2
    assert "::error title=Incidents::no token here may read Electo's issues" in r.stdout and "Issues: Read-only on O/Electo" in r.stdout


def test_incidents_asks_about_three_times_an_hour_and_says_so():
    W = workflow("electo-incidents.yml")
    assert W[True]["schedule"] == [{"cron": "16,36,56 * * * *"}]
    text = (WORKFLOWS / "electo-incidents.yml").read_text()
    assert "within about 20 to 60 minutes" in text and "nothing of an issue is printed here" in text
    job = W["jobs"]["import"]
    assert job["env"]["DATA_TOKEN"] == "${{ secrets.DATA_TOKEN }}"
    # the importer's two streams go to files that are removed, and one fixed-shape line is printed from them
    run = step("electo-incidents.yml", lambda s: s.get("id") == "import")["run"]
    assert '> "$RUNNER_TEMP/incidents.out" 2> "$RUNNER_TEMP/incidents.err"' in run
    assert not re.search(r"\b(?:cat|tee|head)\b[^\n]*incidents\.(?:out|err|json)", run)


# --- electo-crawl-policing ------------------------------------------------------------------------------------------

POLICING = '''import os, pathlib, sys
assert sys.argv[1:] == ["--policing-only"], sys.argv
b = pathlib.Path(os.environ["CRAWL_BUNDLE"])
out = pathlib.Path("ga/crawl"); out.mkdir(parents=True, exist_ok=True)
(out / "police.json").write_text("from " + (b / "police.json").read_text())
(out / "donors.json").write_text("older money")
p = pathlib.Path(os.environ["CALLS"]); p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else "1")
print("crawl import (policing only)")
sys.exit(int(os.environ.get("IMPORTER_RC", "0")))
'''
BUNDLE = {"ga/crawl_import.py": POLICING, "data/crawl/police.json": "P1", "data/crawl/media.json": "M1",
          "data/crawl/bill_reasons.json": "B1", "ga/crawl/police.json": "from P1", "ga/crawl/donors.json": "newer money",
          "data/crawl/packets/a.json": "PACKET"}


def policing_look(tmp, g, marker=None):
    m = tmp / ".electo-policing-done"
    m.unlink(missing_ok=True)
    if marker is not None:
        m.write_text(marker + "\n")
    r, _, out = bash(run_of("electo-crawl-policing.yml", lambda s: s.get("id") == "look"), tmp, env(tmp, ELECTO_PATH="data/crawl", **g))
    assert r.returncode == 0 and TOKEN not in said(r), said(r)
    return out


def policing_import(tmp, g, key, **kw):
    (tmp / "calls.txt").unlink(missing_ok=True)
    r, log, out = bash(run_of("electo-crawl-policing.yml", lambda s: s.get("id") == "import"), tmp,
                       env(tmp, ELECTO_PATH="data/crawl", KEY=key, CALLS=tmp / "calls.txt", **g, **kw))
    return r, out


def test_crawl_policing_imports_bundle_files_nobody_imported_and_only_those(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, BUNDLE)
    out = policing_look(tmp_path, g)
    key = out["key"]
    assert out["go"] == "true" and re.fullmatch(r"[0-9a-f]{20}", key)
    # the files are already imported (police.yml ran the importer in its own commit): nothing committed, and remembered
    r, o = policing_import(tmp_path, g, key)
    assert r.returncode == 0 and o == {} and "the Policing tab's files are current" in r.stdout and len(commits(bare)) == 1
    assert (tmp_path / ".electo-policing-done").read_text().strip() == key
    assert policing_look(tmp_path, g, marker=key)["go"] == "false"
    # a push of a bundle file by someone else (a person, or a crawl whose import then stopped): another key, imported
    commit(tmp_path / "electo-src", "crawl data", {"data/crawl/police.json": "P2"})
    out = policing_look(tmp_path, g, marker=key)
    assert out["go"] == "true" and out["key"] != key
    r, o = policing_import(tmp_path, g, out["key"])
    assert r.returncode == 0 and o == {"pushed": "true"} and commits(bare)[0].startswith("Policing data from Crawl: ")
    assert at_main(bare, "ga/crawl/police.json") == "from P2"
    assert at_main(bare, "ga/crawl/donors.json") == "newer money"       # the money jobs' file is never committed here
    assert (tmp_path / ".electo-policing-done").read_text().strip() == out["key"]
    assert not (tmp_path / "rt" / "electo-policing" / "data" / "crawl" / "packets").exists()     # the packets stay out
    # a file that is not one of the five does not start an import; a change put back does (the last import was of P2)
    commit(tmp_path / "electo-src", "money", {"data/crawl/donors.json": "D"})
    assert policing_look(tmp_path, g, marker=out["key"])["go"] == "false"
    commit(tmp_path / "electo-src", "back", {"data/crawl/police.json": "P1"})
    assert policing_look(tmp_path, g, marker=out["key"]) == {"key": key, "go": "true"}


def test_crawl_policing_imports_again_on_electos_newest_main_after_a_refused_push(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, dict(BUNDLE, **{"data/crawl/police.json": "P3"}), refuse=1)
    key = policing_look(tmp_path, g)["key"]
    r, o = policing_import(tmp_path, g, key)
    assert r.returncode == 0 and o == {"pushed": "true"} and "the push was refused (try 1)" in r.stdout, said(r)
    assert (tmp_path / "calls.txt").read_text() == "2" and at_main(bare, "ga/crawl/police.json") == "from P3"
    assert len(commits(bare)) == 2                                     # one commit, not the refused one as well


def test_crawl_policing_commits_and_remembers_nothing_when_the_importer_fails(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, dict(BUNDLE, **{"data/crawl/police.json": "P4"}))
    key = policing_look(tmp_path, g)["key"]
    r, o = policing_import(tmp_path, g, key, IMPORTER_RC=4)
    assert r.returncode == 1 and o == {} and len(commits(bare)) == 1
    assert "::error title=Policing import::ga/crawl_import.py --policing-only ended with exit 4; nothing committed to Electo" in r.stdout
    assert not (tmp_path / ".electo-policing-done").exists()
    W = "electo-crawl-policing.yml"
    assert step(W, lambda s: (s.get("uses") or "").startswith("actions/cache/save@"))["if"] == "steps.import.outcome == 'success'"
    # by hand it always imports: the marker is not read
    assert step(W, lambda s: s.get("id") == "seen")["if"] == "github.event_name != 'workflow_dispatch'"


def test_the_markers_are_the_newest_of_their_own_kind():
    # each run that handled files leaves a marker named by its run; the next takes the newest (never an exact match),
    # so a file changed and then put back is seen as changed (an exact match on the old key would say "handled")
    for name, prefix, path in (("electo-crawl-policing.yml", "electo-crawl-policing-", ".electo-policing-done"),
                               ("electo-geo.yml", "electo-geo-", ".electo-geo-built"),
                               ("electo-ordinances.yml", "electo-ordinances-", ".electo-ordinances-done")):
        restore = step(name, lambda s: (s.get("uses") or "").startswith("actions/cache/restore@"))
        save = step(name, lambda s: (s.get("uses") or "").startswith("actions/cache/save@"))
        assert restore["with"] == {"path": path, "key": prefix + "${{ github.run_id }}", "restore-keys": prefix}, name
        assert save["with"] == {"path": path, "key": prefix + "${{ github.run_id }}"}, name
        assert restore["continue-on-error"] is True and save["continue-on-error"] is True, name
        order = [s.get("id") or s.get("uses", "").split("@")[0] for s in steps(name)]
        assert order.index("seen") < order.index("look") < order.index("actions/cache/save"), name


# --- electo-geo -----------------------------------------------------------------------------------------------------

GEOLOOKUP = '''import os, pathlib
pathlib.Path("ga/lookup.json").write_text(os.environ.get("LOOKUP", "L1"))
pathlib.Path("ga/lookup_shapes.json").write_text("S1")
print("zips 1")
'''
GEOADDR = '''import os, pathlib
d = pathlib.Path("ga/addr"); d.mkdir(exist_ok=True)
for old in d.glob("*.json"): old.unlink()
for n in os.environ.get("SHARDS", "3000").split(","): (d / f"{n}.json").write_text("{}")
print("shards")
'''
GEO = {"ga/geolookup.py": GEOLOOKUP, "ga/geoaddr.py": GEOADDR, "ga/ga-topo.json": "{}", "ga/lookup.json": "L1",
       "ga/lookup_shapes.json": "S1", "ga/addr/3000.json": "{}", "ga/addr/3001.json": "{}", "ga/profiles.json": "PRIVATE"}


def geo_look(tmp, g, marker=None, by_hand="false"):
    m = tmp / ".electo-geo-built"
    m.unlink(missing_ok=True)
    if marker is not None:
        m.write_text(marker + "\n")
    r, _, out = bash(run_of("electo-geo.yml", lambda s: s.get("id") == "look"), tmp, env(tmp, BY_HAND=by_hand, **g))
    assert r.returncode == 0 and TOKEN not in said(r), said(r)
    return r, out


def test_geo_builds_when_a_script_changed_and_not_on_its_first_check(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, GEO)
    # the first check here: nothing is built (Electo's workflow built on every push until now), and that is remembered
    r, out = geo_look(tmp_path, g)
    assert out == {"first": "true", "go": "false", "remember": "true"} and "::notice::first check here" in r.stdout
    key = (tmp_path / ".electo-geo-built").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{20}", key)
    # the same scripts: nothing to do, nothing new to remember
    r, out = geo_look(tmp_path, g, marker=key)
    assert out == {"go": "false"} and "built from these scripts: nothing to do" in r.stdout
    # a push that leaves the scripts alone starts nothing; one that changes either script builds
    commit(tmp_path / "electo-src", "data", {"ga/profiles.json": "P2"})
    assert geo_look(tmp_path, g, marker=key)[1] == {"go": "false"}
    commit(tmp_path / "electo-src", "script", {"ga/geoaddr.py": GEOADDR + "# changed\n"})
    r, out = geo_look(tmp_path, g, marker=key)
    assert out == {"go": "true", "remember": "true"} and "a build script changed" in r.stdout
    assert (tmp_path / ".electo-geo-built").read_text().strip() != key
    # by hand: always
    assert geo_look(tmp_path, g, marker=(tmp_path / ".electo-geo-built").read_text().strip(), by_hand="true")[1] == {"go": "true"}
    W = "electo-geo.yml"
    assert workflow(W)["jobs"]["build"]["env"]["BY_HAND"] == "${{ github.event_name == 'workflow_dispatch' && 'true' || 'false' }}"
    assert step(W, lambda s: (s.get("uses") or "").startswith("actions/cache/save@"))["if"] == \
        "${{ !cancelled() && steps.look.outputs.remember == 'true' && (steps.build.outcome == 'success' || steps.look.outputs.first == 'true') }}"
    assert step(W, lambda s: s.get("id") == "build")["if"] == "steps.look.outputs.go == 'true'"


def geo_build(tmp, g, **kw):
    geo_look(tmp, g, by_hand="true")
    return bash(run_of("electo-geo.yml", lambda s: s.get("id") == "build"), tmp, env(tmp, **g, **kw))


def test_geo_commits_the_lookup_files_with_removed_shards_and_only_when_they_changed(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, GEO)
    r, log, out = geo_build(tmp_path, g, SHARDS="3000,3001")
    assert r.returncode == 0 and out == {} and "the lookup files did not change" in r.stdout and len(commits(bare)) == 1, said(r)
    assert "pip install -q pyshp==2.3.1 shapely==2.0.6" in log
    r, log, out = geo_build(tmp_path, g, SHARDS="3000,3002", LOOKUP="L2")
    assert r.returncode == 0 and out == {"pushed": "true"}, said(r)
    assert commits(bare)[0] == "Geo lookup: ZIP code and place district shares from Census files"
    assert at_main(bare, "ga/lookup.json") == "L2" and at_main(bare, "ga/addr/3002.json") == "{}"
    assert at_main(bare, "ga/addr/3001.json") is None                   # a shard the build no longer makes is removed
    assert at_main(bare, "ga/profiles.json") == "PRIVATE" and TOKEN not in said(r)
    assert not (tmp_path / "rt" / "electo-geo" / "ga" / "profiles.json").exists()


def test_geo_puts_its_build_on_electos_newest_main_after_a_refused_push(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, GEO, refuse=1)
    r, _, out = geo_build(tmp_path, g, SHARDS="3005", LOOKUP="L3")
    assert r.returncode == 0 and out == {"pushed": "true"} and "the push was refused (try 1)" in r.stdout, said(r)
    assert at_main(bare, "ga/lookup.json") == "L3" and at_main(bare, "ga/addr/3005.json") == "{}"
    assert at_main(bare, "ga/addr/3000.json") is None and len(commits(bare)) == 2


# --- electo-ordinances ----------------------------------------------------------------------------------------------

PUBLISHER = '''echo "$TOKEN" > "$TOKEN_SEEN"; echo "${SITE_DATA_REPO}" > "$REPO_SEEN"
n=$(( $(cat "$TRIES" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$TRIES"
[ "$n" -gt "${FAIL_FIRST:-0}" ] || { echo "ordinances: push refused"; exit 1; }
echo "ordinances: published to ${SITE_DATA_REPO} (branch ordinances)"
'''
ORD = {"tools/ordinances_publish.sh": PUBLISHER, "ga/ordinances_build.py": "", "data/ordinances/index.json": "{}",
       "data/ordinances/docs/a.json": "{}", "data/ord_lay.1.json.gz": "q1", "data/crawl/police.json": "P", "ga/profiles.json": "PRIVATE"}


def ord_look(tmp, g, name="electo-ordinances.yml", marker=None, **kw):
    m = tmp / ".electo-ordinances-done"
    m.unlink(missing_ok=True)
    if marker is not None:
        m.write_text(marker + "\n")
    r, log, out = bash(run_of(name, lambda s: s.get("id") == "look"), tmp,
                       env(tmp, **{"SITE_TOKEN": SITE, "SITE_DATA_REPO": "O/electo-data", **g, **kw}))
    assert TOKEN not in said(r) and SITE not in said(r), said(r)
    return r, log, out


def ord_publish(tmp, g, key, **kw):
    (tmp / "tries.txt").unlink(missing_ok=True)
    (tmp / ".electo-ordinances-done").unlink(missing_ok=True)
    return bash(run_of("electo-ordinances.yml", lambda s: s.get("id") == "publish"), tmp,
                env(tmp, SITE_TOKEN=SITE, SITE_DATA_REPO="O/electo-data", KEY=key, TRIES=tmp / "tries.txt",
                    TOKEN_SEEN=tmp / "token.txt", REPO_SEEN=tmp / "repo.txt", **g, **kw))


def test_ordinances_publishes_when_the_files_changed_with_the_site_token(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, ORD)
    r, _, out = ord_look(tmp_path, g)
    key = out["key"]
    assert r.returncode == 0 and out["go"] == "true"
    r, _, o = ord_publish(tmp_path, g, key)
    assert r.returncode == 0 and "ordinances: published to O/electo-data (branch ordinances)" in r.stdout, said(r)
    # Electo's publisher gets the token that may write electo-data, and nothing prints it
    assert (tmp_path / "token.txt").read_text().strip() == SITE and (tmp_path / "repo.txt").read_text().strip() == "O/electo-data"
    assert SITE not in said(r) and TOKEN not in said(r)
    assert (tmp_path / ".electo-ordinances-done").read_text().strip() == key
    E = tmp_path / "rt" / "electo-ordinances"
    assert (E / "data" / "ordinances" / "docs" / "a.json").exists() and (E / "data" / "ord_lay.1.json.gz").exists()
    assert not (E / "data" / "crawl").exists() and not (E / "ga" / "profiles.json").exists()
    # the same files: nothing to do. A new download, a new question file or a changed builder: another key
    assert ord_look(tmp_path, g, marker=key)[2] == {"key": key, "go": "false"}
    commit(tmp_path / "electo-src", "police data", {"data/crawl/police.json": "P2"})
    assert ord_look(tmp_path, g, marker=key)[2] == {"key": key, "go": "false"}
    seen = {key}
    for msg, files in (("Downloaded ordinances from Crawl", {"data/ordinances/docs/b.json": "{}"}),
                       ("Everyday questions", {"data/ord_lay.runner.json.gz": "q2"}),
                       ("builder", {"ga/ordinances_build.py": "# changed\n"})):
        commit(tmp_path / "electo-src", msg, files)
        out = ord_look(tmp_path, g, marker=key)[2]
        assert out["go"] == "true" and out["key"] not in seen, msg
        seen.add(out["key"])


def test_ordinances_tries_again_when_the_site_publish_pushed_first_and_fails_after_three(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, ORD)
    key = ord_look(tmp_path, g)[2]["key"]
    r, _, _ = ord_publish(tmp_path, g, key, FAIL_FIRST=1)
    assert r.returncode == 0 and "not published (exit 1; try 1)" in r.stdout and (tmp_path / "tries.txt").read_text().strip() == "2"
    assert (tmp_path / ".electo-ordinances-done").exists()
    r, _, _ = ord_publish(tmp_path, g, key, FAIL_FIRST=9)
    assert r.returncode == 1 and (tmp_path / "tries.txt").read_text().strip() == "3"
    assert "::error title=Ordinances::tools/ordinances_publish.sh failed three times (exit 1)" in r.stdout
    assert not (tmp_path / ".electo-ordinances-done").exists()          # not remembered: the next start builds again
    # without a token for electo-data the run stops at its first step (Electo's publisher would end green with nothing)
    r, _, out = ord_look(tmp_path, g, SITE_TOKEN="")
    assert r.returncode == 1 and "can't be written" in r.stdout and out == {}
    env_ = workflow("electo-ordinances.yml")["jobs"]["publish"]["env"]
    assert env_["SITE_TOKEN"] == "${{ secrets.SITE_DATA_TOKEN || secrets.ELECTO_TOKEN }}"
    assert step("electo-ordinances.yml", lambda s: (s.get("uses") or "").startswith("actions/cache/save@"))["if"] == \
        "steps.publish.outcome == 'success' && hashFiles('.electo-ordinances-done') != ''"


# --- electo-lawsearch-zip -------------------------------------------------------------------------------------------

ZIPPER = '''mkdir -p "$(dirname "$1")"; [ "${ZIP_RC:-0}" = 0 ] || { echo "export failed"; exit "$ZIP_RC"; }
echo "PRIVATE question writer" > "$1"; echo "wrote $1 (0.1 MB)"
'''
LAW = {"tools/train/make_lawsearch_zip.sh": ZIPPER, "data/ordinances/index.json": "{}", "data/ordinances/docs/a.json": "{}",
       "data/ord_lay.1.json.gz": "q1", "ga/ordinances_build.py": "", "ga/pipeline_civics/ocga_index.json": "{}",
       "research/train/law_bench.jsonl": "", "ga/profiles.json": "PRIVATE"}


def law_look(tmp, g, notes="", by_hand="false"):
    (tmp / "notes.txt").write_text(notes)
    r, log, out = bash(run_of("electo-lawsearch-zip.yml", lambda s: s.get("id") == "look"), tmp, env(tmp, BY_HAND=by_hand, **g))
    assert r.returncode == 0 and TOKEN not in said(r), said(r)
    return r, log, out


def test_lawsearch_zip_builds_when_the_release_holds_a_zip_of_other_files(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, LAW)
    r, log, out = law_look(tmp_path, g, notes="electo_lawsearch.zip ... Rebuilt by the lawsearch-zip workflow, from abc1234. OWNER-NOTE")
    key = out["key"]
    assert out["go"] == "true" and "OWNER-NOTE" not in said(r)          # the release's notes are read, not printed
    assert f"GH_TOKEN={TOKEN} gh release view lawsearch -R O/Electo --json body --jq .body" in log
    done = f"... from abc1234 (files {key})."
    assert law_look(tmp_path, g, notes=done)[2] == {"key": key, "go": "false"}
    assert law_look(tmp_path, g, notes=done, by_hand="true")[2]["go"] == "true"
    # a new download changes the index; a new question file counts too; one document alone does not (Electo's trigger)
    commit(tmp_path / "electo-src", "doc", {"data/ordinances/docs/b.json": "{}"})
    assert law_look(tmp_path, g, notes=done)[2]["go"] == "false"
    commit(tmp_path / "electo-src", "index", {"data/ordinances/index.json": '{"documents": [1]}'})
    k2 = law_look(tmp_path, g, notes=done)[2]
    assert k2["go"] == "true" and k2["key"] != key
    commit(tmp_path / "electo-src", "questions", {"data/ord_lay.runner.json.gz": "q2"})
    k3 = law_look(tmp_path, g, notes=f"(files {k2['key']})")[2]
    assert k3["go"] == "true" and k3["key"] not in (key, k2["key"])


def law_build(tmp, g, **kw):
    key = law_look(tmp, g)[2]["key"]
    r, log, _ = bash(run_of("electo-lawsearch-zip.yml", lambda s: (s.get("name") or "").startswith("Build the zip")), tmp,
                     env(tmp, KEY=key, **g, **kw))
    return r, [line for line in log.splitlines() if " gh release " in line], key


def test_lawsearch_zip_goes_to_electos_private_release_and_nowhere_public(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, LAW)
    r, calls, key = law_build(tmp_path, g)
    assert r.returncode == 0, said(r)
    assert all(c.startswith(f"GH_TOKEN={TOKEN} gh release ") and " -R O/Electo" in c for c in calls), calls
    assert [c.split(" gh release ")[1].split(" ")[0] for c in calls] == ["view", "upload", "edit"]
    assert " upload lawsearch data/electo_lawsearch.zip -R O/Electo --clobber" in calls[1]
    # the notes are written after the upload and name the files the zip was built from (what the next start reads)
    assert f"(files {key})." in calls[2] and "Crawl-runner's electo-lawsearch-zip workflow" in calls[2]
    assert "On Electo's lawsearch release:" in r.stdout and TOKEN not in said(r) and "PRIVATE" not in said(r)
    E = tmp_path / "rt" / "electo-lawsearch"
    assert (E / "ga" / "pipeline_civics" / "ocga_index.json").exists() and not (E / "ga" / "profiles.json").exists()
    assert len(commits(bare)) == 1                                       # nothing is committed to Electo
    # no release yet: it is made (a prerelease, not the latest) before the upload
    r, calls, key = law_build(tmp_path, g, GH_NO_RELEASE=1)
    assert [c.split(" gh release ")[1].split(" ")[0] for c in calls] == ["view", "create", "upload", "edit"]
    assert "--prerelease --latest=false" in calls[1]
    # the zip holds Electo's question writer: it is never attached to a run in this public repository
    text = (WORKFLOWS / "electo-lawsearch-zip.yml").read_text()
    assert "upload-artifact" not in text and "-R \"$ELECTO_REPO\"" in text


def test_lawsearch_zip_leaves_the_release_alone_when_the_build_or_the_upload_fails(tmp_path):
    fakes(tmp_path)
    bare, g = electo(tmp_path, LAW)
    r, calls, _ = law_build(tmp_path, g, ZIP_RC=2)
    assert r.returncode == 1 and calls == []
    assert "::error title=Law-search zip::tools/train/make_lawsearch_zip.sh ended with exit 2; the release keeps its zip" in r.stdout
    # a failed upload leaves the old notes, so the next start builds again
    r, calls, _ = law_build(tmp_path, g, GH_UPLOAD_RC=1)
    assert r.returncode == 1 and [c.split(" gh release ")[1].split(" ")[0] for c in calls] == ["view", "upload"]
