"""A city whose directory entry was read with an older parser is due at once (its officials had first names only)."""
from gacrawl import seed


def test_old_directory_reading_is_due():
    j = {"id": "dunwoody", "kind": "city", "name": "Dunwoody", "fips": "1324768"}
    fresh = {"seed_version": seed.SEED_VERSION, "checked_at": seed._now(), "website": "https://www.dunwoodyga.gov/"}
    assert seed.due({**fresh, "gma_parser": seed.GMA_PARSER}, j, 30) is False
    assert seed.due({**fresh, "gma_parser": seed.GMA_PARSER - 1}, j, 30) is True
    assert seed.due(fresh, j, 30) is True                    # read before the parser was versioned
    assert seed.due(fresh, {**j, "kind": "county", "id": "co-dekalb"}, 30) is False


# ----------------------------------------------------------------------------- the directory entry on its own
import datetime as _dt
import json as _json

GMA_PAGE = """<html><body><h1>Dunwoody</h1><a href="https://www.dunwoodyga.gov/">www.dunwoodyga.gov</a>
<p>Council meets: Second/Fourth Monday</p><h2>Elected Officials</h2>
<div><span>Mayor</span><span>Lynn</span> <span>Deutsch</span></div>
<div><span>Councilmember</span><span>Post 2</span><span>Stacey</span><span>Harris</span></div>
<div><span>Mayor Pro Tem</span><span>Tom</span><span>Lambert</span></div><h2>Appointed Officials</h2>
<div><span>City Manager</span><span>Someone</span></div></body></html>"""


class Net:
    """Counts what is asked for; answers the directory page (or `status`) and nothing else."""

    def __init__(self, status=200):
        self.asked, self.status = [], status

    def get(self, url, **kw):
        self.asked.append(url)

        class R:
            pass
        r = R()
        if "gacities.com" in url:
            r.status, r.text, r.url = self.status, GMA_PAGE if self.status == 200 else "", url
            return r
        raise RuntimeError("not the directory: " + url)


def _days_ago(n):
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=n)).replace(microsecond=0).isoformat()


J = {"id": "dunwoody", "kind": "city", "name": "Dunwoody", "fips": "1324768", "population": 51683}


def _seed(tmp_path, rec, net, monkeypatch):
    monkeypatch.setattr(seed, "wikidata_websites", lambda f: {})
    p = tmp_path / "discovered.json"
    p.write_text(_json.dumps({"dunwoody": rec}))
    return seed.seed(net, [J], p, workers=1, log=lambda *_: None, max_age_days=30)["dunwoody"]


def test_an_old_parsers_entry_costs_one_request_and_the_rest_of_the_record_stands(tmp_path, monkeypatch):
    old = {"seed_version": seed.SEED_VERSION, "checked_at": _days_ago(3), "website": "https://www.dunwoodyga.gov/",
           "portals": {"legistar": "x"}, "feeds": ["f"], "gma_url": seed.gma_url(J), "officials": [{"title": "Mayor", "name": "Lynn"}]}
    net = Net()
    rec = _seed(tmp_path, old, net, monkeypatch)
    assert net.asked == [seed.gma_url(J)]                              # the directory page, and no other host
    assert [o["name"] for o in rec["officials"]] == ["Lynn Deutsch", "Stacey Harris", "Tom Lambert"]
    assert rec["officials"][1]["title"] == "Councilmember Post 2" and rec["gma_parser"] == seed.GMA_PARSER
    assert rec["portals"] == {"legistar": "x"} and rec["checked_at"] == old["checked_at"] and rec["gma_checked_at"]
    assert seed.due(rec, J, 30) is False                                # done: not asked again tomorrow


def test_the_entry_is_read_again_after_half_a_year_counted_from_its_own_reading(tmp_path, monkeypatch):
    """The 180 days were counted from the record's last look, which every monthly look renews: an entry once read
    was never read again, whoever was elected since."""
    base = {"seed_version": seed.SEED_VERSION, "website": "https://www.dunwoodyga.gov/", "gma_url": seed.gma_url(J),
            "gma_parser": seed.GMA_PARSER, "officials": [{"title": "Mayor", "name": "Old Mayor"}]}
    looked_last_week = {**base, "checked_at": _days_ago(6), "gma_checked_at": _days_ago(181)}
    assert seed.due(looked_last_week, J, 30) is True
    assert seed.due({**looked_last_week, "gma_checked_at": _days_ago(100)}, J, 30) is False
    net = Net()
    rec = _seed(tmp_path, looked_last_week, net, monkeypatch)
    assert net.asked == [seed.gma_url(J)] and rec["officials"][0]["name"] == "Lynn Deutsch"


def test_a_directory_page_that_is_not_there_is_asked_for_weekly_not_every_run(tmp_path, monkeypatch):
    """Ranger and Sunny Side (HTTP 404 on Nov. 19, 2026) had no parser stamp, so they were due on every run and each
    time got the whole look."""
    old = {"seed_version": seed.SEED_VERSION, "checked_at": _days_ago(1), "website": "https://x.example/", "gma_error": "HTTP 404"}
    net = Net(status=404)
    rec = _seed(tmp_path, old, net, monkeypatch)
    assert net.asked == [seed.gma_url(J)] and rec["gma_error"] == "HTTP 404" and rec["website"] == "https://x.example/"
    assert seed.due(rec, J, 30) is False
    assert seed.due({**rec, "gma_tried": _days_ago(8)}, J, 30) is True
