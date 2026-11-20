"""Sources that refuse the crawler, read from the Internet Archive's copies (gacrawl/archive_sources.py): the owner's
rule since Nov. 19, 2026. A made-up archive; nothing is asked of the refusing site itself."""
import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

from gacrawl import archive_sources as ar
from gacrawl.store import Store

SITE = "https://www.smyrnaga.gov/"
PAGE = "https://www.smyrnaga.gov/government/city-council"
PDF = "https://www.smyrnaga.gov/home/showpublisheddocument/123/minutes-2026-10-06.pdf"
WALL = "https://www.smyrnaga.gov/parks"
IMG = "https://www.smyrnaga.gov/logo.png"


def pdf_bytes():
    from reportlab.pdfgen import canvas

    b = io.BytesIO(); c = canvas.Canvas(b)
    c.drawString(40, 800, "City Council Minutes October 6, 2026. Motion to approve the budget carried 7-0.")
    c.save(); return b.getvalue()


class Archive:
    def __init__(self):
        self.asked = []
        self.rows = [["urlkey", "timestamp", "original", "mimetype"],
                     ["k1", "20260101000000", PAGE, "text/html"], ["k1", "20260901000000", PAGE, "text/html"],
                     ["k2", "20261010000000", PDF, "application/pdf"], ["k3", "20260901000000", WALL, "text/html"],
                     ["k4", "20260901000000", IMG, "image/png"]]
        self.pages = {ar.RAW.format(ts="20260901000000", url=PAGE): ("<html><title>City Council</title><body><main><h1>City Council</h1>"
                                                                      + "<p>The council meets the first Monday of each month at City Hall.</p>" * 20 + "</main></body></html>").encode(),
                      ar.RAW.format(ts="20261010000000", url=PDF): pdf_bytes(),
                      ar.RAW.format(ts="20260901000000", url=WALL): b"<html><body>Request unsuccessful. Incapsula incident ID: 77</body></html>" + b" " * 2000}

    def get(self, url, params=None, **kw):
        self.asked.append(url)
        assert urlsplit(url).netloc == "web.archive.org" and "/save/" not in url, url
        if url == ar.CDX:
            return SimpleNamespace(status=200, json=lambda: self.rows, text=json.dumps(self.rows), content=b"", content_type="application/json")
        b = self.pages.get(url)
        return SimpleNamespace(status=200 if b else 404, content=b or b"", text=(b or b"").decode("latin-1"),
                               content_type="application/pdf" if (b or b"")[:5] == b"%PDF-" else "text/html")


def test_a_refusing_site_is_read_from_its_archived_copies(tmp_path):
    store = Store(tmp_path / "data")
    arc = Archive()
    src = {"id": "smyrna:site", "url": SITE, "kind": "page", "jurisdiction": "smyrna", "tier": "a"}
    out = ar.RefusedArchive(arc, store, log=lambda *a: None).run([src])
    assert out["sources"] == 1 and out["documents"] == 1 and out["pages"] == 1 and out["challenge_captures"] == 1
    recs = {r["meta"]["original"]: r for r in store.query("source_id='smyrna:site'")}
    assert set(recs) == {PAGE, PDF}
    p = recs[PAGE]
    assert p["url"] == ar.SHOW.format(ts="20260901000000", url=PAGE) and p["meta"]["captured"] == "2026-09-01"   # the newest capture
    assert p["jurisdiction"] == "smyrna" and p["meta"]["archived"]
    d = recs[PDF]
    assert d["kind"] == "document" and "approve the budget" in d["text"]
    assert not any(IMG in u for u in arc.asked)
    # read again only after RECHECK_DAYS
    n = len(arc.asked)
    ar.RefusedArchive(arc, store, log=lambda *a: None).run([src])
    assert len(arc.asked) == n
    store.close()


def test_time_running_out_in_the_middle_of_a_source_stops_the_step_cleanly(tmp_path):
    """Nov. 20, 2026, the first run with the Archive read again: the fetcher raised OutOfTime at a host's next turn,
    nothing caught it, and the step ended with a traceback and exit 1 after seven sources, its count never printed."""
    from gacrawl.fetch import OutOfTime

    class Slow(Archive):
        def get(self, url, params=None, **kw):
            if url != ar.CDX and sum(1 for u in self.asked if u != ar.CDX) >= 1:
                self.asked.append(url)
                raise OutOfTime("https://web.archive.org: next turn at +4s is past the deadline (the run's time budget)")
            return super().get(url, params=params, **kw)

    store = Store(tmp_path / "data")
    arc, said = Slow(), []
    src = {"id": "smyrna:site", "url": SITE, "kind": "page", "jurisdiction": "smyrna", "tier": "a"}
    other = {"id": "other:site", "url": "https://other.example.gov/", "kind": "page", "jurisdiction": "other", "tier": "b"}
    out = ar.RefusedArchive(arc, store, log=said.append).run([src, other])
    assert out["stopped"] == "out of time in smyrna:site" and out["sources"] == 0
    assert said == ["archive (refused): smyrna:site: out of time; the next run carries on with it"]
    # what was read before the time ran out is kept; the source is not stamped, and the one after it was not begun
    first = [r["meta"]["original"] for r in store.query("source_id='smyrna:site'")]
    assert len(first) == 1 and not store.kv_get("archive_src:smyrna:site") and not store.kv_get("archive_src:other:site")
    # the next run carries on: the page already read is not asked for again, the rest is read, the source is stamped
    arc2 = Archive()
    out = ar.RefusedArchive(arc2, store, log=lambda *a: None).run([src])
    assert out["sources"] == 1 and "stopped" not in out and store.kv_get("archive_src:smyrna:site")
    assert {r["meta"]["original"] for r in store.query("source_id='smyrna:site'")} == {PAGE, PDF}
    assert not any(first[0] in u for u in arc2.asked if u != ar.CDX)
    store.close()


def test_which_sources_count_as_refused():
    srcs = [{"id": "a:site", "url": "https://a.gov/"}, {"id": "b:site", "url": "https://b.gov/"},
            {"id": "c:site", "url": "https://c.gov/"}, {"id": "d:policies", "url": "https://simbli.eboardsolutions.com/x?S=1"},
            {"id": "e:site", "url": "https://e.gov/"}, {"id": "f:site", "url": "https://f.gov/"}]
    prog = {"a:site": {"status": "blocked: https://a.gov: HTTP 403 bot-protection challenge"},
            "b:site": {"status": "stopped: robots.txt disallows https://b.gov/ (robots.txt disallows this path for our user agent)"},
            "c:site": {"status": "ok"},
            "d:policies": {"status": "blocked: https://simbli.eboardsolutions.com: HTTP 200 bot-protection challenge"},
            "e:site": {"status": "blocked: https://e.gov: HTTP 401 login required"},
            "f:site": {"status": "stopped: robots.txt disallows https://f.gov/ (unreachable (ConnectTimeout))"}}
    assert [s["id"] for s in ar.refused(prog, srcs)] == ["a:site", "b:site"]
