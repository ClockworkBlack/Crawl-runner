"""Getting more out of a short run: sources ordered by what a run should reach first, one source's time slice,
progress files written in batches, research several candidates at a time with news in the same chunk, campaign
sites read once and remembered, dead sites left alone, scanned pages deferred when they would hold a worker."""
import datetime as dt
import json
import threading
import time

from gacrawl import candidates as cand_mod
from gacrawl.candidates import Researcher, _chunks_by_place
from gacrawl.channels import ChannelCache, Probe
from gacrawl.scheduler import Progress, due
from gacrawl.store import Record, Store


def test_due_order_resume_wanted_tier_structured(tmp_path):
    prog = Progress(tmp_path / "progress.json")
    now = dt.datetime(2026, 11, 6, 4, 0, tzinfo=dt.timezone.utc)
    old = (now - dt.timedelta(days=9)).isoformat()
    prog.update("z-city:site", last_ok=old, status="ok")                               # overdue, plain pages
    prog.update("a-city:site", last_run=(now - dt.timedelta(hours=20)).isoformat(), status="partial (html full): 10 new / 10 seen (time slice)")
    prog.update("b-county:legistar", last_ok=old, status="ok")                         # overdue, structured
    prog.update("c-town:site", last_run=(now - dt.timedelta(hours=1)).isoformat(), status="blocked: x")   # just refused
    cache = ChannelCache(tmp_path / "channels.json")
    cache.put("b-county:legistar", Probe("api", True, 0.1, 5, "legistar:bcounty"), [], save=False)
    srcs = [{"id": "z-city:site", "tier": "a", "jurisdiction": "z-city"},
            {"id": "a-city:site", "tier": "b", "jurisdiction": "a-city"},
            {"id": "b-county:legistar", "tier": "a", "jurisdiction": "b-county"},
            {"id": "c-town:site", "tier": "c", "jurisdiction": "c-town"},
            {"id": "d-city:site", "tier": "a", "jurisdiction": "d-city"},               # never run
            {"id": "w-city:site", "tier": "a", "jurisdiction": "w-city"}]               # never run, wanted
    prog.update("w-city:site", status="")
    order = [s["id"] for s in due(srcs, prog, {"a": 2, "b": 7, "c": 30}, now=now, cache=cache, wanted={"w-city"})]
    assert order[0] == "a-city:site"                        # resumes first
    assert order.index("w-city:site") < order.index("d-city:site")   # wanted before the rest of the never-run
    assert order.index("b-county:legistar") < order.index("z-city:site")  # structured before pages at equal overdue
    assert "c-town:site" not in order                       # refused an hour ago: left alone


def test_progress_writes_in_batches(tmp_path):
    p = Progress(tmp_path / "p.json", write_every=60)
    for i in range(50):
        p.update(f"s{i}", status="ok")
    assert not (tmp_path / "p.json").exists() or len(json.loads((tmp_path / "p.json").read_text())) < 50
    p.flush()
    assert len(json.loads((tmp_path / "p.json").read_text())) == 50


def test_chunks_by_place_keep_order():
    order = [{"id": "1", "jurisdiction": "a"}, {"id": "2", "jurisdiction": "b"}, {"id": "3", "jurisdiction": "a"},
             {"id": "4", "counties": ["Cobb"]}, {"id": "5", "jurisdiction": "a"}, {"id": "6", "jurisdiction": "a"}]
    ch = _chunks_by_place(order, 3)
    assert [[c["id"] for c in x] for x in ch] == [["1", "3", "5"], ["2"], ["4"], ["6"]]


class FakeFetcher:
    user_agent = "t"

    def get(self, *a, **k):
        raise RuntimeError("no network in this test")


def test_research_many_runs_chunks_in_parallel_with_news(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text("counties: []\nmunicipalities: []\n")
    store = Store(tmp_path / "data")
    r = Researcher(tmp_path, {"candidates": {"workers": 3, "gdelt": True, "google_news": False, "bluesky_search": False}},
                   FakeFetcher(), store, log=lambda *_: None)
    seen, news, lock = [], [], threading.Lock()

    def fake_step(self, c, cls, add, since):
        time.sleep(0.05)
        with lock:
            seen.append((threading.get_ident(), c["id"]))
        add(Record("", "mention", f"https://x/{c['id']}", title="t", text="t", channel="store", evidence="primary"))
    monkeypatch.setattr(Researcher, "_mentions", fake_step)
    for name in ("_votes", "_sponsorships", "_posts", "_statements", "_leads", "_social_mentions", "_connections"):
        monkeypatch.setattr(Researcher, name, lambda self, c, cls, add, since: None)

    def fake_articles(self, group, since, adders):
        with lock:
            news.append((threading.current_thread().name, [c["id"] for c in group]))
        for c in group:
            adders[c["id"]](Record("", "article", f"https://news.example/{c['id']}", title="t", text="t", channel="api",
                                   evidence="primary", meta={"opened": True}))
        return len(group)
    monkeypatch.setattr(Researcher, "articles_batch", fake_articles)
    cands = [{"id": f"c{i}", "name": f"Person {i}", "jurisdiction": ["a", "b", "c"][i % 3], "aliases": []} for i in range(12)]
    prog = Progress(tmp_path / "cp.json")
    res = r.research_many(cands, "2016-01-01", time.monotonic() + 30, prog)
    assert res["researched"] == 12 and res["added"]["mention"] == 12 and res["added"]["article"] == 12
    assert len({t for t, _ in seen}) > 1                     # more than one thread did the work
    assert {t for t, _ in news} == {"news-lane"}             # news has its own lane, not the research workers
    assert sorted(x for _, g in news for x in g) == sorted(c["id"] for c in cands)   # every candidate got its news
    assert all(len({cands[int(x[1:])]["jurisdiction"] for x in g}) == 1 for _, g in news)  # one place per query
    assert res["news"] == {"queries": 3, "candidates": 12, "due": 12}
    assert all(prog.get(f"news:{c['id']}").get("last_ok") for c in cands)     # news progress kept per candidate
    # a second run: research is not due (cadence), and news is not due either
    res2 = r.research_many(cands, "2016-01-01", time.monotonic() + 30, prog)
    assert res2["researched"] == 0 and res2["news"]["due"] == 0
    store.close()


def test_statements_remember_the_way_in_and_dead_sites(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text("counties: []\nmunicipalities: []\n")
    store = Store(tmp_path / "data")
    r = Researcher(tmp_path, {"candidates": {}}, FakeFetcher(), store, log=lambda *_: None)
    probes_called = []

    def fake_probe(fetcher, src):
        probes_called.append(src["url"])
        if "gone" in src["url"]:
            return [Probe("html", False, 0.1, 0, src["url"], "HTTP 404")]
        return [Probe("html", True, 0.1, 1, src["url"], "")]
    monkeypatch.setattr(cand_mod.channels, "probe_source", fake_probe)
    crawled = []
    monkeypatch.setattr(cand_mod.channels, "crawl", lambda f, src, chosen, limits, ctx=None: iter(crawled.append(src["url"]) or []))
    c = {"id": "k", "name": "Kay", "websites": ["https://kay.example", "https://gone.example"], "aliases": []}
    cls = r._classifier(c)
    add = r._adder(c, cls, {})
    r._statements(c, cls, add, "2016-01-01")
    r._statements(c, cls, add, "2016-01-01")
    assert probes_called == ["https://kay.example", "https://gone.example"]   # probed once each, not per run
    assert crawled == ["https://kay.example", "https://kay.example"]         # the live site is read each time
    assert store.kv_get("dead_site:cand:k:site1")                             # the gone site is remembered
    store.close()


def test_pdf_scanned_pages_deferred_by_budget(monkeypatch):
    from gacrawl import ingest

    # a 3-page "scan": no text layer; OCR budget allows one page
    monkeypatch.setattr(ingest, "_pdf_pages", lambda data: (["", "", ""], lambda i: None))
    monkeypatch.setattr(ingest, "preprocess", lambda img: img)
    monkeypatch.setattr(ingest, "_lines_from_tesseract", lambda img: [{"block": 0, "par": 0, "text": "Minutes of the meeting", "conf": 90.0, "top": 0}])
    rec = ingest.pdf_to_record(b"%PDF-fake", url="https://x/m.pdf", ocr_max_pages=1)
    assert rec.meta["ocr_pages"] == [1] and rec.meta["ocr_pending"] == [2, 3] and rec.meta["ocr_truncated"]
    assert "Minutes of the meeting" in rec.text


def test_rate_limit_turn_past_deadline_is_not_taken():
    """Sixteen workers queued on one host used to sleep through the run's deadline; a turn that would come after
    the deadline now raises at once."""
    from gacrawl.fetch import Fetcher, OutOfTime
    from gacrawl.polite import RateLimiter, SlotPastDeadline

    lim = RateLimiter(min_interval=3.0, sleep=lambda _: None, clock=lambda: 100.0)
    lim.wait("https://api.example/a")                  # takes the turn at 100, next at 103
    with __import__("pytest").raises(SlotPastDeadline):
        lim.wait("https://api.example/b", until=101.0)   # the next turn (103) is past the deadline
    assert lim.wait("https://api.example/c", until=200.0) == 3.0


def test_browser_crawl_checks_the_deadline(monkeypatch, tmp_path):
    from gacrawl import channels
    from gacrawl.fetch import Fetcher, OutOfTime

    f = Fetcher("t", tmp_path / "s", min_interval=0, sleep=lambda _: None)
    f.robots.allowed = lambda url: True
    f.stop_at = time.monotonic() - 1
    with __import__("pytest").raises(OutOfTime):
        list(channels._browser_crawl(f, {"id": "x", "url": "http://127.0.0.1:9/"}, 5, channels.CrawlContext()))


def test_ocr_pending_count(tmp_path, monkeypatch, capsys):
    """`gacrawl ocr pending --count` prints how many documents wait for OCR (the merge job installs the OCR engine
    only when it is not zero)."""
    from gacrawl import cli, config

    store = Store(tmp_path / "data")
    store.add(Record("x:site", "document", "https://x.example/a.pdf", title="a", text="", meta={"ocr_pending": [2, 3]}))
    store.add(Record("x:site", "document", "https://x.example/b.pdf", title="b", text="fine"))
    store.add(Record("x:site", "page", "https://x.example/c", title="c", text="", meta={"ocr_pending": [1]}))   # not a document
    store.flush()
    store.close()
    monkeypatch.setattr(config, "make_store", lambda cfg: Store(tmp_path / "data"))
    monkeypatch.setattr(config, "load", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: {}, raising=False)
    cli.main(["ocr", "pending", "--count"])
    assert capsys.readouterr().out.strip() == "1"


def test_research_cut_short_by_the_deadline_leaves_the_candidate_due(tmp_path, monkeypatch):
    """A candidate whose research the deadline interrupted keeps what was found but is not marked done, so the
    next run finishes it; the fetcher carries the budget while research runs."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text("counties: []\nmunicipalities: []\n")
    store = Store(tmp_path / "data")
    f = FakeFetcher()
    r = Researcher(tmp_path, {"candidates": {"workers": 1, "gdelt": False, "google_news": False, "bluesky_search": False}},
                   f, store, log=lambda *_: None)
    clock = {"t": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    seen_budget = []

    def mentions(self, c, cls, add, since):
        seen_budget.append(self.f.stop_at)
        add(Record("", "mention", f"https://x/{c['id']}", title="t", text="t", channel="store", evidence="primary"))

    def votes(self, c, cls, add, since):
        if c["id"] == "c1":
            clock["t"] = 1000.0          # the budget runs out while c1 is being researched
    monkeypatch.setattr(Researcher, "_mentions", mentions)
    monkeypatch.setattr(Researcher, "_votes", votes)
    for name in ("_sponsorships", "_posts", "_statements", "_leads", "_social_mentions", "_connections"):
        monkeypatch.setattr(Researcher, name, lambda self, c, cls, add, since: None)
    cands = [{"id": f"c{i}", "name": f"Person {i}", "jurisdiction": "a", "aliases": []} for i in range(3)]
    prog = Progress(tmp_path / "cp.json")
    res = r.research_many(cands, "2016-01-01", 500.0, prog)
    assert res["researched"] == 1 and res["cut_short"] == 1 and res["added"]["mention"] == 2
    assert prog.get("cand:c0").get("last_ok") and not prog.get("cand:c1").get("last_ok")
    assert "still due" in prog.get("cand:c1").get("status", "") and not prog.get("cand:c2")
    assert seen_budget == [500.0, 500.0] and f.stop_at is None     # the budget was on the fetcher, and is off again
    store.close()


def test_research_stops_cleanly_on_sigint(tmp_path, monkeypatch):
    """The workflow's watchdog sends SIGINT: research finishes the candidates in hand, keeps what was found,
    returns normally (so the yield is written) and puts the signal handler back."""
    import os
    import signal

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text("counties: []\nmunicipalities: []\n")
    store = Store(tmp_path / "data")
    r = Researcher(tmp_path, {"candidates": {"workers": 1, "gdelt": False, "google_news": False, "bluesky_search": False}},
                   FakeFetcher(), store, log=lambda *_: None)
    before = signal.getsignal(signal.SIGINT)

    def mentions(self, c, cls, add, since):
        add(Record("", "mention", f"https://x/{c['id']}", title="t", text="t", channel="store", evidence="primary"))
        if c["id"] == "c1":
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)              # the handler runs in the main thread; give it a moment
    monkeypatch.setattr(Researcher, "_mentions", mentions)
    for name in ("_votes", "_sponsorships", "_posts", "_statements", "_leads", "_social_mentions", "_connections"):
        monkeypatch.setattr(Researcher, name, lambda self, c, cls, add, since: None)
    cands = [{"id": f"c{i}", "name": f"Person {i}", "jurisdiction": "a", "aliases": []} for i in range(3)]
    prog = Progress(tmp_path / "cp.json")
    res = r.research_many(cands, "2016-01-01", time.monotonic() + 60, prog)
    assert res["researched"] == 1 and res["cut_short"] == 1 and res["added"]["mention"] == 2
    assert prog.get("cand:c0").get("last_ok") and not prog.get("cand:c1").get("last_ok") and not prog.get("cand:c2")
    assert len(list(store.query("kind='mention'"))) == 2          # flushed on the way out
    assert signal.getsignal(signal.SIGINT) is before
    store.close()


def test_a_late_host_turn_skips_that_step_only(tmp_path, monkeypatch):
    """A shared host whose next turn comes after the deadline skips the step that needs it; the candidate's other
    steps still run, the candidate stays due, and the host is counted."""
    from gacrawl.fetch import OutOfTime

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text("counties: []\nmunicipalities: []\n")
    store = Store(tmp_path / "data")
    r = Researcher(tmp_path, {"candidates": {"workers": 1, "gdelt": False, "google_news": True, "bluesky_search": False,
                                             "leads_for": "all"}}, FakeFetcher(), store, log=lambda *_: None)
    ran = []

    def leads(self, c, cls, add, since):
        raise OutOfTime("news.google.com: next turn at +30s is past the deadline")

    def connections(self, c, cls, add, since):
        ran.append(c["id"])
    monkeypatch.setattr(Researcher, "_leads", leads)
    monkeypatch.setattr(Researcher, "_connections", connections)
    for name in ("_mentions", "_votes", "_sponsorships", "_posts", "_statements", "_social_mentions"):
        monkeypatch.setattr(Researcher, name, lambda self, c, cls, add, since: None)
    cands = [{"id": "c0", "name": "Person 0", "jurisdiction": "a", "aliases": [], "priority": True,
              "news_feeds": ["https://local-paper.example/feed"]}]
    prog = Progress(tmp_path / "cp.json")
    res = r.research_many(cands, "2016-01-01", time.monotonic() + 60, prog)
    # the busy host's step is skipped; the candidate's other steps ran and it is done (it is not sent back to the
    # queue for one busy host: that step is tried again at its next research)
    assert ran == ["c0"] and res["cut_short"] == 0 and res["researched"] == 1
    assert res["late_hosts"] == {"news.google.com": 1} and prog.get("cand:c0").get("last_ok")
    assert prog.get("cand:c0")["counts"].get("steps_skipped") == 1
    assert res["steps_skipped"] == 1 and "steps_skipped" not in res["added"]     # not counted as records added
    store.close()


def test_research_does_not_wait_long_for_a_strained_host():
    """With max_wait set (research), a host whose next turn is further off than that is not waited for."""
    from gacrawl.fetch import OutOfTime
    from gacrawl.polite import RateLimiter

    class F:
        from gacrawl.fetch import Fetcher as _F
        _turn = _F._turn
        deadline = lambda self: None  # noqa: E731
        max_wait = 45.0
    f = F()
    f.limiter = RateLimiter(2.0, sleep=lambda s: None)
    f.limiter._state("https://api.gdeltproject.org").next_ok = time.monotonic() + 300   # backed off far ahead
    try:
        f._turn("https://api.gdeltproject.org/api/v2/doc/doc", None)
        assert False, "should not wait 5 minutes"
    except OutOfTime as exc:
        assert str(exc).startswith("https://api.gdeltproject.org: next turn at +")
    f.max_wait = None
    f.limiter._state("https://x.example").next_ok = time.monotonic() + 1
    f._turn("https://x.example/", None)                                              # a short wait is taken


def test_big_documents_do_not_look_like_a_strained_server():
    """Back-off compares like with like: a PDF that takes longer than a JSON answer is not strain; a page that
    suddenly answers 5x slower than pages did, or a 429, still is."""
    from gacrawl.polite import RateLimiter

    lim = RateLimiter(1.0, sleep=lambda s: None)
    u, doc = "https://www.legis.ga.gov/api/legislation/detail/1", "https://www.legis.ga.gov/api/legislation/document/x/1"
    lim.record(u, 200, 0.2)
    for _ in range(20):                      # act PDFs answer in 1.5 s, the JSON in 0.2 s: no back-off
        lim.record(doc, 200, 1.5, kind="doc")
        lim.record(u, 200, 0.2)
    assert lim._state("https://www.legis.ga.gov").multiplier == 1.0
    assert lim.interval("https://www.legis.ga.gov") == 1.0
    lim.record(u, 200, 1.2)                  # a JSON answer 6x slower than JSON answers: strain
    assert lim._state("https://www.legis.ga.gov").multiplier == 2.0
    lim.record(u, 429, 0.2)
    assert lim._state("https://www.legis.ga.gov").multiplier == 4.0
    lim.record(doc, 200, 9.0, kind="doc")    # a document 6x slower than documents: strain too
    assert lim._state("https://www.legis.ga.gov").multiplier == 8.0


def test_google_news_leads_only_while_robots_allow(tmp_path, monkeypatch):
    """news.google.com's robots.txt refuses crawlers: the leads step then runs only for a candidate's own news feeds,
    and the run says why once."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text("counties: []\nmunicipalities: []\n")
    store = Store(tmp_path / "data")

    class F(FakeFetcher):
        def allowed(self, url):
            return "news.google.com" not in url
    r = Researcher(tmp_path, {"candidates": {"workers": 1, "gdelt": False, "google_news": True, "bluesky_search": False,
                                             "leads_for": "all"}}, F(), store, log=lambda *_: None)
    calls = []
    monkeypatch.setattr(Researcher, "_leads", lambda self, c, cls, add, since: calls.append(c["id"]))
    for name in ("_mentions", "_votes", "_sponsorships", "_posts", "_statements", "_social_mentions", "_connections"):
        monkeypatch.setattr(Researcher, name, lambda self, c, cls, add, since: None)
    cands = [{"id": "a", "name": "A A", "jurisdiction": "x", "aliases": []},
             {"id": "b", "name": "B B", "jurisdiction": "x", "aliases": [], "news_feeds": ["https://paper.example/rss"]}]
    res = r.research_many(cands, "2016-01-01", time.monotonic() + 30, Progress(tmp_path / "cp.json"))
    assert calls == ["b"] and "robots.txt" in res["google_news"]
    store.close()


def test_gdelt_spacing_counts_from_its_answer():
    from gacrawl.polite import RateLimiter

    t = {"now": 100.0}
    lim = RateLimiter(3.0, sleep=lambda s: t.__setitem__("now", t["now"] + s), clock=lambda: t["now"],
                      host_intervals={"api.gdeltproject.org": 6}, after_answer=["api.gdeltproject.org"])
    u = "https://api.gdeltproject.org/api/v2/doc/doc"
    lim.wait(u)
    t["now"] += 70                      # the search took 70 s to answer
    lim.record(u, 200, 70.0)
    assert lim.wait(u) == 6.0            # the next one waits 6 s after the answer, not 0
    other = "https://www.example.org/"
    lim.wait(other)
    t["now"] += 10
    lim.record(other, 200, 10.0)
    assert lim.wait(other) == 0.0        # other hosts: spacing from the request (10 s > 3 s already passed)


def test_articles_batch_opens_pages_concurrently_and_skips_untried(tmp_path, monkeypatch):
    from gacrawl.fetch import OutOfTime

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jurisdictions.yaml").write_text("counties: []\nmunicipalities: []\n")
    store = Store(tmp_path / "data")
    arts = {"articles": [{"url": "https://a.example/1", "title": "Mayor Jane Roe wins", "seendate": "20261001T000000Z", "domain": "a.example"},
                         {"url": "https://b.example/2", "title": "Jane Roe on budget", "seendate": "20261002T000000Z", "domain": "b.example"},
                         {"url": "https://c.example/3", "title": "Jane Roe again", "seendate": "20261003T000000Z", "domain": "c.example"}]}

    class R:
        def __init__(self, body, status=200, ctype="application/json"):
            self.content, self.status, self.content_type, self.url = body, status, ctype, ""
            self.text = body.decode()

        def json(self):
            return json.loads(self.text)

    class F(FakeFetcher):
        def get(self, url, params=None, **kw):
            return R(json.dumps(arts).encode())
    r = Researcher(tmp_path, {"candidates": {"news_workers": 3}}, F(), store, log=lambda *_: None)
    opened = []

    def open_article(self, url):
        opened.append(threading.current_thread().name)
        if "a.example" in url:
            return {"url": url, "title": "Mayor Jane Roe wins", "text": "Mayor Jane Roe of Testville said the budget passed."}
        if "b.example" in url:
            return None                       # tried, could not be opened: a lead (its title names her)
        raise OutOfTime("run time budget used up")   # never tried: nothing
    monkeypatch.setattr(Researcher, "_open_article", open_article)
    monkeypatch.setattr(Researcher, "_places", lambda self, c: ["Testville"])
    got = []
    c = {"id": "jane", "name": "Jane Roe", "jurisdiction": "testville", "aliases": []}
    n = r.articles_batch([c], "2016-01-01", {"jane": got.append})
    assert n == 1 and len(set(opened)) >= 1 and len(opened) == 3
    kinds = sorted((x.kind, x.url) for x in got)
    assert kinds == [("article", "https://a.example/1"), ("lead", "https://b.example/2")]
    store.close()


def test_a_source_completed_by_an_older_reader_is_due_now(tmp_path):
    """The old CivicClerk reader saw 15 meetings and the old YouTube reader 200 videos, then counted the source done:
    such a source is due at once (whatever its cadence) and read from the start; once the new reader completes it,
    its progress carries the reader version and the cadence applies again."""
    from gacrawl.scheduler import READER_VERSIONS, stale_reader

    prog = Progress(tmp_path / "progress.json")
    now = dt.datetime(2026, 11, 6, 21, 0, tzinfo=dt.timezone.utc)
    hour_ago = (now - dt.timedelta(hours=1)).isoformat()
    prog.update("homeland:civicclerk", last_ok=hour_ago, status="ok")                       # old reader, tier c (30 days)
    prog.update("tyrone:youtube", last_ok=hour_ago, status="ok", reader=READER_VERSIONS["youtube"])   # new reader
    prog.update("x-city:legistar", last_ok=hour_ago, status="ok")                           # not a changed reader
    srcs = [{"id": "homeland:civicclerk", "tier": "c", "endpoint": "civicclerk:homelandga"},
            {"id": "tyrone:youtube", "tier": "b", "endpoint": "youtube:https://www.youtube.com/@tyrone"},
            {"id": "x-city:legistar", "tier": "a", "endpoint": "legistar:xcity"}]
    order = [s["id"] for s in due(srcs, prog, {"a": 2, "b": 7, "c": 30}, now=now)]
    assert order == ["homeland:civicclerk"]
    assert stale_reader(srcs[0], prog.get("homeland:civicclerk")) and not stale_reader(srcs[1], prog.get("tyrone:youtube"))


def test_run_stamps_the_reader_version_when_a_source_completes(tmp_path, monkeypatch):
    from gacrawl import channels as ch_mod
    from gacrawl.scheduler import READER_VERSIONS, run_sources

    store = Store(tmp_path / "data")
    prog = Progress(tmp_path / "progress.json")
    cache = ChannelCache(tmp_path / "channels.json")
    seen = {}

    def fake_crawl(fetcher, src, chosen, limits, modified_since=None, ctx=None):
        seen["modified_since"] = modified_since
        yield Record(src["id"], "meeting", "https://homelandga.api.civicclerk.com/v1/Events(1)", title="Council 2026-10-01",
                     text="Council · 2026-10-01 · City Hall, Homeland, GA 31537")
    monkeypatch.setattr(ch_mod, "crawl", fake_crawl)

    class F:
        stop_at = None
        source_id = ""

        def set_pool(self, n):
            pass
    f = F()
    f.thread_stop_at = None
    prog.update("homeland:civicclerk", last_ok="2026-11-06T17:00:00+00:00", status="ok")     # the old reader's
    src = {"id": "homeland:civicclerk", "tier": "c", "channel": "api", "endpoint": "civicclerk:homelandga", "url": "x",
           "jurisdiction": "homeland"}
    run_sources({"schedule": {"api_anytime": True}}, [src], f, store, cache, prog, time.monotonic() + 60, force=True, workers=1,
                log=lambda *_: None)
    assert seen["modified_since"] is None                                    # read from the start
    p = prog.get("homeland:civicclerk")
    assert p["status"] == "ok" and p["reader"] == READER_VERSIONS["civicclerk"]
    store.close()
