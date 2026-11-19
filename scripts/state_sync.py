#!/usr/bin/env python3
"""Three-way copy of the kept files for scripts/ci_runner.sh (public runner -> private data repository).

A job pulls the private repository's kept files at its start (`pull-state`) and pushes them back hours later
(`push-state`). In between, the owner may add a screenshot to imports/screens/, edit data/verified/, or another job
may store state/blocked.json. Replacing whole paths with the runner's copy (as push-state did: `rm -rf` then `cp -r`)
deletes those. Instead, against what the run started from (`record`, at pull time):

  - a file the run did not change is left as the repository has it now (a newer edit, or its removal, stays)
  - a file the run changed or added is written
  - a file the run removed (moved to processed/, cleared) is removed, unless the repository changed it meanwhile
  - a file added to the repository meanwhile is kept
  - state/blocked.json is merged host by host (the same rule as the shards' merge), not replaced

Without a record of the pull (an older job, or pull-state not run in this job), nothing is removed except a file the
run moved into a processed/ folder of the same path, and every file the run has is written.

  state_sync.py record BASE SRC PATH..        note what SRC (the fresh clone) holds under PATH.. in BASE
  state_sync.py apply BASE RUN DEST PATH..    copy RUN's PATH.. into DEST (the clone to commit), as above
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

MERGED_JSON = {"state/blocked.json"}


def sha(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def files_under(root: Path, rel: str) -> dict[str, Path]:
    p = root / rel
    if p.is_file():
        return {rel: p}
    out = {}
    if p.is_dir():
        for d, _, names in os.walk(p):
            if "/.git" in d.replace(os.sep, "/") + "/":
                continue
            for n in names:
                f = Path(d) / n
                if f.is_file():
                    out[f.relative_to(root).as_posix()] = f
    return out


def load_manifest(base: Path) -> dict | None:
    try:
        return json.loads((base / "manifest.json").read_text())
    except (OSError, ValueError):
        return None


def record(base: Path, src: Path, paths: list[str]) -> None:
    man = load_manifest(base) or {}
    for rel in paths:
        rel = rel.rstrip("/")
        for k in [k for k in man if k == rel or k.startswith(rel + "/")]:
            del man[k]
        for k, f in files_under(src, rel).items():
            man[k] = sha(f)
            if k in MERGED_JSON:
                (base / "copy" / k).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, base / "copy" / k)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps(man, indent=0, sort_keys=True))


def _blocked_pick(a: dict, b: dict) -> dict:
    # gacrawl/shard.py merge_state's rule for blocked.json
    return {**(b if (b.get("last_seen") or "") >= (a.get("last_seen") or "") else a),
            "count": max(a.get("count", 0), b.get("count", 0)),
            "first_seen": min(a.get("first_seen") or "9", b.get("first_seen") or "9"),
            "sources": sorted(set(a.get("sources", [])) | set(b.get("sources", []))),
            "urls": list(dict.fromkeys(a.get("urls", []) + b.get("urls", [])))[-20:]}


def _load_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def merge_json(base: dict | None, run: dict, other: dict) -> dict:
    """Host by host: a side that left a host as it was gives way to the side that changed (or removed) it."""
    out = {}
    for k in list(other) + [k for k in run if k not in other]:
        r, o = run.get(k), other.get(k)
        b = base.get(k) if base is not None else None
        if base is not None and r == b:
            v = o                       # the run left it: the repository's version (or its removal)
        elif base is not None and o == b:
            v = r                       # the repository left it: the run's version (or its removal)
        elif r is not None and o is not None:
            v = r if r == o else _blocked_pick(o, r)
        else:
            v = r if r is not None else o   # changed on one side, removed on the other: the change wins
        if v is not None:
            out[k] = v
    return out


def apply(base: Path, run: Path, dest: Path, paths: list[str]) -> None:
    man = load_manifest(base)
    wrote = kept = removed = 0
    for rel in paths:
        rel = rel.rstrip("/")
        mine = files_under(run, rel)
        theirs = files_under(dest, rel)
        for k, f in mine.items():
            d = dest / k
            if k in MERGED_JSON and d.is_file():
                r, o = _load_json(f), _load_json(d)
                if r is not None and o is not None:
                    b = _load_json(base / "copy" / k) if man is not None and k in man else None
                    m = merge_json(b, r, o)
                    if m != o:
                        d.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
                        wrote += 1
                    continue
            if man is not None and man.get(k) == sha(f):
                kept += 1               # unchanged by the run: whatever the repository has now stays
                continue
            if d.is_dir():
                shutil.rmtree(d)
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, d)
            wrote += 1
        gone_to = {Path(k).name for k in mine if "/processed/" in "/" + k}
        for k, d in theirs.items():
            if k in mine:
                continue
            if man is not None:
                drop = k in man and man[k] == sha(d)    # pulled, removed by the run, not changed since
            else:
                drop = Path(k).name in gone_to and "/processed/" not in "/" + k
            if drop:
                d.unlink()
                removed += 1
    print(f"state: {wrote} written, {kept} left as the repository has them, {removed} removed"
          + ("" if man is not None else " (no record of the pull: nothing removed but what moved to processed/)"))


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "record":
        record(Path(argv[1]), Path(argv[2]), argv[3:])
        return 0
    if len(argv) >= 4 and argv[0] == "apply":
        apply(Path(argv[1]), Path(argv[2]), Path(argv[3]), argv[4:])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
