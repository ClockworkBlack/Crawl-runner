"""The nightly legis-votes job's hand-off to the crawl (.github/workflows/legis-votes.yml, scripts/legis_handoff.sh).

The job reads roll calls (legis.ga.gov, then LegiScan) on a copy of the crawl database, so it never races the crawl's
own upload. What it read reaches the main database as two files in the private crawl-data release, which the crawl
merge job applies and deletes once stored:

  legis-delta.db.zst   every record it added or changed (gacrawl delta --since)
  legis-kv.json        its marks: which bills' roll calls were read (legis:votes:<id>), how often a bill's member
                       lists failed the check (legis:votes_try:<id>), which LegiScan datasets were read
                       (legiscan:dataset:<session>), and `_since`, where its records start (a run that carries an
                       unapplied hand-off starts its own from there)

Applying keeps the later of two read dates and the higher try count, so an older hand-off never undoes a newer mark.
"""
from __future__ import annotations

import json
from pathlib import Path

PREFIXES = ("legis:votes:", "legis:votes_try:", "legiscan:dataset:", "legiscan:read:")


def export_kv(store, out: Path, since: str | None = None) -> dict:
    with store._lock:
        rows = store.db.execute("SELECT k, v FROM kv WHERE " + " OR ".join("k LIKE ?" for _ in PREFIXES),
                                tuple(p + "%" for p in PREFIXES)).fetchall()
    d = {k: v for k, v in rows}
    if since:
        d["_since"] = since
    Path(out).write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
    return {"marks": len(rows), "file": str(out)}


def _newer(k: str, new: str, old: str | None) -> bool:
    if old is None:
        return True
    if k.startswith("legis:votes_try:"):
        try:
            return int(new) > int(old)
        except ValueError:
            return False
    if k.startswith("legiscan:dataset:"):
        return True               # a dataset hash: the job read it after the crawl's copy was taken
    return new > old              # ISO dates


def apply(store, delta: Path | None, kv: Path | None) -> dict:
    from .shard import merge_delta

    out = {}
    if delta and Path(delta).exists():
        out["delta"] = merge_delta(store, Path(delta))
    n = 0
    if kv and Path(kv).exists():
        for k, v in json.loads(Path(kv).read_text()).items():
            if not k.startswith(PREFIXES):
                continue
            if _newer(k, str(v), store.kv_get(k)):
                store.kv_set(k, str(v))
                n += 1
    out["marks"] = n
    store.flush()
    return out
