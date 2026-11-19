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
Roll calls are fetched for the landmark bills in config/legislature.yaml, in the current sessions for
legislation that passed, and then (votes.all_floor_votes) for every bill of every followed session whose record
shows it reached a floor vote, newest session first, within a per-run budget; the rest is picked up on later runs.
Every member's vote on every roll call is what the vote model (gacrawl/vote_model.py) learns from.
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

from ..fetch import Blocked, Disallowed, Fetcher, OutOfTime
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


# the last step on a bill's record that only a floor vote leads to; a step in the other chamber counts too (reached_floor)
FLOOR_RX = re.compile(r"passed|adopted|agree|lost|third read|conference|transmitted|insist|recede|signed by governor|vetoed|"
                      r"effective date|act \d|enrolled|reconsider|tabled", re.I)


def reached_floor(meta: dict) -> bool:
    """Whether a bill's record shows it got as far as a floor vote: an act or veto number, a last step taken on the
    floor, or a last step in the other chamber (it had to pass its own to get there). A resolution read and adopted
    the day it was offered is taken by voice, with no roll call to read, so it is left out."""
    status = (meta.get("status") or "").strip()
    if meta.get("enacted") or meta.get("vetoed"):
        return True
    if re.search(r"read and adopted", status, re.I):
        return False
    origin = (meta.get("chamber") or "")[:1].upper() or (meta.get("number") or " ")[:1].upper()
    if status[:1].upper() in ("H", "S") and origin in ("H", "S") and status[:1].upper() != origin:
        return True
    return bool(FLOOR_RX.search(status))


class TokenRefused(RuntimeError):
    """legis.ga.gov would not hand out an access token (it refuses now and then; asking again later works)."""


class Legis:
    # seconds before each try for a token: legis.ga.gov refuses one now and then (Nov 9: for about six minutes, after
    # 25 minutes of steady requests), so a refusal is met by waiting longer, not by asking again at once
    TOKEN_WAITS = (0, 20, 60)
    REFUSED_PAUSE = 900            # after a refusal, no token is asked for again in this run for this long

    def __init__(self, fetcher: Fetcher, log=print):
        self.f, self.log = fetcher, log
        self._tok, self._tok_at = None, 0.0
        self.members_by_session: dict[int, dict[int, dict]] = {}
        self.refused_until = 0.0
        self.refusals = 0

    # ------------------------------------------------------------------ transport
    def token(self) -> str:
        """The site's public access token (the same one its own pages ask for), renewed every 200 s. A refusal is
        asked again after a wait (TOKEN_WAITS); TokenRefused if it keeps refusing, and then no token is asked for again
        for REFUSED_PAUSE seconds (the caller stops this step: one refusal used to cost 25 s for every bill left)."""
        if self._tok and time.time() - self._tok_at < 200:
            return self._tok
        if time.monotonic() < self.refused_until:
            raise TokenRefused("legis token: refused earlier in this run; not asked again yet")
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
        self.refusals += 1
        self.refused_until = time.monotonic() + self.REFUSED_PAUSE
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
            check = check_roll_call({"yea": yea, "nay": nay, "not_voting": v.get("notVoting"), "excused": v.get("excused")}, votes, chamber)
            out.append(Record(SOURCE, "vote", f"{SITE}/legislation/{lid}", title=f"{number} — {v.get('name')}: {v.get('caption') or ''}"[:300],
                              text=f"{number} ({session_name}) {caption}\n{v.get('name')} {v.get('caption')}: {yea}-{nay}",
                              published=_d10(v.get("date")), channel="api", jurisdiction="ga", evidence="primary",
                              key=f"{SOURCE}|vote|{v['id']}",
                              meta={"VoteId": v["id"], "legislation_id": lid, "MatterFile": number, "bill": number, "session": session,
                                    "session_name": session_name, "caption": caption, "vote_name": v.get("name"), "vote_caption": v.get("caption"),
                                    "EventBodyName": f"Georgia {chamber}", "chamber": chamber, "date": _d10(v.get("date")),
                                    "yea": yea, "nay": nay, "not_voting": v.get("notVoting"), "excused": v.get("excused"),
                                    "result": "passed" if yea > nay else "failed", "substantive": _substantive(v.get("caption") or ""),
                                    "votes": votes, "check": check, "opened": True, "opened_url": f"{API}Vote/detail/{v['id']}"}))
        return out


SEATS = {"House": 180, "Senate": 56}
CHECK_TRIES = 3          # a bill whose roll calls fail the check is read again on later runs, this many times at most


def check_roll_call(reported: dict, votes: list[dict], chamber: str) -> dict:
    """Whether a roll call's member-by-member list is whole and agrees with the tally the site reports beside it:
    Yea and Nay counted from the list equal the reported Yea and Nay, the members who did not vote (excused or not
    voting, taken together) equal the reported ones, nobody is listed twice, and no more members than the chamber has
    seats. A list that fails is kept but marked, read again later, and left out of the vote model."""
    from collections import Counter

    c = Counter(v.get("vote") for v in votes)
    issues = []
    for label, field in (("Yea", "yea"), ("Nay", "nay")):
        if c.get(label, 0) != int(reported.get(field) or 0):
            issues.append(f"{label}: {c.get(label, 0)} listed, {int(reported.get(field) or 0)} reported")
    others = c.get("Not voting", 0) + c.get("Excused", 0)
    rep_others = int(reported.get("not_voting") or 0) + int(reported.get("excused") or 0)
    if others != rep_others:
        issues.append(f"not voting or excused: {others} listed, {rep_others} reported")
    ids = [v.get("person_id") for v in votes if v.get("person_id") is not None]
    if len(ids) != len(set(ids)):
        issues.append(f"{len(ids) - len(set(ids))} member(s) listed twice")
    seats = SEATS.get(chamber)
    if seats and len(votes) > seats:
        issues.append(f"{len(votes)} members listed; the {chamber} has {seats} seats")
    unknown = sum(1 for v in votes if not v.get("party"))
    if votes and unknown > max(2, len(votes) // 20):
        issues.append(f"{unknown} voters not on the session's member list")
    return {"ok": not issues, "listed": len(votes), "issues": issues}


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


def _read_already(store: Store, b: dict, current: list) -> bool:
    prev = store.kv_get(f"legis:votes:{b['meta']['legislation_id']}")
    return bool(prev) and (prev >= (b["meta"].get("status_date") or "") or b["meta"].get("session") not in current)


def votes_todo(store: Store, cfg: dict, current: list, history: list) -> list[tuple[int, int, dict]]:
    """The bills whose roll calls are wanted, in reading order: landmark bills, the current sessions' passed bills,
    then every other bill of a followed session that reached a floor vote, newest session first, and last the past
    bills LegiScan already gave in full (the site reading them is the cross-check) (no requests)."""
    votes_cfg = cfg.get("votes") or {}
    wanted = {(int(kb["session"]), kb["bill"]) for kb in votes_cfg.get("key_bills") or []}
    passed_current = votes_cfg.get("all_passed_in_current", True)
    every_floor = votes_cfg.get("all_floor_votes", True)
    followed = set(current) | set(history)
    covered = legiscan_covered(store)
    todo = []
    for b in store.iter_query("source_id=? AND kind='bill'", (SOURCE,)):
        m = b["meta"]
        sid = m.get("session")
        if (sid, m.get("number")) in wanted:
            todo.append((1, 0, b))
        elif passed_current and sid in current and (PASSED_RX.search(m.get("status") or "") or m.get("enacted")):
            todo.append((2, 0, b))
        elif every_floor and sid in followed and reached_floor(m):
            # LegiScan first: a past bill whose every roll call LegiScan already gave, each adding up to its own tally,
            # is in the vote model already; the site's reading of it is the cross-check, so it waits behind the bills
            # no source has given yet
            todo.append((4 if m.get("legislation_id") in covered else 3, -int(sid or 0), b))
    return sorted(todo, key=lambda t: t[:2])


def legiscan_covered(store: Store) -> set:
    """legis.ga.gov legislation ids whose LegiScan roll calls are all on file and all pass check_roll_call (no requests)."""
    ok, bad = set(), set()
    for v in store.iter_query("source_id='ga:legiscan' AND kind='vote'"):
        m = v.get("meta") or {}
        lid = m.get("legislation_id")
        if lid is None:
            continue
        (ok if (m.get("check") or {}).get("ok") else bad).add(lid)
    return ok - bad


def votes_waiting(store: Store, root) -> int:
    """How many bills' roll calls are still to read (no requests): the nightly job ends at once when this is 0."""
    from .. import legis_sessions

    S = legis_sessions.load(root, store)
    seen, n = set(), 0
    for _, _, b in votes_todo(store, load_config(root), S["current"], S["history"]):
        lid = b["meta"]["legislation_id"]
        if lid not in seen:
            seen.add(lid)
            n += not _read_already(store, b, S["current"])
    return n


def update(fetcher: Fetcher, store: Store, root, deadline: float | None = None, log=print, max_bills: int | None = None,
           votes_only: bool = False) -> dict:
    """Members, legislation and roll calls, incrementally, within the deadline. votes_only: roll calls alone (the
    nightly legis-votes job; the crawl keeps members and bill lists current)."""
    from .. import legis_sessions

    cfg = load_config(root)
    api = Legis(fetcher, log)
    # the sessions followed: the config's, plus any newer one the legislature's own list showed (refreshed below)
    S = legis_sessions.load(root, store)
    current, history, names = S["current"], S["history"], S["names"]
    out = {"members": 0, "member_details": 0, "bills": 0, "votes": 0, "sessions": []}
    recheck = int((cfg.get("members") or {}).get("recheck_days", 30))
    cutoff = (dt.date.today() - dt.timedelta(days=recheck)).isoformat()

    def timed_out():
        return deadline is not None and time.monotonic() > deadline

    try:
        # 0. the legislature's own list of sessions: a session newer than the config's is followed from now on
        if not timed_out() and not votes_only:
            try:
                legis_sessions.save(store, api.call("sessions"))
            except (Blocked, Disallowed, TokenRefused):
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"  sessions list: {type(exc).__name__}: {exc}"[:200])
            S = legis_sessions.load(root, store)
            current, history, names = S["current"], S["history"], S["names"]
            if S["new"]:
                log(f"  sessions the legislature lists beyond config/legislature.yaml, now followed: {S['new']}")
        # 1. members: current sessions every run; past sessions once. A member's detail (which lists every
        #    session they served) is read once per recheck period, however many session lists name them.
        order = [] if votes_only else sorted(set(current) | set(history), key=lambda s: (s in current, s), reverse=True)
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
                    except (Blocked, Disallowed, TokenRefused, OutOfTime):
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
        for sid in ([] if votes_only else current + [s for s in history if s not in current]):
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
        # 3. roll calls: landmark bills first, then what passed in the current sessions, then every other bill that
        #    reached a floor vote, newest session first (budgeted; a closed session's bill is read once)
        budget = max_bills if max_bills is not None else int((cfg.get("votes") or {}).get("max_bills_per_run", 40))
        todo = votes_todo(store, cfg, current, history)
        seen_ids = set()
        fetched = 0
        waiting = 0
        spent = False
        for _, _, b in sorted(todo, key=lambda t: t[:2]):
            lid = b["meta"]["legislation_id"]
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            if _read_already(store, b, current):
                continue  # fetched since the bill last moved
            mark = f"legis:votes:{lid}"
            if timed_out() or spent or fetched >= budget:
                waiting += 1   # counted, so each run says how many bills' roll calls are still to read
                continue
            try:
                recs = api.roll_calls(lid, b["meta"]["number"], int(b["meta"]["session"]), b["meta"].get("session_name", ""), b["meta"].get("caption", ""))
            except (Blocked, Disallowed, TokenRefused):
                raise
            except OutOfTime:
                spent = True   # the next request would end past the deadline: this bill and the rest wait, counted
                waiting += 1
                continue
            except Exception as exc:  # noqa: BLE001
                log(f"  votes {lid}: {type(exc).__name__}: {exc}"[:200])
                continue
            fetched += 1
            bad = 0
            for r in recs:
                bad += not (r.meta.get("check") or {}).get("ok", True)
                if store.add(r):
                    out["votes"] += 1
            if bad:
                # a list that does not add up to its own tally: read again on a later run, CHECK_TRIES times at most
                tries = int(store.kv_get(f"legis:votes_try:{lid}") or 0) + 1
                store.kv_set(f"legis:votes_try:{lid}", str(tries))
                out["failed_check"] = out.get("failed_check", 0) + bad
                if tries < CHECK_TRIES:
                    continue
            store.kv_set(mark, dt.date.today().isoformat())
        out["vote_bills"], out["vote_bills_waiting"] = fetched, waiting
        store.flush()
    except (Blocked, Disallowed) as exc:
        out["error"] = str(exc)
    except OutOfTime as exc:
        # the time budget ran out between two requests (the fetcher will not start a wait that ends past the
        # deadline). Until Nov. 2026 this escaped: with a minute or two left in the merge job the step died with a
        # traceback while listing bills, the roll calls were never reached, the run was not counted in
        # legis_refusals and the refusal marker was not cleared. What was read is kept; the rest is next run's.
        store.flush()
        out["out_of_time"] = str(exc)[:160]
        log(f"the legislature step's time is up ({exc}); what was read is kept, the rest waits for the next run")
    except TokenRefused as exc:
        # the site said no for now: what was read is kept, the step stops (every bill left would only be refused
        # too), and the refusal is counted so the coverage report shows how often it happens
        store.flush()
        out["refused"] = str(exc)
        log(f"legis.ga.gov refused an access token ({exc}); this step stops here and carries on next time")
    note_refusal(store, out.get("refused"))
    return out


def note_refusal(store: Store, refused: str | None) -> None:
    """Keep the last 60 runs' outcome of the legislature step in kv legis_refusals ({at, refused}), for the coverage
    report: an occasional refusal recovers by itself; frequent ones mean bill and vote updates are lagging."""
    import json

    try:
        hist = json.loads(store.kv_get("legis_refusals") or "[]")
    except ValueError:
        hist = []
    hist.append({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "refused": bool(refused)})
    store.kv_set("legis_refusals", json.dumps(hist[-60:]))


# ----------------------------------------------------------------------------- per person, in Electo's shapes
# Generational suffixes tell a father from a son, so they are part of a name; degrees after a name are not.
GENERATIONS = {"jr": "jr", "sr": "sr", "ii": "ii", "iii": "iii", "iv": "iv", "v": "v"}
CREDENTIALS = {"esq", "md", "phd", "dds", "cpa"}


def full_names(raws) -> set[tuple[str, str]]:
    """Every way the names in raws are written in full, as (the letters of the whole name, generational suffix):
    'Harvey Williams, Jr.' -> ('harveywilliams', 'jr'); 'Lauren McDonald III' -> ('laurenmcdonald', 'iii');
    'Elizabeth "Beth" Beskin' -> ('elizabethbeskin', ''), ('bethbeskin', ''). Middle names and initials are kept, so
    'David Keith Clark' is not 'David Clark'. (names.name_variants drops suffixes and middles on purpose, to find a
    person in text; it also reads 'Noel Williams, Jr.' as a reversed 'Jr. Noel Williams', which is how a Harvey
    Williams, Jr. once got Noel Williams, Jr.'s House record.)"""
    from ..names import QUOTED, TITLES, _ascii

    out = set()
    for raw in raws:
        if not raw:
            continue
        raw = _ascii(raw)
        nicks = [re.sub(r"[^a-z]", "", n.lower()) for n in QUOTED.findall(raw)]
        base = TITLES.sub("", re.sub(r"\([^)]*\)", " ", QUOTED.sub(" ", raw)).strip())
        sfx, parts = "", []
        for i, part in enumerate(base.split(",")):
            words = [w for w in (re.sub(r"[^a-z]", "", x.lower()) for x in part.split()) if w]
            # a suffix ends a part: 'McDonald III', or a part of its own after a comma ('Williams, Jr.')
            while words and (len(words) > 1 or i > 0) and (words[-1] in GENERATIONS or words[-1] in CREDENTIALS):
                sfx = sfx or GENERATIONS.get(words[-1], "")
                words.pop()
            if words:
                parts.append(words)
        if len(parts) == 2 and len(parts[0]) <= 2:  # "DOE, JANE A"
            parts = [parts[1], parts[0]]
        words = [w for p in parts for w in p]
        if len(words) < 2:
            continue
        out.add(("".join(words), sfx))
        for n in nicks:     # 'William J. "Bill" Yearta' is also Bill J. Yearta and Bill Yearta
            if n:
                out.update({(n + "".join(words[1:]), sfx), (n + words[-1], sfx)})
    return out


def member_full_names(m: dict) -> set[tuple[str, str]]:
    """full_names of a legislator record: the page's full name, and first and last name (with the nickname)."""
    first, last, nick = m.get("first") or "", m.get("last") or "", m.get("nickname") or ""
    return full_names([m.get("name") or "", f'{first} "{nick}" {last}' if nick else f"{first} {last}"])


def suffix_comma(name: str | None) -> str:
    """'Noel Williams, Jr.' -> 'Noel Williams Jr.'. names.name_variants reads a two-word name with a comma as a
    reversed 'DOE, JANE', so it gave 'Jr. Noel Williams' and 'Jr. Williams': the member himself (and every candidate
    written that way) went unfound, and every 'X Williams, Jr.' matched every other one."""
    return re.sub(r",\s*((?:jr|sr|ii|iii|iv|v)\.?)$", r" \1", (name or "").strip(), flags=re.I)


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def place_index(root=None) -> dict:
    """Counties and municipalities of config/jurisdictions.yaml, for telling where a member and a candidate live:
    {"county": {alnum name: alnum name}, "county_id": {id: alnum name}, "city": {alnum name: (id, {counties})},
    "city_id": {id: {counties}}}."""
    from pathlib import Path

    from ..registry import load_yaml

    p = Path(root) / "config" / "jurisdictions.yaml" if root else None
    if not p or not p.exists():     # a test's project folder has no registry: the package's own is the same map
        p = Path(__file__).resolve().parents[2] / "config" / "jurisdictions.yaml"
    reg = load_yaml(p) or {}
    idx = {"county": {}, "county_id": {}, "city": {}, "city_id": {}}
    for c in reg.get("counties") or []:
        n = _alnum(c["name"].removesuffix(" County"))
        idx["county"][n] = n
        idx["county_id"][c["id"]] = n
    for m in reg.get("municipalities") or []:
        cs = {_alnum(x) for x in m.get("counties") or []}
        idx["city"][_alnum(m["name"])] = (m["id"], cs)
        idx["city_id"][m["id"]] = cs
    return idx


def candidate_places(c: dict, idx: dict) -> tuple[set, set, bool]:
    """(counties, city ids, whether the candidate carries any place at all) from the candidate's counties, its races'
    counties and its jurisdictions. A place that is not in the registry still counts as carried: it cannot be
    checked, so a match that needs it is not made."""
    counties = {_alnum(x) for x in (c.get("counties") or []) + [r.get("county") for r in c.get("races") or []] if x}
    cities, any_place = set(), bool(counties)
    for jid in set(c.get("jurisdictions") or []) | ({c["jurisdiction"]} if c.get("jurisdiction") else set()):
        any_place = True
        if jid in idx["county_id"]:
            counties.add(idx["county_id"][jid])
        elif jid in idx["city_id"]:
            cities.add(jid)
            counties |= idx["city_id"][jid]
    return {x for x in counties if x}, cities, any_place


def member_places(m: dict, idx: dict) -> tuple[set, set]:
    """(counties, city ids) of the city a legislator's page gives ('Cumming' -> Forsyth). Some pages give an
    unincorporated place, which tells nothing here, or a county's name, which is taken as that county."""
    city = _alnum(re.sub(r",\s*(GA|Georgia)\.?$", "", (m.get("city") or "").strip(), flags=re.I))
    if city in idx["city"]:
        cid, cs = idx["city"][city]
        return set(cs), {cid}
    if city in idx["county"]:
        return {idx["county"][city]}, set()
    return set(), set()


def match_member(c: dict, hits: list[dict], idx: dict) -> dict | None:
    """The legislator record that is this candidate, among the records its name variants found, or None.

    When the candidate's races name a State House or Senate seat, the member who held that seat (chamber + district)
    is the one. Without one (a county commissioner, a judge, a statewide candidate), a shared name is not enough:
    until Nov. 2026 the first record of that name was taken, and seven people (a Public Service Commissioner, county
    commissioners, a city council member, a judge) showed another person's House record, bills and committees. Now
    the full name, middle names and generational suffix included, must be the member's; exactly one member may
    have it; and where the candidate's records say where they ran, the member's city must lie in one of those
    counties or be that city. A candidate with no place at all (a statewide, federal or circuit race) has only the
    name to go on, so it must also not be a name Electo knows two people by (split) and must not be of the other
    party. Never the first of several."""
    seats = set()
    for r in c.get("races") or []:
        mm = re.match(r"State (House|Senate) District (\d+)", r.get("office") or "")
        if mm:
            seats.add((mm.group(1), int(mm.group(2))))
    members = list({h["id"]: h for h in hits}.values())
    names = full_names([c.get("name") or "", *(c.get("aliases") or [])])
    if seats:
        held = [lg for lg in members if (lg["meta"].get("chamber"), int(lg["meta"].get("district") or 0)) in seats]
        if len(held) > 1:   # two members of the same name in that seat (a father and a son): the full name decides
            held = [lg for lg in held if names & member_full_names(lg["meta"])]
        return held[0] if len(held) == 1 else None
    same = [lg for lg in members if names & member_full_names(lg["meta"])]
    if len(same) != 1:
        return None
    lg = same[0]
    counties, cities, any_place = candidate_places(c, idx)
    if any_place:
        mc, mcity = member_places(lg["meta"], idx)
        return lg if (mc & counties or mcity & cities) else None
    party, mparty = (c.get("party") or "")[:1].upper(), (lg["meta"].get("party") or "")[:1].upper()
    if c.get("split") or (party in ("D", "R") and mparty in ("D", "R") and party != mparty):
        return None
    return lg


def legis_people(store: Store, candidates: list[dict], root=None) -> dict:
    """{electo_key or candidate id: {member, terms, committees, votes[], bills[]}} for candidates who are or were
    legislators, found by name variants and then matched by match_member (seat, or full name and place)."""
    from ..names import name_variants

    from .. import legis_sessions

    S = legis_sessions.load(root, store) if root else {"years": {}, "names": {}}
    years, names = S["years"], S["names"]
    legislators = store.query("kind='legislator'", limit=20000)
    by_norm: dict[str, list[dict]] = {}
    for lg in legislators:
        m = lg["meta"]
        for v in name_variants(suffix_comma(m.get("name")), [f"{m.get('nickname')} {m.get('last')}"] if m.get("nickname") else []):
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
    idx = place_index(root)
    out = {}
    for c in candidates:
        # the variants Electo's import made, and the name and every alias read again here: 'Alvin Payton, Jr.' read
        # with its comma gave only 'Jr. Payton', and an alias config/candidates.yaml adds ('Lynn Heffner' for Lynn
        # Gladney) comes after the import's variants were made. Either way the seat or the full name still decides.
        variants = list(c.get("variants") or name_variants(c["name"], c.get("aliases", [])))
        variants = list(dict.fromkeys(variants + name_variants(suffix_comma(c["name"]), [suffix_comma(a) for a in c.get("aliases") or []])))
        hits = []
        for v in variants:
            hits += by_norm.get(re.sub(r"[^a-z]", "", v.lower()), [])
        if not hits:
            continue
        chosen = match_member(c, hits, idx)
        if not chosen:
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
