"""Federal bills: every bill and resolution of the U.S. House and Senate since the 114th Congress (2015-2016, so all
of 2016 on), from GovInfo's Bill Status bulk data (the Library of Congress's official bill status XML, the data behind
congress.gov), for Electo's Bills & proposals tab (its Federal toggle) and the bill pop-up any page reference opens.

Where it reads (no key; the public bulk data repository, read through the crawler's own fetcher, robots and all):
  https://www.govinfo.gov/bulkdata/json/BILLSTATUS/{congress}/{type}    the listing (Accept: application/json): each
                                                                       bill's XML and one zip of the whole folder,
                                                                       with last-modified times
  a folder is read from its zip the first time and whenever the zip changes; for the Congress in session, bills whose
  own file changed since the last run are read one by one (the zip is rebuilt less often than the files).

Each bill is one `federal_bill` record (key fed|{congress}|{type}|{number}) whose meta keeps what the page needs: title,
sponsor (name, party, state), cosponsors (count, how many from the other party, Georgia members), the dates it reached
each stage (introduced, reported, passed the House, passed the Senate, presented, law, vetoed, failed), the latest
action, the policy area, the law number, the official summary's first sentences, and congress.gov's page.

The outlook ("Pass Likely" / "Pass Not Likely") for a bill still pending in the Congress in session is the record of
earlier Congresses, not a guess: of the bills in the 114th-118th Congresses that stood at the same stage, at the same
point in their Congress, with a sponsor in the same position (majority or minority party of their chamber) and the
same kind of support (any cosponsor from the other party or not), the share that became law (a simple or concurrent
resolution: the share agreed to). The page shows that share, how many bills it rests on, and the reasons.

    gacrawl federal-bills [--max-minutes 20]      read what changed
    export_federal(store, out_dir)                 dist/electo/federal_bills/{congress}.json (+ meta.json)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

BASE = "https://www.govinfo.gov/bulkdata"
FIRST = 114
TYPES = ("hr", "s", "hjres", "sjres", "hres", "sres", "hconres", "sconres")
LABEL = {"hr": "H.R.", "s": "S.", "hjres": "H.J.Res.", "sjres": "S.J.Res.", "hres": "H.Res.", "sres": "S.Res.",
         "hconres": "H.Con.Res.", "sconres": "S.Con.Res."}
CG_TYPE = {"hr": "house-bill", "s": "senate-bill", "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution",
           "hres": "house-resolution", "sres": "senate-resolution", "hconres": "house-concurrent-resolution",
           "sconres": "senate-concurrent-resolution"}
# majority party of each chamber, by Congress (the Senate's 117th: 50-50, the Vice President's party)
MAJORITY = {114: ("R", "R"), 115: ("R", "R"), 116: ("D", "R"), 117: ("D", "D"), 118: ("R", "D"), 119: ("R", "R")}
STAGES = ("introduced", "reported", "passed_one", "passed_both", "law")
STAGE_LABEL = {"introduced": "Introduced", "reported": "Reported by committee", "passed_one": "Passed one chamber",
               "passed_both": "Passed both chambers", "law": "Became law", "adopted": "Agreed to", "vetoed": "Vetoed",
               "failed": "Failed"}


def congress_now(today: dt.date | None = None) -> int:
    t = today or dt.date.today()
    c = (t.year - 1789) // 2 + 1
    if t.year % 2 == 1 and t < dt.date(t.year, 1, 3):
        c -= 1
    return c


def congress_start(c: int) -> dt.date:
    return dt.date(1789 + 2 * (c - 1), 1, 3)


def cg_url(c: int, t: str, n: int | str) -> str:
    return f"https://www.congress.gov/bill/{c}th-congress/{CG_TYPE[t]}/{n}"


def _t(e, path, default=""):
    x = e.find(path) if e is not None else None
    return (x.text or "").strip() if x is not None and x.text else default


def parse(xml: bytes) -> dict | None:
    """One BILLSTATUS XML -> the bill's meta (both schema generations: number/type and billNumber/billType)."""
    root = ET.fromstring(xml)
    b = root.find("bill")
    if b is None:
        return None
    num = _t(b, "number") or _t(b, "billNumber")
    typ = (_t(b, "type") or _t(b, "billType")).lower()
    c = int(_t(b, "congress") or 0)
    if not num or typ not in LABEL or not c:
        return None
    sp = b.find("sponsors/item")
    sponsor = {"name": _t(sp, "fullName"), "party": _t(sp, "party"), "state": _t(sp, "state"),
               "id": _t(sp, "bioguideId")} if sp is not None else {}
    cos = [x for x in b.findall("cosponsors/item") if not _t(x, "sponsorshipWithdrawnDate")]
    other = sum(1 for x in cos if sponsor.get("party") and _t(x, "party") and _t(x, "party") != sponsor["party"])
    ga = [_t(x, "fullName") for x in cos if _t(x, "state") == "GA"]
    acts = []
    items = b.findall("actions/item")
    loc = [a for a in items if _t(a, "sourceSystem/code") == "9"]     # the Library of Congress's own action lines
    for a in (loc or items):
        acts.append((_t(a, "actionDate"), _t(a, "text"), _t(a, "type")))
    acts.sort()
    m: dict = {}

    def mark(k, d):
        if d and (k not in m or d < m[k]):
            m[k] = d

    for d, text, typ_a in acts:
        tl = text.lower()
        if typ_a == "IntroReferral" or "introduced in" in tl:
            mark("intro", d)
        if re.search(r"^reported (by|with|to|in)|\breported to (the )?(house|senate)\b(?!.*rules committee)|ordered to be reported|"
                     r"placed on (the )?(union|house|senate legislative) calendar|discharged", tl) and not tl.startswith("rules committee resolution"):
            mark("rep", d)
        if re.search(r"passed/agreed to in house|passed house|agreed to in house|resolution agreed to in house", tl) and "motion" not in tl[:20]:
            mark("ph", d)
        if re.search(r"passed/agreed to in senate|passed senate|agreed to in senate|resolution agreed to in senate", tl):
            mark("ps", d)
        if "presented to president" in tl:
            mark("pres", d)
        if re.search(r"became public law|became private law|signed by president", tl):
            mark("law", d)
        if "vetoed by president" in tl or "pocket vetoed" in tl:
            mark("veto", d)
        if re.search(r"failed of passage|failed to pass|motion to proceed .{0,40}not agreed", tl):
            mark("fail", d)
        if "veto overridden" in tl or "passed over veto" in tl:
            mark("override", d)
    law = b.find("laws/item")
    if law is not None:
        mark("law", m.get("pres") or (acts[-1][0] if acts else ""))
    intro = _t(b, "introducedDate") or m.get("intro", "")
    m["intro"] = intro
    summ = ""
    sums = b.findall("summaries/summary") or b.findall("summaries/billSummaries/item")
    if sums:
        s = max(sums, key=lambda x: _t(x, "actionDate") or _t(x, "updateDate"))
        txt = _t(s, "cdata/text") or _t(s, "text")
        txt = re.sub(r"<[^>]+>", " ", txt)
        summ = re.sub(r"\s+", " ", txt).strip()[:600]
    la = b.find("latestAction")
    return {"c": c, "t": typ, "n": int(num), "title": _t(b, "title")[:300], "intro": intro, "sponsor": sponsor,
            "cos": len(cos), "cos_other": other, "cos_ga": ga[:12], "m": m,
            "last": {"d": _t(la, "actionDate"), "text": _t(la, "text")[:300]},
            "area": _t(b, "policyArea/name"), "law": (_t(law, "type") + " " + _t(law, "number")).strip() if law is not None else "",
            "summary": summ, "u": cg_url(c, typ, num)}


def stage_of(meta: dict, on: str | None = None) -> str:
    """Where the bill stood on date `on` (default: now)."""
    m = meta["m"]
    ok = (lambda k: m.get(k) and (on is None or m[k] <= on))
    t = meta["t"]
    simple = t in ("hres", "sres")
    conc = t in ("hconres", "sconres")
    if ok("veto") and not ok("override"):
        return "vetoed"
    if simple and (ok("ph") or ok("ps")):
        return "adopted"
    if conc and ok("ph") and ok("ps"):
        return "adopted"
    if ok("law") or ok("override"):
        return "law"
    if ok("ph") and ok("ps"):
        return "passed_both"
    if ok("ph") or ok("ps"):
        return "passed_one"
    if ok("fail"):
        return "failed"
    if ok("rep"):
        return "reported"
    return "introduced"


def done(meta: dict, on: str | None = None) -> bool:
    return stage_of(meta, on) in ("law", "adopted")


def _kind(t: str) -> str:
    return "simple" if t in ("hres", "sres") else "concurrent" if t in ("hconres", "sconres") else "bill"


def _feat(meta: dict) -> tuple[str, bool, bool]:
    c, t = meta["c"], meta["t"]
    maj = MAJORITY.get(c, ("", ""))[0 if t.startswith("h") else 1]
    return _kind(t), (meta["sponsor"].get("party") or "") == maj, meta["cos_other"] > 0


class Outlook:
    """Base rates from earlier Congresses (see the module docstring). The table for one day of a Congress is built once
    (each earlier bill's stage on that day and how it ended), then every pending bill is a lookup."""

    def __init__(self, past: list[dict]):
        self.past = [p for p in past if p["intro"]]
        self._day = None
        self._tab: dict = {}

    def _table(self, day: int) -> dict:
        if self._day != day:
            tab: dict = defaultdict(lambda: [0, 0])
            for p in self.past:
                on = (congress_start(p["c"]) + dt.timedelta(days=day)).isoformat()
                if p["intro"] > on:
                    continue
                kind, maj, bip = _feat(p)
                st = stage_of(p, on)
                ok = done(p)
                for key in ((kind, st, maj, bip), (kind, st, maj), (kind, st)):
                    tab[key][0] += 1
                    tab[key][1] += ok
            self._day, self._tab = day, tab
        return self._tab

    def rate(self, meta: dict, today: dt.date | None = None) -> dict | None:
        today = today or dt.date.today()
        c = meta["c"]
        if c != congress_now(today):
            return None
        day = (today - congress_start(c)).days
        st = stage_of(meta)
        if st in ("law", "adopted", "vetoed", "failed"):
            return None
        kind, maj, bip = _feat(meta)
        tab = self._table(day)
        n = won = 0
        how = ""
        for key, h in (((kind, st, maj, bip), "the same stage, a sponsor in the same position and the same kind of support"),
                       ((kind, st, maj), "the same stage and a sponsor in the same position"),
                       ((kind, st), "the same stage")):
            n, won = tab.get(key, (0, 0))
            how = h
            if n >= 30:
                break
        if not n:
            return None
        pct = round(100 * won / n, 1)
        yes = pct >= 50
        far = abs(pct - 50)
        conf = "Very likely" if far >= 40 else "Fairly confident" if far >= 25 else "Lean"
        what = "agreed to" if kind != "bill" else "became law"
        party = meta["sponsor"].get("party") or "?"
        why = [f"Of {n:,} bills in the 114th-118th Congresses that stood where this one stands ({STAGE_LABEL[st].lower()}) "
               f"{day} days into their Congress, with {how}, {won:,} ({pct}%) {what}.",
               f"Sponsor: {meta['sponsor'].get('name') or 'unknown'} ({party}), "
               + ("in the majority party of the chamber." if maj else "in the minority party of the chamber."),
               f"{meta['cos']} cosponsor{'s' if meta['cos'] != 1 else ''}"
               + (f", {meta['cos_other']} from the other party." if bip else ", none from the other party."),
               f"The {c}th Congress ends on January 3, {congress_start(c + 1).year}: a bill not enacted by then dies."]
        return {"v": "y" if yes else "n", "label": "Pass Likely" if yes else "Pass Not Likely", "pct": pct, "n": n, "conf": conf, "why": why}


# ------------------------------------------------------------------------------------------------- reading
def _listing(f, c: int, t: str) -> list[dict]:
    r = f.get(f"{BASE}/json/BILLSTATUS/{c}/{t}", headers={"Accept": "application/json"}, use_cache=False)
    if r.status != 200:
        raise RuntimeError(f"listing {c}/{t}: HTTP {r.status}")
    return json.loads(r.text).get("files") or []


def _save(store, meta: dict) -> bool:
    from .store import Record

    rec = Record("federal", "federal_bill", meta["u"], title=f"{LABEL[meta['t']]} {meta['n']} ({meta['c']}th): {meta['title']}"[:400],
                 text=meta["summary"], published=meta["intro"] or None, channel="api", evidence="primary",
                 key=f"fed|{meta['c']}|{meta['t']}|{meta['n']}", meta=meta)
    return store.add(rec)


def run(f, store, deadline: float, log=print, first: int = FIRST) -> dict:
    st = {"read": 0, "changed": 0, "folders": 0, "errors": 0}
    now_c = congress_now()
    for c in range(now_c, first - 1, -1):              # the Congress in session first
        for t in TYPES:
            if time.monotonic() > deadline:
                log("federal bills: out of time; the rest next run")
                return st
            ck = f"fedbills:{c}:{t}"
            cur = json.loads(store.kv_get(ck) or "{}")
            try:
                files = _listing(f, c, t)
            except Exception as e:  # noqa: BLE001
                st["errors"] += 1
                log(f"federal bills {c}/{t}: {type(e).__name__}: {str(e)[:160]}")
                continue
            z = next((x for x in files if (x.get("fileExtension") or "").lower() == "zip" or str(x.get("link", "")).endswith(".zip")), None)
            zmod = (z or {}).get("formattedLastModifiedTime") or ""
            if z and zmod != cur.get("zip"):
                try:
                    r = f.get(z["link"], use_cache=False)
                    zf = zipfile.ZipFile(io.BytesIO(r.content))
                except Exception as e:  # noqa: BLE001
                    st["errors"] += 1
                    log(f"federal bills {c}/{t}: zip: {type(e).__name__}: {str(e)[:160]}")
                    continue
                n = ch = 0
                for name in zf.namelist():
                    if not name.endswith(".xml"):
                        continue
                    try:
                        meta = parse(zf.read(name))
                    except ET.ParseError:
                        continue
                    if meta:
                        n += 1
                        ch += _save(store, meta)
                cur = {"zip": zmod, "files": {x["justFileName"]: x.get("formattedLastModifiedTime") or "" for x in files
                                              if x.get("justFileName", "").endswith(".xml")}}
                store.kv_set(ck, json.dumps(cur))
                st["read"] += n
                st["changed"] += ch
                st["folders"] += 1
                log(f"federal bills {c}/{t}: {n} read from the zip, {ch} new or changed")
                continue
            if c == now_c:     # in session: the bills whose own file changed since the zip
                seen = cur.get("files") or {}
                todo = [x for x in files if x.get("justFileName", "").endswith(".xml")
                        and (x.get("formattedLastModifiedTime") or "") != seen.get(x["justFileName"])]
                n = ch = 0
                for x in todo:
                    if time.monotonic() > deadline:
                        break
                    try:
                        meta = parse(f.get(x["link"], use_cache=False).content)
                    except Exception:  # noqa: BLE001
                        continue
                    if meta:
                        n += 1
                        ch += _save(store, meta)
                    seen[x["justFileName"]] = x.get("formattedLastModifiedTime") or ""
                cur["files"] = seen
                store.kv_set(ck, json.dumps(cur))
                st["read"] += n
                st["changed"] += ch
                if todo:
                    log(f"federal bills {c}/{t}: {n} changed bills read ({len(todo) - n} left)")
    log(f"federal bills: {st['read']} read, {st['changed']} new or changed, {st['folders']} folders from zips, {st['errors']} errors")
    return st


# ------------------------------------------------------------------------------------------------- export
COLS = ["t", "n", "title", "intro", "sponsor", "party", "state", "cos", "cos_other", "ga", "stage", "last_d", "last",
        "law", "area", "summary", "outlook", "ai"]


def export_federal(store, out_dir: Path, log=print, today: dt.date | None = None) -> dict:
    """federal_bills/{congress}.json: {"c", "cols", "rows"} (one row per bill, columns as COLS), and meta.json."""
    import shutil

    by_c: dict[int, list] = defaultdict(list)
    for r in store.iter_query("kind='federal_bill'"):
        m = r.get("meta") or {}
        if m.get("c"):
            by_c[m["c"]].append(m)
    if not by_c:
        log("federal bills: none in the store yet")
        return {"bills": 0}
    try:
        about = json.loads(store.kv_get("federal_bill_ai") or "{}")
    except ValueError:
        about = {}
    now_c = congress_now(today)
    ol = Outlook([m for c, ms in by_c.items() if c < now_c for m in ms])
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    counts = {}
    for c, ms in sorted(by_c.items()):
        rows = []
        for m in sorted(ms, key=lambda x: (TYPES.index(x["t"]) if x["t"] in TYPES else 9, x["n"])):
            o = ol.rate(m, today) if c == now_c else None
            sp = m.get("sponsor") or {}
            a = about.get(f"{c}|{m['t']}|{m['n']}")
            rows.append([m["t"], m["n"], m["title"], m["intro"], sp.get("name", ""), sp.get("party", ""), sp.get("state", ""),
                         m["cos"], m["cos_other"], m.get("cos_ga") or [], stage_of(m), m["last"]["d"], m["last"]["text"],
                         m.get("law", ""), m.get("area", ""), (m.get("summary") or "")[:400], o,
                         a if a and a.get("t") else None])
        (out_dir / f"{c}.json").write_text(json.dumps({"c": c, "cols": COLS, "rows": rows}, ensure_ascii=False, separators=(",", ":")),
                                           encoding="utf-8")
        counts[c] = len(rows)
    (out_dir / "meta.json").write_text(json.dumps({"congresses": sorted(counts), "now": now_c, "counts": counts,
                                                  "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                                  "source": "GovInfo Bill Status bulk data (Library of Congress)",
                                                  "source_url": f"{BASE}/BILLSTATUS"}, separators=(",", ":")), encoding="utf-8")
    log(f"federal bills: {sum(counts.values())} bills in {len(counts)} Congresses exported")
    return {"bills": sum(counts.values()), "congresses": len(counts)}


# ------------------------------------------------------------------------------------------------- the open model
AI_V = 1
AI_SYSTEM = ("You write short, plain-language descriptions of U.S. congressional bills for a civic information site. You use "
             "only what the official summary and title say. You never add opinions, predictions, praise or criticism.")
AI_ASK = """Bill: {label} ({congress}th Congress)
Title: {title}
Official summary (Congressional Research Service):
{summary}

In one or two sentences and at most 40 words, say in plain words what this bill would do. Use only the title and
summary above. Answer with the description only."""


def ai_run(store, root: Path, deadline: float, log=print) -> dict:
    """Two plain sentences per bill, written by the open model from the official summary and checked (llm.check), for
    the bills people are most likely to look at: the Congress in session's bills that passed a chamber or that a
    Georgia member sponsored or cosponsored, newest action first. Kept in kv federal_bill_ai; logged for training."""
    from . import llm

    try:
        have = json.loads(store.kv_get("federal_bill_ai") or "{}")
    except ValueError:
        have = {}
    now_c = congress_now()
    todo = []
    for r in store.iter_query("kind='federal_bill' AND key LIKE ?", (f"fed|{now_c}|%",)):
        m = r.get("meta") or {}
        k = f"{m['c']}|{m['t']}|{m['n']}"
        if not m.get("summary") or len(m["summary"]) < 120 or (have.get(k) or {}).get("v") == AI_V:
            continue
        ga = (m.get("sponsor") or {}).get("state") == "GA" or m.get("cos_ga")
        if not ga and stage_of(m) in ("introduced", "reported"):
            continue
        todo.append((m["last"]["d"], k, m))
    todo.sort(reverse=True)
    st = {"todo": len(todo), "read": 0, "kept": 0}
    for _, k, m in todo:
        if time.monotonic() > deadline:
            break
        label = f"{LABEL[m['t']]} {m['n']}"
        msgs = [{"role": "system", "content": AI_SYSTEM},
                {"role": "user", "content": AI_ASK.format(label=label, congress=m["c"], title=m["title"], summary=m["summary"])}]
        try:
            out = llm.chat(msgs, max_tokens=100)
        except Exception as e:  # noqa: BLE001
            log(f"federal bills ai: {type(e).__name__}: {str(e)[:200]}")
            break
        line = " ".join(out.split()).strip('"')
        why = llm.check(line, m["title"] + " " + m["summary"], max_words=45, allow=(label,)) if line else ["empty answer"]
        st["read"] += 1
        have[k] = {"v": AI_V, "m": llm.model_name(), **({"t": line} if not why else {"why": why[:3]})}
        st["kept"] += not why
        llm.log(str(root / "data" / "train" / "federal_bill_log.jsonl"),
                {"task": "federal_bill_summary", "bill": k, "input": msgs, "output": out, "kept": not why, "why": why})
        store.kv_set("federal_bill_ai", json.dumps(have, separators=(",", ":")))
    st["left"] = st["todo"] - st["read"]
    log(f"federal bills ai: {st['read']} read, {st['kept']} kept; {st['left']} left for the next run")
    return st
