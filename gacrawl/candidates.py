"""Candidate research at scale: statements, articles, official-record mentions,
votes, social posts and connections for every candidate, with every item graded
by evidence and unverified material routed to the Rumor Mill.

Candidates come from config/candidates.yaml (hand-written) plus every Electo
person imported with `gacrawl electo import` (state/electo_people.json).

Evidence levels
  primary     an official record (council vote, agenda item, ordinance, minutes,
              campaign-finance filing) or the candidate's own channel (campaign
              site, verified social account)
  reported    a news outlet listed in config/outlets.yaml
  unverified  anything else. Kept in the Rumor Mill (data/rumor_mill/), never in a
              profile, until corroborated (`gacrawl rumor`)
  lead        found in a search index but NOT opened (blocked, paywalled, robots,
              redirect-only links). Never cited; listed so a researcher can open it.

Where things come from (cost per candidate in brackets)
  mentions        full-text search over every crawled official record  [no network]
  votes           Legistar roll calls already in the database            [no network]
  posts           verified social accounts (Bluesky / YouTube / X APIs)  [only if accounts known]
  statements      campaign websites                                      [only if sites known]
  articles        GDELT news index, several names per query; each article opened and checked
                  for the name AND the place before it counts; only excerpts are kept  [shared]
  leads           Google News RSS (links can't be opened directly)       [1 request]
  social_mentions Bluesky public search                                   [1-3 requests]
  connections     endorsements on the campaign site; imports/finance/<id>.csv; FEC (federal)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import yaml

from . import channels, extract
from .blocked import Blocked
from .feeds import parse_feed
from .fetch import Disallowed, Fetcher, OutOfTime
from .names import excerpts, name_regex, name_variants
from .registry import by_id
from .store import Record, Store, utcnow
from .verified_posts import Registry, handle_of, platform_of

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS = "https://news.google.com/rss/search"
BSKY_SEARCH = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
FEC = "https://api.open.fec.gov/v1"
STEPS = ("mentions", "votes", "sponsorships", "posts", "statements", "articles", "leads", "social_mentions", "connections")
GDELT_WINDOW_DAYS = 89      # the DOC API searches a rolling ~3-month window
GDELT_NAMES_PER_QUERY = 6
CAMPAIGN_HOT = re.compile(r"issue|platform|priorit|about|bio|meet|record|accomplish|endorse|support|news|press|statement|"
                          r"plan|vision|why|position|policy|values|experience|leadership|community", re.I)


def _chunks_by_place(order: list[dict], size: int) -> list[list[dict]]:
    """Cut an ordered candidate list into chunks of up to `size` from the same place (jurisdiction, else first
    county, else the state), keeping the order: a chunk sits where its first member sat."""
    def place(c):
        return c.get("jurisdiction") or "-".join(c.get("counties", [])[:1]) or "ga"
    open_: dict[str, list[dict]] = {}
    chunks: list[list[dict]] = []
    for c in order:
        p = place(c)
        cur = open_.get(p)
        if cur is None:
            cur = open_[p] = []
            chunks.append(cur)
        cur.append(c)
        if len(cur) >= size:
            open_.pop(p)
    return [ch for ch in chunks if ch]


# ----------------------------------------------------------------------------- config
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_candidates(root: Path, cfg: dict | None = None, include_electo: bool = True) -> list[dict]:
    """Hand-written candidates (config/candidates.yaml) + imported Electo people; hand entries win."""
    out: dict[str, dict] = {}
    if include_electo:
        from .electo import candidate_from_person, electo_people

        sel = (cfg or {}).get("electo", {})
        levels = set(sel.get("levels") or [])
        min_year = str(sel.get("min_last_race_year") or "")
        for p in electo_people(root).values():
            if levels and not levels & set(p.get("levels", [])):
                continue
            if min_year and (p.get("last_race") or {}).get("date", "")[:4] < min_year:
                continue
            c = candidate_from_person(p)
            out[c["id"]] = c
    # school people (board members, superintendents, principals: `gacrawl schools people`) are researched like
    # everyone else; one who also ran for office keeps the Electo person's entry and gains the school role
    from .schools import people_as_candidates

    for c in people_as_candidates(root):
        ek = c.get("electo_key")
        hit = next((o for o in out.values() if ek and o.get("electo_key") == ek), None) if ek else None
        if hit:
            hit["school"] = c["school"]
            hit["jurisdictions"] = list(dict.fromkeys((hit.get("jurisdictions") or []) + c["jurisdictions"]))
            hit["websites"] = list(dict.fromkeys((hit.get("websites") or []) + c["websites"]))
            hit["levels"] = list(dict.fromkeys((hit.get("levels") or []) + ["school"]))
        else:
            out[c["id"]] = c
    p = root / "config" / "candidates.yaml"
    from .registry import load_yaml

    hand = (load_yaml(p) or {}).get("candidates") or []
    for c in hand:
        c = dict(c)
        c["id"] = c.get("id") or (_slug(c["electo_key"]) if c.get("electo_key") else _slug(c["name"]))
        base = out.get(c["id"], {})
        merged = {**base, **{k: v for k, v in c.items() if v not in (None, "", [])}}
        for k in ("websites", "social", "aliases"):  # union list fields
            merged[k] = list(dict.fromkeys((base.get(k) or []) + (c.get(k) or [])))
        out[c["id"]] = merged
    for c in out.values():
        for k in ("aliases", "websites", "social", "news_feeds", "jurisdictions", "profile_urls"):
            c.setdefault(k, [])
        if not c["jurisdictions"] and c.get("jurisdiction"):
            c["jurisdictions"] = [c["jurisdiction"]]
        c["variants"] = c.get("variants") or name_variants(c["name"], c["aliases"])
    return list(out.values())


def load_outlets(root: Path) -> tuple[set[str], set[str]]:
    p = root / "config" / "outlets.yaml"
    from .registry import load_yaml

    d = load_yaml(p)
    return set(d.get("outlets") or []), set(d.get("exclude_domains") or [])


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("amp.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])  # e.g. example.co.uk
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _in(domain: str, domains: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


class Classifier:
    def __init__(self, root: Path, candidate: dict, jurisdiction: dict | None, registry: Registry,
                 outlets: tuple[set, set] | None = None, extra_official: set[str] | None = None):
        self.outlets, self.exclude = outlets or load_outlets(root)
        self.official = {"legistar.com", "granicus.com", "civicclerk.com", "municode.com", "ecode360.com", "amlegal.com"}
        self.official |= extra_official or set()
        if jurisdiction and jurisdiction.get("website"):
            self.official.add(domain_of(jurisdiction["website"]))
        self.own = {domain_of(w) for w in candidate.get("websites", [])}
        self.verified_accounts = {(a["platform"], a.get("handle")) for a in registry.accounts() if a.get("status") == "verified"
                                  and a.get("candidate_id") == candidate["id"]}

    def excluded(self, url: str) -> bool:
        return _in(domain_of(url), self.exclude)

    def classify(self, url: str) -> str:
        d = domain_of(url)
        if d.endswith((".gov", ".ga.us", ".mil")) or _in(d, self.official) or _in(d, self.own):
            return "primary"
        plat = platform_of(url)
        if plat:
            return "primary" if (plat, handle_of(url)) in self.verified_accounts else "unverified"
        if _in(d, self.outlets):
            return "reported"
        return "unverified"


# ----------------------------------------------------------------------------- rumor mill
class RumorMill:
    """Unverified claims about candidates, kept apart from everything verified.
    Rows are cached in memory and written on flush (rewriting the file per claim doesn't scale)."""

    def __init__(self, root: Path):
        self.path = root / "data" / "rumor_mill" / "rumors.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rows: list[dict] | None = None
        self._index: dict[tuple, dict] = {}
        self._dirty = False
        self._lock = threading.RLock()

    def _load(self) -> list[dict]:
        if self._rows is None:
            self._rows = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()] \
                if self.path.exists() else []
            self._index = {(r["candidate_id"], r["url"].strip().lower()): r for r in self._rows}
        return self._rows

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._load())

    def flush(self):
        with self._lock:
            if self._dirty and self._rows is not None:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self._rows), encoding="utf-8")
                tmp.replace(self.path)
                self._dirty = False

    def add(self, candidate_id: str, url: str, claim: str, origin: str, seen_at: str | None = None,
            author: str = "", meta: dict | None = None, opened: bool = False) -> dict:
        with self._lock:
            rows = self._load()
            k = (candidate_id, url.strip().lower())
            row = self._index.get(k)
            if row:
                row["last_seen"] = utcnow()
                row["claim"] = row["claim"] or claim[:1000]
                row["opened"] = row.get("opened", False) or opened
            else:
                row = {"id": f"r{len(rows) + 1:05d}", "candidate_id": candidate_id, "url": url, "domain": domain_of(url),
                       "claim": claim[:1000], "origin": origin, "author": author, "status": "unverified",
                       "opened": opened, "first_seen": seen_at or utcnow(), "last_seen": utcnow(), "corroboration": [],
                       "notes": "", "meta": meta or {}}
                rows.append(row)
                self._index[k] = row
            self._dirty = True
            return row

    def set_status(self, rumor_id: str, status: str, evidence_url: str = "", note: str = "") -> dict | None:
        assert status in ("unverified", "corroborated", "debunked", "retracted")
        with self._lock:
            for r in self._load():
                if r["id"] == rumor_id:
                    r["status"] = status
                    if evidence_url:
                        r["corroboration"].append({"url": evidence_url, "at": utcnow(), "note": note})
                    elif note:
                        r["notes"] = (r["notes"] + "\n" + note).strip()
                    self._dirty = True
                    self.flush()
                    return r
        return None


# ----------------------------------------------------------------------------- researcher
class Researcher:
    def __init__(self, root: Path, cfg: dict, fetcher: Fetcher, store: Store, log=print):
        self.root, self.cfg, self.f, self.store, self.log = root, cfg, fetcher, store, log
        self._notes: dict = {}         # one-a-run observations (e.g. GDELT answering with text instead of JSON)
        self.registry = Registry(root / "data" / "verified", autoflush=False)   # flushed at the end of research
        self.rumors = RumorMill(root)
        self._cache_lock = threading.Lock()
        self.channel_cache = channels.ChannelCache(root / "state" / "channels.json", int(self.opts_of(cfg).get("site_recheck_days", 14)))
        self.jurisdictions = by_id(root)
        self.outlets = load_outlets(root)
        self.opts = (cfg or {}).get("candidates", {})
        self._opened: dict[str, dict | None] = {}  # article url -> {"title","text","url","outlet"} or None (couldn't open)
        from collections import OrderedDict

        self._roll: OrderedDict = OrderedDict()     # jurisdiction -> {person key: [(row, vote)]}, a few at a time
        self._spons: OrderedDict = OrderedDict()    # jurisdiction -> {person key: [(row, sponsor)]}

    @staticmethod
    def opts_of(cfg) -> dict:
        return (cfg or {}).get("candidates", {})

    @staticmethod
    def _pk(name: str) -> str:
        return re.sub(r"[^a-z]", "", (name or "").lower())

    def _cached(self, cache, jid: str, build):
        with self._cache_lock:
            if jid in cache:
                cache.move_to_end(jid)
                return cache[jid]
        built = build(jid)              # outside the lock: a big jurisdiction's index takes a moment
        with self._cache_lock:
            cache[jid] = built
            while len(cache) > 24:      # bounded: research visits many jurisdictions in one run
                cache.popitem(last=False)
            return cache[jid]

    def _roll_index(self, jid: str) -> dict:
        """Every roll call in a jurisdiction, by voter (read once per run, not once per candidate)."""
        def build(j):
            idx: dict[str, list] = {}
            for r in self.store.iter_query("jurisdiction=? AND kind='vote'", (j,)):
                m = r["meta"]
                row = {"id": r["id"], "url": r["url"], "title": r["title"], "published": r.get("published"),
                       "meta": {k: m.get(k) for k in ("result", "MatterFile", "EventBodyName", "topics", "EventItemId",
                                                      "opened_url", "procedural", "from", "tally", "ordinance_ref")}}
                for v in m.get("votes", []):
                    k = self._pk(v.get("person"))
                    if k:
                        idx.setdefault(k, []).append((row, v))
            return idx
        return self._cached(self._roll, jid, build)

    def _sponsor_index(self, jid: str) -> dict:
        def build(j):
            idx: dict[str, list] = {}
            for r in self.store.iter_query("jurisdiction=? AND kind IN ('ordinance','matter') AND meta LIKE '%\"sponsors\": [{%'", (j,)):
                m = r["meta"]
                for sp in m.get("sponsors") or []:
                    k = self._pk(sp.get("name"))
                    if k:
                        idx.setdefault(k, []).append((r, sp))
            return idx
        return self._cached(self._spons, jid, build)

    # ------------------------------------------------------------------ plumbing
    def _classifier(self, c: dict) -> Classifier:
        j = self.jurisdictions.get(c.get("jurisdiction") or "")
        extra = {domain_of(self.jurisdictions[x]["website"]) for x in c.get("jurisdictions", [])
                 if x in self.jurisdictions and self.jurisdictions[x].get("website")}
        return Classifier(self.root, c, j, self.registry, self.outlets, extra)

    def _adder(self, c: dict, cls: Classifier, counts: dict):
        sid = f"cand:{c['id']}"

        def add(rec: Record) -> None:
            rec.source_id, rec.jurisdiction = sid, c.get("jurisdiction", "")
            rec.meta.setdefault("candidate_id", c["id"])
            if c.get("electo_key"):
                rec.meta.setdefault("electo_key", c["electo_key"])
            if cls.excluded(rec.url):
                return
            rec.evidence = rec.evidence or cls.classify(rec.url)
            if rec.evidence == "unverified":
                self.rumors.add(c["id"], rec.url, rec.title or rec.text[:300], origin=rec.kind, seen_at=rec.published,
                                author=rec.meta.get("author", ""), meta={"via": rec.channel},
                                opened=bool(rec.meta.get("opened")))
                counts["rumor_mill"] = counts.get("rumor_mill", 0) + 1
                return
            if self.store.add(rec):
                counts[rec.kind] = counts.get(rec.kind, 0) + 1
        return add

    def _places(self, c: dict) -> list[str]:
        """Words that tie an article to this candidate's place (city, county, state)."""
        out = []
        for jid in c.get("jurisdictions", []):
            j = self.jurisdictions.get(jid)
            if j:
                out.append(j["name"])
        out += [f"{x} County" for x in c.get("counties", [])]
        return out + ["Georgia", "Ga."]

    # ------------------------------------------------------------------ single candidate
    def research(self, c: dict, since: str = "2016-01-01", skip: set[str] | None = None) -> dict:
        skip = set(skip or ())
        cls = self._classifier(c)
        counts: dict[str, int] = {}
        add = self._adder(c, cls, counts)
        c.setdefault("variants", name_variants(c["name"], c.get("aliases", [])))
        for name in STEPS:
            if name in skip:
                continue
            try:
                if name == "articles":
                    self.articles_batch([c], since, {c["id"]: add})
                else:
                    getattr(self, "_" + name)(c, cls, add, since)
            except (Disallowed, Blocked) as exc:
                self.log(f"  {name}: skipped ({exc})")
            except Exception as exc:  # noqa: BLE001
                self.log(f"  {name}: error {type(exc).__name__}: {exc}"[:300])
        self.store.flush()
        self.rumors.flush()
        return counts

    # ------------------------------------------------------------------ many candidates, budgeted
    NETWORK_STEPS = ("posts", "statements", "leads", "social_mentions", "connections")

    def research_many(self, cands: list[dict], since: str, deadline: float, progress, skip: set[str] | None = None,
                      cadence_days: float = 7, recent_cadence_days: float = 3, workers: int | None = None) -> dict:
        """Research due candidates until the deadline, several at a time.

        Due candidates are ordered (officeholders, never researched, those Electo cannot infer for yet, thin
        profiles, this year's candidates), then cut into chunks of up to GDELT_NAMES_PER_QUERY candidates from the
        same place. A pool of `workers` threads takes chunks in that order: each thread researches its chunk's
        candidates one by one (official records first, no network; then the candidate's own channels and the
        public indexes) and then runs the chunk's news search, one GDELT query for the whole chunk. So news
        arrives with the rest of a candidate's research instead of after every candidate (which a budget never
        reached), and the shared hosts (GDELT, Google News, Bluesky) are paced by the per-host limiter."""
        skip = set(skip or ())
        workers = max(1, int(workers or self.opts.get("workers", 6)))
        now = dt.datetime.now(dt.timezone.utc)
        this_year = str(now.year)

        def due(c):
            p = progress.get(f"cand:{c['id']}")
            last = p.get("last_ok")
            recent = (c.get("last_race_date") or "")[:4] >= str(now.year - 1)
            cad = recent_cadence_days if recent else cadence_days
            return not last or (now - dt.datetime.fromisoformat(last)).total_seconds() > cad * 86400

        inputs = _profile_inputs(self.root, self.store)   # what Electo's inference can already see per candidate

        def rank(c):
            return (not c.get("priority"),                                              # priority: true (officeholders) first
                    bool(progress.get(f"cand:{c['id']}").get("last_ok")),            # never researched first
                    min(inputs.get(c["id"], 0), 2) >= 2,                                # then those Electo can't infer for yet
                    c.get("profile_depth") not in ("none", "thin", ""),                 # then thin profiles
                    -int((c.get("last_race_date") or "0000")[:4] >= this_year),
                    progress.get(f"cand:{c['id']}").get("last_ok") or "")

        order = sorted((c for c in cands if due(c)), key=rank)
        chunks = _chunks_by_place(order, GDELT_NAMES_PER_QUERY)
        totals: dict[str, int] = defaultdict(int)
        state = {"done": 0, "chunks": 0, "partial": 0, "news_queries": 0, "news_done": 0, "news_due": 0, "steps_skipped": 0}
        lock = threading.Lock()
        articles_on = "articles" not in skip and self.opts.get("gdelt", True)
        leads_for = self.opts.get("leads_for", "all")
        stop = {"at": deadline}      # moved up to "now" by an interrupt (the workflow's watchdog sends SIGINT)
        late: dict[str, int] = defaultdict(int)   # steps skipped because a host's next turn was past the deadline
        self._notes = {}
        gnews = {}

        def gnews_ok() -> bool:
            """Google News leads only while the option is on and news.google.com's robots.txt allows the crawler."""
            if "ok" not in gnews:
                try:
                    gnews["ok"] = bool(self.opts.get("google_news", True)) and self.f.allowed(GNEWS)
                except Exception:  # noqa: BLE001
                    gnews["ok"] = False
                if not gnews["ok"] and self.opts.get("google_news", True):
                    self._notes["google_news"] = "news.google.com's robots.txt does not allow the crawler: no Google News leads"
            return gnews["ok"]

        def out_of_time() -> bool:
            return time.monotonic() > stop["at"]

        def one(c: dict) -> dict | None:
            if out_of_time():
                return None
            cls = self._classifier(c)
            counts: dict[str, int] = {}
            add = self._adder(c, cls, counts)
            partial = False
            for name in ("mentions", "votes", "sponsorships", "posts", "statements", "leads", "social_mentions", "connections"):
                if name in skip:
                    continue
                if out_of_time():
                    partial = True      # cut short: what was found is kept, but the candidate stays due
                    break
                if name in ("posts", "statements") and not (c.get("websites") or c.get("social")):
                    continue
                if name == "social_mentions" and not self.opts.get("bluesky_search", True):
                    continue
                if name == "leads" and not (c.get("news_feeds") or gnews_ok()):
                    continue                # Google News refuses crawlers in its robots.txt: only own news feeds
                if name == "leads" and leads_for == "priority_or_recent" and not (
                        c.get("priority") or (c.get("last_race_date") or "")[:4] >= str(now.year - 1)):
                    continue  # leads are uncitable pointers: spend the request budget where they matter
                try:
                    getattr(self, "_" + name)(c, cls, add, since)
                except (Disallowed, Blocked):
                    pass
                except OutOfTime as exc:
                    if "next turn" in str(exc):
                        # a busy host (its next turn is too far off): this step is skipped this time and the
                        # candidate's other steps still run; the step is tried again at the candidate's next research
                        with lock:
                            late[str(exc).split(": next turn")[0]] += 1
                        counts["steps_skipped"] = counts.get("steps_skipped", 0) + 1
                        continue
                    partial = True          # the run's time is up: what was found is kept, the candidate stays due
                    break
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  {c['id']} {name}: {type(exc).__name__}: {exc}"[:300])
            return {"c": c, "add": add, "counts": counts, "partial": partial}

        def chunk(cs: list[dict]) -> None:
            results = []
            for c in cs:
                res = one(c)
                if res is None:
                    break
                results.append(res)
                with lock:
                    for k, v in res["counts"].items():
                        if k == "steps_skipped":
                            state["steps_skipped"] += v      # not records: kept out of what was added
                        else:
                            totals[k] += v
                    if res["partial"]:
                        state["partial"] += 1
                    else:
                        state["done"] += 1
                    n = state["done"]
                if res["partial"]:
                    progress.update(f"cand:{c['id']}", status="partial (out of time; still due)", counts=res["counts"])
                    break
                progress.update(f"cand:{c['id']}", last_ok=utcnow(), status="ok", counts=res["counts"])
                if n % 50 == 0:
                    self.store.flush()
                    self.rumors.flush()
                    self.log(f"  researched {n}/{len(order)}")
        # ---- news: its own lane, one GDELT query at a time as fast as GDELT allows (a search can take a minute
        # and a query sent the moment the last one answered is refused), with its own per-candidate progress
        # ("news:<id>"), so slow news never holds up the research workers and every candidate gets its turn
        news_cad = float(self.opts.get("news_cadence_days", 14))

        def news_due(c) -> bool:
            last = progress.get(f"news:{c['id']}").get("last_ok")
            recent = (c.get("last_race_date") or "")[:4] >= str(now.year - 1)
            cad = recent_cadence_days if recent else news_cad
            return not last or (now - dt.datetime.fromisoformat(last)).total_seconds() > cad * 86400

        def news_rank(c):
            last = progress.get(f"news:{c['id']}").get("last_ok") or ""
            return (bool(last), not c.get("priority"), -int((c.get("last_race_date") or "0000")[:4] >= str(now.year - 1)), last)

        def news_lane() -> None:
            groups = _chunks_by_place(sorted((c for c in cands if news_due(c)), key=news_rank), GDELT_NAMES_PER_QUERY)
            state["news_due"] = sum(len(g) for g in groups)
            for g in groups:
                if out_of_time():
                    return
                counts = {c["id"]: {} for c in g}
                adders = {c["id"]: self._adder(c, self._classifier(c), counts[c["id"]]) for c in g}
                try:
                    n_art = self.articles_batch(g, since, adders)
                except (Disallowed, Blocked) as exc:
                    self._notes["news_refused"] = f"{type(exc).__name__}: {exc}"[:200]
                    return                      # GDELT refused the crawler: nothing more to ask it this run
                except OutOfTime:
                    return
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        self._notes["article_errors"] = self._notes.get("article_errors", 0) + 1
                    if self._notes["article_errors"] <= 2:
                        self.log(f"  news: {type(exc).__name__}: {exc}"[:600])
                    continue
                with lock:
                    for cnt in counts.values():
                        for k, v in cnt.items():
                            totals[k] += v
                    state["news_queries"] += 1
                    state["news_done"] += len(g)
                for c in g:
                    progress.update(f"news:{c['id']}", last_ok=utcnow(), counts=counts[c["id"]])

        import signal
        from concurrent.futures import ThreadPoolExecutor

        def on_interrupt(signum, frame):
            if stop["at"] > time.monotonic():
                self.log("interrupted: finishing the candidates in hand and saving")
            stop["at"] = self.f.stop_at = time.monotonic()

        try:
            prev_handler = signal.signal(signal.SIGINT, on_interrupt)
        except ValueError:
            prev_handler = None              # not the main thread (tests): no handler
        self.f.stop_at = deadline            # a wait for a host's turn, or a request mid-candidate, ends at the budget too
        self.f.max_wait = float(self.opts.get("max_host_wait_s", 45))   # a turn further off than this: skip the step
        pool = ThreadPoolExecutor(max_workers=workers)
        news = None
        if articles_on:
            news = threading.Thread(target=news_lane, name="news-lane", daemon=True)
            news.start()
        try:
            futures = [pool.submit(chunk, cs) for cs in chunks]
            for fut in futures:
                try:
                    fut.result()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  research chunk: {type(exc).__name__}: {exc}"[:300])
        except KeyboardInterrupt:
            stop["at"] = self.f.stop_at = time.monotonic()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)   # queued chunks are dropped; running ones stop at their next request
            # research is done or out of time; news carries on until its candidates are done or the deadline
            # (joined in short waits, so an interrupt still reaches this thread)
            while news is not None and news.is_alive():
                news.join(timeout=1.0)
            self.f.stop_at = None
            self.f.max_wait = None
            if prev_handler is not None:
                signal.signal(signal.SIGINT, prev_handler)
        self.store.flush()
        self.rumors.flush()
        self.registry.flush()
        self.channel_cache.save()
        pacing = {}
        hosts = getattr(getattr(self.f, "limiter", None), "_hosts", {})
        for host, st in list(hosts.items()):
            if host in late or st.multiplier > 1.0:     # strained hosts (backed off) and the ones that cost steps
                pacing[host] = {"interval_s": round(self.f.limiter.interval(host), 1), "backoff": round(st.multiplier, 1)}
        if late:
            self.log("  steps skipped for a host turn past the deadline: " +
                     ", ".join(f"{h} {n}" + (f" (interval {pacing[h]['interval_s']}s, back-off x{pacing[h]['backoff']})" if h in pacing else "")
                               for h, n in sorted(late.items(), key=lambda kv: -kv[1])))
        return {"researched": state["done"], "due": len(order), "chunks": len(chunks), "workers": workers, "added": dict(totals),
                "cut_short": state["partial"], "steps_skipped": state["steps_skipped"], "late_hosts": dict(late), "pacing": pacing,
                "news": {"queries": state["news_queries"], "candidates": state["news_done"], "due": state["news_due"]},
                **self._notes}

    # ------------------------------------------------------------------ official records (no network)
    def _mentions(self, c, cls, add, since):
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        match = Store.fts_phrase(*variants)
        if not match:
            return
        jids = [j for j in c.get("jurisdictions", []) if j]
        where, params = "r.source_id NOT LIKE 'cand:%' AND r.kind NOT IN ('vote')", ()
        if jids:
            where += f" AND r.jurisdiction IN ({','.join('?' * len(jids))})"
            params = tuple(jids)
        rx = name_regex(variants)
        from .transcripts import time_at

        for hit in self.store.search(match, where, params, limit=int(self.opts.get("max_mentions", 150))):
            text = hit.get("text") or ""
            ex = excerpts(text, rx, n=2) or [hit.get("snippet", "")]
            meta = {"record_id": hit["id"], "record_kind": hit["kind"], "source": hit["source_id"],
                    "record_jurisdiction": hit.get("jurisdiction"), "opened": True, "opened_at": hit.get("fetched_at")}
            if hit["kind"] == "transcript":
                first = rx.search(text)
                meta["time"] = time_at(hit, first.start()) if first else None
                meta["video_page"] = hit.get("meta", {}).get("video_page")
                meta["meeting_date"] = hit.get("meta", {}).get("meeting_date")
            add(Record("", "mention", hit["url"], title=(hit.get("title") or "")[:200], text="\n".join(ex)[:1200],
                       published=hit.get("published") or meta.get("meeting_date"), channel="store", evidence="primary", meta=meta))

    def _votes(self, c, cls, add, since):
        jids = [j for j in c.get("jurisdictions", []) if j]
        if not jids:
            return
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        want = {self._pk(v) for v in variants}
        seen_rows: set = set()
        for jid in jids:
            idx = self._roll_index(jid)
            for k in want:
                for r, v in idx.get(k, ()):
                    if r["id"] in seen_rows:
                        continue
                    seen_rows.add(r["id"])
                    m = r["meta"]
                    add(Record("", "vote", r["url"], title=r["title"], text=r["title"], published=r.get("published"),
                               channel="store", evidence="primary",
                               meta={"vote": v.get("vote"), "result": m.get("result"), "matter_file": m.get("MatterFile"),
                                     "body": m.get("EventBodyName"), "topics": m.get("topics", []),
                                     "EventItemId": m.get("EventItemId"), "record_id": r["id"], "opened": True,
                                     "opened_url": m.get("opened_url"), "basis": v.get("basis") or "roll call",
                                     "role": v.get("role", ""), "procedural": bool(m.get("procedural")),
                                     "from": m.get("from") or "legistar", "tally": m.get("tally", ""),
                                     "ordinance_ref": m.get("ordinance_ref", "")}))

    def _sponsorships(self, c, cls, add, since):
        """Ordinances and resolutions this person sponsored (Legistar sponsor lists already in the store)."""
        jids = [j for j in c.get("jurisdictions", []) if j]
        if not jids:
            return
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        want = {self._pk(v) for v in variants}
        seen_rows: set = set()
        for jid in jids:
            idx = self._sponsor_index(jid)
            for k in want:
                for r, sp in idx.get(k, ()):
                    if r["id"] in seen_rows:
                        continue
                    seen_rows.add(r["id"])
                    m = r["meta"]
                    role = "sponsor" if (sp.get("sequence") in (None, 0, 1)) else "co-sponsor"
                    add(Record("", "sponsorship", r["url"], title=r["title"], text=(r.get("text") or r["title"])[:600],
                               published=r.get("published"), channel="store", evidence="primary",
                               meta={"number": m.get("MatterFile"), "MatterId": m.get("MatterId"), "role": role,
                                     "status": m.get("MatterStatusName"), "type": m.get("MatterTypeName"),
                                     "enacted": bool(m.get("MatterEnactmentDate")), "enacted_date": m.get("MatterEnactmentDate"),
                                     "topics": m.get("topics", []), "record_id": r["id"], "opened": True,
                                     "opened_url": m.get("opened_url") or r["url"]}))

    # ------------------------------------------------------------------ the candidate's own channels
    def _posts(self, c, cls, add, since):
        proof = c["websites"][0] if c.get("websites") else ""
        for url in c.get("social", []):
            if platform_of(url):
                row = self.registry.add_account(c["name"], url, proof, c.get("office", ""), c.get("jurisdiction", ""))
                self.registry.mark_candidate(row["id"], c["id"])
        if proof:
            self.registry.verify_accounts(self.f, only_candidate=c["id"], recheck_days=float(self.opts.get("account_recheck_days", 7)))
        self.registry.pull(self.f, since=since, only_candidate=c["id"])
        cls.verified_accounts = {(a["platform"], a.get("handle")) for a in self.registry.accounts()
                                 if a.get("status") == "verified" and a.get("candidate_id") == c["id"]}
        for p in self.registry.posts():
            if p.get("candidate_id") == c["id"] and p["status"] == "verified":
                add(Record("", "post", p["post_url"], title=p["text"][:120], text=p["text"],
                           published=p.get("posted_at"), channel=p.get("source", "manual"), evidence="primary",
                           meta={"platform": p["platform"], "account_id": p["account_id"], "topics": p.get("topics", []),
                                 "opened": True}))

    def _statements(self, c, cls, add, since):
        """The candidate's own websites: what they say about themselves (positions, record, endorsements).
        The way in (feed, sitemap, plain pages, or the browser for a script-built site) is found once and kept in
        state/channels.json for `site_recheck_days`; a site that is gone (404, no such host) is noted and left
        alone for `dead_site_days` (30). Campaign sites are small: `site_max_pages` (25) pages, the ones about
        issues, the candidate and endorsements first."""
        max_pages = int(self.opts.get("site_max_pages", 25))
        dead_days = float(self.opts.get("dead_site_days", 30))
        for i, site in enumerate(c.get("websites", [])):
            sid = f"cand:{c['id']}:site{i}"
            dead = self.store.kv_get(f"dead_site:{sid}")
            if dead and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(dead)).total_seconds() < dead_days * 86400:
                continue
            src = {"id": sid, "url": site, "kind": "statement", "depth": 3, "max_pages": max_pages, "hot": CAMPAIGN_HOT,
                   "exclude": ["/tag/", "/author/", "?share", "/feed", "/wp-json", "login", "/cart", "/donate", "/shop",
                               "/events", "/calendar", "/volunteer", "/signup", "/sign-up", "/privacy", "/terms"]}
            hit = self.channel_cache.get(sid)
            if hit:
                chosen = hit["chosen"]
            else:
                try:
                    probes = channels.probe_source(self.f, src)
                except (Disallowed, Blocked):
                    continue
                chosen = channels.choose(probes)
                gone = [p for p in probes if p.channel == "html" and re.search(r"HTTP (404|410)|NameResolutionError|Name or service not known|Max retries", p.note or "")]
                if not chosen:
                    if gone:
                        self.store.kv_set(f"dead_site:{sid}", utcnow())
                        self.log(f"  statements: {site} is gone ({gone[0].note[:60]}); noted, retried in {int(dead_days)} days")
                    else:
                        self.log(f"  statements: no channel for {site}")
                    continue
                self.channel_cache.put(sid, chosen, probes, save=False)
                chosen = {"channel": chosen.channel, "endpoint": chosen.endpoint}
            ctx = channels.CrawlContext(frontier=self.store, page_revisit_days=7)
            for rec in channels.crawl(self.f, src, chosen, {}, ctx=ctx):
                if isinstance(rec, channels.Checkpoint):
                    continue
                rec.kind = "statement"
                rec.evidence = "primary"
                for m in re.finditer(r"endorse[ds]?\s+by\s+(?:the\s+)?([A-Z][^.\n!?\[\]]{3,80})", rec.text or "", re.I):
                    add(Record("", "connection", rec.url, title=f"Endorsement: {m.group(1).strip()}",
                               text=m.group(0), published=rec.published, channel=rec.channel, evidence="primary",
                               meta={"type": "endorsement", "party": m.group(1).strip(), "self_reported": True,
                                     "opened": True}))
                add(rec)

    # ------------------------------------------------------------------ the press
    def _open_article(self, url: str) -> dict | None:
        """Fetch an article once (polite, robots-respecting) and keep its title and text; None if it can't be opened."""
        if url in self._opened:
            return self._opened[url]
        out = None
        try:
            r = self.f.get(url)
            if r.status == 200 and "html" in r.content_type:
                title, md = extract.html_to_markdown(r.text, r.url)
                out = {"url": r.url, "title": title, "text": md}
            self.store.mark_url(url, r.status, "articles", r.content_type)
        except (Disallowed, Blocked) as exc:
            out = None
            self._opened[url + "#why"] = {"why": type(exc).__name__}
        except OutOfTime:
            raise                # not "could not be opened": it was never tried, and must not be remembered as tried
        except Exception:  # noqa: BLE001
            out = None
        self._opened[url] = out
        return out

    def articles_batch(self, group: list[dict], since: str, adders: dict) -> int:
        """One GDELT query for several candidates; open each hit once and attribute it to every candidate whose
        name AND place appear in the article. Unopenable hits whose title names a candidate become leads."""
        floor = max(since[:10], (dt.date.today() - dt.timedelta(days=GDELT_WINDOW_DAYS)).isoformat())
        phrases = []
        for c in group:
            v = (c.get("variants") or name_variants(c["name"], c.get("aliases", [])))[:1]
            phrases += [f'"{x}"' for x in v]
        if not phrases:
            return 0
        q = (phrases[0] if len(phrases) == 1 else "(" + " OR ".join(phrases) + ")") + " sourcecountry:US sourcelang:english"
        r = self.f.get(GDELT, params={"query": q, "mode": "artlist", "maxrecords": 250, "format": "json", "sort": "datedesc",
                                      "startdatetime": floor.replace("-", "") + "000000"}, use_cache=False)
        if r.status == 200 and r.content.strip().startswith(b"{"):
            arts = r.json().get("articles", [])
        else:
            arts = []
            # GDELT answers a rate limit or a bad query as HTTP 200 with a sentence, not JSON: say so once a run
            self._notes["gdelt_not_json"] = self._notes.get("gdelt_not_json", 0) + 1
            if self._notes["gdelt_not_json"] == 1:
                self.log(f"  gdelt: HTTP {r.status} {r.content_type}: {r.text[:160]!r}")
        pats = {c["id"]: name_regex(c.get("variants") or name_variants(c["name"], c.get("aliases", []))) for c in group}
        places = {c["id"]: re.compile("|".join(re.escape(p) for p in self._places(c)), re.I) for c in group}
        arts = [a for a in arts[: int(self.opts.get("max_articles_per_query", 60))] if a.get("url")]
        # only hits whose title or snippet could be about someone in the group are opened; the pages are opened a
        # few at a time (different outlets; each outlet's own spacing still holds)
        pages, untried = {}, set()
        urls = list(dict.fromkeys(a["url"] for a in arts))
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, int(self.opts.get("news_workers", 4)))) as ex:
            futs = {u: ex.submit(self._open_article, u) for u in urls}
            for u, fut in futs.items():
                try:
                    pages[u] = fut.result()
                except OutOfTime:
                    untried.add(u)          # never asked for: neither an article nor a lead
                except Exception:  # noqa: BLE001
                    pages[u] = None
        n = 0
        for a in arts:
            url, title = a["url"], a.get("title", "")
            if url in untried:
                continue
            pub = a.get("seendate", "")
            pub = f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if len(pub) >= 8 else None
            page = pages.get(url)
            for c in group:
                add = adders[c["id"]]
                if page is None:
                    if pats[c["id"]].search(title):
                        why = (self._opened.get(url + "#why") or {}).get("why", "could not be opened")
                        add(Record("", "lead", url, title=title, published=pub, channel="api", evidence="lead",
                                   meta={"outlet": a.get("domain"), "via": "gdelt", "why": why}))
                    continue
                text = page["text"]
                if not pats[c["id"]].search(text) or not places[c["id"]].search(text):
                    continue  # name not on the page, or a same-named person elsewhere
                ex = excerpts(text, pats[c["id"]], n=3)
                add(Record("", "article", page["url"], title=page["title"] or title, text="\n".join(ex)[:1800],
                           published=pub, channel="api",
                           meta={"outlet": a.get("domain"), "via": "gdelt", "opened": True}))
                n += 1
        return n

    def _leads(self, c, cls, add, since):
        """Google News RSS: its links are redirect pages that can't be opened directly, so hits are leads only."""
        terms = list(c.get("search_terms") or [])
        if not terms:
            j = self.jurisdictions.get(c.get("jurisdiction") or "")
            terms = [f'"{c["name"]}" {j["name"] if j else "Georgia"}']
        pat = name_regex(c.get("variants") or name_variants(c["name"], c.get("aliases", [])))
        feeds = [f"{GNEWS}?q={quote_plus(t)}&hl=en-US&gl=US&ceid=US:en" for t in terms[:2]] + list(c.get("news_feeds", []))
        for feed in feeds:
            try:
                r = self.f.get(feed, use_cache=False)
            except (Disallowed, Blocked):
                continue
            if r.status != 200:
                continue
            for it in parse_feed(r.content)[:30]:
                if not pat.search(it.get("title", "")):
                    continue
                outlet_url = it.get("source_url") or it["link"]
                if cls.excluded(outlet_url):
                    continue
                if "news.google.com" in feed:
                    add(Record("", "lead", it["link"], title=it["title"], published=it["published"], channel="feed",
                               evidence="lead", meta={"outlet": it.get("source") or domain_of(outlet_url),
                                                      "why": "news index link (not opened)", "via": "google_news"}))
                else:  # a feed the user configured: items link straight to articles; open before counting
                    page = self._open_article(it["link"])
                    if page and pat.search(page["text"]):
                        add(Record("", "article", page["url"], title=page["title"] or it["title"],
                                   text="\n".join(excerpts(page["text"], pat, n=3))[:1800], published=it["published"],
                                   channel="feed", meta={"outlet": domain_of(page["url"]), "via": "feed", "opened": True}))

    # ------------------------------------------------------------------ other people's social posts
    def _social_mentions(self, c, cls, add, since):
        cursor, n = None, 0
        floor = max(since[:10], (dt.date.today() - dt.timedelta(days=365)).isoformat())
        while n < 200:
            params = {"q": f'"{c["name"]}"', "limit": 100, "since": floor + "T00:00:00Z"}
            if cursor:
                params["cursor"] = cursor
            r = self.f.get(BSKY_SEARCH, params=params, use_cache=False)
            if r.status != 200:
                return
            data = r.json()
            for post in data.get("posts", []):
                rec_ = post.get("record", {})
                handle = post.get("author", {}).get("handle", "")
                rkey = post.get("uri", "").rsplit("/", 1)[-1]
                url = f"https://bsky.app/profile/{handle}/post/{rkey}"
                add(Record("", "social_mention", url, title=rec_.get("text", "")[:120], text=rec_.get("text", ""),
                           published=rec_.get("createdAt"), channel="api",
                           meta={"platform": "bluesky", "author": handle, "likes": post.get("likeCount"),
                                 "uri": post.get("uri"), "opened": True}))
                n += 1
            cursor = data.get("cursor")
            if not cursor:
                return

    # ------------------------------------------------------------------ money and endorsements
    def _connections(self, c, cls, add, since):
        csv_path = self.root / (c.get("finance_csv") or f"imports/finance/{c['id']}.csv")
        if csv_path.exists():
            for row in parse_finance_csv(csv_path):
                add(Record("", "connection", f"file://{csv_path.name}", title=f"Contribution: {row['name']}",
                           text=json.dumps(row), published=row.get("date"), channel="import", evidence="primary",
                           meta={"type": "contribution", **row,
                                 "filing_source": "Georgia Government Transparency and Campaign Finance Commission export"}))
        if c.get("fec_id"):
            key = os.environ.get("FEC_API_KEY", "DEMO_KEY")
            r = self.f.get(f"{FEC}/committees/", params={"candidate_id": c["fec_id"], "api_key": key}, use_cache=False)
            for com in (r.json().get("results", []) if r.status == 200 else []):
                page = 1
                while page <= 20:
                    rr = self.f.get(f"{FEC}/schedules/schedule_a/", params={
                        "committee_id": com["committee_id"], "api_key": key, "per_page": 100, "page": page,
                        "sort": "-contribution_receipt_date", "min_date": since[:10]}, use_cache=False)
                    if rr.status != 200:
                        break
                    rows = rr.json().get("results", [])
                    for s in rows:
                        add(Record("", "connection", s.get("pdf_url") or f"{FEC}/schedules/schedule_a/?committee_id={com['committee_id']}",
                                   title=f"Contribution: {s.get('contributor_name')}", text=json.dumps({
                                       "name": s.get("contributor_name"), "amount": s.get("contribution_receipt_amount"),
                                       "date": s.get("contribution_receipt_date"), "employer": s.get("contributor_employer"),
                                       "occupation": s.get("contributor_occupation"), "city": s.get("contributor_city"),
                                       "state": s.get("contributor_state")}),
                                   published=s.get("contribution_receipt_date"), channel="api", evidence="primary",
                                   meta={"type": "contribution", "name": s.get("contributor_name"),
                                         "amount": s.get("contribution_receipt_amount"), "employer": s.get("contributor_employer"),
                                         "date": s.get("contribution_receipt_date"), "committee": com["committee_id"],
                                         "filing_source": "FEC", "opened": True}))
                    if len(rows) < 100:
                        break
                    page += 1


def _profile_inputs(root: Path, store: Store | None = None) -> dict:
    """{candidate id: inference inputs} from the last `gacrawl profiles` / export (database kv; older runs: a state file)."""
    try:
        raw = store.kv_get("profile_inputs") if store is not None else None
        if raw:
            return json.loads(raw)
        p = root / "state" / "profile_inputs.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def parse_finance_csv(path: Path) -> list[dict]:
    """Tolerant reader for campaign-finance exports: finds the contributor, amount, date, employer columns by name."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower(): c for c in reader.fieldnames or []}

        def col(*keys):
            for k in keys:
                for lc, orig in cols.items():
                    if k in lc:
                        return orig
            return None

        name_c, amt_c, date_c = col("contributor name", "contributor", "name", "payee"), col("amount", "total"), col("date")
        emp_c, occ_c, city_c, type_c = col("employer"), col("occupation"), col("city"), col("type", "category")
        for r in reader:
            if not name_c or not r.get(name_c):
                continue
            amt = re.sub(r"[^\d.\-]", "", r.get(amt_c, "") or "") if amt_c else ""
            rows.append({"name": r.get(name_c, "").strip(), "amount": float(amt) if amt else None,
                         "date": (r.get(date_c) or "").strip() if date_c else None,
                         "employer": (r.get(emp_c) or "").strip() if emp_c else "",
                         "occupation": (r.get(occ_c) or "").strip() if occ_c else "",
                         "city": (r.get(city_c) or "").strip() if city_c else "",
                         "kind": (r.get(type_c) or "").strip() if type_c else ""})
    return rows
