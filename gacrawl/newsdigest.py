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

Federal news (Nov 10, 2026): a story about the federal government (Congress, the President and the White House,
federal agencies, the federal courts) that the Georgia rules above leave out is kept as level "federal", from the same
outlets (their national and wire stories were read and dropped before) and from the federal sections and outlets in
config/outlets.yaml (federal_pages, federal_outlets: the feed each page itself links to). A Georgia story that is also
about the federal government (a bill by one of Georgia's senators) stays a Georgia story and is marked fed. The page's
News tab shows the two levels under a Georgia / Federal toggle.

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
NEWS_KEEP = 150         # Georgia stories in the exported digest
FED_KEEP = 120          # federal stories in it, counted apart so neither level crowds the other out
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
# whole words only ("vot" was inside "pivotal", "court" inside "courtesy", "bond" inside "Bondi", "commission" inside
# "Public Service Commission"); the News tab reads these tags with the story's own words to file it under its topics
TAGS = [("elections", r"\b(elections?|electoral|ballots?|vot(e|es|ed|ers?|ing)|runoffs?|recounts?|precincts?|campaign\w*|"
                      r"candidates?|primary|primaries)\b"),
        ("legislature", r"\b(legislat\w*|general assembly|lawmakers?|state reps?\b|state representatives?|state senat\w*|bill)\b"),
        ("governor", r"\b(governor\w*|kemp|executive order)\b"),
        ("local", r"\b((city|town|county) (council|commission)\w*|board of commissioners|county commissioners?|"
                  r"council(man|woman|member|members)?|mayors?|city manager|county manager|zoning|rezon\w*|annexation|ordinances?)\b"),
        ("schools", r"\b(school board|board of education|superintendent|school district)\b"),
        ("budget", r"\b(budget\w*|millage|tax|taxes|splost|tsplost|e-?splost|appropriat\w*|bonds?)\b"),
        ("courts", r"\b(courts?|judges?|ruling|lawsuits?|indict\w*|district attorney)\b"),
        ("policing", r"\b(sheriff\w*|police|arrest\w*|shooting)\b")]
# set aside before tagging: bodies that share the words of a city's or county's ("Public Service Commission",
# "Georgia Council for the Arts", "former mayor")
TAG_ASIDE = re.compile(r"\b(public service|utility|insurance|agriculture|labor) commission\w*|\bpsc\b|"
                       r"\b(georgia|sports|arts|student|youth|security|advisory|tourism) council\b|\bcouncil (for|of|on) \w+|"
                       r"\b(former|ex|then)[- ](\w+ ){0,3}mayor\b")


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
NOT_NEWS = re.compile(r"\b(restaurants?|favorite|recipes?|things to do|this weekend|ends season|playoffs?|football|basketball|"
                      r"marathon|half marathon|5k|filmmakers?|film festival|to screen|concert|"
                      r"baseball|soccer|softball|volleyball|touchdown|quarterback|fundraising campaign|"
                      r"news quiz|quiz|horoscope|obituar\w*|my five|at your service)\b", re.I)
NATIONAL = re.compile(r"\b(Trump|White House|Oval Office|Pentagon|D\.C\.|Washington|Vance|Cruz|DeSantis|Biden|Harris|"
                      r"Supreme Court|federal (?:health )?policy|midterm (?:voters|losses))\b")
REPORT = re.compile(r"\b(arrest|booking|jail) (report|log)s?\b|\bnewsletter\b", re.I)


# the whole state's news (shown under every county): not a single state House or Senate district's race
STATEWIDE = re.compile(r"\b(kemp|raffensperger|bottoms|gold dome|general assembly|state capitol|gov\.?-elect|governor-elect|"
                       r"lieutenant governor|public service commission|statewide|legislature|"
                       r"control of (the )?(georgia )?(state )?(house|senate)|state (house|senate) (majority|control|is in play))\b", re.I)
AMBIGUOUS = {"washington", "lincoln", "jackson", "franklin", "union", "columbia", "georgia", "america", "early", "price",
             "lumber city", "jefferson", "madison", "monroe", "clay", "grant", "lee", "hart", "berlin", "milan", "rome",
             "perry", "douglas", "baker", "warwick", "allen", "riddleville", "white", "young", "harris", "smith"}
_PLACES: list = []
_COUNTY_OF: dict = {}    # place name (lower case) -> its counties, for the News tab's county filter


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
                    cos = [re.sub(r"\s+County$", "", n)] if j.get("kind") == "county" else list(j.get("counties") or [])
                    _COUNTY_OF.setdefault(n.lower(), [])
                    _COUNTY_OF[n.lower()] += [c for c in cos if c not in _COUNTY_OF[n.lower()]]
        except Exception:  # noqa: BLE001
            names = set()
        _PLACES.append(re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\b") if names else None)
    return _PLACES[0]


# the big cities whose names GEORGIA matches (they are left out of the place list's ambiguity rules)
BIG = {"atlanta": ["Fulton", "DeKalb"], "savannah": ["Chatham"], "augusta": ["Richmond"], "macon": ["Bibb"],
       "columbus": ["Muscogee"], "athens": ["Clarke"], "albany": ["Dougherty"], "valdosta": ["Lowndes"],
       "gwinnett": ["Gwinnett"], "cobb": ["Cobb"], "dekalb": ["DeKalb"], "fulton": ["Fulton"], "clayton": ["Clayton"],
       "cherokee": ["Cherokee"], "forsyth": ["Forsyth"], "chatham": ["Chatham"]}


_AFTER_OK = {"city", "county", "council", "mayor", "police", "commission", "commissioners", "board", "schools", "school",
             "high", "elementary", "middle", "road", "street", "avenue", "ga", "georgia", "area", "residents", "voters",
             "officials", "fire", "public", "planning", "downtown", "municipal", "elections", "election", "city's"}
_BEFORE_OK = {"in", "of", "at", "from", "to", "near", "and", "the", "for", "city", "town", "downtown", "north", "south",
              "east", "west", "around", "outside", "across"}


def _in_a_name(hay: str, a: int, b: int) -> bool:
    """Is the place name at hay[a:b] part of a person's name: next to another capitalized word that is not a
    place word ("Jason Collins", "Butler Smith", "Mayor Tom Reynolds")?"""
    before = re.findall(r"([A-Za-z.'’]+)\W*$", hay[max(0, a - 30):a])
    after = re.findall(r"^\W*([A-Za-z.'’]+)", hay[b:b + 30])
    pb = before[0] if before else ""
    na = after[0] if after else ""
    cap = lambda w: bool(re.match(r"^[A-Z][a-z]+\.?$", w))
    if pb and cap(pb) and pb.lower().strip(".") not in _BEFORE_OK and not hay[max(0, a - 2):a].strip(" ").endswith((",", ".")):
        return True
    if na and cap(na) and na.lower() not in _AFTER_OK and not re.match(r"^\W*[,;:.]", hay[b:b + 3]):
        return True
    return False


_CUE_BEFORE = {"in", "of", "at", "from", "near", "to", "city", "town", "downtown", "outside", "around", "across"}
_CUE_AFTER = {"city", "county", "council", "mayor", "commission", "commissioners", "police", "residents", "school", "schools",
              "area", "officials", "voters", "downtown", "municipal", "city's"}


def _place_cue(hay: str, a: int, b: int) -> bool:
    before = re.findall(r"([A-Za-z.'’]+)\W*$", hay[max(0, a - 30):a])
    after = re.findall(r"^\W*([A-Za-z.'’]+)", hay[b:b + 30])
    return bool((before and before[0].lower() in _CUE_BEFORE) or (after and after[0].lower() in _CUE_AFTER))


def counties_of(title: str, text: str) -> list[str]:
    """The Georgia counties a story names: a county by name ("Wilcox County"), a city by name (its county)."""
    hay = f"{title} {text[:2500]}"
    pl = _places()
    out: list[str] = []
    ms = [m for m in (pl.finditer(hay) if pl else []) if not m.group(0).lower().endswith(" county")]
    # a word that is someone's name in this story ("Rep. Jim Kingston", "Mike Collins") is that person's surname in its
    # other mentions too ("Kingston said"), unless a place word sits next to it ("in Kingston", "Collins City Council")
    named = {m.group(0).lower() for m in ms if _in_a_name(hay, m.start(), m.end())}
    # the state's own news happens in Atlanta (the Capitol, the governor's office): Atlanta is Fulton and DeKalb's only
    # when the city itself is the subject ("Atlanta City Council", "Atlanta residents")
    statewide = bool(STATEWIDE.search(hay[:1500]))
    for m in (pl.finditer(hay) if pl else []):
        k = m.group(0).lower()
        if not k.endswith(" county") and (_in_a_name(hay, m.start(), m.end()) or (k in named and not _place_cue(hay, m.start(), m.end()))):
            continue      # "Jason Collins", "Butler Smith": a surname, not the town
        if statewide and k in BIG and not re.match(r"^\W*(" + "|".join(_CUE_AFTER) + r")\b", hay[m.end():m.end() + 20], re.I):
            continue
        for c in _COUNTY_OF.get(k, []):
            if c not in out:
                out.append(c)
    for m in re.finditer(r"\b(" + "|".join(BIG) + r")\b", hay, re.I):
        if m.group(0).lower() in ("cobb", "dekalb", "fulton", "gwinnett", "clayton", "cherokee", "forsyth", "chatham") or not (out or statewide):
            for c in BIG[m.group(0).lower()]:
                if c not in out:
                    out.append(c)
    return out[:6]


def in_georgia(title: str, text: str) -> bool:
    hay = f"{title} {text[:1500]}"
    pl = _places()
    return bool(GEORGIA.search(hay) or STATEWIDE.search(hay) or re.search(r"\bstate (house|senate)\b", hay, re.I)
                or LOCAL.search(title) or (pl and pl.search(hay)))


def _other_state(title: str) -> bool:
    from .policing_news import OTHER_STATES

    return bool(OTHER_STATES.search(title)) and not GEORGIA.search(title)


def keep(title: str, text: str) -> bool:
    """Georgia government and politics: a civic subject in the headline, or in the excerpt when the headline is not a
    crime or accident report; never a story whose headline is about another state, national sports or travel."""
    hay = f"{title} {text[:1500]}"
    if _other_state(title) or re.search(r"\b(NCAA|NFL|NBA|MLB|SEC|Amtrak|recipe|weather)\b", title):
        return False
    if NOT_NEWS.search(title) or not re.search(r"[A-Za-z]{3,}.*\s.*[A-Za-z]{3,}.*\s.*[A-Za-z]{3,}", title):
        return False      # lifestyle, sports, newsletter headers ("👋 At your service"), headlines under three words
    if NATIONAL.search(title) and not (GEORGIA.search(title) or STATEWIDE.search(title)):
        return False      # Washington's news, unless the headline ties it to Georgia
    if REPORT.search(title) or (CRIME.search(title) and not GOV_TITLE.search(title)):
        return False
    if not (CIVIC.search(title) or CIVIC.search(text[:600])):
        return False
    # every outlet is a Georgia outlet, so a local body in the headline (a council, a commission, a board) is local;
    # otherwise the story must name a Georgia place (a national wire story about Washington or another state is not this tab's)
    return in_georgia(title, text)


# the federal government itself: Congress, the President, the agencies, the federal courts
FEDERAL = re.compile(
    r"\b(Congress|congressional|congress(?:man|woman)|Capitol Hill|"
    r"U\.?S\.? (?:House|Senate|[Ss]enators?|Sen\.|Rep\.|Reps\.|[Rr]epresentatives?|[Cc]ongress\w*|attorney|Supreme Court)|"
    r"(?:Sen\.|Senator) (?:Jon )?Ossoff|(?:Sen\.|Senator) (?:Raphael )?Warnock|"
    r"House (?:Republicans|Democrats|GOP|speaker|committee|Oversight|Judiciary|Ways and Means|Appropriations)|"
    r"Senate (?:Republicans|Democrats|GOP|majority|minority|committee|Judiciary|Finance|Appropriations|confirm\w*)|"
    r"Speaker (?:Mike )?Johnson|Thune|Schumer|Jeffries|filibuster|impeach\w*|White House|Oval Office|President (?:Donald )?Trump|"
    r"Trump administration|Trump|Vance|executive orders?|Supreme Court|SCOTUS|federal (?:judge|court|appeals court|"
    r"government|agenc\w+|workers?|employees?|funding|funds|grants?|budget|law|rule|regulations?|shutdown|prosecutors?|"
    r"officials?|immigration|troops|layoffs|aid|election commission|trade commission|reserve)|appeals court|circuit court|"
    r"government shutdown|Justice Department|DOJ|FBI|Homeland Security|DHS|ICE|Border Patrol|Pentagon|"
    r"Defense Department|War Department|State Department|Treasury|IRS|EPA|FDA|CDC|HHS|FEMA|USDA|"
    r"Education Department|Department of (?:Education|Labor|Energy|Agriculture|Transportation|Veterans Affairs|"
    r"Health and Human Services|Justice|Defense|War|the Interior|Commerce|Housing and Urban Development)|"
    r"Veterans Affairs|Medicare|Social Security|SNAP|Affordable Care Act|Obamacare|tariffs?|National Guard|"
    r"Federal Reserve|the Fed|attorney general Pam Bondi|Bondi|Hegseth|Rubio|Noem|RFK Jr\.|Kennedy Jr\.|"
    r"continuing resolution|reconciliation bill|debt ceiling|NDAA|midterm (?:losses|voters|wave)|(?:national|congressional) midterms?|FEC)\b")
FED_TAGS = [("congress", r"congress|capitol hill|u\.?s\.? (house|senate|senator|sen\.|rep)|house (republicans|democrats|gop|speaker|committee)|"
                         r"senate (republicans|democrats|gop|majority|minority|committee|confirm)|filibuster|speaker johnson|"
                         r"thune|schumer|jeffries|continuing resolution|reconciliation|ndaa|shutdown|impeach"),
            ("white house", r"white house|oval office|president trump|trump administration|\btrump\b|vance|executive order"),
            ("courts", r"supreme court|scotus|federal (judge|court|appeals court)|appeals court|circuit court|justices?\b"),
            ("agencies", r"justice department|\bdoj\b|\bfbi\b|homeland security|\bdhs\b|\bice\b|pentagon|department|treasury|"
                         r"\birs\b|\bepa\b|\bfda\b|\bcdc\b|\bhhs\b|fema|usda|federal (agenc|workers|employees)|federal reserve"),
            ("elections", r"midterm|election|ballot|voters?|voting|campaign|candidate|primary|redistrict|\bfec\b"),
            ("budget", r"budget|appropriat|spending|tariff|tax|debt ceiling|shutdown|medicare|medicaid|social security|snap")]


# not the federal government's news even when an agency is named (seen in the first federal run, Nov 10)
FED_NOT = re.compile(r"\b(homicide|cruise ship|recalls?|recalled|warns consumers|check your (freezers?|pantry|fridge)|UFOs?|"
                     r"affair|celebrity|box office)\b", re.I)


def federal(title: str, text: str) -> bool:
    """A story about the federal government: named in the headline, or twice in the opening of the story's text."""
    if FEDERAL.search(title):
        return True
    return len({m.group(0).lower() for m in FEDERAL.finditer(text[:700])}) >= 2


def level_of(title: str, text: str) -> str | None:
    """"ga" (Georgia government and politics, the rules of keep), "federal" (the federal government's news that keep
    leaves out), or None. Sports, lifestyle, crime reports and newsletter headers are no one's."""
    if keep(title, text):
        return "ga"
    if re.search(r"\b(NCAA|NFL|NBA|MLB|SEC|Amtrak|recipe|weather)\b", title) or NOT_NEWS.search(title) or REPORT.search(title):
        return None
    if not re.search(r"[A-Za-z]{3,}.*\s.*[A-Za-z]{3,}.*\s.*[A-Za-z]{3,}", title):
        return None
    if (CRIME.search(title) or FED_NOT.search(title)) and not re.search(r"\b(Congress|Senate|House|White House|Supreme Court|National Guard)\b", title):
        return None      # a crime report is not federal news because the FBI made the arrest; nor a product recall or a UFO
    return "federal" if federal(title, text) else None


def fed_tags_of(title: str, text: str) -> list[str]:
    hay = f"{title} {text[:1500]}".lower()
    return [t for t, rx in FED_TAGS if re.search(rx, hay)][:3]


def tags_of(title: str, text: str) -> list[str]:
    hay = TAG_ASIDE.sub(" ", f"{title} {text[:1500]}".lower())
    return [t for t, rx in TAGS if re.search(rx, hay)][:3]


class Digest:
    def __init__(self, fetcher, store, root: Path, state_dir: Path, log=print):
        from .registry import load_yaml

        self.f, self.store, self.root, self.log = fetcher, store, root, log
        o = load_yaml(root / "config" / "outlets.yaml") or {}
        self.outlets = [d for d in (o.get("outlets") or []) if isinstance(d, str)]
        self.names = o.get("names") or {}
        # federal news: a section page of an outlet already listed (its politics page, whose own feed link is followed)
        # and outlets that cover the federal government only; their stories are kept only as federal ones
        self.fed_pages = {d: u for d, u in (o.get("federal_pages") or {}).items() if isinstance(u, str) and u.startswith("https://")}
        self.fed_only = {d for d in (o.get("federal_outlets") or []) if isinstance(d, str)}
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
        self.stats = {"outlets": 0, "seen": 0, "kept": 0, "new": 0, "federal": 0}
        # a loop of passes in one process (gacrawl news --every): feeds are asked with ETag / Last-Modified (an unchanged
        # feed answers 304 with no body), a WordPress outlet is asked for its list without the posts' text and only the
        # posts not read before come with theirs, and a story already read this process is not read again
        self.conditional = False
        self.read: set[str] = set()            # outlets that answered (their names, as the page shows them)
        self._wp_known: dict[str, set] = {}    # WordPress outlet -> post ids read with their text
        self._links: set[str] = set()          # story links read this process

    def add(self, url: str, title: str, text: str, date: str, outlet: str, via: str, body: str = "", fed_only: bool = False) -> None:
        self.stats["seen"] += 1
        title = _text(title)[:240]
        lv = level_of(title, text) if url.startswith("http") and title else None
        if lv == "ga" and fed_only:      # a federal outlet or section: its stories are federal ones or nothing
            lv = "federal" if federal(title, text) else None
        if not lv:
            return
        self.stats["kept"] += 1
        fed = lv == "federal"
        self.stats["federal"] += fed
        rec = Record(SOURCE, "news_item", url, title=title, text=_excerpt(text), published=(date or "")[:10] or None,
                     channel=via, jurisdiction="us" if fed else "ga", evidence="reported", key=f"{SOURCE}|{url}",
                     meta={"outlet": outlet, "outlet_name": self.names.get(outlet, outlet),
                           "tags": fed_tags_of(title, text) if fed else tags_of(title, text), "level": lv,
                           "via": via, "opened": True, "counties": [] if fed else counties_of(title, body or text),
                           "statewide": (not fed) and bool(STATEWIDE.search(f"{title} {text}")),
                           # the outlet's own text, for the model that writes the summary (never published: the page
                           # shows the headline, the outlet's excerpt or the checked summary, and links to the story)
                           "body": (body or text)[:3000]})
        self.stats["new"] += self.store.add(rec)

    def from_wp(self, dom: str, api: str) -> list[tuple]:
        after = (dt.date.today() - dt.timedelta(days=NEWS_DAYS)).isoformat() + "T00:00:00"
        url = api.rstrip("/") + "/wp/v2/posts"
        known = self._wp_known.get(dom)
        full = "id,link,date,title,excerpt,content"
        r = self.f.get(url, params={"after": after, "per_page": PER_OUTLET, "_fields": full if known is None else "id,link,date,title"},
                       use_cache=False)
        if r.status != 200 or not r.content.strip().startswith(b"["):
            return []
        self.read.add(self.names.get(dom, dom))
        posts = [p for p in r.json() if isinstance(p, dict)]
        if known is not None:
            new = [p.get("id") for p in posts if p.get("id") not in known]
            posts = []
            if new:     # the text of the posts not read before (one request, the posts by id)
                r = self.f.get(url, params={"include": ",".join(str(i) for i in new), "per_page": len(new), "_fields": full},
                               use_cache=False)
                if r.status == 200 and r.content.strip().startswith(b"["):
                    posts = [p for p in r.json() if isinstance(p, dict)]
        self._wp_known[dom] = (known or set()) | {p.get("id") for p in posts}
        return [(p.get("link") or "", (p.get("title") or {}).get("rendered") or "",
                 _text((p.get("excerpt") or {}).get("rendered") or ""), p.get("date") or "", dom, "wp_json",
                 _text((p.get("content") or {}).get("rendered") or ""), False) for p in posts]

    def feed_of(self, dom: str, page: str | None = None) -> str | None:
        """The feed a page itself links to: the outlet's home page, or (page) its federal section, kept under "fed:dom"
        and preferring the feed named for politics when the page links several."""
        key = f"fed:{dom}" if page else dom
        e = self.feeds.get(key) or {}
        if e.get("checked") and (dt.date.today() - dt.date.fromisoformat(e["checked"])).days < FEED_RECHECK_DAYS:
            return e.get("feed")
        feed = None
        try:
            r = self.f.get(page or f"https://{dom}/", use_cache=False)
            if r.status == 200 and "html" in r.content_type:
                links = [u for u in extract.find_feed_links(r.text, r.url) if not re.search(r"comments", u, re.I)]
                pol = [u for u in links if re.search(r"politic|congress|government|washington", u, re.I)]
                feed = (pol if page and pol else links or [None])[0]
        except Exception:  # noqa: BLE001
            feed = None
        self.feeds[key] = {"feed": feed, "checked": dt.date.today().isoformat()}
        return feed

    def from_feed(self, dom: str, feed: str, fed_only: bool = False) -> list[tuple]:
        from .feeds import parse_feed

        r = self.f.get(feed, use_cache=self.conditional)
        if r.status != 200:
            return []
        self.read.add(self.names.get(dom, dom))
        if getattr(r, "not_modified", False) and self._links:
            return []      # unchanged since this process last read it
        from .channels import _date10

        floor = (dt.date.today() - dt.timedelta(days=NEWS_DAYS)).isoformat()
        out = []
        for item in parse_feed(r.content)[:PER_OUTLET]:
            d = _date10(item.get("published")) or ""
            if d and d < floor:
                continue
            out.append((item.get("link") or "", item.get("title") or "", _text(item.get("summary") or ""),
                        d, dom, "feed", "", fed_only))
        return out

    def _outlet(self, dom: str, fed: bool) -> list[tuple]:
        """One outlet's stories (a worker thread: it only fetches; the fetcher spaces the requests to each host)."""
        if fed:
            feed = self.feed_of(dom, self.fed_pages.get(dom) or f"https://{dom}/")
            return self.from_feed(dom, feed, fed_only=True) if feed else []
        if dom in self.wp:
            return self.from_wp(dom, self.wp[dom])
        feed = self.feed_of(dom)
        return self.from_feed(dom, feed) if feed else []

    def run(self, deadline: float | None = None, workers: int = 8) -> dict:
        """Every outlet once: `workers` outlets at a time (each its own site, so the per-host spacing is unchanged; 180
        outlets one after another took longer than a run's five minutes, and the last ones were never read). The
        stories are added in the outlets' order, whatever order the answers come in, so a run's digest is the same."""
        from concurrent.futures import ThreadPoolExecutor

        from .fetch import OutOfTime

        self.stats = {"outlets": 0, "seen": 0, "kept": 0, "new": 0, "federal": 0}
        order = [d for d in self.outlets if d in self.wp] + [d for d in self.outlets if d not in self.wp]
        # the federal sections and outlets first (each one's own feed, federal stories only), then Georgia's outlets
        jobs = [(d, True) for d in sorted(self.fed_only | set(self.fed_pages))] + [(d, False) for d in order]

        def one(job):
            dom, fed = job
            if deadline and time.monotonic() > deadline:
                return None
            try:
                return self._outlet(dom, fed)
            except OutOfTime:
                return None
            except Exception as exc:  # noqa: BLE001
                self.log(f"  news: {'federal ' if fed else ''}{dom}: {type(exc).__name__}: {str(exc)[:100]}")
                return []

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(one, jobs))
        else:
            results = [one(j) for j in jobs]
        for res in results:
            if res is None:
                continue
            self.stats["outlets"] += 1
            for url, title, text, date, outlet, via, body, fed_only in res:
                if url in self._links:
                    continue
                self._links.add(url)
                self.add(url, title, text, date, outlet, via, body=body, fed_only=fed_only)
        try:
            self.feeds_p.write_text(json.dumps(self.feeds, indent=1, sort_keys=True))
        except OSError:
            pass
        self.store.flush()
        return self.stats


def export_news(store, out_path: Path, text_path: Path | None = None, read: list[str] | None = None) -> dict:
    """The digest in the page's format; with text_path, also {url: the outlet's own text} for the summary model (that
    file is the model's input only and is never published)."""
    floor = (dt.date.today() - dt.timedelta(days=21)).isoformat()
    items, seen, texts = [], set(), {}
    for r in store.iter_query("kind='news_item'"):
        d = (r.get("published") or "")[:10]
        if not d or d < floor:
            continue
        m0 = r.get("meta") or {}
        if isinstance(m0, str):
            try:
                m0 = json.loads(m0)
            except ValueError:
                m0 = {}
        lv = level_of(r.get("title") or "", r.get("text") or "")
        if m0.get("level") == "federal" and lv:      # read from a federal section: federal however the excerpt reads
            lv = "federal"
        if not lv:
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
        # tags, counties and the statewide mark by today's rules (stories stored earlier were filed by older ones)
        co = counties_of(r.get("title") or "", m.get("body") or r.get("text") or "")
        it = {"t": r.get("title") or "", "u": r["url"], "s": m.get("outlet_name") or m.get("outlet") or "",
              "d": d, "sum": r.get("text") or "", "tags": tags_of(r.get("title") or "", r.get("text") or "") or m.get("tags") or [],
              "also": [], "auto": 1, "co": co}
        if lv == "federal":
            it.update({"lv": "federal", "co": [], "tags": fed_tags_of(it["t"], it["sum"])})
        else:
            if federal(it["t"], it["sum"]):
                it["fed"] = 1      # a Georgia story that is also the federal government's (shown under both)
            if STATEWIDE.search(it["t"] + " " + it["sum"]):
                it["sw"] = 1
        items.append(it)
        texts[r["url"]] = m.get("body") or r.get("text") or ""
    items.sort(key=lambda x: (x["d"], x["u"]), reverse=True)     # the same order whatever order the stories were read in
    items = sorted([x for x in items if x.get("lv") != "federal"][:NEWS_KEEP] + [x for x in items if x.get("lv") == "federal"][:FED_KEEP],
                   key=lambda x: (x["d"], x["u"]), reverse=True)
    window = f"{items[-1]['d']} to {items[0]['d']}" if items else ""
    out_path.write_text(json.dumps({"updated": _now(), "window": window, "note": "Headlines and the outlets' own excerpts, "
                                    "collected by the crawler from Georgia outlets' news feeds (federal stories, lv federal, also from national "
                                    "outlets' politics feeds); every line links to the story.",
                                    # the outlets that answered: Electo's import keeps the earlier stories of the others
                                    **({"read": sorted(read)} if read else {}),
                                    "items": items}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if text_path:
        keep_u = {x["u"] for x in items}
        text_path.write_text(json.dumps({u: t for u, t in texts.items() if u in keep_u}, ensure_ascii=False), encoding="utf-8")
    return {"items": len(items), "federal": sum(1 for x in items if x.get("lv") == "federal"), "window": window}
