"""Police incidents as high-trust outlets report them, per department, and the officers named in that reporting.

For each department found by `gacrawl police` (state/police.json), one GDELT news query a week for the department's
name (and its chief's), hits kept only from the outlets in config/outlets.yaml (evidence `reported`) or official
.gov pages (`primary`); every kept article is opened and must name the department and describe an incident (a
shooting, use of force, a lawsuit or settlement, a firing or suspension, an indictment, a misconduct complaint,
an investigation, a pursuit or in-custody death). Each becomes a `police_incident` record: title, date, outlet,
an excerpt around the first incident cue, and the officers named with a rank beside the name (Officer, Sgt.,
Lt., Capt., Deputy, Chief, Sheriff...). Nothing is inferred from a bare name: a person is listed only when the
article prints a rank or title with the name. Profiles (export) group mentions by name within a department, each
with its articles, so the Policing tab can show what the record says and link every line to the article.

Run: gacrawl police news [--max-minutes M]. State: the records in the store (source police:<jid>) and
state/police_news.json (department id -> last checked; police.json is only read, so the police contacts job and
this step never write the same file). Export: police_incidents.json in the Electo bundle.
"""
from __future__ import annotations

import datetime as dt
import json
from .registry import write_json_atomic
import re
import time
from collections import defaultdict
from pathlib import Path

from . import extract
from .store import Record

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
WINDOW_DAYS = 89
RECHECK_DAYS = 7
MAX_HITS = 40
CUE = re.compile(r"\b(shot|shooting|shoot|killed|death|died|dies|use of force|excessive force|force|tas(?:ed|er)|chokehold|"
                 r"lawsuit|sued|sues|settlement|settle[ds]?|indict(?:ed|ment)|charged|arrest(?:ed)?|fired|terminated|"
                 r"suspended|resign(?:ed|s)?|body[\s-]?cam(?:era)?|dash[\s-]?cam|misconduct|complaint|internal affairs|"
                 r"investigat(?:ion|ed|ing)|GBI|pursuit|chase|crash|in custody|wrongful|brutality|civil rights|"
                 r"discriminat\w+|harass\w+|DUI|domestic|assault)\b", re.I)
RANK = (r"(?:Chief of Police|Police Chief|Deputy Chief|Assistant Chief|Interim Chief|Interim Police Chief|Chief|Assistant Chief|Interim Chief|Sheriff|Chief Deputy|Undersheriff|Major|Maj\.|"
        r"Captain|Capt\.|Lieutenant|Lt\.|Sergeant|Sgt\.|Corporal|Cpl\.|Officer|Ofc\.|Patrolman|Deputy|Detective|Det\.|"
        r"Investigator|Trooper|Marshal|Commander|Cmdr\.)")
NAME = r"([A-Z][a-z'’\-]+(?:\s(?:[A-Z]\.|[A-Z][a-z'’\-]+)){1,2}(?:,?\s(?:Jr|Sr|II|III|IV)\.?)?)"
RANK_NAME = re.compile(r"\b(?:former\s|ex-)?(" + RANK + r")\s+" + NAME)
NAME_RANK = re.compile(NAME + r",\s+(?:an?\s|the\s)?(?:former\s)?(" + RANK + r")\b")
STOP = re.compile(r"\b(Department|Police|County|City|Office|Sheriff|Georgia|Atlanta|Monday|Tuesday|Wednesday|Thursday|"
                  r"Friday|Saturday|Sunday|January|February|March|April|May|June|July|August|September|October|November|"
                  r"December|Street|Avenue|Road|Drive|Highway)\b")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def names_in(text: str) -> list[dict]:
    """People the article names with a rank: [{name, rank}], first mention first, each name once."""
    out, seen = [], set()

    def add(rank: str, name: str):
        name = re.sub(r"\s+", " ", name).strip(" ,.")
        rank = re.sub(r"\s+", " ", rank).strip(" ,.")
        if STOP.search(name) or len(name.split()) < 2 or name.lower() in seen:
            return
        seen.add(name.lower())
        out.append({"name": name, "rank": rank})

    for m in RANK_NAME.finditer(text):
        add(m.group(1), m.group(2))
    for m in NAME_RANK.finditer(text):
        add(m.group(2), m.group(1))
    return out[:12]


def excerpt_of(text: str, dept_name: str, width: int = 700) -> str:
    """The passage around the first incident cue near the department's name (or the first cue)."""
    t = re.sub(r"\s+", " ", text or "")
    key = dept_name.split()[0] if dept_name else ""
    best = None
    for m in CUE.finditer(t):
        window = t[max(0, m.start() - 300): m.end() + 300]
        if key and key.lower() in window.lower():
            best = m
            break
        best = best or m
    if not best:
        return t[:width]
    a = max(0, best.start() - width // 2)
    return ("…" if a else "") + t[a: a + width].strip() + ("…" if a + width < len(t) else "")


def _mentions_department(text: str, dept: dict, jname: str) -> bool:
    t = (text or "").lower()
    name = (dept.get("name") or "").lower()
    if name and name in t:
        return True
    place = jname.replace(" county", "").lower()
    kind = "sheriff" if dept.get("kind") == "sheriff" else "police"
    return bool(place and place in t and kind in t and re.search(r"\b" + re.escape(place) + r"\b[^.]{0,60}\b" + kind, t))


class PoliceNews:
    def __init__(self, fetcher, store, root: Path, log=print):
        from .candidates import load_outlets

        self.f, self.store, self.root, self.log = fetcher, store, root, log
        self.outlets, self.exclude = load_outlets(root)
        self.notes: dict = {}

    def classify(self, url: str) -> str | None:
        from .candidates import _in, domain_of

        d = domain_of(url)
        if _in(d, self.exclude):
            return None
        if d.endswith((".gov", ".ga.us")):
            return "primary"
        if _in(d, self.outlets):
            return "reported"
        return None

    def search(self, phrases: list[str]) -> list[dict]:
        floor = (dt.date.today() - dt.timedelta(days=WINDOW_DAYS)).isoformat()
        q = (phrases[0] if len(phrases) == 1 else "(" + " OR ".join(phrases) + ")") + " sourcecountry:US sourcelang:english"
        r = self.f.get(GDELT, params={"query": q, "mode": "artlist", "maxrecords": 100, "format": "json", "sort": "datedesc",
                                      "startdatetime": floor.replace("-", "") + "000000"}, use_cache=False)
        if r.status == 200 and r.content.strip().startswith(b"{"):
            return r.json().get("articles", []) or []
        self.notes["gdelt_not_json"] = self.notes.get("gdelt_not_json", 0) + 1
        if self.notes["gdelt_not_json"] == 1:
            self.log(f"  gdelt: HTTP {r.status} {r.content_type}: {r.text[:160]!r}")
        if r.status == 429 or b"rate" in r.content[:300].lower():
            raise RateLimited()
        return []

    def department(self, jid: str, dept: dict, jname: str) -> int:
        """One query for the department (and its chief); open the trusted hits once; keep the incidents."""
        from .blocked import Blocked
        from .fetch import Disallowed, OutOfTime

        phrases = [f'"{dept["name"]}"'] if dept.get("name") else []
        if dept.get("chief") and dept["chief"].get("name"):
            phrases.append(f'"{dept["chief"]["name"]}"')
        if not phrases:
            return 0
        hits = [a for a in self.search(phrases) if a.get("url") and self.classify(a["url"])]
        n = 0
        for a in hits[:MAX_HITS]:
            url = a["url"]
            if self.store.fresh(url, 3650):
                continue
            try:
                r = self.f.get(url)
            except OutOfTime:
                raise
            except (Disallowed, Blocked, Exception):  # noqa: BLE001
                continue
            self.store.mark_url(url, r.status, f"police:{jid}", r.content_type)
            if r.status != 200 or "html" not in r.content_type:
                continue
            title, md = extract.html_to_markdown(r.text, r.url)
            text = md or ""
            if not _mentions_department(text, dept, jname) or not CUE.search(text):
                continue
            pub = a.get("seendate", "")
            pub = f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if len(pub) >= 8 else (extract.page_date(r.text, text) or None)
            names = names_in(text)
            cues = sorted({m.group(1).lower() for m in CUE.finditer(text)})[:8]
            from .candidates import domain_of

            rec = Record(f"police:{jid}", "police_incident", r.url, title=(title or a.get("title") or "")[:200],
                         text=excerpt_of(text, dept.get("name", "")), published=pub, channel="html", jurisdiction=jid,
                         evidence=self.classify(r.url) or "reported",
                         meta={"department": dept.get("name"), "department_kind": dept.get("kind"), "outlet": domain_of(r.url),
                               "names": names, "cues": cues, "opened": True, "checked": dt.date.today().isoformat()})
            n += self.store.add(rec)
        return n


class RateLimited(Exception):
    pass


def research(fetcher, store, root: Path, state_dir: Path, deadline: float | None = None, log=print,
             limit: int | None = None) -> dict:
    from .fetch import OutOfTime
    from .registry import jurisdictions

    p = state_dir / "police.json"
    police = json.loads(p.read_text()) if p.exists() else {}
    cp = state_dir / "police_news.json"
    checked = json.loads(cp.read_text()) if cp.exists() else {}
    pop = {j["id"]: (j.get("population") or 0, j["name"]) for j in jurisdictions(root)}
    pn = PoliceNews(fetcher, store, root, log)
    done = found = 0
    order = sorted((jid for jid, d in police.items() if d.get("found")), key=lambda j: -pop.get(j, (0, ""))[0])
    for jid in order:
        d = police[jid]
        last = checked.get(jid) or d.get("news_checked_at")
        if last and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).days < RECHECK_DAYS:
            continue
        if deadline and time.monotonic() > deadline or (limit and done >= limit):
            break
        try:
            n = pn.department(jid, d, pop.get(jid, (0, jid))[1])
        except OutOfTime:
            break
        except RateLimited:
            log("  gdelt rate-limited this run; the rest wait for the next run")
            break
        checked[jid] = _now()
        done += 1
        found += n
        if n:
            log(f"{jid}: {n} new incident reports")
        if done % 5 == 0:
            write_json_atomic(cp, checked, indent=1)
    write_json_atomic(cp, checked, indent=1)
    store.flush()
    return {"departments": done, "new_incidents": found}


def export_incidents(store, out_path: Path, jurisdictions: list[dict]) -> dict:
    """police_incidents.json: per department, its reported incidents (newest first) and the people named with a
    rank, each with the articles that name them."""
    by_id = {j["id"]: j for j in jurisdictions}
    depts: dict = {}
    for r in store.iter_query("kind='police_incident'"):
        jid = r.get("jurisdiction") or r["source_id"].split(":", 1)[-1]
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        D = depts.setdefault(jid, {"jurisdiction": jid, "jurisdiction_name": (by_id.get(jid) or {}).get("name", jid),
                                   "department": meta.get("department"), "kind": meta.get("department_kind"),
                                   "incidents": [], "people": {}})
        item = {"t": r.get("title") or "", "u": r["url"], "d": r.get("published") or "", "outlet": meta.get("outlet", ""),
                "evidence": r.get("evidence"), "excerpt": r.get("text") or "", "names": meta.get("names") or [], "cues": meta.get("cues") or []}
        D["incidents"].append(item)
        for nm in item["names"]:
            P = D["people"].setdefault(nm["name"], {"name": nm["name"], "ranks": [], "items": []})
            if nm["rank"] not in P["ranks"]:
                P["ranks"].append(nm["rank"])
            P["items"].append({"t": item["t"], "u": item["u"], "d": item["d"], "outlet": item["outlet"]})
    n_inc = 0
    for D in depts.values():
        D["incidents"].sort(key=lambda x: x["d"], reverse=True)
        n_inc += len(D["incidents"])
        D["people"] = sorted(D["people"].values(), key=lambda P: -len(P["items"]))
        for P in D["people"]:
            P["items"].sort(key=lambda x: x["d"], reverse=True)
            P["mentions"] = len(P["items"])
    out_path.write_text(json.dumps({
        "note": "Police incidents as the outlets in config/outlets.yaml and official pages report them, per department, "
                "with the officers each article names together with a rank. Excerpts are the article's words around "
                "the first incident cue; every line links to the article. A name appears only with a printed rank; "
                "nothing here is a finding, only what was reported.",
        "departments": list(depts.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"departments": len(depts), "incidents": n_inc}
