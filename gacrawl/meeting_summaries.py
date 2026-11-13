"""Meeting summaries: what each meeting took up and decided, from its own minutes or its recording's captions.

Two readings by the open model (gacrawl/llm.py), both made the same checked way (gacrawl/video_ai.py analyze): the
model lists motions, votes and agenda items as sentences copied from the text; a line is kept only when it is in the
text word for word; then it writes two or three plain sentences from the kept lines alone, and those are kept only
when the checker finds nothing in them that the lines do not say.

  recordings   already read by `gacrawl video-ai`: each transcript record's meta.ai
  minutes      read here: every opened minutes file (documents with doc_type "minutes": cities, counties, school
               boards, and the state boards' minutes gacrawl agencies opens), newest first
  hearings     read here: the General Assembly's committee hearings' captions (transcripts of source
               ga:legis-hearing, gacrawl/legis_video.py)
               Each reading is a record of kind `meeting_summary` (key <source>|meeting_summary|<url>)

Working the backlog down (Nov 12: thousands of minutes, a model that writes a few hundred words a minute on a CPU):
  - minutes whose motions and votes the crawler's own parser already read (gacrawl/minutes.py: agenda_item records
    "from": "minutes") need no listing pass: the model writes the summary from the parser's lines in one short call
    instead of five, and those lines reach the site at once, before any model has run (export_summaries);
  - these go first, newest first; a quarter of each run is kept for minutes the parser read nothing from and for hearings;
  - the readings run in a job of their own (meeting-summaries.yml: about five hours, four times a day, beside the
    open model's other work) and travel to the crawl in its own hand-off, meetings-handoff.json.

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
HEARING_PARTS = 6          # of a hearing's captions, spread evenly over its whole length
HEARINGS = "ga:legis-hearing"
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
HEAR_SYSTEM = ("You read transcripts of Georgia General Assembly committee hearings and list what happened, literally. "
               "You copy sentences exactly as they appear in the transcript. You answer with JSON lines only.")
ROUTINE = re.compile(r"\b(call(?:ed)? to order|pledge of allegiance|invocation|adjourn|approv\w+ (?:of )?the (?:agenda|minutes))\b", re.I)


def _meta(r: dict) -> dict:
    m = r.get("meta")
    return json.loads(m) if isinstance(m, str) else dict(m or {})


def parsed_lines(store) -> dict[str, list[dict]]:
    """minutes file -> the motions the crawler's parser read from it (gacrawl/minutes.py), as lines: a motion with its
    result is a vote, one without is a motion; procedural motions (the agenda, the minutes, adjournment) are left out."""
    by: dict[str, list[dict]] = {}
    for r in store.iter_query("kind='agenda_item' AND channel='derived' AND meta LIKE '%\"from\": \"minutes\"%'"):
        m = _meta(r)
        what = re.sub(r"\s+", " ", r.get("title") or "").strip()
        if m.get("procedural") or len(what) < 12 or what == "Motion":
            continue
        res = " ".join(x for x in (m.get("result") or "", m.get("tally") or "") if x)
        by.setdefault(r["url"], []).append({"type": "vote" if res else "motion", "who": m.get("mover") or "",
                                            "text": what + (f" ({res})" if res else "")})
    return by


def _spread(text: str, n: int, size: int = 6000) -> tuple[str, float]:
    """Up to n parts of `size` characters spread evenly over the text, joined; and the share of the text they cover."""
    if len(text) <= n * size:
        return text, 1.0
    step = (len(text) - size) / (n - 1)
    return "".join(text[int(i * step):int(i * step) + size] for i in range(n)), round(n * size / len(text), 2)


def in_slice(url: str, shard: tuple[int, int] | None) -> bool:
    """Whether a meeting belongs to shard i of n: by a fixed hash of its file's or video's address, so every runner
    of a sharded job reads its own meetings and no two read the same one."""
    if not shard:
        return True
    import zlib

    return zlib.crc32((url or "").encode()) % shard[1] == shard[0]


def minutes_run(store, root: Path, deadline: float, llm=None, log=print, per_run: int = 100000,
                shard: tuple[int, int] | None = None) -> dict:
    """Read minutes and hearings not read yet until the time is up: first the minutes the parser already drew lines
    from (one short call each), newest first; then, in the last quarter of the time, the rest and the hearings (the
    full reading). One record a reading; a file nothing checkable came from is kept as an empty reading and not read
    again until VERSION changes."""
    from .store import Record
    from .video_ai import ASK as VIDEO_ASK, SUM_ASK as VIDEO_SUM, analyze

    if llm is None:
        from . import llm as _llm
        llm = _llm
    log_path = str(Path(root) / "data" / "train" / "minutes_log.jsonl")
    st = {"waiting": 0, "from_parsed_lines": 0, "full_readings": 0, "hearings": 0, "summaries": 0, "nothing": 0, "left": 0}
    have = {_meta(r).get("of_url") for r in store.iter_query("kind='meeting_summary'") if _meta(r).get("v") == VERSION}
    parsed = parsed_lines(store)
    docs = [r for r in store.iter_query("kind='document' AND meta LIKE '%\"doc_type\": \"minutes\"%'")
            if in_slice(r["url"], shard) and r["url"] not in have and len(r.get("text") or "") >= MIN_CHARS and (r.get("evidence") or "primary") == "primary"]
    hear = [r for r in store.iter_query("kind='transcript' AND source_id=?", (HEARINGS,))
            if in_slice(r["url"], shard) and r["url"] not in have and len(r.get("text") or "") >= MIN_CHARS]
    day = lambda r: (r.get("published") or "")[:10]
    cheap = sorted([r for r in docs if len(parsed.get(r["url"]) or []) >= 2], key=day, reverse=True)
    rest = sorted([r for r in docs if len(parsed.get(r["url"]) or []) < 2] + hear, key=day, reverse=True)
    st["waiting"] = len(cheap) + len(rest)
    t0 = time.monotonic()
    cheap_until = t0 + (deadline - t0) * (0.75 if rest else 1.0)
    done = 0

    def keep(r, of, a, items, by):
        title = re.sub(r"\s+", " ", r.get("title") or "")[:140] or ("Hearing" if of == "hearing" else "Minutes")
        m = _meta(r)
        store.add(Record(r["source_id"], "meeting_summary", r["url"] + "#summary", title=title, text=(a or {}).get("summary") or "",
                         published=r.get("published"), channel="store", jurisdiction=r.get("jurisdiction") or "",
                         evidence="primary", key=f"{r['source_id']}|meeting_summary|{r['url']}",
                         meta={"of": of, "of_url": r["url"], "v": VERSION, "m": (a or {}).get("m") or llm.model_name(),
                               "at": (a or {}).get("at") or dt.date.today().isoformat(), "items": items[:40], "by": by,
                               "summary": (a or {}).get("summary") or "", "doc_title": title, "empty": not items,
                               **({"cover": a["cover"]} if a and a.get("cover", 1.0) < 1.0 else {}),
                               **({"committee": m.get("committee")} if m.get("committee") else {})}))
        st["summaries"] += bool((a or {}).get("summary"))
        st["nothing"] += not items
        store.flush()             # kept at once: a runner stopped by its time limit loses nothing it read

    try:
        for r in cheap:
            if time.monotonic() > cheap_until or done >= per_run:
                break
            lines = parsed[r["url"]][:30]
            title = re.sub(r"\s+", " ", r.get("title") or "")[:140] or "Minutes"
            shown = "\n".join(f"- {it['type']}: {it['text']}" for it in lines)
            msgs = [{"role": "system", "content": SYSTEM.replace("You answer with JSON lines only.", "")},
                    {"role": "user", "content": SUM_ASK.format(title=title, date=day(r), lines=shown)}]
            out = llm.chat(msgs, max_tokens=160)
            summary = " ".join(out.split())
            fails = llm.check(summary, shown + " " + title, max_words=80, allow=(title,))
            llm.log(log_path, {"task": "minutes_summary", "title": title, "input": msgs, "output": out, "kept": not fails, "why": fails})
            keep(r, "minutes", {"summary": "" if fails else summary}, lines, "parser")
            st["from_parsed_lines"] += 1
            done += 1
        per_part = 100.0          # seconds a part of a full reading takes; learned from this run's own readings
        for r in rest:
            if time.monotonic() > deadline or done >= per_run:
                break
            hearing = r["source_id"] == HEARINGS
            parts = HEARING_PARTS if hearing else min(MAX_PARTS, -(-len(r["text"]) // 6000))
            if deadline - time.monotonic() < parts * per_part:
                continue          # not started when it could not finish: a shorter one may still fit
            t1 = time.monotonic()
            title = re.sub(r"\s+", " ", r.get("title") or "")[:140] or ("Hearing" if hearing else "Minutes")
            if hearing:
                text, cover = _spread(r["text"], HEARING_PARTS)
                a = analyze(text, title, day(r), llm, log_path, ask=VIDEO_ASK, system=HEAR_SYSTEM, sum_ask=VIDEO_SUM,
                            task="hearing", max_parts=HEARING_PARTS)
            else:
                cover = min(1.0, round(MAX_PARTS * 6000 / max(1, len(r["text"])), 2))
                a = analyze(r["text"], title, day(r), llm, log_path, ask=ASK, system=SYSTEM, sum_ask=SUM_ASK, task="minutes",
                            max_parts=MAX_PARTS)
            items = [it for it in (a or {}).get("items", []) if it["type"] != "statement"]
            keep(r, "hearing" if hearing else "minutes", {**(a or {}), "cover": cover}, items, "model")
            per_part = 0.5 * per_part + 0.5 * (time.monotonic() - t1) / max(1, parts)
            st["hearings" if hearing else "full_readings"] += 1
            done += 1
    except Exception as e:  # noqa: BLE001 - the model failing ends this run's readings, not the job
        log(f"minutes ai: the model failed: {type(e).__name__}: {str(e)[:150]}")
    except KeyboardInterrupt:       # stopped (the step's time limit, a cancelled run): what was read is kept
        log("minutes ai: stopped; keeping what was read")
    st["left"] = st["waiting"] - done
    st["seconds_each"] = round((time.monotonic() - t0) / done, 1) if done else None
    if shard:
        st["shard"] = f"{shard[0]}/{shard[1]}"
    store.flush()
    log(f"minutes ai: {st}")
    return st


def _lines(items: list[dict], cap: int = 10) -> list[list[str]]:
    """The lines worth showing: votes and motions first, routine business (the pledge, adjournment) left out."""
    keep = [it for it in items if it.get("type") in ("vote", "motion", "agenda") and not ROUTINE.search(it.get("text") or "")]
    keep.sort(key=lambda it: {"vote": 0, "motion": 1, "agenda": 2}[it["type"]])
    return [[it["type"], it.get("who") or "", it["text"][:300]] for it in keep[:cap]]


def export_summaries(store, path: Path) -> dict:
    """meeting_summaries.json: {"as_of", "note", "summaries": [{j, k, d, t, u, s, i, by, m, at, cov?}]}: j the
    jurisdiction id, k "minutes", "recording" or "hearing", d the day, t the title, u the minutes file or the video's
    page, s the summary ("" when none passed the check or none is written yet), i the lines [[type, who, text]], by
    who drew the lines ("model": copied by the model and checked word for word; "parser": the crawler's own reading
    of the minutes' motions), m the model, at the day it was read, cov the share of a long text that was read.
    Minutes the parser has read but the model has not reached yet are here too, with their lines and no summary."""
    out, seen = [], set()
    for r in store.iter_query("kind='meeting_summary'"):
        m = _meta(r)
        lines = _lines(m.get("items") or [])
        if not (m.get("summary") or lines) or not m.get("of_url"):
            seen.add(m.get("of_url"))
            continue
        seen.add(m["of_url"])
        out.append({"j": r.get("jurisdiction") or "", "k": m.get("of") or "minutes", "d": (r.get("published") or "")[:10],
                    "t": m.get("doc_title") or r.get("title") or "", "u": m["of_url"], "s": m.get("summary") or "", "i": lines,
                    "by": m.get("by") or "model", "m": m.get("m") or "", "at": m.get("at") or "",
                    **({"cov": m["cover"]} if m.get("cover") else {}), **({"c": m["committee"]} if m.get("committee") else {})})
    parsed = parsed_lines(store)
    if parsed:
        for r in store.iter_query("kind='document' AND meta LIKE '%\"doc_type\": \"minutes\"%'"):
            lines = _lines(parsed.get(r["url"]) or [])
            if r["url"] in seen or len(lines) < 2:
                continue
            out.append({"j": r.get("jurisdiction") or "", "k": "minutes", "d": (r.get("published") or "")[:10],
                        "t": re.sub(r"\s+", " ", r.get("title") or "")[:140], "u": r["url"], "s": "", "i": lines, "by": "parser",
                        "m": "", "at": ""})
    for r in store.iter_query("kind='transcript' AND meta LIKE '%\"ai\"%'"):
        m = _meta(r)
        a = m.get("ai") or {}
        lines = _lines(a.get("items") or [])
        if not (a.get("summary") or lines):
            continue
        out.append({"j": r.get("jurisdiction") or "", "k": "recording", "d": (r.get("published") or "")[:10],
                    "t": (r.get("title") or "").replace(" — transcript", ""), "u": m.get("video_page") or r["url"],
                    "s": a.get("summary") or "", "i": lines, "by": "model", "m": a.get("m") or "", "at": a.get("at") or ""})
    out.sort(key=lambda x: x["d"], reverse=True)
    Path(path).write_text(json.dumps({
        "as_of": dt.date.today().isoformat(),
        "note": "What each meeting took up and decided, from the meeting's own minutes or its recording's captions. Lines "
                "marked by the model are copied word for word from that source and were checked against it; lines marked by "
                "the parser are the motions the crawler read from the minutes. A summary's sentences were written by an open "
                "AI model from those lines alone. The minutes or the recording is the record.",
        "summaries": out}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"summaries": len(out), "from_minutes": sum(1 for x in out if x["k"] == "minutes"),
            "from_recordings": sum(1 for x in out if x["k"] == "recording"), "from_hearings": sum(1 for x in out if x["k"] == "hearing"),
            "with_summary": sum(1 for x in out if x["s"]), "lines_by_parser": sum(1 for x in out if x["by"] == "parser")}
