"""Reading: one item at a time, each finished and checkpointed before the next (an interrupted run loses nothing)."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import time
from pathlib import Path

from .. import config
from .model import contest_id
from .sources import SOURCES, Skip

# raise a source's number when its reader changes: its items are read again (and those it could not read, tried again)
READER = {"manual": 1, "medsl": 2, "legislators": 3, "fec": 2, "openelections": 2, "sos_zip": 1, "sos_pdf": 1, "clerk": 1}
# a failed item is tried again by the next run (three tries, then every three days)
ORDER = ["manual", "legislators", "medsl", "openelections", "sos_zip", "fec", "sos_pdf", "clerk"]
# documented data services and file hosts (a dataset's API, a repository's API): the crawler's spacing, back-off and
# block list apply to them as to every host; robots.txt, written for page crawlers, is not what governs an API
API_ENDPOINTS = ["dataverse.harvard.edu/api", "api.github.com/repos/openelections"]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Net:
    def __init__(self, cfg: dict):
        self.f = config.make_fetcher(cfg)
        self.f.max_bytes = self.f.max_doc_bytes = 600_000_000        # result files: one CSV of every House race is 30 MB
        self.f.api_endpoints = list(self.f.api_endpoints) + API_ENDPOINTS
        self.requests = 0

    def bytes(self, url: str, params: dict | None = None) -> bytes:
        self.requests += 1
        r = self.f.get(url, params=params, use_cache=False)
        if r.status != 200:
            raise IOError(f"HTTP {r.status} from {url}")
        return r.content

    def text(self, url: str, params: dict | None = None) -> str:
        return self.bytes(url, params).decode("utf-8", errors="replace")

    def json(self, url: str, params: dict | None = None):
        return json.loads(self.bytes(url, params))


def paths(cfg: dict) -> tuple[Path, Path]:
    work = Path(os.environ.get("HISTORY_WORK") or (Path(cfg["data_dir"]) / "history"))
    state = Path(cfg["state_dir"]) / "history.json"
    return work, state


def load_state(p: Path) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"items": {}, "discover": {}, "runs": []}


def save_state(p: Path, st: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def norm_path(work: Path, item_id: str) -> Path:
    src, _, rest = item_id.partition(":")
    return work / "norm" / src / (re.sub(r"[^A-Za-z0-9._-]+", "_", rest)[:150] + ".jsonl.gz")


def run(cfg: dict, only: list[str] | None = None, max_minutes: float | None = None, net=None, log=print) -> dict:
    work, sp = paths(cfg)
    work.mkdir(parents=True, exist_ok=True)
    st = load_state(sp)
    net = net or Net(cfg)
    end = time.monotonic() + max_minutes * 60 if max_minutes else None
    summary = {"read": 0, "contests": 0, "kept_for_later": 0, "failed": 0, "left": 0, "by_source": {}}
    for name in [n for n in ORDER if not only or n in only]:
        src = SOURCES[name]
        per = summary["by_source"].setdefault(name, {"read": 0, "contests": 0, "held": 0, "failed": 0, "left": 0})
        try:
            items = src.discover(net)
            st["discover"][name] = {"at": _now(), "n": len(items)}
        except Exception as exc:                      # the index did not answer: this source waits for the next run
            st["discover"][name] = {"at": _now(), "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            log(f"{name}: its index could not be read ({type(exc).__name__}: {str(exc)[:160]})")
            per["failed"] += 1; summary["failed"] += 1
            save_state(sp, st)
            continue
        log(f"{name}: {len(items)} items listed")
        current = {it.id for it in items}
        for it in items:
            rec = st["items"].get(it.id) or {}
            if rec.get("reader") == READER[name] and rec.get("status") in ("done", "held"):
                continue
            if rec.get("status") == "failed" and rec.get("reader") == READER[name] and rec.get("tries", 0) >= 3 \
                    and rec.get("at", "") > (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"):
                continue                               # three tries; again after three days
            if end and time.monotonic() > end:
                per["left"] += 1; summary["left"] += 1
                continue
            rec = {"reader": READER[name], "title": it.title, "url": it.url, "tries": rec.get("tries", 0) + 1, "at": _now()}
            np_ = norm_path(work, it.id)
            np_.parent.mkdir(parents=True, exist_ok=True)
            tmp = np_.with_suffix(".tmp")
            n = 0
            try:
                seen = set()
                with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                    for c in src.read(net, it, work):
                        cid = contest_id(c)
                        if cid in seen:
                            continue
                        seen.add(cid)
                        c["item"] = it.id
                        fh.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
                        n += 1
                tmp.replace(np_)
                rec.update(status="done", n=n)
                per["read"] += 1; per["contests"] += n; summary["read"] += 1; summary["contests"] += n
                log(f"  {it.id}: {n} contests")
            except Skip as why:
                tmp.unlink(missing_ok=True)
                rec.update(status="held", note=str(why)[:700])
                per["held"] += 1; summary["kept_for_later"] += 1
                log(f"  {it.id}: {str(why)[:200]}")
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                rec.update(status="failed", note=f"{type(exc).__name__}: {str(exc)[:400]}")
                per["failed"] += 1; summary["failed"] += 1
                log(f"  {it.id}: failed ({rec['note'][:200]})")
            st["items"][it.id] = rec
            save_state(sp, st)
        # a file the publisher replaced (a new version, a new week's member list): the old item's contests go
        for iid in [i for i in st["items"] if i.split(":", 1)[0] == name and i not in current]:
            norm_path(work, iid).unlink(missing_ok=True)
            del st["items"][iid]
    st["runs"] = (st.get("runs") or [])[-20:] + [{"at": _now(), **{k: v for k, v in summary.items() if k != "by_source"}}]
    save_state(sp, st)
    return summary


def iter_contests(work: Path):
    for p in sorted((work / "norm").glob("*/*.jsonl.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def status(cfg: dict) -> dict:
    work, sp = paths(cfg)
    st = load_state(sp)
    out = {"sources": {}, "decades": {}, "last_runs": (st.get("runs") or [])[-3:]}
    for iid, rec in st["items"].items():
        s = out["sources"].setdefault(iid.split(":", 1)[0], {"done": 0, "held": 0, "failed": 0, "contests": 0, "notes": []})
        s[rec.get("status", "failed")] = s.get(rec.get("status", "failed"), 0) + 1
        s["contests"] += rec.get("n", 0)
        if rec.get("status") != "done" and len(s["notes"]) < 6:
            s["notes"].append(f"{iid}: {rec.get('note', '')[:160]}")
    for name, d in (st.get("discover") or {}).items():
        out["sources"].setdefault(name, {"done": 0, "held": 0, "failed": 0, "contests": 0, "notes": []})["index"] = d
    if (work / "norm").exists():
        for c in iter_contests(work):
            k = f"{c['scope']} {c['y'] // 10 * 10}s"
            d = out["decades"].setdefault(k, {"contests": 0, "primaries": 0, "with_votes": 0})
            d["contests"] += 1
            d["primaries"] += 1 if "primary" in c["stage"] else 0
            d["with_votes"] += 1 if c.get("tot") else 0
    return out
