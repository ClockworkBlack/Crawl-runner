"""LegiScan (legiscan.com): a second source for the General Assembly's roll calls, member by member.

legis.ga.gov is the primary record. LegiScan reads the same chamber journals and publishes each state's sessions as
weekly datasets (built Sunday mornings): every bill, every roll call with each legislator's vote, every legislator.
Crawl uses them for two things:

  fill    a roll call legis.ga.gov did not give (unreachable that night, or a member list that did not add up to
          its own tally) is taken from LegiScan, so the vote model is not short of it
  check   a roll call both sources have is compared, tally and member by member (gacrawl/vote_model.py reports how
          often they agree, and every disagreement)

API: https://api.legiscan.com/?key=...&op=... with a free key (LEGISCAN_API_KEY; a GitHub secret on the runner). One
getDatasetList call a run; a dataset is downloaded (getDataset) only when its dataset_hash changed, so a quiet week
costs one call a night, well inside the free key's monthly allowance. The key is never written anywhere: not in a
record, not in an error message, not in a log line.

Records (source ga:legiscan, jurisdiction ga), in the same shape as ga:legis roll calls so the vote model reads both:
  vote   one per roll call (meta.votes [{person, person_id, vote, party, legiscan_id}]); person_id is the legis.ga.gov
         member id when the legislator is matched (chamber, district and last name), else "ls:<people_id>"
Each record's url is a link LegiScan's own data gives (the roll call's page, else the bill's legis.ga.gov page).
License: LegiScan's data is CC BY 4.0; the Electo bundle names LegiScan as the source of every roll call taken from it.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re
import unicodedata
import zipfile
from collections import defaultdict

from ..store import Record, Store
from .legis import SOURCE as LEGIS, _substantive, check_roll_call

API = "https://api.legiscan.com/"
SOURCE = "ga:legiscan"
STATE = "GA"
FIRST_YEAR = 2015                 # the sessions legis.ga.gov's archive is followed for
VOTE_TEXT = {"Yea": "Yea", "Nay": "Nay", "NV": "Not voting", "Absent": "Excused"}
CREDIT = "LegiScan (legiscan.com), CC BY 4.0"


class NoKey(RuntimeError):
    pass


def _key() -> str:
    return os.environ.get("LEGISCAN_API_KEY", "").strip()


def redact(text: str, key: str | None = None) -> str:
    """An error message with the key taken out (requests puts the whole address, query and all, in its errors)."""
    key = key if key is not None else _key()
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(key=)[^&\s'\"]+", r"\1***", text)


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def _number(n: str) -> str:
    return re.sub(r"\s+", "", (n or "").upper())


class LegiScan:
    def __init__(self, fetcher, key: str | None = None, log=print):
        self.f, self.log = fetcher, log
        self.key = key if key is not None else _key()
        self.calls = 0
        if not self.key:
            raise NoKey("no LEGISCAN_API_KEY")

    def call(self, op: str, **params) -> dict:
        self.calls += 1
        try:
            r = self.f.get(API, params={"key": self.key, "op": op, **params}, use_cache=False)
        except Exception as exc:  # noqa: BLE001  re-raised without the key
            raise RuntimeError(f"LegiScan {op}: {type(exc).__name__}: {redact(exc, self.key)}"[:300]) from None
        if r.status != 200:
            raise RuntimeError(f"LegiScan {op}: HTTP {r.status}")
        d = r.json()
        if (d or {}).get("status") != "OK":
            msg = ((d or {}).get("alert") or {}).get("message") or "not OK"
            raise RuntimeError(f"LegiScan {op}: {redact(msg, self.key)}"[:300])
        return d

    def datasets(self) -> list[dict]:
        d = self.call("getDatasetList", state=STATE)
        return [x for x in d.get("datasetlist") or [] if int(x.get("year_start") or 0) >= FIRST_YEAR]

    def dataset(self, ds: dict) -> zipfile.ZipFile:
        d = self.call("getDataset", id=ds["session_id"], access_key=ds["access_key"])
        return zipfile.ZipFile(io.BytesIO(base64.b64decode((d.get("dataset") or {}).get("zip") or "")))


# ----------------------------------------------------------------------------- matching to legis.ga.gov
class Matcher:
    """LegiScan legislators -> legis.ga.gov member ids (chamber, district, last name), and LegiScan bills -> legis.ga.gov
    legislation ids (the bill's own legis.ga.gov link, else its number in a session of the same years)."""

    def __init__(self, store: Store):
        self.by_seat: dict = defaultdict(list)
        self.party: dict = {}
        for lg in store.iter_query("kind='legislator' AND source_id=?", (LEGIS,)):
            m = lg["meta"]
            last = _norm(m.get("last") or (m.get("name") or "").split(" ")[-1])
            chambers = {s.get("chamber") for s in m.get("sessions") or []} | {m.get("chamber")}
            for ch in chambers - {None}:
                self.by_seat[(ch, m.get("district"))].append((last, m.get("member_id"), m.get("name") or ""))
            self.party[m.get("member_id")] = m.get("party") or ""
        self.bills: dict = {}
        for b in store.iter_query("kind='bill' AND source_id=?", (LEGIS,)):
            m = b["meta"]
            years = re.findall(r"\d{4}", m.get("session_name") or "")
            if years:
                self.bills[(int(years[0]), _number(m.get("number")))] = m.get("legislation_id")

    def member(self, person: dict) -> int | None:
        role = (person.get("role") or "").lower()
        ch = "House" if role.startswith("rep") else "Senate" if role.startswith("sen") else None
        dm = re.search(r"(\d+)", person.get("district") or "")
        if not ch or not dm:
            return None
        last = _norm(person.get("last_name") or "")
        hits = {mid for l, mid, _ in self.by_seat.get((ch, int(dm.group(1))), []) if last and (l == last or last in l or l in last)}
        return hits.pop() if len(hits) == 1 else None

    def legislation(self, bill: dict) -> int | None:
        for link in (bill.get("state_link"), bill.get("url")):
            m = re.search(r"legis\.ga\.gov/legislation/(\d+)", link or "")
            if m:
                return int(m.group(1))
        sess = bill.get("session") or {}
        return self.bills.get((int(sess.get("year_start") or 0), _number(bill.get("bill_number"))))


# ----------------------------------------------------------------------------- one dataset -> records
def _members_of(zf: zipfile.ZipFile, folder: str) -> dict[str, dict]:
    out = {}
    for name in zf.namelist():
        if f"/{folder}/" in f"/{name}" and name.endswith(".json"):
            try:
                out[name] = json.loads(zf.read(name))
            except ValueError:
                continue
    return out


def records_from(zf: zipfile.ZipFile, ds: dict, matcher: Matcher) -> tuple[list[Record], dict]:
    people = {}
    for d in _members_of(zf, "people").values():
        p = d.get("person") or {}
        if p.get("people_id"):
            people[p["people_id"]] = p
    bills = {}
    for d in _members_of(zf, "bill").values():
        b = d.get("bill") or {}
        if b.get("bill_id"):
            bills[b["bill_id"]] = b
    pid = {k: matcher.member(p) for k, p in people.items()}
    stats = {"roll_calls": 0, "people": len(people), "people_matched": sum(1 for v in pid.values() if v), "bills_matched": 0}
    matched_bills = set()
    out = []
    session_name = ds.get("session_name") or ds.get("session_title") or str(ds.get("session_id"))
    for d in _members_of(zf, "vote").values():
        rc = d.get("roll_call") or {}
        if not rc.get("roll_call_id"):
            continue
        b = bills.get(rc.get("bill_id")) or {}
        lid = matcher.legislation(b) if b else None
        if lid:
            matched_bills.add(lid)
        link = next((v.get("url") or v.get("state_link") for v in b.get("votes") or [] if v.get("roll_call_id") == rc["roll_call_id"]), None)
        url = link or b.get("state_link") or b.get("url") or ""
        if not url:
            continue                               # nothing to cite: never a link built here
        chamber = "Senate" if (rc.get("chamber") or "").upper().startswith("S") else "House"
        votes = []
        for v in rc.get("votes") or []:
            p = people.get(v.get("people_id")) or {}
            mid = pid.get(v.get("people_id"))
            party = (p.get("party") or "").upper()[:1]
            votes.append({"person": p.get("name") or "", "person_id": mid if mid is not None else f"ls:{v.get('people_id')}",
                          "vote": VOTE_TEXT.get(v.get("vote_text"), v.get("vote_text") or ""),
                          "party": party if party in ("R", "D", "I") else (matcher.party.get(mid) or ""), "legiscan_id": v.get("people_id")})
        reported = {"yea": rc.get("yea"), "nay": rc.get("nay"), "not_voting": rc.get("nv"), "excused": rc.get("absent")}
        desc = rc.get("desc") or ""
        number = b.get("bill_number") or ""
        out.append(Record(SOURCE, "vote", url, title=f"{number} — {desc}"[:300],
                          text=f"{number} ({session_name}) {b.get('title') or ''}\n{desc}: {rc.get('yea')}-{rc.get('nay')} (from {CREDIT})",
                          published=(rc.get("date") or "")[:10] or None, channel="api", jurisdiction="ga", evidence="primary",
                          key=f"{SOURCE}|vote|{rc['roll_call_id']}",
                          meta={"roll_call_id": rc["roll_call_id"], "legiscan_bill_id": rc.get("bill_id"), "legislation_id": lid,
                                "bill": re.sub(r"^([HS][BR])(\d)", r"\1 \2", number), "session_name": session_name,
                                "legiscan_session_id": ds.get("session_id"), "chamber": chamber, "date": (rc.get("date") or "")[:10],
                                "vote_name": f"{chamber} roll call", "vote_caption": desc, "yea": rc.get("yea"), "nay": rc.get("nay"),
                                "not_voting": rc.get("nv"), "excused": rc.get("absent"),
                                "result": "passed" if rc.get("passed") else "failed", "substantive": _substantive(desc),
                                "votes": votes, "check": check_roll_call(reported, votes, chamber), "credit": CREDIT,
                                "opened": True, "opened_url": f"{API}?op=getDataset&id={ds.get('session_id')}"}))
        stats["roll_calls"] += 1
    stats["bills_matched"] = len(matched_bills)
    return out, stats


# ----------------------------------------------------------------------------- the run
def update(fetcher, store: Store, deadline=None, log=print, key: str | None = None) -> dict:
    """The GA dataset list; every dataset whose hash changed since last read is downloaded and stored."""
    import time

    try:
        api = LegiScan(fetcher, key, log)
    except NoKey:
        return {"skipped": "no LEGISCAN_API_KEY secret: LegiScan not read (a free key from legiscan.com/legiscan)"}
    out = {"datasets": 0, "changed": 0, "roll_calls": 0, "new_or_changed": 0, "sessions": []}
    try:
        lists = api.datasets()
        out["datasets"] = len(lists)
        matcher = None
        for ds in sorted(lists, key=lambda x: -int(x.get("year_start") or 0)):
            if deadline is not None and time.monotonic() > deadline:
                out["stopped"] = "out of time; the rest next run"
                break
            mark = f"legiscan:dataset:{ds['session_id']}"
            if store.kv_get(mark) == ds.get("dataset_hash"):
                continue
            matcher = matcher or Matcher(store)
            recs, st = records_from(api.dataset(ds), ds, matcher)
            n = sum(1 for r in recs if store.add(r))
            store.flush()
            store.kv_set(mark, ds.get("dataset_hash") or "")
            store.kv_set(f"legiscan:read:{ds['session_id']}", dt.date.today().isoformat())
            out["changed"] += 1
            out["roll_calls"] += st["roll_calls"]
            out["new_or_changed"] += n
            out["sessions"].append({"session": ds.get("session_name"), **st, "stored": n})
            log(f"  LegiScan {ds.get('session_name')}: {st['roll_calls']} roll calls, {st['people_matched']}/{st['people']} legislators matched")
    except Exception as exc:  # noqa: BLE001
        out["error"] = redact(f"{type(exc).__name__}: {exc}", api.key)[:300]
    out["calls"] = api.calls
    return out
