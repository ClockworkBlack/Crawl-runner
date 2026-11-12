"""The web lane of candidate research: the trusted outlets' own archives and one web search per candidate, kept only
when the page names the person and is tied to their place, and only from sites the project already trusts."""
import datetime as dt
import json
import time

from gacrawl.candidates import Researcher
from gacrawl.scheduler import Progress
from gacrawl.store import Store
from gacrawl.webresearch import WebLane, clean_office, office_words, search_query


def test_office_is_cleaned_for_a_search_box():
    assert clean_office("State Representative, District 33; State House District 33 (elected Nov. 3, 2026)") == "State Representative District 33"
    assert clean_office("Council Member - Dist 3 - Social Circle (elected 2025-11-04)") == "Council Member Dist 3 Social Circle"
    assert search_query("Adam Thomas", "Mayor - Baxley (elected 2025-11-04)", "Baxley") == '"Adam Thomas" Mayor Baxley Georgia'
    assert search_query("Jane Roe", "", "Cobb County") == '"Jane Roe" Cobb County Georgia'
    assert office_words("City Council - District 1") == ["council"]


class R:
    def __init__(self, body, status=200, ctype="application/json", url=""):
        self.content = body if isinstance(body, bytes) else body.encode()
        self.status, self.content_type, self.url = status, ctype, url
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.text)


class F:
    user_agent = "t"

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get(self, url, params=None, **kw):
        self.asked.append((url, params))
        for k, v in self.answers.items():
            if k in url:
                return R(json.dumps(v)) if not isinstance(v, R) else v
        raise RuntimeError("no network in this test")


class FakeSearch:
    def __init__(self, hits, cap=5):
        self.hits, self.st, self.n, self.cap, self.saved = hits, {"cache": {}}, 0, cap, False

    def left(self):
        return self.cap - self.n

    def search(self, q, count=8):
        self.n += 1
        self.q = q
        return self.hits

    def save(self):
        self.saved = True


def setup(tmp_path, fetcher, search=None, opts=None):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text(
        "counties: []\nmunicipalities:\n  - {id: testville, name: Testville, kind: city, counties: [Test]}\n")
    (tmp_path / "config" / "outlets.yaml").write_text(
        "outlets: [testvilletimes.com, statewide.example]\nexclude_domains: [ballotpedia.org]\n"
        "statewide: [statewide.example]\ncounty_outlets:\n  test: [testvilletimes.com]\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "wp_outlets.json").write_text(json.dumps({
        "testvilletimes.com": {"api": "https://testvilletimes.com/wp-json/"}, "statewide.example": {"api": "https://statewide.example/wp-json/"},
        "nopapi.example": {"api": None}}))
    store = Store(tmp_path / "data")
    r = Researcher(tmp_path, {"candidates": {"gdelt": False, "google_news": False, "bluesky_search": False, **(opts or {})},
                              "state_dir": tmp_path / "state"}, fetcher, store, log=lambda *_: None, search=search)
    return r, store


JANE = {"id": "jane", "name": "Jane Roe", "office": "City Council - District 2", "jurisdiction": "testville",
        "jurisdictions": ["testville"], "counties": [], "aliases": [], "websites": []}


def post(link, title, body, date="2019-05-02T10:00:00"):
    return {"link": link, "date": date, "title": {"rendered": title}, "content": {"rendered": f"<p>{body}</p>"}}


def test_outlet_archive_keeps_stories_about_them_only(tmp_path):
    f = F({"testvilletimes.com/wp-json": [
        post("https://testvilletimes.com/a", "Council votes", "Councilwoman Jane Roe moved to approve the Testville budget."),
        post("https://testvilletimes.com/b", "Roe on zoning", "Jane Roe told the council the rezoning should wait."),   # no place: the local paper + her office
        post("https://testvilletimes.com/c", "Obituaries", "Jane Roe, 91, loved her garden."),                        # same name, nothing ties it to her
        post("https://elsewhere.example/d", "Syndicated", "Jane Roe of Testville council."),                          # not on the outlet's own site
        post("https://testvilletimes.com/e", "Football", "The Testville team won.")]})                                # no name
    r, store = setup(tmp_path, f)
    lane = WebLane(r, tmp_path / "state")
    apis, local = lane.apis_for(JANE)
    assert list(apis) == ["testvilletimes.com"] and local == {"testvilletimes.com"}      # local paper; no statewide for a council seat
    got = []
    n = lane.outlet_archive(dict(JANE), got.append, "2016-01-01")
    assert n == 2 and sorted(x.url for x in got) == ["https://testvilletimes.com/a", "https://testvilletimes.com/b"]
    assert all(x.kind == "article" and x.evidence == "reported" and x.meta["via"] == "outlet_archive" for x in got)
    assert got[0].published == "2019-05-02"                     # years back, not the news index's three months
    url, params = f.asked[0]
    assert params["search"] == '"Jane Roe"' and params["after"].startswith("2016-01-01")
    store.close()


def test_statewide_outlets_for_state_offices_and_people_with_no_local_paper(tmp_path):
    r, store = setup(tmp_path, F({}))
    lane = WebLane(r, tmp_path / "state")
    rep = {"id": "rep", "name": "Al Poe", "office": "State Representative, District 9", "jurisdictions": ["testville"], "counties": []}
    assert list(lane.apis_for(rep)[0]) == ["testvilletimes.com", "statewide.example"]
    nowhere = {"id": "n", "name": "Bo Doe", "office": "Coroner", "jurisdictions": [], "counties": ["Elsewhere"]}
    assert list(lane.apis_for(nowhere)[0]) == ["statewide.example"] and lane.apis_for(nowhere)[1] == set()
    store.close()


def test_web_search_opens_trusted_sites_only(tmp_path, monkeypatch):
    hits = [{"url": "https://ballotpedia.org/Jane_Roe", "title": "Jane Roe", "snippet": "Jane Roe ran"},                 # excluded
            {"url": "https://randomblog.example/jane", "title": "Jane Roe is great", "snippet": "Jane Roe"},              # not trusted: left alone
            {"url": "https://statewide.example/story", "title": "Testville council race", "snippet": "Jane Roe won"},     # listed outlet
            {"url": "https://www.testville.gov/council", "title": "City Council", "snippet": "Testville council members"},  # official
            {"url": "https://statewide.example/other", "title": "Weather", "snippet": "Rain on Tuesday"}]                 # nothing about her
    s = FakeSearch(hits)
    r, store = setup(tmp_path, F({}), s)
    opened = []

    def open_article(self, url):
        opened.append(url)
        if "story" in url:
            return {"url": url, "title": "Race", "text": "Jane Roe won the District 2 seat on the Testville City Council."}
        return {"url": url, "title": "Council", "text": "District 2: Jane Roe. Meetings are on Mondays."}   # names her district
    monkeypatch.setattr(Researcher, "_open_article", open_article)
    lane = WebLane(r, tmp_path / "state", s)
    c = dict(JANE)
    got = []
    res = lane.one(c, r._classifier(c), got.append, "2016-01-01")
    # another city's official site with a council member of the same name: her place is not on it, so it is not hers
    assert not lane._about(c, "Council member Jane Roe chairs the finance committee.", __import__("re").compile("Jane Roe"), False)
    assert s.q == '"Jane Roe" City Council District 2 Testville Georgia'
    assert opened == ["https://statewide.example/story", "https://www.testville.gov/council"]
    assert sorted((x.url, x.evidence) for x in got) == [("https://statewide.example/story", "reported"),
                                                         ("https://www.testville.gov/council", "primary")]
    assert res["web"] == 2 and res["search"] == "ok" and all(x.meta["via"] == "web_search" for x in got)
    store.close()


def test_no_search_left_is_owed_not_done(tmp_path):
    s = FakeSearch([], cap=0)
    r, store = setup(tmp_path, F({}), s)
    lane = WebLane(r, tmp_path / "state", s)
    c = dict(JANE)
    res = lane.one(c, r._classifier(c), lambda rec: None, "2016-01-01")
    assert res["search"] == "skipped" and s.n == 0
    # due again after a day for the search alone; a whole look-up only after the cadence
    prog = Progress(tmp_path / "cp.json")
    now = dt.datetime(2026, 11, 12, tzinfo=dt.timezone.utc)
    prog.update("web:jane", last_ok=(now - dt.timedelta(days=2)).isoformat(), search="skipped")
    prog.update("web:done", last_ok=(now - dt.timedelta(days=2)).isoformat(), search="ok")
    cands = [dict(JANE), {**JANE, "id": "done"}, {**JANE, "id": "new"}, {**JANE, "id": "full"}]
    prog.update("cand:full", last_ok=now.isoformat(), counts={"mention": 9})
    s.cap = 5
    due = [(c["id"], only) for c, only in lane.due(cands, prog, now)]
    assert due == [("new", False), ("full", False), ("jane", True)]      # nothing on file first; the owed search last
    store.close()


def test_research_many_runs_the_web_lane(tmp_path, monkeypatch):
    f = F({"testvilletimes.com/wp-json": [post("https://testvilletimes.com/a", "Council", "Jane Roe of Testville spoke.")]})
    s = FakeSearch([])
    r, store = setup(tmp_path, f, s)
    for name in ("_mentions", "_votes", "_sponsorships", "_posts", "_statements", "_leads", "_social_mentions", "_connections"):
        monkeypatch.setattr(Researcher, name, lambda self, c, cls, add, since: None)
    prog = Progress(tmp_path / "cp.json")
    res = r.research_many([dict(JANE)], "2016-01-01", time.monotonic() + 30, prog)
    assert res["web"]["candidates"] == 1 and res["web"]["due"] == 1 and res["web"]["added"] == 1 and res["added"]["article"] == 1
    p = prog.get("web:jane")
    assert p["last_ok"] and p["search"] == "ok" and p["counts"] == {"article": 1} and s.saved
    res2 = r.research_many([dict(JANE)], "2016-01-01", time.monotonic() + 30, prog)
    assert res2["web"]["due"] == 0                            # not asked again inside the cadence
    assert r.research_many([{**JANE, "id": "x"}], "2016-01-01", time.monotonic() + 30, prog, skip={"web"})["web"]["due"] == 0
    store.close()
