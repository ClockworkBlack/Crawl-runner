"""Every county's sitting sheriff from the Georgia Sheriffs' Association's list (gacrawl/sheriffs.py): read from the
association's site, and from the Internet Archive's newest copy where the site refuses the crawler. Made-up answers;
nothing is asked of either site."""
import json
from types import SimpleNamespace

from gacrawl import sheriffs as sh
from gacrawl.blocked import Blocked
from gacrawl.wayback import CDX, RAW

import itertools
COUNTIES = ["Tst" + a + b for a, b in itertools.islice(itertools.product("abcdefghijklm", repeat=2), 150)] + ["Appling", "Ben Hill", "McDuffie", "Hall", "Harris", "Jasper", "Fulton", "Jeff Davis", "McIntosh"]


def table(extra_rows=""):
    rows = "".join(f"<tr><td>{c.upper()}</td><td>{c.upper()}</td><td>Sheriff Ann Person{i}</td>"
                   f"<td>{i} Main Street, Town, GA 30{i:03d}</td><td>706-555-{i:04d}</td></tr>" for i, c in enumerate(COUNTIES[:150]))
    return ("<html><body><table><tr><th>County</th><th>County</th><th>Sheriff</th><th>Address</th><th>Phone Number</th></tr>"
            "<tr><td>APPLING</td><td>APPLING</td><td>Sheriff Mark Melton</td><td>560 Barnes Street, Suite B, Baxley, GA 31513</td><td>912-367-8120</td></tr>"
            "<tr><td>BEN HILL</td><td>BEN HILL</td><td>Sheriff Lee Cone</td><td>PO Box 738, Fitzgerald, GA 31750</td><td>229-426-5161</td></tr>"
            "<tr><td>McDUFFlE</td><td>McDUFFlE</td><td>Sheriff Logan Marshall</td><td>751 Public Safety Drive, Thomson, GA 30824</td><td>706-595-2040</td></tr>"
            "<tr><td>McINTOSH</td><td>McINTOSH</td><td>Sheriff Thornell “T.K.” King</td><td>12317 Hwy 251, Darien, GA 31305</td><td>912-437-5870</td></tr>"
            "<tr><td>HARRIS</td><td>HARRIS</td><td>Interim Sheriff Pat Newcomer</td><td>PO Box 286, Hamilton, GA 31811</td><td>706-628-4211</td></tr>"
            + rows + extra_rows + "</table></body></html>")


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("gacrawl.registry.jurisdictions", lambda root: [{"id": f"co-{c.lower()}", "name": f"{c} County", "kind": "county"} for c in COUNTIES]
                        + [{"id": "baxley", "name": "Baxley", "kind": "city"}])
    (tmp_path / "state").mkdir()
    return tmp_path / "state"


def test_the_list_is_read_with_every_county_matched(tmp_path, monkeypatch):
    rows = sh.parse(table(), COUNTIES)
    assert rows["Appling"] == {"name": "Mark Melton", "title": "Sheriff", "address": "560 Barnes Street, Suite B, Baxley, GA 31513", "phone": "912-367-8120"}
    assert rows["McDuffie"]["name"] == "Logan Marshall"                    # "McDUFFlE": a lower-case L for the i
    assert rows["Ben Hill"]["name"] == "Lee Cone" and rows["McIntosh"]["name"] == 'Thornell "T.K." King'
    assert rows["Harris"]["title"] == "Interim Sheriff" and rows["Harris"]["name"] == "Pat Newcomer"
    assert "County" not in rows and len(rows) == 155


def test_the_associations_site_first(tmp_path, monkeypatch):
    state = setup(tmp_path, monkeypatch)

    class Site:
        asked = []

        def get(self, url, params=None, **kw):
            self.asked.append(url)
            assert url == sh.URL
            return SimpleNamespace(status=200, text=table(), content=b"", content_type="text/html")

    f = Site()
    out = sh.read(f, tmp_path, state, log=lambda *a: None)
    assert out["read"] and out["via"] == "site" and out["counties"] == 155 and "Hall" in out["missing"]
    d = json.loads((state / "sheriffs.json").read_text())
    assert d["sheriffs"]["Appling"]["src"] == sh.URL and d["via"] == "site"
    assert sh.read(f, tmp_path, state, log=lambda *a: None) == {"counties": 155, "read": False}   # once a week
    assert len(f.asked) == 1


def test_a_refusing_site_is_read_from_the_archives_newest_copy(tmp_path, monkeypatch):
    state = setup(tmp_path, monkeypatch)
    import datetime as dt
    ts_new = (dt.date.today() - dt.timedelta(days=40)).strftime("%Y%m%d") + "120000"
    ts_old = (dt.date.today() - dt.timedelta(days=90)).strftime("%Y%m%d") + "120000"
    rows = [["urlkey", "timestamp", "original"], ["k", ts_old, sh.URL], ["k", ts_new, sh.URL]]

    class Net:
        asked = []

        def get(self, url, params=None, **kw):
            self.asked.append(url)
            assert "/save/" not in url
            if url == sh.URL:
                raise Blocked("bot protection (403)")
            if url == CDX:
                return SimpleNamespace(status=200, json=lambda: rows, text=json.dumps(rows), content=b"", content_type="application/json")
            if url == RAW.format(ts=ts_new, url=sh.URL):
                return SimpleNamespace(status=200, text=table(), content=b"", content_type="text/html")
            return SimpleNamespace(status=404, text="", content=b"", content_type="text/html")

    n = Net()
    out = sh.read(n, tmp_path, state, log=lambda *a: None)
    assert out["read"] and out["via"] == "wayback"
    d = json.loads((state / "sheriffs.json").read_text())
    a = d["sheriffs"]["Appling"]
    assert a["via"] == "wayback" and a["captured"] == f"{ts_new[:4]}-{ts_new[4:6]}-{ts_new[6:8]}" and "web.archive.org/web/" + ts_new in a["src"]
    assert RAW.format(ts=ts_old, url=sh.URL) not in n.asked                  # the newest copy that reads is enough


def test_a_short_list_does_not_replace_the_last_one(tmp_path, monkeypatch):
    state = setup(tmp_path, monkeypatch)
    (state / "sheriffs.json").write_text(json.dumps({"as_of": "2020-01-01T00:00:00Z", "sheriffs": {"Appling": {"name": "Mark Melton"}}}))

    class Short:
        def get(self, url, params=None, **kw):
            if url == sh.URL:
                return SimpleNamespace(status=200, text="<table><tr><td>APPLING</td><td>Sheriff X Y</td></tr></table>", content=b"", content_type="text/html")
            return SimpleNamespace(status=404, text="", content=b"", content_type="text/html", json=lambda: [])

    out = sh.read(Short(), tmp_path, state, log=lambda *a: None)
    assert not out["read"] and json.loads((state / "sheriffs.json").read_text())["sheriffs"] == {"Appling": {"name": "Mark Melton"}}
