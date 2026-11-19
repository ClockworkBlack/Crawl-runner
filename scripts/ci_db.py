#!/usr/bin/env python3
"""The crawl database (and its copies) in the "crawl-data" release: scripts/ci_db.sh calls this.

    ci_db.py pull                    the stored database -> data/crawl.db
    ci_db.py push                    data/crawl.db -> the release (FTS merge first)
    ci_db.py fetch OUT [NAME]        the stored compressed file NAME (default crawl.db.zst) -> OUT, checked
    ci_db.py store FILE NAME         a compressed file under another name (tempsave-crawl.db.zst, crawl-backup.db.zst)
    ci_db.py drop NAME               remove a stored copy (its parts and list, and a single file of that name)

How a file is stored. GitHub refuses a release file of 2 GiB or more, and `gh release upload --clobber` deletes the old
file before it sends the new one. The compressed database was 1.7 GiB on Nov. 18, 2026 and growing about 0.1 GiB a day:
within days the upload would have been refused with the old copy already deleted, and the next run, finding no
database, would have started an empty one ("no database yet (first run)") and stored that. So a file is stored as
parts of at most PART_BYTES, each named for its upload (NAME.<gen>.part-NN), and a small list NAME.parts.json that
names them with sizes and SHA-256. The list is written last, after the parts are confirmed on the release, so a
reader sees the whole old copy or the whole new one; the parts of older uploads are deleted after that. While the
whole file still fits in one release file it is also stored the old way under NAME itself, for runs started with the
older scripts (a run uses the scripts of the commit it was started from); `pull` and `fetch` take whichever of the
two is newer.

What stops a run instead of going on:
  - the release exists but no database could be read from it (a failed download, a part missing, a checksum that does
    not match, zstd missing): exit 1. Only a release that does not exist, or ALLOW_EMPTY_DB=1, starts an empty
    database. (Before: any failed download printed "no database yet (first run)" and exited 0; the merge job would
    then have stored a near-empty database over the real one and deleted the hand-offs it believed applied.)
  - a database less than half the size of the stored one is not uploaded (ALLOW_SMALLER_DB=1 to do it on purpose,
    after a purge).

Needs: gh (GH_TOKEN; GH_REPO or GITHUB_REPOSITORY names the repository), zstd, python3. DATA_TAG (default crawl-data);
LIVE_TAG (a test tag with no database of its own reads the live one).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PART_BYTES = int(os.environ.get("CI_DB_PART_BYTES", str(1 << 30)))          # 1 GiB a part (the limit is 2 GiB)
SINGLE_MAX = int(os.environ.get("CI_DB_SINGLE_MAX", str(1900 * (1 << 20))))  # also stored as one file below this
LEGACY = os.environ.get("CI_DB_LEGACY", "1") != "0"                          # ... unless switched off
TRIES = int(os.environ.get("CI_DB_TRIES", "4"))
WAIT = float(os.environ.get("CI_DB_WAIT", "20"))                             # seconds between tries (x try number)
GUARD_MIN = int(os.environ.get("CI_DB_GUARD_MIN", str(100 * (1 << 20))))            # the size check starts above this
DB = "crawl.db.zst"


class Stop(Exception):
    """The run must not go on (printed as an error, exit 1)."""


def say(*a):
    print(*a, flush=True)


def repo() -> str:
    r = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    if not r:
        raise Stop("no repository named (GH_REPO or GITHUB_REPOSITORY)")
    return r


def gh(*args: str, out: Path | None = None, ok: bool = False) -> subprocess.CompletedProcess:
    """gh with the given arguments. out: stdout goes to that file. ok: a failure is returned, not raised."""
    if out is not None:
        with open(out, "wb") as f:
            r = subprocess.run(["gh", *args], stdout=f, stderr=subprocess.PIPE)
    else:
        r = subprocess.run(["gh", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode and not ok:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {(r.stderr or b'').decode(errors='replace').strip()[:300]}")
    return r


def again(what: str, fn, tries: int = TRIES):
    """fn(), tried again with growing waits; the last failure is raised."""
    for i in range(1, tries + 1):
        try:
            return fn()
        except Stop:
            raise
        except Exception as e:  # noqa: BLE001
            if i == tries:
                raise
            say(f"{what}: {str(e)[:200]}; try {i + 1} of {tries} in {int(WAIT * i)} s")
            time.sleep(WAIT * i)


def release(tag: str) -> dict | None:
    """The release ({"id", "assets": {name: {id, size, updated_at}}}), None when there is no such release. Any other
    failure is raised: "could not ask" is never "nothing there"."""
    def ask():
        r = gh("api", f"repos/{repo()}/releases/tags/{tag}", ok=True)
        if r.returncode:
            err = (r.stderr or b"").decode(errors="replace")
            if "404" in err or "Not Found" in err:
                return None
            raise RuntimeError(f"the release {tag} could not be read: {err.strip()[:200]}")
        rel = json.loads(r.stdout)
        # the release's own list holds the first page of files only; ask for all of them
        assets, page = [], 1
        while True:
            got = json.loads(gh("api", f"repos/{repo()}/releases/{rel['id']}/assets?per_page=100&page={page}").stdout)
            assets += got
            if len(got) < 100:
                break
            page += 1
        return {"id": rel["id"], "assets": {a["name"]: {"id": a["id"], "size": a["size"], "updated_at": a.get("updated_at") or ""}
                                            for a in assets}}
    return again(f"release {tag}", ask)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def download(asset_id: int, to: Path, size: int | None = None) -> None:
    def get():
        gh("api", "-H", "Accept: application/octet-stream", f"repos/{repo()}/releases/assets/{asset_id}", out=to)
        if size is not None and to.stat().st_size != size:
            raise RuntimeError(f"{to.name}: {to.stat().st_size:,} bytes arrived of {size:,}")
    again(f"download {to.name}", get)


def stored(rel: dict, name: str) -> dict | None:
    """What the release holds for `name`, the newer of its two forms: {"kind": "parts", "list": {...}, "at"} or
    {"kind": "single", "asset": {...}, "at"}. None when it holds neither."""
    A = rel["assets"]
    best = None
    lst = A.get(name + ".parts.json")
    if lst:
        best = {"kind": "parts", "asset": lst, "at": lst["updated_at"]}
    one = A.get(name)
    if one and (best is None or one["updated_at"] > best["at"]):
        best = {"kind": "single", "asset": one, "at": one["updated_at"]}
    return best


def stored_bytes(rel: dict, name: str, work: Path) -> int:
    s = stored(rel, name)
    if not s:
        return 0
    if s["kind"] == "single":
        return int(s["asset"]["size"])
    try:
        p = work / "old.parts.json"
        download(s["asset"]["id"], p)
        return int(json.loads(p.read_text()).get("bytes") or 0)
    except Exception:  # noqa: BLE001 — the size check is skipped rather than block the upload on an unreadable list
        return 0


def fetch(name: str, out: Path, tag: str) -> bool:
    """The stored compressed file `name` -> out, complete and checked. False: the release holds no such file (or
    does not exist). Raises Stop when it holds one that cannot be read."""
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / (out.name + ".parts")
    for attempt in range(1, TRIES + 1):
        rel = release(tag)
        if rel is None:
            return False
        s = stored(rel, name)
        if s is None:
            # an upload in progress? (the single file is absent while `gh release upload --clobber` sends its
            # replacement: 80-105 s for the database). Look again before believing it.
            if attempt < TRIES:
                say(f"{name}: not on the release {tag} just now; looking again in {int(WAIT * attempt)} s")
                time.sleep(WAIT * attempt)
                continue
            return False
        try:
            if s["kind"] == "single":
                download(s["asset"]["id"], out, int(s["asset"]["size"]))
            else:
                shutil.rmtree(work, ignore_errors=True)
                work.mkdir(parents=True)
                download(s["asset"]["id"], work / "list.json")
                L = json.loads((work / "list.json").read_text())
                with open(out, "wb") as dst:
                    for part in L["parts"]:
                        a = rel["assets"].get(part["name"])
                        if not a:
                            raise RuntimeError(f"{part['name']} is not on the release (a newer upload replaced it?)")
                        pp = work / "part"
                        download(a["id"], pp, int(part["bytes"]))
                        if sha256(pp) != part["sha256"]:
                            raise RuntimeError(f"{part['name']}: checksum does not match the list")
                        with open(pp, "rb") as src:
                            shutil.copyfileobj(src, dst, 1 << 22)
                        pp.unlink()
                if out.stat().st_size != int(L["bytes"]):
                    raise RuntimeError(f"{name}: {out.stat().st_size:,} bytes put together of {int(L['bytes']):,}")
            if name.endswith(".zst"):
                r = subprocess.run(["zstd", "-t", "-q", str(out)], stderr=subprocess.PIPE)
                if r.returncode:
                    raise RuntimeError(f"{name}: zstd finds the file damaged ({(r.stderr or b'').decode(errors='replace').strip()[:120]})")
            shutil.rmtree(work, ignore_errors=True)
            return True
        except FileNotFoundError as e:
            raise Stop(f"{e.filename or 'a program'} is not installed on this runner; the stored {name} cannot be read") from None
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(work, ignore_errors=True)
            if attempt == TRIES:
                raise Stop(f"the stored {name} could not be read after {TRIES} tries: {str(e)[:300]}") from None
            say(f"{name}: {str(e)[:200]}; reading the release again in {int(WAIT * attempt)} s")
            time.sleep(WAIT * attempt)
    return False


def upload(tag: str, path: Path, clobber: bool = False) -> None:
    again(f"upload {path.name}", lambda: gh("release", "upload", tag, str(path), *(["--clobber"] if clobber else [])))


def delete(asset_id: int) -> None:
    gh("api", "-X", "DELETE", f"repos/{repo()}/releases/assets/{asset_id}", ok=True)


def store(path: Path, name: str, tag: str, guard: bool = True) -> None:
    """path -> the release, as `name`: parts, then the list that names them, then the old parts go."""
    size = path.stat().st_size
    work = path.parent / (path.name + ".up")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        rel = release(tag)
        if rel is None:
            gh("release", "create", tag, "--title", "Crawl data", "--prerelease", "--notes",
               "The crawl database (crawl.db.zst, in parts) and the Electo import bundle, updated by the crawl workflow.")
            rel = release(tag) or {"id": 0, "assets": {}}
        was = stored_bytes(rel, name, work) if guard else 0
        if guard and was > GUARD_MIN and size * 2 < was and os.environ.get("ALLOW_SMALLER_DB") != "1":
            raise Stop(f"{name}: this copy is {size / 2**20:,.0f} MiB and the stored one {was / 2**20:,.0f} MiB. A database that "
                       "lost more than half is not stored over the one on the release (ALLOW_SMALLER_DB=1 to store it anyway, "
                       "after a purge)")
        gen = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid() % 10000:04d}"
        parts = []
        with open(path, "rb") as src:
            n = 0
            while True:
                pn = f"{name}.{gen}.part-{n:02d}"
                pp, h, wrote = work / pn, hashlib.sha256(), 0
                with open(pp, "wb") as dst:
                    while wrote < PART_BYTES:
                        b = src.read(min(1 << 22, PART_BYTES - wrote))
                        if not b:
                            break
                        dst.write(b)
                        h.update(b)
                        wrote += len(b)
                if not wrote and n:
                    pp.unlink()
                    break
                upload(tag, pp)
                pp.unlink()
                parts.append({"name": pn, "bytes": wrote, "sha256": h.hexdigest()})
                n += 1
                if wrote < PART_BYTES:
                    break
        # every part is on the release at its full size before the list names them
        rel = release(tag) or {"assets": {}}
        for p in parts:
            a = rel["assets"].get(p["name"])
            if not a or int(a["size"]) != p["bytes"]:
                raise Stop(f"{p['name']} did not arrive whole on the release; the list was not changed, the stored {name} stands")
        lp = work / (name + ".parts.json")
        lp.write_text(json.dumps({"v": 1, "name": name, "gen": gen, "bytes": size, "parts": parts,
                                  "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}, indent=1))
        upload(tag, lp, clobber=True)
        # the old way too, while it fits: runs started from an older commit read only this
        legacy = LEGACY and size < SINGLE_MAX
        if legacy:
            lk = work / name
            try:
                os.link(path, lk)
            except OSError:
                shutil.copyfile(path, lk)
            try:
                upload(tag, lk, clobber=True)
            except Exception as e:  # noqa: BLE001 — the parts and their list are stored: this copy is a convenience
                say(f"{name}: the single-file copy was not stored ({str(e)[:160]}); the parts are")
        # older uploads' parts go; so does a single file that no longer matches (too big to store that way now)
        rel = release(tag) or {"assets": {}}
        keep = {p["name"] for p in parts}
        for an, a in rel["assets"].items():
            if an.startswith(name + ".") and ".part-" in an and an not in keep:
                delete(a["id"])
            elif an == name and not legacy:
                delete(a["id"])
        say(f"uploaded {size / 2**20:,.0f} MiB as {len(parts)} part(s)" + (" and as one file" if legacy else ""))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def drop(name: str, tag: str) -> int:
    rel = release(tag)
    n = 0
    for an, a in ((rel or {}).get("assets") or {}).items():
        if an in (name, name + ".parts.json") or (an.startswith(name + ".") and ".part-" in an):
            delete(a["id"])
            n += 1
    return n


def pull(tag: str) -> None:
    data = Path("data")
    data.mkdir(exist_ok=True)
    z = data / DB
    if fetch(DB, z, tag):
        subprocess.run(["zstd", "-d", "-q", "-f", "--rm", str(z), "-o", str(data / "crawl.db")], check=True)
        say(f"database: {(data / 'crawl.db').stat().st_size / 2**30:.1f}G (zstd)")
        return
    rel = release(tag)
    gz = ((rel or {}).get("assets") or {}).get("crawl.db.gz")
    if gz:       # a release from before zstd
        download(gz["id"], data / "crawl.db.gz", int(gz["size"]))
        subprocess.run(["gunzip", "-f", str(data / "crawl.db.gz")], check=True)
        say("database: (gzip)")
        return
    live = os.environ.get("LIVE_TAG") or ""
    if live and live != tag:
        pull(live)
        say(f"(a copy of the live database: this test run writes only to {tag})")
        return
    if rel is None or os.environ.get("ALLOW_EMPTY_DB") == "1":
        say("no database yet (first run)")
        return
    raise Stop(f"the release {tag} exists but holds no database. Starting an empty one would store it over whatever is "
               "restored later, so this run stops here. If this really is a first run, set ALLOW_EMPTY_DB=1; if the "
               "database was lost, restore it with the db-backup workflow (crawl-backup.db.zst, tempsave-crawl.db.zst)")


def push(tag: str) -> None:
    db = Path("data/crawl.db")
    if not db.exists():
        say("no database to upload")
        return
    import sqlite3

    con = sqlite3.connect(db)
    if os.environ.get("OPTIMIZE_FTS") == "true":
        con.execute("INSERT INTO records_fts(records_fts) VALUES('optimize')")       # weekly: one segment per index level
    else:
        con.execute("INSERT INTO records_fts(records_fts, rank) VALUES('merge', 300)")   # nightly: bounded incremental merge
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")    # fold the write-ahead log into the file before copying
    con.close()
    z = Path(DB)
    subprocess.run(["zstd", "-T0", "-3", "-q", "-f", str(db), "-o", str(z)], check=True)
    store(z, DB, tag)
    gz = ((release(tag) or {}).get("assets") or {}).get("crawl.db.gz")
    if gz:
        delete(gz["id"])        # one copy only


def main(argv: list[str]) -> int:
    tag = os.environ.get("DATA_TAG") or "crawl-data"
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "pull":
            pull(tag)
        elif cmd == "push":
            push(tag)
        elif cmd == "fetch" and len(argv) >= 3:
            name = argv[3] if len(argv) > 3 else DB
            if not fetch(name, Path(argv[2]), tag):
                raise Stop(f"the release {tag} holds no {name}")
            say(f"{name}: {Path(argv[2]).stat().st_size / 2**20:,.0f} MiB")
        elif cmd == "store" and len(argv) >= 4:
            store(Path(argv[2]), argv[3], tag, guard=False)
        elif cmd == "drop" and len(argv) >= 3:
            say(f"{argv[2]}: {drop(argv[2], tag)} file(s) removed")
        else:
            say("usage: ci_db.py pull | push | fetch OUT [NAME] | store FILE NAME | drop NAME")
            return 2
    except Stop as e:
        say(f"::error title=Crawl database::{e}")
        return 1
    except Exception as e:  # noqa: BLE001 — a failure is a failure, said in one line (never "nothing there")
        say(f"::error title=Crawl database::{cmd} failed: {type(e).__name__}: {str(e)[:400]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
