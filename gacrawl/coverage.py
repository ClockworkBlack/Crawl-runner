"""What the data holds for each of Electo's features, after every run.

A gap used to be found only by auditing the database by hand (no roll calls for days, no school board members, every
DeKalb agenda item stored twice). This counts, per feature, what the store and the state files hold, compares it with
the last run, and flags what is still at zero or shrank:

  state/coverage.json   the counts (kept between runs: the comparison's baseline)
  data/coverage.md      the report, by feature, with what each flag means

Run: gacrawl coverage. The merge job runs it after the database is stored; both files are kept in the private
repository (the public runner's logs only get a one-line count).
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path

from .registry import load_json, write_json_atomic

# (section, key, label, kind of check). "need": zero is a gap; "info": counted, never flagged at zero.
FEATURES: list[tuple[str, str, str, str]] = [
    ("Legislature", "bills", "Bills", "need"),
    ("Legislature", "roll_calls", "General Assembly roll calls (how each member voted)", "need"),
    ("Legislature", "legislators", "Legislators", "need"),
    ("Legislature", "bill_reasons", "Why bills passed, failed or were vetoed (news, statements)", "need"),
    ("Legislature", "code_acts", "Code changes of enacted acts", "need"),
    ("Local government", "meetings", "Meetings", "need"),
    ("Local government", "meeting_places", "Places with meetings", "need"),
    ("Local government", "agenda_items", "Agenda items", "need"),
    ("Local government", "agenda_places", "Places with agenda items", "need"),
    ("Local government", "local_roll_calls", "Council and commission roll calls", "need"),
    ("Local government", "documents", "Minutes, packets and other documents", "need"),
    ("Local government", "ordinances", "Ordinances", "need"),
    ("Local government", "ordinance_places", "Places with ordinances", "need"),
    ("Local government", "officials", "Officials", "need"),
    ("Local government", "official_places", "Places with officials", "need"),
    ("Local government", "videos", "Meeting videos", "need"),
    ("Local government", "video_transcripts", "Videos with a transcript", "need"),
    ("Candidates and donors", "contributions_state", "Campaign contributions (state filings)", "need"),
    ("Candidates and donors", "contributions_local", "Campaign contributions (local filings)", "need"),
    ("Candidates and donors", "statements", "Public statements", "need"),
    ("Candidates and donors", "mentions", "Research mentions", "need"),
    ("Schools", "schools", "Schools", "need"),
    ("Schools", "school_stats", "School statistics", "need"),
    ("Schools", "milestones", "Georgia Milestones scores", "need"),
    ("Schools", "spending_per_student", "Spending per student", "need"),
    ("Schools", "ccrpi", "CCRPI scores", "need"),
    ("Schools", "district_sites_read", "School districts whose own site has been read", "need"),
    ("Schools", "school_pages", "School web pages read", "need"),
    ("Schools", "board_members", "School board members", "need"),
    ("Schools", "superintendents", "Superintendents", "need"),
    ("Schools", "principals", "Principals", "need"),
    ("Schools", "budget_docs", "School budget documents", "need"),
    ("Policing", "departments", "Police and sheriff's departments found", "need"),
    ("Policing", "chiefs", "Departments with a chief or sheriff named", "need"),
    ("Policing", "incidents", "Reported incidents", "need"),
    ("Policing", "incident_departments", "Departments with reported incidents", "need"),
    ("Crawl health", "sources_ok", "Sources read completely at least once", "info"),
    ("Crawl health", "sources_waiting", "Sources waiting for the off-peak window", "info"),
    ("Crawl health", "sources_error", "Sources whose last run failed", "info"),
    ("Crawl health", "sources_blocked", "Sources refused by bot protection or robots.txt", "info"),
    ("Crawl health", "probe_timeouts", "Sources marked failed only because they ran out of time (should be 0)", "info"),
]
SHRINK = 0.9   # a count below 90% of the last run's is flagged


def _one(db, sql: str, args: tuple = ()) -> int:
    try:
        row = db.execute(sql, args).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:  # noqa: BLE001 — a missing column or table counts as nothing
        return 0


def measure(root: Path, store) -> dict[str, int]:
    db = store.db
    c: dict[str, int] = {}
    k = lambda kind: _one(db, "SELECT count(*) FROM records WHERE kind=?", (kind,))  # noqa: E731
    places = lambda kind: _one(db, "SELECT count(DISTINCT jurisdiction) FROM records WHERE kind=? AND jurisdiction<>''", (kind,))  # noqa: E731
    c["bills"] = k("bill")
    c["roll_calls"] = _one(db, "SELECT count(*) FROM records WHERE source_id='ga:legis' AND kind='vote'")
    c["legislators"] = k("legislator")
    c["bill_reasons"] = k("bill_reason")
    c["code_acts"] = k("code_act")
    c["meetings"], c["meeting_places"] = k("meeting"), places("meeting")
    c["agenda_items"], c["agenda_places"] = k("agenda_item"), places("agenda_item")
    c["local_roll_calls"] = _one(db, "SELECT count(*) FROM records WHERE kind='vote' AND source_id<>'ga:legis' "
                                     "AND source_id NOT LIKE 'cand:%'")
    c["documents"] = k("document")
    c["ordinances"], c["ordinance_places"] = k("ordinance"), places("ordinance")
    c["officials"], c["official_places"] = k("official"), places("official")
    c["videos"] = k("video")
    c["video_transcripts"] = _one(db, "SELECT count(*) FROM records WHERE kind='video' AND length(text) > 500")
    c["contributions_state"] = _one(db, "SELECT count(*) FROM records WHERE kind='contribution' AND jurisdiction=''")
    c["contributions_local"] = _one(db, "SELECT count(*) FROM records WHERE kind='contribution' AND jurisdiction<>''")
    c["statements"], c["mentions"] = k("statement"), k("mention")
    c["schools"], c["school_stats"] = k("school"), k("school_stat")
    series = Counter()
    pp = 0
    try:
        for (m,) in db.execute("SELECT meta FROM records WHERE kind='school_stat'"):
            try:
                d = json.loads(m or "{}")
            except ValueError:
                continue
            series[d.get("series") or ""] += 1
            pp += 1 if d.get("per_pupil") not in (None, "") else 0
    except Exception:  # noqa: BLE001
        pass
    c["milestones"] = series["eog_ela"] + series["eog_math"] + series["eoc_all"]
    c["spending_per_student"] = pp
    c["ccrpi"] = series["ccrpi"]
    c["district_sites_read"] = _one(db, "SELECT count(DISTINCT source_id) FROM records WHERE source_id LIKE 'sd-%:site' AND kind='page'")
    c["school_pages"] = _one(db, "SELECT count(*) FROM records WHERE source_id='schools:sites'")
    roles = Counter(p.get("role") for p in (load_json(root / "state" / "school_people.json").get("people") or []))
    c["board_members"] = roles["Member"] + roles["Chair"] + roles["Vice Chair"]
    c["superintendents"], c["principals"] = roles["Superintendent"], roles["Principal"]
    c["budget_docs"] = k("budget_doc")
    police = load_json(root / "state" / "police.json")
    found = [d for d in police.values() if isinstance(d, dict) and d.get("found")]
    c["departments"], c["chiefs"] = len(found), sum(1 for d in found if d.get("chief"))
    c["incidents"], c["incident_departments"] = k("police_incident"), places("police_incident")
    prog = load_json(root / "state" / "progress.json")
    st = Counter()
    for p in prog.values():
        s = (p or {}).get("status") or ""
        if p.get("last_ok"):
            st["ok"] += 1
        if s.startswith("waiting"):
            st["waiting"] += 1
        elif s.startswith(("blocked", "stopped")):
            st["blocked"] += 1
        elif s.startswith("error"):
            st["error"] += 1
            if "OutOfTime" in s or "past the deadline" in s:
                st["timeouts"] += 1
    c["sources_ok"], c["sources_waiting"] = st["ok"], st["waiting"]
    c["sources_error"], c["sources_blocked"], c["probe_timeouts"] = st["error"], st["blocked"], st["timeouts"]
    return c


def compare(now: dict[str, int], before: dict[str, int]) -> list[dict]:
    rows = []
    for section, key, label, check in FEATURES:
        v, b = now.get(key, 0), before.get(key)
        flag = ""
        if check == "need" and v == 0:
            flag = "zero"
        elif check == "need" and b and v < b * SHRINK:
            flag = "shrank"
        elif key == "probe_timeouts" and v:
            flag = "check"
        rows.append({"section": section, "key": key, "label": label, "now": v, "before": b, "flag": flag})
    return rows


PURGED = "a drop right after a config/purge.yaml entry is expected (duplicates or another place's records removed)"
NOTES = {
    "agenda_items": PURGED, "officials": PURGED, "ordinances": PURGED, "meetings": PURGED,
    "roll_calls": "legis update reads roll calls for landmark and passed bills within its time budget",
    "district_sites_read": "school district sites are read by day (schedule.daytime); board, superintendent and principal pages come from them",
    "board_members": "found on district sites' board pages (needs district sites read first)",
    "superintendents": "found on district sites (needs district sites read first)",
    "principals": "found on school sites listed on district pages (needs district sites read first)",
    "budget_docs": "gacrawl schools budgets: GaDOE finance pages (rendered) and the files they link",
    "milestones": "GOSA EOG/EOC files (gacrawl schools stats)",
    "ccrpi": "GaDOE's CCRPI page (rendered); its score files",
    "incidents": "police news: GDELT finds, trusted outlets confirm; Georgia stories that name the department",
    "contributions_local": "local candidates file with county or city offices; few publish data the crawler can read",
    "official_places": "officials come from Legistar and similar portals; most towns list theirs only on web pages",
    "video_transcripts": "official captions where the channel has them; YouTube captions are not read",
}


def report(rows: list[dict], when: str, before_when: str | None) -> str:
    zero = [r for r in rows if r["flag"] == "zero"]
    shrank = [r for r in rows if r["flag"] == "shrank"]
    lines = [f"# Data coverage ({when[:16].replace('T', ' ')} UTC)", "",
             f"What the database and state files hold for each Electo feature, compared with the last run"
             f"{' (' + before_when[:16].replace('T', ' ') + ' UTC)' if before_when else ''}. "
             f"**{len(zero)}** features are at zero and **{len(shrank)}** shrank by more than a tenth.", ""]
    if zero or shrank:
        lines += ["## Needs attention", ""]
        for r in zero + shrank:
            what = "still at zero" if r["flag"] == "zero" else f"shrank from {r['before']:,} to {r['now']:,}"
            lines.append(f"- **{r['label']}** ({r['section']}): {what}" + (f". {NOTES[r['key']]}" if r["key"] in NOTES else ""))
        lines.append("")
    section = None
    for r in rows:
        if r["section"] != section:
            section = r["section"]
            lines += ["", f"## {section}", "", "| Feature | Now | Last run | Change |", "|---|---:|---:|---|"]
        b = r["before"]
        delta = "" if b is None else ("same" if b == r["now"] else f"{r['now'] - b:+,}")
        mark = " ⚠" if r["flag"] else ""
        lines.append(f"| {r['label']}{mark} | {r['now']:,} | {'' if b is None else f'{b:,}'} | {delta} |")
    lines.append("")
    return "\n".join(lines)


def run(root: Path, store, state_path: Path | None = None, out_path: Path | None = None) -> dict:
    state_path = state_path or root / "state" / "coverage.json"
    out_path = out_path or root / "data" / "coverage.md"
    prev = load_json(state_path)
    now = measure(root, store)
    when = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows = compare(now, prev.get("counts") or {})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report(rows, when, prev.get("as_of")), encoding="utf-8")
    hist = (prev.get("history") or [])[-47:] + [{"as_of": when, **{k: now[k] for k in ("roll_calls", "board_members", "incidents",
                                                                                          "milestones", "budget_docs", "district_sites_read")}}]
    write_json_atomic(state_path, {"as_of": when, "counts": now, "history": hist}, indent=1)
    return {"zero": [r["key"] for r in rows if r["flag"] == "zero"], "shrank": [r["key"] for r in rows if r["flag"] == "shrank"],
            "as_of": when}
