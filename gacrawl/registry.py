"""Jurisdiction registry: every Georgia county and municipality, plus whatever
`gacrawl seed` discovered about each one (website, meeting portal, code
library, feeds) and any hand edits in config/overrides.yaml.

Layers, lowest to highest priority:
  config/jurisdictions.yaml   generated registry (name, kind, FIPS, counties, population)
  config/school_districts.yaml the 180 public school districts (kind school_district)
  state/discovered.json       machine discoveries from `gacrawl seed`
  config/overrides.yaml       hand edits: website, portals, enabled, tier, notes

A jurisdiction expands into one crawl source per place its records live:
  <id>:legistar   Legistar API            (council records, votes)
  <id>:boarddocs  BoardDocs               (school boards: meetings, agenda items, minutes)
  <id>:policies   Simbli / eBOARD         (school boards: the policy manual, and meetings where it holds them)
  <id>:agendas    CivicPlus AgendaCenter  (agendas/minutes; RSS)
  <id>:granicus   Granicus                (agendas/minutes; RSS)
  <id>:code       eCode360 / amlegal      (ordinances)
  <id>:site       official website        (pages, PDFs, feeds; auto channel)
Municode links are recorded but not crawled (see blocked.py).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

PORTAL_PATTERNS = {
    "legistar": re.compile(r"https?://([a-z0-9\-]+)\.legistar\.com", re.I),
    "granicus": re.compile(r"https?://([a-z0-9\-]+)\.granicus\.com[^\s\"'<>]*", re.I),
    "civicclerk": re.compile(r"https?://([a-z0-9\-]+)\.portal\.civicclerk\.com", re.I),
    "boarddocs": re.compile(r"https?://go\.boarddocs\.com/[^\s\"'<>]+", re.I),
    # GSBA's eBOARD (Simbli): most Georgia school boards' policy manuals and many boards' meetings
    "simbli": re.compile(r"https?://simbli\.eboardsolutions\.com/[^\s\"'<>]+", re.I),
    "agenda_center": re.compile(r"https?://[^\s\"'<>]+/AgendaCenter[^\s\"'<>]*", re.I),
    "municode": re.compile(r"https?://library\.municode\.com/ga/[a-z0-9_\-]+", re.I),
    "amlegal": re.compile(r"https?://codelibrary\.amlegal\.com/codes/[a-z0-9_\-]+", re.I),
    "ecode360": re.compile(r"https?://(?:www\.)?ecode360\.com/[A-Z]{2}\d{3,5}", re.I),
    "iqm2": re.compile(r"https?://[a-z0-9\-]+\.iqm2\.com[^\s\"'<>]*", re.I),
    "escribe": re.compile(r"https?://pub-[a-z0-9\-]+\.escribemeetings\.com[^\s\"'<>]*", re.I),
    "novusagenda": re.compile(r"https?://[a-z0-9\-]+\.novusagenda\.com[^\s\"'<>]*", re.I),
    "diligent": re.compile(r"https?://[a-z0-9\-]+\.diligent\.com/[^\s\"'<>]*community[^\s\"'<>]*", re.I),
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9.\-_]+/?", re.I),
    # channel links only (@handle, /channel/, /c/, /user/, or a bare youtube.com/name as in a site's social icons),
    # never a single video, playlist or embed
    "youtube": re.compile(r"https?://(?:www\.|m\.)?youtube\.com/(?:@[\w.\-]+|channel/[\w\-]+|c/[\w\-]+|user/[\w\-]+|"
                          r"(?!(?:watch|embed|playlist|shorts|live|results|feed|redirect|t|about|hashtag|channel|c|user|"
                          r"premium|kids|music|gaming|account|signin|logout|s|v|attribution_link)\b)[A-Za-z][\w\-]{2,})"
                          r"(?=[/?\"'#\s<>]|$)", re.I),
}
SOCIAL = {"facebook", "youtube"}
TIER_CADENCE_DAYS = {"a": 1, "b": 7, "c": 30}


_YAML: dict[str, tuple[float, int, object]] = {}
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)   # libyaml: about 8x faster on the big registry files


def yaml_text(text: str):
    return yaml.load(text, Loader=_LOADER)


def load_yaml(path: Path, default=None):
    """Parsed YAML, cached by file modification time (the big registry and candidate files are read by several
    steps of one run); callers get their own copy, so changing the result never changes the cache."""
    if not path.exists():
        return default if default is not None else {}
    st = path.stat()
    hit = _YAML.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        data = hit[2]
    else:
        data = yaml_text(path.read_text(encoding="utf-8"))
        _YAML[str(path)] = (st.st_mtime, st.st_size, data)
    return copy.deepcopy(data) or (default if default is not None else {})


def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # a file cut off mid-write (a run stopped by its time limit): start that state over rather than fail the run
        print(f"warning: {path.name} is not valid JSON; treating it as empty")
        return default if default is not None else {}


def write_json_atomic(path: Path, data, **kw) -> None:
    """Write to a temporary file beside it, then rename: a stop mid-write never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, **kw), encoding="utf-8")
    tmp.replace(path)


def tier_of(j: dict) -> str:
    if j.get("tier"):
        return j["tier"]
    pop = j.get("population") or 0
    if j["kind"] == "school_district":           # boards meet monthly: weekly checks for the big systems
        return "b" if pop >= 20000 else "c"
    if j["kind"] in ("county", "consolidated") or pop >= 10000:
        return "a"
    return "b" if pop >= 1000 else "c"


def school_districts(root: Path) -> list[dict]:
    return list(load_yaml(root / "config" / "school_districts.yaml").get("districts") or [])


def electo_codes(root: Path) -> dict[str, dict]:
    """Code-of-ordinances links Electo opened on official pages (state/electo_codes.json, from `gacrawl electo import`):
    {jurisdiction id: {"portals": {...}, "website_hint": "https://official.host/"}}."""
    out = {}
    for jid, c in load_json(root / "state" / "electo_codes.json").items():
        portals = find_portals(c.get("code_url", ""), c.get("found_on") or c.get("code_url", ""))
        entry = {"portals": {k: v for k, v in portals.items() if k in ("municode", "ecode360", "amlegal")}}
        m = re.match(r"(https?://[^/]+)", c.get("found_on") or "")
        if m:
            entry["website_hint"] = m.group(1) + "/"
        out[jid] = entry
    return out


def jurisdictions(root: Path) -> list[dict]:
    reg = load_yaml(root / "config" / "jurisdictions.yaml")
    discovered = load_json(root / "state" / "discovered.json")
    overrides = load_yaml(root / "config" / "overrides.yaml").get("jurisdictions", {}) or {}
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()   # the block list's week
    blocked = {h: v for h, v in load_json(root / "state" / "blocked.json").items()
               if isinstance(v, dict) and (v.get("last_seen") or "") > cutoff}
    codes = electo_codes(root)
    out = []
    for j in (reg.get("counties", []) + reg.get("municipalities", []) + school_districts(root)):
        j = dict(j)
        d = discovered.get(j["id"], {})
        e = codes.get(j["id"], {})
        j.setdefault("portals", {})
        j.update({k: v for k, v in d.items() if k not in ("portals",)})
        j["portals"] = {**e.get("portals", {}), **d.get("portals", {}), **j["portals"]}
        if e.get("portals", {}).get("municode") and not j.get("municode"):
            j["municode"] = e["portals"]["municode"]
        if not j.get("website") and e.get("website_hint"):
            j["website"], j["website_source"] = e["website_hint"], "electo (official page Electo opened)"
        if e.get("website_hint"):
            j["website_hint"] = e["website_hint"]      # a candidate for the seed when the directories' listings fail
        o = overrides.get(j["id"], {}) or {}
        j["portals"] = {**j["portals"], **(o.pop("portals", {}) or {})}
        j.update(o)
        # which fields a person set by hand (the seed must not take its own earlier finding, merged in above
        # from discovered.json, for a hand-set website and stop looking)
        j["overrides"] = sorted(o)
        # the site the directory lists under another name (GMA's entry, say) when the first one refuses the crawler
        alt = j.get("website_alt")
        if alt and j.get("website") and _host_blocked(j["website"], blocked) and not _host_blocked(alt, blocked):
            j["website"], j["website_source"] = alt, f"{j.get('website_source', 'directory')} alternate ({j['website']} refuses the crawler)"
        j["tier"] = tier_of(j)
        out.append(j)
    return out


def _host_blocked(url: str, blocked: dict) -> bool:
    h = urlsplit(url).netloc.lower()
    return h in blocked or h.removeprefix("www.") in blocked or ("www." + h) in blocked


def by_id(root: Path) -> dict[str, dict]:
    return {j["id"]: j for j in jurisdictions(root)}


def sources_for(j: dict, defaults: dict | None = None) -> list[dict]:
    """Expand one jurisdiction into crawl sources (see module docstring)."""
    if j.get("enabled") is False:
        return []
    base = {"jurisdiction": j["id"], "jurisdiction_name": j["name"], "tier": j["tier"],
            "since": (defaults or {}).get("since", "2016-01-01")}
    srcs = []
    p = j.get("portals", {})
    if p.get("legistar"):
        client = PORTAL_PATTERNS["legistar"].search(p["legistar"])
        client = client.group(1).lower() if client else p["legistar"]
        srcs.append({**base, "id": f"{j['id']}:legistar", "url": f"https://{client}.legistar.com/", "legistar": client,
                     "kind": "council", "channel": "api", "endpoint": f"legistar:{client}"})
    if p.get("civicclerk"):
        cc = PORTAL_PATTERNS["civicclerk"].search(p["civicclerk"])
        client = cc.group(1).lower() if cc else p["civicclerk"]
        srcs.append({**base, "id": f"{j['id']}:civicclerk", "url": f"https://{client}.portal.civicclerk.com",
                     "civicclerk": client, "kind": "meeting", "channel": "api", "endpoint": f"civicclerk:{client}"})
    if p.get("boarddocs"):
        srcs.append({**base, "id": f"{j['id']}:boarddocs", "url": p["boarddocs"], "kind": "meeting", "channel": "api",
                     "endpoint": f"boarddocs:{p['boarddocs']}", "max_items": 120, "max_docs": 40})
    if p.get("simbli") and j["kind"] == "school_district":
        # the policy manual (and meetings) on GSBA's eBOARD: pages render client-side, so the browser channel reads
        # them and follows the portal's own links; a policy page becomes kind `policy`, a meeting page `meeting`
        srcs.append({**base, "id": f"{j['id']}:policies", "url": p["simbli"], "kind": "policy", "render": "browser",
                     "max_pages": 150 if j["tier"] == "b" else 80, "follow": "eboardsolutions.com",
                     "kind_by_url": [["/Policy/", "policy"], ["/Meetings/", "meeting"], ["Meeting", "meeting"]],
                     "exclude": ["login", "Login", "Search", "?print", "Calendar", "Print", "Export"]})
    if p.get("elections"):
        srcs.append({**base, "id": f"{j['id']}:elections", "url": p["elections"], "kind": "election", "depth": 3,
                     "max_pages": 150, "coverage": True,
                     "include": ["result", "qualif", "candidate", "runoff", "ballot", "election", "returns", "canvass",
                                 "referendum", "splost", "precinct", "summary"],
                     "exclude": ["calendar.aspx?", "/search", "login", "?print", "/tag/", "/author/"]})
    if p.get("agenda_center"):
        srcs.append({**base, "id": f"{j['id']}:agendas", "url": p["agenda_center"], "kind": "meeting",
                     "follow_feed_links": True, "include": ["/AgendaCenter/", "/DocumentCenter/", "/Archive"],
                     "max_pages": 150})
    if p.get("granicus"):
        srcs.append({**base, "id": f"{j['id']}:granicus", "url": p["granicus"], "kind": "meeting", "max_pages": 150,
                     "feeds": [_granicus_rss(p["granicus"])] if _granicus_rss(p["granicus"]) else None})
    if p.get("youtube"):
        # why this channel is believed to be the government's own: a hand check, or a link on its official website
        basis = (f"hand-checked: {j['youtube_checked']}" if j.get("youtube_checked") else
                 f"linked from the official website ({j['website']})" if j.get("website") else "")
        srcs.append({**base, "id": f"{j['id']}:youtube", "url": p["youtube"], "kind": "video", "channel": "api",
                     "endpoint": f"youtube:{p['youtube']}", "youtube_basis": basis})
    for key, follow in (("ecode360", "ecode360.com/"), ("amlegal", "/codes/")):
        if p.get(key):
            # Both code libraries render section bodies client-side, so use the
            # headless-browser channel and follow the table-of-contents links.
            srcs.append({**base, "id": f"{j['id']}:code", "url": p[key], "kind": "ordinance", "max_pages": 400,
                         "render": "browser", "follow": follow})
            break
    if j.get("website") and j["kind"] == "school_district":
        srcs.append({**base, "id": f"{j['id']}:site", "url": j["website"], "kind": "page", "depth": 3, "coverage": True,
                     "max_pages": 80 if j["tier"] == "b" else 40,
                     "include": ["board", "agenda", "minute", "meeting", "policy", "policies", "superintendent", "budget",
                                 "millage", "news", "press", "election", "resolution", "district-office", "about",
                                 "video", "transcript", "calendar-of-meetings", "boarddocs", "esplost", "splost"],
                     "exclude": ["calendar.aspx?", "/search", "login", "?print", "/tag/", "/author/", "lunch", "menu",
                                 "athletics", "sports", "staff-directory", "/students", "/parents", "powerschool",
                                 "infinite-campus", "registration", "enroll"]})
    elif j.get("website"):
        srcs.append({**base, "id": f"{j['id']}:site", "url": j["website"], "kind": "page", "depth": 3, "coverage": True,
                     "max_pages": 120 if j["tier"] == "a" else 60,
                     "include": ["council", "commission", "agenda", "minute", "ordinance", "resolution", "meeting",
                                 "mayor", "clerk", "election", "qualif", "result", "runoff", "ballot", "news", "press",
                                 "document", "board", "appoint", "transcript", "video"],
                     "exclude": ["calendar.aspx?", "/search", "login", "?print", "/tag/", "/author/"]})
    for s in srcs:
        s.setdefault("kind", "page")
    return [s for s in srcs if s.get("url")]


def _granicus_rss(url: str) -> str | None:
    m = re.search(r"(https?://[a-z0-9\-]+\.granicus\.com)/ViewPublisher\.php\?view_id=(\d+)", url, re.I)
    if m:
        return f"{m.group(1)}/ViewPublisherRSS.php?view_id={m.group(2)}&mode=agendas"
    return None


def press_sources(root: Path, defaults: dict | None = None) -> list[dict]:
    """Official newsrooms from config/press.yaml, crawled as press releases."""
    out = []
    for p in load_yaml(root / "config" / "press.yaml").get("press") or []:
        if p.get("enabled", True) and p.get("url"):
            out.append({"id": p["id"], "url": p["url"], "kind": "press_release", "tier": p.get("tier", "b"), "depth": 3,
                        "max_pages": p.get("max_pages", 80), "include": p.get("include") or ["press", "news"],
                        "exclude": p.get("exclude") or ["/search", "login", "?print", "/tag/", "/author/", "calendar"],
                        "since": (defaults or {}).get("since", "2016-01-01"), "jurisdiction": "", "level": "state",
                        "jurisdiction_name": p.get("name", p["id"])})
    return out


def guide_sources(root: Path, defaults: dict | None = None) -> list[dict]:
    """Endorsement / scorecard / voter-guide organizations from config/guides.yaml (kind guide)."""
    out = []
    for g in load_yaml(root / "config" / "guides.yaml").get("guides") or []:
        if g.get("enabled", True) and g.get("url"):
            out.append({"id": g["id"], "url": g["url"], "kind": "guide", "tier": g.get("tier", "b"), "depth": 3,
                        "max_pages": g.get("max_pages", 60), "include": g.get("include") or ["endorse", "candidate"],
                        "exclude": g.get("exclude") or ["/search", "login", "?print", "/tag/", "/author/", "donate", "cart", "shop"],
                        "since": (defaults or {}).get("since", "2016-01-01"), "jurisdiction": "", "level": "guide",
                        "jurisdiction_name": g.get("name", g["id"]), "guide_name": g.get("name", g["id"]),
                        "guide_kind": g.get("kind", "")})
    return out


def all_sources(root: Path, defaults: dict | None = None) -> list[dict]:
    """Registry-derived sources plus the newsrooms in config/press.yaml and the hand-written ones in
    config/sources.yaml (which win on id clash)."""
    hand = {s["id"]: s for s in (load_yaml(root / "config" / "sources.yaml").get("sources") or [])}
    out = []
    for j in jurisdictions(root):
        for s in sources_for(j, defaults):
            out.append({**s, **hand.pop(s["id"], {})})
    for s in press_sources(root, defaults) + guide_sources(root, defaults):
        out.append({**s, **hand.pop(s["id"], {})})
    out += [s for s in hand.values() if s.get("enabled", True)]
    return out


def find_portals(html: str, base_url: str) -> dict:
    """Links on an official site that point at a meeting portal, code library or official social account."""
    found = {}
    host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    for name, pat in PORTAL_PATTERNS.items():
        for m in pat.finditer(html):
            url = m.group(0).rstrip("/\\'\"")
            if name == "agenda_center" and urlsplit(url).netloc.lower().removeprefix("www.") != host:
                continue
            if name == "facebook" and re.search(r"facebook\.com/(sharer|share|plugins|dialog|login|tr\b)", url, re.I):
                continue
            found.setdefault(name, url)
            break
    return found
