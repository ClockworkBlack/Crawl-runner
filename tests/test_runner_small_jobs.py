"""code-finder and the hand-run checks (probe, dry-run, qbe-discover, election-reports-check) run in the public runner
repository, on its free minutes, and take from the private repository only what they read (scripts/private_data.py).

Until Nov 19, 2026 they ran in the private repository on paid minutes: code-finder about 11 minutes a day, the checks
135 minutes in twelve days. Here the workflows are read as GitHub reads them, their shell is parsed by bash, the step
that starts the ordinances workflow is run against a stand-in gh, and private_data.py is run with the real git against
a local repository standing in for the private one (git's url.<base>.insteadOf sends the script's github.com address
there), with the real code finder between its pull and its push.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / "scripts" / "private_data.py"
RUNNER = "ClockworkBlack/Crawl-runner"
GUARD = "github.event.repository.private == false"
# workflow: (its job, what it asks private_data.py for)
MOVED = {"code-finder.yml": ("find", ["finder-pull", "finder-push"]), "probe.yml": ("probe", ["state"]),
         "dry-run.yml": ("run", ["state"]), "qbe-discover.yml": ("discover", ["state"]),
         "election-reports-check.yml": ("check", ["state"])}
MAILBOX = ["records-send.yml", "records-test.yml", "mail-send.yml"]
TOKEN = "data-token-0123"
# what sync-code refuses to copy to the public repository (its private-text check), put together here so that this
# file does not hold the text itself
PRIVATE_TEXT = re.compile("|".join(["Humble" + " Servant", "ORR_PASSWORD" + "=", "@gmail" + r"\.com"]))

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(w):
    return w.get("on", w.get(True))      # YAML reads a bare `on` as true


def shell(run):
    """A step's shell with its ${{ }} expressions taken out, as bash gets it."""
    return re.sub(r"\$\{\{.*?\}\}", "X", run, flags=re.S)


# ---- the workflows ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(MOVED))
def test_the_job_runs_on_the_public_runner_only_and_takes_only_what_it_reads(name):
    text = (WORKFLOWS / name).read_text()
    w = workflow(name)
    job_name, asks = MOVED[name]
    assert list(w["jobs"]) == [job_name]
    job = w["jobs"][job_name]
    # a start in the private repository ends skipped at once: no paid minutes
    assert job["if"] == GUARD
    assert "github.repository ==" not in text
    # nothing is committed to the public repository, and its checkout keeps no credential while pages are read
    assert w["permissions"]["contents"] == "read"
    checkout = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")]
    assert len(checkout) == 1 and checkout[0]["with"] == {"persist-credentials": False}
    # the data token is given only to the steps that copy from (or to) the private repository, never to a step that
    # reads a site or prints what it found, and never the mailbox's secrets (those stay in the private repository)
    assert "DATA_TOKEN" not in (w.get("env") or {}) and "DATA_TOKEN" not in (job.get("env") or {})
    with_token = [s for s in job["steps"] if "DATA_TOKEN" in (s.get("env") or {})]
    assert [s["run"] for s in with_token] == [f"python3 scripts/private_data.py {a}" for a in asks]
    assert all(s["env"] == {"DATA_TOKEN": "${{ secrets.DATA_TOKEN }}"} for s in with_token)
    assert text.count("secrets.DATA_TOKEN") == len(asks) and "secrets.ORR_" not in text and "ci_runner.sh" not in text
    # the copy comes before the first command that reads a site
    runs = [s.get("run") or "" for s in job["steps"]]
    first_read = next(i for i, r in enumerate(runs) if re.search(r"(^|\s)gacrawl ", r))
    assert job["steps"].index(with_token[0]) < first_read
    # the crawler names the repository it runs from to the sites it reads
    assert w["env"]["GACRAWL_USER_AGENT"] == "GACivicCrawler/0.3 (+https://github.com/${{ github.repository }})"
    # sync-code's private-text check would stop the whole copy (and every other job's code with it)
    assert not PRIVATE_TEXT.search(text)


def test_the_hand_run_checks_keep_their_forms_and_what_they_run():
    for name in ("probe.yml", "dry-run.yml", "qbe-discover.yml", "election-reports-check.yml"):
        w = workflow(name)
        assert list(triggers(w)) == ["workflow_dispatch"], name
        assert "actions" not in w["permissions"], name          # they start nothing
    inputs = lambda n: sorted((triggers(workflow(n))["workflow_dispatch"] or {}).get("inputs") or {})   # noqa: E731
    assert inputs("probe.yml") == ["accept", "browser", "dump", "grep", "timeout", "urls"]
    assert inputs("dry-run.yml") == ["only", "what"] and inputs("election-reports-check.yml") == ["cap", "counties"]
    text = lambda n: (WORKFLOWS / n).read_text()             # noqa: E731
    assert 'gacrawl probe $URLS $BROWSER $ACCEPT $TIMEOUT --out probe' in text("probe.yml")
    assert 'gacrawl agencies --dry-run --max-minutes 15 --only "$ONLY"' in text("dry-run.yml")
    assert "gacrawl schools qbe-discover --max-minutes 10 > qbe.json" in text("qbe-discover.yml")
    assert 'gacrawl elections reports --dry-run --counties "$COUNTIES" --cap "$CAP" --max-minutes 22' in text("election-reports-check.yml")


def test_the_mailbox_jobs_stay_in_the_private_repository():
    sync = (WORKFLOWS / "sync-code.yml").read_text()
    for name in MAILBOX:
        text = (WORKFLOWS / name).read_text()
        assert GUARD not in text and "private_data.py" not in text, name
        assert f"--exclude .github/workflows/{name}" in sync and f"pub/.github/workflows/{name}" in sync, name


def test_sync_code_carries_the_moved_workflows_to_the_runner():
    """A job that runs only in the public runner repository has to be copied there: one sync-code still leaves out
    (or removes from the copy) runs nowhere, since a start in the private repository now ends skipped."""
    sync = (WORKFLOWS / "sync-code.yml").read_text()
    left_out = [f"--exclude .github/workflows/{n}" for n in sorted(MOVED) if f"--exclude .github/workflows/{n}" in sync]
    removed = [f"pub/.github/workflows/{n}" for n in sorted(MOVED) if f"pub/.github/workflows/{n}" in sync]
    assert not left_out and not removed, (
        "sync-code.yml must copy these to the runner: take out of its rsync command " + " ".join(left_out)
        + " and out of its rm list " + " ".join(removed))
    # what they run comes with them
    assert "--exclude scripts" not in sync and "pub/scripts" not in sync


@pytest.mark.parametrize("name", sorted(MOVED) + ["ordinances.yml"])
def test_every_step_is_shell_bash_can_read(name, tmp_path):
    for job in workflow(name)["jobs"].values():
        for i, s in enumerate(job["steps"]):
            if "run" not in s:
                continue
            f = tmp_path / f"{i}.sh"
            f.write_text(shell(s["run"]))
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, (name, s.get("name"), r.stderr)


@pytest.mark.parametrize("name", sorted(MOVED) + ["ordinances.yml"])
def test_every_start_of_a_workflow_names_this_repository_and_may_start_it(name):
    w = workflow(name)
    n = 0
    for job in w["jobs"].values():
        for s in job["steps"]:
            for m in re.finditer(r"gh workflow run\b[^;&|\n]*", s.get("run") or ""):
                n += 1
                assert re.search(r'\s-R "(\$GITHUB_REPOSITORY|\$\{\{ github\.repository \}\})"(\s|$)', m.group(0)), (name, m.group(0))
                assert s["env"]["GH_TOKEN"] == "${{ github.token }}", (name, s.get("name"))
    if n:
        assert w["permissions"]["actions"] == "write", name
    assert n == {"code-finder.yml": 1, "ordinances.yml": 2}.get(name, 0)


def tool(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_code_finder_keeps_its_schedule_and_starts_ordinances_here(tmp_path):
    w = workflow("code-finder.yml")
    on = triggers(w)
    assert [c["cron"] for c in on["schedule"]] == ["17 6 * * *"]
    assert list(on["workflow_dispatch"]["inputs"]) == ["only"]
    assert w["concurrency"] == {"group": "code-finder", "cancel-in-progress": False}
    steps = w["jobs"]["find"]["steps"]
    names = [s.get("name", "") for s in steps]
    pull, read, keep, start = (next(i for i, n in enumerate(names) if n.startswith(x))
                               for x in ("What the finder reads", "Read the sites", "Keep what was found", "Start the ordinances workflow"))
    assert pull < read < keep < start
    assert "gacrawl records find --max-minutes 40" in steps[read]["run"] and "env" in steps[read]
    assert set(steps[read]["env"]) == {"ONLY"}, "no token and no mailbox address where the sites are read"
    # what was found is kept even when the finder stopped on an error, but never without the copy it started from
    assert steps[keep]["if"] == "always() && steps.pull.outcome == 'success'"
    assert steps[pull]["id"] == "pull" and steps[keep]["id"] == "keep"
    # the ordinances workflow is started only for new code files, in this repository, with this repository's own token
    assert steps[start]["if"] == "always() && steps.keep.outputs.new_codes != '' && steps.keep.outputs.new_codes != '0'"
    log = tmp_path / "gh.log"
    tool(tmp_path / "bin" / "gh", 'echo "GH_TOKEN=$GH_TOKEN gh $*" >> "$GH_LOG"\nexit "${GH_RC:-0}"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "RUNNER_"))}
    env.update(PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", GH_LOG=str(log), GITHUB_REPOSITORY=RUNNER, GH_TOKEN="run-token")
    r = subprocess.run(["bash", "-e", "-c", shell(steps[start]["run"])], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and log.read_text() == f"GH_TOKEN=run-token gh workflow run ordinances.yml -R {RUNNER} --ref main\n"
    # a start GitHub refused does not fail the run: the files are kept, and the next scheduled ordinances run reads them
    r = subprocess.run(["bash", "-e", "-c", shell(steps[start]["run"])], env={**env, "GH_RC": "1"}, capture_output=True, text=True)
    assert r.returncode == 0 and "::warning title=Ordinances::" in r.stdout


def test_ordinances_can_be_started_from_the_private_repository_and_reads_nothing_of_the_event():
    text = (WORKFLOWS / "ordinances.yml").read_text()
    w = workflow("ordinances.yml")
    on = triggers(w)
    # records-send (private) cannot start a workflow here with its own token; a repository_dispatch sent with the data
    # token can, and only a token that may write this repository can send one
    assert on["repository_dispatch"] == {"types": ["ordinances"]}
    assert "workflow_dispatch" in on and "schedule" in on
    assert w["jobs"]["send"]["if"] == GUARD
    # nothing the sender put in the event reaches a command
    assert "client_payload" not in text and "github.event.action" not in text
    assert set(re.findall(r"github\.event\.[A-Za-z_.]+", text)) == {"github.event.repository.private"}


# ---- scripts/private_data.py, against a local repository standing in for the private one ------------------------

G = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
SOURCES = "file,url,title,date,kind,note\n"


def private_repo(tmp, files):
    """A bare repository at <tmp>/remotes/ClockworkBlack/Crawl.git holding `files`, and its working copy."""
    src = tmp / "src"
    for rel, body in files.items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body if isinstance(body, bytes) else body.encode())
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(G + ["-C", str(src), "add", "-A"], check=True)
    subprocess.run(G + ["-C", str(src), "commit", "-qm", "start"], check=True)
    bare = tmp / "remotes" / "ClockworkBlack" / "Crawl.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    for k in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):      # as github.com serves partial clones
        subprocess.run(["git", "-C", str(bare), "config", k, "true"], check=True)
    subprocess.run(["git", "-C", str(src), "remote", "add", "origin", str(bare)], check=True)
    return src, bare


def meanwhile(src, files):
    """A commit to the private repository while the job runs."""
    for rel, body in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(body)
    subprocess.run(G + ["-C", str(src), "add", "-A"], check=True)
    subprocess.run(G + ["-C", str(src), "commit", "-qm", "meanwhile"], check=True)
    subprocess.run(["git", "-C", str(src), "push", "-q", "origin", "main"], check=True)


def workspace(tmp):
    """The runner's checkout of the public repository: the code and config, no state/, data/ or imports/."""
    work = tmp / "work"
    (work / "config").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "runner.json", work / "config" / "runner.json")
    (work / "config" / "jurisdictions.yaml").write_text(
        "counties:\n- {id: co-had, name: Had County, kind: county}\n- {id: co-seat, name: Seat County, kind: county}\n"
        "municipalities:\n- {id: smallton, name: Smallton, kind: city}\n- {id: libton, name: Libton, kind: city}\n"
        "- {id: nosite, name: Nosite, kind: town}\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(G + ["-C", str(work), "add", "-A"], check=True)
    subprocess.run(G + ["-C", str(work), "commit", "-qm", "code"], check=True)
    (tmp / "rt").mkdir()
    return work


def run(tmp, work, *args, token=TOKEN, repository=RUNNER):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "RUNNER_", "GIT_", "DATA_"))}
    env.update(HOME=str(tmp), RUNNER_TEMP=str(tmp / "rt"), GITHUB_OUTPUT=str(tmp / "output.txt"), DATA_TOKEN=token,
               GITHUB_REPOSITORY=repository,
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.file://{tmp}/remotes/.insteadOf",
               GIT_CONFIG_VALUE_0=f"https://x-access-token:{TOKEN}@github.com/", GIT_TRACE=str(tmp / "git-trace.txt"))
    (tmp / "output.txt").write_text("")
    (tmp / "git-trace.txt").write_text("")
    # the script as the workflow runs it, but for the waits between its tries
    code = ("import runpy, sys, time; time.sleep = lambda s: None; sys.argv = sys.argv[1:]; "
            "runpy.run_path(sys.argv[0], run_name='__main__')")
    r = subprocess.run([sys.executable, "-c", code, str(SCRIPT), *args], cwd=work, env=env, capture_output=True, text=True, timeout=180)
    assert TOKEN not in r.stdout + r.stderr
    # no clone, with the token in its remote, is left behind
    assert not (tmp / "rt" / "private-in").exists() and not (tmp / "rt" / "private-out").exists()
    # the files git went back to the repository for, one at a time (a clone made without them fetches one when asked)
    r.fetched = len(re.findall(r"built-in: git fetch ", (tmp / "git-trace.txt").read_text()))
    return r


def files_of(d):
    return sorted(f.relative_to(d).as_posix() for f in d.rglob("*") if f.is_file() and ".git/" not in f.as_posix())


def show(bare, path):
    return subprocess.run(["git", "-C", str(bare), "show", f"HEAD:{path}"], check=True, capture_output=True).stdout


BIG_CODE = b"<html>" + b"x" * 5000 + b"</html>"
PRIVATE = {
    "state/discovered.json": json.dumps({"smallton": {"website": "https://smallton.ga.us/"}}),
    "state/blocked.json": "{}",
    "data/openrecords/found.json": json.dumps({"co-had": {"v": 2, "name": "Had County"}}, indent=1),
    "data/openrecords/online_codes.md": "# Codes that are online but not downloaded\n",
    # what must never reach the runner for these jobs: the tracker, received mail, the requester's signature
    "data/openrecords/requests.json": json.dumps({"smallton": {"to": "clerk@smallton.example.gov", "status": "sent"}}),
    "data/openrecords/attachments/smallton/reply.eml": "From: clerk@smallton.example.gov\n\nAttached.\n",
    "config/private/settings.yaml": "requester: A Requester\n",
    "imports/screens/a.png": "png",
    # Had County's code, from the downloader (GA/: gigabytes in the real repository), and a hand upload
    "imports/ordinances/GA/had_county/code of ordinances/code.html": BIG_CODE,
    "imports/ordinances/README.md": "# Downloaded ordinances\n",
    "imports/ordinances/libton/sources.csv": SOURCES + "old.pdf,https://libton.gov/old.pdf,Old,,ordinance,by hand\n",
    "imports/ordinances/libton/old.pdf": b"%PDF-1.4 an old ordinance",
}


def test_state_brings_state_alone(tmp_path):
    src, bare = private_repo(tmp_path, PRIVATE)
    work = workspace(tmp_path)
    r = run(tmp_path, work, "state")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "state from the private repository: 2 files"
    assert files_of(work) == ["config/jurisdictions.yaml", "config/runner.json", "state/blocked.json", "state/discovered.json"]
    assert (work / "state" / "discovered.json").read_text() == PRIVATE["state/discovered.json"]
    # without the token, or with no state/ in the answer, the job stops before a site is read
    r = run(tmp_path, work, "state", token="")
    assert r.returncode == 1 and "::error title=Private data::no DATA_TOKEN secret" in r.stdout
    assert run(tmp_path, work, "bogus").returncode == 2


def test_nothing_is_touched_outside_a_run_of_the_runner_repository(tmp_path):
    """In a checkout of the private repository state/ and imports/ordinances/ are the real ones: a command run there
    by mistake (finder-pull puts empty files in place of GA/) must not replace them."""
    private_repo(tmp_path, PRIVATE)
    work = workspace(tmp_path)
    (work / "state").mkdir()
    (work / "state" / "discovered.json").write_text("the real one")
    (work / "imports/ordinances/GA/x").mkdir(parents=True)
    (work / "imports/ordinances/GA/x/code.html").write_text("the real code")
    for where in ("ClockworkBlack/Crawl", ""):
        for cmd in ("state", "finder-pull", "finder-push"):
            r = run(tmp_path, work, cmd, repository=where)
            assert r.returncode == 1 and "for a run in the public runner repository" in r.stdout, (where, cmd)
            assert r.fetched == 0 and (tmp_path / "git-trace.txt").read_text() == ""
    assert (work / "state" / "discovered.json").read_text() == "the real one"
    assert (work / "imports/ordinances/GA/x/code.html").read_text() == "the real code"


def test_state_stops_when_none_came(tmp_path):
    private_repo(tmp_path, {"README.md": "x"})
    r = run(tmp_path, workspace(tmp_path), "state")
    assert r.returncode == 1 and "::error title=Private data::no state/ came from the private repository" in r.stdout


def test_the_finder_reads_its_files_finds_codes_and_hands_them_back(tmp_path):
    """finder-pull, the real code finder on the copy, finder-push: what a run of code-finder.yml does."""
    from test_codefinder import FakeFetcher, J

    from gacrawl import codefinder as cf

    src, bare = private_repo(tmp_path, PRIVATE)
    work = workspace(tmp_path)
    r = run(tmp_path, work, "finder-pull")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == ("from the private repository: state (2 files), imports/ordinances (3 files, 1 more by "
                                "name only), found.json (1 places)")
    # the tracker, received mail, the signature and the screenshots stay in the private repository
    assert files_of(work) == [
        "config/jurisdictions.yaml", "config/runner.json", "data/openrecords/found.json", "data/openrecords/online_codes.md",
        "imports/ordinances/GA/had_county/code of ordinances/code.html", "imports/ordinances/README.md",
        "imports/ordinances/libton/old.pdf", "imports/ordinances/libton/sources.csv", "state/blocked.json", "state/discovered.json"]
    # the downloader's code is here by name only; the finder's own folders with their contents
    assert (work / "imports/ordinances/GA/had_county/code of ordinances/code.html").stat().st_size == 0
    assert (work / "imports/ordinances/libton/old.pdf").read_bytes() == PRIVATE["imports/ordinances/libton/old.pdf"]

    # the finder, as `gacrawl records find` runs it: Had County has a code (known from the names alone) and Libton
    # has files, so their sites are not read; Smallton's and Seat County's posted codes are saved
    f = FakeFetcher()
    out = cf.run(work, J, f, log=lambda *_: None, workers=3)
    assert not any("had.gov" in u or "libton.gov" in u for u in f.got)
    assert out["looked"] == 3 and out["docs_saved"] == 7

    # meanwhile, in the private repository: records-send stored a clerk's file for Libton, and the owner's edit
    meanwhile(src, {"imports/ordinances/libton/sources.csv": PRIVATE["imports/ordinances/libton/sources.csv"]
                    + "clerk.pdf,,From the clerk,,code,attached to a reply\n",
                    "imports/ordinances/libton/clerk.pdf": "%PDF-1.4 from the clerk",
                    "data/openrecords/requests.json": json.dumps({"smallton": {"status": "received"}})})
    # and the finder added a row to Libton's sources.csv too (as it does when a place posts one more file)
    with open(work / "imports/ordinances/libton/sources.csv", "a") as fh:
        fh.write("new.pdf,https://libton.gov/new.pdf,New,,ordinance,found by codefinder\n")

    r = run(tmp_path, work, "finder-push")
    assert r.returncode == 0, r.stdout + r.stderr
    # counts only: no place, no file name, no address in the public log
    assert r.stdout.strip() == "kept in the private repository: 12 file(s) written; 7 new code file(s) in 2 place(s)"
    assert "new_codes=7\n" == (tmp_path / "output.txt").read_text()
    log = subprocess.run(["git", "-C", str(bare), "log", "--format=%an|%s", "-2"], capture_output=True, text=True).stdout.splitlines()
    assert log[0].startswith("gacrawl-bot|Code finder: addresses and posted codes 20") and log[1] == "t|meanwhile"
    z = subprocess.run(["git", "-C", str(bare), "show", "--name-status", "--format=", "-z", "HEAD"],
                       capture_output=True, text=True).stdout.split("\0")
    changed = dict(zip(z[1::2], z[0::2]))
    assert changed == {
        "data/openrecords/found.json": "M", "data/openrecords/online_codes.md": "M", "imports/ordinances/libton/sources.csv": "M",
        **{f"imports/ordinances/co-seat/{n}": "A" for n in [f"Chapter{i}.pdf" for i in range(1, 6)] + ["sources.csv"]},
        **{f"imports/ordinances/smallton/{n}": "A" for n in ("Code of Ordinances 2024.pdf", "Zoning Ordinance.pdf", "sources.csv")}}
    # the downloader's code is as it was (never the empty stand-in), and so is everything the finder did not write
    assert show(bare, "imports/ordinances/GA/had_county/code of ordinances/code.html") == BIG_CODE
    assert json.loads(show(bare, "data/openrecords/requests.json")) == {"smallton": {"status": "received"}}
    assert show(bare, "imports/ordinances/libton/clerk.pdf") == b"%PDF-1.4 from the clerk"
    # Libton's sources.csv has the clerk's row and the finder's
    rows = show(bare, "imports/ordinances/libton/sources.csv").decode().splitlines()
    assert rows[0] == SOURCES.strip() and [x.split(",")[0] for x in rows[1:]] == ["old.pdf", "clerk.pdf", "new.pdf"]
    assert show(bare, "imports/ordinances/smallton/Code of Ordinances 2024.pdf") == b"%PDF-1.4 code of ordinances"
    found = json.loads(show(bare, "data/openrecords/found.json"))
    assert found["co-had"] == {"v": 2, "name": "Had County"} and found["nosite"]["records"]["emails"] == ["cityclerk@nosite-ga.org"]
    assert show(bare, "data/openrecords/found.json") == (work / "data/openrecords/found.json").read_bytes()
    # a second push has nothing to add, and starts nothing
    r = run(tmp_path, work, "finder-push")
    assert r.returncode == 0 and "0 file(s) written" in r.stdout


def test_a_run_that_found_nothing_writes_nothing(tmp_path):
    src, bare = private_repo(tmp_path, PRIVATE)
    work = workspace(tmp_path)
    assert run(tmp_path, work, "finder-pull").returncode == 0
    head = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    r = run(tmp_path, work, "finder-push")
    assert r.returncode == 0 and r.stdout.strip() == "nothing new to keep"
    assert (tmp_path / "output.txt").read_text() == "new_codes=0\n"
    assert subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head


def test_writing_downloads_none_of_the_repositorys_files(tmp_path):
    """The private repository holds gigabytes of codes. The clone to write into holds no file, and a commit made
    with `git commit`, or a push with a thin pack, would fetch them first (each of them, one request a file)."""
    src, bare = private_repo(tmp_path, PRIVATE)
    work = workspace(tmp_path)
    assert run(tmp_path, work, "finder-pull").returncode == 0
    (work / "data/openrecords/found.json").write_text(json.dumps({"co-had": {"v": 2, "name": "Had County"}, "smallton": {"v": 2}}))
    (work / "imports/ordinances/smallton").mkdir()
    (work / "imports/ordinances/smallton/code.pdf").write_bytes(b"%PDF-1.4 a code")
    with open(work / "imports/ordinances/libton/sources.csv", "a") as fh:
        fh.write("new.pdf,https://libton.gov/new.pdf,New,,ordinance,found\n")
    r = run(tmp_path, work, "finder-push")
    assert r.returncode == 0 and "3 file(s) written; 1 new code file(s) in 1 place(s)" in r.stdout, r.stdout + r.stderr
    assert r.fetched == 0
    assert show(bare, "imports/ordinances/smallton/code.pdf") == b"%PDF-1.4 a code"
    assert show(bare, "imports/ordinances/libton/sources.csv").decode().endswith("by hand\nnew.pdf,https://libton.gov/new.pdf,New,,ordinance,found\n")
    assert show(bare, "imports/ordinances/GA/had_county/code of ordinances/code.html") == BIG_CODE


def test_found_json_changed_meanwhile_is_merged_place_by_place_and_a_file_that_came_meanwhile_stays(tmp_path):
    src, bare = private_repo(tmp_path, PRIVATE)
    work = workspace(tmp_path)
    assert run(tmp_path, work, "finder-pull").returncode == 0
    # the run read Smallton and saved a file for it
    mine = {"co-had": {"v": 2, "name": "Had County"}, "smallton": {"v": 2, "name": "Smallton", "saved": [{"file": "smallton/code.pdf"}]}}
    (work / "data/openrecords/found.json").write_text(json.dumps(mine, indent=1, sort_keys=True))
    (work / "imports/ordinances/smallton").mkdir()
    (work / "imports/ordinances/smallton/code.pdf").write_bytes(b"%PDF-1.4 the finder's")
    (work / "imports/ordinances/smallton/sources.csv").write_text(SOURCES + "code.pdf,https://smallton.ga.us/code.pdf,Code,,code,found\n")
    # meanwhile the repository's found.json got another place and lost Had County's note, and a file of the same
    # name arrived for Smallton
    meanwhile(src, {"data/openrecords/found.json": json.dumps({"co-had": {"v": 2, "name": "Had County", "note": "by hand"},
                                                                 "libton": {"v": 2, "name": "Libton"}}),
                    "imports/ordinances/smallton/code.pdf": "%PDF-1.4 the clerk's"})
    r = run(tmp_path, work, "finder-push")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::warning title=Code finder::1 file(s) changed in the private repository while this run read the sites" in r.stdout
    assert r.fetched == 1, "only the repository's newer found.json is read back, to merge into"
    assert (tmp_path / "output.txt").read_text() == "new_codes=0\n"      # its code file was not written: nothing to start
    found = json.loads(show(bare, "data/openrecords/found.json"))
    assert found == {"co-had": {"v": 2, "name": "Had County", "note": "by hand"}, "libton": {"v": 2, "name": "Libton"},
                     "smallton": mine["smallton"]}
    assert show(bare, "imports/ordinances/smallton/code.pdf") == b"%PDF-1.4 the clerk's"


def test_the_finder_never_runs_on_an_empty_copy_and_never_pushes_without_its_pull(tmp_path):
    files = {k: v for k, v in PRIVATE.items() if not k.startswith("imports/ordinances/")}
    src, bare = private_repo(tmp_path, files)
    work = workspace(tmp_path)
    # with no file every place would look as if it had no code
    r = run(tmp_path, work, "finder-pull")
    assert r.returncode == 1 and "no file came from the private repository's imports/ordinances/; no site is read" in r.stdout
    (work / "data/openrecords").mkdir(parents=True, exist_ok=True)
    (work / "data/openrecords/found.json").write_text("{}")
    r = run(tmp_path, work, "finder-push")
    assert r.returncode == 1 and "no record of what this run started from" in r.stdout
    assert (tmp_path / "output.txt").read_text() == ""
    assert json.loads(show(bare, "data/openrecords/found.json")) == {"co-had": {"v": 2, "name": "Had County"}}


def test_a_push_that_cannot_reach_the_repository_fails_the_step(tmp_path):
    src, bare = private_repo(tmp_path, PRIVATE)
    work = workspace(tmp_path)
    assert run(tmp_path, work, "finder-pull").returncode == 0
    (work / "data/openrecords/found.json").write_text(json.dumps({"smallton": {"v": 2}}))
    shutil.rmtree(bare)
    r = run(tmp_path, work, "finder-push")
    assert r.returncode == 1 and len(re.findall(r"^try \d: git clone failed", r.stdout, flags=re.M)) == 5
    assert "::error title=Private data::what the finder found could not be written to the private repository" in r.stdout
    assert (tmp_path / "output.txt").read_text() == ""


def test_the_script_and_this_test_pass_sync_codes_private_text_check():
    for f in (SCRIPT, Path(__file__)):
        assert not PRIVATE_TEXT.search(f.read_text()), f.name
