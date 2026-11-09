"""The open model's own job (model-work.yml) hands what it wrote to the crawl, the way the FEC re-read does.

The model job runs on its own runner for up to five hours on a copy of the crawl database and the kept state: donor
backgrounds (donors_ai), Rumor Mill readings (rumors_ai) and federal bill descriptions (federal_bills.ai_run). What
it wrote travels as one file, model-handoff.json in the private crawl-data release:

  {"kv": {"donor_about": {...}, "federal_bill_ai": {...}},   entries written or rewritten in this job
   "rumors": {rumor id: its "ai" reading},
   "train": {"data/train/<file>.jsonl": [log lines added in this job]}}

The crawl's merge job applies it (apply) before its export, and deletes the file only after the database holding it
is stored, so a run that dies in between leaves it for the next run. Applying twice changes nothing.

    gacrawl model-handoff mark --out mark.json         (start of the model job: train log sizes, kv as it was)
    gacrawl model-handoff export --mark mark.json --out model-handoff.json
    gacrawl model-handoff apply --in model-handoff.json
"""
from __future__ import annotations

import json
from pathlib import Path

KV_KEYS = ("donor_about", "federal_bill_ai")


def _kv(store, k: str) -> dict:
    try:
        return json.loads(store.kv_get(k) or "{}")
    except ValueError:
        return {}


def mark(store, root: Path) -> dict:
    train = root / "data" / "train"
    return {"kv": {k: _kv(store, k) for k in KV_KEYS},
            "train": {str(p.relative_to(root)): p.stat().st_size for p in sorted(train.glob("*.jsonl"))} if train.exists() else {}}


def export(store, root: Path, start: dict) -> dict:
    from .candidates import RumorMill

    out = {"kv": {}, "rumors": {}, "train": {}}
    for k in KV_KEYS:
        before = (start.get("kv") or {}).get(k) or {}
        now = _kv(store, k)
        out["kv"][k] = {e: v for e, v in now.items() if before.get(e) != v}
    for r in RumorMill(root).all():
        if r.get("ai"):
            out["rumors"][r["id"]] = r["ai"]
    train = root / "data" / "train"
    for p in sorted(train.glob("*.jsonl")) if train.exists() else []:
        rel = str(p.relative_to(root))
        off = (start.get("train") or {}).get(rel, 0)
        with p.open("rb") as fh:
            fh.seek(off)
            lines = [x.decode("utf-8", "replace") for x in fh.read().splitlines() if x.strip()]
        if lines:
            out["train"][rel] = lines
    return out


def apply(store, root: Path, h: dict) -> dict:
    from .candidates import RumorMill

    st = {"kv": 0, "rumors": 0, "train": 0}
    for k, entries in (h.get("kv") or {}).items():
        if k not in KV_KEYS or not entries:
            continue
        cur = _kv(store, k)
        for e, v in entries.items():
            if cur.get(e) != v:
                cur[e] = v
                st["kv"] += 1
        store.kv_set(k, json.dumps(cur, ensure_ascii=False, separators=(",", ":")))
    rm = RumorMill(root)
    by = {r["id"]: r for r in rm.all()}
    for rid, ai in (h.get("rumors") or {}).items():
        r = by.get(rid)
        if r is not None and r.get("ai") != ai and (r.get("status") or "unverified") == "unverified":
            r["ai"] = ai
            rm._dirty = True
            st["rumors"] += 1
    rm.flush()
    for rel, lines in (h.get("train") or {}).items():
        p = (root / rel).resolve()
        if not str(p).startswith(str((root / "data" / "train").resolve())) or not p.name.endswith(".jsonl"):
            continue                      # only the training logs, nowhere else
        p.parent.mkdir(parents=True, exist_ok=True)
        have = set(p.read_text(encoding="utf-8").splitlines()) if p.exists() else set()
        new = [x for x in lines if x not in have]
        if new:
            with p.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(new) + "\n")
            st["train"] += len(new)
    return st
