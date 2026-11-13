"""The General Assembly's committee meeting schedule, from the legislature's own data service.

legis.ga.gov's schedule page (https://www.legis.ga.gov/schedule/all) asks its data service for `meetings` with a start
date, an end date and a chamber; this reads the same list with the same public token (sources/legis.py). Each meeting
is kept as one record: the committee or body, the chamber, when and where it meets, the agenda the notice links, and
the livestream link when the notice has one. Nothing is inferred: a meeting with no agenda link has none here.

Record: source "ga:legis", kind "meeting", key "ga:legis|meeting|<id>", meta in the same field names the local
councils' meetings use (EventBodyName, EventDate, EventAgendaFile) so one reader on the site serves both.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from urllib.parse import quote, urljoin

from .sources.legis import SITE, SOURCE, Legis, TokenRefused
from .store import Record

CHAMBER = {1: "House", 2: "Senate", "1": "House", "2": "Senate", "house": "House", "senate": "Senate", 0: "Joint", "0": "Joint"}
BACK_DAYS = 400        # the first read goes back this far; later reads go back RECENT_DAYS (a notice can change late)
RECENT_DAYS = 45
AHEAD_DAYS = 120
WINDOW = 31            # days asked for in one request


def _dstr(d: dt.date) -> str:
    """The date as the site's own page sends it (JavaScript's Date.toDateString: "Thu Nov 12 2026")."""
    return d.strftime("%a %b %d %Y")


def _first(m: dict, *names):
    for n in names:
        v = m.get(n)
        if v not in (None, "", [], {}):
            return v
    return None


def _text(v) -> str:
    if isinstance(v, dict):
        v = _first(v, "name", "title", "description", "value") or ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(v or ""))).strip()


def _url(v) -> str:
    if isinstance(v, dict):
        v = _first(v, "url", "uri", "href") or ""
    v = str(v or "").strip()
    if not v:
        return ""
    v = urljoin(SITE + "/", v)
    return v if v.startswith(("http://", "https://")) else ""


def shape(m: dict) -> dict | None:
    """One meeting of the service's list in this crawler's terms, or None when it has no readable start."""
    start = str(_first(m, "start", "startDate", "date", "meetingDate") or "")
    mm = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}))?", start)
    if not mm:
        return None
    day = mm.group(1)
    clock = f"{mm.group(2)}:{mm.group(3)}" if mm.group(2) else ""
    ch = _first(m, "chamber", "chamberType", "chamberId")
    chamber = CHAMBER.get(ch if not isinstance(ch, str) else ch.lower(), _text(ch) if isinstance(ch, str) else "")
    body = _text(_first(m, "subject", "title", "committeeName", "committee", "name", "description") or "")
    committee = _text(_first(m, "committeeName", "committee") or "")
    return {
        "id": str(_first(m, "id", "meetingId", "eventId") or f"{day}|{clock}|{body}"[:120]),
        "date": day, "time": clock, "chamber": chamber, "body": body or committee, "committee": committee,
        "location": _text(_first(m, "location", "room") or ""),
        "agenda": _url(_first(m, "agendaUri", "agendaUrl", "agenda")),
        "video": _url(_first(m, "livestreamUrl", "livestreamUri", "videoUrl")),
        "notes": _text(_first(m, "notes", "note", "body") or "")[:600],
        "cancelled": bool(_first(m, "isCancelled", "cancelled", "canceled", "isCanceled")),
    }


def run(fetcher, store, deadline: float | None = None, dry_run: bool = False, today: dt.date | None = None, log=print) -> dict:
    """Read the schedule from the legislature's data service and keep every meeting. One request per WINDOW days."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    lg = Legis(fetcher, log=log)
    first = not (store.kv_get("legis:meetings:read") if store is not None else None)
    lo = today - dt.timedelta(days=BACK_DAYS if (first or dry_run) else RECENT_DAYS)
    hi = today + dt.timedelta(days=AHEAD_DAYS)
    out = {"from": lo.isoformat(), "to": hi.isoformat(), "requests": 0, "meetings": 0, "new": 0, "unreadable": 0,
           "with_agenda": 0, "with_video": 0, "upcoming": 0, "by_chamber": {}}
    seen: dict[str, dict] = {}
    raw_sample = []
    # the schedule page's own request first (no start: what is coming up), then back from today a window at a time,
    # so a slow answer for old months costs those months only (Nov 13: a two-month window a year back never answered)
    windows = [(None, hi)]
    b = today
    while b > lo:
        a = max(lo, b - dt.timedelta(days=WINDOW - 1))
        windows.append((a, b))
        b = a - dt.timedelta(days=1)
    failed = 0
    for a, b in windows:
        if deadline and time.monotonic() > deadline - 45:
            out["stopped"] = "out of time"
            break
        q = (f"startDate={quote(_dstr(a))}&" if a else "") + f"endDate={quote(_dstr(b))}"
        t0 = time.monotonic()
        try:
            rows = lg.call("meetings?" + q)
        except TokenRefused as e:
            out["refused"] = str(e)
            break
        except Exception as e:  # noqa: BLE001 - one window the service does not answer must not lose the others
            out.setdefault("errors", []).append(f"{a or 'upcoming'}..{b}: {type(e).__name__} after {time.monotonic() - t0:.0f}s")
            failed += 1
            if failed >= 2:
                out["stopped"] = "the service did not answer twice"
                break
            continue
        out["requests"] += 1
        if rows is None:
            out.setdefault("errors", []).append(f"{a or 'upcoming'}..{b}: no answer")
        items = rows if isinstance(rows, list) else (rows or {}).get("items") or []
        log(f"legis meetings {a or 'upcoming'}..{b}: {len(items)} in {time.monotonic() - t0:.1f}s")
        for m in items:
            if not isinstance(m, dict):
                continue
            if len(raw_sample) < 2:
                raw_sample.append(m)
            s = shape(m)
            if not s:
                out["unreadable"] += 1
                continue
            seen[s["id"]] = s
    for s in seen.values():
        out["meetings"] += 1
        out["with_agenda"] += bool(s["agenda"])
        out["with_video"] += bool(s["video"])
        out["upcoming"] += s["date"] >= today.isoformat()
        out["by_chamber"][s["chamber"] or "?"] = out["by_chamber"].get(s["chamber"] or "?", 0) + 1
        if dry_run:
            continue
        rec = Record(SOURCE, "meeting", f"{SITE}/schedule/all#meeting-{s['id']}", title=f"{s['body']} {s['date']}"[:300],
                     text=s["notes"], published=s["date"], channel="api", jurisdiction="ga", evidence="primary",
                     key=f"{SOURCE}|meeting|{s['id']}",
                     meta={"EventBodyName": s["body"], "EventDate": s["date"], "EventTime": s["time"], "EventAgendaFile": s["agenda"],
                           "EventMinutesFile": "", "EventVideoPath": s["video"], "chamber": s["chamber"], "committee": s["committee"],
                           "location": s["location"], "cancelled": s["cancelled"], "found_on": f"{SITE}/schedule/all"})
        out["new"] += bool(store.add(rec))
    if dry_run:
        out["raw_sample"] = raw_sample
        out["sample"] = sorted(seen.values(), key=lambda s: (s["date"], s["time"]))[-4:]
    elif store is not None and seen and not out.get("refused") and not out.get("errors") and not out.get("stopped"):
        store.kv_set("legis:meetings:read", today.isoformat())
    return out
