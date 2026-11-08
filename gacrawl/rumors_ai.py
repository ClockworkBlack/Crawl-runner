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
read again next run (or after the text changes). Saved in the row as "ai": {"v", "m", "named", "quote",
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
    st = {"todo": len(todo), "read": 0, "kept": 0}
    for r in todo:
        if time.monotonic() > deadline:
            break
        c = by_id.get(r.get("candidate_id")) or {}
        name = c.get("name") or r.get("candidate_id", "").replace("-", " ").title()
        text = r["meta"]["text"][:3000]
        office = f" ({c['office'].split(';')[0]})" if c.get("office") else ""
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": ASK.format(name=name, office=office, domain=r.get("domain", ""), text=text)}]
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
    st["left"] = st["todo"] - st["read"]
    log(f"rumors ai: {st['read']} items read, {st['kept']} readings kept; {st['left']} left for the next run")
    return st
