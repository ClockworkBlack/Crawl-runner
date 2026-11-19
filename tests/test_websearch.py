"""Web search (gacrawl/websearch.py): an engine that answers nothing is not "nothing found".

From Nov. 13 to 18, 2026 every search came back empty (DuckDuckGo alone, from the runner's address); each was kept for
30 days as an answer, no county election report was found (0 of 159, tries used up) and candidates' web look-ups were
marked done. These tests use a made-up engine in place of ddgs."""
import datetime as dt
import json

import pytest

from gacrawl import election_reports as er
from gacrawl import websearch
from gacrawl.store import Store


class NoResults(Exception):
    def __init__(self):
        super().__init__("No results found.")


def engine(answers, asked):
    """A stand-in for ddgs.DDGS: answers(query, backend) -> list of {href, title, body}, or raises."""
    class D:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, backend=None, **kw):
            asked.append((q, backend))
            r = answers(q, backend)
            if not r:
                raise NoResults()
            return r
    return D


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(websearch, "MIN_GAP", 0)
    monkeypatch.setattr(websearch.time, "sleep", lambda s: None)


def make(tmp_path, answers, asked, cap=50):
    s = websearch.Search(tmp_path, log=lambda *a: asked.append(("log", " ".join(map(str, a)))), cap=cap)
    s._DDGS = engine(answers, asked)
    return s


HIT = [{"href": "https://www.dekalbcountyga.gov/a.pdf", "title": "DeKalb County results", "body": "summary report"}]


def test_an_engine_that_answers_nothing_is_a_refusal_not_an_answer(tmp_path, quick):
    asked = []
    s = make(tmp_path, lambda q, be: [], asked)
    for i in range(3):
        assert s.search(f'"some county" report {i}') == []
    # three empty answers and the control search empty too: refused; nothing kept, nothing settled, no more searches
    assert s.refused() and s.left() == 0
    assert not s.st["cache"] and not s.settled('"some county" report 0')
    assert (websearch.CANARY, websearch.BACKEND) in asked
    n = len(asked)
    assert s.search('"another county" report') == [] and len([a for a in asked if a[0] != "log"]) == len([a for a in asked[:n] if a[0] != "log"])
    s.save()
    assert any("::warning title=Web search::" in a[1] for a in asked if a[0] == "log")
    assert json.loads((tmp_path / "websearch.json").read_text())["cache"] == {}


def test_empty_answers_are_believed_once_the_engine_shows_it_answers(tmp_path, quick):
    asked = []
    s = make(tmp_path, lambda q, be: HIT if "dekalb" in q.lower() or q == websearch.CANARY else [], asked)
    assert s.search('"nowhere county" report') == [] and not s.settled('"nowhere county" report')   # held back
    assert s.search('"DeKalb County" report')                                                       # found: alive
    assert s.settled('"nowhere county" report') and s.settled('"DeKalb County" report')
    assert s.search('"elsewhere county" report') == [] and s.settled('"elsewhere county" report')   # believed at once
    s.save()
    kept = json.loads((tmp_path / "websearch.json").read_text())["cache"]
    assert kept['"nowhere county" report'] == {"at": dt.date.today().isoformat(), "results": [], "v": websearch.CACHE_V}
    # a new run answers all three from the cache
    again = []
    s2 = make(tmp_path, lambda q, be: 1 / 0, again)
    assert s2.search('"nowhere county" report') == [] and s2.search('"DeKalb County" report') and not again


def test_the_control_search_tells_a_quiet_engine_from_a_silent_one(tmp_path, quick):
    asked = []
    s = make(tmp_path, lambda q, be: HIT if q == websearch.CANARY else [], asked)
    for i in range(3):
        s.search(f'"tiny town {i}" report')
    # the control search had results: the three empty answers were answers
    assert not s.refused() and s.left() > 0 and all(s.settled(f'"tiny town {i}" report') for i in range(3))


def test_a_refusal_part_way_through_a_run_takes_back_its_empty_answers(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(websearch, "EMPTY_STOP", 4)
    asked, alive = [], {"on": True}
    s = make(tmp_path, lambda q, be: HIT if alive["on"] and ("dekalb" in q.lower() or q == websearch.CANARY) else [], asked)
    assert s.search('"DeKalb County" one')
    alive["on"] = False                 # the engine starts turning the runner away
    for i in range(4):
        s.search(f'"county {i}" report')
    assert s.refused() and s.left() == 0
    assert not any(s.settled(f'"county {i}" report') for i in range(4)) and s.settled('"DeKalb County" one')


def test_an_empty_answer_kept_by_the_old_code_is_asked_again(tmp_path, quick):
    today = dt.date.today().isoformat()
    (tmp_path / "websearch.json").write_text(json.dumps({"months": {}, "cache": {
        "old empty": {"at": today, "results": []},
        "old found": {"at": today, "results": [{"url": "https://x.gov/a", "title": "t", "snippet": ""}]}}}))
    s = websearch.Search(tmp_path, log=lambda *a: None)
    assert "old empty" not in s.st["cache"] and s.settled("old found") and not s.settled("old empty")


def test_other_engines_are_asked_when_the_first_finds_nothing_and_must_carry_the_quoted_words(tmp_path, quick):
    asked = []

    def answers(q, be):
        if be == websearch.BACKEND:
            return []
        if be == "brave":       # ignores the quotes: one result about someone else, one about her
            return [{"href": "https://example.com/adam", "title": "Adam of Genesis", "body": "a film called Adam"},
                    {"href": "https://www.cantonga.gov/council", "title": "Council Member Jane Doe", "body": "Canton"}]
        return []

    s = make(tmp_path, answers, asked)
    res = s.search('"Jane Doe" Canton city council')
    assert [r["url"] for r in res] == ["https://www.cantonga.gov/council"]
    assert s.settled('"Jane Doe" Canton city council')
    assert websearch.carries_phrases('"Jane Doe" Canton', {"title": "JANE DOE wins", "snippet": "", "url": ""})
    assert not websearch.carries_phrases('"Jane Doe" "Canton"', {"title": "Jane Doe wins", "snippet": "", "url": ""})


def test_a_county_is_not_written_off_when_the_search_was_never_answered(tmp_path, quick, monkeypatch):
    from gacrawl import config

    asked = []
    monkeypatch.setattr(websearch.Search, "__init__", _init_with(engine(lambda q, be: [], asked)))
    store = Store(tmp_path / "data")
    out = er.run(None, store, config.ROOT, tmp_path / "state", counties=["co-dekalb"], log=lambda *a: None)
    assert out["search_refused"] and out["not_found"] == 0 and out["unanswered"] >= 1
    assert store.kv_get("elections:report:co-dekalb:2023-11-07") is None
    st = er.status(store, config.ROOT)
    assert st["2023-11-07"]["not_searched"] == 159 and st["2023-11-07"]["not_found"] == 0
    store.close()


def test_tries_counted_before_the_check_are_not_believed(tmp_path):
    from gacrawl import config

    store = Store(tmp_path / "data")
    store.kv_set("elections:report:co-dekalb:2023-11-07", json.dumps({"at": "2026-11-17", "tries": 3}))
    store.kv_set("elections:report:co-fulton:2023-11-07", json.dumps({"at": "2026-11-17", "tries": 1, "url": "https://x.gov/r.pdf"}))
    store.kv_set("elections:report:co-cobb:2023-11-07", json.dumps({"at": "2026-11-17", "tries": 2, "partial": ["https://c.gov/p.pdf"]}))
    store.kv_set("elections:report:co-gwinnett:2023-11-07", json.dumps({"at": "2026-11-19", "tries": 1, "v": er.STATE_V}))
    assert er._state(store.kv_get("elections:report:co-dekalb:2023-11-07")) == {}
    assert er._state(store.kv_get("elections:report:co-cobb:2023-11-07")) == {"partial": ["https://c.gov/p.pdf"]}
    st = er.status(store, config.ROOT)["2023-11-07"]
    assert st["found"] == 1 and st["missing"] == ["co-gwinnett"] and st["not_searched"] == 157
    store.close()


def _init_with(DDGS):
    real = websearch.Search.__init__

    def init(self, *a, **k):
        real(self, *a, **k)
        self._DDGS = DDGS
    return init


def test_the_coverage_report_says_when_search_stopped_answering(tmp_path, quick):
    asked = []
    s = make(tmp_path, lambda q, be: [], asked)
    for i in range(3):
        s.search(f'"some county" report {i}')
    s.save()
    line, bad = websearch.health_line(tmp_path)
    assert bad and "not answering" in line and "last found something **never**" in line
    s2 = make(tmp_path, lambda q, be: HIT, asked)
    s2.search('"DeKalb County" report')
    s2.save()
    line, bad = websearch.health_line(tmp_path)
    assert not bad and dt.date.today().isoformat() in line
