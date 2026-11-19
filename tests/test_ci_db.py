"""scripts/ci_db.sh / ci_db.py against a stand-in for `gh` (tests/fake_gh.py): the database in the crawl-data release.

What these hold: a database is stored in parts under GitHub's per-file limit behind a list written last; a download
that fails is never taken for "no database yet" (the old script exited 0 and the merge job would have stored an empty
database over the real one); a database that lost more than half is not stored; a failed upload leaves the stored copy
readable; copies under other names (a temp save) work the same way."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")


def make_db(path: Path, kb: int, mark: str = "a"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE VIRTUAL TABLE records_fts USING fts5(t)")
    con.execute("CREATE TABLE blobs(b BLOB)")
    con.execute("CREATE TABLE mark(m TEXT)")
    con.execute("INSERT INTO mark VALUES (?)", (mark,))
    for _ in range(kb):
        con.execute("INSERT INTO blobs VALUES (?)", (os.urandom(1024),))     # does not compress: the size is the size
    con.commit()
    con.close()


def mark_of(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT m FROM mark").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    gh_dir = tmp_path / "gh"
    (gh_dir / "releases").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests' / 'fake_gh.py'} \"$@\"\n")
    gh.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(gh_dir), "GH_REPO": "o/r", "DATA_TAG": "crawl-data",
         "CI_DB_PART_BYTES": str(64 * 1024), "CI_DB_SINGLE_MAX": str(10 * 1024 * 1024), "CI_DB_WAIT": "0", "CI_DB_TRIES": "3",
         "CI_DB_GUARD_MIN": "1024"}
    for k in ("LIVE_TAG", "ALLOW_EMPTY_DB", "ALLOW_SMALLER_DB", "OPTIMIZE_FTS", "GITHUB_REPOSITORY"):
        e.pop(k, None)

    def run(*args, cwd=work, **more):
        return subprocess.run(["bash", str(ROOT / "scripts" / "ci_db.sh"), *args], cwd=cwd, env={**e, **more}, capture_output=True, text=True)

    def assets():
        p = gh_dir / "assets.json"
        return json.loads(p.read_text()) if p.exists() else {}

    return type("E", (), {"run": staticmethod(run), "gh": gh_dir, "work": work, "assets": staticmethod(assets), "e": e})


def test_round_trip_in_parts_behind_a_list(env):
    make_db(env.work / "data" / "crawl.db", 300, "first")
    r = env.run("push")
    assert r.returncode == 0, r.stdout + r.stderr
    names = sorted(env.assets())
    parts = [n for n in names if ".part-" in n]
    assert len(parts) >= 4 and "crawl.db.zst.parts.json" in names and "crawl.db.zst" in names      # parts, their list, and the old single file
    assert all(env.assets()[p]["size"] <= 64 * 1024 for p in parts)
    shutil.rmtree(env.work / "data")
    r = env.run("pull")
    assert r.returncode == 0 and "database:" in r.stdout, r.stdout + r.stderr
    assert mark_of(env.work / "data" / "crawl.db") == "first" and not (env.work / "data" / "crawl.db.zst").exists()
    # a second upload replaces the first: its parts are the only ones left
    make_db(env.work / "data" / "crawl.db", 320, "second")
    assert env.run("push").returncode == 0
    now = [n for n in env.assets() if ".part-" in n]
    assert not set(now) & set(parts) and len(now) >= 4
    shutil.rmtree(env.work / "data")
    assert env.run("pull").returncode == 0 and mark_of(env.work / "data" / "crawl.db") == "second"


def test_a_download_that_fails_is_not_a_first_run(env):
    make_db(env.work / "data" / "crawl.db", 200)
    assert env.run("push").returncode == 0
    shutil.rmtree(env.work / "data")
    (env.gh / "fail_download").write_text("999")
    r = env.run("pull")
    assert r.returncode == 1 and "::error title=Crawl database::" in r.stdout and "first run" not in r.stdout
    assert not (env.work / "data" / "crawl.db").exists()
    # one bad transfer is tried again
    (env.gh / "fail_download").write_text("1")
    r = env.run("pull")
    assert r.returncode == 0 and (env.work / "data" / "crawl.db").exists(), r.stdout + r.stderr


def test_a_release_that_cannot_be_listed_stops_the_run(env):
    (env.gh / "releases" / "crawl-data").mkdir()
    (env.gh / "fail_list").write_text("999")
    r = env.run("pull")
    assert r.returncode != 0 and "first run" not in r.stdout


def test_only_a_missing_release_or_a_say_so_starts_empty(env):
    r = env.run("pull")                                  # no release at all: the very first run
    assert r.returncode == 0 and "no database yet (first run)" in r.stdout
    (env.gh / "releases" / "crawl-data").mkdir()
    r = env.run("pull")                                  # a release with no database in it: stop
    assert r.returncode == 1 and "holds no database" in r.stdout
    assert env.run("pull", ALLOW_EMPTY_DB="1").returncode == 0


def test_a_single_file_from_the_old_script_is_read_and_the_newer_form_wins(env):
    (env.gh / "releases" / "crawl-data").mkdir()
    make_db(env.work / "old.db", 100, "old script")
    subprocess.run(["zstd", "-q", "-f", str(env.work / "old.db"), "-o", str(env.work / "crawl.db.zst")], check=True)
    subprocess.run(["gh", "release", "upload", "crawl-data", str(env.work / "crawl.db.zst"), "--clobber"], env=env.e, check=True)
    assert env.run("pull").returncode == 0 and mark_of(env.work / "data" / "crawl.db") == "old script"
    # the new script stores parts and a list; a run still on the old script then stores a single file again: it is newer
    make_db(env.work / "data" / "crawl.db", 100, "new script")
    assert env.run("push").returncode == 0
    make_db(env.work / "old.db", 100, "old script again")
    subprocess.run(["zstd", "-q", "-f", str(env.work / "old.db"), "-o", str(env.work / "crawl.db.zst")], check=True)
    subprocess.run(["gh", "release", "upload", "crawl-data", str(env.work / "crawl.db.zst"), "--clobber"], env=env.e, check=True)
    shutil.rmtree(env.work / "data")
    assert env.run("pull").returncode == 0 and mark_of(env.work / "data" / "crawl.db") == "old script again"


def test_a_database_past_the_single_file_limit_is_still_stored(env):
    """GitHub refuses a release file of 2 GiB; here the limit is 150 KB and the database about 300."""
    (env.gh / "max_bytes").write_text(str(150 * 1024))
    make_db(env.work / "data" / "crawl.db", 60, "small")
    assert env.run("push", CI_DB_SINGLE_MAX=str(140 * 1024)).returncode == 0 and "crawl.db.zst" in env.assets()
    make_db(env.work / "data" / "crawl.db", 300, "grown")
    r = env.run("push", CI_DB_SINGLE_MAX=str(140 * 1024))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "crawl.db.zst" not in env.assets()            # the single file would no longer match: gone, the parts stand
    shutil.rmtree(env.work / "data")
    assert env.run("pull").returncode == 0 and mark_of(env.work / "data" / "crawl.db") == "grown"


def test_a_database_that_lost_more_than_half_is_not_stored(env):
    make_db(env.work / "data" / "crawl.db", 300, "whole")
    assert env.run("push").returncode == 0
    make_db(env.work / "data" / "crawl.db", 20, "nearly empty")
    r = env.run("push")
    assert r.returncode == 1 and "lost more than half" in r.stdout
    shutil.rmtree(env.work / "data")
    assert env.run("pull").returncode == 0 and mark_of(env.work / "data" / "crawl.db") == "whole"
    make_db(env.work / "data" / "crawl.db", 20, "purged on purpose")
    assert env.run("push", ALLOW_SMALLER_DB="1").returncode == 0


def test_an_upload_that_fails_leaves_the_stored_copy_readable(env):
    make_db(env.work / "data" / "crawl.db", 200, "stored")
    assert env.run("push").returncode == 0
    make_db(env.work / "data" / "crawl.db", 220, "not stored")
    (env.gh / "fail_upload_match").write_text(".part-02")
    (env.gh / "fail_upload").write_text("999")
    r = env.run("push")
    assert r.returncode != 0
    (env.gh / "fail_upload").write_text("0")
    shutil.rmtree(env.work / "data")
    assert env.run("pull").returncode == 0 and mark_of(env.work / "data" / "crawl.db") == "stored"


def test_copies_under_other_names(env):
    make_db(env.work / "data" / "crawl.db", 200, "run that was stopped")
    assert env.run("push").returncode == 0
    r = env.run("store", "crawl.db.zst", "tempsave-crawl.db.zst")
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(n.startswith("tempsave-") or n.startswith("crawl.db.zst") for n in env.assets())
    r = env.run("fetch", "copy.zst", "tempsave-crawl.db.zst")
    assert r.returncode == 0 and (env.work / "copy.zst").read_bytes() == (env.work / "crawl.db.zst").read_bytes()
    assert env.run("fetch", "x.zst", "no-such.db.zst").returncode == 1
    assert env.run("drop", "tempsave-crawl.db.zst").returncode == 0
    assert not [n for n in env.assets() if n.startswith("tempsave-")] and "crawl.db.zst.parts.json" in env.assets()
