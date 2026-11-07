"""The seed's gap handling, offline: a directory listing is a candidate, not the answer.

Dead listings fall through to the next directory's site (and to https/www spellings), refusals say why
(unreachable, robots, blocked), failed records are re-checked weekly and whenever the seed or an override
changes, the GMA officials parser keeps whole names and every official, and `gacrawl seed --report` lists what
still needs a hand check.
"""
import datetime as dt
import http.server
import socket
import threading

import pytest
import requests

from gacrawl import seed
from gacrawl.fetch import Disallowed, Fetcher

LIVE = b"""<html><head><title>City of Liveville</title></head><body>
<a href="https://liveville.legistar.com/Calendar.aspx">Agendas &amp; Minutes</a>
<a href="https://www.youtube.com/@CityofLiveville">YouTube</a></body></html>"""

SITES = {
    "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
    "/": (200, "text/html", LIVE),
    "/gone": (404, "text/html", b"<html><body>not here</body></html>"),
}

ROBOTS_403 = {
    "/robots.txt": (403, "text/html", b"<html><body>" + b"x" * 3000 + b"</body></html>"),   # a plain 403, no challenge
    "/": (200, "text/html", LIVE),
}

CHALLENGE = {
    "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
    "/": (403, "text/html", b"<html><body><h2>Access blocked</h2><script>__cf_chl</script></body></html>"),
}


def _serve(site):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path not in site:
                self.send_response(404); self.end_headers(); return
            status, ctype, body = site[path]
            self.send_response(status)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def live():
    srv = _serve(SITES)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def robots403():
    srv = _serve(ROBOTS_403)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def challenge():
    srv = _serve(CHALLENGE)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def dead():
    """A port nothing listens on: the connection is refused at once."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("TestCrawler/0.1", tmp_path / "state", min_interval=0, session=s, sleep=lambda s: None, timeout=5)


# ----------------------------------------------------------------------------- refusals say why
def test_disallowed_names_the_reason(fetcher, dead, robots403):
    with pytest.raises(Disallowed) as e:
        fetcher.get(dead + "/")
    assert e.value.reason.startswith("unreachable")
    assert seed.error_kind(e.value) == "unreachable"
    with pytest.raises(Disallowed) as e:
        fetcher.get(robots403 + "/")
    assert "robots.txt HTTP 403" in e.value.reason
    assert seed.error_kind(e.value) == "robots"


def test_inspect_site_records_the_kind(fetcher, dead, challenge, live):
    assert seed.inspect_site(fetcher, dead + "/")["site_error_kind"] == "unreachable"
    assert seed.inspect_site(fetcher, challenge + "/")["site_error_kind"] == "blocked"
    gone = seed.inspect_site(fetcher, live + "/gone")
    assert gone["site_error_kind"] == "http" and gone["site_status"] == 404
    assert seed.inspect_site(fetcher, live + "/")["portals"]["legistar"].startswith("https://liveville.legistar.com")


# ----------------------------------------------------------------------------- fallthrough
def test_dead_listing_falls_through_to_the_next_directory(fetcher, dead, live):
    res = seed.resolve_site(fetcher, [(dead + "/", "wikidata"), (live + "/", "gma")])
    assert res["website"] == live + "/" and res["website_source"] == "gma"
    assert "site_error" not in res
    tried = res["website_candidates"]
    assert [t["source"] for t in tried][0] == "wikidata" and tried[0]["kind"] == "unreachable"
    assert tried[-1]["source"] == "gma" and tried[-1]["error"] is None
    assert res["portals"]["legistar"]


def test_blocked_host_is_not_worked_around_but_the_next_site_is_tried(fetcher, challenge, live):
    res = seed.resolve_site(fetcher, [(challenge + "/", "wikidata"), (live + "/", "gma")])
    assert res["website"] == live + "/"
    assert res["website_candidates"][0]["kind"] == "blocked"
    # the blocked host is on the block list now; a second spelling of it is the same host and is not tried
    assert seed.variants(challenge + "/", "blocked") == []
    assert seed.variants("https://www.x.gov/", "robots") == []


def test_wikipedia_is_asked_only_when_everything_listed_failed(fetcher, dead, live):
    asked = []

    def lazy():
        asked.append(1)
        return [(live + "/", "wikipedia")]

    res = seed.resolve_site(fetcher, [(live + "/", "wikidata")], lazy=lazy)
    assert res["website_source"] == "wikidata" and not asked
    res = seed.resolve_site(fetcher, [(dead + "/", "wikidata")], lazy=lazy)
    assert res["website_source"] == "wikipedia" and asked


def test_all_dead_keeps_the_first_error_and_everything_tried(fetcher, dead):
    res = seed.resolve_site(fetcher, [(dead + "/", "wikidata")])
    assert res["website"] == dead + "/" and res["site_error_kind"] == "unreachable"
    # the dead http listing was also tried as https and as the www spellings
    urls = [t["url"] for t in res["website_candidates"]]
    assert urls[0] == dead + "/" and any(u.startswith("https://") for u in urls)
    assert len(urls) <= seed.MAX_CANDIDATES


def test_variants():
    assert seed.variants("http://www.town.org/", "unreachable") == ["https://www.town.org/", "https://town.org/", "http://town.org/"]
    assert seed.variants("http://x.k12.ga.us/education/scrapbook/default.php", "http") == ["http://x.k12.ga.us/"]
    assert seed.variants("https://www.town.org/", "ssl") == ["https://town.org/"]


# ----------------------------------------------------------------------------- when a record is due again
def _rec(days_old: float, **kw):
    at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_old)
    return {"checked_at": at.replace(microsecond=0).isoformat(), "seed_version": seed.SEED_VERSION,
            "website": "https://www.town.org/", **kw}


def test_due_rules():
    j = {"id": "town", "overrides": []}
    assert seed.due(None, j, 30)
    assert seed.due(_rec(1, seed_version=1), j, 30), "an older seed's record is due at once"
    assert not seed.due(_rec(10), j, 30), "a working site inside the cadence"
    assert seed.due(_rec(40), j, 30)
    assert seed.due(_rec(10, site_error="Blocked: x", site_error_kind="blocked"), j, 30), "failures are retried weekly"
    assert not seed.due(_rec(3, site_error="Blocked: x"), j, 30)
    assert seed.due({**_rec(10), "website": None}, j, None), "no website: retried weekly even without --max-age-days"
    assert not seed.due(_rec(10), j, None)
    assert seed.due(_rec(1), {"id": "town", "overrides": ["website"], "website": "https://new.town.org/"}, 30), \
        "a hand override the record has not tried"
    assert not seed.due(_rec(1), {"id": "town", "overrides": ["website"], "website": "https://www.town.org/"}, 30)
    assert seed.due(_rec(1), j, 30, refresh=True)


def test_hand_override_is_told_apart_from_an_earlier_finding():
    # registry.jurisdictions merges the seed's own earlier website into j; only `overrides` marks a hand edit
    assert seed.hand_website({"website": "https://a/", "overrides": []}) is None
    assert seed.hand_website({"website": "https://a/", "overrides": ["website"]}) == "https://a/"


# ----------------------------------------------------------------------------- GMA directory
GMA_LIVE = """<html><body><p>Atlanta</p><p>55 Trinity Avenue SW</p><p>Atlanta,GA30303</p>
<a href="tel:(404) 330-6000">(404) 330-6000</a>
<a href="https://www.atlantaga.gov">www.atlantaga.gov</a>
<p>Council meets: First/Third Monday at 1:00 PM</p>
<p>Year of Incorporation</p><p>1837</p><p>Regional Commission</p><p>Atlanta</p>
<h2>Elected Officials</h2>
<p>Mayor</p><span>Andre</span><span>Dickens</span>
<p>Councilmember</p><span>Byron</span><span>Amos</span>
<p>Council President</p><span>Marci</span><span>Overstreet</span>
<p>Councilmember</p><p>Post 2</p><span>C. Howard</span><span>Shook</span>
<p>Councilmember</p><p>EdgarGreene</p>
<h2>Appointed Officials</h2><h2>Key Roles</h2><p>City Clerk</p><p>Some One</p></body></html>"""


def test_gma_officials_keep_whole_names_and_every_official():
    g = seed.parse_gma(GMA_LIVE, "https://www.gacities.com/gma-cities-districts/atlanta/04000")
    assert g["website"] == "https://www.atlantaga.gov/"
    assert g["phone"] == "(404) 330-6000"
    assert g["address"] == "55 Trinity Avenue SW, Atlanta, GA 30303"
    assert g["incorporated"] == 1837 and g["gma_parser"] == seed.GMA_PARSER
    assert g["officials"] == [
        {"title": "Mayor", "name": "Andre Dickens"},
        {"title": "Councilmember", "name": "Byron Amos"},
        {"title": "Council President", "name": "Marci Overstreet"},
        {"title": "Councilmember Post 2", "name": "C. Howard Shook"},
        {"title": "Councilmember", "name": "Edgar Greene"},
    ]


# ----------------------------------------------------------------------------- the gap report
def test_gap_report_lists_each_kind_with_an_override_stanza():
    discovered = {k: {"seed_version": seed.SEED_VERSION, "checked_at": seed._now(), **v} for k, v in {
        "atlanta": {"website": "https://www.atlantaga.gov/", "site_error": "Blocked: www.atlantaga.gov: HTTP 403 access denied",
                    "site_error_kind": "blocked", "council_meets": "First Monday", "phone": "(404) 330-6000",
                    "portals": {"ecode360": "https://ecode360.com/AT1"}, "website_candidates": []},
        "co-chatham": {"website": "http://www.chathamcounty.org/", "site_error": "Disallowed: ... (unreachable (NameResolutionError))",
                       "site_error_kind": "unreachable",
                       "website_candidates": [{"url": "http://www.chathamcounty.org/", "source": "wikidata", "error": "x", "kind": "unreachable"},
                                              {"url": "https://www.chathamcounty.org/", "source": "wikidata (https form)", "error": "x", "kind": "unreachable"}]},
        "wrightsville": {"portals": {}, "council_meets": "Third Monday", "phone": "(478) 864-3303",
                         "none_listed": {"wikidata": False, "gma": True, "wikipedia": True}, "website_candidates": []},
        "fine": {"website": "https://fine.gov/", "portals": {}},
    }.items()}
    jur = [{"id": "atlanta", "name": "Atlanta", "kind": "city", "population": 500000},
           {"id": "co-chatham", "name": "Chatham County", "kind": "county", "population": 300000},
           {"id": "wrightsville", "name": "Wrightsville", "kind": "city", "population": 3449, "county": "Johnson"},
           {"id": "fine", "name": "Fine", "kind": "city", "population": 10}]
    md, summary = seed.gap_report(discovered, jur)
    assert summary == {"blocked": 1, "unreachable": 1, "none_listed": 1, "other_errors": 0, "working": 1, "pending_next_seed": 0}
    assert "## Refuses the crawler" in md and "atlanta" in md and "ecode360" in md and "open records" in md.lower()
    assert "## Listed site cannot be reached" in md and "https://www.chathamcounty.org/" in md
    assert "## No site listed anywhere the seed looks" in md and "Wrightsville" in md and "Third Monday" in md
    assert md.count("  wrightsville:\n    website: ") == 1, "a ready stanza for config/overrides.yaml"
    assert "fine" not in md.split("## Refuses")[1]
