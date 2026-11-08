"""Discovery guided by the open model (gacrawl/llm.py: Qwen3-8B on the runner's CPU): what Electo lacks, searched for,
opened politely, judged by the model, checked by rules, then added at once when it is as good as the links Electo
already accepts, or listed for the owner's approval.

What it looks for, most useful first:
  people     names in the news the crawler collected (gacrawl news) that Electo does not know yet: the model lists the
             people a story names with their office and place (a name counts only when the story spells it so); a new
             person is added on their own once 3 different trusted outlets name them in the same kind of office, and
             waits for approval until then
  sites      candidates with no website (recent races first) and places with no website listed: their campaign site,
             official pages and trusted outlets' stories about them
How:
  search     DuckDuckGo through the free ddgs library (gacrawl/websearch.py; no key); every
             address comes from a search result or a page the crawler opened, never from a pattern
  open       the crawler's own fetcher: robots.txt, its pace per host, and the block list (a site that refuses the
             crawler is never worked around); Ballotpedia, AI-generated summary portals, Sway groups and the like
             (config/outlets.yaml exclude_domains) are never opened
  judge      the model reads the page: a campaign site, an official page, a news story or something else; is it about
             this person (or place) and this office; and one sentence copied from the page that shows it. The sentence
             must be on the page word for word, and the person's name too, or the page is not used
  add        at once ("auto") only when the page is the same kind and quality as links Electo already accepts:
               - a campaign site: the model says so, the page names the person and the office, the site's address or
                 title carries the person's name (as the campaign sites already accepted do), and it is not a news,
                 government, social or excluded site
               - an official page: a .gov / .ga.us (or the place's own) site naming the person: kept as a primary record
               - a news story from a trusted outlet (config/outlets.yaml) naming the person: kept as a reported record
               - a place's website: a .gov / .ga.us site whose title names the place
             everything else that passes the checks waits in data/seed_review.md for approval
Approve or reject in data/seed_review.md (private Crawl repository): change "[ ]" to "[x]" to approve or to "[r]" to
reject; the next run applies it. Results: state/ai_seeds.json (candidates.py and registry.py read the accepted ones),
records in the store, and every model call in data/train/discover_log.jsonl for training later.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path

from . import extract
from .store import Record

SOURCE = "ai:discover"
AUTO_OUTLETS = 3        # a new person named by this many different trusted outlets, in the same office, is added
OFFICE_WORDS = ("governor", "lieutenant", "attorney general", "secretary of state", "superintendent", "commissioner",
                "commission", "council", "mayor", "senate", "senator", "house", "representative", "congress", "sheriff",
                "judge", "justice", "district attorney", "solicitor", "clerk", "coroner", "tax commissioner",
                "board of education", "school board", "public service commission", "chair", "probate", "magistrate")
VERSION = 1
RETRY_DAYS = 30
SOCIAL = ("facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "linkedin.com", "threads.net",
          "nextdoor.com", "bsky.app")
HOSTING = ("wixsite.com", "squarespace.com", "godaddysites.com", "weebly.com", "wordpress.com", "carrd.co", "nationbuilder.com",
           "square.site", "webflow.io")
NEWSY = re.compile(r"(news|times|herald|journal|gazette|tribune|ledger|chronicle|observer|post|press|daily|wsb|wxia|fox|cbs|nbc|abc)", re.I)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def _dom(u: str) -> str:
    from .candidates import domain_of

    return domain_of(u)


def _office_words(office: str) -> set[str]:
    """The defining words of an office as a story states it ("Fulton County Commission chair" -> commission, chair)."""
    o = (office or "").lower()
    w = {k for k in OFFICE_WORDS if k in o}
    if "senator" in w or "senate" in w:
        w |= {"senate"}; w.discard("senator")
    if "representative" in w:
        w |= {"house"}; w.discard("representative")
    if "commissioner" in w:
        w |= {"commission"}; w.discard("commissioner")
    if "school board" in w:
        w |= {"board of education"}; w.discard("school board")
    return w


# Scope (the owner's rule): a new person is only someone who holds, won, lost or seeks a government office in Georgia.
# Party officials, staff, consultants, activists and federal officials from elsewhere are not candidates or officials
# Electo covers, and a place outside Georgia is out of scope: such names are dropped, never listed or added.
ROLE_OUT = ("party", "gop", "spokes", "aide", "staffer", "chief of staff", "consultant", "lobbyist", "activist",
            "strategist", "donor", "white house", "u.s. president", "president of the united states", "vice president",
            "presidential", "cabinet", "ambassador", "press secretary", "campaign manager", "pastor", "ceo", "executive director")
OTHER_STATES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
                "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
                "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
                "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma",
                "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
                "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia", "d.c.",
                "puerto rico"}
GA_WHOLE = {"", "georgia", "ga", "statewide", "state of georgia", "georgia statewide"}


def _place_key(place: str) -> str:
    p = _norm(place).rstrip(".")
    for suf in (", georgia", ", ga", " georgia", " ga"):
        if p.endswith(suf) and p != suf.strip(", "):
            p = p[: -len(suf)].strip(" ,")
    return p


def scope_check(office: str, place: str, ga_places: set[str] | None) -> tuple[str, str]:
    """('out', why): never list or add; ('wait', why): in scope but not clearly, so the owner decides; ('ok', '')."""
    o = _norm(office)
    words = _office_words(office) - {"chair", "lieutenant"}
    if any(r in o for r in ROLE_OUT):
        return "out", "not a government office Electo covers"
    if not words:
        return "out", "no government office named"
    raw = _norm(place)
    tail = raw.rsplit(",", 1)[-1].strip(" .")
    pk = _place_key(place)
    in_ga = pk in GA_WHOLE or (ga_places is not None and pk in ga_places)
    if not in_ga and (tail in OTHER_STATES or pk in OTHER_STATES or any(f"{st} " in o and "georgia" not in o
                                                                         for st in OTHER_STATES if len(st) > 6)):
        return "out", "outside Georgia"
    if not in_ga:
        return "wait", f"place \"{place}\" is not one Electo knows in Georgia"
    return "ok", ""


def _gov(d: str) -> bool:
    return d.endswith((".gov", ".ga.us", ".mil")) or d.endswith(".ga.gov")


JUDGE_SYSTEM = ("You check web pages for a Georgia civic information site. You are careful and literal: you answer only from "
                "the page text you are given, and you copy sentences exactly.")
JUDGE_ASK = """Who we are looking for: {who}

Page address: {url}
Page title: {title}
Page text (the start):
{text}

Answer with one JSON object and nothing else:
{{"kind": "campaign" | "official" | "news" | "other",
  "about": true if the page is about {name} specifically (not someone else with a similar name),
  "office": true if the page shows {name} with the office or place given above,
  "quote": one sentence copied exactly from the page text that shows who this is (or "")}}
"campaign" = the person's own campaign or personal site; "official" = a government body's own page; "news" = a news story."""

PEOPLE_SYSTEM = ("You list the people Georgia news stories name in government and politics roles. You copy names exactly as "
                 "written and never guess an office the text does not state.")
PEOPLE_ASK = """Stories:
{stories}

List every person these stories name who holds, won, lost or seeks a government office in Georgia (state, county, city,
school board, courts, Congress for Georgia). One JSON object per line and nothing else:
{{"story": 1, "name": "First Last", "office": "office as the story states it", "place": "county or city, or Georgia"}}
If a story names nobody like that, write nothing for it."""


class Discovery:
    def __init__(self, fetcher, store, root: Path, state_dir: Path, search, log=print, llm=None):
        from .candidates import load_candidates, load_outlets

        self.f, self.store, self.root, self.state_dir, self.search, self.log = fetcher, store, root, state_dir, search, log
        if llm is None:
            from . import llm as _llm
            llm = _llm
        self.llm = llm
        self.outlets, self.exclude = load_outlets(root)
        self.p = state_dir / "ai_seeds.json"
        try:
            self.st = json.loads(self.p.read_text())
        except (OSError, ValueError):
            self.st = {}
        self.st.setdefault("v", VERSION); self.st.setdefault("items", {}); self.st.setdefault("tried", {})
        self.review_p = root / "data" / "seed_review.md"
        self.log_p = root / "data" / "train" / "discover_log.jsonl"
        self.cands = load_candidates(root)
        self.known = set()
        for c in self.cands:
            for v in [c.get("name", "")] + list(c.get("aliases") or []) + list(c.get("variants") or []):
                if v:
                    self.known.add(_norm(v))
        for it in self.st["items"].values():
            if it.get("kind") == "new_person":
                self.known.add(_norm(it.get("target", "")))
        self.stats = {"searches": 0, "opened": 0, "judged": 0, "auto": 0, "pending": 0, "dropped": 0, "people": 0,
                      "out_of_scope": 0}
        self._ga = None
        self.rescope()

    def ga_places(self) -> set[str] | None:
        """Georgia's counties, cities, towns and school districts as Electo knows them (None when the list can't load)."""
        if self._ga is None:
            try:
                from .registry import jurisdictions

                g = set()
                for j in jurisdictions(self.root):
                    nm = _norm(j.get("name", ""))
                    if not nm:
                        continue
                    g.add(nm)
                    if j.get("kind") == "county":
                        g.add(nm + " county" if not nm.endswith(" county") else nm)
                        g.add(nm.removesuffix(" county"))
                    else:
                        g.update({"city of " + nm, "town of " + nm})
                self._ga = g
            except Exception:  # noqa: BLE001
                self._ga = set()
        return self._ga or None

    def rescope(self) -> None:
        """Apply the scope rule to people found before it: out of scope -> never used; added on their own but not clearly
        in Georgia -> back to waiting. The owner's own approvals and rejections are left alone."""
        for it in self.st["items"].values():
            if it.get("kind") != "new_person" or it.get("status") not in ("pending", "auto"):
                continue
            how, why = scope_check(it.get("office", ""), it.get("place", ""), self.ga_places())
            if how == "out":
                it["status"], it["why"] = "out_of_scope", why
            elif how == "wait" and it["status"] == "auto":
                it["status"], it["why"] = "pending", why

    # ------------------------------------------------------------ the owner's decisions
    def apply_decisions(self, path: Path | None = None) -> int:
        p = path or self.review_p
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            return 0
        n = 0
        for m in re.finditer(r"^- \[([ xXrR])\]\s+`([0-9a-f]{10})`", txt, re.M):
            mark, iid = m.group(1).lower(), m.group(2)
            it = self.st["items"].get(iid)
            if not it or mark == " ":
                continue
            new = "approved" if mark == "x" else "rejected"
            if it.get("status") == "pending" or (it.get("status") in ("auto", "approved") and new == "rejected"):
                it["status"] = new
            else:
                continue
            it["decided"] = _now()
            n += 1
            self.llm.log(self.log_p, {"task": "decision", "item": iid, "kind": it.get("kind"), "decision": it["status"],
                                      "url": it.get("url"), "target": it.get("target")})
        return n

    # ------------------------------------------------------------ what Electo lacks
    def _tried(self, key: str) -> bool:
        t = self.st["tried"].get(key)
        return bool(t) and (dt.date.today() - dt.date.fromisoformat(t)).days < RETRY_DAYS

    def gaps(self, limit: int = 60) -> list[dict]:
        from .registry import jurisdictions

        out = []
        cs = [c for c in self.cands if not c.get("websites") and c.get("name") and not self._tried("c:" + c["id"])]
        cs.sort(key=lambda c: (not c.get("priority"), c.get("last_race_date") or ""), reverse=False)
        cs.sort(key=lambda c: c.get("last_race_date") or "", reverse=True)
        for c in cs[:limit]:
            place = ", ".join((c.get("counties") or [])[:1]) or "Georgia"
            out.append({"type": "candidate", "key": "c:" + c["id"], "id": c["id"], "name": c["name"], "office": c.get("office") or "",
                        "place": place, "q": f'"{c["name"]}" {c.get("office") or ""} {place if place != "Georgia" else ""} Georgia'})
        try:
            js = [j for j in jurisdictions(self.root) if j.get("kind") in ("county", "city", "town", "consolidated")
                  and not j.get("website") and not j.get("enabled") is False and not self._tried("j:" + j["id"])]
        except Exception:  # noqa: BLE001
            js = []
        js.sort(key=lambda j: -(j.get("population") or 0))
        for j in js[: max(5, limit // 6)]:
            nm = j["name"] if j["kind"] == "county" else f"City of {j['name']}"
            out.append({"type": "place", "key": "j:" + j["id"], "id": j["id"], "name": j["name"], "office": "", "place": nm,
                        "q": f'"{nm}" Georgia official website'})
        return out

    # ------------------------------------------------------------ new people in the news
    def news_people(self, days: int = 14, batch: int = 8, max_batches: int = 6) -> list[dict]:
        floor = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = [r for r in self.store.iter_query("kind='news_item'") if (r.get("published") or "") >= floor
                and not self._tried("n:" + r["url"])]
        rows.sort(key=lambda r: r.get("published") or "", reverse=True)
        found = []
        for b in range(0, min(len(rows), batch * max_batches), batch):
            part = rows[b:b + batch]
            stories = "\n".join(f"{i + 1}. {r.get('title')}. {r.get('text') or ''}" for i, r in enumerate(part))
            msgs = [{"role": "system", "content": PEOPLE_SYSTEM}, {"role": "user", "content": PEOPLE_ASK.format(stories=stories)}]
            try:
                out = self.llm.chat(msgs, max_tokens=500)
            except Exception as e:  # noqa: BLE001
                self.log(f"discover: the model failed: {type(e).__name__}: {str(e)[:120]}")
                break
            got = []
            for line in out.splitlines():
                line = line.strip().strip(",")
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                i = d.get("story")
                nm = " ".join(str(d.get("name") or "").split())
                if not isinstance(i, int) or not (1 <= i <= len(part)) or len(nm.split()) < 2:
                    continue
                r = part[i - 1]
                hay = f"{r.get('title')} {r.get('text') or ''}"
                if nm.lower() not in hay.lower():
                    continue      # a name the story does not spell that way
                got.append({"name": nm, "office": str(d.get("office") or "")[:120], "place": str(d.get("place") or "")[:60],
                            "story": r["url"], "story_title": r.get("title") or "",
                            "outlet": ((r.get("meta") or {}) if isinstance(r.get("meta"), dict) else {}).get("outlet")
                            or _dom(r["url"])})
            self.llm.log(self.log_p, {"task": "news_people", "input": msgs, "output": out, "kept": got})
            for r in part:
                self.st["tried"]["n:" + r["url"]] = dt.date.today().isoformat()
            for g in got:
                iid = _id("new_person", _norm(g["name"]))
                it = self.st["items"].get(iid)
                if it is None:
                    if _norm(g["name"]) in self.known:
                        continue          # Electo already has this person
                    how, why = scope_check(g["office"], g["place"], self.ga_places())
                    if how == "out":
                        self.stats["out_of_scope"] += 1
                        self.llm.log(self.log_p, {"task": "decision", "kind": "new_person", "decision": "out_of_scope",
                                                  "target": g["name"], "office": g["office"], "place": g["place"], "why": why})
                        continue          # not someone Electo covers: never listed, never added
                    it = self.st["items"][iid] = {"id": iid, "kind": "new_person", "target": g["name"], "office": g["office"],
                                                  "place": g["place"], "url": g["story"], "title": g["story_title"],
                                                  "quote": "", "status": "pending", "at": _now(), "why": "named in the news",
                                                  "sources": []}
                    self.known.add(_norm(g["name"]))
                    self.stats["people"] += 1; self.stats["pending"] += 1
                    found.append(g)
                src = it.setdefault("sources", [])
                if g["outlet"] in self.outlets_set() and not any(x["u"] == g["story"] for x in src):
                    src.append({"o": g["outlet"], "u": g["story"], "office": g["office"], "place": g["place"]})
                self._auto_person(it)
        return found

    def outlets_set(self) -> set:
        return {o.lower() for o in self.outlets}

    def _auto_person(self, it: dict) -> None:
        """A new person is added on their own once AUTO_OUTLETS different trusted outlets (config/outlets.yaml) name
        them in the same kind of office (their office descriptions share a defining word: commission, council,
        senate, house, mayor ...); until then, and whenever the outlets disagree, they wait for approval."""
        if it.get("status") != "pending":
            return
        src = it.get("sources") or []
        office = it.get("office") or next((x.get("office") for x in src if x.get("office")), "")
        place = it.get("place") or next((x.get("place") for x in src if x.get("place")), "")
        how, why = scope_check(office, place, self.ga_places())
        if how != "ok":
            it["why"] = why
            if how == "out":
                it["status"] = "out_of_scope"
            return
        by = {}
        for x in it.get("sources") or []:
            by.setdefault(x["o"], set()).update(_office_words(x.get("office", "")))
        if len(by) < AUTO_OUTLETS:
            it["why"] = f"named by {len(by)} trusted outlet{'' if len(by) == 1 else 's'} so far ({AUTO_OUTLETS} add a person on their own)"
            return
        common = set.intersection(*by.values()) if by else set()
        if not common:
            it["why"] = f"{len(by)} trusted outlets name them, but not in the same office"
            return
        it["status"] = "auto"
        it["why"] = f"{len(by)} trusted outlets name them as {', '.join(sorted(common))}: " + ", ".join(sorted(by))
        self.stats["auto"] += 1; self.stats["pending"] -= 1
        self.llm.log(self.log_p, {"task": "decision", "item": it["id"], "kind": "new_person", "decision": "auto",
                                  "target": it["target"], "sources": it.get("sources")})

    # ------------------------------------------------------------ one page: open, judge, check, decide
    def _open(self, url: str):
        try:
            r = self.f.get(url)
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}"
        if r.status != 200 or "html" not in (r.content_type or ""):
            return None, f"HTTP {r.status}"
        soup = extract.soup_of(r.text)
        title = " ".join((soup.title.get_text(" ") if soup.title else "").split())[:200]
        h1 = " ".join((soup.find("h1").get_text(" ") if soup.find("h1") else "").split())[:200]
        text = extract.main_text(r.text)
        self.stats["opened"] += 1
        return {"url": r.url or url, "title": title, "h1": h1, "text": text}, ""

    def judge(self, task: dict, page: dict) -> dict:
        who = (f"{task['name']}, {task['office']}, {task['place']}" if task["type"] == "candidate"
               else f"the government of {task['place']}, Georgia (its official website)")
        msgs = [{"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": JUDGE_ASK.format(who=who, url=page["url"], title=page["title"], text=page["text"][:2500],
                                                             name=task["name"])}]
        out = self.llm.chat(msgs, max_tokens=200)
        m = re.search(r"\{.*\}", out, re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
        self.stats["judged"] += 1
        return {"msgs": msgs, "out": out, "kind": str(d.get("kind") or "other"), "about": bool(d.get("about")),
                "office": bool(d.get("office")), "quote": " ".join(str(d.get("quote") or "").split())[:400]}

    def decide(self, task: dict, page: dict, j: dict) -> tuple[str, str, list[str]]:
        """(what it is, auto | pending | drop, the checks that failed)."""
        from .candidates import domain_of
        from .names import name_regex, name_variants

        d = domain_of(page["url"])
        fails = []
        hay = _norm(page["title"] + " " + page["h1"] + " " + page["text"])
        if j["quote"] and _norm(j["quote"]) not in hay:
            fails.append("the quoted sentence is not on the page")
        if not j["quote"]:
            fails.append("no sentence quoted")
        if not j["about"]:
            fails.append("the model says the page is not about them")
        if task["type"] == "candidate":
            rx = name_regex(name_variants(task["name"], []))
            if not rx.search(page["title"] + " " + page["h1"] + " " + page["text"]):
                fails.append("the page does not name the person")
        else:
            if task["name"].lower() not in hay:
                fails.append("the page does not name the place")
        if fails:
            return "", "drop", fails
        kind = j["kind"]
        if _gov(d):
            what = "official_page" if task["type"] == "candidate" else "place_site"
            if task["type"] == "place" and task["name"].lower() not in _norm(page["title"] + " " + page["h1"]):
                return what, "pending", ["the title does not name the place"]
            return what, "auto", []
        if any(d == o or d.endswith("." + o) for o in self.outlets):
            return ("news", "auto", []) if task["type"] == "candidate" else ("news", "drop", ["a news site is not a place's website"])
        if task["type"] == "place":
            return "place_site", "pending", ["not a .gov or .ga.us address"]
        if kind != "campaign":
            return "", "drop", [f"the model calls it {kind}, from an outlet this site does not list"]
        why = []
        if d.endswith(SOCIAL) or any(d == s for s in SOCIAL):
            return "", "drop", ["a social media page (verified accounts are handled elsewhere)"]
        if NEWSY.search(d):
            why.append("the address looks like a news site's")
        if d.endswith(HOSTING):
            why.append("a free site builder's address (the accepted campaign sites have their own)")
        last = re.sub(r"[^a-z]", "", task["name"].split()[-1].lower())
        in_addr = len(last) >= 3 and last in d.replace("-", "")
        in_title = bool(re.search(re.escape(task["name"].split()[-1]), page["title"] + " " + page["h1"], re.I))
        if not (in_addr or in_title):
            why.append("neither the address nor the title carries the person's name")
        if not j["office"]:
            why.append("the model could not tie the page to this office")
        return "campaign_site", ("pending" if why else "auto"), why

    def keep(self, task: dict, page: dict, j: dict, what: str, how: str, why: list[str], query: str) -> None:
        iid = _id(what, task["key"], page["url"])
        if iid in self.st["items"]:
            return
        self.st["items"][iid] = {"id": iid, "kind": what, "target": task["id"], "name": task["name"], "office": task["office"],
                                 "place": task["place"], "url": page["url"], "title": page["title"], "quote": j["quote"],
                                 "status": how, "why": "; ".join(why), "query": query, "at": _now(), "model": j["kind"]}
        self.stats["auto" if how == "auto" else "pending"] += 1
        if what in ("official_page", "news"):
            # information about the person: a record the profile research finds like any other (it names them)
            ev = "primary" if what == "official_page" else "reported"
            self.store.add(Record(SOURCE, "article", page["url"], title=page["title"], text=page["text"][:20000],
                                  channel="html", evidence=ev, key=f"{SOURCE}|{page['url']}",
                                  meta={"candidate_id": task["id"], "quote": j["quote"], "opened": True, "found_by": query,
                                        "approved": how == "auto"}))

    # ------------------------------------------------------------ the run
    def run(self, deadline: float, max_pages: int = 4) -> dict:
        n = self.apply_decisions()
        if n:
            self.log(f"discover: applied {n} of the owner's decisions")
        try:
            self.news_people()
        except Exception as e:  # noqa: BLE001
            self.log(f"discover: news people: {type(e).__name__}: {str(e)[:150]}")
        for task in self.gaps():
            if time.monotonic() > deadline or self.search.left() <= 0:
                break
            res = self.search.search(task["q"])
            self.stats["searches"] += 1
            self.st["tried"][task["key"]] = dt.date.today().isoformat()
            for hit in res[:max_pages]:
                if time.monotonic() > deadline:
                    break
                page, err = self._open(hit["url"])
                if not page:
                    continue
                try:
                    j = self.judge(task, page)
                except Exception as e:  # noqa: BLE001
                    self.log(f"discover: the model failed: {type(e).__name__}: {str(e)[:120]}")
                    return self.finish()
                what, how, why = self.decide(task, page, j)
                self.llm.log(self.log_p, {"task": "judge_page", "target": task["key"], "url": page["url"], "input": j["msgs"],
                                          "output": j["out"], "decision": how, "what": what, "why": why})
                if how == "drop":
                    self.stats["dropped"] += 1
                    continue
                self.keep(task, page, j, what, how, why, task["q"])
                if what == "campaign_site" and how == "auto":
                    break     # one campaign site per person is enough
        return self.finish()

    def finish(self) -> dict:
        try:
            self.p.write_text(json.dumps(self.st, ensure_ascii=False, indent=0))
        except OSError:
            pass
        self.search.save()
        self.write_review()
        self.store.flush()
        return self.stats

    def write_review(self) -> None:
        items = sorted(self.st["items"].values(), key=lambda x: x.get("at", ""), reverse=True)
        pend = [x for x in items if x["status"] == "pending"]
        auto = [x for x in items if x["status"] == "auto"][:60]
        label = {"new_person": "new person in the news", "campaign_site": "campaign site", "place_site": "place's website",
                 "official_page": "official page", "news": "news story"}
        L = [f"# Discovery review ({dt.date.today().isoformat()})", "",
             "Found by the open model's discovery step (gacrawl/discover_ai.py). Change `[ ]` to `[x]` to approve or to `[r]` "
             "to reject, then commit; the next run applies it. Everything here passed the checks (the page names the person "
             "or place and the quoted sentence is on it) but is not the same kind as links Electo already accepts, so it waits "
             "for you. Approved new people become candidates the crawler researches.", "",
             f"## Waiting for approval ({len(pend)})", ""]
        for x in pend:
            who = x.get("name") or x.get("target")
            L.append(f"- [ ] `{x['id']}` **{label.get(x['kind'], x['kind'])}** · {who}"
                     + (f" ({x['office']}" + (f", {x['place']}" if x.get("place") else "") + ")" if x.get("office") else "")
                     + f" · {x.get('url', '')}" + (f" · \"{x['quote'][:200]}\"" if x.get("quote") else "")
                     + (f" · {x['title'][:120]}" if x.get("title") and x["kind"] == "new_person" else "")
                     + (f" · why it waits: {x['why']}" if x.get("why") else ""))
        L += ["", f"## Added on their own, newest first ({sum(1 for x in items if x['status'] == 'auto')} in all)", "",
              "The same kind and quality as links Electo already accepts. To take one back, change `[x]` to `[r]`.", ""]
        for x in auto:
            L.append(f"- [x] `{x['id']}` {label.get(x['kind'], x['kind'])} · {x.get('name') or x.get('target')} · {x.get('url', '')}" + (f" · {x['why']}" if x.get('kind') == 'new_person' and x.get('why') else ""))
        self.review_p.parent.mkdir(parents=True, exist_ok=True)
        self.review_p.write_text("\n".join(L) + "\n", encoding="utf-8")


def accepted(state_dir: Path) -> list[dict]:
    """The items candidates.py and registry.py use: added on their own or approved, and not taken back."""
    try:
        st = json.loads((Path(state_dir) / "ai_seeds.json").read_text())
    except (OSError, ValueError):
        return []
    return [x for x in st.get("items", {}).values() if x.get("status") in ("auto", "approved")]
