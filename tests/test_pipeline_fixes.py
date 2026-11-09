"""Nov 9 fixes to the pipeline's weak spots: the host spacing that climbed to its cap on fast-and-slow APIs (EFile,
GovInfo), the legislature step that a token refusal kept busy for the rest of its minutes, and the checks that go
with the later fixes in this file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gacrawl.fetch import Fetcher
from gacrawl.polite import RateLimiter
from gacrawl.store import Record, Store


# --------------------------------------------------------------------------------------------- host spacing
def test_a_slow_endpoint_is_compared_with_itself_not_with_a_quick_lookup():
    """EFile: a 0.2 s lookup, then 100-line searches at 5 s each. Compared with the lookup, every search looked like a
    strained server and the spacing sat at 16x (48 s a page); compared with itself, it stays at the base."""
    rl = RateLimiter(3.0, sleep=lambda s: None)
    rl.record("https://api.example/api/Lookup/Years", 200, 0.2, kind="api:/api/Lookup/Years")
    for _ in range(30):
        rl.record("https://api.example/api/Search/Get", 200, 5.0, kind="api:/api/Search/Get")
    assert rl.interval("https://api.example") <= 3.0 * 1.25


def test_one_kind_that_starts_quick_learns_the_endpoint_slowly_and_recovers():
    """Even within one kind, a first answer that happened to be quick no longer pins the spacing at the cap: slow
    answers double it (polite at once) but the baseline learns, and the spacing comes back down."""
    rl = RateLimiter(3.0, sleep=lambda s: None)
    rl.record("https://h.example/listing", 200, 0.3)
    peak = 0.0
    for _ in range(60):
        rl.record("https://h.example/listing", 200, 6.0)
        peak = max(peak, rl.interval("https://h.example"))
    assert peak >= 12.0                                   # it did slow down
    assert rl.interval("https://h.example") <= 3.0 * 1.25  # and recovered once 6 s was the norm


def test_a_refusal_still_doubles_the_spacing_and_teaches_nothing():
    rl = RateLimiter(2.0, sleep=lambda s: None)
    rl.record("https://h.example/x", 200, 0.5)
    rl.record("https://h.example/x", 429, 0.5)
    rl.record("https://h.example/x", 503, 0.5)
    assert rl.interval("https://h.example") == 8.0


def test_latency_kinds_split_an_api_by_endpoint(tmp_path):
    f = Fetcher("t", tmp_path, api_endpoints=["api.open.fec.gov/v1"])
    assert f.latency_kind("https://www.govinfo.gov/bulkdata/json/BILLSTATUS/119/hr", {"Accept": "application/json"}) == \
        f.latency_kind("https://www.govinfo.gov/bulkdata/json/BILLSTATUS/118/hr", {"Accept": "application/json"})
    assert f.latency_kind("https://www.legis.ga.gov/api/authentication/token") != \
        f.latency_kind("https://www.legis.ga.gov/api/Vote/detail/71203")
    assert f.latency_kind("https://www.legis.ga.gov/api/Vote/detail/1") == f.latency_kind("https://www.legis.ga.gov/api/Vote/detail/2")
    assert f.latency_kind("https://example.com/about") == "page"
    assert f.latency_kind("https://example.com/a.pdf", is_doc=True) == "doc"
    assert f.latency_kind("https://x.example/y", kind="bill") == "bill"


# --------------------------------------------------------------------------------------------- the legislature
class _Resp:
    def __init__(self, status, body=None):
        self.status, self._body, self.content = status, body, b"x"

    def json(self):
        return self._body


class _RefusingLegis:
    """legis.ga.gov answering every token request with 401 (Nov 9, 10:36-10:42 UTC)."""

    def __init__(self):
        self.token_calls = 0
        self.calls = 0

    def get(self, url, **kw):
        if url.endswith("authentication/token"):
            self.token_calls += 1
            return _Resp(401)
        self.calls += 1
        return _Resp(200, {})

    def post(self, url, **kw):
        self.calls += 1
        return _Resp(200, {})

    def deadline(self):
        return None


def test_a_token_refusal_stops_the_step_instead_of_costing_every_bill(tmp_path, monkeypatch):
    from gacrawl.sources import legis as L

    monkeypatch.setattr(L.Legis, "TOKEN_WAITS", (0, 0, 0))
    (tmp_path / "config").mkdir()
    keys = [{"session": 1031, "bill": f"HB {n}"} for n in range(1, 31)]
    (tmp_path / "config" / "legislature.yaml").write_text(json.dumps({"sessions": {"current": [], "history": []},
                                                                     "votes": {"key_bills": keys}}))
    store = Store(tmp_path / "data")
    for n in range(1, 31):
        store.add(Record(L.SOURCE, "bill", f"{L.SITE}/legislation/{n}", title=f"HB {n}", key=f"{L.SOURCE}|bill|{n}",
                         meta={"legislation_id": n, "number": f"HB {n}", "session": 1031, "status": "Signed"}))
    store.flush()
    f = _RefusingLegis()
    out = L.update(f, store, tmp_path, log=lambda *_: None)
    assert out.get("refused")
    assert f.token_calls == 3          # one round of tries, not three for each of the 30 bills
    hist = json.loads(store.kv_get("legis_refusals"))
    assert hist[-1]["refused"] is True
    store.close()


def test_after_a_refusal_no_token_is_asked_for_again_this_run(monkeypatch):
    from gacrawl.sources.legis import Legis, TokenRefused

    monkeypatch.setattr(Legis, "TOKEN_WAITS", (0, 0, 0))
    f = _RefusingLegis()
    api = Legis(f, log=lambda *_: None)
    with pytest.raises(TokenRefused):
        api.token()
    with pytest.raises(TokenRefused):
        api.token()
    assert f.token_calls == 3 and api.refusals == 1
