"""The connections graph: donors, organizations, candidates, offices and the patterns between them.

Nodes are entities (gacrawl.entities): person, candidate, org, committee, plus bodies
(a council, a board) and matters (an ordinance) where an edge needs one.
Edges are observations with the records they rest on:
  contributed   donor -> candidate/committee   amount, date, kind (contribution / in_kind / loan)
  paid          committee -> payee             expenditure
  funds         org/committee -> recipient     what a PAC gives (FEC schedule B / its own filings)
  officer_of    person -> org                  role (treasurer, president, board member ...)
  employed_by   person -> org                  from the donor's listed employer
  committee_of  committee -> candidate         "Friends of X" is X's committee
  connected_to  committee -> org               FEC connected organization
  endorsed      org/person -> candidate
  serves        person -> body                 office records (Legistar, GMA listings)
  sponsored     person -> matter               Legistar sponsors
  appointed     official -> person (body)      appointment announcements
  member_of     person -> org                  stated memberships / caucuses

Patterns (every one lists the record urls it rests on; none is a conclusion):
  shared_donor      one donor gave to two or more candidates
  employer_cluster  three or more donors to one candidate list the same employer
  org_chain         money moved org -> org -> candidate
  officer_overlap   one person is an officer of two or more organizations in the graph,
                    or an officer of an organization that gave to a candidate they sit with
  donor_official    a donor (or their employer) is itself a candidate or office-holder
  donor_matter      a donor's or employer's name appears in a matter the recipient voted on
                    within 180 days of the gift  -> evidence "lead": verify before citing
  shared_endorser   one organization endorsed two or more candidates
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from .entities import EntityIndex, committee_candidate, is_org, norm_org
from .store import Store

MATTER_WINDOW_DAYS = 180


def _d(s: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


class Graph:
    def __init__(self, entities: EntityIndex):
        self.ix = entities
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set[str] = set()
        self.out: dict[str, list[dict]] = defaultdict(list)      # node -> edges leaving it
        self.inn: dict[str, list[dict]] = defaultdict(list)      # node -> edges arriving at it
        self.pattern_index: dict[str, list[int]] = {}            # node -> indexes into the patterns list
        # money a recipient raised with no donor named (a filer's unitemized small gifts, a platform's lump line):
        # recipient (candidate id or entity) -> {platform label: total}; counted in what they raised, never as a donor
        self.lumps: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    def node(self, eid: str, kind: str | None = None, name: str | None = None, **attrs) -> str:
        if eid not in self.nodes:
            e = self.ix.get(eid) if eid in self.ix.entities else None
            self.nodes[eid] = {"id": eid, "kind": kind or (e or {}).get("kind") or "other",
                               "name": name or (e or {}).get("name") or eid, "attrs": {}}
        if attrs:
            self.nodes[eid]["attrs"].update({k: v for k, v in attrs.items() if v not in (None, "")})
        return eid

    def edge(self, a: str, b: str, etype: str, src: str, **attrs) -> dict | None:
        if not a or not b or a == b:
            return None
        key = f"{a}|{b}|{etype}|{attrs.get('date') or ''}|{attrs.get('amount') or ''}|{attrs.get('role') or ''}|{src}|{attrs.pop('line', '')}"
        if key in self._edge_keys:
            return None
        self._edge_keys.add(key)
        e = {"a": a, "b": b, "type": etype, "src": src, **{k: v for k, v in attrs.items() if v not in (None, "")}}
        self.edges.append(e)
        self.out[a].append(e)
        self.inn[b].append(e)
        return e

    def neighbors(self, eid: str, etype: str | None = None, out: bool = True):
        for e in (self.out if out else self.inn).get(eid, ()):
            if etype is None or e["type"] == etype:
                yield e

    def index_patterns(self, pats: list[dict]) -> None:
        """Which patterns mention each node (so a candidate's patterns are a lookup, not a scan of every pattern)."""
        idx: dict[str, list[int]] = defaultdict(list)

        def ids(x, acc):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("id", "donor", "recipient", "person", "endorser") and isinstance(v, str):
                        acc.add(v)
                    else:
                        ids(v, acc)
            elif isinstance(x, list):
                for v in x:
                    ids(v, acc)
        for i, p in enumerate(pats):
            acc: set = set()
            ids(p, acc)
            for n in acc:
                idx[n].append(i)
        self.pattern_index = dict(idx)


def build(store: Store, entities: EntityIndex, jurisdictions: dict | None = None) -> Graph:
    g = Graph(entities)
    # money: every line once, by the shared rules (gacrawl/money.py), so the graph, the profiles' money summaries and
    # the Donors tab count the same money
    from .money import lines

    for L in lines(store):
        m = L["meta"]
        r = {"url": L["url"]}
        donor, rcp = m.get("donor_entity"), m.get("recipient_entity")
        if not donor:
            if L["platform"] and m.get("type", "contribution") == "contribution" and L["amount"] > 0:
                who = m.get("candidate_id") or rcp
                if who:
                    g.lumps[who][L["platform"]] += L["amount"]
            continue
        g.node(donor)
        if rcp:
            g.node(rcp)
        if m.get("type") == "expenditure":
            if rcp:
                g.edge(rcp, donor, "paid", r["url"], amount=m.get("amount"), date=m.get("date"), purpose=m.get("purpose"),
                       evidence="primary", filing=m.get("filing_source"), review=bool(m.get("needs_review")))
        elif rcp:
            g.edge(donor, rcp, "contributed", r["url"], amount=L["amount"], date=m.get("date"), kind=m.get("type"),
                   evidence="primary", filing=m.get("filing_source"), review=bool(m.get("needs_review")),
                   employer=m.get("employer"), occupation=m.get("occupation"),
                   line=L["rid"], **({"via": L["via"]} if L["via"] else {}), **({"platform": L["platform"]} if L["platform"] else {}))
        if m.get("employer_entity"):
            g.node(m["employer_entity"])
            g.edge(donor, m["employer_entity"], "employed_by", r["url"], evidence="primary")
        if m.get("committee_of"):
            g.node(m["committee_of"])
            g.edge(donor, m["committee_of"], "committee_of", r["url"], evidence="primary")
    # organizations: officers, connected orgs, what they fund
    for r in store.iter_query("kind='organization'"):
        m = r["meta"]
        oid = m.get("entity_id")
        if not oid:
            continue
        g.node(oid, what="; ".join(w.get("text", "") for w in (m.get("what") or [])[:2])[:300], org_kind=m.get("org_kind"),
               website=m.get("website"), ein=m.get("ein"), fec_id=m.get("fec_id"))
        for o in m.get("officers") or []:
            if not o.get("name"):
                continue
            pid = entities.resolve(o["name"], "person", url=o.get("src"))
            if pid:
                g.node(pid)
                g.edge(pid, oid, "officer_of", o.get("src") or r["url"], role=o.get("role", ""), evidence="primary")
        if m.get("connected_org"):
            cid = entities.resolve(m["connected_org"], "org", url=r["url"])
            if cid:
                g.node(cid)
                g.edge(oid, cid, "connected_to", r["url"], evidence="primary")
        for rc in m.get("recipients") or []:
            if not rc.get("name"):
                continue
            kind = "committee" if is_org(rc["name"]) else "person"
            rid = entities.resolve(rc["name"], kind, url=rc.get("src"))
            if rid:
                g.node(rid)
                g.edge(oid, rid, "funds", rc.get("src") or r["url"], amount=rc.get("amount"), date=rc.get("date"),
                       purpose=rc.get("purpose"), evidence="primary")
                cn = committee_candidate(rc["name"]) if kind == "committee" else None
                if cn:
                    pid = entities.resolve(cn, "person", url=rc.get("src"))
                    if pid:
                        g.node(pid)
                        g.edge(rid, pid, "committee_of", rc.get("src") or r["url"], evidence="primary")
    # endorsements (candidate research) and memberships
    for r in store.iter_query("kind='connection'"):
        m = r["meta"]
        cand = m.get("candidate_id")
        ceid = entities.by_candidate(cand) if cand else None
        if not ceid:
            continue
        g.node(ceid)
        if m.get("type") == "endorsement" and m.get("party"):
            who = m["party"]
            eid = entities.resolve(who, "org" if is_org(who) else "person", url=r["url"])
            if eid:
                g.node(eid)
                g.edge(eid, ceid, "endorsed", r["url"], date=(r.get("published") or "")[:10],
                       evidence=r.get("evidence") or "primary", self_reported=bool(m.get("self_reported")))
        elif m.get("type") in ("membership", "affiliation") and m.get("party"):
            eid = entities.resolve(m["party"], "org", url=r["url"])
            if eid:
                g.node(eid)
                g.edge(ceid, eid, "member_of", r["url"], role=m.get("role", ""), evidence=r.get("evidence") or "primary")
    # offices held (Legistar office records) and sponsors
    for r in store.iter_query("kind='official'"):
        m = r["meta"]
        name = r.get("title") or m.get("name") or ""
        body = m.get("OfficeRecordBodyName") or m.get("body") or ""
        if not name or not body:
            continue
        pid = entities.resolve(name, "person", url=r["url"])
        if not pid:
            continue
        bid = f"body:{r.get('jurisdiction') or r['source_id'].split(':')[0]}:{norm_org(body)}"
        g.node(pid)
        g.node(bid, "body", body, jurisdiction=r.get("jurisdiction"))
        g.edge(pid, bid, "serves", r["url"], role=m.get("OfficeRecordTitle") or m.get("role", ""),
               start=(m.get("OfficeRecordStartDate") or m.get("start") or "")[:10],
               end=(m.get("OfficeRecordEndDate") or m.get("end") or "")[:10], evidence="primary")
    for r in store.iter_query("kind IN ('ordinance','matter') AND meta LIKE '%\"sponsors\": [{%'"):
        m = r["meta"]
        mid = f"matter:{r['source_id']}:{m.get('MatterId') or m.get('MatterFile')}"
        g.node(mid, "matter", (m.get("MatterFile") or "") + " " + (r.get("title") or "")[:120], jurisdiction=r.get("jurisdiction"),
               status=m.get("MatterStatusName"))
        for sp in m.get("sponsors") or []:
            if sp.get("name"):
                pid = entities.resolve(sp["name"], "person", url=m.get("opened_url") or r["url"])
                if pid:
                    g.node(pid)
                    g.edge(pid, mid, "sponsored", m.get("opened_url") or r["url"], date=(r.get("published") or "")[:10], evidence="primary")
    for r in store.iter_query("kind='appointment'"):
        m = r["meta"]
        if m.get("person") and m.get("body"):
            pid = entities.resolve(m["person"], "person", url=r["url"])
            bid = f"body:{m.get('level') or 'ga'}:{norm_org(m['body'])}"
            if pid:
                g.node(pid)
                g.node(bid, "body", m["body"])
                g.edge(pid, bid, "appointed_to", r["url"], date=(r.get("published") or "")[:10], by=m.get("by", ""),
                       evidence=r.get("evidence") or "primary")
    return g


# ----------------------------------------------------------------------------- patterns
def patterns(g: Graph, store: Store | None = None) -> list[dict]:
    out: list[dict] = []
    contrib = [e for e in g.edges if e["type"] == "contributed"]
    by_donor: dict[str, list[dict]] = defaultdict(list)
    by_rcp: dict[str, list[dict]] = defaultdict(list)
    for e in contrib:
        by_donor[e["a"]].append(e)
        by_rcp[e["b"]].append(e)
    name = lambda i: g.nodes.get(i, {}).get("name", i)  # noqa: E731
    # shared donors
    for donor, es in by_donor.items():
        rcps = defaultdict(float)
        for e in es:
            rcps[e["b"]] += e.get("amount") or 0
        if len(rcps) >= 2:
            out.append({"kind": "shared_donor", "donor": donor, "donor_name": name(donor),
                        "recipients": [{"id": r, "name": name(r), "total": round(t, 2)} for r, t in sorted(rcps.items(), key=lambda kv: -kv[1])],
                        "total": round(sum(rcps.values()), 2), "n": len(es), "src": sorted({e["src"] for e in es})[:20],
                        "evidence": "primary", "note": "filings list the same donor giving to several candidates or committees"})
    # employer clusters
    for rcp, es in by_rcp.items():
        emp = defaultdict(list)
        for e in es:
            if e.get("employer"):
                emp[norm_org(e["employer"])].append(e)
        for k, ees in emp.items():
            donors = {e["a"] for e in ees}
            if len(donors) >= 3:
                out.append({"kind": "employer_cluster", "recipient": rcp, "recipient_name": name(rcp), "employer": ees[0]["employer"],
                            "donors": [{"id": d, "name": name(d)} for d in donors], "total": round(sum(e.get("amount") or 0 for e in ees), 2),
                            "src": sorted({e["src"] for e in ees})[:20], "evidence": "primary",
                            "note": "several donors to one recipient list the same employer"})
    # org chains: org -> org/committee -> candidate (contributed or funds edges). One chain per path, each leg the
    # total of its lines: a PAC's hundreds of lines to a party committee times the party's hundreds of lines out made
    # one chain per pair of lines (lines squared: Georgia's full money histories would make millions)
    money = [e for e in g.edges if e["type"] in ("contributed", "funds")]
    legs: dict[tuple, dict] = {}
    for e in money + [e for e in g.edges if e["type"] == "committee_of"]:
        L = legs.get((e["a"], e["b"]))
        if L is None:
            L = legs[(e["a"], e["b"])] = {"t": 0.0, "money": False, "date": None, "src": e["src"], "top": -1.0}
        if e["type"] != "committee_of":
            amt = e.get("amount") or 0
            L["t"] += amt
            L["money"] = True
            L["date"] = max(x for x in (L["date"], e.get("date")) if x) if (L["date"] or e.get("date")) else None
            if amt > L["top"]:
                L["top"], L["src"] = amt, e["src"]
    gives: dict[str, list[str]] = defaultdict(list)
    for a, b in legs:
        gives[a].append(b)
    for (a, mid), L1 in legs.items():
        if not L1["money"] or g.nodes.get(a, {}).get("kind") not in ("org", "committee") \
                or g.nodes.get(mid, {}).get("kind") not in ("org", "committee"):
            continue
        for b in gives.get(mid, []):
            if g.nodes.get(b, {}).get("kind") in ("candidate", "person", "committee") and b != a:
                L2 = legs[(mid, b)]
                out.append({"kind": "org_chain", "path": [{"id": a, "name": name(a)}, {"id": mid, "name": name(mid)},
                                                          {"id": b, "name": name(b)}],
                            "amounts": [round(L1["t"], 2), round(L2["t"], 2) if L2["money"] else None],
                            "dates": [L1["date"], L2["date"]], "src": [L1["src"], L2["src"]], "evidence": "primary",
                            "note": "money recorded moving from one organization to another and on to a candidate"})
    # officer overlap and officers who are candidates/office-holders
    officer_of: dict[str, list[dict]] = defaultdict(list)
    for e in g.edges:
        if e["type"] == "officer_of":
            officer_of[e["a"]].append(e)
    for pid, es in officer_of.items():
        orgs = {e["b"] for e in es}
        pnode = g.nodes.get(pid, {})
        gave_to = set()
        for o in orgs:
            gave_to.update(gives.get(o, []))
        if len(orgs) >= 2 or pnode.get("kind") == "candidate" or gave_to:
            out.append({"kind": "officer_overlap", "person": pid, "person_name": name(pid), "is_candidate": pnode.get("kind") == "candidate",
                        "organizations": [{"id": o, "name": name(o), "roles": [e.get("role", "") for e in es if e["b"] == o]} for o in orgs],
                        "organizations_gave_to": [{"id": r, "name": name(r)} for r in gave_to],
                        "src": sorted({e["src"] for e in es})[:20], "evidence": "primary",
                        "note": "one person listed as an officer of these organizations (registries / filings)"})
    # donors who are themselves candidates or office-holders
    serves = {e["a"] for e in g.edges if e["type"] in ("serves", "sponsored")}
    for donor, es in by_donor.items():
        k = g.nodes.get(donor, {}).get("kind")
        if k == "candidate" or donor in serves:
            out.append({"kind": "donor_official", "donor": donor, "donor_name": name(donor), "is_candidate": k == "candidate",
                        "recipients": [{"id": e["b"], "name": name(e["b"]), "amount": e.get("amount"), "date": e.get("date")} for e in es],
                        "src": sorted({e["src"] for e in es})[:20], "evidence": "primary",
                        "note": "a candidate or office-holder appears as a donor"})
    # shared endorsers
    endo: dict[str, set] = defaultdict(set)
    endo_src: dict[str, set] = defaultdict(set)
    for e in g.edges:
        if e["type"] == "endorsed":
            endo[e["a"]].add(e["b"])
            endo_src[e["a"]].add(e["src"])
    for org, cands in endo.items():
        if len(cands) >= 2:
            out.append({"kind": "shared_endorser", "endorser": org, "endorser_name": name(org),
                        "candidates": [{"id": c, "name": name(c)} for c in cands], "src": sorted(endo_src[org])[:20],
                        "evidence": "primary", "note": "the same organization or person endorsed these candidates"})
    # donor names in matters the recipient voted on (lead: verify)
    if store is not None:
        out += donor_matter_leads(g, store, by_rcp)
    return out


def donor_matter_leads(g: Graph, store: Store, by_rcp: dict) -> list[dict]:
    """A donor's (or their employer's) name inside a matter the recipient voted on, within MATTER_WINDOW_DAYS.
    Each vote's text is normalized once and indexed by token, so a name is matched against the few votes that
    contain all its words instead of against every vote."""
    out = []
    norm_cache: dict[str, str] = {}

    def nrm(x: str) -> str:
        v = norm_cache.get(x)
        if v is None:
            v = norm_cache[x] = norm_org(x)
        return v

    for rcp, es in by_rcp.items():
        node = g.nodes.get(rcp, {})
        if node.get("kind") != "candidate":
            continue
        cand_id = (g.ix.get(rcp) or {}).get("attrs", {}).get("candidate_id")
        if not cand_id:
            continue
        votes = store.raw("source_id=? AND kind='vote'", (f"cand:{cand_id}",))   # meta decoded only for matches
        if not votes:
            continue
        vinfo, postings = [], defaultdict(set)
        for i, v in enumerate(votes):
            hay = nrm(f"{v['title'] or ''} {v['text'] or ''}")
            vinfo.append((_d(v["published"]), f" {hay} "))
            for tok in set(hay.split()):
                postings[tok].add(i)
        term_hits: dict[str, list[int]] = {}

        def hits_for(nt: str) -> list[int]:
            h = term_hits.get(nt)
            if h is None:
                cand = None
                for tok in sorted(nt.split(), key=lambda t: len(postings.get(t, ()))):
                    p = postings.get(tok)
                    cand = set(p) if (p and cand is None) else (cand & p if p else set())
                    if not cand:
                        break
                h = term_hits[nt] = sorted(i for i in (cand or ()) if f" {nt} " in vinfo[i][1])
            return h

        for e in es:
            donor_name = g.nodes.get(e["a"], {}).get("name", "")
            terms = [t for t in (donor_name, e.get("employer")) if t and len(nrm(t)) >= 6]
            if not terms:
                continue
            d0 = _d(e.get("date"))
            matched: set[int] = set()
            for t in terms:
                for i in hits_for(nrm(t)):
                    if i in matched:
                        continue
                    vd = vinfo[i][0]
                    if d0 and vd and abs((vd - d0).days) > MATTER_WINDOW_DAYS:
                        continue
                    matched.add(i)
                    v = votes[i]
                    out.append({"kind": "donor_matter", "donor": e["a"], "donor_name": donor_name, "term": t,
                                "recipient": rcp, "recipient_name": node.get("name"), "amount": e.get("amount"), "gift_date": e.get("date"),
                                "matter": v["title"], "vote": json.loads(v["meta"] or "{}").get("vote"), "vote_date": v["published"],
                                "days_apart": abs((vd - d0).days) if d0 and vd else None,
                                "src": [e["src"], v["url"]], "evidence": "lead",
                                "note": "a donor's or employer's name appears in a matter the recipient voted on; "
                                        "open both records and confirm it is the same entity before citing"})
    return out


# ----------------------------------------------------------------------------- per-candidate money summary
def money_for(g: Graph, store: Store, entities: EntityIndex, candidate_id: str, pats: list[dict] | None = None) -> dict:
    eid = entities.by_candidate(candidate_id)
    if not eid:
        return {}
    ins = [e for e in g.inn.get(eid, ()) if e["type"] == "contributed"]
    outs = [e for e in g.out.get(eid, ()) if e["type"] == "paid"]
    by_donor: dict[str, dict] = {}
    for e in ins:
        d = by_donor.setdefault(e["a"], {"id": e["a"], "name": g.nodes[e["a"]]["name"], "kind": g.nodes[e["a"]]["kind"], "total": 0.0,
                                         "n": 0, "first": e.get("date"), "last": e.get("date"), "src": []})
        d["total"] += e.get("amount") or 0
        d["n"] += 1
        d["first"] = min(x for x in (d["first"], e.get("date")) if x) if (d["first"] or e.get("date")) else None
        d["last"] = max(x for x in (d["last"], e.get("date")) if x) if (d["last"] or e.get("date")) else None
        if e["src"] not in d["src"] and len(d["src"]) < 5:
            d["src"].append(e["src"])
        if e.get("employer"):
            d.setdefault("employer", e["employer"])
        if e.get("occupation"):
            d.setdefault("occupation", e["occupation"])
    orgs = []
    for d in by_donor.values():
        d["total"] = round(d["total"], 2)
        if d["kind"] in ("org", "committee"):
            n = g.nodes.get(d["id"], {}).get("attrs", {})
            d["what"] = n.get("what", "")
            d["org_kind"] = n.get("org_kind", "")
            d["officers"] = [{"name": g.nodes[e["a"]]["name"], "role": e.get("role", ""), "src": e["src"]}
                             for e in g.inn.get(d["id"], ()) if e["type"] == "officer_of"][:12]
            d["also_funds"] = [{"name": g.nodes[e["b"]]["name"], "amount": e.get("amount"), "src": e["src"]}
                               for e in g.out.get(d["id"], ()) if e["type"] in ("funds", "contributed") and e["b"] != eid][:12]
            orgs.append(d)
    employers = defaultdict(float)
    for e in ins:
        if e.get("employer"):
            employers[e["employer"]] += e.get("amount") or 0
    if pats and not g.pattern_index:
        g.index_patterns(pats)
    related = [pats[i] for i in g.pattern_index.get(eid, [])] if pats else []
    via = defaultdict(float)
    for e in ins:
        if e.get("via"):
            via[e["via"]] += e.get("amount") or 0
    out_fec = fec_check(store, candidate_id, sum(e.get("amount") or 0 for e in ins if e.get("filing") == "FEC"))
    lump: dict[str, float] = defaultdict(float)
    for k in {candidate_id, eid}:
        for name, v in (g.lumps.get(k) or {}).items():
            lump[name] += v
    return {"entity": eid, "total_raised": round(sum(e.get("amount") or 0 for e in ins) + sum(lump.values()), 2), "contributions": len(ins),
            "unattributed": [{"name": k, "total": round(v, 2)} for k, v in sorted(lump.items(), key=lambda kv: -kv[1])],
            "fec": out_fec, "via": [{"name": k, "total": round(v, 2)} for k, v in sorted(via.items(), key=lambda kv: -kv[1])][:12],
            "total_spent": round(sum(e.get("amount") or 0 for e in outs), 2), "expenditures": len(outs),
            "donors": sorted(by_donor.values(), key=lambda d: -d["total"])[:60],
            "organizations": sorted(orgs, key=lambda d: -d["total"])[:40],
            "employers": [{"employer": k, "total": round(v, 2)} for k, v in sorted(employers.items(), key=lambda kv: -kv[1])[:20]],
            "patterns": related[:40], "sources": sorted({e["src"] for e in ins + outs})[:200]}


_COMMITTEES: dict = {}
SINCE_CYCLE = 2016


def fec_check(store: Store, candidate_id: str, itemized_fec: float) -> dict | None:
    """What the FEC itself reports the candidate's committees raised (every cycle the crawler holds totals for),
    against the FEC lines the crawler counted. Itemized lines can never add up to more than the committee's receipts,
    so more than 5% over means lines are counted twice (or belong to another person): check "mismatch", and the
    page shows no total. Less is normal (gifts under $200 are not itemized). None for a candidate with no FEC
    committee."""
    if not _COMMITTEES:
        with store._lock:
            rows = store.db.execute("SELECT DISTINCT json_extract(meta,'$.candidate_id') AS c, json_extract(meta,'$.committee') AS k "
                                    "FROM records WHERE kind='contribution' AND json_extract(meta,'$.filing_source')='FEC'").fetchall()
        for r in rows:
            if r["c"] and r["k"]:
                _COMMITTEES.setdefault(r["c"], set()).add(r["k"])
        _COMMITTEES.setdefault("__loaded__", set())
    coms = sorted(_COMMITTEES.get(candidate_id) or ())
    if not coms:
        return None
    T = json.loads(store.kv_get("fec_totals") or "{}")
    receipts, cycles, have = 0.0, set(), 0
    for k in coms:
        for cy, t in ((T.get(k) or {}).get("cycles") or {}).items():
            if int(cy) < SINCE_CYCLE:
                continue        # before the receipts the crawler reads (2016 on)
            have += 1
            receipts += float(t.get("receipts") or 0)
            cycles.add(int(cy))
    if not have:
        return {"committees": coms, "check": "unchecked", "itemized": round(itemized_fec, 2)}
    check = "mismatch" if itemized_fec > receipts * 1.05 + 100 else "ok"
    return {"committees": coms, "receipts": round(receipts, 2), "cycles": sorted(cycles), "itemized": round(itemized_fec, 2),
            "check": check}


def fec_cycle_totals(store: Store, candidate_id: str) -> dict:
    """The FEC's own totals for the candidate's campaign committees, added up per two-year cycle (2016 on): receipts (all
    the money in, gifts under $200 included), individual and unitemized gifts, and the date the newest report covers.
    What the Donors tab shows as a federal campaign's money. {} when none has been fetched."""
    if not _COMMITTEES:
        fec_check(store, candidate_id, 0.0)          # loads the candidate -> committees map
    T = json.loads(store.kv_get("fec_totals") or "{}")
    out: dict = {}
    for k in sorted(_COMMITTEES.get(candidate_id) or ()):
        for cy, t in ((T.get(k) or {}).get("cycles") or {}).items():
            if not str(cy).isdigit() or int(cy) < SINCE_CYCLE or not t.get("receipts"):
                continue
            o = out.setdefault(str(cy), {"receipts": 0.0, "individual": 0.0, "unitemized": 0.0, "through": ""})
            o["receipts"] += float(t.get("receipts") or 0)
            o["individual"] += float(t.get("individual_contributions") or 0)
            o["unitemized"] += float(t.get("individual_unitemized_contributions") or 0)
            o["through"] = max(o["through"], (t.get("coverage_end_date") or "")[:10])
    return {cy: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in o.items()} for cy, o in sorted(out.items())}


PATTERN_CAP = 4000            # patterns of each kind in connections.json (largest first); profiles keep their own
MONEY_EDGES = ("contributed", "paid", "employed_by")


def _pattern_size(p: dict) -> float:
    return float(p.get("total") or sum(a or 0 for a in (p.get("amounts") or [])) or p.get("amount") or 0)


def export_graph(g: Graph, pats: list[dict], out: Path) -> dict:
    """connections.json: the graph as Electo reads it (two people linked through one organization or committee), so it
    stays a size Electo's repository takes (GitHub refuses files over 100 MB). Private individuals' gifts and
    employers are left out of the file (they are in donors.json, the donor profiles and each candidate's money, and
    Electo never links people through a private individual); gifts are one edge per donor and recipient (amount the
    total, n the lines, date the newest, src the largest gift's filing); each kind of pattern its PATTERN_CAP largest.
    Returns the whole graph's counts (what the profiles and patterns were built from) and the file's."""
    nodes = g.nodes

    def private(i):
        return nodes.get(i, {}).get("kind") == "person"

    agg: dict[tuple, dict] = {}
    edges = []
    for e in g.edges:
        if e["type"] in MONEY_EDGES and (private(e["a"]) or private(e["b"])):
            continue
        if e["type"] not in ("contributed", "paid"):
            edges.append(e)
            continue
        k = (e["a"], e["b"], e["type"])
        A = agg.get(k)
        amt = e.get("amount") or 0
        if A is None:
            A = agg[k] = {**{x: v for x, v in e.items() if x not in ("employer", "occupation", "via", "platform", "purpose", "line")},
                          "amount": 0.0, "n": 0, "first": e.get("date"), "_top": -1.0}
            edges.append(A)
        A["amount"] += amt
        A["n"] += 1
        if e.get("date"):
            A["first"] = min(x for x in (A.get("first"), e["date"]) if x)
            A["date"] = max(x for x in (A.get("date"), e["date"]) if x)
        if amt > A["_top"]:
            A["_top"], A["src"] = amt, e["src"]
    for A in agg.values():
        A.pop("_top", None)
        A["amount"] = round(A["amount"], 2)
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for p in pats:
        by_kind[p["kind"]].append(p)
    for k in by_kind:
        by_kind[k].sort(key=_pattern_size, reverse=True)
    # the file must stay under GitHub's 100 MB (Nov 9: 113 MB, and Electo refused the whole push): over the cap, the
    # smallest gift and payment edges and the smallest patterns of each kind are left out until it fits, and the
    # file says so (the profiles, donors.json and each candidate's money still count everything)
    cap = float(os.environ.get("CONNECTIONS_MAX_MB") or 90) * 1_000_000
    money = sorted((e for e in edges if e["type"] in ("contributed", "paid")), key=lambda e: -(e.get("amount") or 0))
    n_money, pcap = len(money), PATTERN_CAP
    while True:
        keep = {id(e) for e in money[:n_money]}
        kept_edges = [e for e in edges if e["type"] not in ("contributed", "paid") or id(e) in keep]
        kept_pats = [p for ps in by_kind.values() for p in ps[:pcap]]
        used = {i for e in kept_edges for i in (e["a"], e["b"])}
        for p in kept_pats:
            used.update(v for k, v in p.items() if k in ("donor", "recipient", "person", "endorser") and isinstance(v, str))
            for k in ("path", "recipients", "donors", "organizations", "candidates"):
                used.update(x["id"] for x in p.get(k) or [] if isinstance(x, dict) and x.get("id"))
        trimmed = n_money < len(money) or pcap < PATTERN_CAP
        doc = {"note": "Observations from filings and registries, each with the records it rests on. Patterns are starting "
                       "points for a researcher, not findings; items with evidence 'lead' must be verified before use. Gifts "
                       "are one edge per donor and recipient (amount: their total; n: the filed lines). Private individuals' "
                       "gifts are counted in donors.json, the donor profiles and each candidate's money, not drawn here; "
                       f"each kind of pattern lists its {pcap:,} largest."
                       + (f" To keep the file under GitHub's size limit, only the {n_money:,} largest of {len(money):,} gift and "
                          "payment edges are drawn." if n_money < len(money) else ""),
               "counts": {"nodes": len(used), "edges": len(kept_edges), "patterns": len(kept_pats),
                          "graph": {"nodes": len(nodes), "edges": len(g.edges), "patterns": len(pats)},
                          **({"trimmed": {"money_edges": len(money) - n_money, "pattern_cap": pcap}} if trimmed else {})},
               "nodes": [n for i, n in nodes.items() if i in used], "edges": kept_edges, "patterns": kept_pats}
        text = json.dumps(doc, ensure_ascii=False)
        if len(text.encode("utf-8")) <= cap or (n_money == 0 and pcap <= 100):
            break
        n_money, pcap = int(n_money * 0.75), max(100, int(pcap * 0.75))
    edges = kept_edges
    out.write_text(text, encoding="utf-8")
    return {"nodes": len(nodes), "edges": len(g.edges), "patterns": len(pats),
            "file": {"nodes": len(used), "edges": len(edges), "patterns": len(kept_pats), "bytes": out.stat().st_size}}
