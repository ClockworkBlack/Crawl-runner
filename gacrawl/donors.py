"""donors.json: every contribution and expenditure the crawler holds, aggregated for Electo's Donors tab.

Built at export from the `contribution` records (FEC schedule A receipts, Georgia CCDR filings a city or county
posted, imported Commission exports; expenditures and loans are the same records with meta.type). Nothing is
inferred: a row is a filing line as the filer reported it; donor identity is the filer's spelling plus the
entity ids `gacrawl finance` resolved (the same donor across filings shares one id).

  recipients[]     one per candidate or committee that received money: name, Electo key, party, office, level,
                   county, totals (raised, contributions, spent, expenditures, loans, in-kind), shares (in Georgia,
                   small gifts under $200, from organizations and committees), top employers, top payees
  gifts[]          donor -> recipient aggregates, largest first (capped): donor name, kind (person / org /
                   committee), city, state, county (when the city is a Georgia city the registry knows), employer,
                   occupation, recipient index, total, count, first and last dates, filing source, one source url
  spend[]          recipient -> payee aggregates (expenditures), with the purpose, largest first (capped)
  donors[]         one per donor entity with money to more than one recipient: name, kind, city, state, total,
                   recipients (indexes), for the cross-reference view
  by               totals by donor state, by donor county, by donor city, by recipient party, office and level,
                   by donor kind, by filing source
There is no demographic data in campaign filings beyond employer, occupation and address, so none is invented.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

GIFT_CAP = 25000
# Earmarking platforms: a line "from" one of these is many people's gifts passed through, not one donor. Counted in
# what the recipient raised, listed as a conduit, kept out of donor rankings and donor geography.
CONDUITS = {"C00694323": "WinRed", "C00401224": "ActBlue", "C00797670": "AIPAC PAC (earmarked)"}
CONDUIT_NAME = re.compile(r"^(winred|actblue|democracy engine|anedot)\b", re.I)
CONDUIT_LABEL = {"winred": "WinRed", "actblue": "ActBlue", "democracy engine": "Democracy Engine", "anedot": "Anedot"}
JFC = re.compile(r"\b(victory (fund|committee)|joint fund|jfc)\b", re.I)
ORGISH = re.compile(r"\b(llc|inc|corp|co|company|group|strategies|partners|associates|pac|committee|fund|party|union|association|foundation)\b\.?$|\b(llc|inc|strategies)\b", re.I)
SPEND_CAP = 8000
DONOR_CAP = 6000


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _level(c: dict) -> str:
    lv = c.get("levels") or []
    for k in ("federal", "statewide", "legislative", "judicial", "local", "school"):
        if k in lv:
            return k
    o = (c.get("office") or "").lower()
    if "u.s." in o or "congress" in o or "senate" in o and "state" not in o:
        return "federal"
    return "local" if c.get("jurisdiction") else ""


def export_donors(store, root: Path, candidates: list[dict], out_path: Path, log=print) -> dict:
    from .registry import jurisdictions

    by_id = {c["id"]: c for c in candidates}
    # Georgia city -> county, for a donor's county
    city_county: dict[str, str] = {}
    for j in jurisdictions(root):
        if j.get("kind") != "county" and j.get("counties"):
            city_county[_norm(j["name"])] = j["counties"][-1] if j["name"] == "Atlanta" else j["counties"][0]   # Atlanta: mostly Fulton
    recips: dict[str, dict] = {}
    gifts: dict[tuple, dict] = {}
    spend: dict[tuple, dict] = {}
    donors: dict[str, dict] = {}
    conduits_total: dict[str, float] = defaultdict(float)
    n_rows = 0

    def recip_of(m: dict, r: dict) -> dict:
        cid = m.get("candidate_id")
        c = by_id.get(cid) if cid else None
        key = f"cand:{cid}" if c else "ent:" + str(m.get("recipient_entity") or _norm(m.get("recipient") or ""))
        R = recips.get(key)
        if not R:
            R = recips[key] = {"i": len(recips), "name": c["name"] if c else (m.get("recipient") or ""), "key": (c or {}).get("electo_key") or "",
                               "cid": cid or "", "party": (c or {}).get("party") or "", "office": (c or {}).get("office") or "",
                               "level": _level(c) if c else ("committee" if m.get("recipient_kind") == "committee" else ""),
                               "county": ((c or {}).get("counties") or [""])[0], "jur": (c or {}).get("jurisdiction") or "",
                               "raised": 0.0, "n": 0, "spent": 0.0, "nx": 0, "loans": 0.0, "in_kind": 0.0, "in_ga": 0.0, "small": 0.0,
                               "orgs": 0.0, "employers": defaultdict(float), "sources": set(), "first": "", "last": ""}
        return R

    # each candidate's own committees (a transfer between them is not new money)
    own: dict[str, set] = defaultdict(set)
    for r in store.iter_query("kind='contribution'"):
        m = r.get("meta") or {}
        if m.get("candidate_id") and m.get("committee"):
            own[m["candidate_id"]].add(m["committee"])
    seen: set = set()
    n_memo = n_dup = n_transfer = 0
    for r in store.iter_query("kind='contribution'"):
        m = r.get("meta") or {}
        amt = m.get("amount")
        if amt is None:
            continue
        amt = float(amt)
        if (m.get("memo_code") or "").upper() == "X":
            n_memo += 1
            continue          # a memo line: the money is on another line already
        # the same line read from two pages of one filing, or from an amendment that repeats it
        dk = (m.get("committee") or m.get("recipient") or "", _norm(m.get("name") or ""), (m.get("date") or "")[:10], round(amt, 2),
              m.get("type") or "contribution", m.get("line") or "")
        if m.get("filing_source") == "FEC" and dk in seen:
            n_dup += 1
            continue
        seen.add(dk)
        if m.get("candidate_id") and m.get("contributor_committee_id") and m["contributor_committee_id"] in own.get(m["candidate_id"], ()):
            n_transfer += 1
            continue          # from another of the candidate's own committees: counted where it was first given
        n_rows += 1
        R = recip_of(m, r)
        t = m.get("type") or "contribution"
        d = (m.get("date") or r.get("published") or "")[:10]
        if len(R["sources"]) < 6:
            R["sources"].add(r["url"])
        if d:
            R["first"] = min(R["first"] or d, d)
            R["last"] = max(R["last"], d)
        if t == "expenditure":
            R["spent"] += amt
            R["nx"] += 1
            k = (R["i"], _norm(m.get("name") or ""))
            S = spend.get(k)
            if not S:
                S = spend[k] = {"r": R["i"], "payee": m.get("name") or "", "city": m.get("city") or "", "state": m.get("state") or "",
                                "purpose": (m.get("purpose") or "")[:80], "total": 0.0, "n": 0, "u": r["url"], "last": ""}
            S["total"] += amt
            S["n"] += 1
            S["last"] = max(S["last"], d)
            continue
        if t == "loan":
            R["loans"] += amt
            continue
        if t == "in_kind":
            R["in_kind"] += amt
        R["raised"] += amt
        if amt <= 0:
            continue      # a refund or a correction: nets out of the total, counts as nothing else
        R["n"] += 1
        st = (m.get("state") or "").upper()[:2]
        if st == "GA" or (not st and m.get("city") and _norm(m["city"]) in city_county):
            R["in_ga"] += amt
        if amt < 200:
            R["small"] += amt
        nm = (m.get("name") or "").strip()
        last = (R["name"].split()[-1] if R["name"] else "").lower()
        cm = CONDUIT_NAME.match(nm)
        conduit = CONDUITS.get(m.get("contributor_committee_id") or "") or (cm and CONDUIT_LABEL[cm.group(1).lower()]) or ""
        if not conduit and (m.get("contributor_committee_id") or m.get("donor_is_org")) and (JFC.search(nm) or (len(last) > 2 and re.search(r"\b" + re.escape(last) + r"\b", nm, re.I))):
            conduit = "Joint fundraising / candidate committees"
        if conduit:
            R["conduit"] = R.get("conduit", 0.0) + amt
            R.setdefault("conduits", defaultdict(float))[conduit] += amt
            conduits_total[conduit] += amt
            continue          # many people's gifts in one line: not a donor, no donor geography
        et = (m.get("entity_type") or "").upper()
        if et in ("CAN",) or (len(last) > 2 and "," in nm and nm.lower().split(",")[0].strip() == last and not m.get("donor_is_org")):
            kind = "self"     # the candidate's own money
        elif et in ("ORG", "CCM", "PAC", "COM", "PTY") or m.get("contributor_committee_id") or m.get("donor_is_org") or ("," not in nm and ORGISH.search(nm)):
            kind = "committee" if (et in ("PAC", "COM", "PTY", "CCM") or m.get("contributor_committee_id") or re.search(r"\b(pac|committee|party|fund)\b", nm, re.I)) else "org"
        else:
            kind = "person"
        if kind in ("committee", "org"):
            R["orgs"] += amt
        elif m.get("employer"):
            R["employers"][m["employer"].strip()[:60]] += amt
        did = str(m.get("donor_entity") or "") or _norm(m.get("name") or "")
        k = (did, R["i"])
        G = gifts.get(k)
        if not G:
            city = (m.get("city") or "").strip().title()
            G = gifts[k] = {"donor": did, "name": m.get("name") or "", "kind": kind, "city": city, "state": st or ("GA" if _norm(city) in city_county else ""),
                            "county": city_county.get(_norm(city), "") if (st in ("GA", "") ) else "", "employer": (m.get("employer") or "")[:60],
                            "occupation": (m.get("occupation") or "")[:50], "r": R["i"], "total": 0.0, "n": 0, "first": d, "last": d,
                            "fs": m.get("filing_source") or "", "u": r["url"], "review": bool(m.get("needs_review"))}
        G["total"] += amt
        G["n"] += 1
        if d:
            G["first"] = min(G["first"] or d, d)
            G["last"] = max(G["last"], d)
        D = donors.get(did)
        if not D:
            D = donors[did] = {"id": did, "name": G["name"], "kind": kind, "city": G["city"], "state": G["state"], "county": G["county"],
                               "employer": G["employer"], "total": 0.0, "n": 0, "r": set()}
        D["total"] += amt
        D["n"] += 1
        D["r"].add(R["i"])

    rec_list = sorted(recips.values(), key=lambda x: x["i"])
    for R in rec_list:
        emp = sorted(R["employers"].items(), key=lambda kv: -kv[1])[:8]
        R["employers"] = [{"name": k, "total": round(v, 2)} for k, v in emp]
        R["sources"] = sorted(R["sources"])
        if R.get("conduits"):
            R["conduits"] = [{"name": k, "total": round(v, 2)} for k, v in sorted(R["conduits"].items(), key=lambda kv: -kv[1])]
            R["conduit"] = round(R["conduit"], 2)
        R["office"] = (R["office"] or "").split(";")[0].strip()
        for k in ("raised", "spent", "loans", "in_kind", "in_ga", "small", "orgs"):
            R[k] = round(R[k], 2)
    gift_list = sorted(gifts.values(), key=lambda g: -g["total"])[:GIFT_CAP]
    for g in gift_list:
        g["total"] = round(g["total"], 2)
    spend_list = sorted(spend.values(), key=lambda s: -s["total"])[:SPEND_CAP]
    for s in spend_list:
        s["total"] = round(s["total"], 2)
    multi = [d for d in donors.values() if len(d["r"]) > 1]
    multi.sort(key=lambda d: -d["total"])
    donor_list = [{**d, "total": round(d["total"], 2), "r": sorted(d["r"])} for d in multi[:DONOR_CAP]]
    # totals by dimension (every gift, not only the capped list)
    by: dict[str, dict] = {k: defaultdict(float) for k in ("donor_state", "donor_county", "donor_city", "party", "office", "level", "kind", "source")}
    for g in gifts.values():
        R = rec_list[g["r"]]
        by["donor_state"][g["state"] or "unknown"] += g["total"]
        if g["county"]:
            by["donor_county"][g["county"]] += g["total"]
        if g["city"]:
            by["donor_city"][g["city"] + (", " + g["state"] if g["state"] and g["state"] != "GA" else "")] += g["total"]
        by["party"][R["party"] or "none listed"] += g["total"]
        by["office"][R["office"] or "committee"] += g["total"]
        by["level"][R["level"] or "other"] += g["total"]
        by["kind"][g["kind"]] += g["total"]
        by["source"][g["fs"] or "filing"] += g["total"]
    by_out = {k: [{"k": kk, "t": round(v, 2)} for kk, v in sorted(d.items(), key=lambda kv: -kv[1])[:60]] for k, d in by.items()}
    doc = {"note": "Campaign money as filed: FEC itemized receipts and disbursements for federal candidates, and Georgia "
                   "Campaign Contribution Disclosure Reports the crawler read or that were imported. Each gift is the filer's "
                   "own line (u is the filing it was read from); donors are matched across filings by name and address, "
                   "which can split one person or merge two with the same name. Gifts that came through an earmarking platform (WinRed, ActBlue) are counted in what the recipient raised and listed under conduits, but the platform is not ranked as a donor. Memo lines, the same line read twice, and transfers between a candidate's own committees are left out. Filings carry no demographic data beyond "
                   "employer, occupation and address. review marks rows parsed from a scanned PDF.",
           "generated": _now(), "rows": n_rows, "recipients": rec_list, "gifts": gift_list, "spend": spend_list, "donors": donor_list,
           "by": by_out, "conduits": [{"name": k, "total": round(v, 2)} for k, v in sorted(conduits_total.items(), key=lambda kv: -kv[1])],
           "skipped": {"memo_lines": n_memo, "duplicate_lines": n_dup, "transfers_between_own_committees": n_transfer}, "caps": {"gifts": GIFT_CAP, "spend": SPEND_CAP, "donors": DONOR_CAP}}
    out_path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"rows": n_rows, "recipients": len(rec_list), "gifts": len(gift_list), "spend": len(spend_list), "donors": len(donor_list)}
