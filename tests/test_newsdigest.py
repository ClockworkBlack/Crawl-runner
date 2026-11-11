import json
from pathlib import Path

from gacrawl import newsdigest as nd
from gacrawl.store import Store


def test_keep_and_tags():
    assert nd.keep("Gwinnett commission approves budget", "The county commission voted 4-1.")
    assert nd.keep("Kemp signs order", "")
    assert not nd.keep("Braves win in extra innings", "A walk-off homer in Atlanta.")
    assert "budget" in nd.tags_of("Gwinnett commission approves budget", "")


def test_excerpt_trims():
    assert nd._excerpt("One. Two. Three.") == "One. Two."
    assert nd._excerpt("A story. The post A story appeared first on X.") == "A story."


def test_export(tmp_path):
    import datetime as dt
    from gacrawl.store import Record

    s = Store(tmp_path / "t.db")
    d = dt.date.today().isoformat()
    s.add(Record(nd.SOURCE, "news_item", "https://ex.com/a", title="Council passes ordinance", text="x.", published=d,
                 meta={"outlet_name": "Ex News", "tags": ["local"]}))
    s.add(Record(nd.SOURCE, "news_item", "https://ex.com/old", title="Old council story", published="2020-01-01"))
    s.flush()
    out = tmp_path / "news.json"
    c = nd.export_news(s, out)
    j = json.loads(out.read_text())
    assert c["items"] == 1 and j["items"][0]["s"] == "Ex News" and j["items"][0]["d"] == d and j["updated"]


def test_crime_national_and_local():
    assert not nd.keep("Walker County Sheriff Arrest Report 11/7/2026", "")
    assert not nd.keep("Drunk driver gets 10 years for killing YMCA worker in Augusta", "")
    assert not nd.keep("Texas' School Voucher Program Could Be Extinct, Says Democratic Lawmaker", "")
    assert not nd.keep("Stephen Miller replaces chief of staff after GOP midterm losses", "The White House said Friday.")
    assert nd.keep("Missing memory card temporarily changed apparent outcome in state House race", "")
    assert nd.keep("Rochelle adopts budget; tax hearings set before millage vote", "The council met Monday.")
    assert nd.keep("Kemp declines special session to limit executive powers", "")


def test_surname_is_not_a_town_and_lifestyle_is_out():
    assert nd.counties_of("Jason Collins named to the board", "") == []
    assert nd.counties_of("Collins council approves budget", "") == ["Tattnall"]
    assert not nd.keep("My Five: Tucker Mayor's favorite local restaurants", "")
    assert not nd.keep("Still focused on D.C. projects, Trump signs order preserving Oval Office", "The county said.")
    assert nd.keep("Election Season and Business Confidence: Reading the Room in Cobb County", "The commission met.")


def test_federal_level():
    """The federal government's news is its own level; Georgia's rules are unchanged, and crime, sports and other
    states' news belong to neither."""
    assert nd.level_of("Senate passes bill to end government shutdown", "The Senate voted 60-40 Monday.") == "federal"
    assert nd.level_of("Supreme Court to hear Texas redistricting case", "") == "federal"
    assert nd.level_of("Stephen Miller replaces chief of staff after GOP midterm losses", "The White House said Friday.") == "federal"
    assert nd.level_of("Ossoff, Warnock push bill for rural hospitals in Georgia", "A bill in Congress.") == "ga"
    assert nd.level_of("Atlanta City Council approves budget", "") == "ga"
    for t in ("Man arrested by FBI in Macon bank robbery", "California governor signs housing law", "Falcons beat Saints in overtime",
              "Texas' School Voucher Program Could Be Extinct, Says Democratic Lawmaker",
              "FBI investigates suspected homicide aboard MSC Seascape cruise ship after sudden return to Texas",
              "Check your freezers: USDA warns consumers about frozen meat, poultry products sold nationwide",
              "Trump says Air Force pilots described a round UFO flying three times faster than an F-22 fighter jet"):
        assert nd.level_of(t, "") is None, t
    assert nd.fed_tags_of("Senate passes bill to end government shutdown", "") == ["congress", "budget"]


def test_export_keeps_levels_apart(tmp_path):
    import datetime as dt
    from gacrawl.store import Record

    s = Store(tmp_path / "t.db")
    d = dt.date.today().isoformat()
    for i, (t, m) in enumerate([("Council passes ordinance", {}), ("Senate confirms judge after Trump nomination", {"level": "federal"}),
                                ("Ossoff bill in Congress would fund Georgia hospitals", {}),
                                ("Budget talks continue", {"level": "federal"})]):      # a federal section's story without a federal word: dropped
        s.add(Record(nd.SOURCE, "news_item", f"https://ex.com/{i}", title=t, text="x.", published=d, meta={"outlet_name": "Ex", **m}))
    s.flush()
    out = tmp_path / "news.json"
    c = nd.export_news(s, out)
    by = {x["t"]: x for x in json.loads(out.read_text())["items"]}
    assert c["items"] == 3 and c["federal"] == 1
    assert by["Senate confirms judge after Trump nomination"]["lv"] == "federal" and "courts" not in by["Council passes ordinance"]["tags"]
    assert by["Ossoff bill in Congress would fund Georgia hospitals"].get("fed") == 1 and "lv" not in by["Ossoff bill in Congress would fund Georgia hospitals"]
    assert "lv" not in by["Council passes ordinance"] and "fed" not in by["Council passes ordinance"]


def test_federal_feeds_keep_only_federal_stories(tmp_path):
    """A federal section's feed (the one its page links to, the politics feed when there are several): federal stories
    are kept as federal, the rest dropped, and nothing from it becomes a Georgia story."""
    class R:
        def __init__(self, body, ct="text/html", url=""):
            self.status, self.content, self.text, self.content_type, self.url = 200, body.encode(), body, ct, url
    page = ('<html><head><link rel="alternate" type="application/rss+xml" href="https://fed.example/rss/main/">'
            '<link rel="alternate" type="application/rss+xml" href="https://fed.example/rss/politics/"></head></html>')
    today = __import__("email.utils").utils.format_datetime(__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    feed = ('<rss version="2.0"><channel>' + "".join(f"<item><title>{t}</title><link>https://fed.example/{i}</link><description>{x}</description><pubDate>{today}</pubDate></item>"
            for i, (t, x) in enumerate([("House passes spending bill, sending it to Trump", "The House voted Tuesday."),
                                        ("Atlanta City Council approves budget", "The council voted."),
                                        ("A recipe for fall", "Soup.")])) + "</channel></rss>")
    class F:
        got = []
        def get(self, url, **k):
            F.got.append(url)
            return R(page, url=url) if url.endswith("/politics/") and "rss" not in url else R(feed, "application/rss+xml", url)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "outlets.yaml").write_text("outlets: []\nfederal_pages:\n  fed.example: https://fed.example/politics/\n")
    s = Store(tmp_path / "t.db")
    st = nd.Digest(F(), s, tmp_path, tmp_path, log=lambda *a: None).run()
    assert "https://fed.example/rss/politics/" in F.got and "https://fed.example/rss/main/" not in F.got
    rows = list(s.iter_query("kind='news_item'"))
    assert st["federal"] == 1 and len(rows) == 1 and rows[0]["title"].startswith("House passes spending bill")


def test_news_audit_nov10():
    """The Nov 10 News audit: whole-word tags, a district race is not statewide, a surname is not a town in its other
    mentions, "US Senate" and "Sen. Ossoff" are federal, "midterm" alone is not, and races and screenings are not news."""
    assert nd.tags_of("Pivotal week for courtesy", "Pam Bondi spoke.") == []
    assert "local" not in nd.tags_of("Democrats flip Georgia Public Service Commission", "The commission sets rates.")
    assert "local" in nd.tags_of("Augusta Commission weighs rentals", "The city council met.")
    assert not nd.STATEWIDE.search("David Dillie wins state House District 161 race")
    assert nd.STATEWIDE.search("Democrats win control of the state House")
    assert nd.counties_of("Rep. Mike Collins wins Senate seat", "Collins said he would work for Georgia.") == []
    assert nd.counties_of("Collins council approves budget", "") == ["Tattnall"]
    assert nd.counties_of("Bottoms takes office", "Bottoms spoke in Atlanta.") == []
    assert nd.federal("Incoming chair of US Senate panel visits Macon", "")
    assert nd.federal("Sen. Ossoff introduces rural hospitals bill", "")
    assert not nd.federal("Cobb certifies 2026 midterm results after record turnout", "")
    assert not nd.keep("Augusta Half Marathon seeks 300 volunteers for 2027 race", "The Augusta Sports Council needs help.")
    assert not nd.keep("Conyers filmmakers to screen Southern Gothic film", "The city council approved.")


def test_news_loop_passes_read_only_what_changed(tmp_path):
    """Outlets read several at a time give the same digest as one at a time; a second pass in the same process asks a
    WordPress outlet for its list without the posts' text and then for the text of the new post only, an unchanged feed
    (304) adds nothing, and the export names the outlets that answered, in the same order every run."""
    import datetime as _dt
    import json as _json
    import threading
    today = _dt.date.today().isoformat() + "T09:00:00"
    rfc = __import__("email.utils").utils.format_datetime(_dt.datetime.now(_dt.timezone.utc))
    posts = {"a.example": [{"id": 1, "link": "https://a.example/1", "date": today, "title": {"rendered": "Georgia Senate passes budget"},
                            "excerpt": {"rendered": "Lawmakers in Atlanta voted."}, "content": {"rendered": "<p>Lawmakers in Atlanta voted on the budget.</p>"}}],
             "b.example": [{"id": 7, "link": "https://b.example/7", "date": today, "title": {"rendered": "Cobb County commission approves millage"},
                            "excerpt": {"rendered": "The Cobb County commission voted."}, "content": {"rendered": "<p>The Cobb County commission voted.</p>"}}]}
    feed = ('<rss version="2.0"><channel><item><title>Macon City Council adopts budget</title><link>https://c.example/9</link>'
            f'<description>The Macon council voted.</description><pubDate>{rfc}</pubDate></item></channel></rss>')

    class R:
        def __init__(self, body, url, nm=False):
            self.status, self.url, self.not_modified = 200, url, nm
            self.content = body.encode() if isinstance(body, str) else body
            self.text, self.content_type = self.content.decode(), "application/json" if self.content[:1] == b"[" else "application/rss+xml"

        def json(self):
            return _json.loads(self.content)

    class F:
        def __init__(self):
            self.calls, self.lock, self.etag = [], threading.Lock(), False

        def get(self, url, params=None, use_cache=True, **k):
            with self.lock:
                self.calls.append((url, dict(params or {}), use_cache))
            dom = url.split("/")[2]
            if dom in posts:
                ps = posts[dom]
                if (params or {}).get("include"):
                    ids = {int(i) for i in params["include"].split(",")}
                    ps = [p for p in ps if p["id"] in ids]
                fields = (params or {}).get("_fields", "").split(",")
                return R(_json.dumps([{k2: v for k2, v in p.items() if k2 in fields} for p in ps]), url)
            nm = use_cache and self.etag
            self.etag = self.etag or use_cache
            return R(feed, url, nm)

    def make(root):
        (root / "config").mkdir(parents=True)
        (root / "config" / "outlets.yaml").write_text("outlets: [a.example, b.example, c.example]\nnames: {c.example: The C}\n")
        (root / "wp_outlets.json").write_text(_json.dumps({"a.example": {"api": "https://a.example/wp-json"},
                                                            "b.example": {"api": "https://b.example/wp-json"}}))
        (root / "news_feeds.json").write_text(_json.dumps({"c.example": {"feed": "https://c.example/feed", "checked": _dt.date.today().isoformat()}}))
        return root

    one, many = make(tmp_path / "one"), make(tmp_path / "many")
    s1, s2 = Store(one / "t.db"), Store(many / "t.db")
    nd.Digest(F(), s1, one, one, log=lambda *a: None).run(workers=1)
    f = F()
    dg = nd.Digest(f, s2, many, many, log=lambda *a: None)
    dg.conditional = True
    st = dg.run(workers=4)
    rows = lambda s: sorted(r["url"] for r in s.iter_query("kind='news_item'"))
    assert st["new"] == 3 and rows(s1) == rows(s2)
    assert dg.read == {"a.example", "b.example", "The C"}
    # pass 2: a new post at b.example
    posts["b.example"].append({"id": 8, "link": "https://b.example/8", "date": today, "title": {"rendered": "Georgia governor signs order"},
                               "excerpt": {"rendered": "Gov. Kemp signed it in Atlanta."}, "content": {"rendered": "<p>Kemp signed it.</p>"}})
    f.calls.clear()
    st2 = dg.run(workers=4)
    wp = sorted((u.split("/")[2], p.get("_fields"), p.get("include")) for u, p, _ in f.calls if "wp-json" in u)
    assert wp == [("a.example", "id,link,date,title", None), ("b.example", "id,link,date,title", None),
                  ("b.example", "id,link,date,title,excerpt,content", "8")]
    assert st2["new"] == 1 and len(rows(s2)) == 4
    # export: the outlets that answered, newest first and then by link, the same however the stories came in
    out = tmp_path / "news.json"
    nd.export_news(s2, out, read=sorted(dg.read))
    d = _json.loads(out.read_text())
    assert d["read"] == ["The C", "a.example", "b.example"]
    assert [x["u"] for x in d["items"]] == sorted([x["u"] for x in d["items"]], reverse=True)
