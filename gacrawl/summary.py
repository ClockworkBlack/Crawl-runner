"""`gacrawl summary --since <run start>`: one Markdown page about a run, for the workflow's job summary.

What this run crawled (by outcome, with the sources that need a person), what the research added, where the
daytime backfill stands, what is still blocked, and the database's size and growth. Read from the state files the
run just wrote (progress.json, candidate_progress.json, research_last.json, backfill.json, blocked.json) and the
database, so it costs nothing extra.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _outcome(status: str) -> str:
    s = status or "never run"
    if s.startswith("partial"):
        return "partial (resumes next run)"
    if s.startswith("blocked"):
        return "blocked by the site"
    if s.startswith("stopped"):
        return "robots.txt disallows"
    if s.startswith("waiting"):
        return "waiting for the off-peak window"
    if s.startswith("skipped"):
        return "not reached (time)"
    if s.startswith("error"):
        return "error"
    if s == "ok":
        return "ok"
    return s.split(":")[0].split(" (")[0]


def build(root: Path, state_dir: Path, store, since: str | None, title: str = "Crawl run") -> str:
    now = dt.datetime.now(dt.timezone.utc)
    prog = _load(state_dir / "progress.json", {})
    since_dt = None
    if since:
        try:
            since_dt = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            since_dt = None
    ran = {}
    for sid, p in prog.items():
        lr = p.get("last_run")
        if not lr:
            continue
        try:
            t = dt.datetime.fromisoformat(lr)
        except ValueError:
            continue
        if since_dt is None or t >= since_dt - dt.timedelta(minutes=5):
            ran[sid] = p
    out = [f"## {title}", ""]
    # ---- crawl
    by = Counter(_outcome(p.get("status", "")) for p in ran.values())
    new = sum(int(p.get("new") or 0) for p in ran.values())
    seen = sum(int(p.get("seen") or 0) for p in ran.values())
    fetched = sum(int(p.get("fetched") or 0) for p in ran.values())
    out.append(f"**Crawl:** {len(ran)} sources ran · {new:,} new records of {seen:,} seen · {fetched:,} pages and files fetched")
    if by:
        out.append("")
        out.append("| outcome | sources |")
        out.append("|---|---:|")
        for k, n in by.most_common():
            out.append(f"| {k} | {n} |")
    top = sorted(((int(p.get("new") or 0), sid) for sid, p in ran.items()), reverse=True)[:10]
    if top and top[0][0]:
        out.append("")
        out.append("Most new records: " + ", ".join(f"{sid} ({n:,})" for n, sid in top if n))
    needs = [(sid, p.get("status", "")) for sid, p in ran.items()
             if _outcome(p.get("status", "")) in ("blocked by the site", "robots.txt disallows", "error")]
    if needs:
        out.append("")
        out.append("<details><summary>Sources that need a person (%d)</summary>\n" % len(needs))
        out.append("| source | what happened |")
        out.append("|---|---|")
        for sid, st in sorted(needs)[:80]:
            out.append(f"| {sid} | {re.sub(r'[|`]', ' ', st)[:160]} |")
        out.append("\n</details>")
    # ---- research
    rl = _load(state_dir / "research_last.json", None)
    if rl:
        added = rl.get("added") or {}
        out.append("")
        out.append(f"**Candidate research:** {rl.get('researched', 0)} of {rl.get('due', 0)} due candidates"
                   + (f" · {rl.get('workers')} at a time" if rl.get("workers") else "")
                   + (f" · {rl.get('cut_short')} cut short by the budget (still due)" if rl.get("cut_short") else "")
                   + (f" · {rl['steps_skipped']} steps skipped for a busy host" if rl.get("steps_skipped") else "")
                   + (" · host turns past the deadline: " + ", ".join(f"{h} {n}" for h, n in sorted((rl.get("late_hosts") or {}).items(), key=lambda kv: -kv[1])[:5])
                      if rl.get("late_hosts") else "")
                   + (f" · GDELT answered without JSON {rl['gdelt_not_json']} times" if rl.get("gdelt_not_json") else "")
                   + (f" · news: {rl['news'].get('queries', 0)} searches for {rl['news'].get('candidates', 0)} of "
                      f"{rl['news'].get('due', 0)} candidates due" if isinstance(rl.get("news"), dict) else "")
                   + (f" · {rl['google_news']}" if rl.get("google_news") else "")
                   + (" · added " + ", ".join(f"{v:,} {k}" for k, v in sorted(added.items(), key=lambda kv: -kv[1])) if added else " · nothing new"))
    cp = _load(state_dir / "candidate_progress.json", {})
    if cp:
        never = sum(1 for k, v in cp.items() if k.startswith("cand:") and not v.get("last_ok"))
        out.append(f"Candidates researched at least once: {sum(1 for k, v in cp.items() if k.startswith('cand:') and v.get('last_ok')):,}"
                   + (f" · never yet: {never:,}" if never else ""))
    # ---- backfill
    bf = _load(state_dir / "backfill.json", None)
    if bf:
        m = now.strftime("%Y-%m")
        cfg = _load(root / "config" / "backfill.json", {})
        used = int((bf.get("months") or {}).get(m, 0)) + int((cfg.get("month_minutes_before") or {}).get(m, 0))
        cap = cfg.get("month_minutes_cap", "?")
        state = "finished" if bf.get("done") else ("on" if cfg.get("enabled") else "off")
        out.append("")
        out.append(f"**Daytime backfill:** {state} · {bf.get('runs', 0)} runs so far · Actions minutes this month: {used:,} of {cap}"
                   + (f" · {bf.get('done_reason')}" if bf.get("done") else ""))
    # ---- blocked
    bl = _load(state_dir / "blocked.json", {})
    if bl:
        out.append("")
        out.append(f"**Blocked hosts (left alone until they expire):** {len(bl)} — " +
                   ", ".join(sorted(bl)[:25]) + (" …" if len(bl) > 25 else ""))
    # ---- database
    if store is not None:
        try:
            total = store.total()
            kinds = store.counts("kind")
            today = now.date().isoformat()
            fresh = store.db.execute("SELECT count(*) FROM records WHERE first_seen >= ?", (today,)).fetchone()[0]
            out.append("")
            out.append(f"**Database:** {total:,} records ({fresh:,} first seen today) · " +
                       ", ".join(f"{v:,} {k}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:12]))
        except Exception:  # noqa: BLE001
            pass
    # ---- rumor mill video
    rv = root / "data" / "rumor_mill" / "videos.jsonl"
    if rv.exists():
        n = sum(1 for l in rv.read_text(encoding="utf-8").splitlines() if l.strip())
        if n:
            chans = defaultdict(int)
            for l in rv.read_text(encoding="utf-8").splitlines():
                if l.strip():
                    try:
                        chans[(json.loads(l).get("channel") or {}).get("title") or "?"] += 1
                    except Exception:  # noqa: BLE001
                        pass
            out.append("")
            out.append(f"**Rumor Mill – Video:** {n} videos from {len(chans)} channels not confirmed as official: " +
                       ", ".join(f"{k} ({v})" for k, v in sorted(chans.items(), key=lambda kv: -kv[1])[:8]))
    return "\n".join(out) + "\n"
