"""`gacrawl export`: the import-ready bundle for the Electo site.

dist/electo/
  manifest.json                 what's in the bundle, counts, schema version
  schema.md                     field reference
  jurisdictions.json            all 696 jurisdictions: ids, FIPS, counties, population, website, portals,
                                council schedule, officials, crawl status per source, record counts
  jurisdictions/<id>.json       that jurisdiction's records grouped by kind
  councils/<id>.json            how often each pair of council members voted the same way (non-unanimous votes)
  candidates.json               candidate index with record counts
  candidates/<id>.json          a candidate's records (primary/reported only; never unverified)
  packets/<key>.json            Electo research packets: opened sources in {t,u,d} form, votes by topic,
                                mentions, statements, endorsements, money, posts, leads, rumors, profile
  profiles/<key>.json           profile supplements in Electo's item shapes (ideology, positions, statements,
                                ratings, endorsements, votes, bills, ordinances supported/opposed, career, money)
  connections.json              the donors / organizations / candidates / offices graph and its patterns
  elections.json                city/county-run election results and qualifying lists (Electo archive shape)
  ballot_questions.json         local ballot questions: text, type, amounts, results
  organizations.json            what each organization is, who runs it, who it funds (sourced)
  code_acts.json                every enacted 2023-2026 act's exact Code changes (as-passed text with struck and
                                underlined words, targets, effective dates) for Electo's current Code text
  legislation.json              every 2023-2026 bill and resolution (status, act number, sponsors) in Electo's
                                acts.json item shape
  schools.json, schools/<district>.json, school_bounds.json
                                every Georgia school and college (NCES), district outlines, official statistics
                                (GOSA, CCRPI), board policies, meetings and votes, board members, superintendents
                                and principals (each a research candidate with a packet and profile supplement)
  screenshots.json              the Screenshots box: submitted screenshots by ordinances / posts / text, all needs-review
  verified_posts.json           every post in the verified-posts registry, with status and evidence
  rumor_mill.json               unverified items, kept apart, each with status + corroboration trail
  rumor_mill_video.json         Rumor Mill - Video: meeting videos from channels not confirmed as official accounts
  blocked.json                  hosts that refused the crawler and the suggested manual route

Every record carries a stable `id` (hash of its natural key: same id when the
content changes, so the site can upsert) and `hash` (changes with the content).
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from . import __version__
from .blocked import BlockList
from .candidates import RumorMill, load_candidates
from .registry import all_sources, jurisdictions
from .scheduler import Progress
from .store import Store
from .verified_posts import Registry

SCHEMA_VERSION = "2.1"
SLIM = {"rid", "key", "first_seen", "thash"}

SCHEMA_MD = """# Electo import bundle — field reference (schema {version})

## Record (jurisdictions/<id>.json, candidates/<id>.json)
| field | meaning |
|---|---|
| id | stable id (hash of source, kind, url and the record's own id): use as the upsert key |
| hash | content hash: changes when the record's content changes |
| version | how many times the content has changed since first seen |
| source_id | crawl source (`canton:legistar`, `co-fulton:site`, `cand:jane-doe`) |
| jurisdiction | registry id (`canton`, `co-fulton`) |
| kind | ordinance, matter, meeting, agenda_item, vote, official, page, document, feed_item, ocr, statement, article, mention, social_mention, post, connection, lead |
| url | the page or API resource that was opened (or a link that resource itself gave); never built from a pattern |
| title, text, markdown | content (`markdown` only when it differs from `text`); articles keep excerpts only |
| published | date from the source when it gave one |
| channel | how it was fetched: api, wp_json, feed, sitemap, html, browser, pdf, ocr, import, store |
| evidence | primary (official record / candidate's own channel), reported (listed news outlet), lead (not opened; never cite). Unverified items are only in rumor_mill.json |
| fetched_at, changed_at | when this content was fetched / last changed |
| meta | kind-specific: Legistar ids, vote lists, topics, OCR confidence, outlet, contribution details, opened_url |

## Research packet (packets/<key>.json)
key (Electo person key), name, jurisdictions, note, counts, sources[{{t,u,d,kind,evidence,opened_at,excerpt,in_profile}}],
votes[{{d,item,vote,result,matter,topics,src}}], vote_topics{{topic:{{yes,no,other,examples}}}}, mentions, articles,
statements, endorsements, finance, posts, social_mentions, leads (not opened), rumors (unverified; kept apart).
`src` lists are 0-based indexes into `sources`, the same convention as Electo profiles.

## Profile supplement (profiles/<key>.json)
Electo profile item shapes: ideology + isrc, positions[{{topic,stance,d,src,basis,evidence}}], statements[{{d,text,src}}],
ratings[{{org,rating,year,src}}], endorsements[] + esrc (+ endorsement_items with direction and basis), votes[{{bill,session,
title,vote,d,src,basis}}], bills[{{number,session,caption,role,src}}], ordinances_supported / ordinances_opposed, affiliations,
career[{{d,to,kind,role,body,how,src,basis}}], money{{total_raised,donors,organizations,employers,patterns}}, inference_inputs, depth.

## Open records (open_records.json)
requests[{{jurisdiction, name, how (email|portal), status, sent_at, due, followed_up_at, found_on, portal, received,
history[{{at, status}}]}}], counts. Code of Ordinances requests under the Georgia Open Records Act; no addresses.

## Connections (connections.json)
nodes[{{id,kind,name,attrs}}], edges[{{a,b,type,src,amount,date,role,evidence}}] (contributed, paid, funds, officer_of, employed_by,
committee_of, connected_to, endorsed, serves, sponsored, appointed_to, member_of), patterns[] (shared_donor, employer_cluster,
org_chain, officer_overlap, donor_official, shared_endorser, donor_matter [evidence lead: verify first]).

## Council alignment (councils/<id>.json)
members[], votes_counted, pairs[{{a,b,shared,agree,pct}}] over non-unanimous roll calls.

## Donors (donors.json)

| field | what |
|---|---|
| recipients[] | per candidate or committee: `i`, `name`, `key`, `cid`, `party`, `office`, `level`, `county`, `jur`, `raised`, `n`, `spent`, `nx`, `loans`, `in_kind`, `in_ga`, `small` (gifts under $200), `orgs` (from organizations and committees), `employers[]` ({{name, total}}), `sources`, `first`, `last` |
| gifts[] | donor -> recipient aggregates, largest first (capped): `donor` (entity id), `name`, `kind` (person / org / committee), `city`, `state`, `county`, `employer`, `occupation`, `r` (recipient index), `total`, `n`, `first`, `last`, `fs` (filing source), `u`, `review` |
| spend[] | recipient -> payee: `r`, `payee`, `city`, `state`, `purpose`, `total`, `n`, `u`, `last` |
| donors[] | donors with money to more than one recipient: `id`, `name`, `kind`, `city`, `state`, `county`, `employer`, `total`, `n`, `r[]` |
| by | totals by `donor_state`, `donor_county`, `donor_city`, `party`, `office`, `level`, `kind`, `source`: [{{k, t}}] |

## Why bills passed or died (bill_reasons.json)

| field | what |
|---|---|
| patterns | across every decided bill in the store: `bills`, `became_law`, `rate`, and `by` feature (`passed_first_chamber`, `lead_majority`, `bipartisan_sponsors`, `first_vote_party_line`, `no_floor_vote`): `label`, `n`, `became_law`, `rate` |
| bills[] | `number`, `session`, `session_name`, `caption`, `url` (legis.ga.gov), `outcome` (law / vetoed / died / pending), `stage`, `status`, `status_date`, `act`, `origin`, `committees`, `lead_sponsor` ({{name, party}}), `sponsor_parties`, `bipartisan_sponsors`, `majority_party`, `votes[]` ({{chamber, date, caption, yea, nay, result, party: {{R: [yea, nay], D: [...]}}, party_line}}), `features`, `reasons[]` ({{t, u, d, outlet, excerpts: [{{x, stance}}]}}; stance support / oppose / outcome / context) |

## Policing (police.json, media.json, records_contacts.json)
| field | meaning |
|---|---|
| departments[] | one per county or city whose site links to its police, sheriff or public-safety section: `jurisdiction`, `jurisdiction_name`, `county`, `kind` (police / sheriff / marshal), `name`, `url` |
| departments[].chief | `{{name, title, src}}` exactly as printed on the page `src`; absent when no page named one |
| departments[].phones / emails / address / precincts | each with `src` (the page it was read on); emails only on the government's own domain; precincts `{{name, address, phone, src}}` |
| departments[].complaints / records | the internal-affairs or complaints page and the records page, with the emails and phones printed there |
| outlets[] | news outlets (config/outlets.yaml): `domain`, `name`, `page`, `tips[] {{address, label, src}}` from the outlet's own contact page |
| contacts{{}} | open-records contacts by jurisdiction id: `emails` (an approved address first), `portal`, `page`, `website` |

## Jurisdiction (jurisdictions.json)
id, name, kind (county|city|town|consolidated), fips, counties, county_seat, population, website, website_source,
gma_url, council_meets, regional_commission, incorporated, officials[], portals{{legistar, agenda_center, granicus,
civicclerk, boarddocs, ecode360, amlegal, municode, facebook, youtube}}, tier (a|b|c), sources[] (crawl status),
record_counts{{kind: n}}
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


_HEAD = ("id", "hash", "source_id", "jurisdiction", "kind", "url", "title", "text", "markdown", "published", "channel", "evidence")
_TAIL = ("fetched_at", "changed_at", "version")


def _write_jurisdiction(path: Path, j: dict, store: Store, counts: dict) -> list[dict]:
    """Stream a jurisdiction's records to its file kind by kind, copying each record's stored meta JSON through as
    is (no decode and re-encode); returns the vote records' vote lists for the council alignment table."""
    votes: list[dict] = []
    cols = ",".join(_HEAD + ("meta",) + _TAIL)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"jurisdiction": %s, "name": %s, "exported_at": %s, "records": {' %
                 (json.dumps(j["id"]), json.dumps(j["name"], ensure_ascii=False), json.dumps(_now())))
        first_kind = True
        for kind in sorted(counts):
            if kind == "screenshot":
                continue
            rows = store.raw_iter("jurisdiction=? AND kind=? AND source_id NOT LIKE 'cand:%'", (j["id"], kind), cols=cols)
            first = True
            for r in rows:
                if first:
                    fh.write(("" if first_kind else ", ") + json.dumps(kind) + ": [")
                    first_kind, first = False, False
                else:
                    fh.write(", ")
                head = {k: r[k] for k in _HEAD}
                if head["markdown"] == head["text"]:
                    head.pop("markdown")
                fh.write(json.dumps(head, ensure_ascii=False)[:-1] + ', "meta": ' + (r["meta"] or "{}") + ", " +
                         json.dumps({k: r[k] for k in _TAIL})[1:])
                if kind == "vote":
                    votes.append({"meta": {"votes": json.loads(r["meta"] or "{}").get("votes", [])}})
            if not first:
                fh.write("]")
        fh.write("}}")
    return votes


def _slim(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k not in SLIM}
    if out.get("markdown") == out.get("text"):
        out.pop("markdown", None)
    out.pop("snippet", None)
    out.pop("score", None)
    return out


def council_alignment(votes: list[dict]) -> dict:
    """Pairwise agreement between members over non-unanimous recorded votes."""
    members, shared, agree, counted = Counter(), Counter(), Counter(), 0
    for r in votes:
        vs = {(v.get("person") or "").strip(): (v.get("vote") or "").strip().lower()
              for v in r.get("meta", {}).get("votes", []) if v.get("person")}
        cast = {p: v for p, v in vs.items() if v in ("yea", "nay", "aye", "no", "yes")}
        norm = {p: ("yes" if v in ("yea", "aye", "yes") else "no") for p, v in cast.items()}
        if len(set(norm.values())) < 2:
            continue  # unanimous: says nothing about alignment
        counted += 1
        members.update(norm.keys())
        for a, b in combinations(sorted(norm), 2):
            shared[(a, b)] += 1
            agree[(a, b)] += norm[a] == norm[b]
    pairs = [{"a": a, "b": b, "shared": n, "agree": agree[(a, b)], "pct": round(100 * agree[(a, b)] / n, 1)}
             for (a, b), n in sorted(shared.items(), key=lambda kv: -kv[1]) if n >= 3]
    return {"members": sorted(members), "votes_counted": counted, "pairs": pairs}


def export(root: Path, out: Path, log=print, store: Store | None = None, cfg: dict | None = None,
           packets: bool = True) -> dict:
    for sub in ("jurisdictions", "candidates", "councils", "packets"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    own = store is None
    store = store or Store(root / "data")
    store.flush()
    progress = Progress(root / "state" / "progress.json")
    src_status = {s["id"]: {k: v for k, v in progress.get(s["id"]).items()
                            if k in ("last_ok", "status", "channel", "mode", "new", "seen")} for s in all_sources(root)}
    by_jur_counts = defaultdict(dict)
    for (jid, kind), n in _counts_by_jur_kind(store).items():
        by_jur_counts[jid][kind] = n

    # ---- jurisdictions
    jur = jurisdictions(root)
    status_by_jur = defaultdict(list)
    for sid, st in src_status.items():
        status_by_jur[sid.split(":")[0]].append({"id": sid, **st})
    index, total_records = [], Counter()
    for j in jur:
        counts = by_jur_counts.get(j["id"], {})
        if counts:
            votes = _write_jurisdiction(out / "jurisdictions" / f"{j['id']}.json", j, store, counts)
            if votes:
                al = council_alignment(votes)
                if al["votes_counted"]:
                    (out / "councils" / f"{j['id']}.json").write_text(json.dumps({"jurisdiction": j["id"], **al}, indent=1))
        total_records.update({k: v for k, v in counts.items()})
        entry = {k: v for k, v in j.items() if k not in ("hints", "site_error", "gma_error", "wikipedia_error")}
        entry["sources"] = status_by_jur.get(j["id"], [])
        entry["record_counts"] = counts
        index.append(entry)
    (out / "jurisdictions.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- candidates (only those with something found), profile supplements and packets.
    # Each candidate's records are read once and feed all three outputs.
    cands = load_candidates(root, cfg)
    rm = RumorMill(root)
    rumors = rm.all()
    rumors_by = defaultdict(list)
    for r in rumors:
        rumors_by[r["candidate_id"]].append(r)
    cindex = []
    cand_sources = set(s for s in store.sources() if s.startswith("cand:"))
    n_packets = n_profiles = 0
    graph_counts = {}
    pb, profiles = None, []
    if packets:
        from .electo import packet_for, slug, write_packet_index
        from .profiles import ProfileBuilder

        legis = _load_json(root / "state" / "legis_people.json")
        pb = ProfileBuilder(root, store, cfg, cands, legis)
    for c in cands:
        sid = f"cand:{c['id']}"
        records: dict[str, list] = {}
        if sid in cand_sources:
            for kind in store.kinds(sid):
                records[kind] = [r for r in store.read(sid, kind) if r.get("evidence") != "unverified"]
            grouped = {k: [_slim(r) for r in v] for k, v in records.items()}
            counts = {k: len(v) for k, v in grouped.items()}
            total_records.update(counts)
            slim_c = {k: v for k, v in c.items() if k not in ("variants", "profile_urls")}
            (out / "candidates" / f"{c['id']}.json").write_text(
                json.dumps({"candidate": slim_c, "exported_at": _now(), "records": grouped}, ensure_ascii=False), encoding="utf-8")
            cindex.append({**slim_c, "record_counts": counts})
        if pb is None:
            continue
        prof = pb.profile(c, records)
        if prof:
            profiles.append(prof)
        pk = packet_for(c, records, rumors_by.get(c["id"], []), prof) if (records or rumors_by.get(c["id"])) else None
        if pk:
            (out / "packets" / f"{slug(c.get('electo_key') or c['id'])}.json").write_text(
                json.dumps(pk, ensure_ascii=False, indent=1), encoding="utf-8")
            n_packets += 1
    (out / "candidates.json").write_text(json.dumps(cindex, ensure_ascii=False, indent=1), encoding="utf-8")
    if pb is not None:
        from .connections import export_graph
        from .profiles import write_profiles

        write_packet_index(out / "packets")
        log(f"wrote {n_packets} research packets to {out / 'packets'}")
        pb.finish()
        n_profiles = write_profiles(profiles, out / "profiles", log=log)
        bundle = pb.bundle()
        graph_counts = export_graph(bundle["_graph"], bundle["_patterns"], out / "connections.json")
        orgs = [_slim(r) for r in store.iter_query("kind='organization'")]
        (out / "organizations.json").write_text(json.dumps({"note": "What each organization in the money and endorsement "
                                                            "records is (registries and its own site), who runs it and who it "
                                                            "funds; every field carries its source url.", "organizations": orgs},
                                                           ensure_ascii=False), encoding="utf-8")

    # ---- elections, qualifying lists and ballot questions in Electo shapes
    from .ballot import export_questions
    from .elections import electo_elections, electo_qualifying

    jur_by_id = {j["id"]: j for j in jur}
    (out / "elections.json").write_text(json.dumps({"note": "City/county-run election results in Electo's archive shape "
                                                    "(eids prefixed x); contests with sub 'question' are ballot questions.",
                                                    "elections": electo_elections(store, jur_by_id),
                                                    "qualifying": electo_qualifying(store)}, ensure_ascii=False), encoding="utf-8")
    (out / "ballot_questions.json").write_text(json.dumps({"note": "Local ballot questions: text (sample ballots, notices, "
                                                           "resolutions) paired with results where both were found.",
                                                           "questions": export_questions(store, jur_by_id)}, ensure_ascii=False),
                                               encoding="utf-8")

    # ---- the Code changes of every enacted 2023-2026 act (Electo applies them to its Release 86 text)
    from .codeacts import export_code_acts, export_legislation

    from .schools import export_schools

    schools_counts = export_schools(root, store, out, log=log)
    from .police import export_policing

    policing_counts = export_policing(root, out, jur)
    from .bill_reasons import export_reasons

    reasons_counts = export_reasons(store, root, out / "bill_reasons.json")
    from .donors import export_donors

    donors_counts = export_donors(store, root, cands, out / "donors.json", log=log)
    n_code_acts = export_code_acts(store, out / "code_acts.json")
    n_legislation = export_legislation(store, out / "legislation.json")

    # ---- the Screenshots box (inbox items, grouped ordinances / posts / text)
    from .screens import Inbox

    box = Inbox.box_export(store)
    (out / "screenshots.json").write_text(json.dumps({"note": "Screenshots people submitted, read by OCR. Each item "
                                                      "needs review: open the original page or post before citing it.",
                                                      "boxes": box}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- posts, rumors, blocked
    posts = Registry(root / "data" / "verified").posts()
    (out / "verified_posts.json").write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "rumor_mill.json").write_text(json.dumps(rumors, ensure_ascii=False, indent=1), encoding="utf-8")
    from .rumor_video import VideoRumorMill

    rumor_videos = VideoRumorMill(root).all()
    (out / "rumor_mill_video.json").write_text(json.dumps({
        "note": "Rumor Mill - Video: meeting videos from YouTube channels not confirmed as the government's official "
                "account (reason per row). Never cite these; confirm the channel by hand first.",
        "videos": rumor_videos}, ensure_ascii=False, indent=1), encoding="utf-8")
    blocked = BlockList(root / "state" / "blocked.json").data
    (out / "blocked.json").write_text(json.dumps(blocked, ensure_ascii=False, indent=1), encoding="utf-8")
    n_orr = export_open_records(root, out / "open_records.json")
    (out / "schema.md").write_text(SCHEMA_MD.format(version=SCHEMA_VERSION), encoding="utf-8")

    manifest = {
        "schema_version": SCHEMA_VERSION, "gacrawl_version": __version__, "generated_at": _now(),
        "jurisdictions": len(index), "jurisdictions_with_website": sum(1 for j in index if j.get("website")),
        "jurisdictions_with_records": sum(1 for j in index if j["record_counts"]),
        "councils_with_vote_alignment": len(list((out / "councils").glob("*.json"))),
        "candidates_with_records": len(cindex), "research_packets": n_packets, "profile_supplements": n_profiles,
        "connections": graph_counts,
        "verified_posts": sum(1 for p in posts if p["status"] == "verified"),
        "rumor_mill": dict(Counter(r["status"] for r in rumors)), "blocked_hosts": len(blocked),
        "screenshots": {k: len(v) for k, v in box.items()}, "code_acts": n_code_acts, "rumor_mill_videos": len(rumor_videos), "legislation": n_legislation,
        "schools": schools_counts, "open_records": n_orr,
        "policing": policing_counts,
        "bill_reasons": reasons_counts,
        "donors": donors_counts,
        "records_by_kind": dict(total_records), "records_total": store.total(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if own:
        store.close()
    log(json.dumps(manifest, indent=1))
    return manifest


def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _counts_by_jur_kind(store: Store) -> dict:
    with store._lock:
        rows = store.db.execute("SELECT jurisdiction, kind, COUNT(*) FROM records WHERE jurisdiction<>'' AND "
                                "source_id NOT LIKE 'cand:%' GROUP BY jurisdiction, kind").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def export_open_records(root: Path, path: Path) -> dict:
    """Where each place's Code of Ordinances request stands. No addresses: how it was asked (email / portal),
    status, dates, the official page the contact came from, and any files received."""
    from .openrecords import Tracker

    keep = ("status", "sent_at", "due", "followed_up_at", "found_on", "portal", "updated_at")
    rows = []
    for jid, t in sorted(Tracker(root).data.items()):
        r = {"jurisdiction": jid, "name": t.get("name") or jid,
             "how": "portal" if t.get("portal") or t.get("status") == "portal_only" else "email",
             **{k: t[k] for k in keep if t.get(k)},
             "received": len(t.get("files") or []),
             "history": [{"at": h["at"], "status": h["status"]} for h in t.get("history", [])][-20:]}
        if r["how"] == "email":
            r.pop("portal", None)
        rows.append(r)
    from collections import Counter

    counts = dict(Counter(r["status"] for r in rows if r.get("status")))
    path.write_text(json.dumps({"note": "Georgia Open Records Act requests for each place's current Code of "
                                        "Ordinances (O.C.G.A. 50-18-70). status: queued/portal_only/sent/acknowledged/"
                                        "cost_estimate/partial/fulfilled/denied/bounced/followed_up. Received codes "
                                        "arrive as ordinance records through the Screenshots box.",
                                "counts": counts, "requests": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    return counts
