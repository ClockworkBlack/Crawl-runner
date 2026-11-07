"""Daytime backfill: back-to-back runs until candidate profiles stop filling, then back to the nightly cadence.

config/backfill.json   the switch and the limits (edit `enabled` to stop or restart it)
state/backfill.json    what has happened (committed with the rest of state): Actions minutes per month (every run),
                       backfill runs, the last runs' research yield, done

A backfill run is a normal run with one crawl shard (by day only API sources are due: HTML page crawling keeps to
the off-peak windows in settings.yaml), a long candidate-research block and finance. At its end it dispatches the
next one while it is inside the day window, under the month's minute cap, and not done. Done means every candidate
has been researched at least once and `quiet_runs` runs in a row each added fewer than `min_added` records.
While backfill is active the nightly run crawls with more shards (`night_shards`, `night_minutes`) so council
minutes and roll calls (the Grand Cross side) fill faster inside the off-peak window.

`python -m gacrawl.backfill plan` needs only the standard library (the workflow's prepare job runs it before
installing anything); `gacrawl backfill next` runs in the merge job.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "enabled": False, "window": {"start": "06:00", "end": "21:30", "timezone": "America/New_York"},
    "research_minutes": 100, "finance_minutes": 20, "crawl_minutes": 18,
    "night_shards": 3, "night_minutes": 40,
    "stop": {"never_researched": 0, "quiet_runs": 2, "min_added": 60},
    "month_minutes_cap": 6000,
}


def load_cfg(root: Path = ROOT) -> dict:
    p = Path(root) / "config" / "backfill.json"
    cfg = json.loads(json.dumps(DEFAULTS))
    if p.exists():
        d = json.loads(p.read_text())
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif not k.startswith("_"):
                cfg[k] = v
    return cfg


def load_state(root: Path = ROOT) -> dict:
    p = Path(root) / "state" / "backfill.json"
    return json.loads(p.read_text()) if p.exists() else {"months": {}, "runs": 0, "quiet": 0, "done": False, "history": []}


def save_state(st: dict, root: Path = ROOT) -> None:
    p = Path(root) / "state" / "backfill.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    st["history"] = st.get("history", [])[-40:]
    p.write_text(json.dumps(st, indent=1) + "\n")


def _now(cfg, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    tz = ZoneInfo(cfg["window"]["timezone"]) if ZoneInfo else dt.timezone.utc
    return now.astimezone(tz)


def in_window(cfg, now=None) -> bool:
    t = _now(cfg, now).strftime("%H:%M")
    return cfg["window"]["start"] <= t < cfg["window"]["end"]


def month_minutes(st, now=None, cfg=None) -> int:
    """Actions minutes used this month: the repository's own count from the Actions API when a run has taken it
    (every run of every workflow and branch: tests, probes, cancelled runs; `months_api`), else the runs that
    reached their end (`months`) plus any the config says were used before counting began."""
    m = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")
    tally = int((st.get("months") or {}).get(m, 0)) + int(((cfg or {}).get("month_minutes_before") or {}).get(m, 0))
    return max(tally, int((st.get("months_api") or {}).get(m, 0)))


def actions_minutes(repo: str, token: str, since: dt.datetime, cache: dict | None = None, now=None,
                    api: str = "https://api.github.com", session=None) -> tuple[int, dict]:
    """Billable minutes of every workflow run in `repo` created since `since`, as GitHub bills them: each job that
    ran rounded up to the minute (skipped jobs and self-hosted runners cost nothing; a job still running counts up to
    now). `cache` holds finished runs' minutes (they never change), so each run only asks about new runs.
    Returns (total, cache)."""
    import math

    import requests

    now = now or dt.datetime.now(dt.timezone.utc)
    cache = dict(cache or {})
    s = session or requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gacrawl-backfill"})

    def pages(url, key, params):
        page = 1
        while True:
            r = s.get(url, params={**params, "per_page": 100, "page": page}, timeout=30)
            r.raise_for_status()
            rows = r.json().get(key) or []
            yield from rows
            if len(rows) < 100:
                return
            page += 1

    def ts(v):
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None

    total = 0
    for run in pages(f"{api}/repos/{repo}/actions/runs", "workflow_runs", {"created": f">={since:%Y-%m-%d}"}):
        rid = str(run["id"])
        if rid in cache:
            total += int(cache[rid])
            continue
        n = 0
        for j in pages(f"{api}/repos/{repo}/actions/runs/{rid}/jobs", "jobs", {"filter": "all"}):
            if j.get("conclusion") == "skipped" or not j.get("started_at") or "self-hosted" in (j.get("labels") or []):
                continue
            sec = ((ts(j.get("completed_at")) or now) - ts(j["started_at"])).total_seconds()
            if sec > 0:
                n += math.ceil(sec / 60)
        total += n
        if run.get("status") == "completed":
            cache[rid] = n
    return total, cache


def _now(cfg, now=None):
    tz = ZoneInfo(cfg["window"]["timezone"]) if ZoneInfo else dt.timezone.utc
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)


def _parse_until(cfg):
    tz = ZoneInfo(cfg["window"]["timezone"]) if ZoneInfo else dt.timezone.utc
    return dt.datetime.fromisoformat(cfg["until"]).replace(tzinfo=tz)


def free_mode(cfg, now=None) -> bool:
    """After the catch-up: everything sized to fit GitHub's free 2,000 minutes a month. Not on the public runner
    (config/runner.json), whose minutes are free."""
    if os.environ.get("PUBLIC_RUNNER") == "true":
        return False
    return bool(cfg.get("free_after") and _now(cfg, now) >= dt.datetime.fromisoformat(cfg["free_after"]).replace(tzinfo=_now(cfg, now).tzinfo))


def active(cfg, st, now=None) -> tuple[bool, str]:
    if not cfg.get("enabled"):
        return False, "backfill is switched off (config/backfill.json)"
    if cfg.get("until") and _now(cfg, now) >= _parse_until(cfg):
        return False, f"backfill ended at {cfg['until']} (config/backfill.json until)"
    if st.get("done") and not (os.environ.get("PUBLIC_RUNNER") == "true" and cfg.get("continuous")):
        # (the public runner's minutes are free and it gathers far more than candidate profiles: with `continuous`
        # it keeps chaining runs; the old stop rule, which counts only candidate research, ended it on Nov 7)
        return False, "backfill finished: " + (st.get("done_reason") or "profiles stopped filling")
    used, cap = month_minutes(st, now, cfg), int(cfg["month_minutes_cap"])
    if os.environ.get("PUBLIC_RUNNER") == "true":
        cap = 10 ** 9        # the public runner's minutes are free: no monthly cap
    if used >= cap:
        return False, f"month's Actions minutes reached the cap ({used} of {cap})"
    return True, f"active ({used} of {cap} minutes this month)"


def plan(root: Path = ROOT, now=None) -> dict:
    cfg, st = load_cfg(root), load_state(root)
    if os.environ.get("PUBLIC_RUNNER") == "true" and cfg.get("runner"):
        cfg = {**cfg, **cfg["runner"]}       # free minutes: the faster profile
    ok, why = active(cfg, st, now)
    return {"active": ok, "go": ok and in_window(cfg, now), "why": why if ok and in_window(cfg, now) or not ok else "outside the day window",
            "research_minutes": cfg["research_minutes"], "finance_minutes": cfg["finance_minutes"], "crawl_minutes": cfg["crawl_minutes"],
            "night_shards": cfg["night_shards"], "night_minutes": cfg["night_minutes"],
            "crawl_shards": cfg.get("crawl_shards") or "", "backfill_shards": cfg.get("backfill_shards") or 1,
            "police_minutes": cfg.get("police_minutes") or "", "police_news_minutes": cfg.get("police_news_minutes") or "",
            "free": free_mode(cfg, now), "free_today": _now(cfg, now).weekday() in ((cfg.get("free") or {}).get("days") or range(7)), **{"free_" + k: v for k, v in (cfg.get("free") or {}).items() if k != "days"}}


def next_run(root: Path, minutes: int, backfill_run: bool, research: dict | None, never_researched: int | None,
             run_id: str = "", now=None, month_total: int | None = None, run_cache: dict | None = None) -> dict:
    """Record this run's minutes (every run), and for a backfill run its yield; decide whether to dispatch the next.
    `month_total`: the repository's own count of this month's minutes (actions_minutes), kept as `months_api`."""
    cfg, st = load_cfg(root), load_state(root)
    if os.environ.get("PUBLIC_RUNNER") == "true" and cfg.get("runner"):
        cfg = {**cfg, **cfg["runner"]}
    now = now or dt.datetime.now(dt.timezone.utc)
    if run_id and run_id == st.get("last_run"):
        return {"go": False, "why": "this run was already counted"}
    st["last_run"] = run_id
    m = now.strftime("%Y-%m")
    st.setdefault("months", {})[m] = int(st["months"].get(m, 0)) + int(minutes)
    if month_total is not None:
        st.setdefault("months_api", {})[m] = int(month_total)
        st["months_api"] = {k: v for k, v in st["months_api"].items() if k >= m}      # this month only
    if run_cache is not None:
        st["run_minutes"] = {"month": m, "runs": run_cache}
    out = {"minutes": minutes, "month_minutes": month_minutes(st, now, cfg)}
    if month_total is not None:
        out["month_minutes_counted_by_runs"] = int(st["months"][m]) + int((cfg.get("month_minutes_before") or {}).get(m, 0))
    if backfill_run:
        added = sum((research or {}).get("added", {}).values()) if research else 0
        st["runs"] = int(st.get("runs", 0)) + 1
        stop = cfg["stop"]
        st["quiet"] = int(st.get("quiet", 0)) + 1 if added < int(stop["min_added"]) else 0
        st.setdefault("history", []).append({"at": now.isoformat(timespec="minutes"), "minutes": minutes, "added": added,
                                             "researched": (research or {}).get("researched"), "never_researched": never_researched})
        if never_researched is not None and never_researched <= int(stop["never_researched"]) and st["quiet"] >= int(stop["quiet_runs"]):
            st["done"] = True
            st["done_reason"] = (f"every candidate researched, and the last {st['quiet']} runs each added fewer than "
                                 f"{stop['min_added']} records ({now.date().isoformat()})")
        out.update(added=added, never_researched=never_researched, quiet=st["quiet"])
    ok, why = active(cfg, st, now)
    go = backfill_run and ok and in_window(cfg, now)
    out.update(go=go, why=why if (go or not ok) else ("outside the day window" if backfill_run else "not a backfill run"),
               done=bool(st.get("done")))
    save_state(st, root)
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["plan"]:
        p = plan(Path(os.environ.get("GACRAWL_ROOT", ROOT)))
        import shlex

        for k, v in p.items():   # shell-quoted: the workflow evals these lines
            print(f"{k}={shlex.quote(str(v).lower() if isinstance(v, bool) else str(v))}")
    else:
        sys.exit("usage: python -m gacrawl.backfill plan")
