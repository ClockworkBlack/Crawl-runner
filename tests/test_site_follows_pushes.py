"""The jobs that push to Electo start publish-site; ga-money hands over the newest Georgia filings; news-research says
when its research or its push failed; the results job keeps the counties' local races current. Each step's shell is
run in bash the way the workflow reads it, against stand-ins for gh, gacrawl, Electo's scripts and (for the steps that
commit) a local git repository standing in for Electo.

Until Nov 19, 2026:
  - publish-site's schedule was the only thing that put the pushes of grand-cross, fec-reread, ga-money and police on
    the site, and GitHub runs about two in five of its 20-minute slots in the runner repository: they waited up to 75
    minutes (median 26);
  - ga-money read only the years not yet read in full and, with every one read, handed over nothing (runs of a
    minute and a half), while the crawl's Georgia step found no time left in every merge: no new Georgia filing reached
    Electo from Nov 14 on;
  - news-research printed "no research updates this run" for any failure of research_ai.py, and a push Electo refused
    five times ended green;
  - nothing refreshed ga/local_2026.json and ga/bills_local.json after election week (every county on its Nov 4 count).
"""
import base64
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
STOPPED = ROOT / "scripts" / "stopped.sh"
RUNNER = "ClockworkBlack/Crawl-runner"
PUSHERS = ["grand-cross.yml", "fec-reread.yml", "ga-money.yml", "police.yml", "history.yml", "results.yml"]

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


def fakes(tmp):
    """gh and sleep stand-ins on PATH; gh writes each call (and the token it was given) to log.txt."""
    b = tmp / "bin"
    tool(b / "gh", 'echo "GH_TOKEN=$GH_TOKEN gh $*" >> "$FAKE_LOG"\n'
                   'case "$*" in "workflow run"*) exit "${GH_RUN_RC:-0}";; esac\nexit 0\n')
    tool(b / "sleep", "exit 0\n")
    return b


def env(tmp, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "RUNNER_"))}
    e.update(PATH=f"{tmp / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(tmp / "log.txt"), GITHUB_REPOSITORY=RUNNER,
             GITHUB_REF_NAME="main", GITHUB_OUTPUT=str(tmp / "output.txt"), GITHUB_STEP_SUMMARY=str(tmp / "summary.md"),
             GITHUB_WORKSPACE=str(tmp), RUNNER_TEMP=str(tmp / "rt"), GH_TOKEN="data-token", RUN_TOKEN="run-token",
             IS_MAIN="true", HOME=str(tmp))
    (tmp / "rt").mkdir(exist_ok=True)
    e.update({k: str(v) for k, v in kw.items()})
    return e


def bash(script, tmp, e, cwd=None):
    for f in ("log.txt", "output.txt"):
        (tmp / f).unlink(missing_ok=True)
        (tmp / f).touch()
    r = subprocess.run(["bash", "-e", "-c", script], cwd=cwd or tmp, env=e, capture_output=True, text=True, timeout=120)
    return r, (tmp / "log.txt").read_text(), (tmp / "output.txt").read_text()


def dispatches(log):
    return [line for line in log.splitlines() if " gh workflow run publish-site.yml" in line]


# --- every job that pushes to Electo starts publish-site ------------------------------------------------------------


def test_every_job_that_pushes_to_electo_starts_publish_site():
    for name in PUSHERS:
        W = workflow(name)
        assert W["permissions"].get("actions") == "write", name
        runs = [s["run"] for s in steps(name) if "run" in s]
        hits = [line for r in runs for line in r.splitlines() if re.search(r"gh workflow run publish-site\.yml\b", line)]
        assert hits, name
        assert all(re.search(r"(?:^|\s)-R\s", h) for h in hits), name
    for name in ("grand-cross.yml", "publish-site.yml"):
        assert "within 20 minutes" not in (WORKFLOWS / name).read_text(), name


def test_a_burst_of_starts_never_cuts_off_a_publish():
    W = workflow("publish-site.yml")
    # one running and one waiting: a newer start replaces only the waiting run, never the one publishing
    assert W["concurrency"] == {"group": "publish-site", "cancel-in-progress": False}
    check = step("publish-site.yml", lambda s: s.get("id") == "check")
    assert "commits/main" in check["run"] and "current.json" in check["run"]    # the waiting run compares at its start


def test_the_dispatch_uses_this_repositorys_own_token_where_gh_holds_the_data_token():
    # scripts/ci_runner.sh mode puts the private repository's token in GH_TOKEN for the whole job; the start of a run
    # here takes the job's own token, inline, whichever wins between a step's env and $GITHUB_ENV
    for name in ("fec-reread.yml", "ga-money.yml", "police.yml"):
        assert any("ci_runner.sh mode" in (s.get("run") or "") for s in steps(name)), name
        hits = [(s, line) for s in steps(name) for line in (s.get("run") or "").splitlines()
                if "gh workflow run publish-site.yml" in line]
        assert hits, name
        for s, line in hits:
            assert 'GH_TOKEN="$RUN_TOKEN" gh workflow run' in line, (name, line)
            assert s["env"]["RUN_TOKEN"] == "${{ github.token }}", name


def test_grand_cross_and_police_start_publish_site_only_after_a_push():
    pub = step("grand-cross.yml", lambda s: s.get("name") == "Publish the site now")
    assert pub["if"] == "${{ !cancelled() && steps.rebuild.outputs.pushed == 'true' }}"
    rebuild = step("grand-cross.yml", lambda s: s.get("id") == "rebuild")["run"]
    assert re.search(r'if git push -q origin HEAD:main; then\n.*echo "pushed=true" >> "\$GITHUB_OUTPUT"', rebuild)
    pub = step("police.yml", lambda s: s.get("name") == "Publish the site now")
    assert pub["if"] == "${{ !cancelled() && steps.electo.outputs.pushed == 'true' && github.event.repository.private == false }}"
    push = step("police.yml", lambda s: s.get("id") == "electo")["run"]
    assert 'git push -q origin HEAD:main && { echo "pushed to Electo"; echo "pushed=true" >> "$GITHUB_OUTPUT"; return 0; }' in push


def electo(tmp, files):
    """A bare repository standing in for Electo, and the git settings that send the workflow's clone URL to it."""
    src = tmp / "electo-src"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    g = ["git", "-c", "user.name=test", "-c", "user.email=gacrawl-bot@users.noreply.github.com"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(g + ["-C", str(src), "add", "-A"], check=True)
    subprocess.run(g + ["-C", str(src), "commit", "-qm", "start"], check=True)
    bare = tmp / "electo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{bare}.insteadOf",
                  "GIT_CONFIG_VALUE_0": "https://x-access-token:tok@github.com/O/Electo.git",
                  "ELECTO_TOKEN": "tok", "ELECTO_REPO": "O/Electo"}


def commits(bare):
    return subprocess.run(["git", "-C", str(bare), "log", "--format=%s", "main"], capture_output=True, text=True,
                          check=True).stdout.splitlines()


def test_grand_cross_marks_a_push_and_not_a_build_that_changed_nothing(tmp_path):
    fakes(tmp_path)
    builder = ("import os, pathlib\np = pathlib.Path('ga/crawl/gc/money.json'); p.parent.mkdir(parents=True, exist_ok=True)\n"
               "p.write_text(os.environ.get('MONEY', '{}'))\nprint('built')\n")
    bare, g = electo(tmp_path, {"ga/gc_build.py": builder, "ga/crawl/gc/money.json": "{}", "tools/data_audit.py": ""})
    run = shell(step("grand-cross.yml", lambda s: s.get("id") == "rebuild")["run"])
    r, _, out = bash(run, tmp_path, env(tmp_path, FORCE="", MONEY='{"a": 1}', **g))
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "pushed=true\n" and commits(bare)[0].startswith("Grand Cross: money and strongest ties")
    r, _, out = bash(run, tmp_path, env(tmp_path, FORCE="", MONEY='{"a": 1}', **g))
    assert r.returncode == 0 and out == "" and "Grand Cross already current" in r.stdout


# --- fec-reread: each round's money push starts publish-site --------------------------------------------------------


def fec_tree(tmp):
    tool(tmp / "bin" / "gacrawl", 'echo "gacrawl $*" >> "$FAKE_LOG"\n')
    tool(tmp / "scripts" / "fec_handoff.sh", 'echo "handoff" >> "$FAKE_LOG"\n')
    tool(tmp / "scripts" / "money_to_electo.sh", 'echo "push $1" >> "$FAKE_LOG"\nexit "${PUSH_RC:-0}"\n')
    (tmp / "scripts" / "fec_left.py").write_text(
        "import os, pathlib\np = pathlib.Path(os.environ['FAKE_LOG']).with_name('left_calls')\n"
        "n = int(p.read_text()) if p.exists() else 0\np.write_text(str(n + 1))\n"
        "print(os.environ['LEFTS'].split(',')[min(n, len(os.environ['LEFTS'].split(',')) - 1)])\n")
    shutil.copy(STOPPED, tmp / "scripts" / "stopped.sh")


def test_fec_reread_starts_publish_site_after_each_money_push(tmp_path):
    fakes(tmp_path)
    fec_tree(tmp_path)
    run = shell(step("fec-reread.yml", lambda s: s.get("id") == "read")["run"])
    r, log, _ = bash(run, tmp_path, env(tmp_path, FEC_API_KEY="k", LEFTS="5,0"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert [line.split(" ", 1)[1] for line in log.splitlines() if line.startswith("push ")] == \
        ["FEC re-read, round 1", "FEC re-read, round 2"]
    d = dispatches(log)
    assert len(d) == 2 and all(x.startswith("GH_TOKEN=run-token ") and f"-R {RUNNER} --ref main" in x for x in d)
    (tmp_path / "left_calls").unlink()
    r, log, _ = bash(run, tmp_path, env(tmp_path, FEC_API_KEY="k", LEFTS="0", PUSH_RC=1))
    assert r.returncode == 0 and not dispatches(log) and "::warning::the money push of round 1 did not finish" in r.stdout


# --- ga-money: the newest filings are read and handed over every run ------------------------------------------------


def ga_tree(tmp):
    tool(tmp / "bin" / "gacrawl", '''echo "gacrawl $*" >> "$FAKE_LOG"
case "$*" in
  *"--mode refresh"*) echo "ethics peachfile 2026: lines"; echo "{\\"added\\": ${REFRESH_ADDED:-0}, \\"pages\\": 6, \\"mode\\": \\"refresh\\"}" ;;
  *"--mode full"*)
    c="$(dirname "$FAKE_LOG")/full_calls"; n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$c"
    p=$(echo "$FULL_PAGES" | cut -d, -f"$n"); echo "{\\"added\\": 1, \\"pages\\": ${p:-0}, \\"mode\\": \\"full\\"}" ;;
  *progress*) echo "{}" ;;
esac
''')
    tool(tmp / "scripts" / "ga_handoff.sh", 'echo "handoff" >> "$FAKE_LOG"\n')
    tool(tmp / "scripts" / "money_to_electo.sh", 'echo "push $1" >> "$FAKE_LOG"\nexit "${PUSH_RC:-0}"\n')
    shutil.copy(STOPPED, tmp / "scripts" / "stopped.sh")
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / "crawl.db").write_bytes(b"")


def ga_run(tmp, **kw):
    (tmp / "full_calls").unlink(missing_ok=True)
    run = shell(step("ga-money.yml", lambda s: s.get("id") == "read")["run"])
    r, log, _ = bash(run, tmp, env(tmp, **kw))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [line for line in log.splitlines() if line.startswith(("gacrawl ethics run", "handoff", "push "))]
    return r.stdout, calls, dispatches(log)


def test_ga_money_hands_over_the_newest_filings_when_every_full_read_is_done(tmp_path):
    fakes(tmp_path)
    ga_tree(tmp_path)
    out, calls, d = ga_run(tmp_path, FULL_PAGES="0", REFRESH_ADDED=7)
    assert calls[0].startswith("gacrawl ethics run --mode refresh --no-entities --max-minutes 15")
    assert calls[1].startswith("gacrawl ethics run --mode full --no-entities")
    assert calls[2:] == ["handoff", "push Georgia money, newest filings"]
    assert len(d) == 1 and d[0].startswith("GH_TOKEN=run-token ") and f"-R {RUNNER} --ref main" in d[0]
    # nothing new: nothing handed over, pushed or published
    out, calls, d = ga_run(tmp_path, FULL_PAGES="0", REFRESH_ADDED=0)
    assert [c.split(" --")[0] for c in calls] == ["gacrawl ethics run", "gacrawl ethics run"] and not d
    assert "no new Georgia filings since the stored database; nothing to hand over" in out


def test_ga_money_sends_the_newest_filings_with_the_first_full_round(tmp_path):
    fakes(tmp_path)
    ga_tree(tmp_path)
    out, calls, d = ga_run(tmp_path, FULL_PAGES="120,0", REFRESH_ADDED=7)
    assert [c for c in calls if not c.startswith("gacrawl")] == ["handoff", "push Georgia money, round 1"]
    assert len(d) == 1
    # a refused money push publishes nothing; off main the lines are handed over but nothing is pushed
    out, calls, d = ga_run(tmp_path, FULL_PAGES="0", REFRESH_ADDED=3, PUSH_RC=1)
    assert calls[-2:] == ["handoff", "push Georgia money, newest filings"] and not d
    assert "::warning::the money push of newest filings did not finish" in out
    out, calls, d = ga_run(tmp_path, FULL_PAGES="0", REFRESH_ADDED=3, IS_MAIN="false")
    assert calls[-1] == "handoff" and not d


def test_ga_money_hands_over_a_refresh_that_ended_without_its_count(tmp_path):
    fakes(tmp_path)
    ga_tree(tmp_path)
    # the read crashed before its last line: what it stored may be new, so it goes over all the same
    tool(tmp_path / "bin" / "gacrawl", 'echo "gacrawl $*" >> "$FAKE_LOG"\ncase "$*" in *"--mode refresh"*) echo Traceback; exit 1;; '
                                       '*"--mode full"*) echo \'{"pages": 0}\';; *) echo "{}";; esac\n')
    out, calls, d = ga_run(tmp_path)
    assert calls[-2:] == ["handoff", "push Georgia money, newest filings"] and len(d) == 1
    assert "::warning::ended with exit 1" in out


def test_the_crawl_applies_what_ga_money_hands_over():
    # the crawl's merge takes the two assets ga_handoff.sh uploads (ga-delta.db.zst, ga-kv.json) and applies both
    handoff = (ROOT / "scripts" / "ga_handoff.sh").read_text()
    assert "gh release upload \"$DATA_TAG\" ga-delta.db.zst ga-kv.json --clobber" in handoff
    apply = step("crawl.yml", lambda s: (s.get("name") or "").startswith("Apply the parallel Georgia money read"))["run"]
    assert 'select(.name=="ga-delta.db.zst" or .name=="ga-kv.json")' in apply
    assert "gacrawl finance apply --delta gad/ga-delta.db && gacrawl ethics apply-kv --kv gad/ga-kv.json" in apply


# --- news-research: a failed research run and a refused push are said ------------------------------------------------


def news_tree(tmp):
    tool(tmp / "bin" / "git", '''echo "git $*" >> "$FAKE_LOG"
case "$1" in
  clone) d="${@: -1}"; mkdir -p "$d/tools/ai" "$d/ga"; printf 'import sys\\nsys.exit(%s)\\n' "${RESEARCH_RC:-0}" > "$d/tools/ai/research_ai.py"
         echo "{}" > "$d/ga/upcoming.json" ;;
  diff) exit 1 ;;
  pull) exit "${PULL_RC:-0}" ;;
  push) exit "${PUSH_RC:-0}" ;;
esac
exit 0
''')
    (tmp / "dist" / "news").mkdir(parents=True, exist_ok=True)
    (tmp / "dist" / "news" / "news.json").write_text('{"items": [{"t": "a story"}]}')


def news_run(tmp, **kw):
    run = step("news-research.yml", lambda s: s.get("name") == "Electo's research files and the public site")["run"]
    run = shell(run).replace("/tmp/electo-news", str(tmp / "electo-news"))
    shutil.rmtree(tmp / "electo-news", ignore_errors=True)
    r, log, _ = bash(run, tmp, env(tmp, **kw))
    return r, log


def test_news_research_says_when_research_ai_failed_and_still_commits(tmp_path):
    fakes(tmp_path)
    news_tree(tmp_path)
    r, log = news_run(tmp_path, RESEARCH_RC=3)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::warning title=News research::tools/ai/research_ai.py ended with exit 3 (an error; see the lines above)" in r.stdout
    assert "no research updates this run" not in r.stdout
    assert log.count("git push") == 1
    r, log = news_run(tmp_path, RESEARCH_RC=124)
    assert r.returncode == 0 and "ended with exit 124 (its 20 minutes ran out)" in r.stdout
    r, log = news_run(tmp_path)
    assert r.returncode == 0 and "::warning title=News research" not in r.stdout


def test_news_research_fails_when_electo_refuses_every_push(tmp_path):
    fakes(tmp_path)
    news_tree(tmp_path)
    r, log = news_run(tmp_path, PUSH_RC=1)
    assert r.returncode == 1 and log.count("git push") == 5
    assert "::error title=News research::Electo refused the research files five times" in r.stdout
    # a pull stopped on a conflict is undone before the next try
    r, log = news_run(tmp_path, PULL_RC=1)
    assert r.returncode == 1 and log.count("git rebase --abort") == 5 and "git push" not in log


# --- results: every county's local races, until the count is final --------------------------------------------------

READER = '''import json, os, signal, sys, time
if os.environ.get("HANG"):              # a state site that does not answer; "deaf" also ignores the polite stop
    if os.environ["HANG"] == "deaf":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("reading 159 counties", flush=True); time.sleep(60)
if os.environ.get("LOCAL_RC", "0") != "0":
    print("local races: the state's site answered for none of the 159 counties; nothing written"); sys.exit(int(os.environ["LOCAL_RC"]))
if "--eid" in sys.argv and sys.argv[sys.argv.index("--eid") + 1] != "11032026GeneralAndSpecialElection":
    print("local races: another election, nothing read"); sys.exit(0)
a = os.environ["ASOF"]
json.dump({"counties": {"appling": {"asOf": a, "off": 1}}}, open("ga/local_2026.json", "w"))
json.dump({"checked": "2026-11-03", "items": [{"stage": "enacted"}]}, open("ga/bills_local.json", "w"))
if os.environ.get("NOT_READ"):
    print("counties not read: 1 (jones), their last postings kept")
print(f"local postings: 1 of 1 counties changed since the last refresh (appling), 1 certified, newest {a}")
'''


def results_tree(tmp):
    return electo(tmp, {"ga/local_update.py": READER, "ga/sos_hist.py": "", "ga/build.py": "", "ga/local_2025m.json": "{}",
                        "ga/local_2026.json": '{"counties": {}}', "ga/bills_local.json": '{"items": []}'})


def local_run(tmp, g, limit=None, **kw):
    run = step("results.yml", lambda s: s.get("id") == "local")["run"]
    run = shell(run).replace("/tmp/electo-local", str(tmp / "electo-local"))
    if limit:                     # the reader's time limit, shortened so a test need not wait 8 minutes
        run, k = re.subn(r"timeout -k 30 8m python3", f"timeout -k 1 {limit} python3", run)
        assert k == 1
    (tmp / ".local-read").unlink(missing_ok=True)
    return bash(run, tmp, env(tmp, **{"EID": "11032026GeneralAndSpecialElection", **g, **kw}))


def test_results_commits_the_local_races_when_a_county_posting_changed(tmp_path):
    fakes(tmp_path)
    bare, g = results_tree(tmp_path)
    r, _, out = local_run(tmp_path, g, ASOF="2026-11-09T18:00:00Z")
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "pushed=true\n" and (tmp_path / ".local-read").exists()
    assert commits(bare)[0] == ("Local results from the Secretary of State: 1 of 1 counties changed since the last "
                                "refresh (appling), 1 certified, newest 2026-11-09T18:00:00Z")
    files = subprocess.run(["git", "-C", str(bare), "show", "--name-only", "--format=", "main"], capture_output=True,
                           text=True).stdout.split()
    assert sorted(files) == ["ga/bills_local.json", "ga/local_2026.json"]
    # the same postings again: read (the slot is marked), nothing committed or published
    r, _, out = local_run(tmp_path, g, ASOF="2026-11-09T18:00:00Z")
    assert r.returncode == 0 and out == "" and "no county posted anything new" in r.stdout
    assert len(commits(bare)) == 2 and (tmp_path / ".local-read").exists()


def test_results_says_which_counties_the_state_did_not_answer_and_fails_when_none_answered(tmp_path):
    fakes(tmp_path)
    bare, g = results_tree(tmp_path)
    r, _, _ = local_run(tmp_path, g, ASOF="2026-11-10T01:00:00Z", NOT_READ=1)
    assert r.returncode == 0 and "::warning title=Local results::some counties were not read and keep their last postings" in r.stdout
    r, _, out = local_run(tmp_path, g, ASOF="x", LOCAL_RC=2)
    assert r.returncode == 1 and "::error title=Local results::ga/local_update.py ended with exit 2" in r.stdout
    assert out == "" and not (tmp_path / ".local-read").exists()          # not marked: the next run reads again
    r, _, out = local_run(tmp_path, g, ASOF="x", EID="12012026GeneralRunoff")
    assert r.returncode == 0 and out == "" and "another election, nothing read" in r.stdout


def test_results_reads_the_local_races_until_the_count_is_final():
    S = {s.get("id") or s.get("name") or s.get("uses"): s for s in steps("results.yml")}
    due = "(steps.check.outputs.go == 'true' || env.FORCE == 'true' || steps.localseen.outputs.cache-hit != 'true')"
    assert S["local"]["if"] == "${{ !cancelled() && steps.check.outputs.local == 'true' && " + due + " }}"
    assert S["localseen"]["with"] == {"path": ".local-read", "key": "local-results-${{ steps.check.outputs.slot }}",
                                      "lookup-only": True}
    assert S["localseen"].get("continue-on-error") is True                   # a cache that does not answer stops nothing
    save = next(s for s in steps("results.yml") if s.get("uses") == "actions/cache/save@v4")
    assert save["with"]["key"] == S["localseen"]["with"]["key"] and "steps.local.outcome == 'success'" in save["if"]
    pub = S["Publish the site again with the counties' new postings"]
    assert pub["if"] == "${{ !cancelled() && steps.local.outputs.pushed == 'true' }}"


def test_results_publishes_a_new_count_before_it_reads_the_counties():
    # a slow county read could run the job into its time limit, and a job cut off there skips every later step: the
    # new statewide count is sent to the site before that read, on nothing but its own push
    names = [s.get("id") or s.get("name") for s in steps("results.yml")]
    first = step("results.yml", lambda s: s.get("name") == "Publish the site now with the new count")
    assert names.index("build") < names.index(first["name"]) < names.index("local")
    assert first["if"] == "steps.build.outputs.pushed == 'true'"
    assert first["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert re.search(r'gh workflow run publish-site\.yml -R "\$\{\{ github\.repository \}\}"', first["run"])
    # every start of publish-site waits on a push of this run
    for s in steps("results.yml"):
        if "gh workflow run publish-site.yml" in (s.get("run") or ""):
            assert "pushed == 'true'" in s["if"], s["name"]


def test_results_gives_the_county_read_a_time_limit_inside_the_jobs():
    W = workflow("results.yml")
    job = W["jobs"]["results"]
    local = step("results.yml", lambda s: s.get("id") == "local")
    # the reader stops after 8 minutes (30 seconds more for one that ignores the stop), and the step's own limit, for
    # a clone or push that hangs, ends before the job's: a step past its limit fails, a job past its limit is cancelled
    assert re.search(r"^\s*timeout -k 30 8m python3 -I ga/local_update\.py ", local["run"], flags=re.M)
    assert 9 <= local["timeout-minutes"] < job["timeout-minutes"]


@pytest.mark.parametrize("hang", ["yes", "deaf"])
def test_results_stops_a_county_read_that_hangs_and_commits_nothing(tmp_path, hang):
    fakes(tmp_path)
    bare, g = results_tree(tmp_path)
    r, _, out = local_run(tmp_path, g, limit="2", HANG=hang, ASOF="2026-11-12T18:00:00Z")
    assert r.returncode == 1, r.stdout + r.stderr
    assert ("::error title=Local results::the counties' postings were not all read within 8 minutes (the state's site "
            "is slow)") in r.stdout
    # nothing pushed (no second publish), and the slot not marked: the next run asks again
    assert out == "" and len(commits(bare)) == 1 and not (tmp_path / ".local-read").exists()


def test_results_check_reads_no_local_races_once_the_count_is_final(tmp_path):
    # with the page on the final count of the election to follow, neither the page nor the local races are read (and
    # the state's site is not asked: no network here)
    b = fakes(tmp_path)
    built = {"eid": "E1", "final": True, "version": "2026-11-20T10:00:00"}
    content = {"ga/built.json": json.dumps(built), "ga/build.py": "print('E1')\n"}
    (tmp_path / "content.json").write_text(json.dumps({k: base64.b64encode(v.encode()).decode() for k, v in content.items()}))
    tool(b / "gh", 'python3 - "$@" <<\'PY\'\nimport json, os, sys\nc = json.load(open(os.path.join(os.environ["HOME"], "content.json")))\n'
                   'path = sys.argv[2].split("/contents/", 1)[1]\nprint(c[path])\nPY\n')
    run = shell(step("results.yml", lambda s: s.get("id") == "check")["run"])
    r, _, out = bash(run, tmp_path, env(tmp_path, ELECTO_REPO="O/Electo", FORCE="false"))
    assert r.returncode == 0, r.stdout + r.stderr
    o = dict(line.split("=", 1) for line in out.splitlines())
    assert o["go"] == "false" and o["local"] == "false" and o["eid"] == "E1" and re.fullmatch(r"\d{8}-\d{1,2}", o["slot"])


# --- ai-law-questions, ai-replay, ai-compare-eval and ordinances: a push to Electo starts publish-site ---------------
# Until Nov 19, 2026 their pushes waited for publish-site's schedule, which fires about once an hour in the runner
# repository. candidate-news pushes nothing to Electo (its findings go to the crawl's merge, which publishes).

LATE = ["ai-law-questions.yml", "ai-replay.yml", "ai-compare-eval.yml", "ordinances.yml"]


def pushes_to_electo(name):
    """Whether a workflow commits to Electo itself: the money push, or a git push in a workflow that holds Electo's
    token or names its repository (the pushes to the private repository go through scripts/ci_runner.sh)."""
    runs = [s.get("run") or "" for s in steps(name)]
    if any("money_to_electo.sh" in r for r in runs):
        return True
    return bool(re.search(r"ELECTO_(?:TOKEN|REPO)", (WORKFLOWS / name).read_text())
                and any(re.search(r"\bgit (?:-C \S+ )?push\b", r) for r in runs))


def publishes(name):
    runs = [s.get("run") or "" for s in steps(name)]
    return any(re.search(r"gh workflow run publish-site\.yml -R ", r) or "tools/site_data.sh" in r for r in runs)


def test_every_workflow_that_commits_to_electo_publishes_the_site():
    names = sorted(p.name for p in WORKFLOWS.glob("*.yml"))
    pushers = [n for n in names if pushes_to_electo(n)]
    for n in LATE + ["grand-cross.yml", "police.yml", "history.yml", "results.yml", "news-research.yml", "crawl.yml"]:
        assert n in pushers, n
    # the watchdog commits only the open models' pass rates (tools/ai/models.json, research/train/health.json)
    missing = [n for n in pushers if n != "watchdog.yml" and not publishes(n)]
    assert not missing, f"push to Electo without starting publish-site or running tools/site_data.sh: {missing}"
    assert not pushes_to_electo("candidate-news.yml")


def test_the_new_publish_steps_follow_a_real_push_with_this_repositorys_token():
    for name, out in (("ai-law-questions.yml", "steps.combine.outputs.pushed == 'true'"),
                      ("ai-replay.yml", "steps.combine.outputs.pushed == 'true'"),
                      ("ai-compare-eval.yml", "steps.results.outputs.pushed == 'true'"),
                      ("ordinances.yml", "steps.send.outputs.sent == '1'")):
        assert workflow(name)["permissions"].get("actions") == "write", name
        pub = step(name, lambda s: "gh workflow run publish-site.yml" in (s.get("run") or ""))
        assert pub["if"] == "${{ !cancelled() && " + out + " }}", name
        assert pub["env"] == {"GH_TOKEN": "${{ github.token }}"}, name
        assert re.search(r'-R "\$\{\{ github\.repository \}\}" --ref "\$\{\{ github\.ref_name \}\}" \|\| echo', pub["run"]), name


def stub_electo(tmp, files, refuse=False):
    bare, g = electo(tmp, files)
    if refuse:
        hook = bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
        hook.chmod(0o755)
    return bare, g


LAW = ("import os, pathlib, sys\nif '--combine' in sys.argv:\n    p = pathlib.Path('data/ord_lay.runner.json.gz')\n"
       "    p.parent.mkdir(parents=True, exist_ok=True)\n    p.write_text(os.environ.get('OUT', '{}'))\n")


def combine_run(tmp, name, find, clone, g, **kw):
    shutil.rmtree(tmp / "clone", ignore_errors=True)
    run = shell(step(name, find)["run"]).replace(clone, str(tmp / "clone")).replace("/tmp/need.txt", str(tmp / "need.txt"))
    (tmp / "parts" / "0").mkdir(parents=True, exist_ok=True)
    (tmp / "parts" / "0" / "part.json").write_text("{}")
    return bash(run, tmp, env(tmp, SPARSE=workflow(name)["env"].get("SPARSE", ""), **g, **kw))


def test_ai_law_questions_marks_a_push_and_not_a_combine_that_changed_nothing(tmp_path):
    fakes(tmp_path)
    bare, g = stub_electo(tmp_path, {"tools/ai/law_questions.py": LAW, "data/ord_lay.runner.json.gz": "{}"})
    find = lambda s: s.get("id") == "combine"
    r, _, out = combine_run(tmp_path, "ai-law-questions.yml", find, "/tmp/electo-law", g, OUT='{"q": 1}')
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "pushed=true\n" and commits(bare)[0].startswith("Everyday questions for new ordinance sections")
    r, _, out = combine_run(tmp_path, "ai-law-questions.yml", find, "/tmp/electo-law", g, OUT='{"q": 1}')
    assert r.returncode == 0 and out == "" and "nothing new" in r.stdout


def test_ai_law_questions_marks_nothing_when_electo_refuses_the_push(tmp_path):
    fakes(tmp_path)
    bare, g = stub_electo(tmp_path, {"tools/ai/law_questions.py": LAW, "data/ord_lay.runner.json.gz": "{}"}, refuse=True)
    r, _, out = combine_run(tmp_path, "ai-law-questions.yml", lambda s: s.get("id") == "combine", "/tmp/electo-law", g,
                            OUT='{"q": 2}')
    assert r.returncode == 1 and "::error::the questions could not be pushed to Electo" in r.stdout
    assert out == "" and len(commits(bare)) == 1


def test_ai_replay_marks_a_push_and_not_a_combine_that_changed_nothing(tmp_path):
    fakes(tmp_path)
    replay = ("import os, pathlib, sys\nif '--combine' in sys.argv:\n    p = pathlib.Path('research/train/replay.json')\n"
              "    p.parent.mkdir(parents=True, exist_ok=True)\n    p.write_text(os.environ.get('OUT', '{}'))\n")
    bare, g = stub_electo(tmp_path, {"tools/ai/replay.py": replay, "tools/ai/models.json": "{}",
                                     "research/train/replay.json": "{}"})
    find = lambda s: s.get("id") == "combine"
    r, _, out = combine_run(tmp_path, "ai-replay.yml", find, "/tmp/electo-ai", g, OUT='{"r": 1}', DECIDE="false")
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "pushed=true\n" and commits(bare)[0].startswith("Open-model jobs replayed through other models")
    r, _, out = combine_run(tmp_path, "ai-replay.yml", find, "/tmp/electo-ai", g, OUT='{"r": 1}', DECIDE="false")
    assert r.returncode == 0 and out == "" and "nothing new" in r.stdout


def test_ai_compare_eval_marks_a_push_of_its_results(tmp_path):
    b = fakes(tmp_path)
    tool(b / "node", 'echo \'{"run": "x"}\'\n')
    bare, g = stub_electo(tmp_path, {"qa/compare_eval.js": "", "research/eval/compare/log.jsonl": ""})
    ce = tmp_path / "ce"
    ce.mkdir()
    (ce / "now.json").write_text('{"score": 1}')
    run = shell(step("ai-compare-eval.yml", lambda s: s.get("id") == "results")["run"])
    run = run.replace("/tmp/electo-ce", str(tmp_path / "electo-ce")).replace("/tmp/ce/", f"{ce}/")
    subprocess.run(["git", "clone", "-q", str(bare), str(tmp_path / "electo-ce")], check=True)
    r, _, out = bash(run, tmp_path, env(tmp_path, **g))
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "pushed=true\n" and commits(bare)[0].startswith("Ask AI comparisons eval on the offline model")
    # the same results again: nothing to commit, nothing to publish
    r, _, out = bash(run.replace('node qa/compare_eval.js --log', 'true'), tmp_path, env(tmp_path, **g))
    assert r.returncode == 0 and out == "" and "nothing new" in r.stdout


def test_results_says_why_a_county_read_failed_under_pipefail_too(tmp_path):
    """Nov 19 review: under bash -eo pipefail (a step with shell: bash) the failed read ended the step at the pipeline,
    before its ::error said why; the read now runs between set +e and set -e"""
    fakes(tmp_path)
    bare, g = results_tree(tmp_path)
    run = step("results.yml", lambda s: s.get("id") == "local")["run"]
    run = "set -o pipefail\n" + shell(run).replace("/tmp/electo-local", str(tmp_path / "electo-local"))
    (tmp_path / ".local-read").unlink(missing_ok=True)
    r, _, out = bash(run, tmp_path, env(tmp_path, **{"EID": "11032026GeneralAndSpecialElection", **g, "ASOF": "x", "LOCAL_RC": "2"}))
    assert r.returncode == 1 and "::error title=Local results::ga/local_update.py ended with exit 2" in r.stdout, r.stdout
    assert out == "" and not (tmp_path / ".local-read").exists()
