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

Beside it, donor_profiles/ (export_donors writes it too): one profile for EVERY donor, not only the capped lists,
for the pop-up a donor's name opens on the page:
  dp/XX.json       {donor id: profile}, XX = shard_of(id) (FNV-1a of the id, 128 shards, so the page loads one
                   small file for one donor): name as filed and other spellings, kind, city/state/county, employer
                   and occupation as last filed (individuals), totals, gift count, first and last gift, money by year,
                   every recipient (total, gifts, dates, filing source, one filing link, conduit), the conduits their
                   gifts passed through, the donor's rank; for an organization or committee, what the registries say
                   (FEC registration, IRS exempt status, a hand note: each line with its source), its FEC id, connected
                   organization and website, the recipient row when the committee itself files, and "about" (two
                   neutral sentences the open model wrote from those opened sources, gacrawl donors-ai; never for a
                   person)
  dpi/X.json       the search index, by the first letter of the donor's name: [id, name, kind, total, place,
                   recipients], largest first
A person's profile is the filings and nothing else: no street address (filings give one; the profile keeps the
city), no web search, no model text.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

GIFT_CAP = 12000             # what Electo's Donors tab keeps (ga/crawl_import.py donors_from): more was cut there, by size
GIFT_FLOOR, GIFT_TOP = 10, 60   # every recipient's own largest gifts kept first: at least GIFT_FLOOR, up to GIFT_TOP each
# Earmarking platforms: a line "from" one of these is many people's gifts passed through, not one donor. Counted in
# what the recipient raised, listed as a conduit, kept out of donor rankings and donor geography.
CONDUITS = {"C00694323": "WinRed", "C00401224": "ActBlue"}   # a platform's lump line; an earmark through a PAC is the
                                                             # person's gift, with the PAC named as how it came (via)
CONDUIT_NAME = re.compile(r"^(winred|actblue|democracy engine|anedot)\b", re.I)
CONDUIT_LABEL = {"winred": "WinRed", "actblue": "ActBlue", "democracy engine": "Democracy Engine", "anedot": "Anedot"}
JFC = re.compile(r"\b(victory (fund|committee)|joint fund|jfc)\b", re.I)
# The State Ethics Commission's own "source type" of a contribution line (EFile and PeachFile), which the filer chose:
# what a donor is comes from it first, and from the name only where the filer chose "Other" or nothing. Guessed from
# the name alone, "The Justice Project", "Save America" and "AFSCME" were listed as people (Nov 9).
SELF_OVER = 100000.0       # a person's gifts to one campaign committee above this can only be the candidate's own money


def source_kind(source_type: str | None) -> str | None:
    """person, org, committee or self as the filing itself says, or None when it does not (then the name decides)."""
    t = (source_type or "").strip().lower()
    if not t or t == "other":
        return None
    if t == "self":
        return "self"
    if "individual" in t or t == "lobbyist" or "spouse" in t:
        return "person"
    if "corporation" in t or "business" in t or "organization" in t:      # "... / Unregistered Committee" is the business row
        return "org"
    if "committee" in t or "party" in t or "pac" in t.split():
        return "committee"
    return None


def own_campaign(donor: str, committee: str) -> bool:
    """Is `committee` a campaign named for the donor's surname ("Jackson for Governor, Inc." and "JACKSON, RICHARD L.")?
    Only the surname and the campaign form of the name ("<Surname> for <office>") are compared: used with SELF_OVER,
    since Georgia caps what anyone but the candidate may give a campaign (O.C.G.A. 21-5-41), a person of the
    candidate's surname who gave far more than the cap is the candidate."""
    d = (donor or "").strip()
    if not d or not committee:
        return False
    last = d.split(",")[0] if "," in d else d.split()[-1]
    last = _norm(last).split()
    cw = _norm(committee).split()
    return bool(last) and len(last[-1]) > 2 and "for" in cw[1:] and last[-1] in cw[:cw.index("for")]
# a political committee's name: on Georgia's shared "Corporation / Business / Unregistered Committee" row it tells a committee
# (a PAC, a party, a governors' association, a campaign) from a business
POLITICAL = re.compile(r"\b(pac|political|committee|party|republicans?|democrats?|democratic|caucus|leadership|campaign|victory "
                       r"(fund|committee|pac)|friends of|re-?elect|to elect|for (governor|lieutenant governor|lt\.? governor|senate|congress|"
                       r"house|state (senate|house)|georgia|attorney general|secretary of state|commissioner|judge|sheriff|mayor|council))\b", re.I)
CAMPAIGN_FORM = re.compile(r"^([a-z][a-z ]*?) for (governor|lieutenant governor|lt governor|georgia)\b")    # on _norm'd names
COMMITTEE_WORDS = re.compile(r"\b(friends|committee|elect|campaign|for|pac|fund)\b", re.I)    # a campaign committee's name
ORGISH = re.compile(r"\b(llc|inc|corp|co|company|group|strategies|partners|associates|pac|committee|fund|party|union|association|foundation)\b\.?$|\b(llc|inc|strategies)\b", re.I)
SPEND_CAP = 4000             # what Electo keeps (donors_from)
DONOR_CAP = 6000
SHARDS = 128
PROFILE_RECIPS = 60          # recipients listed in one profile (the rest summed as "others")
# donors with a profile (the largest by total): Georgia's full money histories name hundreds of thousands of donors,
# and every profile is published with the site; the rest are counted everywhere else (donors.json, candidates' money)
# and the page shows their gifts without a profile. DONOR_PROFILE_CAP overrides it.
PROFILE_CAP = 60000


def shard_of(did: str) -> str:
    """FNV-1a (32-bit) of the donor id, mod SHARDS, as two hex digits; the page computes the same (dnShard)."""
    h = 0x811C9DC5
    for b in str(did).encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return f"{h % SHARDS:02x}"


def index_letter(name: str) -> str:
    n = _norm(name)
    return n[0] if n and "a" <= n[0] <= "z" else "_"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def cycle_of(d: str) -> str:
    """The two-year election cycle a date falls in, as the FEC counts them: 2025 and 2026 are the 2026 cycle."""
    try:
        y = int((d or "")[:4])
    except ValueError:
        return ""
    return str(y if y % 2 == 0 else y + 1) if 1980 <= y <= 2100 else ""


def load_links(root: Path) -> dict:
    """config/committee_links.yaml: committees linked to their candidate by hand, each with its source, by name."""
    import yaml

    p = root / "config" / "committee_links.yaml"
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, yaml.YAMLError):
        return {}
    out = {}
    for L in (doc or {}).get("links") or []:
        if L.get("committee") and L.get("person") and L.get("relation") in ("campaign", "leadership"):
            out[_norm(L["committee"])] = {"person": L["person"], "name": L.get("name") or "", "party": L.get("party") or "",
                                          "relation": L["relation"], "source": L.get("source") or "", "committee": L["committee"]}
    return out


_FIRSTS: dict = {}


def _first_forms(first: str) -> set:
    if first not in _FIRSTS:
        from .ethics import _firsts

        _FIRSTS[first] = set(_firsts(first))
    return _FIRSTS[first]


def is_self(donor: str, cand: str) -> bool:
    """Is a gift's donor the candidate (their own money)? The filed name ("Last, First Middle" or "First Last") against
    the candidate's: the same last name AND a first or middle name that is theirs (Rick / Richard, Burt / Burton). A
    relative or a stranger with the same last name is not the candidate."""
    cw = [w for w in _norm(cand).split() if w not in SUFFIX]
    if len(cw) < 2 or not donor:
        return False
    cf, cl = cw[0], cw[-1]
    if "," in donor:
        last, given = donor.split(",", 1)
        lw = [w for w in _norm(last).split() if w not in SUFFIX]
        dl, gw = (lw[-1] if lw else ""), [w for w in _norm(given).split() if w not in SUFFIX]
    else:
        w = [x for x in _norm(donor).split() if x not in SUFFIX]
        if len(w) < 2:
            return False
        dl, gw = w[-1], w[:-1]
    if dl != cl or not gw:
        return False
    forms = _first_forms(cf)
    return any(g in forms or cf in _first_forms(g) or (len(cf) >= 4 and g.startswith(cf)) for g in gw if len(g) > 1)


def _level(c: dict) -> str:
    lv = c.get("levels") or []
    for k in ("federal", "statewide", "legislative", "judicial", "local", "school"):
        if k in lv:
            return k
    o = (c.get("office") or "").lower()
    if "u.s." in o or "congress" in o or "senate" in o and "state" not in o:
        return "federal"
    return "local" if c.get("jurisdiction") else ""


def capped(items, cap: int, candidates: set | None = None) -> list[dict]:
    """The `cap` items (gifts or spending, each with its recipient "r") the page lists, largest first. Every candidate's
    own largest come first (at least GIFT_FLOOR each, more when the cap allows, up to GIFT_TOP), then the largest of the
    rest: ranked only by size, one Senate campaign's thousands of large gifts left every other campaign's donors out of
    the list, and a candidate's page showed none."""
    items = list(items)
    if len(items) <= cap:
        return sorted(items, key=lambda g: -g["total"])
    by_r: dict = defaultdict(list)
    for g in items:
        by_r[g["r"]].append(g)
    own = [r for r in by_r if candidates is None or r in candidates]
    per = max(GIFT_FLOOR, min(GIFT_TOP, int(cap * 0.75) // max(1, len(own))))
    first = []
    for r in own:
        first += sorted(by_r[r], key=lambda g: -g["total"])[:per]
    first = sorted(first, key=lambda g: -g["total"])[:cap]
    taken = {id(g) for g in first}
    rest = sorted((g for g in items if id(g) not in taken), key=lambda g: -g["total"])[:cap - len(first)]
    return sorted(first + rest, key=lambda g: -g["total"])


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
    links = load_links(root)
    link_party = {L["person"]: L["party"] for L in links.values() if L.get("party")}
    from .ethics import candidate_index
    from .ethics import match as filer_match

    cidx = candidate_index(candidates)
    rematched: dict = {}

    def linked(m: dict):
        """(candidate, leadership link) for a line no candidate was set on when it was read: the hand link table first
        (config/committee_links.yaml), then the filer's name matched again against today's candidates, by the same
        rule as when Georgia's lines are read (ethics.match: the candidate's own name, or a full name inside the
        committee's), since the candidate list grows after a filing is read."""
        for n in (m.get("filer_name") or "", m.get("recipient") or ""):
            L = links.get(_norm(n)) if n else None
            if L:
                if L["relation"] == "leadership":
                    return None, L
                c = by_id.get(L["person"]) or {"id": L["person"], "name": L["name"], "electo_key": "", "office": "",
                                                "levels": [], "counties": [], "jurisdiction": ""}
                return {**c, "party": c.get("party") or L["party"], "_link": L}, None
        if m.get("ga_system") and m.get("filer_name"):
            k = (m["filer_name"], m.get("filer_type") or "")
            if k not in rematched:
                ft = m.get("filer_type") or ""
                rematched[k] = filer_match({"filerName": m["filer_name"], "filerTypeDesc": ft,
                                            "filerTypeCode": ft if len(ft) <= 3 else ""}, cidx)
            return rematched[k], None
        return None, None

    # whose committee each filer on record is (one grouped query over the filers, not a pass over every line), by the same
    # rules as the recipients below: money one of a candidate's committees moved to another of theirs (a leadership
    # committee paying into the campaign, an old account into the new one) was counted where it was first given, so it is
    # not counted again where it arrived. For All Georgians' $5M to Jackson for Governor was counted twice on his bar.
    owner_of: dict[str, str] = {}
    fields = ("filer_name", "recipient", "candidate_id", "leadership_pac_of", "ga_system", "filer_type")
    try:
        filers = store.distinct(", ".join(f"json_extract(meta, '$.{f}')" for f in fields), "kind='contribution'")
    except Exception:  # noqa: BLE001  (a store without the grouped query: no transfers told apart, as before)
        filers = []
    for row in filers:
        m0 = dict(zip(fields, tuple(row)))
        who = m0["candidate_id"] or m0["leadership_pac_of"] or ""
        if not who:
            c0, l0 = linked(m0)
            who = (c0 or {}).get("id") or (l0 or {}).get("person") or ""
        if not who:
            continue
        for n in (m0["filer_name"], m0["recipient"]):
            k = _norm(n or "")
            if k:
                owner_of[k] = who if owner_of.get(k, who) == who else ""      # "": two owners, so no one's

    def own_transfer(m: dict, R: dict) -> bool:
        """A Georgia line from another committee of the recipient's own candidate (FEC transfers are told by committee id,
        gacrawl/money.py). The candidate's own money (a person, or filed as Self) is not a transfer, nor a committee
        naming itself as its own donor."""
        mine = R.get("cid") or R.get("pac_of") or ""
        nm = (m.get("name") or "").strip()
        if not mine or not nm or m.get("filing_source") == "FEC":
            return False
        kn = _norm(nm)
        if kn in (_norm(m.get("filer_name") or ""), _norm(m.get("recipient") or "")):
            return False
        sk = source_kind(m.get("source_type"))
        if sk in ("person", "self") or (sk is None and ("," in nm and not ORGISH.search(nm) or is_self(nm, R["name"]))):
            return False
        if kn not in owner_of:
            # a committee that never filed here under this spelling ("Friends of Blake Tillery-Senate"): the hand link table,
            # else the rule that links filers (a candidate's full name inside a campaign committee's name)
            L0 = links.get(kn)
            c0 = None if L0 else filer_match({"filerName": nm, "filerTypeDesc": "", "filerTypeCode": ""}, cidx) if (sk == "committee" or COMMITTEE_WORDS.search(nm)) else None
            owner_of[kn] = (L0 or {}).get("person") or (c0 or {}).get("id") or ""
        if owner_of[kn]:
            return owner_of[kn] == mine
        # a leadership committee's own candidate's campaign by its campaign name ("Abrams for Governor", $5M to One Georgia
        # in 2022): the surname of the candidate who chairs it, and the statewide office it exists for (O.C.G.A. 21-5-34.2)
        cf = CAMPAIGN_FORM.match(kn) if R.get("pac_of") and R.get("pac_of_name") else None
        return bool(cf) and cf.group(1).split()[-1] == (_norm(R["pac_of_name"]).split() or [""])[-1]

    def recip_of(m: dict, r: dict) -> dict:
        cid = m.get("candidate_id")
        c = by_id.get(cid) if cid else None
        lead = None
        if not cid:
            c, lead = linked(m)
            cid = c["id"] if c else None
        # a linked leadership committee is one recipient however many filer records the state keeps for it (Georgians First
        # files under two); anything else by its entity, as before
        key = (f"cand:{cid}" if c else f"lead:{lead['person']}:{_norm(m.get('recipient') or '')}" if lead
               else "ent:" + str(m.get("recipient_entity") or _norm(m.get("recipient") or "")))
        R = recips.get(key)
        if not R:
            if m.get("leadership_pac_of"):
                pac = {"pac_of": m["leadership_pac_of"], "pac_of_name": (by_id.get(m["leadership_pac_of"]) or {}).get("name", ""),
                       "pac_of_key": m.get("leadership_pac_of_key") or ""}
            elif lead:
                pc = by_id.get(lead["person"]) or {}
                pac = {"pac_of": lead["person"], "pac_of_name": pc.get("name") or lead["name"], "pac_of_key": pc.get("electo_key") or "",
                       "pac_of_party": pc.get("party") or lead["party"], "link_src": lead["source"]}
            else:
                pac = {}
            R = recips[key] = {"i": len(recips), "name": c["name"] if c else (m.get("recipient") or ""), "key": (c or {}).get("electo_key") or "",
                               "cid": cid or "", "party": (c or {}).get("party") or "", "office": (c or {}).get("office") or "",
                               "level": _level(c) if c else ("committee" if m.get("recipient_kind") == "committee" or lead else ""),
                               "county": ((c or {}).get("counties") or [""])[0], "jur": (c or {}).get("jurisdiction") or "",
                               "raised": 0.0, "self": 0.0, "n": 0, "spent": 0.0, "nx": 0, "loans": 0.0, "in_kind": 0.0, "in_ga": 0.0,
                               "small": 0.0, "orgs": 0.0, "employers": defaultdict(float), "sources": set(), "first": "", "last": "",
                               "_fec": 0.0, "cy": {}, "yr": {}, **pac}
        if c and c.get("_link"):
            # each committee linked by hand, with its own source (a candidate can have several: Tillery's Senate account is
            # matched by his name, his lieutenant governor campaign only by the link)
            lk = R.setdefault("_links", {})
            if c["_link"]["committee"] not in lk and len(lk) < 6:
                lk[c["_link"]["committee"]] = c["_link"]["source"]
            R.setdefault("link_src", c["_link"]["source"])
        fn = (m.get("filer_name") or "").strip()
        if c and fn and _norm(fn) != _norm(R["name"]):
            cm = R.setdefault("_comm", set())      # the committees that filed as this candidate's (shown on the page)
            if len(cm) < 6:
                cm.add(fn)
        return R

    # every line once, by the shared rules (gacrawl/money.py): no memo lines, no line twice, no FEC line read before
    # the FEC's own fields were kept, no transfer between a candidate's own committees
    from .money import lines

    mstats: dict = {}
    for L in lines(store, mstats):
        m, amt = L["meta"], L["amount"]
        r = {"url": L["url"], "published": L["published"]}
        n_rows += 1
        R = recip_of(m, r)
        t = m.get("type") or "contribution"
        d = (m.get("date") or r.get("published") or "")[:10]
        if t in ("contribution", "in_kind") and own_transfer(m, R):
            # counted where it was first given; kept here only as a note, by committee and by cycle and year
            Mv = R.setdefault("_moved", {"total": 0.0, "n": 0, "from": defaultdict(float), "cy": defaultdict(float), "yr": defaultdict(float)})
            Mv["total"] += amt
            Mv["n"] += 1
            Mv["from"][(m.get("name") or "").strip()] += amt
            if cycle_of(d):
                Mv["cy"][cycle_of(d)] += amt
                Mv["yr"][d[:4]] += amt
            mstats["own_transfer"] = mstats.get("own_transfer", 0) + 1
            continue
        if len(R["sources"]) < 6:
            R["sources"].add(r["url"])
        if d:
            R["first"] = min(R["first"] or d, d)
            R["last"] = max(R["last"], d)
        cy = cycle_of(d)
        yr = d[:4] if cy else ""
        CS = []     # the same totals for each two-year election cycle (cy) and each calendar year (yr), with their dates
        for part, k in (("cy", cy), ("yr", yr)):
            if not k:
                continue
            C = R[part].get(k)
            if C is None:
                C = R[part][k] = {"raised": 0.0, "self": 0.0, "n": 0, "spent": 0.0, "loans": 0.0, "first": d, "last": d}
            C["first"], C["last"] = min(C["first"], d), max(C["last"], d)
            CS.append(C)
        if t == "expenditure":
            R["spent"] += amt
            R["nx"] += 1
            for C in CS:
                C["spent"] += amt
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
            for C in CS:
                C["loans"] += amt
            continue
        if t == "in_kind":
            R["in_kind"] += amt
        R["raised"] += amt
        for C in CS:
            C["raised"] += amt
        if m.get("filing_source") == "FEC":
            R["_fec"] += amt
        if amt <= 0:
            continue      # a refund or a correction: nets out of the total, counts as nothing else
        R["n"] += 1
        for C in CS:
            C["n"] += 1
        st = (m.get("state") or "").upper()[:2]
        if st == "GA" or (not st and m.get("city") and _norm(m["city"]) in city_county):
            R["in_ga"] += amt
            for C in CS:      # the shares per cycle and year too, so the page never has to show every year's under one
                C["in_ga"] = C.get("in_ga", 0.0) + amt
        if amt < 200:
            R["small"] += amt
            for C in CS:
                C["small"] = C.get("small", 0.0) + amt
        nm = (m.get("name") or "").strip()
        last = (R["name"].split()[-1] if R["name"] else "").lower()
        if L["via"]:      # one person's gift that passed through a conduit (ActBlue, WinRed, AIPAC PAC ...): theirs, via it
            R.setdefault("via", defaultdict(float))[L["via"]] += amt
        conduit = L["platform"]
        if not conduit and (m.get("contributor_committee_id") or m.get("donor_is_org")) and (JFC.search(nm) or (len(last) > 2 and re.search(r"\b" + re.escape(last) + r"\b", nm, re.I))):
            conduit = "Joint fundraising / candidate committees"
        if conduit:
            R["conduit"] = R.get("conduit", 0.0) + amt
            R.setdefault("conduits", defaultdict(float))[conduit] += amt
            conduits_total[conduit] += amt
            continue          # many people's gifts in one line: not a donor, no donor geography
        et = (m.get("entity_type") or "").upper()
        sk = source_kind(m.get("source_type"))       # Georgia: what the filing itself says the donor is
        orgish = "," not in nm and bool(ORGISH.search(nm))
        st_low = (m.get("source_type") or "").strip().lower()
        # the candidate's own name, filed as a committee or a business ("Greene, Karen Scott" giving to her own campaign as
        # "Candidate/Campaign Committee"): their own money (an account of theirs that filed is a transfer, left out above)
        named_self = (bool(R["cid"]) and sk in ("committee", "org") and not orgish and not COMMITTEE_WORDS.search(nm)
                      and is_self(nm, R["name"]))
        if sk == "self" or named_self or (R["cid"] and (et in ("CAN",) or (sk in (None, "person") and not m.get("donor_is_org") and is_self(nm, R["name"])))):
            kind = "self"     # the candidate's own money
            R["self"] += amt
            for C in CS:
                C["self"] += amt
        elif sk == "org" and "unregistered committee" in st_low and POLITICAL.search(nm):
            # Georgia's one row for "Corporation / Business / Unregistered Committee": a political committee by its name (the
            # Republican and Democratic Governors Associations, the RNC, a PAC) is a committee, not a business
            kind = "committee"
        elif sk in ("org", "committee"):
            kind = sk
        elif sk == "person":
            kind = "org" if orgish else "person"      # filed as an individual, but the name is a company's ("... LLC")
        elif (et in ("ORG", "CCM", "PAC", "COM", "PTY") or m.get("contributor_committee_id") or m.get("donor_is_org") or orgish
              # filed as "Other": the filer passed over "Individual", so not a person unless the name is one ("Last, First")
              or ((m.get("source_type") or "").strip().lower() == "other" and "," not in nm and nm)):
            kind = "committee" if (et in ("PAC", "COM", "PTY", "CCM") or m.get("contributor_committee_id") or re.search(r"\b(pac|committee|party|fund)\b", nm, re.I)) else "org"
        else:
            kind = "person"
        if kind in ("committee", "org"):
            R["orgs"] += amt
            for C in CS:
                C["orgs"] = C.get("orgs", 0.0) + amt
        elif m.get("employer"):
            R["employers"][m["employer"].strip()[:60]] += amt
        R.setdefault("_kinds", defaultdict(float))[kind] += amt
        for C in CS:
            CK = C.setdefault("kinds", {})
            CK[kind] = CK.get(kind, 0.0) + amt
        ft = (m.get("filer_type") or "").lower()
        if ft and "_ft" not in R:
            R["_ft"] = ft
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
        if cy:      # [total, gifts] in each election cycle
            gc = G.setdefault("cy", {}).setdefault(cy, [0.0, 0])
            gc[0] += amt
            gc[1] += 1
            gy = G.setdefault("yr", {}).setdefault(yr, [0.0, 0])     # and in each calendar year
            gy[0] += amt
            gy[1] += 1
        if d:
            G["first"] = min(G["first"] or d, d)
            G["last"] = max(G["last"], d)
        D = donors.get(did)
        if not D:
            D = donors[did] = {"id": did, "name": G["name"], "kind": kind, "city": G["city"], "state": G["state"], "county": G["county"],
                               "employer": G["employer"], "total": 0.0, "n": 0, "r": set(), "_aka": set(), "_years": defaultdict(float),
                               "_via": defaultdict(float), "_occ": "", "_last": "", "_first": ""}
        D["total"] += amt
        D["n"] += 1
        D["r"].add(R["i"])
        D["_aka"].add((m.get("name") or "").strip())
        if d:
            D["_years"][d[:4]] += amt
            D["_first"] = min(D["_first"] or d, d)
            if d >= D["_last"]:           # employer and occupation as last filed
                D["_last"] = d
                D["employer"] = (m.get("employer") or D["employer"] or "")[:60]
                D["_occ"] = (m.get("occupation") or D["_occ"] or "")[:50]
        if L["via"]:
            D["_via"][L["via"]] += amt
            G.setdefault("via", {})
            G["via"][L["via"]] = G["via"].get(L["via"], 0.0) + amt

    rec_list = sorted(recips.values(), key=lambda x: x["i"])
    # a candidate's own money filed under a campaign this crawl has not linked to a candidate: a person of the campaign's
    # surname ("Jackson, Richard L." to "Jackson for Governor, Inc.") whose gifts to it pass SELF_OVER. No one else may
    # give a campaign that much, so it is the candidate's own (marked self_inferred, and the page says how it was told)
    for G in gifts.values():
        R = rec_list[G["r"]]
        ft = R.get("_ft") or ""
        if (G["kind"] == "person" and not R.get("cid") and G["total"] >= SELF_OVER and own_campaign(G["name"], R["name"])
                and not any(w in ft for w in ("leadership", "independent", "party", "non-candidate"))):
            G["kind"], G["self_inferred"] = "self", True
            R["self"] += G["total"]
            for part in ("cy", "yr"):       # and in each cycle and year it was given
                for k, v in (G.get(part) or {}).items():
                    if k in R[part]:
                        R[part][k]["self"] += v[0]
                        CK = R[part][k].setdefault("kinds", {})
                        CK["person"] = CK.get("person", 0.0) - v[0]
                        CK["self"] = CK.get("self", 0.0) + v[0]
            K = R.setdefault("_kinds", defaultdict(float))
            K["person"] -= G["total"]
            K["self"] += G["total"]
            D = donors.get(G["donor"])
            if D and D["kind"] == "person" and G["total"] >= D["total"] / 2:
                D["kind"] = "self"
    # each federal campaign checked against what the FEC itself reports its committees raised: itemized lines can never
    # add up to more than the committee's receipts, so more than 5% over means a line is counted twice or belongs to
    # someone else (the page then says so beside the total)
    from .connections import fec_check, fec_cycle_totals

    for R in rec_list:
        if R.get("cid") and R["level"] == "federal" or R.get("_fec"):
            try:
                off = fec_cycle_totals(store, R["cid"]) if R.get("cid") else {}
            except Exception:  # noqa: BLE001
                off = {}
            if off:
                R["fec_cy"] = off      # the FEC's own totals per cycle: what the page shows as the campaign's money
        fx = R.pop("_fec", 0.0)
        if R.get("cid") and fx:
            try:
                chk = fec_check(store, R["cid"], fx)
            except Exception:  # noqa: BLE001
                chk = None
            if chk and chk.get("check") != "unchecked":
                R["fec"] = {"receipts": chk.get("receipts"), "itemized": chk.get("itemized"), "check": chk["check"],
                            "cycles": chk.get("cycles") or []}
    legacy_by = mstats.get("legacy_by") or {}
    # committees whose FEC read is part-way (finance.part_read): the itemized lines listed so far are the newest ones;
    # the page says how far back they reach (the FEC's own totals stand for the whole campaign meanwhile)
    from .finance import part_read

    reading: dict[str, dict] = {}
    for com, cid in part_read(store).items():
        try:
            rs = json.loads(store.kv_get(f"fec:a2r:{com}") or "{}")
        except ValueError:
            rs = {}
        back = ((rs.get("last") or {}).get("last_contribution_receipt_date") or "")[:10]
        if cid:
            x = reading.setdefault(cid, {"committees": 0, "back_to": back, "newest": (rs.get("newest") or "")[:10]})
            x["committees"] += 1
            x["back_to"] = min(x["back_to"] or back, back or x["back_to"])
    for R in rec_list:
        if R.get("cid") and legacy_by.get(R["cid"]):
            R["held"] = legacy_by[R["cid"]]     # FEC lines waiting for the re-read: the page says this total is incomplete
        if R.get("cid") in reading:
            R["reading"] = reading[R["cid"]]
        if not R["party"] and R.get("cid") in link_party:
            R["party"] = link_party[R["cid"]]
        if R.get("_comm"):
            R["committees"] = sorted(R.pop("_comm"))
        if R.get("_links"):
            R["links"] = [{"committee": k, "source": v} for k, v in sorted(R.pop("_links").items())]
        R["self"] = round(R["self"], 2)
        for part in ("cy", "yr"):
            R[part] = {k: {kk: (round(vv, 2) if isinstance(vv, float) else
                                {a: round(b, 2) for a, b in sorted(vv.items()) if round(b, 2) > 0} if isinstance(vv, dict) else vv)
                           for kk, vv in v.items()} for k, v in sorted(R[part].items())}
        emp = sorted(R["employers"].items(), key=lambda kv: -kv[1])[:8]
        R["employers"] = [{"name": k, "total": round(v, 2)} for k, v in emp]
        R["sources"] = sorted(R["sources"])
        if R.get("via"):
            R["via"] = [{"name": k, "total": round(v, 2)} for k, v in sorted(R["via"].items(), key=lambda kv: -kv[1])][:12]
        if R.get("conduits"):
            R["conduits"] = [{"name": k, "total": round(v, 2)} for k, v in sorted(R["conduits"].items(), key=lambda kv: -kv[1])]
            R["conduit"] = round(R["conduit"], 2)
        R["office"] = (R["office"] or "").split(";")[0].strip()
        for k in ("raised", "spent", "loans", "in_kind", "in_ga", "small", "orgs"):
            R[k] = round(R[k], 2)
        # what named donors of each kind gave (every line): the page's "by donor type" under a recipient filter
        R["kinds"] = {k: round(v, 2) for k, v in sorted((R.pop("_kinds", None) or {}).items()) if round(v, 2) > 0}
        R.pop("_ft", None)
        Mv = R.pop("_moved", None)
        if Mv and round(Mv["total"], 2):
            R["moved"] = {"total": round(Mv["total"], 2), "n": Mv["n"],
                          "from": [{"name": k, "total": round(v, 2)} for k, v in sorted(Mv["from"].items(), key=lambda kv: -kv[1])][:6],
                          "cy": {k: round(v, 2) for k, v in sorted(Mv["cy"].items())}, "yr": {k: round(v, 2) for k, v in sorted(Mv["yr"].items())}}
    cand_r = {R["i"] for R in recips.values() if R.get("cid")}
    gift_list = capped(gifts.values(), GIFT_CAP, cand_r)
    for g in gift_list:
        g["total"] = round(g["total"], 2)
        for part in ("cy", "yr"):
            if g.get(part):
                g[part] = {k: [round(v[0], 2), v[1]] for k, v in sorted(g[part].items())}
    spend_list = capped(spend.values(), SPEND_CAP, cand_r)
    for s in spend_list:
        s["total"] = round(s["total"], 2)
    multi = [d for d in donors.values() if len(d["r"]) > 1]
    multi.sort(key=lambda d: -d["total"])
    donor_list = [{**{k: v for k, v in d.items() if not k.startswith("_")}, "total": round(d["total"], 2), "r": sorted(d["r"])}
                  for d in multi[:DONOR_CAP]]
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
                   "which can split one person or merge two with the same name. Only a candidate's own campaign committees count as what the candidate raised; a leadership PAC the candidate runs is listed as its own committee (pac_of), and a joint fundraising committee's money is counted where it was transferred. Each federal campaign carries fec: the FEC's own receipts total for its committees, against which the itemized lines are checked. A platform's lump line (WinRed, ActBlue) is counted in what the recipient raised and listed under conduits, but the platform is not ranked as a donor; a gift earmarked through a conduit (a platform or a PAC such as AIPAC PAC) is the person's own, listed under via with the conduit named. Memo lines, the same line read twice, and transfers between a candidate's own committees are left out. Filings carry no demographic data beyond "
                   "employer, occupation and address. review marks rows parsed from a scanned PDF. Each recipient's and gift's money is also "
                   "split by two-year election cycle (cy; 2025 and 2026 are the 2026 cycle, as the FEC counts them), with the first and last "
                   "date of the lines in it, and by calendar year (yr, the same shape). self is the candidate's own money (gifts filed under the candidate's own name, or filed as Self). "
                   "A donor's kind (person, org, committee) is the source type the Georgia filing itself gives, and the name only "
                   "where the filing gives none. A gift marked self_inferred is counted as the candidate's own money because a person "
                   "of the campaign's surname gave it more than $100,000, which Georgia law (O.C.G.A. 21-5-41) lets no one but the "
                   "candidate give. kinds is each recipient's money from named donors by kind; totals covers every line. A committee "
                   "whose name does not carry its candidate's full name is linked to the candidate only by config/committee_links.yaml, "
                   "each link with its source (links: committee and source; link_src: the first); committees lists the committees counted under a candidate. held is the number "
                   "of the campaign's FEC lines waiting to be read again: its federal total is incomplete until then. fec_cy is the FEC's "
                   "own totals for a federal campaign's committees per cycle (receipts, gifts under $200 included; through: the "
                   "newest report's end date): the page shows these as the campaign's money, the itemized lines as the detail. "
                   "moved is money another committee of the same candidate moved to this one (a leadership committee paying "
                   "into the campaign, an old campaign account into the new one: total, from whom, by cycle and year): it was "
                   "counted where it was first given, so it is not in raised here.",
           "cycles": sorted({k for R in rec_list for k in R["cy"]}),
           "years": sorted({k for R in rec_list for k in R["yr"]}),
           "generated": _now(), "rows": n_rows, "recipients": rec_list, "gifts": gift_list, "spend": spend_list, "donors": donor_list,
           "by": by_out, "conduits": [{"name": k, "total": round(v, 2)} for k, v in sorted(conduits_total.items(), key=lambda kv: -kv[1])],
           # over every line, not only the listed gifts: what the page's headline figures are (the gift list is the
           # largest donor-to-recipient totals; a page that added those up alone showed $191M of $305M raised)
           "totals": {"raised": round(sum(R["raised"] for R in rec_list), 2), "gifts": sum(R["n"] for R in rec_list),
                      "named": round(sum(g["total"] for g in gifts.values()), 2), "pairs": len(gifts), "donors": len(donors),
                      "recipients": len(rec_list), "listed": len(gift_list), "self_inferred": sum(1 for g in gifts.values() if g.get("self_inferred"))},
           "hold": _hold_note(mstats, reading, rec_list),
           "skipped": {"memo_lines": mstats.get("memo", 0), "duplicate_lines": mstats.get("repeat", 0),
                       "transfers_between_own_committees": mstats.get("transfer", 0) + mstats.get("own_transfer", 0),
                       "fec_lines_awaiting_reread": mstats.get("legacy", 0),
                       "fec_lines_filed_twice": mstats.get("refiled", 0),
                       "ga_late_lines_listed_again": mstats.get("late_listed_again", 0),
                       "fec_campaigns_part_read": len(reading)}, "caps": {"gifts": GIFT_CAP, "spend": SPEND_CAP, "donors": DONOR_CAP}}
    for g in gift_list:
        if g.get("via"):
            g["via"] = sorted(g["via"])[:3]
    out_path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    np = export_profiles(store, donors, gifts, rec_list, out_path.parent / "donor_profiles", log=log, candidates=candidates)
    return {"profiles": np, "rows": n_rows, "recipients": len(rec_list), "gifts": len(gift_list), "spend": len(spend_list), "donors": len(donor_list)}


def _hold_note(mstats: dict, reading: dict, rec_list: list) -> str:
    """What the Donors tab says while federal money is incomplete: FEC lines waiting for the re-read (not counted), and
    campaigns whose itemized lines are part-way (counted as far as read; their totals are the FEC's own)."""
    parts = []
    if mstats.get("legacy"):
        parts.append("Federal campaign money (FEC) is being read again with the FEC's own line codes: a check found FEC memo "
                     "lines counted as gifts (one AIPAC PAC figure was counted 20 times). Federal candidates come back as each "
                     "campaign is read again; until then their money is left out rather than shown wrong.")
    if reading:
        names = sorted(R["name"] for R in rec_list if R.get("cid") in reading)
        parts.append(f"The itemized gifts of {len(reading)} federal campaign{'' if len(reading) == 1 else 's'} "
                     f"({', '.join(names[:6])}{'…' if len(names) > 6 else ''}) are still being read from the FEC, newest first: "
                     "their listed donors cover only what has been read so far, while their totals are the FEC's own.")
    return " ".join(parts)


def _orgs(store) -> dict:
    """entity id -> what the organization registries say (finance.py enrich: FEC registration, IRS data, hand notes),
    each line with its source; the self-description is left for the model (about), not shown raw."""
    out = {}
    for r in store.iter_query("kind='organization'"):
        m = r.get("meta") or {}
        eid = m.get("entity_id")
        if not eid:
            continue
        out[eid] = {"org_kind": m.get("org_kind") or "", "fec_id": m.get("fec_id") or "", "ein": m.get("ein") or "",
                    "connected_org": m.get("connected_org") or "", "website": m.get("website") or "",
                    "what": [{"t": w["text"], "u": w.get("src") or ""} for w in (m.get("what") or [])
                             if w.get("basis") != "self-description" and w.get("text")][:5],
                    # the people behind it, as its own filings and pages list them (treasurer and custodian of records on
                    # its FEC registration, officers its own site names), each with the page it came from
                    "people": [{"name": o["name"], "role": o.get("role") or "", "u": o.get("src") or ""}
                               for o in (m.get("officers") or []) if o.get("name") and str(o.get("src", "")).startswith("http")][:8],
                    "src": [{"t": x.get("t") or "", "u": x.get("u") or ""} for x in (m.get("sources") or []) if x.get("u")][:5]}
    return out


def _money(v: float) -> str:
    return f"${v:,.0f}"


def summary_of(P: dict) -> str:
    """One plain sentence from the profile's own figures (no model): who, how much, to whom, when, by party."""
    to = P.get("to") or []
    nrec = len(to) + (P.get("others") or {}).get("recipients", 0)
    by_party: dict[str, float] = defaultdict(float)
    for t in to:
        p = (t.get("party") or "").upper()[:1]
        by_party["Democrats" if p == "D" else "Republicans" if p == "R" else "others"] += t["t"]
    who = {"person": "This donor", "self": "This candidate", "org": "This organization", "committee": "This committee"}.get(P["kind"], "This donor")
    yrs = (P.get("first") or "")[:4], (P.get("last") or "")[:4]
    when = f" from {yrs[0]} to {yrs[1]}" if yrs[0] and yrs[1] and yrs[0] != yrs[1] else (f" in {yrs[0]}" if yrs[0] else "")
    s = f"{who} gave {_money(P['total'])} in {P['n']} gift{'s' if P['n'] != 1 else ''} to {nrec} recipient{'s' if nrec != 1 else ''}{when}"
    if to:
        s += f"; the most, {_money(to[0]['t'])}, to {to[0]['name']}"
    parts = [f"{_money(v)} to {k}" for k, v in sorted(by_party.items(), key=lambda kv: -kv[1]) if v > 0]
    if len(parts) > 1:
        s += " (" + ", ".join(parts) + ")"
    if P.get("via"):
        s += f". {_money(sum(v['t'] for v in P['via']))} of it passed through " + ", ".join(v["name"] for v in P["via"][:3])
    return s + "."


# a filed occupation or employer that names an elected office: the donor is a public official, not a private person
OFFICE_WORDS = re.compile(r"(?i)\b(state (representative|rep|senator)|u\.?s\.? (representative|senator)|congress(man|woman|member)?|"
                          r"senator|legislator|general assembly|county commission\w*|commissioner|mayor|city council\w*|"
                          r"council ?member|sheriff|district attorney|solicitor general|school board|board of education|"
                          r"elected official|public servant|governor|secretary of state|attorney general|clerk of (superior )?court|"
                          r"tax commissioner|probate judge|judge|coroner|alderm[ae]n)\b")


def _public_figure_index(candidates: list[dict]) -> dict:
    """'first last' -> the Electo person, for names exactly one Electo person carries (nicknames both ways)."""
    from .ethics import candidate_index

    return candidate_index(candidates or [])


def public_figure(D: dict, idx: dict) -> dict | None:
    """The Electo person a donor is, when it is safe to say: the filed name matches exactly one Electo person AND the
    filed occupation or employer names an elected office. Otherwise None (a private person with a public figure's
    name keeps a private profile)."""
    if D["kind"] not in ("person", "self") or not idx:
        return None
    if not OFFICE_WORDS.search(f"{D.get('_occ') or ''} {D.get('employer') or ''}") and D["kind"] != "self":
        return None
    n = D["name"]
    if "," in n:
        last, first = n.split(",", 1)
        first = (first.split() or [""])[0]
    else:
        w = n.split()
        first, last = (w[0] if w else ""), (w[-1] if w else "")
    last = re.sub(r"(?i)\b(jr|sr|ii|iii|iv)\b\.?", "", last).strip()
    c = idx.get(_norm(f"{first} {last}"))
    return {"name": c["name"], "key": c.get("electo_key") or "", "office": (c.get("office") or "").split(";")[0]} if c else None


def giving_record(P: dict) -> str:
    """How long and how steadily the donor has given, from the filings (no model): since when, in how many years,
    the busiest year, and how many of the recipients' parties."""
    yrs = sorted((y, v) for y, v in (P.get("years") or {}).items() if v > 0)
    if not yrs:
        return ""
    first, last = int(yrs[0][0]), int(yrs[-1][0])
    span = last - first + 1
    peak = max(yrs, key=lambda x: x[1])
    s = f"Giving since {first}" + (f", in {len(yrs)} of the {span} years through {last}" if span > 1 else "")
    if len(yrs) > 1:
        s += f"; the most in {peak[0]} ({_money(peak[1])})"
    s += "."
    to = P.get("to") or []
    parties = {t.get("party") for t in to if t.get("party") in ("D", "R")}
    if len(parties) == 2:
        s += " Has given to both Democrats and Republicans."
    levels = {"federal" if (t.get("office") or "").startswith(("U.S.", "US ")) else "state or local" for t in to}
    if len(levels) == 2:
        s += " Gives to federal and to state or local candidates."
    return s


def export_profiles(store, donors: dict, gifts: dict, rec_list: list, out_dir: Path, log=print, candidates: list | None = None) -> int:
    """donor_profiles/dp/XX.json and dpi/X.json (see the module docstring)."""
    import shutil

    orgs = _orgs(store)
    pidx = _public_figure_index(candidates or [])
    try:
        about = json.loads(store.kv_get("donor_about") or "{}")
    except ValueError:
        about = {}
    by_donor: dict[str, list] = defaultdict(list)
    for g in gifts.values():
        by_donor[g["donor"]].append(g)
    rname = {_norm(R["name"]): R["i"] for R in rec_list if R.get("name")}
    order = sorted(donors.values(), key=lambda d: -d["total"])
    import os

    cap = int(os.environ.get("DONOR_PROFILE_CAP") or PROFILE_CAP)
    shards: dict[str, dict] = defaultdict(dict)
    index: dict[str, list] = defaultdict(list)
    for rank, D in enumerate(order[:cap], 1):
        gs = sorted(by_donor.get(D["id"], []), key=lambda g: -g["total"])
        to = []
        for g in gs[:PROFILE_RECIPS]:
            R = rec_list[g["r"]]
            to.append({"r": g["r"], "name": R["name"], "party": R.get("party") or "", "office": R.get("office") or "",
                       "t": round(g["total"], 2), "n": g["n"], "first": g["first"], "last": g["last"], "fs": g["fs"], "u": g["u"],
                       **({"via": sorted(g["via"])[:3]} if g.get("via") else {}), **({"review": True} if g.get("review") else {})})
        rest = gs[PROFILE_RECIPS:]
        aka = sorted(x for x in D["_aka"] if x and x != D["name"])[:6]
        P = {"id": D["id"], "name": D["name"], "kind": D["kind"], "city": D["city"], "state": D["state"], "county": D["county"],
             "total": round(D["total"], 2), "n": D["n"], "first": D["_first"], "last": D["_last"], "rank": rank, "of": len(order),
             "years": {y: round(v, 2) for y, v in sorted(D["_years"].items())}, "to": to,
             **({"others": {"recipients": len(rest), "t": round(sum(g["total"] for g in rest), 2)}} if rest else {}),
             **({"aka": aka} if aka else {}),
             **({"via": [{"name": k, "t": round(v, 2)} for k, v in sorted(D["_via"].items(), key=lambda kv: -kv[1])]} if D["_via"] else {})}
        if D["kind"] in ("person", "self"):
            P.update({"employer": D["employer"], "occupation": D["_occ"]})
            pf = public_figure(D, pidx)
            if pf:
                P["electo"] = pf          # a public official or candidate: their political profile on the page
        else:
            o = orgs.get(D["id"])
            if o:
                P["org"] = o
            a = about.get(D["id"])
            if a and a.get("t"):
                P["about"] = a
            ri = rname.get(_norm(D["name"]))
            if ri is not None:
                P["recv"] = ri          # the committee files too: its own receipts are a recipient on the tab
        P["summary"] = summary_of(P)
        P["record"] = giving_record(P)
        shards[shard_of(D["id"])][D["id"]] = P
        place = ", ".join(x for x in (D["city"], D["state"]) if x)
        index[index_letter(D["name"])].append([D["id"], D["name"], D["kind"], round(D["total"], 2), place, len(D["r"])])
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "dp").mkdir(parents=True)
    (out_dir / "dpi").mkdir()
    for k in (f"{i:02x}" for i in range(SHARDS)):
        (out_dir / "dp" / f"{k}.json").write_text(json.dumps(shards.get(k, {}), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for k, rows in index.items():
        (out_dir / "dpi" / f"{k}.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = min(len(order), cap)
    (out_dir / "meta.json").write_text(json.dumps({"donors": n, "all_donors": len(order), "cap": cap, "shards": SHARDS,
                                                  "generated": _now(), "letters": sorted(index)}, separators=(",", ":")), encoding="utf-8")
    log(f"donor profiles: {n} donors in {SHARDS} shards" + (f" (the {cap:,} largest of {len(order):,})" if len(order) > cap else "")
        + f"; {sum(1 for D in order[:cap] if D['id'] in orgs)} with registry facts, "
        f"{sum(1 for D in order[:cap] if (about.get(D['id']) or {}).get('t'))} with the model's summary")
    return n
