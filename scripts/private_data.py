#!/usr/bin/env python3
"""The private repository's files for the public runner's small jobs (config/runner.json): narrow copies, and one
hand-back.

The crawl and police take everything a run may need (scripts/ci_runner.sh pull-state: state/, the records tracker,
config/private/). The jobs below read public websites and print what they find to a log that is public until
sync-code deletes the run, so each takes only what it reads, never the tracker (data/openrecords/requests.json),
received mail or config/private/ (the requester's signature), and the clone, which holds the token in its remote, is
removed before a site is read:

  private_data.py state         state/ alone, read only: the registry's websites (state/discovered.json), the hosts
                                that refused the crawler (state/blocked.json, never asked again inside their week) and
                                the web-search cache. For probe, dry-run, qbe-discover and election-reports-check,
                                which keep nothing.
  private_data.py finder-pull   what code-finder reads: state/, data/openrecords/found.json and online_codes.md, and
                                imports/ordinances/: the files outside GA/ (the finder's own <id>/ folders and hand
                                uploads; it adds to their sources.csv and skips a file it has), and for GA/ (the
                                downloader's codes, gigabytes) each file's name as an empty file: which places have a
                                code is read from the names alone (gacrawl.openrecords.downloaded_places).
  private_data.py finder-push   what the finder found, into the private repository: found.json, online_codes.md and
                                the files it added or changed under imports/ordinances/ (never GA/, never a removal).
                                Writes new_codes=<n> to $GITHUB_OUTPUT (the code files added, for starting ordinances).

finder-push writes only what the run changed since finder-pull, on a fresh clone each try (trees only: no file is
downloaded to write one, and the commit is made from the index alone). A file the private repository changed meanwhile is not replaced whole: found.json is merged
place by place (the places this run read are the run's), a sources.csv keeps the repository's rows and gets the run's
new ones, and any other file stays as the repository has it.

Prints counts only. Needs DATA_TOKEN (the token the runner's other jobs use); the repository is config/runner.json's
data_repo.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

INBOX = "imports/ordinances"
BIG = INBOX + "/GA/"                      # the downloader's codes: names only on the runner, never written from it
FOUND = "data/openrecords/found.json"
ONLINE = "data/openrecords/online_codes.md"
BOT = ["-c", "user.name=gacrawl-bot", "-c", "user.email=gacrawl-bot@users.noreply.github.com"]


class Stop(Exception):
    pass


def temp() -> Path:
    return Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())


def runner_json() -> dict:
    try:
        d = json.loads(Path("config/runner.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def data_repo() -> str:
    return runner_json().get("data_repo") or "ClockworkBlack/Crawl"


def on_the_runner() -> None:
    """Only in a run of the runner repository: each command replaces state/ (and finder-pull imports/ordinances/, with
    empty files for GA/) in the folder it runs in, which in a checkout of the private repository are the real ones."""
    here, runner = os.environ.get("GITHUB_REPOSITORY") or "", runner_json().get("runner_repo") or ""
    if not runner or here.lower() != runner.lower():
        raise Stop("for a run in the public runner repository (config/runner.json runner_repo) only; nothing is touched here")


def token() -> str:
    t = os.environ.get("DATA_TOKEN") or ""
    if not t:
        raise Stop("no DATA_TOKEN secret: the private repository cannot be read")
    return t


def git(*args: str, cwd: Path | None = None, text: bool = True) -> str | bytes:
    """git, quiet: on failure its own message, with the token taken out, cut short."""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").replace(os.environ.get("DATA_TOKEN") or "\0", "***")
        raise Stop(f"git {args[0]} failed: {' '.join(err.split())[:300]}")
    return r.stdout.decode("utf-8", "replace") if text else r.stdout


def clone(dest: Path, *how: str) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    git("clone", "-q", "--depth", "1", "--filter=blob:none", *how,
        f"https://x-access-token:{token()}@github.com/{data_repo()}.git", str(dest))


def tree(repo: Path, *paths: str) -> dict[str, str]:
    """{path: blob id} of HEAD under paths (names as they are: -z)."""
    out = {}
    for row in git("ls-tree", "-r", "-z", "HEAD", "--", *paths, cwd=repo, text=False).split(b"\0"):
        meta, _, path = row.partition(b"\t")
        m = meta.split()
        if len(m) == 3 and m[1] == b"blob" and m[0] != b"120000":
            out[path.decode("utf-8", "surrogateescape")] = m[2].decode()
    return out


def blob_id(p: Path) -> str:
    """The id git gives this file's contents."""
    h = hashlib.sha1(b"blob %d\0" % p.stat().st_size)
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def inside(path: str) -> bool:
    return ".." not in path.split("/") and not path.startswith("/")


def take(src: Path, rel: str) -> int:
    """Move src/rel (a file or a folder) to ./rel, replacing what is there; how many files came."""
    s, d = src / rel, Path(rel)
    if d.is_dir():
        shutil.rmtree(d)
    elif d.exists():
        d.unlink()
    if not s.exists():
        return 0
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return 1 if d.is_file() else sum(1 for f in d.rglob("*") if f.is_file())


def state() -> None:
    t = temp() / "private-in"
    try:
        clone(t, "--sparse")
        git("sparse-checkout", "set", "--no-cone", "/state/", cwd=t)
        n = take(t, "state")
    finally:
        shutil.rmtree(t, ignore_errors=True)     # its git folder, with the token in its remote, goes
    if not n:
        raise Stop("no state/ came from the private repository")
    print(f"state from the private repository: {n} files")


def finder_pull() -> None:
    t, base = temp() / "private-in", temp() / "finder-base"
    shutil.rmtree(base, ignore_errors=True)
    try:
        clone(t, "--sparse")
        git("sparse-checkout", "set", "--no-cone", "/state/", "/" + FOUND, "/" + ONLINE, f"/{INBOX}/", f"!/{BIG}", cwd=t)
        was = tree(t, INBOX, FOUND, ONLINE)
        n_state = take(t, "state")
        take(t, FOUND)
        take(t, ONLINE)
        n_files = take(t, INBOX)
    finally:
        shutil.rmtree(t, ignore_errors=True)     # its git folder, with the token in its remote, goes
    if not n_state:
        # without state/discovered.json no place has a website, and the finder would note that for every one of them
        raise Stop("no state/ came from the private repository; no site is read")
    names = [p for p in was if p.startswith(INBOX + "/") and inside(p)]
    if not names:
        # with no file every place would look as if it had no code, and the finder would download for all of them
        raise Stop("no file came from the private repository's imports/ordinances/; no site is read")
    n_names = 0
    for p in names:
        f = Path(p)
        if f.is_file():
            continue
        if not p.startswith(BIG):
            # an empty stand-in for a file that was to be copied would go back as that file, emptied
            raise Stop("imports/ordinances/ came incomplete from the private repository; no site is read")
        f.parent.mkdir(parents=True, exist_ok=True)      # GA/: the name alone
        f.touch()
        n_names += 1
    base.mkdir(parents=True)
    (base / "tree.json").write_text(json.dumps(was))
    if Path(FOUND).is_file():
        shutil.copy2(FOUND, base / "found.json")
    try:
        places = len(json.loads(Path(FOUND).read_text()))
    except (OSError, ValueError):
        places = 0
    print(f"from the private repository: state ({n_state} files), imports/ordinances ({n_files} files, {n_names} more "
          f"by name only), found.json ({places} places)")


def load(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def merged_found(base: dict | None, run: dict, theirs: dict) -> str:
    """The repository's found.json with the places this run read (changed or added since the pull) from the run."""
    out = dict(theirs)
    for k, v in run.items():
        if base is None or base.get(k) != v:
            out[k] = v
    return json.dumps(out, indent=1, ensure_ascii=False, sort_keys=True)     # as gacrawl.codefinder writes it


def merged_rows(run: str, theirs: str) -> str:
    """The repository's sources.csv, then the run's rows it does not have."""
    have = set(theirs.splitlines())
    more = [x for x in run.splitlines() if x not in have]
    return theirs + ("" if theirs.endswith("\n") or not theirs else "\n") + "".join(x + "\n" for x in more)


def changed_by_run(was: dict[str, str]) -> dict[str, str]:
    """{path: blob id} of the files the run added or changed: found.json, online_codes.md, imports/ordinances/ but GA/."""
    mine = [Path(FOUND), Path(ONLINE)]
    if Path(INBOX).is_dir():
        mine += [f for f in Path(INBOX).rglob("*") if f.is_file() and not f.is_symlink()
                 and not (f.as_posix() + "/").startswith(BIG)]
    out = {}
    for f in mine:
        if f.is_file():
            b = blob_id(f)
            if was.get(f.as_posix()) != b:
                out[f.as_posix()] = b
    return out


def finder_push() -> None:
    base, t = temp() / "finder-base", temp() / "private-out"
    try:
        was = json.loads((base / "tree.json").read_text())
    except (OSError, ValueError):
        raise Stop("no record of what this run started from (finder-pull): nothing is written") from None
    mine = changed_by_run(was)
    if not mine:
        print("nothing new to keep")
        output(0)
        return
    for i in range(1, 6):
        try:
            clone(t, "--no-checkout")
            git("read-tree", "HEAD", cwd=t)       # the index as the repository is; no file is downloaded
            now = tree(t, *sorted(mine))
            wrote, left = [], set()
            for p, b in sorted(mine.items()):
                d, theirs = t / p, now.get(p)
                if theirs == b:
                    continue                      # the repository has it already
                d.parent.mkdir(parents=True, exist_ok=True)
                if theirs is None or theirs == was.get(p) or p == ONLINE:
                    # as the run started from (or gone since): the run's file. online_codes.md is rewritten whole
                    # from found.json by every run
                    shutil.copy2(p, d)
                elif p == FOUND:
                    try:
                        there = json.loads(git("cat-file", "blob", theirs, cwd=t))
                    except ValueError:
                        there = None
                    run = load(Path(p))
                    if run is None or not isinstance(there, dict):
                        left.add(p)
                        continue
                    d.write_text(merged_found(load(base / "found.json"), run, there), encoding="utf-8")
                elif Path(p).name == "sources.csv":
                    d.write_text(merged_rows(Path(p).read_text(encoding="utf-8", errors="replace"),
                                             git("cat-file", "blob", theirs, cwd=t)), encoding="utf-8")
                else:
                    left.add(p)                   # a file of this name came to the repository meanwhile: it stays
                    continue
                wrote.append(p)
            for k in range(0, len(wrote), 200):
                git("--literal-pathspecs", "add", "-f", "--", *wrote[k:k + 200], cwd=t)
            staged = [x for x in git("diff", "--cached", "--name-only", "-z", cwd=t).split("\0") if x]
            if staged:
                # the commit is made from the index alone (write-tree, commit-tree): `git commit` and a thin pack
                # would first download the repository's files, gigabytes of them, from a clone that holds none
                made = git(*BOT, "commit-tree", git("write-tree", "--missing-ok", cwd=t).strip(), "-p", "HEAD", "-m",
                           f"Code finder: addresses and posted codes {time.strftime('%Y-%m-%d', time.gmtime())}", cwd=t).strip()
                branch = git("symbolic-ref", "--short", "HEAD", cwd=t).strip()
                git("push", "-q", "--no-thin", "origin", f"{made}:refs/heads/{branch}", cwd=t)
            # the code files this run added (whichever try wrote them): the ordinances workflow is started for them
            codes = [p for p in mine if p.startswith(INBOX + "/") and Path(p).name != "sources.csv" and p not in was
                     and p not in left]
            if left:
                print(f"::warning title=Code finder::{len(left)} file(s) changed in the private repository while this "
                      "run read the sites: left as they are there")
            print(f"kept in the private repository: {len(staged)} file(s) written; {len(codes)} new code file(s) in "
                  f"{len({p.split('/')[2] for p in codes})} place(s)")
            output(len(codes))
            return
        except Stop as e:
            print(f"try {i}: {e}")
            if i < 5:
                time.sleep(i * 5)
        finally:
            shutil.rmtree(t, ignore_errors=True)  # its git folder, with the token in its remote, goes
    raise Stop("what the finder found could not be written to the private repository (five tries)")


def output(new_codes: int) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"new_codes={new_codes}\n")


def main(argv: list[str]) -> int:
    cmds = {"state": state, "finder-pull": finder_pull, "finder-push": finder_push}
    if len(argv) != 1 or argv[0] not in cmds:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        on_the_runner()
        cmds[argv[0]]()
    except Stop as e:
        print(f"::error title=Private data::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
