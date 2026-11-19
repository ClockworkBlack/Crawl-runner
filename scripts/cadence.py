#!/usr/bin/env python3
"""Is the nightly or the weekly work due? (crawl.yml, prepare job; the merge job records it as done.)

    python3 scripts/cadence.py due   EVENT_SCHEDULE IS_MAIN   -> prints NIGHTLY=true|false, WEEKLY=..., OPTIMIZE_FTS=...
    python3 scripts/cadence.py done  NIGHTLY WEEKLY           -> stamps state/cadence.json

The nightly work (OCR of scanned documents, the nightly legislature and research shares) and the weekly work (the
seed, the schools registry, the full-text index rebuilt) were keyed to the schedule that started the run. A workflow's
concurrency group holds one waiting run, and each daytime backfill run queues the next one about half an hour before
it ends; the queued run took the place of the scheduled 04:07 and Sunday 09:41 runs (ten runs with no jobs at all,
Nov. 13-18, 2026), so from Nov. 14 no run was nightly and none weekly. Now the work is due by the date it was last
done, whichever run gets there: nightly after NIGHT_HOURS, weekly after WEEK_DAYS. The scheduled runs still count."""
import datetime as dt
import json
import sys
from pathlib import Path

NIGHTLY_CRON, WEEKLY_CRON = "7 4 * * *", "41 9 * * 0"
NIGHT_HOURS, WEEK_DAYS = 20, 6.5
P = Path("state/cadence.json")


def load() -> dict:
    try:
        return json.loads(P.read_text())
    except (OSError, ValueError):
        return {}


def age_h(st: dict, k: str, now: dt.datetime) -> float:
    try:
        return (now - dt.datetime.fromisoformat(st[k])).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return 1e9


def due(schedule: str, is_main: str, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    st = load()
    main = is_main == "true"
    nightly = schedule == NIGHTLY_CRON or (main and age_h(st, "nightly_at", now) >= NIGHT_HOURS)
    weekly = schedule == WEEKLY_CRON or (main and age_h(st, "weekly_at", now) >= WEEK_DAYS * 24)
    return {"NIGHTLY": nightly, "WEEKLY": weekly, "OPTIMIZE_FTS": weekly}


def done(nightly: str, weekly: str, now: dt.datetime | None = None) -> dict:
    now = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    st = load()
    if nightly == "true":
        st["nightly_at"] = now
    if weekly == "true":
        st["weekly_at"] = now
    P.parent.mkdir(parents=True, exist_ok=True)
    P.write_text(json.dumps(st, indent=1) + "\n")
    return st


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["due"] and len(a) >= 3:
        for k, v in due(a[1], a[2]).items():
            print(f"{k}={'true' if v else 'false'}")
    elif a[:1] == ["done"] and len(a) >= 3:
        print(json.dumps(done(a[1], a[2])))
    else:
        sys.exit("usage: cadence.py due SCHEDULE IS_MAIN | done NIGHTLY WEEKLY")
