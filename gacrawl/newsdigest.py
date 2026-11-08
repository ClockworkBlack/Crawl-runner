"""Georgia political news for Electo's News tab, refreshed every run.

The tab used to show a digest written by hand (it stopped on Nov 4, when nobody wrote the next one). This keeps it
current from the outlets in config/outlets.yaml:
  - outlets with a WordPress news API (state/wp_outlets.json, found on each outlet's home page by bill reasons): the
    last NEWS_DAYS days of posts, each with its own excerpt
  - other outlets: the RSS/Atom feeds their home pages link to (found once a week, state/news_feeds.json; never a
    feed address built by hand)
Kept: stories about Georgia government and politics (the legislature, the Governor and state offices, elections,
city councils, county commissions, school boards, courts, budgets and taxes, policing). Each becomes a `news_item`
record (source news:digest): title, outlet, date, the outlet's own excerpt (at most two sentences), topic tags.

Export: dist/electo/news.json in the page's format ({updated, window, items: [{t, u, s, d, sum, tags, also}]}), the
newest NEWS_KEEP stories; Electo's import merges it with any hand-written digest.
"""
from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import re
import time
from pathlib import Path

from . import extract
from .store import Record

SOURCE = "news:digest"
NEWS_DAYS = 10          # how far back each run looks
NEWS_KEEP = 150         # stories in the exported digest
PER_OUTLET = 30
FEED_RECHECK_DAYS = 7
POLITICS = re.compile(
    r"\b(legislat\w*|general assembly|state house|state senate|state rep\w*|senator|lawmakers?|bill|governor|kemp|"
    r"lieutenant governor|attorney general|secretary of state|raffensperger|election\w*|ballot|vot(e|es|ers?|ing)|"
    r"runoff|recount|campaign|candidate|primary|council\w*|commission\w*|mayor|city manager|county manager|"
    r"school board|board of education|superintendent|budget|millage|property tax|tax(es)? |sales tax|splost|tspl?ost|"
    r"ordinance|zoning|rezon\w*|referendum|court|judge|ruling|lawsuit|sheriff|police chief|district attorney|"
    r"congress\w*|congressman|congresswoman|public service commission|psc|ethics|audit|appropriat\w*|policy|"
    r"democrat\w*|republican\w*|gop|annexation|charter|municipal)\b", re.I)
GEORGIA = re.compile(r"\b(georgia|ga\.|atlanta|savannah|augusta|macon|columbus|athens|albany|valdosta|gwinnett|cobb|"
                     r"dekalb|fulton|clayton|henry|cherokee|forsyth|chatham|county|city of)\b", re.I)
TAGS = [("elections", r"election|ballot|vot|runoff|recount|precinct|campaign|candidate|primary"),
        ("legislature", r"legislat|general assembly|lawmaker|state rep|state senat|\bbill\b"),
        ("governor", r"governor|kemp|executive order"),
        ("local", r"council|commission|mayor|city manager|county manager|zoning|rezon|annexation|ordinance"),
        ("schools", r"school board|board of education|superintendent|school district"),
        ("budget", r"budget|millage|tax|splost|appropriat|bond"),
        ("courts", r"court|judge|ruling|lawsuit|indict|district attorney"),
        ("policing", r"sheriff|police|arrest|shooting")]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _text(h: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def _excerpt(t: str, n: int = 2, cap: int = 320) -> str:
    parts = re.split(r"(?<=[.!?])\s+", t.strip())
    out = " ".join(parts[:n]).strip()
    out = re.sub(r"\s*(\[…\]|\[\.\.\.\]|…|Continue reading.*|The post .* appeared first on .*)$", "", out)
    return out[:cap].rstrip() + ("…" if len(out) > cap else "")


# government itself: what a story must be about (crime, crashes and court sentences alone are not political news)
CIVIC = re.compile(
    r"\b(legislat\w*|general assembly|state house|state senate|state rep\w*|senators?|lawmakers?|governor|kemp|"
    r"lieutenant governor|attorney general|secretary of state|raffensperger|elections?|ballots?|voters?|voting|"
    r"runoff|recount|campaign\w*|candidates?|primary|council\w*|commission(ers?)?|mayor\w*|city manager|"
    r"county manager|school board|board of education|superintendent|budget\w*|millage|property tax\w*|tax rollback|"
    r"sales tax|splost|tspl?ost|e-?splost|ordinance\w*|zoning|rezon\w*|referendum|annexation|"
    r"congress\w*|public service commission|ethics|audit\w*|appropriat\w*|democrat\w*|republican\w*|gop|"
    r"medicaid|transition team|special session|executive order|redistrict\w*|state agency|regulations?)\b", re.I)
CRIME = re.compile(r"\b(arrest\w*|charged|killed|killing|murder\w*|shot|shooting|stabb\w*|crash\w*|wreck|overturn\w*|"
                   r"sentenced|gets (life|\d+ years)|years in prison|dui|drunk driv\w*|robber\w*|burglar\w*|missing|"
                   r"fire\b|blaze|obituar\w*|arrest report|booking)\b", re.I)
GOV_TITLE = re.compile(r"\b(council|commission|board|legislat|lawmaker|governor|kemp|mayor|sheriff election|election|"
                       r"vote|ballot|budget|tax|ordinance|policy|state house|state senate|race|approv|contract)\w*", re.I)
LOCAL = re.compile(r"\b(council\w*|commission(ers?)?|board of (education|elections|commissioners)|school board|mayor|"
                   r"city|town|county|millage)\b", re.I)
REPORT = re.compile(r"\b(arrest|booking|jail) (report|log)s?\b|\bnewsletter\b", re.I)


STATEWIDE = re.compile(r"\b(kemp|raffensperger|bottoms|gold dome|general assembly|state capitol|gov\.?-elect|"
                       r"lieutenant governor|state house|state senate|public service commission)\b", re.I)
AMBIGUOUS = {"washington", "lincoln", "jackson", "franklin", "union", "columbia", "georgia", "america", "early", "price",
             "lumber city", "jefferson", "madison", "monroe", "clay", "grant", "lee", "hart", "berlin", "milan", "rome",
             "perry", "douglas", "baker", "warwick", "allen", "riddleville", "white", "young", "harris", "smith"}
_PLACES: list = []


def _places():
    """Every Georgia county and city in config/jurisdictions.yaml (Rochelle, Tucker, Brookhaven ...) as one pattern."""
    if not _PLACES:
        from .registry import jurisdictions

        names = set()
        try:
            for j in jurisdictions(Path(__file__).resolve().parent.parent):
                # counties keep "County" (Miller, Taylor, Cook ... are surnames too); cities drop "City of"
                if j.get("kind") not in ("city", "town", "municipality", "consolidated"):
                    n = (j.get("name") or "").strip() if j.get("kind") == "county" else ""
                else:
                    n = re.sub(r"^(city|town) of\s+|\s+(city|town|consolidated government|unified government)$", "",
                               (j.get("name") or "").strip(), flags=re.I).strip()
                if len(n) >= 4 and n.lower() not in AMBIGUOUS:
                    names.add(re.escape(n))
        except Exception:  # noqa: BLE001
            names = set()
        _PLACES.append(re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\b") if names else None)
    return _PLACES[0]


def in_georgia(title: str, text: str) -> bool:
    hay = f"{title} {text[:1500]}"
    pl = _places()
    return bool(GEORGIA.search(hay) or STATEWIDE.search(hay) or LOCAL.search(title) or (pl and pl.search(hay)))


def _other_state(title: str) -> bool:
    from .policing_news import OTHER_STATES

    return bool(OTHER_STATES.search(title)) and not GEORGIA.search(title)


def keep(title: str, text: str) -> bool:
    """Georgia government and politics: a civic subject in the headline, or in the excerpt when the headline is not a
    crime or accident report; never a story whose headline is about another state, national sports or travel."""
    hay = f"{title} {text[:1500]}"
    if _other_state(title) or re.search(r"\b(NCAA|NFL|NBA|MLB|SEC|Amtrak|recipe|weather)\b", title):
        return False
    if REPORT.search(title) or (CRIME.search(title) and not GOV_TITLE.search(title)):
        return False
    if not (CIVIC.search(title) or CIVIC.search(text[:600])):
        return False
    # every outlet is a Georgia outlet, so a local body in the headline (a council, a commission, a board) is local;
    # otherwise the story must name a Georgia place (a national wire story about Washington or another state is not this tab's)
    return in_georgia(title, text)


def tags_of(title: str, text: str) -> list[str]:
    hay = f"{title} {text[:1500]}".lower()
    return [t for t, rx in TAGS if re.search(rx, hay)][:3]


class Digest:
    def __init__(self, fetcher, store, root: Path, state_dir: Path, log=print):
        from .registry import load_yaml

        self.f, self.store, self.root, self.log = fetcher, store, root, log
        o = load_yaml(root / "config" / "outlets.yaml") or {}
        self.outlets = [d for d in (o.get("outlets") or []) if isinstance(d, str)]
        self.names = o.get("names") or {}
        self.wp = {}
        try:
            w = json.loads((state_dir / "wp_outlets.json").read_text())
            self.wp = {d: e["api"] for d, e in w.items() if isinstance(e, dict) and e.get("api")}
        except (OSError, ValueError):
            pass
        self.feeds_p = state_dir / "news_feeds.json"
        try:
            self.feeds = json.loads(self.feeds_p.read_text())
        except (OSError, ValueError):
            self.feeds = {}
        self.stats = {"outlets": 0, "seen": 0, "kept": 0, "new": 0}

    def add(self, url: str, title: str, text: str, date: str, outlet: str, via: str) -> None:
        self.stats["seen"] += 1
        title = _text(title)[:240]
        if not url.startswith("http") or not title or not keep(title, text):
            return
        self.stats["kept"] += 1
        rec = Record(SOURCE, "news_item", url, title=title, text=_excerpt(text), published=(date or "")[:10] or None,
                     channel=via, jurisdiction="ga", evidence="reported", key=f"{SOURCE}|{url}",
                     meta={"outlet": outlet, "outlet_name": self.names.get(outlet, outlet), "tags": tags_of(title, text),
                           "via": via, "opened": True})
        self.stats["new"] += self.store.add(rec)

    def from_wp(self, dom: str, api: str) -> None:
        after = (dt.date.today() - dt.timedelta(days=NEWS_DAYS)).isoformat() + "T00:00:00"
        r = self.f.get(api.rstrip("/") + "/wp/v2/posts", params={"after": after, "per_page": PER_OUTLET,
                                                                 "_fields": "link,date,title,excerpt"}, use_cache=False)
        if r.status != 200 or not r.content.strip().startswith(b"["):
            return
        for p in r.json():
            self.add(p.get("link") or "", (p.get("title") or {}).get("rendered") or "",
                     _text((p.get("excerpt") or {}).get("rendered") or ""), p.get("date") or "", dom, "wp_json")

    def feed_of(self, dom: str) -> str | None:
        e = self.feeds.get(dom) or {}
        if e.get("checked") and (dt.date.today() - dt.date.fromisoformat(e["checked"])).days < FEED_RECHECK_DAYS:
            return e.get("feed")
        feed = None
        try:
            r = self.f.get(f"https://{dom}/", use_cache=False)
            if r.status == 200 and "html" in r.content_type:
                links = extract.find_feed_links(r.text, r.url)
                feed = next((u for u in links if not re.search(r"comments", u, re.I)), None)
        except Exception:  # noqa: BLE001
            feed = None
        self.feeds[dom] = {"feed": feed, "checked": dt.date.today().isoformat()}
        return feed

    def from_feed(self, dom: str, feed: str) -> None:
        from .feeds import parse_feed

        r = self.f.get(feed, use_cache=False)
        if r.status != 200:
            return
        from .channels import _date10

        floor = (dt.date.today() - dt.timedelta(days=NEWS_DAYS)).isoformat()
        for item in parse_feed(r.content)[:PER_OUTLET]:
            d = _date10(item.get("published")) or ""
            if d and d < floor:
                continue
            self.add(item.get("link") or "", item.get("title") or "", _text(item.get("summary") or ""),
                     d, dom, "feed")

    def run(self, deadline: float | None = None) -> dict:
        from .fetch import OutOfTime

        order = [d for d in self.outlets if d in self.wp] + [d for d in self.outlets if d not in self.wp]
        for dom in order:
            if deadline and time.monotonic() > deadline:
                break
            try:
                if dom in self.wp:
                    self.from_wp(dom, self.wp[dom])
                else:
                    feed = self.feed_of(dom)
                    if feed:
                        self.from_feed(dom, feed)
                self.stats["outlets"] += 1
            except OutOfTime:
                break
            except Exception as exc:  # noqa: BLE001
                self.log(f"  news: {dom}: {type(exc).__name__}: {str(exc)[:100]}")
        try:
            self.feeds_p.write_text(json.dumps(self.feeds, indent=1, sort_keys=True))
        except OSError:
            pass
        self.store.flush()
        return self.stats


def export_news(store, out_path: Path) -> dict:
    floor = (dt.date.today() - dt.timedelta(days=21)).isoformat()
    items, seen = [], set()
    for r in store.iter_query("kind='news_item'"):
        d = (r.get("published") or "")[:10]
        if not d or d < floor:
            continue
        if not keep(r.get("title") or "", r.get("text") or ""):
            continue
        key = re.sub(r"\W+", " ", (r.get("title") or "").lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        m = r.get("meta") or {}
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except ValueError:
                m = {}
        items.append({"t": r.get("title") or "", "u": r["url"], "s": m.get("outlet_name") or m.get("outlet") or "",
                      "d": d, "sum": r.get("text") or "", "tags": m.get("tags") or [], "also": [], "auto": 1})
    items.sort(key=lambda x: x["d"], reverse=True)
    items = items[:NEWS_KEEP]
    window = f"{items[-1]['d']} to {items[0]['d']}" if items else ""
    out_path.write_text(json.dumps({"updated": _now(), "window": window, "note": "Headlines and the outlets' own excerpts, "
                                    "collected by the crawler from Georgia outlets' news feeds; every line links to the story.",
                                    "items": items}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"items": len(items), "window": window}
