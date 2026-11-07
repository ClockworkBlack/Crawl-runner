"""A robots.txt that cannot be read (5xx, dropped connection) denies the host only briefly, then is asked for
again; 401/403 and a real disallow hold for the day."""
from gacrawl.polite import RobotsCache


def test_transient_robots_error_is_retried_soon():
    answers = [(599, ""), (200, "User-agent: *\nDisallow: /private\n")]
    calls = []

    def get(url):
        calls.append(url)
        return answers.pop(0) if answers else (200, "User-agent: *\nAllow: /\n")
    clock = {"t": 1000.0}
    rc = RobotsCache(get, "GACivicCrawler/0.3", error_ttl=120, warn=lambda *_: None)
    import gacrawl.polite as polite
    real = polite.time.time
    polite.time.time = lambda: clock["t"]
    try:
        assert not rc.allowed("https://www.legis.ga.gov/api/x")          # unreadable: disallowed for now
        assert rc.failures == {"https://www.legis.ga.gov": 599}
        clock["t"] += 60
        assert not rc.allowed("https://www.legis.ga.gov/api/x") and len(calls) == 1   # still inside the short hold
        clock["t"] += 61
        assert rc.allowed("https://www.legis.ga.gov/api/x") and len(calls) == 2       # asked again, now readable
        assert not rc.allowed("https://www.legis.ga.gov/private/x")
        clock["t"] += 3600
        assert rc.allowed("https://www.legis.ga.gov/api/x") and len(calls) == 2       # a good answer holds for the day
    finally:
        polite.time.time = real


def test_forbidden_robots_holds():
    rc = RobotsCache(lambda url: (403, ""), "GACivicCrawler/0.3", warn=lambda *_: None)
    assert not rc.allowed("https://x.example/") and not rc.failures
