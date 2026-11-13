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
MAX_HITS = 25          # trusted hits opened per department (headlines naming the place first)
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


NEWS_VERSION = 2      # 2: place-qualified searches ("DeKalb County police"), Georgia stories only. Version 1 searched
                      # the bare page heading ("Police Department") and filed Colorado and Atlanta stories under DeKalb.
OTHER_STATES = re.compile(r"\b(Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Hawaii|Idaho|"
                          r"Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
                          r"Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
                          r"North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
                          r"South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming)\b")


def _mentions_department(text: str, dept: dict, jname: str, phrase: str = "") -> bool:
    """The article names this department: its name, or what a story calls it ("DeKalb County police", "Hall County
    Sheriff's Office", "Atlanta Police Department"). A bare heading ("Police Department") never counts alone."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    name = (dept.get("name") or "").lower()
    place = (phrase or "").rsplit(" ", 1)[0].lower() or jname.lower()
    if name and place.split()[0] in name and name in t:
        return True
    kind = "sheriff" if dept.get("kind") == "sheriff" else "marshal" if dept.get("kind") == "marshal" else "police"
    return bool(place and re.search(r"\b" + re.escape(place) + r"(?:'s)?\s+(?:county\s+)?" + kind, t))


def in_georgia(title: str, text: str, outlet_local: bool, jname: str, county: str = "") -> bool:
    """A Georgia story: it says Georgia (or Ga.), or names the county, or comes from a Georgia outlet without a
    headline about another state ("Police identify fallen Colorado officer...", a FOX 5 Atlanta story, was filed
    under DeKalb). "Columbus police" alone could be Ohio's."""
    head = title or ""
    if OTHER_STATES.search(head) and not re.search(r"\bGeorgia\b|\bGa\.", head):
        return False
    body = (text or "")[:6000]
    if re.search(r"\bGeorgia\b|,\s*Ga\.|\bGa\.\s", body) or (county and re.search(r"\b" + re.escape(county) + r"\s+County\b", body)):
        return True
    return outlet_local and not OTHER_STATES.search(body[:1500])


class PoliceNews:
    def __init__(self, fetcher, store, root: Path, log=print):
        from .candidates import load_outlets

        self.f, self.store, self.root, self.log = fetcher, store, root, log
        self.outlets, self.exclude = load_outlets(root)
        self.notes: dict = {}
        self.wp = {}                  # outlet -> its WordPress news API (state/wp_outlets.json, kept by bill reasons)
        try:
            w = json.loads((root / "state" / "wp_outlets.json").read_text())
            self.wp = {d: e["api"] for d, e in w.items() if isinstance(e, dict) and e.get("api")}
        except (OSError, ValueError):
            pass
        self.county_outlets: dict = {}
        self.local = set()            # Georgia outlets: statewide, regional and county papers in config/outlets.yaml
        try:
            from .registry import load_yaml

            o = load_yaml(root / "config" / "outlets.yaml") or {}
            self.local |= set(o.get("statewide") or [])
            for r in (o.get("regions") or {}).values():
                self.local |= set((r or {}).get("outlets") or [])
            for lst in (o.get("county_outlets") or {}).values():
                self.local |= set(lst or [])
            self.county_outlets = {k.lower(): list(v or []) for k, v in (o.get("county_outlets") or {}).items()}
        except Exception:  # noqa: BLE001
            pass

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
        # GDELT allows one query per 5 seconds per address, and a GitHub runner's address is shared (the step before
        # this one queries GDELT too): a 429 is waited out (10, 20, 40 s) before this run gives up on the search;
        # after three refused searches GDELT is left alone for the rest of the run (the outlets' APIs carry on)
        if self.notes.get("gdelt_limited", 0) >= 3:
            raise RateLimited()
        for attempt in range(4):
            r = self.f.get(GDELT, params={"query": q, "mode": "artlist", "maxrecords": 100, "format": "json", "sort": "datedesc",
                                          "startdatetime": floor.replace("-", "") + "000000"}, use_cache=False)
            if r.status == 200 and r.content.strip().startswith(b"{"):
                return r.json().get("articles", []) or []
            limited = r.status == 429 or b"limit requests" in r.content[:300].lower()
            if not limited:
                break
            if attempt < 3:
                self.notes["gdelt_waits"] = self.notes.get("gdelt_waits", 0) + 1
                (getattr(self.f, "_sleep", None) or time.sleep)(10 * 2 ** attempt)
        self.notes["gdelt_not_json"] = self.notes.get("gdelt_not_json", 0) + 1
        if self.notes["gdelt_not_json"] == 1:
            self.log(f"  gdelt: HTTP {r.status} {r.content_type}: {r.text[:160]!r}")
        if r.status == 429 or b"limit requests" in r.content[:300].lower():
            raise RateLimited()
        return []

    def area_apis(self, j: dict, county: str) -> dict[str, str]:
        """The Georgia outlets with a WordPress news API that cover this department's area: the county's papers in
        config/outlets.yaml, and papers named for the county or the city (daltoncitizen.com, valdostadailytimes.com).
        Their API answers with each story's text, so nothing has to be opened, and it is not GDELT's shared limit."""
        keys = {re.sub(r"[^a-z]", "", x.lower()) for x in (county, re.sub(r"\s+county$", "", j.get("name") or "", flags=re.I)) if x}
        out = {}
        for dom in self.county_outlets.get(county.lower(), []):
            if dom in self.wp:
                out[dom] = self.wp[dom]
        for dom, api in self.wp.items():
            if any(k and len(k) >= 4 and k in dom.replace("-", "") for k in keys):
                out[dom] = api
        return dict(list(out.items())[:4])

    def wp_search(self, phrase: str, apis: dict[str, str]) -> list[dict]:
        """[{url, title, text, date}] from each outlet's own search, last WINDOW_DAYS days."""
        from .fetch import OutOfTime

        after = (dt.date.today() - dt.timedelta(days=WINDOW_DAYS)).isoformat() + "T00:00:00"
        out = []
        for dom, api in apis.items():
            try:
                r = self.f.get(api.rstrip("/") + "/wp/v2/posts", params={"search": phrase, "after": after, "per_page": 20,
                                                                          "_fields": "link,date,title,content"})
            except OutOfTime:
                raise
            except Exception:  # noqa: BLE001
                continue
            if r.status != 200 or not r.content.strip().startswith(b"["):
                continue
            try:
                posts = r.json()
            except ValueError:
                continue
            for p in posts:
                link = p.get("link") or ""
                if not link.startswith("http"):
                    continue
                _, md = extract.html_to_markdown((p.get("content") or {}).get("rendered") or "", link)
                title = re.sub(r"<[^>]+>", "", (p.get("title") or {}).get("rendered") or "")
                out.append({"url": link, "title": title, "text": md or "", "date": (p.get("date") or "")[:10], "outlet": dom})
        return out

    def department(self, jid: str, dept: dict, jname: str, j: dict | None = None) -> int:
        """One query for the department (and its chief); open the trusted hits once; keep the incidents."""
        from .blocked import Blocked
        from .candidates import domain_of
        from .fetch import Disallowed, OutOfTime
        from .police import POLICE_VERSION, department_name, news_phrase

        j = j or {"name": jname, "kind": "county" if jname.endswith("County") else "city"}
        phrase = news_phrase(j, dept)
        name = dept.get("name") if dept.get("v") == POLICE_VERSION or dept.get("listed_by") else department_name(j, dept)[0]
        dept = {**dept, "name": name}
        county = (j.get("counties") or [""])[0] if j.get("kind") != "county" else jname.replace(" County", "")
        phrases = [f'"{phrase}"']
        if dept.get("chief") and dept["chief"].get("name"):
            phrases.append(f'"{dept["chief"]["name"]}"')
        self.gdelt_ok = True
        try:
            hits = [a for a in self.search(phrases) if a.get("url") and self.classify(a["url"])]
        except RateLimited:
            hits, self.gdelt_ok = [], False
            self.notes["gdelt_limited"] = self.notes.get("gdelt_limited", 0) + 1
        n = self._from_outlet_apis(jid, dept, jname, j, phrase, county)
        # stories whose headline names the place first: they are the likeliest to be about this department
        key = phrase.rsplit(" ", 1)[0].split()[0].lower()
        hits.sort(key=lambda a: 0 if key in (a.get("title") or "").lower() else 1)
        for a in hits[:MAX_HITS]:
            if OTHER_STATES.search(a.get("title") or "") and not re.search(r"\bGeorgia\b|\bGa\.", a.get("title") or ""):
                continue                                   # the headline is about another state: not opened
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
            if not _mentions_department(text, dept, jname, phrase) or not CUE.search(text):
                continue
            if not in_georgia(title or a.get("title") or "", text, domain_of(r.url) in self.local or r.url.endswith((".gov", ".ga.us")),
                              jname, county):
                continue
            pub = a.get("seendate", "")
            pub = f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if len(pub) >= 8 else (extract.page_date(r.text, text) or None)
            names = names_in(text)
            cues = sorted({m.group(1).lower() for m in CUE.finditer(text)})[:8]

            rec = Record(f"police:{jid}", "police_incident", r.url, title=(title or a.get("title") or "")[:200],
                         text=excerpt_of(text, dept.get("name", "")), published=pub, channel="html", jurisdiction=jid,
                         evidence=self.classify(r.url) or "reported",
                         meta={"department": dept.get("name"), "department_kind": dept.get("kind"), "outlet": domain_of(r.url),
                               "names": names, "cues": cues, "opened": True, "checked": dt.date.today().isoformat(),
                               "search": phrase, "v": NEWS_VERSION})
            n += self.store.add(rec)
        return n


    def _from_outlet_apis(self, jid: str, dept: dict, jname: str, j: dict, phrase: str, county: str) -> int:
        from .candidates import domain_of

        n = 0
        for a in self.wp_search(phrase, self.area_apis(j, county)):
            if self.store.fresh(a["url"], 3650) or domain_of(a["url"]) != a["outlet"] and not domain_of(a["url"]).endswith("." + a["outlet"]):
                continue
            text = a["text"]
            if not _mentions_department(text, dept, jname, phrase) or not CUE.search(text):
                continue
            if not in_georgia(a["title"], text, True, jname, county):
                continue
            self.store.mark_url(a["url"], 200, f"police:{jid}", "text/html")
            rec = Record(f"police:{jid}", "police_incident", a["url"], title=a["title"][:200], text=excerpt_of(text, dept.get("name", "")),
                         published=a["date"] or None, channel="wp_json", jurisdiction=jid, evidence=self.classify(a["url"]) or "reported",
                         meta={"department": dept.get("name"), "department_kind": dept.get("kind"), "outlet": a["outlet"],
                               "names": names_in(text), "cues": sorted({m.group(1).lower() for m in CUE.finditer(text)})[:8],
                               "opened": True, "via": "outlet news API", "checked": dt.date.today().isoformat(),
                               "search": phrase, "v": NEWS_VERSION})
            n += self.store.add(rec)
        return n


class RateLimited(Exception):
    pass


def research(fetcher, store, root: Path, state_dir: Path, deadline: float | None = None, log=print,
             limit: int | None = None) -> dict:
    """Search the news for every department: the ones read from the governments' own pages and the ones on the FBI's
    roster whose pages are not read yet (a news search needs only the department's name and place), largest places
    first, each once a week."""
    from .fetch import OutOfTime
    from .police import department_list
    from .registry import jurisdictions

    cp = state_dir / "police_news.json"
    checked = json.loads(cp.read_text()) if cp.exists() else {}
    jl = jurisdictions(root)
    jur = {j["id"]: j for j in jl}
    pop = {jid: (j.get("population") or 0, j["name"]) for jid, j in jur.items()}
    if checked.get("_version") != NEWS_VERSION:
        # incidents found by the old searches are dropped and every department is searched again (the articles they
        # came from are unmarked, so the new search may open them again and file them where they belong)
        gone = store.db.execute("DELETE FROM records WHERE kind='police_incident' AND "
                                "coalesce(json_extract(meta, '$.v'), 1) < ?", (NEWS_VERSION,)).rowcount
        store.db.execute("DELETE FROM urls WHERE source_id LIKE 'police:%'")
        store.flush()
        checked = {"_version": NEWS_VERSION}
        log(f"police news v{NEWS_VERSION}: {gone} incidents from the old searches removed; every department is searched again")
    pn = PoliceNews(fetcher, store, root, log)
    done = found = 0
    pairs, _, _ = department_list(root, jl)
    pairs.sort(key=lambda p: -pop.get(p[0]["id"], (0, ""))[0])
    first_kind: dict[str, str] = {}
    for j, d in pairs:
        first_kind.setdefault(j["id"], d.get("kind") or "police")
    for j, d in pairs:
        jid, kind = j["id"], d.get("kind") or "police"
        key = f"{jid}:{kind}"
        # a department searched before this list held more than one per place is keyed by its place alone
        last = checked.get(key) or (checked.get(jid) if first_kind.get(jid) == kind else None) or d.get("news_checked_at")
        if last and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).days < RECHECK_DAYS:
            continue
        if deadline and time.monotonic() > deadline or (limit and done >= limit):
            break
        try:
            n = pn.department(jid, d, pop.get(jid, (0, jid))[1], j)
        except OutOfTime:
            break
        except RateLimited:
            log("  gdelt rate-limited this run; the rest wait for the next run")
            break
        if not getattr(pn, "gdelt_ok", True) and not pn.wp:
            log("  gdelt keeps refusing this runner's address and no outlet API is known; the rest wait for the next run")
            break
        if getattr(pn, "gdelt_ok", True):
            checked[key] = _now()      # a department GDELT refused is searched again next run (outlet APIs found what they could)
        done += 1
        found += n
        if n:
            log(f"{jid} ({kind}): {n} new incident reports")
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
        # one entry per department: a county's sheriff's office and its county police are reported apart
        D = depts.setdefault((jid, meta.get("department_kind") or "police"),
                             {"jurisdiction": jid, "jurisdiction_name": (by_id.get(jid) or {}).get("name", jid),
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
