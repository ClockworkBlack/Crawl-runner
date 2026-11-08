"""The open model's summaries for donor profiles: two neutral sentences on what an organization or committee that gave
money is, for the pop-up its name opens on the Donors tab (donor_profiles in gacrawl/donors.py).

Only organizations and committees, never a person. The model reads only what the crawler already opened and kept
for that organization (finance.py enrich: its FEC registration, IRS exempt-organization data, a hand note, and the
"about" page of its own website), and writes from that alone. The line is kept only when the shared checker passes
(llm.check: every number and nearly every word from those texts, no judging words), and the model's second reading
finds nothing unsupported; otherwise the profile shows the registry lines by themselves. Kept summaries go to the
store's kv "donor_about" ({entity id: {"t", "u": [opened source urls], "m", "v", "at"}}), which the donor export
reads. An organization is summarized again when its sources change or VERSION does.

    gacrawl donors-ai --max-minutes 20

Every call goes to data/train/donor_about_log.jsonl (input, output, verdict) for training a model of our own later.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

VERSION = 1
MIN_SOURCE = 60          # characters of source text below which there is nothing to summarize

SYSTEM = ("You write short, neutral descriptions of organizations that give money to Georgia candidates, for a civic "
          "information site. You use only facts stated in the texts you are given. You never add background, opinions, "
          "praise, criticism or adjectives of your own, and you never say whom the organization supports or opposes "
          "unless the texts say so.")
ASK = """Organization: {name}

{texts}

In at most two sentences and at most 45 words, say what this organization is: its type (as registered), whom it is
connected to or affiliated with, and what it says it does, using only the texts above. If the texts do not say what
it does, describe only its registration. Answer with the description only."""
VERIFY = """Texts:
{texts}

Description to check:
{line}

Does the description state anything the texts do not say: a wrong type, a wrong connection, a purpose or activity
the texts do not give? Answer with one word, OK, if every statement is supported, or NO followed by the unsupported
statement."""


def _sources(meta: dict) -> list[tuple[str, str]]:
    """(text, opened url) pairs the crawler kept for the organization."""
    out = []
    for w in meta.get("what") or []:
        if w.get("text") and w.get("src", "").startswith("http"):
            out.append((w["text"].strip()[:900], w["src"]))
    if meta.get("connected_org"):
        fec = next((s["u"] for s in meta.get("sources") or [] if s.get("registry") == "fec"), "")
        if fec:
            out.append((f"Connected organization: {meta['connected_org']}", fec))
    return out


def _sig(src: list[tuple[str, str]]) -> str:
    return hashlib.sha1(json.dumps(src, sort_keys=True).encode()).hexdigest()[:12]


def run(store, root: Path, deadline: float, log=print) -> dict:
    from . import llm
    from .money import lines

    try:
        have = json.loads(store.kv_get("donor_about") or "{}")
    except ValueError:
        have = {}
    totals: dict[str, float] = defaultdict(float)
    for L in lines(store):
        e = (L["meta"] or {}).get("donor_entity")
        if e and L["amount"] > 0 and L["type"] != "expenditure":
            totals[str(e)] += L["amount"]
    todo = []
    for r in store.iter_query("kind='organization'"):
        m = r.get("meta") or {}
        eid = m.get("entity_id")
        if not eid or eid not in totals:
            continue
        src = _sources(m)
        if sum(len(t) for t, _ in src) < MIN_SOURCE:
            continue
        sig = _sig(src)
        h = have.get(eid) or {}
        if h.get("v") == VERSION and h.get("sig") == sig:
            continue
        todo.append((totals[eid], eid, m.get("name") or r.get("title") or "", src, sig))
    todo.sort(key=lambda x: -x[0])            # the largest donors first
    log_path = root / "data" / "train" / "donor_about_log.jsonl"
    st = {"todo": len(todo), "read": 0, "kept": 0}
    for _, eid, name, src, sig in todo:
        if time.monotonic() > deadline:
            break
        body = "\n\n".join(f"Text {i + 1} ({u}):\n{t}" for i, (t, u) in enumerate(src))
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ASK.format(name=name, texts=body)}]
        t0 = time.time()
        try:
            out = llm.chat(msgs, max_tokens=120)
        except Exception as e:  # noqa: BLE001
            log(f"donors ai: {type(e).__name__}: {str(e)[:200]}")
            break
        line = " ".join(out.split()).strip('"')
        source = name + " " + " ".join(t for t, _ in src)
        why = llm.check(line, source, max_words=50, allow=(name,)) if line else ["empty answer"]
        second = ""
        if not why:
            second = llm.chat([{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": VERIFY.format(texts=body, line=line)}], max_tokens=60).strip()
            if not second.upper().startswith("OK"):
                why = ["the model's second reading: " + second[:160]]
        st["read"] += 1
        rec = {"v": VERSION, "sig": sig, "m": llm.model_name(), "at": dt.date.today().isoformat()}
        if not why:
            rec.update({"t": line, "u": sorted({u for _, u in src})})
            st["kept"] += 1
        else:
            rec["why"] = why[:3]
        have[eid] = rec
        llm.log(str(log_path), {"task": "donor_about", "entity": eid, "name": name, "input": msgs, "output": out,
                                "second_reading": second, "kept": not why, "why": why, "seconds": round(time.time() - t0, 1)})
        log(f"  {'kept' if not why else 'dropped'} ({time.time() - t0:.0f}s): {name[:70]}" + (f"  [{'; '.join(why[:2])}]" if why else ""))
        store.kv_set("donor_about", json.dumps(have, ensure_ascii=False, separators=(",", ":")))
    st["left"] = st["todo"] - st["read"]
    log(f"donors ai: {st['read']} organizations read, {st['kept']} summaries kept; {st['left']} left for the next run")
    return st
