"""The web lane of candidate research (gacrawl/candidates.py): what GDELT's three-month news window and the official
records never reach. For most local candidates those two are all there was, and three of four profiles stayed empty.

Two routes per candidate, both free and keyless, both under the crawler's usual rules (robots.txt, each host's pace,
the block list; nothing is worked around):

  outlet archives   the WordPress news API of the trusted outlets that cover the candidate's place (the county's papers
                    in config/outlets.yaml `county_outlets`, papers named for the county or city, and the statewide
                    outlets for statewide and legislative offices; API roots in state/wp_outlets.json, kept by bill
                    reasons). One search per outlet for the name as a phrase, back to `since` (2016), not 89 days. The
                    API answers with each story's text, so no page is opened.
  web search        one DuckDuckGo query (gacrawl/websearch.py: cached 30 days, capped a run) for the name, the office
                    and the place. Only hits on sites the project already trusts are opened: official sites (.gov,
                    .ga.us, the place's own site, the candidate's own site) and listed outlets. Everything else is
                    left alone: not opened, not kept, not sent to the Rumor Mill.

A page counts only when the candidate's name is on it AND it is tied to their place: a place name on the page (city,
county, district), or, for the place's own paper, a word of the candidate's office near the name. Records are the same
`article` records the news lane writes (meta.via says which route), so profiles, stances and the export read them
with no changes.

Progress is per candidate (`web:<id>` in state/candidate_progress.json): again after `web_cadence_days` (30). People
with nothing on file go first, then officeholders and recent races. A candidate whose web search could not run (cap
reached, DuckDuckGo failing) is marked `search: skipped` and is due again after a day for the search alone.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path

from . import extract
from .blocked import Blocked
from .fetch import Disallowed, OutOfTime
from .names import excerpts, name_regex, name_variants
from .store import Record

OFFICE_STOP = {"the", "of", "for", "and", "at", "large", "post", "seat", "district", "dist", "ward", "member", "city",
               "county", "state", "georgia", "elected", "board", "no", "to", "in"}
STATE_LEVEL = re.compile(r"\b(state (house|senate|representative|senator|school superintendent)|governor|attorney general|"
                         r"secretary of state|public service|u\.?s\.? (house|senate|representative|senator)|congress|"
                         r"commissioner of (agriculture|insurance|labor)|supreme court|court of appeals)\b", re.I)


def clean_office(office: str) -> str:
    """An office as a person would type it into a search box: 'State Representative, District 33; State House District
    33 (elected Nov. 3, 2026)' -> 'State Representative District 33'. The bracketed notes and the second spelling made
    DuckDuckGo drop the quoted name (Nov 8: 152 of 261 searches came back with nothing naming the person)."""
    o = re.sub(r"\([^)]*\)", " ", office or "")
    o = o.split(";")[0]
    o = re.sub(r"\s+-\s+|[,/]", " ", o)
    return re.sub(r"\s+", " ", o).strip()


def search_query(name: str, office: str = "", place: str = "") -> str:
    office = clean_office(office)
    parts = [f'"{name}"', office]
    if place and place.lower() not in office.lower():
        parts.append(place)
    if "georgia" not in " ".join(parts).lower():
        parts.append("Georgia")
    return " ".join(p for p in parts if p)


def office_words(office: str) -> list[str]:
    """The words of an office that tie a story to it ('council', 'mayor', 'commission'), without the filler."""
    out = []
    for w in re.findall(r"[a-z]{4,}", clean_office(office).lower()):
        if w not in OFFICE_STOP:
            out.append(w[:7] if w.startswith(("council", "commiss", "represent", "senat")) else w)
    return list(dict.fromkeys(out))


class WebLane:
    def __init__(self, researcher, state_dir: Path, search=None):
        self.r = researcher
        self.f, self.store, self.log = researcher.f, researcher.store, researcher.log
        self.opts = researcher.opts
        self.search = search
        self._search_lock = threading.Lock()
        try:
            wp = json.loads((Path(state_dir) / "wp_outlets.json").read_text())
        except (OSError, ValueError):
            wp = {}
        self.wp = {d: v["api"] for d, v in wp.items() if isinstance(v, dict) and v.get("api")}
        try:
            from .registry import load_yaml

            o = load_yaml(researcher.root / "config" / "outlets.yaml") or {}
        except Exception:  # noqa: BLE001
            o = {}
        self.county_outlets = {str(k).lower(): list(v or []) for k, v in (o.get("county_outlets") or {}).items()}
        self.statewide = list(o.get("statewide") or [])
        self.stats = {"candidates": 0, "outlet_searches": 0, "web_searches": 0, "opened": 0, "search_skipped": 0}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ the candidate's place
    def counties(self, c: dict) -> list[str]:
        out = list(c.get("counties") or [])
        for jid in c.get("jurisdictions") or []:
            j = self.r.jurisdictions.get(jid) or {}
            out += [j["county"]] if j.get("county") else list(j.get("counties") or [])
            if j.get("kind") in ("county", "consolidated"):
                out.append(re.sub(r"\s+county$", "", j.get("name") or "", flags=re.I))
        return [x for x in dict.fromkeys(out) if x]

    def place_names(self, c: dict) -> list[str]:
        """City, county and district names: what ties a page to this person. Not the bare word Georgia: a story from
        anywhere in the state about someone of the same name would pass."""
        out = []
        for jid in c.get("jurisdictions") or []:
            j = self.r.jurisdictions.get(jid)
            if j and j.get("name"):
                out.append(j["name"])
        out += [f"{x} County" for x in self.counties(c)]
        m = re.search(r"\bdistrict\s+\d+\b", clean_office(c.get("office") or ""), re.I)
        if m:
            out.append(m.group(0))
        return list(dict.fromkeys(out))

    def apis_for(self, c: dict) -> tuple[dict[str, str], set[str]]:
        """(outlet -> WordPress API root, the local ones among them) for the outlets that cover this candidate:
        local ones first."""
        out: dict[str, str] = {}
        counties = self.counties(c)
        keys = {re.sub(r"[^a-z]", "", x.lower()) for x in counties}
        for jid in c.get("jurisdictions") or []:
            j = self.r.jurisdictions.get(jid) or {}
            if j.get("kind") in ("city", "town", "consolidated"):
                keys.add(re.sub(r"[^a-z]", "", (j.get("name") or "").lower()))
        for county in counties:
            for dom in self.county_outlets.get(county.lower(), []):
                if dom in self.wp:
                    out[dom] = self.wp[dom]
        for dom, api in self.wp.items():
            if any(len(k) >= 5 and k in dom.replace("-", "") for k in keys):
                out.setdefault(dom, api)
        local = set(out)
        if not out or c.get("priority") or STATE_LEVEL.search(c.get("office") or ""):
            for dom in self.statewide:
                if dom in self.wp:
                    out.setdefault(dom, self.wp[dom])
        n = int(self.opts.get("web_outlets_per_candidate", 4))
        picked = dict(list(out.items())[:n])
        return picked, local & set(picked)

    # ------------------------------------------------------------------ is this page about them
    def _about(self, c: dict, text: str, pat: re.Pattern, local_outlet: bool) -> bool:
        m = pat.search(text or "")
        if not m:
            return False
        places = self.place_names(c)
        if places and re.search("|".join(re.escape(p) for p in places), text, re.I):
            return True
        if local_outlet:
            # the place's own paper rarely names the place; the office beside the name ties the story to them
            words = office_words(c.get("office") or "")
            if words:
                near = re.compile("|".join(re.escape(w) for w in words), re.I)
                for m in pat.finditer(text):
                    if near.search(text[max(0, m.start() - 250): m.end() + 250]):
                        return True
        return False

    # ------------------------------------------------------------------ route 1: the outlets' own archives
    def outlet_archive(self, c: dict, add, since: str) -> int:
        from .candidates import domain_of

        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        if not variants:
            return 0
        pat = name_regex(variants)
        apis, local = self.apis_for(c)
        n = 0
        for dom, api in apis.items():
            try:
                r = self.f.get(api.rstrip("/") + "/wp/v2/posts",
                               params={"search": f'"{variants[0]}"', "after": since[:10] + "T00:00:00", "per_page": 20,
                                       "orderby": "date", "_fields": "link,date,title,content"})
            except OutOfTime:
                raise
            except (Disallowed, Blocked):
                continue
            except Exception:  # noqa: BLE001
                continue
            with self._lock:
                self.stats["outlet_searches"] += 1
            if r.status != 200 or not r.content.strip().startswith(b"["):
                continue
            try:
                posts = r.json()
            except ValueError:
                continue
            for p in posts:
                link = p.get("link") or ""
                d = domain_of(link)
                if not link.startswith("http") or not (d == dom or d.endswith("." + dom)):
                    continue      # a story must be on the outlet's own site to count as its reporting
                _, text = extract.html_to_markdown((p.get("content") or {}).get("rendered") or "", link)
                if not self._about(c, text or "", pat, dom in local):
                    continue
                title = re.sub(r"<[^>]+>", "", (p.get("title") or {}).get("rendered") or "")
                add(Record("", "article", link, title=extract_title(title), text="\n".join(excerpts(text, pat, n=3))[:1800],
                           published=(p.get("date") or "")[:10] or None, channel="wp_json", evidence="reported",
                           meta={"outlet": dom, "via": "outlet_archive", "opened": True}))
                n += 1
        return n

    # ------------------------------------------------------------------ route 2: web search, trusted sites only
    def web_search(self, c: dict, cls, add) -> int | None:
        """Articles and official pages added; None when the search could not run (cap, failures, not installed)."""
        if self.search is None:
            return None
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        if not variants:
            return 0
        pat = name_regex(variants)
        places = self.place_names(c)
        j = self.r.jurisdictions.get(c.get("jurisdiction") or "") or {}
        place = j.get("name") or (f"{self.counties(c)[0]} County" if self.counties(c) else "")
        q = search_query(variants[0], c.get("office") or "", place)
        with self._search_lock:
            cached = q.lower() in self.search.st.get("cache", {})
            if not cached and self.search.left() <= 0:
                return None
            hits = self.search.search(q)
            if not cached:
                with self._lock:
                    self.stats["web_searches"] += 1
        from .candidates import domain_of

        # sites that are the place's own: its papers, its government's website, the candidate's own site. Only there
        # does the office beside the name stand in for the place's name (another city's .gov can have a council
        # member of the same name)
        local = set(d for co in self.counties(c) for d in self.county_outlets.get(co.lower(), []))
        for jid in c.get("jurisdictions") or []:
            site = (self.r.jurisdictions.get(jid) or {}).get("website")
            if site:
                local.add(domain_of(site))
        local |= {domain_of(w) for w in c.get("websites") or []}

        n = opened = 0
        for h in hits:
            if opened >= int(self.opts.get("web_pages_per_candidate", 4)):
                break
            url = h.get("url") or ""
            if cls.excluded(url):
                continue
            ev = cls.classify(url)
            if ev not in ("primary", "reported"):
                continue          # not a site the project trusts: left alone
            if self.store.fresh(url, 3650):
                continue          # read before (by the crawl, the news lane or an earlier search)
            if not (pat.search(h.get("title") or "") or pat.search(h.get("snippet") or "")
                    or any(p.lower() in ((h.get("title") or "") + " " + (h.get("snippet") or "")).lower() for p in places)):
                continue          # nothing in the result says it is about them or their place
            page = self.r._open_article(url)
            opened += 1
            with self._lock:
                self.stats["opened"] += 1
            if not page or not self._about(c, page["text"], pat, domain_of(url) in local):
                continue
            add(Record("", "article", page["url"], title=page["title"] or h.get("title") or "",
                       text="\n".join(excerpts(page["text"], pat, n=3))[:1800],
                       published=_page_date(page["text"]),
                       channel="html", evidence=ev,
                       meta={"outlet": domain_of(page["url"]), "via": "web_search", "opened": True, "found_by": q}))
            n += 1
        return n

    # ------------------------------------------------------------------ one candidate
    def one(self, c: dict, cls, add, since: str, search_only: bool = False) -> dict:
        out = {"archive": 0, "web": 0, "search": "ok"}
        if not search_only:
            out["archive"] = self.outlet_archive(c, add, since)
        w = self.web_search(c, cls, add)
        if w is None:
            out["search"] = "skipped"
            with self._lock:
                self.stats["search_skipped"] += 1
        else:
            out["web"] = w
        with self._lock:
            self.stats["candidates"] += 1
        return out

    # ------------------------------------------------------------------ who is due, emptiest first
    def due(self, cands: list[dict], progress, now: dt.datetime) -> list[tuple[dict, bool]]:
        cad = float(self.opts.get("web_cadence_days", 30))
        out = []
        for c in cands:
            if not c.get("name"):
                continue
            p = progress.get(f"web:{c['id']}")
            last = p.get("last_ok")
            age = (now - dt.datetime.fromisoformat(last)).total_seconds() / 86400 if last else None
            if age is None or age > cad:
                out.append((c, False))
            elif p.get("search") == "skipped" and age > 1 and self.search is not None:
                out.append((c, True))        # the outlets were asked; only the web search is owed

        def rank(item):
            c, search_only = item
            found = sum(v for k, v in (progress.get(f"cand:{c['id']}").get("counts") or {}).items()
                        if isinstance(v, int) and k not in ("steps_skipped", "rumor_mill", "lead"))
            found += sum(v for v in (progress.get(f"news:{c['id']}").get("counts") or {}).values() if isinstance(v, int))
            return (search_only,                                    # whole look-ups before search-only repeats
                    min(found, 3),                                  # nothing on file first, then thin
                    not c.get("priority"),
                    -int((c.get("last_race_date") or "0000")[:4] >= str(now.year - 1)),
                    progress.get(f"web:{c['id']}").get("last_ok") or "")
        out.sort(key=rank)
        return out


def _page_date(text: str) -> str | None:
    try:
        return extract.page_date("", text) or None
    except Exception:  # noqa: BLE001
        return None


def extract_title(title: str) -> str:
    import html

    return re.sub(r"\s+", " ", html.unescape(title or "")).strip()[:200]
