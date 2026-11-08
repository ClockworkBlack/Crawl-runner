"""Meeting videos read and analyzed: the captions of official meeting videos (YouTube channels the government links
to, or that the owner confirmed: config/channel_decisions.yaml) become transcripts, and the open model (gacrawl/llm.py,
Qwen3-8B) reads each transcript for what happened at the meeting.

Captions
  Read with yt-dlp (no key): the video's own captions, else YouTube's automatic English captions, as WebVTT. Only
  official channels' videos (records of kind `video`) are read. If YouTube asks the runner to sign in or prove it is
  not a bot, or refuses the request, the step stops for the day and records why: the crawler never works around a
  refusal (no cookies, no other addresses). Each transcript is a record of kind `transcript` (evidence primary,
  meta.segments = [[seconds, char offset], ...] so anything quoted carries its time in the video,
  meta.transcript_source "youtube captions" or "youtube automatic captions").
Analysis (the open model, in passes over ~6,000-character parts of the transcript)
  items    what the meeting did and who said what, one JSON line each: {"type": "motion" | "vote" | "statement" |
           "agenda", "who": name as spoken or "", "text": one sentence copied exactly}. A line is kept only when its
           sentence is in the transcript word for word (automatic captions have no punctuation, so the match ignores
           case and punctuation) and, when it names someone, the name is in that part of the transcript
  summary  two or three plain sentences from the kept lines, checked like the news summaries (llm.check)
Saved in the transcript record's meta.ai ({"v", "m", "at", "items", "summary"}). Speakers in automatic captions are not
labeled, so statements carry needs_review and are never attributed on Electo without review (stances.py rules).
Every model call goes to data/train/video_log.jsonl for the fine-tune.

    gacrawl video-ai --max-minutes 20 [--captions-only]
"""
from __future__ import annotations

import datetime as dt
import json
import re
import tempfile
import time
from pathlib import Path

VERSION = 1
PART = 6000
MAX_PARTS = 8
BLOCK_WORDS = re.compile(r"sign in to confirm|not a bot|captcha|http error 429|too many requests|http error 403|"
                         r"confirm your age|private video|members-only", re.I)
SYSTEM = ("You read transcripts of Georgia local government meetings (city councils, county commissions, school boards) and "
          "list what happened, literally. You copy sentences exactly as they appear in the transcript. You answer with JSON "
          "lines only.")
ASK = """Meeting: {title} ({date})

Transcript part {i} of {n} (automatic captions may lack punctuation):
{text}

List up to 8 things this part records, one JSON object per line:
{{"type": "motion" | "vote" | "statement" | "agenda", "who": "the person's name as the transcript says it, or empty", "text": "one sentence or phrase copied exactly from the transcript"}}
- motion: a motion, a second, or an item being adopted or tabled
- vote: a vote taken or its result
- statement: a member, official or speaker saying where they stand
- agenda: an agenda item taken up
Only what the transcript says; nothing else."""
SUM_ASK = """Meeting: {title} ({date})

What the transcript records (each line copied from it):
{lines}

Write two or three plain sentences saying what this meeting took up and decided, using only the lines above. No
judging words. Do not name anyone unless a line names them."""


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def _json_lines(out: str) -> list[dict]:
    got = []
    for line in (out or "").splitlines():
        line = line.strip().strip(",")
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                got.append(d)
    return got


def check_item(d: dict, part: str) -> tuple[dict | None, str]:
    typ = str(d.get("type") or "").lower()
    if typ not in ("motion", "vote", "statement", "agenda"):
        return None, f"type {typ!r}"
    text = " ".join(str(d.get("text") or "").split())
    if len(text) < 12:
        return None, "too short"
    if _flat(text) not in _flat(part):
        return None, "not copied from the transcript"
    who = " ".join(str(d.get("who") or "").split())[:80]
    if who and _flat(who.split()[-1]) not in _flat(part):
        who = ""        # a name the transcript part does not carry is dropped, the line kept
    return {"type": typ, "who": who, "text": text[:400], **({"needs_review": True} if typ == "statement" else {})}, ""


# ----------------------------------------------------------------------------- captions
def captions(url: str, workdir: Path) -> tuple[list[tuple[float, str]] | None, str, str]:
    """(cues, source, error). error 'blocked: ...' means stop for the day; 'unavailable' means no captions."""
    try:
        import yt_dlp
    except ImportError:
        return None, "", "yt-dlp is not installed"
    from .transcripts import parse_vtt

    opts = {"skip_download": True, "writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": ["en", "en-US", "en-orig"],
            "subtitlesformat": "vtt", "outtmpl": str(workdir / "%(id)s.%(ext)s"), "quiet": True, "no_warnings": True,
            "noplaylist": True, "socket_timeout": 30, "retries": 1, "extractor_retries": 0}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        return None, "", ("blocked: " + msg[:200]) if BLOCK_WORDS.search(msg) else ("unavailable: " + msg[:200])
    vid = (info or {}).get("id") or ""
    manual = set((info or {}).get("subtitles") or {})
    files = sorted(workdir.glob(f"{vid}*.vtt"))
    if not files:
        return None, "", "unavailable: no English captions"
    f = files[0]
    lang = f.name[len(vid) + 1:-4]
    cues = parse_vtt(f.read_text(encoding="utf-8", errors="replace"))
    # automatic captions repeat each line as it scrolls: keep a line once
    out, seen = [], []
    for t, txt in cues:
        if seen and (txt == seen[-1] or txt in seen[-3:]):
            continue
        out.append((t, txt))
        seen.append(txt)
    return out, ("youtube captions" if lang in manual else "youtube automatic captions"), ""


# ----------------------------------------------------------------------------- the model's reading
def analyze(text: str, title: str, date: str, llm, log_path: str) -> dict | None:
    parts = [text[i:i + PART] for i in range(0, len(text), PART)][:MAX_PARTS]
    items, seen = [], set()
    for i, part in enumerate(parts):
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": ASK.format(title=title, date=date, i=i + 1, n=len(parts), text=part)}]
        t0 = time.time()
        out = llm.chat(msgs, max_tokens=600)
        kept, why = [], []
        for d in _json_lines(out):
            it, w = check_item(d, part)
            if it and _flat(it["text"]) not in seen:
                seen.add(_flat(it["text"]))
                kept.append(it)
            elif w:
                why.append(w)
        items += kept
        llm.log(log_path, {"task": "video_items", "title": title, "part": i + 1, "input": msgs, "output": out,
                           "kept": len(kept), "dropped": why[:8], "seconds": round(time.time() - t0, 1)})
    if not items:
        return None
    lines = "\n".join(f"- {it['type']}: {it['text']}" for it in items[:30])
    msgs = [{"role": "system", "content": SYSTEM.replace("You answer with JSON lines only.", "")},
            {"role": "user", "content": SUM_ASK.format(title=title, date=date, lines=lines)}]
    out = llm.chat(msgs, max_tokens=160)
    summary = " ".join(out.split())
    fails = llm.check(summary, lines + " " + title, max_words=80, allow=(title,))
    llm.log(log_path, {"task": "video_summary", "title": title, "input": msgs, "output": out, "kept": not fails, "why": fails})
    return {"v": VERSION, "m": llm.model_name(), "at": dt.date.today().isoformat(), "items": items[:60],
            "summary": "" if fails else summary}


def run(store, root: Path, deadline: float, captions_only: bool = False, log=print, llm=None, per_run: int = 40) -> dict:
    from .transcripts import cues_to_record

    root = Path(root)
    log_path = str(root / "data" / "train" / "video_log.jsonl")
    today = dt.date.today().isoformat()
    st = {"videos": 0, "captioned": 0, "no_captions": 0, "analyzed": 0, "stopped": ""}
    if (store.kv_get("video_ai:blocked") or "")[:10] == today:
        st["stopped"] = "YouTube refused earlier today: " + (store.kv_get("video_ai:why") or "")
        log(f"video ai: {st['stopped']}")
        return st
    if llm is None and not captions_only:
        from . import llm as _llm
        llm = _llm
    done = set(json.loads(store.kv_get("video_ai:tried") or "[]"))
    rows = [r for r in store.iter_query("kind='video'") if r["url"] not in done and "youtube" in (r["url"] or "")]
    rows.sort(key=lambda r: r.get("published") or "", reverse=True)
    with tempfile.TemporaryDirectory() as tmp:
        for r in rows[:per_run]:
            if time.monotonic() > deadline:
                break
            st["videos"] += 1
            cues, src, err = captions(r["url"], Path(tmp))
            if err.startswith("blocked"):
                store.kv_set("video_ai:blocked", today)
                store.kv_set("video_ai:why", err[:300])
                st["stopped"] = err[:200]
                log(f"video ai: YouTube refused ({err[:120]}); stopping until tomorrow, nothing worked around")
                break
            if err.startswith("yt-dlp"):
                st["stopped"] = err
                log(f"video ai: {err}")
                break
            done.add(r["url"])
            if not cues:
                st["no_captions"] += 1
                continue
            meta = json.loads(r["meta"]) if isinstance(r.get("meta"), str) else (r.get("meta") or {})
            rec = cues_to_record(cues, r["source_id"], r["url"], (r.get("title") or "Meeting") + " — transcript",
                                 {"transcript_source": src, "video_page": r["url"], "video_id": meta.get("video_id", ""),
                                  "official_channel": meta.get("official_channel") or {}})
            if not rec:
                continue
            rec.jurisdiction, rec.published = r.get("jurisdiction") or "", r.get("published")
            rec.key = f"{r['source_id']}|transcript|{r['url']}"
            st["captioned"] += 1
            if not captions_only and time.monotonic() < deadline:
                try:
                    a = analyze(rec.text, r.get("title") or "", (r.get("published") or "")[:10], llm, log_path)
                except Exception as e:  # noqa: BLE001
                    log(f"video ai: the model failed: {type(e).__name__}: {str(e)[:150]}")
                    a = None
                    captions_only = True
                if a:
                    rec.meta["ai"] = a
                    st["analyzed"] += 1
            store.add(rec)
            log(f"  {src or 'no captions'}: {(r.get('title') or '')[:70]}" + (f" · {len(rec.meta.get('ai', {}).get('items', []))} items" if rec.meta.get("ai") else ""))
            time.sleep(2)     # polite: one video every few seconds
    # transcripts captioned before the model could read them (the model failed, or --captions-only): read them now
    if not captions_only and not st["stopped"]:
        for t in store.iter_query("kind='transcript' AND meta LIKE '%youtube%captions%'"):
            if time.monotonic() > deadline:
                break
            m = json.loads(t["meta"]) if isinstance(t.get("meta"), str) else dict(t.get("meta") or {})
            if (m.get("ai") or {}).get("v") == VERSION or not str(m.get("transcript_source", "")).startswith("youtube"):
                continue
            try:
                a = analyze(t["text"] or "", (t.get("title") or "").replace(" — transcript", ""), (t.get("published") or "")[:10], llm, log_path)
            except Exception as e:  # noqa: BLE001
                log(f"video ai: the model failed: {type(e).__name__}: {str(e)[:150]}")
                break
            m["ai"] = a or {"v": VERSION, "m": llm.model_name(), "at": today, "items": [], "summary": ""}
            from .store import Record

            store.add(Record(t["source_id"], "transcript", t["url"], title=t.get("title") or "", text=t.get("text") or "",
                             published=t.get("published"), channel=t.get("channel") or "html", meta=m,
                             jurisdiction=t.get("jurisdiction") or "", evidence=t.get("evidence") or "primary", key=t.get("key") or ""))
            st["analyzed"] += 1
    store.kv_set("video_ai:tried", json.dumps(sorted(done)[-20000:]))
    store.flush()
    log(f"video ai: {st}")
    return st
