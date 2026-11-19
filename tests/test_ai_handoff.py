"""The AI jobs' shard results passed to their combine job through the crawl's own data release (scripts/ai_handoff.sh),
never as public run attachments: put, get, drop and the cleanup of a failed run's leftovers, against tests/fake_gh.py
(with gh's --jq, --paginate and -R applied by a small wrapper)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="needs jq")

WRAP = r'''#!/bin/sh
# drop -R REPO and --paginate, apply --jq EXPR to the fake's answer
args=""; jq=""
while [ $# -gt 0 ]; do
  case "$1" in
    -R) shift 2 ;;
    --paginate) shift ;;
    --jq) jq="$2"; shift 2 ;;
    *) args="$args $(printf '%s' "$1" | sed "s/'/'\\\\''/g; s/^/'/; s/$/'/")"; shift ;;
  esac
done
if [ -n "$jq" ]; then eval "exec {py} {fake} $args" | jq -r "$jq"; else eval "exec {py} {fake} $args"; fi
'''


@pytest.fixture
def env(tmp_path):
    gh_dir = tmp_path / "gh"
    (gh_dir / "releases" / "crawl-data").mkdir(parents=True)
    (gh_dir / "files").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(WRAP.replace("{py}", sys.executable).replace("{fake}", str(ROOT / "tests" / "fake_gh.py")))
    gh.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(gh_dir), "DATA_REPO": "o/private",
         "DATA_TOKEN": "x", "GITHUB_RUN_ID": "4242"}
    e.pop("DATA_TAG", None)

    def run(*args, **more):
        return subprocess.run(["bash", str(ROOT / "scripts" / "ai_handoff.sh"), *args], cwd=work, env={**e, **more},
                              capture_output=True, text=True)

    def assets():
        p = gh_dir / "assets.json"
        return json.loads(p.read_text()) if p.exists() else {}

    return type("E", (), {"run": staticmethod(run), "work": work, "assets": staticmethod(assets), "gh": gh_dir})


def test_shard_results_go_through_the_crawls_data_release_and_come_back(env):
    (env.work / "shard.json").write_text('{"who": "model output"}')
    (env.work / "log.jsonl").write_text("{}\n")
    r = env.run("put", "said-by", "3", "shard.json", "log.jsonl")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(env.assets()) == ["ai-said-by-4242-3--log.jsonl", "ai-said-by-4242-3--shard.json"]
    assert not (env.gh / "releases" / "ai-handoff").exists()                  # no release of its own
    r = env.run("get", "said-by", "got")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads((env.work / "got" / "3" / "shard.json").read_text()) == {"who": "model output"}
    r = env.run("drop", "got")
    assert r.returncode == 0 and env.assets() == {}, r.stdout + r.stderr


def test_another_runs_results_and_the_crawls_own_hand_offs_are_left(env):
    (env.work / "shard.json").write_text("{}")
    assert env.run("put", "write", "0", "shard.json", GITHUB_RUN_ID="1111").returncode == 0
    (env.work / "model-handoff.json").write_text("{}")
    subprocess.run(["gh", "release", "upload", "crawl-data", str(env.work / "model-handoff.json")], env={**os.environ,
                   "PATH": f"{env.work.parent / 'bin'}:{os.environ['PATH']}", "FAKE_GH_DIR": str(env.gh)}, check=True)
    r = env.run("get", "write", "got")                                         # run 4242 sees none of run 1111's
    assert r.returncode == 0 and not (env.work / "got" / "0").exists(), r.stdout + r.stderr
    # the cleanup takes only this script's names, and only old ones (the fake dates every asset 2026-11-19)
    r = env.run("clean", "0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(env.assets()) == ["model-handoff.json"]


def test_no_data_release_is_an_error_not_a_new_release(env):
    shutil.rmtree(env.gh / "releases" / "crawl-data")
    (env.work / "shard.json").write_text("{}")
    r = env.run("put", "write", "0", "shard.json")
    assert r.returncode != 0 and "no crawl-data release" in r.stdout + r.stderr
    assert not any((env.gh / "releases").iterdir())                          # none was created


def test_an_empty_shard_file_goes_as_a_marker_and_comes_back_empty(env):
    # Nov 19, 2026: every ai-predict check shard failed uploading its empty log.jsonl (GitHub refuses empty files)
    (env.work / "part.json").write_text('{"n": 0}')
    (env.work / "log.jsonl").write_text("")
    r = env.run("put", "predict", "predict-check-1", "part.json", "log.jsonl")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(env.assets()) == ["ai-predict-4242-predict-check-1--log.jsonl.empty", "ai-predict-4242-predict-check-1--part.json"]
    r = env.run("get", "predict", "got")
    assert r.returncode == 0, r.stdout + r.stderr
    got = env.work / "got" / "predict-check-1"
    assert (got / "log.jsonl").exists() and (got / "log.jsonl").read_text() == "" and not (got / "log.jsonl.empty").exists()
    assert json.loads((got / "part.json").read_text()) == {"n": 0}
    assert env.run("drop", "got").returncode == 0 and env.assets() == {}
