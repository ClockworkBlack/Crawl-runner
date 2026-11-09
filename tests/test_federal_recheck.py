import json

from gacrawl import federal_bills as fb


class KV:
    def __init__(self, d):
        self.d = d

    def kv_get(self, k):
        return self.d.get(k)

    def kv_set(self, k, v):
        self.d[k] = v


class NoNet:
    asked: list = []

    def get(self, url, **kw):
        self.asked.append(url)
        raise RuntimeError("offline")


def test_closed_congress_listings_are_not_fetched_every_run(monkeypatch):
    now = fb.congress_now()
    kv = {f"fedbills:{c}:{t}": json.dumps({"zip": "x", "files": {}}) for c in range(fb.FIRST, now) for t in fb.TYPES}
    f = NoNet()
    f.asked = []
    st = fb.run(f, KV(kv), deadline=10**12, log=lambda *a: None)
    assert all(f"/{now}/" in u for u in f.asked)            # only the Congress in session was asked for
    assert all(json.loads(kv[k]).get("checked") for k in kv if not k.startswith(f"fedbills:{now}:"))
