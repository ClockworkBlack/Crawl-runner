"""CivicClerk: the server pages 15 events at a time whatever $top asks for (a real deployment listed 577), files
come embedded in each event, a first pass walks year windows with a checkpoint after each, later passes re-read
recent meetings and anything created since, and a file opened before is not fetched again."""
import json
import re
from urllib.parse import parse_qs, urlsplit

from gacrawl.channels import Checkpoint
from gacrawl.sources.civicclerk import FULL_PASS, CivicClerk, _year_windows
from gacrawl.store import Store

EVENTS = [{"id": i, "eventName": "City Council Meeting", "startDateTime": f"{2016 + i // 12}-{i % 12 + 1:02d}-10T18:00:00Z",
           "createdOn": f"{2016 + i // 12}-{i % 12 + 1:02d}-01T10:47:48.337Z", "isPublished": "Published", "categoryName": "City Council",
           "eventLocation": {"id": 1, "address1": "City Hall", "address2": None, "city": "Kingsland", "state": "GA", "zipCode": "31548"}, "hasAgenda": True, "isDeleted": False,
           "publishedFiles": [{"id": 0, "fileId": 1000 + i, "type": "Minutes", "name": f"Minutes {i}",
                               "url": f"stream/KINGSLANDGA/{i}.pdf", "publishOn": "2026-01-01T00:00:00Z"}]}
          for i in range(40)]            # 2016-01 .. 2019-04
EVENTS.append({**EVENTS[0], "id": 99, "isDeleted": True})
LATE = {**EVENTS[5], "id": 77, "startDateTime": "2017-02-20T18:00:00Z", "createdOn": "2019-04-25T09:00:00Z",
        "publishedFiles": []}     # an old meeting entered into the portal long after it happened


class Res:
    def __init__(self, status, body, ctype):
        self.status, self.content, self.content_type, self.url = status, body, ctype, ""
        self.text = body.decode()

    def json(self):
        return json.loads(self.text)


class FakeAPI:
    """Pages 15 at a time, honours $skip, $filter on startDateTime/createdOn (ge/lt/and/or), $count, $orderby."""
    page = 15

    def __init__(self):
        self.calls = []
        self.files = 0

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, dict(params or {})))
        u = urlsplit(url)
        if u.path.endswith(".pdf"):
            self.files += 1
            return Res(200, b"%PDF-1.4 minutes text", "application/pdf")
        assert u.path.endswith("/v1/Events"), url
        rows = [e for e in EVENTS if self._match(e, params.get("$filter"))]
        rows.sort(key=lambda e: e["startDateTime"], reverse="desc" in params.get("$orderby", ""))
        skip = int(params.get("$skip", 0))
        out = {"@odata.context": "x", "value": rows[skip: skip + min(self.page, int(params.get("$top", self.page)))]}
        if params.get("$count") == "true":
            out["@odata.count"] = len(rows)
        return Res(200, json.dumps(out).encode(), "application/json")

    @staticmethod
    def _match(e, flt):
        if not flt:
            return True
        def term(t):
            m = re.match(r"(\w+) (ge|lt) (\S+)", t.strip())
            f, op, v = m.groups()
            return (e[f] >= v) if op == "ge" else (e[f] < v)
        return any(all(term(t) for t in part.split(" and ")) for part in flt.split(" or "))


def test_pages_until_the_total_and_checkpoints_each_year(tmp_path):
    api = FakeAPI()
    store = Store(tmp_path / "data")
    cc = CivicClerk(api, "kingslandga")
    out = list(cc.meetings("2016-01-01", store=store))
    recs = [r for r in out if not isinstance(r, Checkpoint)]
    cps = [c for c in out if isinstance(c, Checkpoint)]
    meetings = [r for r in recs if r.kind == "meeting"]
    assert len(meetings) == 40 and all(m.meta["provider"] == "civicclerk" for m in meetings)   # the deleted one left out
    assert {m.meta["location"] for m in meetings} == {"City Hall, Kingsland, GA 31548"}     # the state, as the API gives it
    assert meetings[0].text.endswith("City Hall, Kingsland, GA 31548")
    docs = [r for r in recs if r.kind == "document"]
    assert len(docs) == 40 and docs[0].meta["doc_type"] == "minutes" and docs[0].title == "City Council Meeting: Minutes 0"
    assert [c.value for c in cps][:4] == ["2017-01-01", "2018-01-01", "2019-01-01", "2020-01-01"] and cps[0].stream == "events"
    assert (cps[-1].stream, cps[-1].value) == ("full", FULL_PASS)                              # done: later runs are deltas
    # 2016 has 12 events: a 15-a-page server answers them in one page; 2017 likewise; no page is asked for twice
    pages = [(p.get("$filter"), p.get("$skip")) for u, p in api.calls if u.endswith("/Events")]
    assert len(pages) == len(set(pages))
    # a second full pass opens no file again (the frontier remembers them) but still lists the meetings
    before = api.files
    out2 = [r for r in cc.meetings("2016-01-01", store=store) if not isinstance(r, Checkpoint)]
    assert api.files == before and len([r for r in out2 if r.kind == "meeting"]) == 40
    store.close()


def test_server_page_cap_is_paged_through():
    api = FakeAPI()
    cc = CivicClerk(api, "kingslandga")
    rows = list(cc._rows("/Events", None, "startDateTime desc", None))
    assert len(rows) == 41                                   # 15 + 15 + 11, not the first 15 only
    assert [p.get("$skip") for u, p in api.calls] == [0, 15, 30]
    assert api.calls[0][1].get("$count") == "true" and "$count" not in api.calls[1][1]
    assert cc.count("startDateTime ge 2019-01-01T00:00:00Z") == 4


def test_delta_rereads_recent_meetings_and_late_entries(monkeypatch):
    import datetime as dt

    api = FakeAPI()
    cc = CivicClerk(api, "kingslandga")
    EVENTS.append(LATE)
    try:
        class D(dt.date):
            @classmethod
            def today(cls):
                return cls(2019, 5, 1)
        monkeypatch.setattr(dt, "date", D)
        out = [r for r in cc.meetings("2016-01-01", modified_since="2019-04-20T00:00:00+00:00", lookback_days=45,
                                      cursors={"full": FULL_PASS}) if not isinstance(r, Checkpoint)]
        flt = api.calls[0][1]["$filter"]
        assert flt.startswith("startDateTime ge 2019-03-17T00:00:00Z or createdOn ge 2019-04-20T00:00:00Z"), flt
        assert {r.meta["event_id"] for r in out if r.kind == "meeting"} == {39, 77}   # April's meeting, and the late entry
        assert not any(isinstance(r, Checkpoint) for r in out)
    finally:
        EVENTS.remove(LATE)


def test_resumes_from_the_checkpoint_window():
    api = FakeAPI()
    cc = CivicClerk(api, "kingslandga")
    out = list(cc.meetings("2016-01-01", cursors={"events": "2019-01-01"}))
    ids = {r.meta["event_id"] for r in out if not isinstance(r, Checkpoint) and r.kind == "meeting"}
    assert ids == {36, 37, 38, 39}
    assert _year_windows("2018-06-01", "2020-03-01") == [("2018-06-01", "2019-01-01"), ("2019-01-01", "2020-01-01"), ("2020-01-01", "2020-03-01")]


def test_delta_falls_back_when_createdon_is_not_filterable(monkeypatch):
    import datetime as dt

    class Strict(FakeAPI):
        def get(self, url, params=None, headers=None, **kw):
            if "createdOn" in (params or {}).get("$filter", ""):
                self.calls.append((url, dict(params)))
                return Res(400, b'{"error":{"message":"not filterable"}}', "application/json")
            return super().get(url, params, headers, **kw)

    api = Strict()
    cc = CivicClerk(api, "kingslandga")

    class D(dt.date):
        @classmethod
        def today(cls):
            return cls(2019, 5, 1)
    monkeypatch.setattr(dt, "date", D)
    out = [r for r in cc.meetings("2016-01-01", modified_since="2019-04-20T00:00:00+00:00", lookback_days=45,
                                  cursors={"full": FULL_PASS}) if not isinstance(r, Checkpoint)]
    assert {r.meta["event_id"] for r in out if r.kind == "meeting"} == {39}
    assert api.calls[1][1]["$filter"] == "startDateTime ge 2019-03-17T00:00:00Z"


def test_a_portal_the_old_reader_marked_done_gets_the_full_pass():
    """The old reader saw 15 (future) meetings and the scheduler counted the source done (last_ok). Without this
    reader's `full` marker, a later run walks the portal from 2016 anyway; with it, the run is a delta."""
    api = FakeAPI()
    cc = CivicClerk(api, "kingslandga")
    out = list(cc.meetings("2016-01-01", modified_since="2026-11-06T04:07:00+00:00"))
    assert len([r for r in out if not isinstance(r, Checkpoint) and r.kind == "meeting"]) == 40
    assert ("full", FULL_PASS) in [(c.stream, c.value) for c in out if isinstance(c, Checkpoint)]


def test_finished_backfill_keeps_the_full_marker(tmp_path):
    store = Store(tmp_path / "data")
    store.set_cursor("kingsland:civicclerk", "events", "2027-01-01")
    store.set_cursor("kingsland:civicclerk", "full", FULL_PASS)
    store.clear_cursors("kingsland:civicclerk", keep=("full",))
    assert store.cursors("kingsland:civicclerk") == {"full": FULL_PASS}
    store.clear_cursors("kingsland:civicclerk")                     # --full forgets everything
    assert store.cursors("kingsland:civicclerk") == {}
    store.close()
