"""The open model's reading of meeting-video channels the crawler could not confirm (the Rumor Mill - Video bucket),
taught by the owner's decisions (config/channel_decisions.yaml).

For each channel still unverified, the model sees what the crawler knows about it (its title and handle, the body it
was found for, whether the body's official website links to it, why the name checks held it back, and its newest
video titles) next to the owner's decided channels as worked examples, and answers in JSON:
  official   is this the body's own meeting channel (true/false)
  why        one sentence, from the facts given
A channel is confirmed on its own only when it is as good as the channels the owner already confirmed:
  - the model says it is the body's own, and
  - the body's official website links to it (the seed found the link), and
  - every listed video is a meeting of that body (its title names the body or its place and a meeting word), and
  - no owner decision says otherwise.
Channels confirmed this way are kept in state/channel_ai.json ("auto"), which channel_decisions.load() reads beneath the
owner's own decisions. Everything else the model calls official waits for the owner in data/channel_review.md, each with
the lines to paste into config/channel_decisions.yaml to confirm (or, with status: rejected, to reject) it. A confirmed
channel's videos become records on the next video crawl.

Every model call, and every owner decision as a labeled example, goes to data/train/channel_log.jsonl for the fine-tune.

    gacrawl channel-ai --max-minutes 5
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections import defaultdict
from pathlib import Path


VERSION = 1
MEETING = re.compile(r"board|council|commission|meeting|work session|hearing|called|regular|budget|retreat", re.I)
SYSTEM = ("You decide whether a YouTube channel is a Georgia government body's own channel for its public meetings. You answer "
          "only from the facts given, and in JSON only.")
ASK = """Channels already decided by the site's owner (follow the same standard):
{examples}

Channel to decide:
- title: {title}
- handle: {handle}
- found for: {body}
- linked from the body's official website: {linked}
- why the crawler's name check held it back: {reason}
- newest videos: {videos}

Answer in JSON: {{"official": true or false, "why": "one sentence from these facts"}}"""


def _json(out: str) -> dict | None:
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) > 2 and w not in
            {"the", "and", "county", "city", "school", "schools", "district", "board", "education", "public", "system"}}


def _body_name(jid: str, names: dict) -> str:
    return names.get(jid) or jid.replace("sd-city-", "").replace("sd-", "").replace("-", " ").title()


def examples(decisions: dict, rows_by_ch: dict, k: int = 4) -> str:
    out = []
    for cid, d in list(decisions.items())[:k]:
        vids = [r.get("title", "") for r in rows_by_ch.get(cid, [])][:3]
        out.append(f"- \"{d.get('title', '')}\" for {d.get('jurisdiction', '')}: {d['status'].upper()}"
                   + (f" (linked from {d['found_on']})" if d.get("found_on") else "")
                   + (f"; videos: {'; '.join(vids)}" if vids else "") + (f"; note: {d['note']}" if d.get("note") else ""))
    return "\n".join(out) or "- (none yet)"


def same_quality(row_group: list[dict], body: str) -> tuple[bool, list[str]]:
    """The rule-side checks for confirming without the owner (the model's answer is the other half)."""
    why = []
    reason = row_group[0].get("reason") or ""
    if "linked from the official website" not in reason and "hand-checked" not in reason:
        why.append("the body's official website does not link to it")
    bt = _toks(body)
    for r in row_group:
        t = r.get("title") or ""
        if not MEETING.search(t) or (bt and not (bt & _toks(t))):
            why.append(f"a video is not plainly a meeting of {body}: {t[:60]}")
            break
    return not why, why


def run(root: Path, deadline: float, log=print, llm=None) -> dict:
    from .channel_decisions import load
    from .rumor_video import VideoRumorMill

    if llm is None:
        from . import llm as _llm
        llm = _llm
    root = Path(root)
    review_p = root / "data" / "channel_review.md"
    state_p = root / "state" / "channel_ai.json"
    log_p = str(root / "data" / "train" / "channel_log.jsonl")
    try:
        st = json.loads(state_p.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("v", VERSION); st.setdefault("read", {}); st.setdefault("labeled", []); st.setdefault("auto", {})
    rows = VideoRumorMill(root).all()
    by_ch: dict[str, list] = defaultdict(list)
    for r in rows:
        cid = (r.get("channel") or {}).get("id") or ""
        if cid:
            by_ch[cid].append(r)
    decisions = load(root)
    # every owner decision is a labeled example in the training log, once
    for cid, d in decisions.items():
        if cid not in st["labeled"]:
            g = by_ch.get(cid, [])
            llm.log(log_p, {"task": "channel_decision", "channel": cid, "title": d.get("title"), "jurisdiction": d.get("jurisdiction"),
                            "label": d["status"], "by": d.get("by", "owner"), "videos": [r.get("title") for r in g[:5]],
                            "reason_held_back": (g[0].get("reason") if g else ""), "note": d.get("note", "")})
            st["labeled"].append(cid)
    try:
        from .registry import jurisdictions

        names = {j["id"]: j.get("name", "") for j in jurisdictions(root)}
    except Exception:  # noqa: BLE001
        names = {}
    stats = {"channels": len(by_ch), "decided": len(decisions), "read": 0, "auto": 0, "review": 0}
    review = []
    for cid, g in sorted(by_ch.items(), key=lambda kv: -len(kv[1])):
        if cid in decisions:
            continue
        if time.monotonic() > deadline:
            break
        g.sort(key=lambda r: r.get("published") or "", reverse=True)
        ch = g[0].get("channel") or {}
        body = _body_name(g[0].get("jurisdiction", ""), names)
        sig = f"{len(g)}:{g[0].get('reason', '')[:80]}"
        prev = st["read"].get(cid)
        if prev and prev.get("sig") == sig:
            ans = prev
        else:
            msgs = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": ASK.format(examples=examples(decisions, by_ch), title=ch.get("title", ""),
                                                           handle=ch.get("handle", ""), body=body,
                                                           linked="yes" if "linked from the official website" in (g[0].get("reason") or "") else "not found",
                                                           reason=g[0].get("reason", ""), videos="; ".join(r.get("title", "") for r in g[:6]))}]
            t0 = time.time()
            try:
                out = llm.chat(msgs, max_tokens=120)
            except Exception as e:  # noqa: BLE001
                log(f"channel ai: the model failed: {type(e).__name__}: {str(e)[:150]}")
                break
            j = _json(out) or {}
            ans = {"sig": sig, "official": bool(j.get("official")), "why": " ".join(str(j.get("why") or "").split())[:300],
                   "at": dt.date.today().isoformat(), "m": llm.model_name()}
            st["read"][cid] = ans
            stats["read"] += 1
            ok, fails = same_quality(g, body)
            llm.log(log_p, {"task": "channel_judge", "channel": cid, "input": msgs, "output": out, "official": ans["official"],
                            "auto": bool(ans["official"] and ok), "fails": fails, "seconds": round(time.time() - t0, 1)})
        ok, fails = same_quality(g, body)
        if ans["official"] and ok:
            st["auto"][cid] = {"title": ch.get("title", ""), "jurisdiction": g[0].get("jurisdiction", ""), "status": "confirmed",
                               "decided": dt.date.today().isoformat(), "by": "open model (same quality as the owner's confirmed channels)",
                               "note": ans["why"]}
            stats["auto"] += 1
            log(f"  confirmed on its own: {ch.get('title')} ({body})")
        elif ans["official"]:
            review.append((cid, ch, body, g, ans, fails))
    stats["review"] = len(review)
    L = [f"# Meeting channels to decide ({dt.date.today().isoformat()})", "",
         "Channels whose meeting videos the crawler found but could not confirm as the body's own, which the open model "
         "(gacrawl/channel_ai.py) reads as official. To confirm one, paste its lines under `channels:` in "
         "config/channel_decisions.yaml (status: rejected to keep it out) and commit; the crawler and the model use it from the "
         "next run.", ""]
    for cid, ch, body, g, ans, fails in review:
        L.append(f"- [ ] `{cid}` **{ch.get('title', '')}** for {body} · {len(g)} videos · https://www.youtube.com/channel/{cid}")
        L.append(f"  - model: {ans['why']}")
        L.append(f"  - not confirmed on its own because: {'; '.join(fails) or 'the owner decides channels like this one'}")
        L.append(f"  - e.g. {g[0].get('title', '')} ({g[0].get('url', '')})")
        L.append(f"  - paste: `{cid}: {{title: {json.dumps(ch.get('title', ''))}, jurisdiction: {g[0].get('jurisdiction', '')}, "
                 f"status: confirmed, decided: \"{dt.date.today().isoformat()}\", by: owner}}`")
    try:
        review_p.parent.mkdir(parents=True, exist_ok=True)
        review_p.write_text("\n".join(L) + "\n", encoding="utf-8")
        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(st, ensure_ascii=False, indent=0))
    except OSError:
        pass
    log(f"channel ai: {stats}")
    return stats
