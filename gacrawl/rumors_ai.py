"""The open model's reading of the Rumor Mill (data/rumor_mill/rumors.jsonl): what each unverified item actually says
about the person it was filed under, so the page's "Rumor Has It" tab can rate how likely it is to be true
(Electo ga/crawl_import.py rumors_from turns these readings and the corroboration it finds into the rating).

For each item with the outlet's text (kept when the crawler opened the page; never published), the model answers four
questions, as JSON:
  named        is the person named in the text at all (an item can be filed under someone it never mentions)
  quote        the one sentence of the text that says the most about them, copied exactly
  attribution  what the claim about them rests on: "official record" (a filing, vote, court record, police report),
               "named source" (a person speaking on the record), "anonymous" (unnamed sources, "people familiar"),
               or "none" (the writer's own assertion or opinion)
  opinion      whether the piece is opinion or commentary rather than reporting
The answer is kept only when its quote is copied exactly from the text and names the person; otherwise the item is
read again when its text changes (the same question is not put to the same model twice: the training log remembers). Saved in the row as "ai": {"v", "m", "named", "quote",
"attribution", "opinion", "at"}. A row already confirmed or debunked by hand is never read.

    gacrawl rumors-ai --max-minutes 10

Every call goes to data/train/rumor_log.jsonl (input, output, verdict) for training a model of our own later.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

VERSION = 1
ATTR = ("official record", "named source", "anonymous", "none")

SYSTEM = ("You read news and web articles for a civic information site and answer questions about them literally. You "
          "answer only from the article. You copy quotes exactly, character for character. You answer in JSON only.")
ASK = """Person: {name}{office}

Article ({domain}):
{text}

Answer in JSON with these keys:
"named": true if the article names this person, else false.
"quote": if named, the one sentence from the article that says the most about this person, copied exactly; else "".
"attribution": what the article's claim about this person rests on: "official record" (a filing, vote, court or police
record), "named source" (a person quoted by name), "anonymous" (unnamed sources), or "none" (the writer's own assertion).
"opinion": true if the article is opinion or commentary rather than news reporting, else false."""


def _json(out: str) -> dict | None:
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def verdict(ans: dict | None, name: str, text: str) -> tuple[dict | None, list[str]]:
    """The model's answer checked against the text: (reading to keep, reasons it was not kept)."""
    if not ans:
        return None, ["no JSON answer"]
    named = bool(ans.get("named"))
    quote = _norm(str(ans.get("quote") or ""))
    attr = str(ans.get("attribution") or "").lower().strip()
    why = []
    if attr not in ATTR:
        why.append(f"attribution {attr!r} is not one of the four")
    last = (name.split() or [""])[-1].lower()
    if named:
        if not quote or quote.lower() not in _norm(text).lower():
            why.append("the quote is not copied from the text")
        elif len(last) > 2 and last not in quote.lower():
            why.append("the quote does not name the person")
    else:
        if len(last) > 2 and re.search(r"\b" + re.escape(last) + r"\b", text, re.I) and \
                re.search(r"\b" + re.escape(name.split()[0]) + r"\b", text, re.I):
            why.append("the model said not named, but the text has the name")
        quote = ""
    if why:
        return None, why
    return {"named": named, "quote": quote[:400], "attribution": attr, "opinion": bool(ans.get("opinion"))}, []


def _sig(msgs: list) -> str:
    import hashlib
    return hashlib.sha1(json.dumps(msgs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _failed_before(log_path: Path, model: str) -> set[str]:
    """The questions (by signature) this model already answered with something the check refused (the training log)."""
    out: set[str] = set()
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("task") == "rumor_reading" and r.get("model") == model and isinstance(r.get("input"), list):
                    (out.discard if r.get("kept") else out.add)(_sig(r["input"]))
    except OSError:
        pass
    return out


TEXT_MAX = 3000          # what the reading sees of a page (run() reads at most this much)
TEXT_RETRY_DAYS = 30     # a page that could not be read is asked for again after this long


def excerpt(text: str, name: str, limit: int = TEXT_MAX) -> str:
    """The part of a page the model reads: every paragraph that names the person (by last name), each with the one
    before it for context, in page order, up to `limit` characters; a page that never names them keeps its opening
    (so the reading can say so). A long article used to be cut at its first 3,000 characters, before the person came up."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    last = (name.split() or [""])[-1]
    if len(last) <= 2 or not paras:
        return (text or "")[:limit]
    rx = re.compile(r"\b" + re.escape(last) + r"\b", re.I)
    hits = [i for i, p in enumerate(paras) if rx.search(p)]
    if not hits:
        return " ".join(paras)[:limit]
    keep = {j for i in hits for j in (i - 1, i, i + 1) if 0 <= j < len(paras)}
    # a name in a caption or a byline-like line ("Mayor / Andre Dickens") says nothing on its own: short excerpts are
    # widened with the paragraphs that follow the first mention, then those before it
    order = list(range(hits[0], len(paras))) + list(range(hits[0] - 1, -1, -1))
    for j in order:
        if sum(len(paras[i]) + 1 for i in keep) >= 1200:
            break
        keep.add(j)
    keep = sorted(keep)
    out, size = [], 0
    for i in keep:
        p = paras[i]
        if size + len(p) + 1 > limit:
            if i in hits and size < limit - 200:      # a naming paragraph is cut rather than left out
                out.append(p[:limit - size - 1])
            break
        out.append(p)
        size += len(p) + 1
    return "\n".join(out)


def fetch_texts(fetcher, root: Path, candidates: list[dict], deadline: float, log=print, limit: int | None = None) -> dict:
    """Open each unverified Rumor Mill item's page once (the crawler's own fetcher: robots.txt, spacing, back-off, the
    block list) and keep the part that names the person (excerpt) in its row's meta.text for run() to read. Items
    found before Nov 9 had only their headline; new ones keep the text research opened. A page that cannot be read
    (refused, gone, not HTML) is noted (meta.text_error) and asked for again after TEXT_RETRY_DAYS. The text is never
    published (export drops meta.text)."""
    from . import extract
    from .candidates import RumorMill
    from .fetch import Blocked, Disallowed, OutOfTime

    by_id = {c["id"]: c for c in candidates}
    rm = RumorMill(root)
    cutoff = (dt.date.today() - dt.timedelta(days=TEXT_RETRY_DAYS)).isoformat()
    todo = [r for r in rm.all() if r.get("status", "unverified") == "unverified" and r.get("url", "").startswith("http")
            and len((r.get("meta") or {}).get("text") or "") <= 200 and (r.get("meta") or {}).get("text_tried", "") < cutoff]
    todo.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    if limit:
        todo = todo[:limit]
    st = {"todo": len(todo), "read": 0, "kept": 0, "failed": 0}
    for r in todo:
        if time.monotonic() > deadline:
            break
        m = r.setdefault("meta", {})
        c = by_id.get(r.get("candidate_id")) or {}
        name = c.get("name") or r.get("candidate_id", "").replace("-", " ").title()
        try:
            res = fetcher.get(r["url"])
            st["read"] += 1
            if res.status != 200 or "html" not in (res.content_type or ""):
                raise ValueError(f"HTTP {res.status} {res.content_type or ''}".strip())
            text = extract.main_text(res.text)
            if len(text) < 200:
                raise ValueError("no article text on the page")
        except OutOfTime:
            break                     # not tried: it stays first in line next run
        except (Blocked, Disallowed, ValueError, Exception) as e:  # noqa: BLE001
            m["text_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            m["text_tried"] = dt.date.today().isoformat()
            rm._dirty = True
            st["failed"] += 1
            log(f"  rumor page {r['id']}: not read ({m['text_error']}): {r['url'][:100]}")
            continue
        m["text"] = excerpt(text, name)
        m["text_url"] = res.url
        m["text_tried"] = dt.date.today().isoformat()
        m.pop("text_error", None)
        rm._dirty = True
        st["kept"] += 1
        log(f"  rumor page {r['id']}: {len(m['text']):,} characters kept: {r['url'][:100]}")
    rm.flush()
    st["left"] = st["todo"] - st["kept"] - st["failed"]
    log(f"rumor pages: {st['kept']} read and kept, {st['failed']} could not be read, {st['left']} left for the next run")
    return st


def run(root: Path, candidates: list[dict], deadline: float, log=print) -> dict:
    from . import llm
    from .candidates import RumorMill

    by_id = {c["id"]: c for c in candidates}
    rm = RumorMill(root)
    rows = rm.all()
    todo = [r for r in rows if (r.get("meta") or {}).get("text") and r.get("status", "unverified") == "unverified"
            and (r.get("ai") or {}).get("v") != VERSION and len(r["meta"]["text"]) > 200]
    todo.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    log_path = root / "data" / "train" / "rumor_log.jsonl"
    failed = _failed_before(log_path, llm.model_name())
    st = {"todo": len(todo), "read": 0, "kept": 0, "same": 0}
    for r in todo:
        if time.monotonic() > deadline:
            break
        c = by_id.get(r.get("candidate_id")) or {}
        name = c.get("name") or r.get("candidate_id", "").replace("-", " ").title()
        text = r["meta"]["text"][:3000]
        office = f" ({c['office'].split(';')[0]})" if c.get("office") else ""
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": ASK.format(name=name, office=office, domain=r.get("domain", ""), text=text)}]
        if _sig(msgs) in failed:
            # this exact question got an answer that failed the check: the model is greedy and would give it again, so
            # the item waits until its text changes instead of taking the newest items' place on every run
            st["same"] += 1
            continue
        t0 = time.time()
        try:
            out = llm.chat(msgs, max_tokens=220)
        except Exception as e:  # noqa: BLE001
            log(f"rumors ai: {type(e).__name__}: {str(e)[:200]}")
            break
        keep, why = verdict(_json(out), name, text)
        st["read"] += 1
        if keep:
            r["ai"] = {"v": VERSION, "m": llm.model_name(), "at": dt.date.today().isoformat(), **keep}
            rm._dirty = True
            st["kept"] += 1
        llm.log(str(log_path), {"task": "rumor_reading", "rumor": r["id"], "person": name, "input": msgs, "output": out,
                                "kept": bool(keep), "why": why, "seconds": round(time.time() - t0, 1)})
        log(f"  {'kept' if keep else 'dropped'} ({time.time() - t0:.0f}s): {r['id']} {name}: {r.get('claim', '')[:60]}"
            + (f"  [{'; '.join(why[:2])}]" if why else ""))
    rm.flush()
    st["left"] = st["todo"] - st["read"] - st["same"]
    log(f"rumors ai: {st['read']} items read, {st['kept']} readings kept; {st['left']} left for the next run"
        + (f" ({st['same']} not asked again: the same text failed the check before)" if st["same"] else ""))
    return st
