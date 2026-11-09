"""Why a Georgia bill passed, was vetoed or died: the official record behind each outcome, and what high-trust outlets
reported about it, for Electo's Bills tab, its AI briefing and its Pass Likely / Pass Not Likely estimates.

Two kinds of evidence, kept apart:

1. The official record (no requests; built at export from what `gacrawl legis update` stored): the outcome (signed
   into law, vetoed, died), how far the bill got (no committee vote, passed its first chamber, passed both), each
   floor vote with the tally by party (party-line or bipartisan), the lead sponsor's party and whether the sponsors
   came from both parties, and the committees it went to. Each line links to the bill's legis.ga.gov page.

2. What was reported (`gacrawl bills reasons`): for each decided bill, a search of the WordPress news API that an
   outlet in config/outlets.yaml advertises on its own home page (`<link rel="https://api.w.org/">`; nothing is
   guessed), for the bill's number within the session's dates. The API returns the article itself; it is kept only
   when it names the bill, and only the sentences around the bill's number that give a reason (supporters' and
   opponents' arguments, a veto, a deadline missed, a committee that never voted, a cost) are stored, each tagged
   support / oppose / outcome. Records: kind `bill_reason`, source news:bill-reasons, one per bill and article.

Patterns (export): how often bills with each feature of the official record ended up law, across every decided
session in the store (for example, bills that passed their first chamber, bills with sponsors from both parties,
bills whose first floor vote split on party lines). Electo uses them for its estimates and its AI briefing.

State: state/wp_outlets.json (outlet -> WordPress API root, or none; checked weekly). Per bill, a kv mark of when its
reasons were last looked for (again after it moves, or after 60 days while its session is current).
Export: bill_reasons.json in the Electo bundle.
"""
from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import extract
from .store import Record

SOURCE = "news:bill-reasons"
VETO_SOURCE = "gov:veto-statements"
# the General Assembly's own press offices (lawmakers' statements about their bills; WordPress sites with a news API).
# Their releases are kept as the sponsors' own words, labelled as such, never as an outlet's reporting.
PRESS_OFFICES = {"house-press.com": "Georgia House Media Services Office (press release)",
                 "senatepress.net": "Georgia Senate Press Office (press release)"}
# the Governor's office lists every year's vetoed bills on its own site; the other years are the links that page
# itself shows (its "Vetoed Legislation" menu), never addresses built here
VETO_START = "https://gov.georgia.gov/executive-action/legislation/vetoed-legislation/2026"
VETO_RECHECK_DAYS = 7
LEGIS = "ga:legis"
RECHECK_DAYS = 60
OUTLET_RECHECK_DAYS = 7
PER_OUTLET = 10
OUTLET_WORKERS = 12     # outlets asked at once for one bill (different hosts)
CUE = re.compile(r"\b(because|due to|after|amid|opposition|oppos(?:ed|e|ing)|opponents?|critics?|criticiz\w+|concerns?|worr\w+|"
                 r"supporters?|backers?|proponents?|advocates?|argu\w+|said the (?:bill|measure|legislation)|"
                 r"died|dies|dead|stalled|stall|never (?:got|received|came up for) a vote|failed|fail(?:s|ing)?|defeated|"
                 r"veto(?:ed|es)?|sine die|crossover day|deadline|committee|cost(?:s|ly)?|price tag|fiscal note|"
                 r"lobby\w*|amend\w*|compromise|negotiat\w+|signed|signs|law|passed|approved|bipartisan|party[- ]line)\b", re.I)
SUPPORT = re.compile(r"\b(supporters?|backers?|proponents?|advocates?|sponsor(?:ed|s)?|champion\w*|praised|applauded)\b", re.I)
OPPOSE = re.compile(r"\b(opponents?|critics?|opposed|oppose|opposition|criticiz\w+|concerns?|worr\w+|warned|objected)\b", re.I)
OUTCOME = re.compile(r"\b(died|dies|dead|stalled|never (?:got|received|came up for) a vote|failed|defeated|veto(?:ed|es)?|"
                     r"sine die|crossover day|signed|signs|became law|passed|approved)\b", re.I)
WORDS = {"HB": "House Bill", "SB": "Senate Bill", "HR": "House Resolution", "SR": "Senate Resolution"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def bill_rx(number: str) -> re.Pattern:
    """HB 960 -> matches 'HB 960', 'HB960', 'H.B. 960', 'House Bill 960' (not HB 9601)."""
    m = re.match(r"([HS])([BR])\s*(\d+)(\w*)", number.replace(" ", ""))
    if not m:
        return re.compile(re.escape(number), re.I)
    ch, kind, n, suf = m.groups()
    word = WORDS[ch + kind]
    return re.compile(r"(?<![\w.])(?:" + ch + r"\.?\s?" + kind + r"\.?|" + word + r")\s*" + n + re.escape(suf) + r"(?!\d)", re.I)


def sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text or "").strip()
    return [s.strip() for s in re.split(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)]))\s+(?=[A-Z“\"])", t) if s.strip()]


def excerpts_for(text: str, number: str, limit: int = 3, width: int = 420) -> list[dict]:
    """Sentences that name the bill (or follow one that does) and give a reason: [{x, stance}] (support / oppose /
    outcome / context), at most `limit`, each at most `width` characters."""
    rx, ss = bill_rx(number), sentences(text)
    out, seen = [], set()
    for i, s in enumerate(ss):
        if not rx.search(s):
            continue
        for j in (i, i + 1, i + 2):
            if j >= len(ss) or j in seen:
                continue
            x = ss[j]
            if not CUE.search(x) or len(x) < 40:
                continue
            seen.add(j)
            stance = "oppose" if OPPOSE.search(x) else "support" if SUPPORT.search(x) else "outcome" if OUTCOME.search(x) else "context"
            out.append({"x": (x[: width - 1] + "…") if len(x) > width else x, "stance": stance})
            if len(out) >= limit:
                return out
    return out


def session_window(years: list[int]) -> tuple[str, str]:
    """News about a session's bills: from the October before it opens to the end of the year after it closes."""
    y0, y1 = min(years), max(years)
    return f"{y0 - 1}-10-01T00:00:00", f"{y1 + 1}-12-31T23:59:59"


def session_ended(years: list[int], today: dt.date | None = None) -> bool:
    today = today or dt.date.today()
    y1 = max(years)
    return today.year > y1 or (today.year == y1 and today.month >= 5)


def outcome_of(meta: dict, ended: bool) -> str | None:
    if meta.get("vetoed"):
        return "vetoed"
    if meta.get("enacted"):
        return "law"
    return "died" if ended else None


def substantive(meta: dict) -> bool:
    n, cap = meta.get("number") or "", (meta.get("caption") or "").lower()
    if n.startswith(("HB", "SB")):
        return True
    return "constitution" in cap and "amend" in cap   # resolutions: only proposed constitutional amendments


class Reasons:
    def __init__(self, fetcher, store, root: Path, state_dir: Path, log=print):
        from .candidates import load_outlets
        from .registry import load_yaml

        self.f, self.store, self.root, self.state_dir, self.log = fetcher, store, root, state_dir, log
        self.outlets, self.exclude = load_outlets(root)
        from .legis_sessions import load as sessions_load

        cfg = load_yaml(root / "config" / "legislature.yaml")
        self.years = sessions_load(root, store)["years"]
        self.key = {int(kb["session"]): set() for kb in (cfg.get("votes") or {}).get("key_bills") or []}
        for kb in (cfg.get("votes") or {}).get("key_bills") or []:
            self.key[int(kb["session"])].add(kb["bill"])
        self.wp_p = state_dir / "wp_outlets.json"
        self.wp = json.loads(self.wp_p.read_text()) if self.wp_p.exists() else {}

    # ------------------------------------------------------------------ outlets with a WordPress news API
    def wp_roots(self, deadline: float | None) -> dict[str, str]:
        from .fetch import OutOfTime

        statewide = []
        try:
            from .registry import load_yaml

            statewide = list((load_yaml(self.root / "config" / "outlets.yaml").get("statewide") or []))
        except Exception:  # noqa: BLE001
            pass
        order = list(PRESS_OFFICES) + statewide + sorted(d for d in self.outlets if d not in statewide)
        today = dt.date.today()
        for dom in order:
            if deadline and time.monotonic() > deadline:
                break
            e = self.wp.get(dom) or {}
            if e.get("checked") and (today - dt.date.fromisoformat(e["checked"])).days < OUTLET_RECHECK_DAYS:
                continue
            root = None
            try:
                r = self.f.get(f"https://{dom}/", use_cache=False)
                if r.status == 200 and "html" in r.content_type:
                    root = extract.find_api_hints(r.text, r.url).get("wp_json")
            except OutOfTime:
                raise
            except Exception:  # noqa: BLE001
                root = None
            self.wp[dom] = {"api": root, "checked": today.isoformat()}
            self.wp_p.write_text(json.dumps(self.wp, indent=1, sort_keys=True))
        return {d: e["api"] for d, e in self.wp.items() if e.get("api") and (d in self.outlets or d in PRESS_OFFICES)}

    # ------------------------------------------------------------------ which bills, in what order
    def todo(self) -> list[dict]:
        out = []
        for b in self.store.iter_query("source_id=? AND kind='bill'", (LEGIS,)):
            m = b["meta"]
            sid = int(m.get("session") or 0)
            years = self.years.get(sid)
            if not years or not substantive(m):
                continue
            ended = session_ended(years)
            oc = outcome_of(m, ended)
            if not oc:
                continue
            mark = self.store.kv_get(f"bill_reasons:{m['legislation_id']}")
            if mark:
                moved = (m.get("status_date") or "") > mark
                stale = (dt.date.today() - dt.date.fromisoformat(mark)).days >= RECHECK_DAYS
                if not moved and (ended or not stale):
                    continue          # looked for since it last moved (a closed session: once is enough)
            prio = 0 if m.get("number") in self.key.get(sid, ()) else 1 if oc in ("law", "vetoed") else 2
            out.append({"b": b, "prio": prio, "years": years, "outcome": oc, "recent": -max(years)})
        out.sort(key=lambda x: (x["prio"], x["recent"], x["b"]["meta"].get("number") or ""))
        return out

    # ------------------------------------------------------------------ one bill
    def bill(self, item: dict, apis: dict[str, str]) -> int:
        from .candidates import domain_of
        from .fetch import OutOfTime

        b, m = item["b"], item["b"]["meta"]
        number = m["number"]
        rx = bill_rx(number)
        after, before = session_window(item["years"])
        n = 0
        word = WORDS.get(number[:2], "")
        q = f"{word} {number.split()[-1]}" if word else number

        def ask(dom_api):
            # one outlet's search; the outlets are different hosts, so they are asked at once (each host still gets
            # one request at a time at its own pace): 37 outlets one after another took 75 s a bill, 23 bills a run
            dom, api = dom_api
            try:
                r = self.f.get(api.rstrip("/") + "/wp/v2/posts", params={"search": q, "after": after, "before": before,
                                                                         "per_page": PER_OUTLET, "_fields": "link,date,title,content"})
            except OutOfTime:
                raise
            except Exception:  # noqa: BLE001
                return dom, None
            if r.status != 200 or not r.content.strip().startswith(b"["):
                return dom, None
            try:
                return dom, (r, r.json())
            except ValueError:
                return dom, None

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, min(OUTLET_WORKERS, len(apis)))) as pool:
            answers = list(pool.map(ask, apis.items()))
        for dom, got in answers:
            if not got:
                continue
            r, posts = got
            if not isinstance(posts, list):
                continue
            for p in posts[:PER_OUTLET]:
                link = p.get("link") or ""
                if not link.startswith("http") or domain_of(link) != dom and not domain_of(link).endswith("." + dom):
                    continue
                _, md = extract.html_to_markdown((p.get("content") or {}).get("rendered") or "", link)
                text = md or ""
                if not rx.search(text):
                    continue
                ex = excerpts_for(text, number)
                if not ex:
                    continue
                title = htmlmod.unescape(re.sub(r"<[^>]+>", "", (p.get("title") or {}).get("rendered") or ""))[:200]
                press = dom in PRESS_OFFICES
                rec = Record(SOURCE, "bill_reason", link, title=title, text="\n".join(e["x"] for e in ex),
                             published=(p.get("date") or "")[:10] or None, channel="wp_json", jurisdiction="ga",
                             evidence="primary" if press else "reported", key=f"{SOURCE}|{m['legislation_id']}|{link}",
                             meta={"legislation_id": m["legislation_id"], "number": number, "session": m.get("session"),
                                   "session_name": m.get("session_name"), "outlet": PRESS_OFFICES.get(dom, dom), "excerpts": ex,
                                   **({"press_release": True} if press else {}),
                                   "opened": True, "opened_url": r.url, "checked": dt.date.today().isoformat()})
                n += self.store.add(rec)
        self.store.kv_set(f"bill_reasons:{m['legislation_id']}", dt.date.today().isoformat())
        return n


def _veto_session(years_by_session: dict[int, list[int]], year: int) -> list[int]:
    return [sid for sid, ys in years_by_session.items() if year in ys]


def vetoes(fetcher, store, root: Path, state_dir: Path, deadline: float | None = None, log=print) -> dict:
    """The Governor's own veto statements (gov.georgia.gov "Vetoed Legislation", one page a year): each year page's
    documents are read and every bill a statement names gets its passage, stored as an official reason (kind
    bill_reason, source gov:veto-statements, evidence official). Read inside the off-peak window like other pages; a
    year is re-read weekly while it is this year or last year, once otherwise."""
    from .fetch import OutOfTime
    from .ingest import _pdf_pages
    from .registry import load_yaml, write_json_atomic

    from .legis_sessions import load as sessions_load

    cfg = load_yaml(root / "config" / "legislature.yaml")
    years_by = sessions_load(root, store)["years"]
    bills: dict[tuple, dict] = {}
    for b in store.iter_query("source_id=? AND kind='bill'", (LEGIS,)):
        m = b["meta"]
        if m.get("number") and m.get("session"):
            bills[(int(m["session"]), re.sub(r"\s+", " ", m["number"].upper()))] = m
    st_p = state_dir / "veto_statements.json"
    st = json.loads(st_p.read_text()) if st_p.exists() else {}
    seen = st.setdefault("years", {})
    today = dt.date.today()
    out = {"years_read": 0, "documents": 0, "statements": 0}

    def get(url):
        r = fetcher.get(url)
        return r if r.status == 200 else None

    try:
        first = get(VETO_START)
        if not first:
            log("  veto statements: the Governor's vetoed-legislation page did not answer")
            return out
        html = first.text
        years = {}
        for href, label in re.findall(r'<a[^>]+href="([^"]*vetoed-legislation/[^"#?]*)"[^>]*>\s*([^<]{2,60}?)\s*</a>', html):
            u = href if href.startswith("http") else "https://gov.georgia.gov" + href
            if "/document/" in u:
                continue
            ym = re.search(r"(20\d\d)", label + " " + u)
            if ym:
                years[u] = int(ym.group(1))
        years[VETO_START] = 2026
        for url, year in sorted(years.items(), key=lambda kv: -kv[1]):
            if deadline and time.monotonic() > deadline:
                break
            last = seen.get(url)
            if last and (year < today.year - 1 or (today - dt.date.fromisoformat(last)).days < VETO_RECHECK_DAYS):
                continue
            page = first if url == VETO_START else get(url)
            if not page:
                continue
            sessions = _veto_session(years_by, year)
            docs = re.findall(r'<a[^>]+href="([^"]*/document/vetoed-legislation/[^"]+)"[^>]*>(.*?)</a>', page.text, re.S)
            for href, label in docs:
                if deadline and time.monotonic() > deadline:
                    break
                du = href if href.startswith("http") else "https://gov.georgia.gov" + href
                label = re.sub(r"<[^>]+>|\s+", " ", label).strip()
                d = get(du)
                if not d or not d.content.startswith(b"%PDF"):
                    continue
                out["documents"] += 1
                try:
                    texts, _ = _pdf_pages(d.content)
                except Exception:  # noqa: BLE001
                    continue
                text = "\n".join(texts)
                if not re.search(r"\bveto", text, re.I):
                    continue          # the vetoed bill's own text, not a statement
                for num in sorted({re.sub(r"\s+", " ", x.upper()) for x in re.findall(r"\b([HS]\.?\s?[BR]\.?\s?\d{1,4})\b", text)}):
                    num = re.sub(r"([HS])\.?\s?([BR])\.?\s?(\d+)", r"\1\2 \3", num)
                    m = next((bills[(sid, num)] for sid in sessions if (sid, num) in bills), None)
                    if not m:
                        continue
                    ex = excerpts_for(text, num, limit=4, width=900)
                    if not ex:
                        continue
                    rec = Record(VETO_SOURCE, "bill_reason", du, title=f"Governor's veto statement: {num} ({year})",
                                 text="\n".join(e["x"] for e in ex), published=f"{year}-05-01", channel="pdf", jurisdiction="ga",
                                 evidence="official", key=f"{VETO_SOURCE}|{m['legislation_id']}|{du}",
                                 meta={"legislation_id": m["legislation_id"], "number": num, "session": m.get("session"),
                                       "session_name": m.get("session_name"), "outlet": "Office of the Governor",
                                       "official": True, "document": label, "excerpts": ex, "opened": True,
                                       "listed_on": url, "checked": today.isoformat()})
                    out["statements"] += store.add(rec)
            seen[url] = today.isoformat()
            out["years_read"] += 1
            log(f"  veto statements {year}: {len(docs)} documents")
    except OutOfTime:
        pass
    store.flush()
    write_json_atomic(st_p, st, indent=1)
    return out


def research(fetcher, store, root: Path, state_dir: Path, deadline: float | None = None, log=print, limit: int | None = None) -> dict:
    from .fetch import OutOfTime

    R = Reasons(fetcher, store, root, state_dir, log)
    try:
        apis = R.wp_roots(deadline)
    except OutOfTime:
        apis = {d: e["api"] for d, e in R.wp.items() if e.get("api")}
    log(f"  outlets with a news API: {len(apis)} ({', '.join(sorted(apis)[:8])}{'…' if len(apis) > 8 else ''})")
    todo = R.todo()
    done = found = 0
    for item in todo:
        if (deadline and time.monotonic() > deadline) or (limit and done >= limit) or not apis:
            break
        try:
            k = R.bill(item, apis)
        except OutOfTime:
            break
        done += 1
        found += k
        if k:
            log(f"{item['b']['meta']['number']} ({item['b']['meta'].get('session_name')}): {k} reported reasons")
        if done % 10 == 0:
            store.flush()
    store.flush()
    return {"bills_checked": done, "bills_waiting": max(0, len(todo) - done), "new_reasons": found, "outlet_apis": len(apis)}


# ----------------------------------------------------------------------------- export
def _majorities(store) -> dict:
    """(session, chamber) -> majority party, from the party of every member in the stored roll calls."""
    c: dict = defaultdict(Counter)
    seen = set()
    for v in store.iter_query("kind='vote' AND source_id=?", (LEGIS,)):
        m = v["meta"]
        for r in m.get("votes") or []:
            k = (m.get("session"), m.get("chamber"), r.get("person_id"))
            if k in seen or not r.get("party"):
                continue
            seen.add(k)
            c[(m.get("session"), m.get("chamber"))][r["party"]] += 1
    return {k: cnt.most_common(1)[0][0] for k, cnt in c.items() if cnt}


def _party_split(votes: list[dict]) -> dict:
    out: dict = {}
    for r in votes or []:
        p = r.get("party") or "?"
        y, n = out.get(p, [0, 0])
        if r.get("vote") == "Yea":
            y += 1
        elif r.get("vote") == "Nay":
            n += 1
        out[p] = [y, n]
    return out


def _party_line(split: dict) -> bool:
    R, D = split.get("R", [0, 0]), split.get("D", [0, 0])
    if sum(R) < 5 or sum(D) < 5:
        return False
    r_yes, d_yes = R[0] / max(1, sum(R)), D[0] / max(1, sum(D))
    return (r_yes >= .9 and d_yes <= .1) or (r_yes <= .1 and d_yes >= .9)


def export_reasons(store, root: Path, out_path: Path) -> dict:
    from .registry import load_yaml

    from .legis_sessions import load as sessions_load

    cfg = load_yaml(root / "config" / "legislature.yaml")
    years = sessions_load(root, store)["years"]
    key: dict = defaultdict(set)
    for kb in (cfg.get("votes") or {}).get("key_bills") or []:
        key[int(kb["session"])].add(kb["bill"])
    party = {}
    for lg in store.iter_query("kind='legislator' AND source_id=?", (LEGIS,)):
        party[lg["meta"].get("member_id")] = lg["meta"].get("party") or ""
    maj = _majorities(store)
    votes_by = defaultdict(list)
    for v in store.iter_query("kind='vote' AND source_id=?", (LEGIS,)):
        votes_by[v["meta"].get("legislation_id")].append(v["meta"])
    reasons_by = defaultdict(list)
    for src in (VETO_SOURCE, "ga:legis-hearing", SOURCE):
        for r in store.iter_query("kind='bill_reason' AND source_id=?", (src,)):
            m = r["meta"]
            reasons_by[m.get("legislation_id")].append({"t": r.get("title") or "", "u": r["url"], "d": r.get("published") or "",
                                                        "outlet": m.get("outlet", ""), "excerpts": m.get("excerpts") or [],
                                                        **({"official": True} if m.get("official") else {}),
                                                        **({"press_release": True} if m.get("press_release") else {}),
                                                        **({"hearing": True, "at": m.get("at")} if m.get("hearing") else {})})
    videos_by = defaultdict(list)
    for r in store.iter_query("kind='bill_video' AND source_id=?", ("ga:legis-video",)):
        m = r["meta"]
        videos_by[m.get("legislation_id")].append({"t": r.get("title") or "", "u": r["url"], "d": m.get("video_date") or "",
                                                   "committee": m.get("committee") or "", "step": m.get("step") or "",
                                                   "step_date": m.get("step_date") or "", "platform": m.get("platform") or ""})
    bills, feats = [], []
    for b in store.iter_query("source_id=? AND kind='bill'", (LEGIS,)):
        m = b["meta"]
        sid = int(m.get("session") or 0)
        ys = years.get(sid)
        if not ys or not substantive(m):
            continue
        ended = session_ended(ys)
        oc = outcome_of(m, ended)
        lid = m.get("legislation_id")
        vs = sorted(votes_by.get(lid, []), key=lambda v: (v.get("date") or "", v.get("VoteId") or 0))
        floor = [v for v in vs if v.get("substantive", True)]
        vlist = []
        for v in floor:
            sp = _party_split(v.get("votes"))
            vlist.append({"chamber": v.get("chamber"), "date": v.get("date"), "caption": v.get("vote_caption") or v.get("vote_name") or "",
                          "yea": v.get("yea"), "nay": v.get("nay"), "result": v.get("result"), "party": sp, "party_line": _party_line(sp)})
        origin = m.get("chamber")
        passed_origin = any(v["chamber"] == origin and v["result"] == "passed" for v in vlist) or bool(m.get("enacted") or m.get("vetoed"))
        passed_other = any(v["chamber"] != origin and v["result"] == "passed" for v in vlist) or bool(m.get("enacted") or m.get("vetoed"))
        failed_floor = any(v["result"] == "failed" for v in vlist)
        stage = "law" if oc == "law" else "vetoed" if oc == "vetoed" else "both chambers" if passed_origin and passed_other else \
            "first chamber" if passed_origin else "failed on the floor" if failed_floor else "no floor vote" if not vlist else "floor"
        sps = m.get("sponsors") or []
        lead = next((s for s in sps if s.get("sequence") == 1), sps[0] if sps else None)
        lead_party = party.get(lead.get("member_id")) if lead else ""
        sp_parties = Counter(party.get(s.get("member_id")) or "?" for s in sps)
        bip = sp_parties.get("R", 0) > 0 and sp_parties.get("D", 0) > 0
        majority = maj.get((sid, origin)) or maj.get((m.get("session"), origin)) or ""
        first_vote = next((v for v in vlist if v["chamber"] == origin), None)
        rs = sorted(reasons_by.get(lid, []), key=lambda x: x["d"], reverse=True)
        rs.sort(key=lambda x: not x.get("official"))     # the Governor's own statement first
        f = {"lead_majority": bool(lead_party and majority and lead_party == majority), "bipartisan_sponsors": bip,
             "passed_first_chamber": passed_origin, "first_vote_party_line": bool(first_vote and first_vote["party_line"]),
             "no_floor_vote": not vlist and not (m.get("enacted") or m.get("vetoed"))}
        if oc and ended:
            feats.append((f, oc == "law"))
        recent = max(ys) >= dt.date.today().year - 3
        keep = rs or (recent and (oc in ("law", "vetoed") or vlist)) or m.get("number") in key.get(sid, ())
        if not keep:
            continue   # older and quieter bills count toward the patterns only
        bills.append({"number": m.get("number"), "session": sid, "session_name": m.get("session_name"), "caption": m.get("caption"),
                      "url": b["url"], "outcome": oc or "pending", "stage": stage, "status": m.get("status"), "status_date": m.get("status_date"),
                      "act": m.get("act") or "", "origin": origin, "committees": [c for c in (m.get("house_committee"), m.get("senate_committee")) if c],
                      "lead_sponsor": {"name": lead.get("name"), "party": lead_party} if lead else None,
                      "sponsor_parties": dict(sp_parties), "bipartisan_sponsors": bip, "majority_party": majority,
                      "votes": vlist, "features": f, "reasons": rs[:10],
                      "videos": sorted(videos_by.get(lid, []), key=lambda v: v["d"])[:6]})
    pats = {}
    labels = {"passed_first_chamber": "passed the chamber it started in", "lead_majority": "had a lead sponsor from the majority party",
              "bipartisan_sponsors": "had sponsors from both parties", "first_vote_party_line": "passed or failed its first floor vote on party lines",
              "no_floor_vote": "never got a floor vote"}
    total = len(feats)
    base = sum(1 for _, ok in feats if ok)
    for k, lab in labels.items():
        sel = [ok for f, ok in feats if f.get(k)]
        if len(sel) >= 10:
            pats[k] = {"label": lab, "n": len(sel), "became_law": sum(sel), "rate": round(sum(sel) / len(sel), 3)}
    doc = {"note": "Why Georgia bills became law, were vetoed or died. `votes`, `stage`, sponsors and committees are from "
                   "legis.ga.gov's own data (each bill's `url`); `reasons` are sentences from news articles in outlets on "
                   "config/outlets.yaml that name the bill, read through each outlet's own news API, each with its link. "
                   "`patterns` are how often decided bills with each feature became law, across the sessions in the store.",
           "generated": _now(), "patterns": {"bills": total, "became_law": base, "rate": round(base / total, 3) if total else None, "by": pats},
           "bills": bills}
    out_path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"bills": len(bills), "with_reasons": sum(1 for b in bills if b["reasons"]), "patterns": len(pats)}
