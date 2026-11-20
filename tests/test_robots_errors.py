"""A robots.txt that cannot be read (5xx, dropped connection) denies the host only briefly, then is asked for
again; a real disallow holds for the day."""
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


def test_forbidden_robots_is_no_robots():
    """RFC 9309 2.3.1.3: a 4xx answer (401 and 403 too) means the file is unavailable and the site is open. It was
    read as "disallow everything" until Nov. 20, 2026 (tests/test_robots_rfc9309.py has the rest)."""
    rc = RobotsCache(lambda url: (403, ""), "GACivicCrawler/0.3", warn=lambda *_: None)
    assert rc.allowed("https://x.example/") and not rc.failures and rc.unavailable == {"https://x.example": 403}
