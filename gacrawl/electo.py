"""Bridge to the Electo site repository (ClockworkBlack/Electo).

Import (`gacrawl electo import --path <electo checkout>`):
  ga/pipeline_archive/people.json     every person on a Georgia ballot 2016-2026, keyed like the site
  ga/pipeline_archive/aliases.json    alternate ballot names -> canonical key
  ga/pipeline_archive/splits.json     keys the site splits into several people (flagged, not merged)
  ga/pipeline_archive/names.json      when present, Electo's name registry: people as its page shows them (namesakes
                                      under their own keys, other ballot names joined), which replaces the two above
  ga/councils.json                    city council / mayor contests -> which city each person ran in
  ga/pipeline_archive/profiles.json   existing profiles: their links seed campaign sites and social
                                      accounts; their source URLs mark what Electo already cites
  ga/localcodes.json                  code-of-ordinances links Electo opened on official pages
-> state/electo_people.json (people, with registry jurisdiction ids) and
   state/electo_codes.json (code links + the official page each was found on)

Export (`gacrawl electo packets`, also part of `gacrawl export`):
  packets/<slug of key>.json per person: sources in Electo's {t, u, d} shape, every one
  opened by the crawler; votes grouped by topic; official-record mentions; statements,
  endorsements, contributions, posts; leads (not opened, never to be cited); rumors (kept apart).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from .names import name_variants
from .registry import jurisdictions as registry_jurisdictions
from .verified_posts import platform_of

PACKET_NOTE = ("Every entry under sources was opened by the crawler (fetched with HTTP 200 on opened_at) and the "
               "excerpt is text from that page. leads were NOT opened: open one before citing it. rumors are "
               "unverified claims kept apart; never cite them as fact. vote_topics are counts of recorded votes, "
               "not stated positions.")


def pkey(name: str) -> str:
    """Electo's person key (hist16/build_archive.py): no parentheticals or quotes, letters only, upper case,
    without JR/SR/MR/MRS/MS/DR/HON and single-letter tokens. "Henry \"Hank\" Johnson Jr." -> HENRY HANK JOHNSON."""
    n = re.sub(r"\([^)]*\)", " ", name or "")
    n = re.sub(r"[“”\"]", " ", n)
    n = re.sub(r"[^A-Za-z' -]", " ", n).upper().replace("'", "").replace("-", " ")
    w = [x for x in n.split() if x not in ("JR", "SR", "MR", "MRS", "MS", "DR", "HON")]
    return " ".join(x for x in w if len(x) > 1)


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


def norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower()).split("#")[0].split("?")[0].rstrip("/")


def _load(p: Path, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


# --------------------------------------------------------------------------- import
def _split_hit(s: dict, r: list) -> bool:
    return ((not s.get("y") or str(r[2]).startswith(s["y"])) and (not s.get("o") or str(r[3]).startswith(s["o"]))
            and (not s.get("c") or (r[4] if len(r) > 4 else "") == s["c"]))


def resolved_people(raw: dict, names: dict) -> dict:
    """Electo's people as its page shows them, from its name registry (ga/pipeline_archive/names.json): a namesake's
    races under the namesake's own key (splits), one person's several ballot names joined under one key (aliases)."""
    reg = names.get("people") or {}
    rules: dict[str, list] = defaultdict(list)  # ballot key -> [(order, person key, rule)]: the first match takes a race
    for k, e in reg.items():
        for m in e.get("moved_in") or []:
            rules[m["from"]].append((m.get("i", 0), k, m))
    for v in rules.values():
        v.sort(key=lambda x: x[0])

    def dest(base: str, r: list) -> str | None:
        return next((k for _, k, m in rules.get(base, ()) if _split_hit(m, r)), None)

    out = {}
    for k, e in reg.items():
        races, seen = [], set()
        sources = [(x, None) for x in e.get("keys") or [k]] + [(m["from"], k) for m in e.get("moved_in") or []]
        for base, want in sources:
            for r in (raw.get(base) or {}).get("r") or []:
                if dest(base, r) != want:
                    continue
                sig = (r[0], r[1], r[3])
                if sig not in seen:
                    seen.add(sig)
                    races.append(r)
        out[k] = {"n": e.get("n") or k, "aka": list(e.get("aka") or []), "r": races, "same_name": e.get("same_name") or []}
    return out


def import_electo(electo: Path, root: Path, log=print) -> dict:
    electo = Path(electo)
    arc = electo / "ga" / "pipeline_archive"
    people = (_load(arc / "people.json") or {}).get("people") or {}
    if not people:
        raise FileNotFoundError(f"no people.json under {arc}")
    aliases = _load(arc / "aliases.json", {}) or {}
    splits = _load(arc / "splits.json", {}) or {}
    councils = (_load(electo / "ga" / "councils.json", {}) or {}).get("counties") or {}
    profiles = (_load(arc / "profiles.json", {}) or {}).get("profiles") or {}
    names = _load(arc / "names.json", {}) or {}
    key_of: dict[str, str] = {}
    if names.get("people"):
        # Electo's name registry: every person once, as its page shows them. City races reported by news sources
        # (extra.json) and by cities (crawl_elections.json) count as races too, as on the page.
        raw = {k: {"n": v.get("n"), "r": list(v.get("r") or [])} for k, v in people.items()}
        for f in ("extra.json", "crawl_elections.json"):
            for k, v in ((_load(arc / f, {}) or {}).get("people") or {}).items():
                raw.setdefault(k, {"n": v.get("n"), "r": []})["r"] += list(v.get("r") or [])
        people = resolved_people(raw, names)
        key_of = {x: k for k, e in names["people"].items() for x in e.get("keys") or [k]}
        aliases = {}
        splits = {k: True for k, v in people.items() if v.get("same_name")}

    jur = registry_jurisdictions(root)
    county_by_alnum = {_alnum(j["name"].removesuffix(" County")): j for j in jur if j["kind"] == "county"}
    munis = [j for j in jur if j["kind"] != "county"]

    def city_id(name: str, county: str | None) -> str | None:
        n = _alnum(name)
        hits = [m for m in munis if _alnum(m["name"]) == n]
        if county and len(hits) > 1:
            hits = [m for m in hits if any(_alnum(c) == _alnum(county) for c in m.get("counties", []))] or hits
        return hits[0]["id"] if hits else None

    # who ran in which city (council / mayor contests)
    city_of: dict[str, set] = defaultdict(set)
    for cslug, cities in councils.items():
        cj = county_by_alnum.get(_alnum(cslug))
        for city, blob in (cities or {}).items():
            cid = city_id(city, cj["name"].removesuffix(" County") if cj else None)
            for contest in (blob or {}).get("c", []):
                for o in contest.get("o", []):
                    if o and cid:
                        city_of[key_of.get(o[0], o[0])].add(cid)

    canon = {k: v for k, v in aliases.items() if isinstance(v, str)}
    extra_names: dict[str, list] = defaultdict(list)
    for a, c in canon.items():
        if a in people:
            extra_names[c].append(people[a].get("n") or a)

    out = {}
    for key, p in people.items():
        if key in canon:  # an alternate ballot name; its races live under the canonical key too
            continue
        races = p.get("r") or []
        levels = sorted({r[9] for r in races if len(r) > 9 and r[9]})
        counties = sorted({r[4] for r in races if len(r) > 4 and r[4]})
        jids = set(city_of.get(key, ()))
        for r in races:
            if len(r) > 9 and r[9] == "local" and r[4]:
                cj = county_by_alnum.get(_alnum(r[4]))
                if cj:
                    jids.add(cj["id"])
                m = re.search(r" - ([A-Z][A-Za-z .'-]+)$", r[3] or "")  # "Council Member Ward 6 - Baxley"
                if m:
                    cid = city_id(m.group(1), r[4])
                    if cid:
                        jids.add(cid)
        last = max(races, key=lambda r: r[2]) if races else None
        parties = [r[5] for r in sorted(races, key=lambda r: r[2]) if len(r) > 5 and r[5]]
        prof = profiles.get(key) or {}
        links = [l.get("u") for l in prof.get("links") or [] if l.get("u")]
        prof_urls = sorted({norm_url(s.get("u")) for s in prof.get("sources") or [] if s.get("u")} | {norm_url(u) for u in links})
        city_ids = sorted(j for j in jids if not j.startswith("co-"))
        out[key] = {
            "key": key, "id": slug(key), "name": p.get("n") or key.title(),
            "aliases": list(dict.fromkeys((p.get("aka") or []) + extra_names.get(key, []))),
            "levels": levels, "counties": counties, "jurisdictions": sorted(jids),
            "jurisdiction": city_ids[0] if city_ids else (sorted(jids)[0] if jids else ""),
            "n_races": len(races),
            "last_race": {"date": last[2], "office": last[3], "level": last[9] if len(last) > 9 else "",
                          "party": last[5] if len(last) > 5 else ""} if last else None,
            "party": parties[-1] if parties else "",
            "races": [{"eid": r[0], "d": r[2], "office": r[3], "county": r[4] if len(r) > 4 else "", "party": r[5] if len(r) > 5 else "",
                       "votes": r[6] if len(r) > 6 else None, "pct": r[7] if len(r) > 7 else None,
                       "outcome": r[8] if len(r) > 8 else "", "level": r[9] if len(r) > 9 else "", "u": r[10] if len(r) > 10 else ""}
                      for r in sorted(races, key=lambda r: r[2])],
            "websites": [u for u in links if not platform_of(u)],
            "social": [u for u in links if platform_of(u)],
            "profile_depth": prof.get("depth") or ("none" if not prof else ""),
            "profile_urls": prof_urls,
            "split": key in splits,
            **({"same_name": p["same_name"]} if p.get("same_name") else {}),
        }

    codes = {}
    lc = _load(electo / "ga" / "localcodes.json", {}) or {}
    for group, kind in (("counties", "county"), ("cities", "city")):
        for name, c in (lc.get(group) or {}).items():
            if not isinstance(c, dict) or not c.get("code_url"):
                continue
            ids = []
            if kind == "county":
                j = county_by_alnum.get(_alnum(name))
                if j:
                    ids.append(j["id"])
                else:  # consolidated governments ("Athens-Clarke"): the consolidated entry and its county
                    cons = [m for m in munis if m["kind"] == "consolidated" and _alnum(m["id"]) == _alnum(name)]
                    for m in cons:
                        ids.append(m["id"])
                        ids += [county_by_alnum[_alnum(x)]["id"] for x in m.get("counties", []) if _alnum(x) in county_by_alnum]
            else:
                jid = city_id(name, c.get("county"))
                if jid:
                    ids.append(jid)
            for jid in ids:
                codes[jid] = {"code_url": c["code_url"], "host": c.get("host", ""), "found_on": c.get("found_on", ""),
                              "title": c.get("title", "")}

    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    meta = {"imported_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "from": str(electo),
            "people": len(out), "with_jurisdiction": sum(1 for v in out.values() if v["jurisdictions"]),
            "with_links": sum(1 for v in out.values() if v["websites"] or v["social"]), "code_links": len(codes)}
    (state / "electo_people.json").write_text(json.dumps({"meta": meta, "people": out}, ensure_ascii=False), encoding="utf-8")
    (state / "electo_codes.json").write_text(json.dumps(codes, indent=1, ensure_ascii=False), encoding="utf-8")
    log(json.dumps(meta, indent=1))
    return meta


def electo_people(root: Path) -> dict:
    return (_load(root / "state" / "electo_people.json", {}) or {}).get("people") or {}


# --------------------------------------------------------------------------- packets
def build_packet(c: dict, records: dict[str, list], rumors: list[dict], profile_urls: set[str]) -> dict:
    sources: list[dict] = []
    index: dict[str, int] = {}

    def src(r: dict, excerpt: str = "") -> int:
        u = r.get("url") or ""
        k = norm_url(u)
        if k in index:
            i = index[k]
            if excerpt and not sources[i].get("excerpt"):
                sources[i]["excerpt"] = excerpt
            return i
        index[k] = len(sources)
        sources.append({"t": (r.get("title") or "").strip()[:200] or u, "u": u, "d": (r.get("published") or "")[:10],
                        "kind": r["kind"], "evidence": r.get("evidence") or "primary",
                        "opened_at": (r.get("fetched_at") or "")[:10], "excerpt": excerpt[:600],
                        "in_profile": k in profile_urls})
        return index[k]

    def d10(r):
        return (r.get("published") or r.get("fetched_at") or "")[:10]

    votes, topics = [], defaultdict(lambda: {"yes": 0, "no": 0, "other": 0, "examples": []})
    for r in sorted(records.get("vote", []), key=d10, reverse=True)[:300]:
        m = r.get("meta", {})
        i = src(r)
        v = (m.get("vote") or "").lower()
        bucket = "yes" if v in ("yea", "yes", "aye", "for") else "no" if v in ("nay", "no", "against") else "other"
        for t in m.get("topics", []):
            topics[t][bucket] += 1
            if len(topics[t]["examples"]) < 5:
                topics[t]["examples"].append(len(votes))
        votes.append({"d": d10(r), "item": r.get("title", "")[:300], "vote": m.get("vote"), "result": m.get("result"),
                      "matter": m.get("matter_file"), "topics": m.get("topics", []), "src": [i]})
    mentions = [{"d": d10(r), "text": (r.get("text") or "")[:600], "record_kind": r.get("meta", {}).get("record_kind"),
                 "src": [src(r, r.get("text") or "")]} for r in sorted(records.get("mention", []), key=d10, reverse=True)[:60]]
    articles = [{"d": d10(r), "outlet": r.get("meta", {}).get("outlet"), "src": [src(r, (r.get("text") or ""))]}
                for r in sorted(records.get("article", []), key=d10, reverse=True)[:60]]
    statements = [{"d": d10(r), "text": (r.get("text") or "")[:600], "src": [src(r, (r.get("text") or "")[:600])]}
                  for r in sorted(records.get("statement", []), key=d10, reverse=True)[:40]]
    posts = [{"d": d10(r), "text": (r.get("text") or "")[:600], "platform": r.get("meta", {}).get("platform"),
              "src": [src(r, (r.get("text") or "")[:600])]} for r in sorted(records.get("post", []), key=d10, reverse=True)[:60]]
    endorsements, finance = [], []
    for r in records.get("connection", []):
        m = r.get("meta", {})
        if m.get("type") == "endorsement":
            endorsements.append({"name": m.get("party"), "src": [src(r)]})
        elif m.get("type") == "contribution":
            finance.append({"name": m.get("name"), "amount": m.get("amount"), "date": m.get("date"),
                            "employer": m.get("employer"), "src": [src(r)]})
    social = [{"d": d10(r), "text": (r.get("text") or "")[:400], "author": r.get("meta", {}).get("author"),
               "src": [src(r, (r.get("text") or "")[:400])]} for r in records.get("social_mention", [])[:30]]
    leads = [{"t": r.get("title", "")[:200], "u": r.get("url"), "outlet": r.get("meta", {}).get("outlet"),
              "d": d10(r), "why": r.get("meta", {}).get("why", "not opened")} for r in records.get("lead", [])[:40]]
    return {
        "key": c.get("electo_key") or c.get("key"), "candidate_id": c["id"], "name": c["name"],
        "aliases": c.get("aliases", []), "jurisdictions": c.get("jurisdictions") or ([c["jurisdiction"]] if c.get("jurisdiction") else []),
        "note": PACKET_NOTE, "split_key": bool(c.get("split")),
        "counts": {"sources": len(sources), "new_sources": sum(1 for s in sources if not s["in_profile"]),
                   "votes": len(votes), "mentions": len(mentions), "articles": len(articles), "leads": len(leads),
                   "rumors": len(rumors)},
        "sources": sources, "votes": votes, "vote_topics": dict(topics), "mentions": mentions, "articles": articles,
        "statements": statements, "endorsements": endorsements, "finance": finance, "posts": posts,
        "social_mentions": social, "leads": leads,
        "rumors": [{"id": x["id"], "claim": x["claim"], "u": x["url"], "status": x["status"],
                    "first_seen": x["first_seen"][:10], "opened": x.get("opened", False)} for x in rumors],
    }


def packet_for(c: dict, grouped: dict, rumors: list[dict], profile: dict | None = None) -> dict | None:
    """One candidate's research packet from records already grouped by kind (unverified ones excluded by the caller);
    None when there is nothing to put in it."""
    if not any(grouped.values()) and not rumors:
        return None
    pk = build_packet(c, grouped, rumors, set(c.get("profile_urls") or []))
    if profile:
        pk["profile"] = {k: v for k, v in profile.items() if k not in ("name", "candidate_id", "key", "has_new")}
        pk["counts"]["inference_inputs"] = profile.get("inference_inputs", 0)
    return pk


def write_packet_index(out_dir: Path) -> None:
    index = sorted((p.stem for p in out_dir.glob("*.json") if p.name != "index.json"))
    (out_dir / "index.json").write_text(json.dumps({"packets": index, "note": PACKET_NOTE}, indent=1), encoding="utf-8")


def write_packets(store, candidates: list[dict], rumors: list[dict], out_dir: Path, log=print,
                  profiles: dict | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    by_cand = defaultdict(list)
    for r in rumors:
        by_cand[r["candidate_id"]].append(r)
    n = 0
    for c in candidates:
        sid = f"cand:{c['id']}"
        grouped = defaultdict(list)
        for kind in store.kinds(sid):
            grouped[kind] = [r for r in store.read(sid, kind) if r.get("evidence") != "unverified"]
        if not any(grouped.values()) and not by_cand.get(c["id"]):
            continue
        pk = build_packet(c, grouped, by_cand.get(c["id"], []), set(c.get("profile_urls") or []))
        if profiles and c["id"] in profiles:
            pk["profile"] = {k: v for k, v in profiles[c["id"]].items() if k not in ("name", "candidate_id", "key")}
            pk["counts"]["inference_inputs"] = profiles[c["id"]].get("inference_inputs", 0)
        name = slug(c.get("electo_key") or c["id"])
        (out_dir / f"{name}.json").write_text(json.dumps(pk, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    index = sorted((p.stem for p in out_dir.glob("*.json") if p.name != "index.json"))
    (out_dir / "index.json").write_text(json.dumps({"packets": index, "note": PACKET_NOTE}, indent=1), encoding="utf-8")
    log(f"wrote {n} research packets to {out_dir}")
    return n


def candidate_from_person(p: dict) -> dict:
    """An Electo person as a research candidate."""
    return {"id": p["id"], "electo_key": p["key"], "name": p["name"], "aliases": p.get("aliases", []),
            "office": (p.get("last_race") or {}).get("office", ""), "jurisdiction": p.get("jurisdiction", ""),
            "jurisdictions": p.get("jurisdictions", []), "counties": p.get("counties", []), "levels": p.get("levels", []),
            "websites": p.get("websites", []), "social": p.get("social", []), "news_feeds": [],
            "profile_urls": p.get("profile_urls", []), "profile_depth": p.get("profile_depth", ""),
            "last_race_date": (p.get("last_race") or {}).get("date", ""), "split": p.get("split", False),
            "party": p.get("party", ""), "races": p.get("races", []),
            "variants": name_variants(p["name"], p.get("aliases", []))}
