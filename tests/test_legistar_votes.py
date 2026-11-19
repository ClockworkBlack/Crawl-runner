"""Legistar roll calls from office holders' votes (one paged call per person) instead of one call per agenda item;
deltas merge changed votes into stored roll calls and refresh only stale or changed meetings."""
import datetime as dt
import json

from gacrawl import channels
from gacrawl.sources.legistar import Legistar
from gacrawl.store import Store

TODAY = dt.date.today()
OLD = (TODAY - dt.timedelta(days=400)).isoformat()
RECENT = (TODAY - dt.timedelta(days=10)).isoformat()


class Res:
    def __init__(self, obj, status=200):
        self.status, self._b = status, json.dumps(obj).encode()
        self.url, self.headers, self.content_type = "", {}, "application/json"

    def json(self):
        return json.loads(self._b)

    @property
    def text(self):
        return self._b.decode()


class Api:
    """A tiny Legistar: 2 meetings x 3 items, 3 council members with office records."""

    def __init__(self):
        self.calls = []
        self.events = [{"EventId": 1, "EventBodyName": "City Council", "EventDate": OLD + "T00:00:00", "EventLastModifiedUtc": "2025-01-01T00:00:00",
                        "EventInSiteURL": "https://x.legistar.com/MeetingDetail.aspx?ID=1"},
                       {"EventId": 2, "EventBodyName": "City Council", "EventDate": RECENT + "T00:00:00", "EventLastModifiedUtc": "2026-01-01T00:00:00",
                        "EventInSiteURL": "https://x.legistar.com/MeetingDetail.aspx?ID=2"}]
        self.all_events = list(self.events)
        self.items = {1: [{"EventItemId": 11, "EventItemTitle": "Rezoning A", "EventItemPassedFlagName": "Pass"},
                          {"EventItemId": 12, "EventItemTitle": "Budget", "EventItemPassedFlagName": "Pass"},
                          {"EventItemId": 13, "EventItemTitle": "Pledge"}],
                      2: [{"EventItemId": 21, "EventItemTitle": "Data center ordinance", "EventItemPassedFlagName": "Pass"},
                          {"EventItemId": 22, "EventItemTitle": "Alcohol license", "EventItemPassedFlagName": "Fail"},
                          {"EventItemId": 23, "EventItemTitle": "Adjourn"}]}
        self.people = {101: "Pat Local", 102: "Kim Post", 103: "Lee Ward"}
        self.votes = {101: [(11, "Yea"), (12, "Yea"), (21, "Yea"), (22, "Nay")],
                      102: [(11, "Yea"), (12, "Nay"), (21, "Nay"), (22, "Nay")],
                      103: [(11, "Nay"), (12, "Yea"), (21, "Yea"), (22, "Yea")]}
        self.modified = {}       # (person, item) -> VoteLastModifiedUtc

    def get(self, url, params=None, headers=None, use_cache=True):
        params = params or {}
        path = url.split("/v1/x", 1)[1]
        self.calls.append(path)
        if params.get("$skip"):
            return Res([])
        if path == "/events":
            return Res(self.events)
        if path.startswith("/events/") and path.endswith("/eventitems"):
            return Res(self.items[int(path.split("/")[2])])
        if path.startswith("/events/"):
            return Res(next(e for e in self.all_events if e["EventId"] == int(path.split("/")[2])))
        if path == "/officerecords":
            return Res([{"OfficeRecordId": 900 + p, "OfficeRecordPersonId": p, "OfficeRecordFullName": n, "OfficeRecordBodyName": "City Council",
                         "OfficeRecordTitle": "Council Member"} for p, n in self.people.items()])
        if path.startswith("/persons/") and path.endswith("/votes"):
            pid = int(path.split("/")[2])
            flt = params.get("$filter", "")
            since = flt.split("datetime'")[1][:19] if "datetime'" in flt else ""
            rows = [{"VoteId": pid * 100 + iid, "VotePersonId": pid, "VotePersonName": self.people[pid], "VoteValueName": v,
                     "VoteEventItemId": iid, "VoteSort": pid, "VoteLastModifiedUtc": self.modified.get((pid, iid), "2025-06-01T00:00:00")}
                    for iid, v in self.votes[pid]]
            return Res([r for r in rows if not since or r["VoteLastModifiedUtc"] >= since])
        if path.startswith("/eventitems/") and path.endswith("/votes"):
            iid = int(path.split("/")[2])
            return Res([{"VotePersonId": p, "VotePersonName": self.people[p], "VoteValueName": v, "VoteSort": p}
                        for p, vs in self.votes.items() for i, v in vs if i == iid])
        return Res([], 404)


def crawl(api, store, modified_since=None, votes_mode="person"):
    lg = Legistar(api, "x")
    out = []
    for r in lg.officials():
        out.append(r)
    for r in lg.meetings("2016-01-01", modified_since=modified_since, store=store, source_id="x:legistar", votes_mode=votes_mode):
        if not isinstance(r, channels.Checkpoint):
            r.source_id = "x:legistar"
            out.append(r)
    for r in out:
        store.add(r)
    store.flush()
    return out


def test_full_run_builds_roll_calls_from_person_votes(tmp_path):
    api = Api()
    store = Store(tmp_path / "data")
    recs = crawl(api, store)
    votes = {r.meta["EventItemId"]: r for r in recs if r.kind == "vote"}
    assert set(votes) == {11, 12, 21, 22}                                   # items with roll calls; none for Pledge/Adjourn
    assert [(v["person"], v["vote"]) for v in votes[11].meta["votes"]] == [("Pat Local", "Yea"), ("Kim Post", "Yea"), ("Lee Ward", "Nay")]
    assert votes[22].meta["result"] == "Fail" and votes[21].url == "https://x.legistar.com/MeetingDetail.aspx?ID=2"
    assert votes[11].meta["opened_url"].endswith("/persons/101/votes")
    assert not any(c.startswith("/eventitems/") for c in api.calls)        # no per-item vote calls
    assert sum(c.startswith("/persons/") for c in api.calls) == 3 and api.calls.count("/officerecords") == 1
    assert len([r for r in recs if r.kind == "official"]) == 3
    # the same roll calls as the per-item way
    api2 = Api()
    store2 = Store(tmp_path / "data2")
    per_item = {r.meta["EventItemId"]: r.meta["votes"] for r in crawl(api2, store2, votes_mode="item") if r.kind == "vote"}
    assert per_item == {k: v.meta["votes"] for k, v in votes.items()}
    assert sum(c.startswith("/eventitems/") for c in api2.calls) == 4
    store.close()
    store2.close()


def test_delta_merges_changed_votes_and_skips_unchanged_meetings(tmp_path):
    api = Api()
    store = Store(tmp_path / "data")
    crawl(api, store)
    # next night: nothing changed. The old meeting is outside the lookback; the recent one's items were just read
    api.calls.clear()
    api.events = [e for e in api.events if e["EventId"] == 2]               # the API's delta filter returns only the recent one
    crawl(api, store, modified_since="2026-06-01T00:00:00")
    assert not any(c.endswith("/eventitems") for c in api.calls)           # items read today already: not re-read
    assert not any(c.startswith("/eventitems/") for c in api.calls)
    # Kim Post's vote on item 12 (old meeting) is corrected: only her changed vote comes back, merged into the roll call
    api.calls.clear()
    api.votes[102] = [(11, "Yea"), (12, "Yea"), (21, "Nay"), (22, "Nay")]
    api.modified[(102, 12)] = "2026-07-01T00:00:00"
    crawl(api, store, modified_since="2026-06-15T00:00:00")
    v12 = next(r for r in store.query("kind='vote'") if r["meta"]["EventItemId"] == 12)
    assert {x["person"]: x["vote"] for x in v12["meta"]["votes"]} == {"Pat Local": "Yea", "Kim Post": "Yea", "Lee Ward": "Yea"}
    assert "/events/1/eventitems" in api.calls                             # the affected meeting's agenda was refreshed once
    assert sum(c.startswith("/persons/") for c in api.calls) == 3
    store.close()


def test_fallback_without_office_records(tmp_path):
    api = Api()
    api.people_backup = api.people
    store = Store(tmp_path / "data")
    orig = api.get

    def no_office(url, params=None, headers=None, use_cache=True):
        if url.endswith("/officerecords"):
            api.calls.append("/officerecords")
            return Res([])
        return orig(url, params, headers, use_cache)

    api.get = no_office
    recs = crawl(api, store)
    assert {r.meta["EventItemId"] for r in recs if r.kind == "vote"} == {11, 12, 21, 22}
    assert sum(c.startswith("/eventitems/") for c in api.calls) == 4       # read per passed item instead
    store.close()


def test_a_first_pass_stopped_in_the_vote_sweep_carries_on_with_the_next_person(tmp_path, monkeypatch):
    """DeKalb County and Powder Springs, Nov. 7-18, 2026: about 300 passes each began the sweep at the first office
    holder, ran out of time before the last, stored no vote and never became a delta."""
    from gacrawl.fetch import OutOfTime
    from gacrawl.sources import legistar

    monkeypatch.setattr(legistar, "SWEEP_STEP", 1)
    api, store = Api(), Store(tmp_path)
    real_get, budget = api.get, {"left": 0}

    def limited(url, params=None, **kw):
        if "/persons/" in url:
            if budget["left"] <= 0:
                raise OutOfTime("this source's time slice used up")
            budget["left"] -= 1
        return real_get(url, params=params, **kw)

    api.get = limited

    def one_pass(cursors):
        lg, marks = Legistar(api, "x"), dict(cursors)
        try:
            for r in lg.meetings("2016-01-01", store=store, source_id="x:legistar", cursors=cursors):
                if isinstance(r, channels.Checkpoint):
                    marks[r.stream] = r.value
                else:
                    r.source_id = "x:legistar"
                    store.add(r)
        except OutOfTime:
            store.flush()
            return marks, False
        store.flush()
        return marks, True

    budget["left"] = 1          # one request reads one person's votes here: one person a pass
    marks, done = one_pass({})
    assert not done and marks["votes_person"] == "101"
    # the walk's marks are never past today (a resumed pass used to start at "tomorrow" and at next year)
    assert marks["events"] <= TODAY.isoformat()
    votes = {r["meta"]["EventItemId"]: r for r in store.query("kind='vote'")}
    assert set(votes) == {11, 12, 21, 22} and [v["person"] for v in votes[11]["meta"]["votes"]] == ["Pat Local"]
    budget["left"] = 1
    marks, done = one_pass(marks)
    assert not done and marks["votes_person"] == "102"
    budget["left"] = 99
    marks, done = one_pass(marks)
    assert done
    votes = {r["meta"]["EventItemId"]: r for r in store.query("kind='vote'")}
    assert {v["person"]: v["vote"] for v in votes[12]["meta"]["votes"]} == {"Pat Local": "Yea", "Kim Post": "Nay", "Lee Ward": "Yea"}
    assert all(len(v["meta"]["votes"]) == 3 for v in votes.values())
    store.close()


def test_a_meetings_stamp_is_stored_after_its_items_are_read(tmp_path):
    from gacrawl.fetch import OutOfTime

    api, store = Api(), Store(tmp_path)
    crawl(api, store)
    api.events[1]["EventLastModifiedUtc"] = "2026-11-01T00:00:00"      # the recent meeting changed
    real_get = api.get

    def no_items(url, params=None, **kw):
        if url.endswith("/eventitems"):
            raise OutOfTime("this source's time slice used up")
        return real_get(url, params=params, **kw)

    api.get = no_items
    lg, got = Legistar(api, "x"), []
    try:
        for r in lg.meetings("2016-01-01", modified_since="2026-10-01T00:00:00", store=store, source_id="x:legistar"):
            got.append(r)
    except OutOfTime:
        pass
    # nothing says the changed meeting was refreshed: the next run reads its items
    assert not [r for r in got if r.kind == "meeting" and r.meta.get("EventId") == 2]
    store.close()
