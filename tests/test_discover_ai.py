import json
import time
from pathlib import Path

from gacrawl import discover_ai as da
from gacrawl.store import Store


class R:
    def __init__(self, url, html):
        self.url, self.text, self.status, self.content_type = url, html, 200, "text/html"


class F:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **k):
        return R(url, self.pages[url])


class S:
    def __init__(self, hits):
        self.hits, self.n = hits, 0

    def left(self):
        return 5

    def search(self, q, count=8):
        self.n += 1
        return self.hits

    def save(self):
        pass


class L:
    """A stand-in for the model: the judge answer is given per page address."""
    def __init__(self, answers):
        self.answers, self.logged = answers, []

    def chat(self, msgs, max_tokens=0):
        u = msgs[-1]["content"]
        for url, a in self.answers.items():
            if url in u:
                return json.dumps(a)
        return ""

    def log(self, path, rec):
        self.logged.append(rec)


def page(title, body):
    return f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{body}</p></main></body></html>"


def make(tmp_path, pages, answers):
    root = tmp_path
    (root / "state").mkdir()
    (root / "data").mkdir()
    (root / "config").mkdir()
    (root / "config" / "outlets.yaml").write_text("outlets:\n  - ajc.com\nexclude_domains:\n  - ballotpedia.org\n")
    store = Store(root / "db")
    d = da.Discovery(F(pages), store, root, root / "state", S([{"url": u} for u in pages]), llm=L(answers))
    d.cands = []
    return d


def test_campaign_site_auto_and_others_wait(tmp_path):
    u1 = "https://electjanedoe.com/"
    u2 = "https://somesite.example.org/jd"
    pages = {u1: page("Jane Doe for Fulton County Commission", "Jane Doe is running for Fulton County Commission District 3."),
             u2: page("Profile", "Jane Doe is running for Fulton County Commission District 3.")}
    q = "Jane Doe is running for Fulton County Commission District 3."
    d = make(tmp_path, pages, {u1: {"kind": "campaign", "about": True, "office": True, "quote": q},
                               u2: {"kind": "campaign", "about": True, "office": True, "quote": q}})
    task = {"type": "candidate", "key": "c:jane", "id": "jane-doe", "name": "Jane Doe", "office": "County Commission", "place": "Fulton"}
    for u in (u1, u2):
        p, _ = d._open(u)
        j = d.judge(task, p)
        what, how, why = d.decide(task, p, j)
        d.keep(task, p, j, what, how, why, "q")
    st = sorted((x["url"], x["status"]) for x in d.st["items"].values())
    assert st == [(u1, "auto"), (u2, "pending")]
    d.finish()
    rv = (tmp_path / "data" / "seed_review.md").read_text()
    assert "Waiting for approval (1)" in rv and u2 in rv


def test_made_up_quote_or_missing_name_is_dropped(tmp_path):
    u = "https://electjanedoe.com/"
    pages = {u: page("Jane Doe for Commission", "Vote Jane Doe.")}
    d = make(tmp_path, pages, {u: {"kind": "campaign", "about": True, "office": True, "quote": "Jane Doe has served 12 years."}})
    task = {"type": "candidate", "key": "c:j", "id": "j", "name": "Jane Doe", "office": "Commission", "place": "Fulton"}
    p, _ = d._open(u)
    what, how, why = d.decide(task, p, d.judge(task, p))
    assert how == "drop" and "not on the page" in why[0]
    task2 = dict(task, name="John Roe")
    j = {"kind": "campaign", "about": True, "office": True, "quote": "Vote Jane Doe."}
    assert d.decide(task2, p, j)[1] == "drop"


def test_decisions_apply_and_take_back(tmp_path):
    d = make(tmp_path, {}, {})
    d.st["items"] = {"aaaaaaaaaa": {"id": "aaaaaaaaaa", "kind": "campaign_site", "status": "pending", "url": "u"},
                     "bbbbbbbbbb": {"id": "bbbbbbbbbb", "kind": "campaign_site", "status": "auto", "url": "v"}}
    f = tmp_path / "r.md"
    f.write_text("- [x] `aaaaaaaaaa` campaign site\n- [r] `bbbbbbbbbb` campaign site\n")
    assert d.apply_decisions(f) == 2
    assert d.st["items"]["aaaaaaaaaa"]["status"] == "approved" and d.st["items"]["bbbbbbbbbb"]["status"] == "rejected"


def test_new_person_needs_three_outlets_same_office(tmp_path):
    d = make(tmp_path, {}, {})
    d.outlets = {"ajc.com", "wabe.org", "11alive.com", "gpb.org"}
    it = {"id": "cccccccccc", "kind": "new_person", "target": "Mo Ivory", "status": "pending", "sources": []}
    d.st["items"]["cccccccccc"] = it
    for o, office in (("ajc.com", "Fulton County Commission chair"), ("wabe.org", "chair of the Fulton County Commission")):
        it["sources"].append({"o": o, "u": "https://" + o + "/x", "office": office})
    d._auto_person(it)
    assert it["status"] == "pending" and "2 trusted outlets" in it["why"]
    it["sources"].append({"o": "11alive.com", "u": "https://11alive.com/x", "office": "state senator"})
    d._auto_person(it)
    assert it["status"] == "pending" and "not in the same office" in it["why"]
    it["sources"][-1]["office"] = "Fulton County commissioner"
    d._auto_person(it)
    assert it["status"] == "auto"
    from gacrawl.discover_ai import accepted
    d.finish()
    assert any(x["target"] == "Mo Ivory" for x in accepted(tmp_path / "state"))


def test_scope_keeps_to_georgia_officials_and_candidates():
    from gacrawl.discover_ai import scope_check
    ga = {"fulton", "fulton county", "savannah", "city of savannah", "washington", "washington county"}
    assert scope_check("Fulton County Commission chair", "Fulton County", ga)[0] == "ok"
    assert scope_check("state senator", "Georgia", ga)[0] == "ok"
    assert scope_check("city council candidate", "Savannah, GA", ga)[0] == "ok"
    assert scope_check("county commissioner", "Washington County", ga)[0] == "ok"      # a Georgia county, not the state
    assert scope_check("Georgia GOP chair", "Georgia", ga)[0] == "out"                 # party official
    assert scope_check("campaign manager", "Atlanta", ga)[0] == "out"
    assert scope_check("vice president", "", ga)[0] == "out"
    assert scope_check("governor", "Ohio", ga)[0] == "out"                             # another state
    assert scope_check("mayor", "Charlotte, North Carolina", ga)[0] == "out"
    assert scope_check("activist", "Atlanta", ga)[0] == "out"
    assert scope_check("", "Atlanta", ga)[0] == "out"                                  # no office named
    assert scope_check("mayor", "Smallville", ga)[0] == "wait"                         # unknown place: owner decides


def test_scope_covers_police_schools_and_city_roles():
    from gacrawl.discover_ai import scope_check, _office_words
    ga = {"atlanta", "savannah", "cobb", "cobb county", "dekalb", "dekalb county"}
    for office, place in (("Atlanta police chief", "Atlanta"), ("chief of police", "Savannah"), ("sheriff", "Cobb County"),
                          ("Cobb County Schools board member", "Cobb County"), ("school superintendent", "DeKalb County"),
                          ("principal of Grady High School", "Atlanta"), ("alderman", "Savannah"),
                          ("city council member", "Savannah"), ("city manager", "Savannah"),
                          ("DeKalb elections board member", "DeKalb County"), ("director of public safety", "Atlanta")):
        assert scope_check(office, place, ga)[0] == "ok", office
    assert _office_words("alderman") == {"council"}
    assert _office_words("Cobb County Schools board member") >= {"board of education"}
    assert _office_words("school board chair") & _office_words("board of education member")
