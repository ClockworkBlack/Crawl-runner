"""The open model's own job (model-work.yml) hands what it wrote to the crawl, the way the FEC re-read does.

The model job runs on its own runner for up to five hours on a copy of the crawl database and the kept state: what the
organizations that gave money are (finance enrich_orgs: the FEC, State Ethics Commission and IRS registries), donor
backgrounds (donors_ai), Rumor Mill pages and readings (rumors_ai), federal bill descriptions (federal_bills.ai_run).
What it wrote travels as one file, model-handoff.json in the private crawl-data release:

  {"kv": {"donor_about": {...}, "federal_bill_ai": {...}, "orgs": {...}, "irs_bmf": {...}, "ga_registry": {...}},
                                                         entries written or rewritten in this job
   "records": [organization records written in this job, as stored],
   "rumors": {rumor id: its "ai" reading},
   "rumor_text": {rumor id: {"text", "text_url", "text_tried", "text_error"}},   pages read in this job (never published)
   "train": {"data/train/<file>.jsonl": [log lines added in this job]}}

The crawl's merge job applies it (apply) before its export, and deletes the file only after the database holding it
is stored, so a run that dies in between leaves it for the next run. Applying twice changes nothing. A model job that
starts while the last hand-off still waits applies it to its own copy first (carry) and then hands over both, so it
never sits idle waiting for the crawl. The entity index is never handed over (the crawl's ids are the only ones).

    gacrawl model-handoff mark --out mark.json         (start of the model job: train log sizes, kv and rumor texts as they were)
    gacrawl model-handoff export --mark mark.json --out model-handoff.json [--carry waiting.json]
    gacrawl model-handoff apply --in model-handoff.json
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

KV_KEYS = ("donor_about", "federal_bill_ai", "orgs", "irs_bmf", "ga_registry")
RECORD_KINDS = ("organization",)
TEXT_KEYS = ("text", "text_url", "text_tried", "text_error")


def _kv(store, k: str) -> dict:
    try:
        return json.loads(store.kv_get(k) or "{}")
    except ValueError:
        return {}


def _sig(v) -> str:
    return hashlib.sha1(json.dumps(v, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def mark(store, root: Path) -> dict:
    import datetime as dt

    from .candidates import RumorMill

    train = root / "data" / "train"
    return {"kv": {k: {e: _sig(v) for e, v in _kv(store, k).items()} for k in KV_KEYS},
            "rumor_text": {r["id"]: _sig({x: (r.get("meta") or {}).get(x) for x in TEXT_KEYS}) for r in RumorMill(root).all()},
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "train": {str(p.relative_to(root)): p.stat().st_size for p in sorted(train.glob("*.jsonl"))} if train.exists() else {}}


def export(store, root: Path, start: dict, carry: dict | None = None) -> dict:
    from .candidates import RumorMill

    out = {"kv": {}, "records": [], "rumors": {}, "rumor_text": {}, "train": {}}
    for k in KV_KEYS:
        before = (start.get("kv") or {}).get(k) or {}
        now = _kv(store, k)
        # (a mark from before Nov 9 kept whole values, not signatures; both compare the same way)
        out["kv"][k] = {e: v for e, v in now.items() if before.get(e) not in (_sig(v), v)}
    since = start.get("at") or ""
    if since:
        with store._lock:
            rows = store.db.execute("SELECT * FROM records WHERE kind IN (%s) AND changed_at >= ?" % ",".join("?" * len(RECORD_KINDS)),
                                    (*RECORD_KINDS, since)).fetchall()
        for r in rows:
            d = {k: r[k] for k in r.keys() if k not in ("rid", "thash")}
            d["meta"] = json.loads(d.get("meta") or "{}")
            out["records"].append(d)
    for r in RumorMill(root).all():
        if r.get("ai"):
            out["rumors"][r["id"]] = r["ai"]
        t = {x: (r.get("meta") or {}).get(x) for x in TEXT_KEYS}
        if any(t.values()) and (start.get("rumor_text") or {}).get(r["id"]) != _sig(t):
            out["rumor_text"][r["id"]] = {x: v for x, v in t.items() if v}
    train = root / "data" / "train"
    for p in sorted(train.glob("*.jsonl")) if train.exists() else []:
        rel = str(p.relative_to(root))
        off = (start.get("train") or {}).get(rel, 0)
        with p.open("rb") as fh:
            fh.seek(off)
            lines = [x.decode("utf-8", "replace") for x in fh.read().splitlines() if x.strip()]
        if lines:
            out["train"][rel] = lines
    return merge(carry, out) if carry else out


def merge(old: dict, new: dict) -> dict:
    """One hand-off holding a waiting one and this job's (this job's entries win: it started from the waiting one)."""
    out = {"kv": {}, "records": [], "rumors": {}, "rumor_text": {}, "train": {}}
    for k in set((old.get("kv") or {})) | set((new.get("kv") or {})):
        out["kv"][k] = {**((old.get("kv") or {}).get(k) or {}), **((new.get("kv") or {}).get(k) or {})}
    recs = {r["key"]: r for r in (old.get("records") or [])}
    recs.update({r["key"]: r for r in (new.get("records") or [])})
    out["records"] = list(recs.values())
    for part in ("rumors", "rumor_text"):
        out[part] = {**(old.get(part) or {}), **(new.get(part) or {})}
    for rel in set((old.get("train") or {})) | set((new.get("train") or {})):
        out["train"][rel] = list(dict.fromkeys((old.get("train") or {}).get(rel, []) + (new.get("train") or {}).get(rel, [])))
    return out


def has_work(h: dict) -> bool:
    return any(any(v.values()) if k == "kv" else bool(v) for k, v in h.items() if k in ("kv", "records", "rumors", "rumor_text", "train"))


def apply(store, root: Path, h: dict) -> dict:
    from .candidates import RumorMill
    from .store import Record

    st = {"kv": 0, "records": 0, "rumors": 0, "rumor_text": 0, "train": 0}
    for k, entries in (h.get("kv") or {}).items():
        if k not in KV_KEYS or not entries:
            continue
        cur = _kv(store, k)
        for e, v in entries.items():
            if cur.get(e) != v:
                cur[e] = v
                st["kv"] += 1
        store.kv_set(k, json.dumps(cur, ensure_ascii=False, separators=(",", ":")))
    for d in h.get("records") or []:
        if d.get("kind") not in RECORD_KINDS:
            continue                      # only what the model job may write
        with store._lock:
            cur = store.db.execute("SELECT changed_at FROM records WHERE key=?", (d["key"],)).fetchone()
        if cur and d.get("changed_at") and (cur[0] or "") > d["changed_at"]:
            continue                      # the crawl wrote it again after the job did (a carried hand-off's older copy)
        rec = Record(d["source_id"], d["kind"], d.get("url") or "", title=d.get("title") or "", text=d.get("text") or "",
                     markdown=d.get("markdown") or "", published=d.get("published"), channel=d.get("channel") or "",
                     evidence=d.get("evidence") or "", jurisdiction=d.get("jurisdiction") or "", meta=d.get("meta") or {},
                     key=d["key"])
        st["records"] += bool(store.add(rec))
    store.flush()
    rm = RumorMill(root)
    by = {r["id"]: r for r in rm.all()}
    for rid, ai in (h.get("rumors") or {}).items():
        r = by.get(rid)
        if r is not None and r.get("ai") != ai and (r.get("status") or "unverified") == "unverified":
            r["ai"] = ai
            rm._dirty = True
            st["rumors"] += 1
    for rid, t in (h.get("rumor_text") or {}).items():
        r = by.get(rid)
        if r is None or not isinstance(t, dict):
            continue
        m = r.setdefault("meta", {})
        if t.get("text") and len(t["text"]) > len(m.get("text") or ""):
            m.update({x: t[x] for x in TEXT_KEYS if t.get(x)})
            m.pop("text_error", None) if not t.get("text_error") else None
            rm._dirty = True
            st["rumor_text"] += 1
        elif not t.get("text") and t.get("text_tried", "") > m.get("text_tried", ""):
            m.update({x: t[x] for x in ("text_tried", "text_error") if t.get(x)})
            rm._dirty = True
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
