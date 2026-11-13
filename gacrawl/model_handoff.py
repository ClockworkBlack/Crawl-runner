"""The open model's own job (model-work.yml) hands what it wrote to the crawl, the way the FEC re-read does.

The model job runs on its own runner for up to five hours on a copy of the crawl database and the kept state: what the
organizations that gave money are (finance enrich_orgs: the FEC, State Ethics Commission and IRS registries), donor
backgrounds (donors_ai), Rumor Mill pages and readings (rumors_ai), federal bill descriptions (federal_bills.ai_run).
What it wrote travels as one file, model-handoff.json in the private crawl-data release:

  {"kv": {"donor_about": {...}, "federal_bill_ai": {...}, "orgs": {...}, "irs_bmf": {...}, "ga_registry": {...}},
                                                         entries written or rewritten in this job
   "records": [organization records written in this job, as stored],
   "rumors": {"k:<candidate id>|<url>": its "ai" reading},
   "rumor_text": {"k:<candidate id>|<url>": {"text", "text_url", "text_tried", "text_error"}},   pages read in this job
                                                         (never published; a hand-off from before Nov 9 keys both by id)
   "rumor_rows": {"<candidate id>|<url>": a Rumor Mill item found in this job},
   "progress": {"news:<candidate id>": the news lane's progress entry written in this job},
   "train": {"data/train/<file>.jsonl": [log lines added in this job]}}

The candidate-news job (candidate-news.yml: research's GDELT news lane for hours on its own runner) hands its work over
the same way, as news-handoff.json: the articles and leads it found (records of source cand:<id>), the Rumor Mill items
(rumor_rows) and each candidate's news progress, so the crawl neither loses them nor asks GDELT again. Rumor Mill items
travel by candidate and url, never by id: ids are given in order of arrival, and the crawl adds its own while a job runs.

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
RECORD_KINDS = ("organization", "meeting_summary")     # meeting_summary: the readings of minutes and hearings
NEWS_KINDS = ("article", "lead")       # the news lane's (GDELT), only of a candidate's source (cand:<id>)
TEXT_KEYS = ("text", "text_url", "text_tried", "text_error")
ROW_KEYS = ("candidate_id", "url", "claim", "origin", "author", "first_seen", "opened")
PARTS = ("kv", "records", "rumors", "rumor_text", "rumor_rows", "progress", "train")


def rkey(r: dict) -> str:
    return f"{r.get('candidate_id', '')}|{(r.get('url') or '').strip().lower()}"


def _empty() -> dict:
    return {k: ([] if k == "records" else {}) for k in PARTS}


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


def export(store, root: Path, start: dict, carry: dict | None = None, progress: dict | None = None,
           only: tuple | None = None, kinds: tuple | None = None) -> dict:
    """What this job wrote since `start` (mark). `progress`: the candidate progress file's entries (news lane).
    `only`: the parts this job hands over (a job that writes one thing must not hand back the rest as it found it:
    its copy of a Rumor Mill reading may be older than what another job wrote meanwhile); `kinds`: of the records."""
    from .candidates import RumorMill

    out = _empty()
    for k in KV_KEYS:
        before = (start.get("kv") or {}).get(k) or {}
        now = _kv(store, k)
        # (a mark from before Nov 9 kept whole values, not signatures; both compare the same way)
        out["kv"][k] = {e: v for e, v in now.items() if before.get(e) not in (_sig(v), v)}
    since = start.get("at") or ""
    if since:
        with store._lock:
            rows = store.db.execute(
                "SELECT * FROM records WHERE changed_at >= ? AND (kind IN (%s) OR (kind IN (%s) AND source_id LIKE 'cand:%%'))"
                % (",".join("?" * len(RECORD_KINDS)), ",".join("?" * len(NEWS_KINDS))),
                (since, *RECORD_KINDS, *NEWS_KINDS)).fetchall()
        for r in rows:
            d = {k: r[k] for k in r.keys() if k not in ("rid", "thash")}
            d["meta"] = json.loads(d.get("meta") or "{}")
            out["records"].append(d)
    known = start.get("rumor_text")          # every item's id at the job's start (None: a mark too old to tell)
    for r in RumorMill(root).all():
        k = rkey(r)
        if known is not None and r["id"] not in known:
            # found in this job: the whole item travels (its page text and reading with it)
            row = {x: r.get(x) for x in ROW_KEYS if r.get(x) not in (None, "")}
            row["meta"] = dict(r.get("meta") or {})
            if r.get("ai"):
                row["ai"] = r["ai"]
            out["rumor_rows"][k] = row
            continue
        if r.get("ai"):
            out["rumors"]["k:" + k] = r["ai"]
        t = {x: (r.get("meta") or {}).get(x) for x in TEXT_KEYS}
        if any(t.values()) and (known or {}).get(r["id"]) != _sig(t):
            out["rumor_text"]["k:" + k] = {x: v for x, v in t.items() if v}
    if since:
        for pk, v in (progress or {}).items():
            if pk.startswith("news:") and isinstance(v, dict) and (v.get("last_ok") or "") >= since:
                out["progress"][pk] = v
    train = root / "data" / "train"
    for p in sorted(train.glob("*.jsonl")) if train.exists() else []:
        rel = str(p.relative_to(root))
        off = (start.get("train") or {}).get(rel, 0)
        with p.open("rb") as fh:
            fh.seek(off)
            lines = [x.decode("utf-8", "replace") for x in fh.read().splitlines() if x.strip()]
        if lines:
            out["train"][rel] = lines
    if only:
        blank = _empty()
        out = {k: (v if k in only else blank.get(k, type(v)())) for k, v in out.items()}
    if kinds:
        out["records"] = [d for d in out["records"] if d.get("kind") in kinds]
    return merge(carry, out) if carry else out


def merge(old: dict, new: dict) -> dict:
    """One hand-off holding a waiting one and this job's (this job's entries win: it started from the waiting one)."""
    out = _empty()
    for k in set((old.get("kv") or {})) | set((new.get("kv") or {})):
        out["kv"][k] = {**((old.get("kv") or {}).get(k) or {}), **((new.get("kv") or {}).get(k) or {})}
    recs = {r["key"]: r for r in (old.get("records") or [])}
    recs.update({r["key"]: r for r in (new.get("records") or [])})
    out["records"] = list(recs.values())
    for part in ("rumors", "rumor_text"):
        out[part] = {**(old.get(part) or {}), **(new.get(part) or {})}
    rows = {k: dict(v) for k, v in (old.get("rumor_rows") or {}).items()}
    for k, v in (new.get("rumor_rows") or {}).items():
        rows[k] = {**rows.get(k, {}), **v, "meta": {**(rows.get(k, {}).get("meta") or {}), **(v.get("meta") or {})}}
    out["rumor_rows"] = rows
    prog = dict(old.get("progress") or {})
    for k, v in (new.get("progress") or {}).items():
        if (v.get("last_ok") or "") >= ((prog.get(k) or {}).get("last_ok") or ""):
            prog[k] = v
    out["progress"] = prog
    for rel in set((old.get("train") or {})) | set((new.get("train") or {})):
        out["train"][rel] = list(dict.fromkeys((old.get("train") or {}).get(rel, []) + (new.get("train") or {}).get(rel, [])))
    return out


def has_work(h: dict) -> bool:
    return any(any(v.values()) if k == "kv" else bool(v) for k, v in h.items() if k in PARTS)


def apply(store, root: Path, h: dict, progress_path: Path | None = None) -> dict:
    from .candidates import RumorMill
    from .store import Record

    st = {k: 0 for k in PARTS}
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
        if not (d.get("kind") in RECORD_KINDS or (d.get("kind") in NEWS_KINDS and str(d.get("source_id", "")).startswith("cand:"))):
            continue                      # only what the model and news jobs may write
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
    by_key = {rkey(r): r for r in rm.all()}
    for k, row in (h.get("rumor_rows") or {}).items():
        if not isinstance(row, dict) or not row.get("candidate_id") or not row.get("url"):
            continue
        r = by_key.get(k)
        if r is None:
            r = rm.add(row["candidate_id"], row["url"], row.get("claim") or "", origin=row.get("origin") or "article",
                       seen_at=row.get("first_seen"), author=row.get("author") or "", meta=dict(row.get("meta") or {}),
                       opened=bool(row.get("opened")))
            by_key[k] = r
            st["rumor_rows"] += 1
        else:
            m = r.setdefault("meta", {})
            t = (row.get("meta") or {}).get("text")
            if t and len(t) > len(m.get("text") or ""):
                m["text"] = t
                rm._dirty = True
        if row.get("ai") and not r.get("ai") and (r.get("status") or "unverified") == "unverified":
            r["ai"] = row["ai"]
            rm._dirty = True
    by = {r["id"]: r for r in rm.all()}

    def item(k: str):
        return by_key.get(k[2:]) if k.startswith("k:") else by.get(k)     # by candidate and url; old hand-offs by id

    for rid, ai in (h.get("rumors") or {}).items():
        r = item(rid)
        if r is not None and r.get("ai") != ai and (r.get("status") or "unverified") == "unverified":
            r["ai"] = ai
            rm._dirty = True
            st["rumors"] += 1
    for rid, t in (h.get("rumor_text") or {}).items():
        r = item(rid)
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
    if progress_path is not None and h.get("progress"):
        from .scheduler import Progress

        prog = Progress(Path(progress_path))
        for pk, v in h["progress"].items():
            if not pk.startswith("news:") or not isinstance(v, dict):
                continue
            if (v.get("last_ok") or "") > (prog.get(pk).get("last_ok") or ""):
                prog.update(pk, **v)
                st["progress"] += 1
        prog.flush()
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
