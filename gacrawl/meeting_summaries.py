"""Meeting summaries: what each meeting took up and decided, from its own minutes or its recording's captions.

Two readings by the open model (gacrawl/llm.py), both made the same checked way (gacrawl/video_ai.py analyze): the
model lists motions, votes and agenda items as sentences copied from the text; a line is kept only when it is in the
text word for word; then it writes two or three plain sentences from the kept lines alone, and those are kept only
when the checker finds nothing in them that the lines do not say.

  recordings   already read by `gacrawl video-ai`: each transcript record's meta.ai
  minutes      read here: every opened minutes file (documents with doc_type "minutes"), newest first, a few a night.
               Each reading is a record of kind `meeting_summary` beside its document (key <source>|meeting_summary|<url>)

export_summaries() writes the bundle's meeting_summaries.json for Electo's Committee Meetings tab. Statements (who
said where they stand) are never in it: captions do not label speakers, and the site attributes nothing unreviewed.

    gacrawl minutes-ai --max-minutes 10
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

VERSION = 1
MIN_CHARS = 600            # shorter than this is a cover sheet or a scan with nothing read yet
MAX_PARTS = 4              # of 6,000 characters: the first 24,000 of the minutes
SYSTEM = ("You read the written minutes of Georgia public meetings (city councils, county commissions, school boards, "
          "state boards) and list what happened, literally. You copy sentences exactly as they appear in the minutes. "
          "You answer with JSON lines only.")
ASK = """Minutes: {title} ({date})

Minutes, part {i} of {n}:
{text}

List up to 8 things this part records, one JSON object per line:
{{"type": "motion" | "vote" | "agenda", "who": "the person who moved it, as the minutes name them, or empty", "text": "one sentence or phrase copied exactly from the minutes"}}
- motion: a motion, a second, or an item being adopted, approved, denied or tabled
- vote: a vote taken or its result
- agenda: an item taken up without a vote (a report, a presentation, a public hearing)
Only what the minutes say; nothing else. Skip the call to order, the pledge, the roll and adjournment."""
SUM_ASK = """Minutes: {title} ({date})

What the minutes record (each line copied from them):
{lines}

Write two or three plain sentences saying what this meeting took up and decided, using only the lines above. No
judging words. Do not name anyone unless a line names them."""
ROUTINE = re.compile(r"\b(call(?:ed)? to order|pledge of allegiance|invocation|adjourn|approv\w+ (?:of )?the (?:agenda|minutes))\b", re.I)


def _meta(r: dict) -> dict:
    m = r.get("meta")
    return json.loads(m) if isinstance(m, str) else dict(m or {})


def minutes_run(store, root: Path, deadline: float, llm=None, log=print, per_run: int = 60) -> dict:
    """Read minutes files not read yet, newest first, until the time is up. One record a reading; a file the model
    drew nothing checkable from is remembered (kv) and not read again until VERSION changes."""
    from .store import Record
    from .video_ai import analyze

    if llm is None:
        from . import llm as _llm
        llm = _llm
    log_path = str(Path(root) / "data" / "train" / "minutes_log.jsonl")
    st = {"minutes": 0, "read": 0, "summaries": 0, "nothing": 0, "left": 0}
    have = {_meta(r).get("of_url") for r in store.iter_query("kind='meeting_summary'")}
    empty = set(json.loads(store.kv_get(f"minutes_ai:empty:{VERSION}") or "[]"))
    rows = [r for r in store.iter_query("kind='document' AND meta LIKE '%\"doc_type\": \"minutes\"%'")
            if r["url"] not in have and r["url"] not in empty and len(r.get("text") or "") >= MIN_CHARS
            and (r.get("evidence") or "primary") == "primary"]
    rows.sort(key=lambda r: (r.get("published") or "")[:10], reverse=True)
    st["minutes"] = len(rows)
    for n, r in enumerate(rows):
        if time.monotonic() > deadline or st["read"] >= per_run:
            st["left"] = len(rows) - n
            break
        title = re.sub(r"\s+", " ", r.get("title") or "")[:140] or "Minutes"
        day = (r.get("published") or "")[:10]
        try:
            a = analyze(r["text"], title, day, llm, log_path, ask=ASK, system=SYSTEM, sum_ask=SUM_ASK, task="minutes",
                        max_parts=MAX_PARTS)
        except Exception as e:  # noqa: BLE001 - the model failing ends this run's readings, not the crawl
            log(f"minutes ai: the model failed: {type(e).__name__}: {str(e)[:150]}")
            st["left"] = len(rows) - n
            break
        st["read"] += 1
        items = [it for it in (a or {}).get("items", []) if it["type"] != "statement"]
        if not a or not items:
            empty.add(r["url"])
            st["nothing"] += 1
            continue
        store.add(Record(r["source_id"], "meeting_summary", r["url"] + "#summary", title=title, text=a.get("summary") or "",
                         published=r.get("published"), channel="store", jurisdiction=r.get("jurisdiction") or "",
                         evidence="primary", key=f"{r['source_id']}|meeting_summary|{r['url']}",
                         meta={"of": "minutes", "of_url": r["url"], "v": VERSION, "m": a["m"], "at": a["at"], "items": items[:40],
                               "summary": a.get("summary") or "", "doc_title": title}))
        st["summaries"] += bool(a.get("summary"))
        log(f"  minutes: {title[:70]} · {len(items)} items" + ("" if a.get("summary") else " · no summary passed the check"))
    store.kv_set(f"minutes_ai:empty:{VERSION}", json.dumps(sorted(empty)[-40000:]))
    store.flush()
    log(f"minutes ai: {st}")
    return st


def _lines(items: list[dict], cap: int = 10) -> list[list[str]]:
    """The lines worth showing: votes and motions first, routine business (the pledge, adjournment) left out."""
    keep = [it for it in items if it.get("type") in ("vote", "motion", "agenda") and not ROUTINE.search(it.get("text") or "")]
    keep.sort(key=lambda it: {"vote": 0, "motion": 1, "agenda": 2}[it["type"]])
    return [[it["type"], it.get("who") or "", it["text"][:300]] for it in keep[:cap]]


def export_summaries(store, path: Path) -> dict:
    """meeting_summaries.json: {"as_of", "note", "summaries": [{j, k, d, t, u, s, i, m, at}]}: j the jurisdiction id,
    k "minutes" or "recording", d the day, t the title, u the minutes file or the video's page, s the summary ("" when
    none passed the check), i the lines [[type, who, text]], m the model, at the day it was read. Newest first."""
    out = []
    for r in store.iter_query("kind='meeting_summary'"):
        m = _meta(r)
        lines = _lines(m.get("items") or [])
        if not (m.get("summary") or lines) or not m.get("of_url"):
            continue
        out.append({"j": r.get("jurisdiction") or "", "k": "minutes", "d": (r.get("published") or "")[:10], "t": m.get("doc_title") or r.get("title") or "",
                    "u": m["of_url"], "s": m.get("summary") or "", "i": lines, "m": m.get("m") or "", "at": m.get("at") or ""})
    for r in store.iter_query("kind='transcript' AND meta LIKE '%\"ai\"%'"):
        m = _meta(r)
        a = m.get("ai") or {}
        lines = _lines(a.get("items") or [])
        if not (a.get("summary") or lines):
            continue
        out.append({"j": r.get("jurisdiction") or "", "k": "recording", "d": (r.get("published") or "")[:10],
                    "t": (r.get("title") or "").replace(" — transcript", ""), "u": m.get("video_page") or r["url"],
                    "s": a.get("summary") or "", "i": lines, "m": a.get("m") or "", "at": a.get("at") or "",
                    "src": m.get("transcript_source") or ""})
    out.sort(key=lambda x: x["d"], reverse=True)
    Path(path).write_text(json.dumps({
        "as_of": dt.date.today().isoformat(),
        "note": "What each meeting took up and decided, written by an open AI model from the meeting's own minutes or its "
                "recording's captions. Every line under a summary is copied word for word from that source and was checked "
                "against it; the summary's sentences were written from those lines alone. The minutes or the recording is "
                "the record.",
        "summaries": out}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"summaries": len(out), "from_minutes": sum(1 for x in out if x["k"] == "minutes"),
            "from_recordings": sum(1 for x in out if x["k"] == "recording"), "with_summary": sum(1 for x in out if x["s"])}
