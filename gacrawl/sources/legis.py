"""Georgia General Assembly (legis.ga.gov) — members, committees, legislation, sponsors and roll calls,
kept current, through the site's own data service (the same calls Electo's civics/fetch_legis.py makes).

  authentication/token                       bearer token (refreshed every few minutes)
  members/list/{session}?chamber=1|2         every member of a session (1 House, 2 Senate)
  members/detail/{id}?session=&chamber=      committees, title, occupation, biography, sessions in service
  committees/list/{session}                  committees of a session
  Legislation/Search/100/{page} {sessionId}  every bill/resolution of a session: number, caption, status, sponsors
  legislation/detail/{id}                    status, roll calls (ids, captions, tallies)
  Vote/detail/{id}                           how each member voted

Records (source ga:legis, jurisdiction ga)
  legislator   one per member (every session they served; committees per session)
  bill         one per piece of legislation (sponsors with sequence: lead sponsor / co-sponsor / cross-chamber sponsor)
  vote         one per roll call fetched (meta.votes [{person, person_id, vote}], like council roll calls)
Roll calls are fetched for the landmark bills in config/legislature.yaml and, in the current sessions,
for legislation that passed, within a per-run budget; the rest is picked up on later runs.
Incremental: current sessions are re-listed every run (status changes); past sessions once; member
details re-read after recheck_days.

Citation urls are the legislation / member pages legis.ga.gov publishes (the same ones Electo cites);
`opened_url` is the API resource that was read.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Iterator

import yaml

from ..fetch import Blocked, Disallowed, Fetcher
from ..store import Record, Store

API = "https://www.legis.ga.gov/api/"
SITE = "https://www.legis.ga.gov"
SOURCE = "ga:legis"
PARTY = {0: "D", 1: "R", 2: "I"}
VOTE_CODE = {0: "Yea", 1: "Nay", 2: "Excused", 3: "Not voting"}
PASSED_RX = re.compile(r"signed by governor|effective date|act \d|passed|adopted|agreed|enrolled|governor", re.I)


def bill_number(x: dict) -> str:
    return ("H" if x.get("chamberType") == 1 else "S") + ("B" if x.get("documentType") == 1 else "R") + " " + str(x.get("number")) + (x.get("suffix") or "")


def sponsor_role(sp: dict, x: dict) -> str:
    if sp.get("sequence") == 1:
        return "lead sponsor"
    if sp.get("sponsorType") == 3:
        return "Senate sponsor" if x.get("chamberType") == 1 else "House sponsor"
    return "co-sponsor"


class TokenRefused(RuntimeError):
    """legis.ga.gov would not hand out an access token (it refuses now and then; asking again later works)."""


class Legis:
    TOKEN_WAITS = (0, 5, 20)       # seconds before each try for a token

    def __init__(self, fetcher: Fetcher, log=print):
        self.f, self.log = fetcher, log
        self._tok, self._tok_at = None, 0.0
        self.members_by_session: dict[int, dict[int, dict]] = {}

    # ------------------------------------------------------------------ transport
    def token(self) -> str:
        """The site's public access token (the same one its own pages ask for), renewed every 200 s. A refusal is
        asked again after a short wait; TokenRefused if it keeps refusing (the caller stops for this run)."""
        if self._tok and time.time() - self._tok_at < 200:
            return self._tok
        status = 0
        for wait in self.TOKEN_WAITS:
            if wait:
                dl = self.f.deadline() if hasattr(self.f, "deadline") else None
                if dl is not None and time.monotonic() + wait > dl:
                    break
                time.sleep(wait)
            r = self.f.get(API + "authentication/token", use_cache=False)
            status = r.status
            if status == 200:
                self._tok, self._tok_at = r.json(), time.time()
                return self._tok
        raise TokenRefused(f"legis token: HTTP {status}")

    def call(self, path: str, body=None):
        for attempt in range(2):
            headers = {"Authorization": "Bearer " + self.token(), "Accept": "application/json"}
            if body is not None:
                r = self.f.post(API + path, headers=headers, json_body=body)
            else:
                r = self.f.get(API + path, headers=headers, use_cache=False)
            if r.status == 401 and attempt == 0:
                self._tok = None
                continue
            if r.status in (400, 404):
                return None
            if r.status != 200:
                raise RuntimeError(f"legis {path}: HTTP {r.status}")
            return r.json() if r.content else None
        return None

    # ------------------------------------------------------------------ members
    def members(self, session: int) -> dict[int, dict]:
        if session in self.members_by_session:
            return self.members_by_session[session]
        out = {}
        for ch in (1, 2):
            for m in self.call(f"members/list/{session}?chamber={ch}") or []:
                out[m["id"]] = {**m, "chamber": ch}
        self.members_by_session[session] = out
        return out

    def member_detail(self, member_id: int, session: int, chamber: int) -> dict | None:
        return self.call(f"members/detail/{member_id}?session={session}&chamber={chamber}")

    def legislator_record(self, m: dict, detail: dict | None, session: int, names: dict | None = None) -> Record:
        ch = m.get("chamber") or (m.get("district") or {}).get("chamberType") or 1
        chamber = "house" if ch == 1 else "senate"
        sessions = []
        for s in (detail or {}).get("sessionsInService") or []:
            sess = s.get("session") or {}
            sessions.append({"id": sess.get("id"), "description": sess.get("description"), "chamber": "House" if s.get("chamber") == 1 else "Senate",
                             "title": s.get("title"), "committees": [{"name": (c.get("committee") or {}).get("name"), "role": c.get("role")}
                                                                      for c in s.get("committeeMemberships") or []]})
        committees = [{"name": (c.get("committee") or {}).get("name"), "role": c.get("role")} for c in (detail or {}).get("committeeMemberships") or []]
        name = m.get("fullName") or (detail or {}).get("displayName") or ""
        return Record(SOURCE, "legislator", f"{SITE}/members/{chamber}/{m['id']}?session={session}", title=name,
                      text=" · ".join(x for x in (name, f"{'House' if ch == 1 else 'Senate'} District {m.get('districtNumber')}",
                                                  PARTY.get(m.get("party"), ""), (detail or {}).get("title") or "",
                                                  (detail or {}).get("occupation") or "", (detail or {}).get("biography") or "") if x)[:20000],
                      channel="api", jurisdiction="ga", evidence="primary", key=f"{SOURCE}|legislator|{m['id']}",
                      meta={"member_id": m["id"], "name": name, "first": (m.get("name") or {}).get("first") or m.get("firstName"),
                            "last": (m.get("name") or {}).get("last") or m.get("lastName"), "nickname": (m.get("name") or {}).get("nickname"),
                            "chamber": "House" if ch == 1 else "Senate", "district": m.get("districtNumber"), "party": PARTY.get(m.get("party"), ""),
                            "city": m.get("city") or m.get("residence"), "title": (detail or {}).get("title"), "occupation": (detail or {}).get("occupation"),
                            "committees": committees, "sessions": sessions, "session": session, "vacated": m.get("dateVacated"),
                            "checked": dt.date.today().isoformat(), "opened": True,
                            "opened_url": f"{API}members/detail/{m['id']}?session={session}&chamber={ch}"})

    # ------------------------------------------------------------------ legislation
    def legislation(self, session: int) -> Iterator[dict]:
        page, seen = 0, 0
        while page < 400:
            d = self.call(f"Legislation/Search/100/{page}", {"sessionId": session})
            if not d or not d.get("results"):
                return
            for x in d["results"]:
                yield x
            seen += len(d["results"])
            if seen >= d.get("resultCount", 0):
                return
            page += 1

    def bill_record(self, x: dict, session_name: str) -> Record:
        lid = x["legislationId"]
        act = x.get("actVetoNumber") or ""
        number = bill_number(x)
        sponsors = [{"member_id": sp.get("memberId"), "name": (sp.get("name") or "").strip(" ,"), "sequence": sp.get("sequence"),
                     "role": sponsor_role(sp, x), "district": sp.get("district"), "chamber": "House" if sp.get("chamber") == 0 else "Senate"}
                    for sp in x.get("sponsors") or []]
        return Record(SOURCE, "bill", f"{SITE}/legislation/{lid}", title=f"{number}: {x.get('caption') or ''}"[:300],
                      text=f"{number} ({session_name}) — {x.get('caption') or ''}\nStatus: {x.get('status') or ''} {x.get('statusDate') or ''}\n"
                           f"Sponsors: " + "; ".join(f"{s['name']} ({s['role']})" for s in sponsors),
                      published=_d10(x.get("statusDate")), channel="api", jurisdiction="ga", evidence="primary",
                      key=f"{SOURCE}|bill|{lid}",
                      meta={"legislation_id": lid, "number": number, "session": (x.get("session") or {}).get("id"), "session_name": session_name,
                            "caption": x.get("caption") or "", "status": (x.get("status") or "").strip(), "status_date": _d10(x.get("statusDate")),
                            "enacted": bool(act) and not str(act).startswith("V"), "vetoed": str(act).startswith("V"), "act": act,
                            "sponsors": sponsors, "house_committee": (x.get("houseCommittee") or {}).get("name"),
                            "senate_committee": (x.get("senateCommittee") or {}).get("name"), "chamber": "House" if x.get("chamberType") == 1 else "Senate",
                            "opened": True, "opened_url": f"{API}Legislation/Search/100/0 (sessionId {(x.get('session') or {}).get('id')})"})

    # ------------------------------------------------------------------ roll calls
    def roll_calls(self, lid: int, number: str, session: int, session_name: str, caption: str) -> list[Record]:
        det = self.call(f"legislation/detail/{lid}")
        if not det:
            return []
        members = self.members(session)
        out = []
        for v in det.get("votes") or []:
            roll = self.call(f"Vote/detail/{v['id']}")
            rows = (roll or {}).get("votes") if isinstance(roll, dict) else None
            if not rows:
                continue
            votes = []
            for r in rows:
                mid = (r.get("member") or {}).get("id")
                m = members.get(mid) or {}
                votes.append({"person": m.get("fullName") or (r.get("member") or {}).get("name") or "", "person_id": mid,
                              "vote": VOTE_CODE.get(r.get("memberVoted"), str(r.get("memberVoted"))),
                              "party": PARTY.get(m.get("party"), ""), "district": m.get("districtNumber")})
            chamber = "Senate" if (v.get("name") or "").startswith("Senate") else "House"
            yea, nay = v.get("yea") or 0, v.get("nay") or 0
            out.append(Record(SOURCE, "vote", f"{SITE}/legislation/{lid}", title=f"{number} — {v.get('name')}: {v.get('caption') or ''}"[:300],
                              text=f"{number} ({session_name}) {caption}\n{v.get('name')} {v.get('caption')}: {yea}-{nay}",
                              published=_d10(v.get("date")), channel="api", jurisdiction="ga", evidence="primary",
                              key=f"{SOURCE}|vote|{v['id']}",
                              meta={"VoteId": v["id"], "legislation_id": lid, "MatterFile": number, "bill": number, "session": session,
                                    "session_name": session_name, "caption": caption, "vote_name": v.get("name"), "vote_caption": v.get("caption"),
                                    "EventBodyName": f"Georgia {chamber}", "chamber": chamber, "date": _d10(v.get("date")),
                                    "yea": yea, "nay": nay, "not_voting": v.get("notVoting"), "excused": v.get("excused"),
                                    "result": "passed" if yea > nay else "failed", "substantive": _substantive(v.get("caption") or ""),
                                    "votes": votes, "opened": True, "opened_url": f"{API}Vote/detail/{v['id']}"}))
        return out


def _substantive(caption: str) -> bool:
    return bool(re.search(r"PASS|ADOPT|AGREE|CONCUR|OVERRIDE|VETO|FINAL", caption, re.I)) and \
        not re.search(r"ENGROSS|TABLE|RECONSIDER|POSTPONE", caption, re.I)


def _d10(s) -> str | None:
    if not s:
        return None
    s = str(s)
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


# ----------------------------------------------------------------------------- the run
def load_config(root) -> dict:
    p = root / "config" / "legislature.yaml"
    return yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) if p.exists() else {}


def update(fetcher: Fetcher, store: Store, root, deadline: float | None = None, log=print, max_bills: int | None = None) -> dict:
    """Members, legislation and roll calls, incrementally, within the deadline."""
    cfg = load_config(root)
    sessions = cfg.get("sessions") or {}
    current = [int(s) for s in sessions.get("current") or []]
    history = [int(s) for s in sessions.get("history") or []]
    names = {int(k): v for k, v in (sessions.get("names") or {}).items()}
    api = Legis(fetcher, log)
    out = {"members": 0, "member_details": 0, "bills": 0, "votes": 0, "sessions": []}
    recheck = int((cfg.get("members") or {}).get("recheck_days", 30))
    cutoff = (dt.date.today() - dt.timedelta(days=recheck)).isoformat()

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    try:
        # 1. members: current sessions every run; past sessions once. A member's detail (which lists every
        #    session they served) is read once per recheck period, however many session lists name them.
        order = sorted(set(current) | set(history), key=lambda s: (s in current, s), reverse=True)
        for sid in order:
            if timed_out():
                break
            done_key = f"legis:members:{sid}"
            if sid not in current and store.kv_get(done_key):
                continue
            members = api.members(sid)
            for m in members.values():
                existing = store.query("kind='legislator' AND key=?", (f"{SOURCE}|legislator|{m['id']}",), limit=1)
                if existing and (existing[0]["meta"].get("checked") or "") >= cutoff:
                    continue  # read recently; the detail already lists all sessions
                detail = None
                if not timed_out():
                    try:
                        detail = api.member_detail(m["id"], sid, m.get("chamber") or 1)
                        out["member_details"] += 1
                    except (Blocked, Disallowed):
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log(f"  member {m['id']}: {type(exc).__name__}: {exc}"[:200])
                if detail is None and existing:
                    continue  # keep what we have
                latest = sid
                if existing and existing[0]["meta"].get("session"):
                    latest = max(sid, int(existing[0]["meta"]["session"]))   # the member page is cited at their latest session
                if store.add(api.legislator_record(m, detail, latest)):
                    out["members"] += 1
            if not timed_out():
                store.kv_set(done_key, dt.date.today().isoformat())
            out["sessions"].append(sid)
        store.flush()
        # 2. legislation: current sessions every run; past sessions once
        for sid in current + [s for s in history if s not in current]:
            if timed_out():
                break
            done_key = f"legis:legislation:{sid}"
            if sid not in current and store.kv_get(done_key):
                continue
            n = 0
            for x in api.legislation(sid):
                if store.add(api.bill_record(x, names.get(sid, str(sid)))):
                    n += 1
            out["bills"] += n
            if not timed_out():
                store.kv_set(done_key, dt.date.today().isoformat())
            store.flush()
        # 3. roll calls: landmark bills first, then what passed in the current sessions (budgeted)
        votes_cfg = cfg.get("votes") or {}
        budget = max_bills if max_bills is not None else int(votes_cfg.get("max_bills_per_run", 40))
        todo: list[tuple[int, dict]] = []
        # one pass over the stored bills (instead of a filtered scan per landmark bill)
        wanted = {(int(kb["session"]), kb["bill"]) for kb in votes_cfg.get("key_bills") or []}
        passed_current = votes_cfg.get("all_passed_in_current", True)
        for b in store.iter_query("source_id=? AND kind='bill'", (SOURCE,)):
            m = b["meta"]
            sid = m.get("session")
            if (sid, m.get("number")) in wanted:
                todo.append((1, b))
            elif passed_current and sid in current and (PASSED_RX.search(m.get("status") or "") or m.get("enacted")):
                todo.append((2, b))
        seen_ids = set()
        fetched = 0
        for _, b in sorted(todo, key=lambda t: t[0]):
            lid = b["meta"]["legislation_id"]
            if lid in seen_ids or timed_out() or fetched >= budget:
                continue
            seen_ids.add(lid)
            mark = f"legis:votes:{lid}"
            prev = store.kv_get(mark)
            if prev and (prev >= (b["meta"].get("status_date") or "") or b["meta"]["session"] not in current):
                continue  # fetched since the bill last moved
            try:
                recs = api.roll_calls(lid, b["meta"]["number"], int(b["meta"]["session"]), b["meta"].get("session_name", ""), b["meta"].get("caption", ""))
            except (Blocked, Disallowed):
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"  votes {lid}: {type(exc).__name__}: {exc}"[:200])
                continue
            fetched += 1
            for r in recs:
                if store.add(r):
                    out["votes"] += 1
            store.kv_set(mark, dt.date.today().isoformat())
        store.flush()
    except (Blocked, Disallowed) as exc:
        out["error"] = str(exc)
    return out


# ----------------------------------------------------------------------------- per person, in Electo's shapes
def legis_people(store: Store, candidates: list[dict], root=None) -> dict:
    """{electo_key or candidate id: {member, terms, committees, votes[], bills[]}} for candidates who are or were
    legislators, matched by name variants and (when the candidate's races name a seat) chamber + district."""
    from ..names import name_variants

    cfg = load_config(root) if root else {}
    years = {int(k): v for k, v in ((cfg.get("sessions") or {}).get("years") or {}).items()}
    names = {int(k): v for k, v in ((cfg.get("sessions") or {}).get("names") or {}).items()}
    legislators = store.query("kind='legislator'", limit=20000)
    by_norm: dict[str, list[dict]] = {}
    for lg in legislators:
        m = lg["meta"]
        for v in name_variants(m.get("name") or "", [f"{m.get('nickname')} {m.get('last')}"] if m.get("nickname") else []):
            by_norm.setdefault(re.sub(r"[^a-z]", "", v.lower()), []).append(lg)
    bills_by_member: dict[int, list[dict]] = {}
    for b in store.iter_query("kind='bill'"):
        m = b["meta"]
        for sp in m.get("sponsors") or []:
            if sp.get("member_id"):
                bills_by_member.setdefault(sp["member_id"], []).append({
                    "number": m["number"], "session": m.get("session_name") or str(m.get("session")), "session_id": m.get("session"),
                    "caption": m.get("caption", ""), "role": sp.get("role"), "status": m.get("status"), "enacted": m.get("enacted"),
                    "act": m.get("act"), "u": b["url"], "seq": sp.get("sequence") or 99, "src": []})
    votes_by_member: dict[int, list[dict]] = {}
    for v in store.iter_query("kind='vote' AND source_id='ga:legis'"):
        m = v["meta"]
        if not m.get("substantive"):
            continue
        for x in m.get("votes") or []:
            if x.get("person_id"):
                votes_by_member.setdefault(x["person_id"], []).append({
                    "bill": m["bill"], "session": m.get("session_name") or str(m.get("session")), "title": m.get("caption", ""),
                    "vote": x["vote"], "d": m.get("date"), "stage": f"{m.get('vote_name')} {str(m.get('vote_caption') or '').title()} ({m.get('yea')}-{m.get('nay')})",
                    "u": v["url"], "basis": "roll call", "src": []})
    out = {}
    for c in candidates:
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        hits = []
        for v in variants:
            hits += by_norm.get(re.sub(r"[^a-z]", "", v.lower()), [])
        if not hits:
            continue
        seats = set()
        for r in c.get("races") or []:
            mm = re.match(r"State (House|Senate) District (\d+)", r.get("office") or "")
            if mm:
                seats.add((mm.group(1), int(mm.group(2))))
        chosen = None
        for lg in dict((h["id"], h) for h in hits).values():
            m = lg["meta"]
            if not seats or (m.get("chamber"), int(m.get("district") or 0)) in seats:
                chosen = lg
                break
        if not chosen:
            if len({h["id"] for h in hits}) == 1 and not seats:
                chosen = hits[0]
            else:
                continue
        m = chosen["meta"]
        mid = m["member_id"]
        terms = []
        for s in m.get("sessions") or []:
            y = years.get(s.get("id") or -1) or ["", ""]
            terms.append({"from": f"{y[0]}-01-01" if y[0] else "", "to": f"{y[1]}-12-31" if y[1] else "", "role": "member" if not s.get("title") else s["title"],
                          "body": f"Georgia {s.get('chamber')} ({s.get('description') or names.get(s.get('id'), '')})", "u": chosen["url"],
                          "t": f"Georgia General Assembly member page: {m.get('name')}",
                          "text": f"{s.get('chamber')} District {m.get('district')}, {s.get('description') or ''}"})
        committees = [{"name": cm.get("name"), "role": cm.get("role"), "session": s.get("description")}
                      for s in m.get("sessions") or [] for cm in s.get("committees") or []] or [
            {"name": cm.get("name"), "role": cm.get("role"), "session": ""} for cm in m.get("committees") or []]
        bl = bills_by_member.get(mid, [])
        lead = sorted([b for b in bl if b["role"] == "lead sponsor"], key=lambda b: (not b["enacted"], b["session"], b["number"]))
        other = sorted([b for b in bl if b["role"] != "lead sponsor"], key=lambda b: (not b["enacted"], b["session"], b["seq"], b["number"]))
        key = c.get("electo_key") or c["id"]
        out[key] = {"candidate_id": c["id"], "member": {"id": mid, "name": m.get("name"), "party": m.get("party"), "chamber": m.get("chamber"),
                                                        "district": m.get("district"), "title": m.get("title"), "u": chosen["url"], "terms": terms},
                    "committees": committees, "votes": sorted(votes_by_member.get(mid, []), key=lambda v: v.get("d") or "", reverse=True)[:400],
                    "bills": (lead + other)[:200], "counts": {"bills": len(bl), "lead": len(lead), "votes": len(votes_by_member.get(mid, []))}}
    return out
