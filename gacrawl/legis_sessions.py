"""The General Assembly's sessions, with no one editing the config when a new one starts.

config/legislature.yaml lists the sessions this crawler follows (`sessions.current`, `history`, `names`, `years`).
The legislature's own data service lists every session (legis.ga.gov `sessions`: id, description such as
"2027-2028 Regular Session" or "2027 Special Session", isCurrent). `gacrawl legis` reads that list each run and keeps
it in the database (kv `legis:sessions`); load() adds every listed session newer than the config's to the sessions
followed, names it and gives its years from its description, and once a newer regular session exists, the older
ones stop being re-read every run (they are finished: read once more as history, then kept).

Every reader of the session config goes through load(): the legislature source, the act parser and the bill list
exported to Electo, and the bill reasons.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

KV = "legis:sessions"


def describe(desc: str) -> tuple[str, list[int], bool]:
    """'2027-2028 Regular Session' -> ('2027-2028', [2027, 2028], True); '2027 Special Session' -> ('2027 special',
    [2027, 2027], False)."""
    d = " ".join(str(desc or "").split())
    m = re.match(r"(\d{4})\s*-\s*(\d{4})\s+Regular", d, re.I)
    if m:
        return f"{m.group(1)}-{m.group(2)}", [int(m.group(1)), int(m.group(2))], True
    m = re.match(r"(\d{4}).*Special", d, re.I)
    if m:
        return f"{m.group(1)} special", [int(m.group(1)), int(m.group(1))], False
    m = re.match(r"(\d{4})", d)
    y = int(m.group(1)) if m else 0
    return d or "", [y, y], "regular" in d.lower()


def full_name(name: str) -> str:
    """The config's short name as the legislature writes it: '2025-2026' -> '2025-2026 Regular Session'."""
    if re.fullmatch(r"\d{4}-\d{4}", name or ""):
        return f"{name} Regular Session"
    m = re.fullmatch(r"(\d{4}) special", name or "", re.I)
    if m:
        return f"{m.group(1)} Special Session"
    return name


def save(store, rows) -> int:
    """Keep the legislature's session list (the `sessions` answer) for load(); returns how many were listed."""
    keep = [{"id": int(r["id"]), "description": r.get("description") or "", "isCurrent": bool(r.get("isCurrent"))}
            for r in rows or [] if isinstance(r, dict) and str(r.get("id") or "").isdigit()]
    if keep:
        store.kv_set(KV, json.dumps({"at": dt.date.today().isoformat(), "sessions": keep}))
    return len(keep)


def listed(store) -> list[dict]:
    if store is None:
        return []
    try:
        return (json.loads(store.kv_get(KV) or "{}") or {}).get("sessions") or []
    except (TypeError, ValueError):
        return []


def load(root: Path, store=None) -> dict:
    """{"current": [...], "history": [...], "names": {id: short name}, "years": {id: [y1, y2]},
    "full": {id: the legislature's own description}, "new": [ids added from the legislature's list]}."""
    from .registry import load_yaml

    cfg = (load_yaml(Path(root) / "config" / "legislature.yaml") or {}).get("sessions") or {}
    current = [int(s) for s in cfg.get("current") or []]
    history = [int(s) for s in cfg.get("history") or []]
    names = {int(k): str(v) for k, v in (cfg.get("names") or {}).items()}
    years = {int(k): list(v) for k, v in (cfg.get("years") or {}).items()}
    full = {k: full_name(v) for k, v in names.items()}
    known = set(current) | set(history)
    newest = max(known) if known else 0
    added, newest_regular = [], 0
    for r in sorted(listed(store), key=lambda r: r["id"]):
        sid = r["id"]
        if r.get("description"):
            full[sid] = r["description"]
        nm, yrs, regular = describe(r.get("description"))
        if sid > newest and sid not in known:
            added.append(sid)
            history.append(sid)
            names.setdefault(sid, nm)
            years.setdefault(sid, yrs)
            if regular:
                newest_regular = max(newest_regular, sid)
        if r.get("isCurrent") and sid not in current and sid >= newest:
            current.append(sid)
    for sid in added:
        if sid not in current:
            current.append(sid)
    if newest_regular:
        # a newer regular session: the ones before it are finished
        current = [s for s in current if s >= newest_regular]
    return {"current": sorted(set(current)), "history": history, "names": names, "years": years, "full": full,
            "new": added}


def act_sessions(root: Path, store=None, configured=None) -> list[int]:
    """Sessions whose enacted acts are parsed and exported: the configured ones and every session after them."""
    base = sorted(int(s) for s in (configured or [1031, 1032, 1033, 1034]))
    s = load(root, store)
    later = [x for x in set(s["current"]) | set(s["history"]) if base and x > max(base)]
    return sorted(set(base) | set(later))
