"""The Macon-Bibb County Transit Authority read from the Internet Archive's copies (Nov. 20, 2026): its site refuses
automated readers, its current notices page has no copy, and the minutes page names its files by month alone
("MTA Board June 2025 Minutes"), so only one meeting reached the site. The notices of 2025 stood under
/category/governance-and-management/board-meetings/, of which the Archive keeps copies; they give each meeting's day,
and the minutes are filed under those days. The test pages are cut from the copies the runner's probe fetched."""
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from gacrawl import agencies as A
from gacrawl.blocked import Blocked
from gacrawl.store import Store

FX = Path(__file__).parent / "fixtures" / "agencies"
ROOT = Path(__file__).resolve().parents[1]
G = "https://mbcta.net/category/governance-and-management/board-meetings/"
MIN = "https://mbcta.net/board-meeting-minutes/"
OCT = "https://mbcta.net/october-2025-board-meeting/"


def page(name):
    return (FX / name).read_text()


def test_an_articles_own_header_is_its_title_not_the_sites():
    ms = A.notice_posts(page("mbcta_governance_page1_wayback.html"), G, {}, {})
    assert [(m["date"], m["time"], m["page"]) for m in ms] == [
        ("2025-08-26", "17:30", "https://mbcta.net/august-2025-board-meeting/"), ("2025-07-29", "17:30", "https://mbcta.net/july-2025-board-meeting/"),
        ("2025-06-24", "17:30", "https://mbcta.net/june-2025-board-meeting/")]
    assert [m["where"][:16] for m in ms] == ["Terminal Station", "Terminal Station", ""]     # no "at " before the place (June's names none)
    lines = [l["t"] for l in A.page_lines(page("mbcta_governance_page1_wayback.html"), G)]
    assert "August 2025 Board Meeting" in lines and "MBCTA Services Fixed Route Buses" not in lines   # the site's header still goes


def test_a_posts_own_page_is_read_only_when_set_down_so():
    html = page("mbcta_october_2025_post_wayback.html")
    assert A.notice_posts(html, OCT, {}, {}) == []
    (m,) = A.notice_posts(html, OCT, {}, {"post": True})
    # the day it opens with, not the conference the agenda names ("in Augusta on November 19-21, 2025")
    assert (m["date"], m["time"], m["page"]) == ("2025-10-28", "17:30", OCT)


def test_the_minutes_named_by_month_are_filed_under_the_notices_days(tmp_path, monkeypatch):
    import gacrawl.wayback as W

    copies = {G: ("mbcta_governance_page1_wayback.html", "20251024201514"), G + "page/3/": ("mbcta_governance_page3_wayback.html", "20251202144452"),
              OCT: ("mbcta_october_2025_post_wayback.html", "20251207032615"), MIN: ("mbcta_minutes_wayback.html", "20260212100639")}
    monkeypatch.setattr(W, "Wayback", lambda *a, **k: SimpleNamespace(off=""))
    monkeypatch.setattr(A, "read_archived", lambda wb, u, copies_=4: (page(copies[u][0]), copies[u][1]) if u in copies else (None, ""))

    class Refusing:
        def get(self, url, **k):
            raise Blocked("https://mbcta.net", "HTTP 403 bot-protection challenge")

    store = Store(tmp_path / "data")
    try:
        res = A.run(Refusing(), store, ROOT, only=["macon-transit"], log=lambda *a: None, minutes_cap=0)
        out = tmp_path / "sm.json"
        A.export_state_meetings(store, ROOT, out, today="2026-11-20", log=lambda *a: None)
        b = next(x for x in json.loads(out.read_text())["bodies"] if x["id"] == "macon-transit")
        got = {r["d"]: r for r in b["meetings"]}
        assert sorted(got) == ["2024-01-30", "2025-02-25", "2025-03-25", "2025-04-29", "2025-06-24", "2025-07-29", "2025-08-26", "2025-10-28"], sorted(got)
        for d, month in (("2025-10-28", "October-2025"), ("2025-08-26", "August-2025"), ("2025-07-29", "July-2025"), ("2025-06-24", "June-2025"),
                         ("2025-03-25", "March-2025"), ("2025-02-25", "February-2025"), ("2024-01-30", "January-30-2024")):
            assert got[d].get("m", "").endswith(f"MTA-Board-{month}-Minutes.pdf"), (d, got[d])
        assert got["2025-08-26"]["t"] == "17:30" and got["2025-08-26"]["w"].startswith("Terminal Station")
        assert b["source"] == "archive" and b["archive_as_of"] == "2026-02-12"
        assert all(r["p"].startswith("https://") for r in b["meetings"])
        # the site's own refusal is said; nothing is asked of the site but its pages, and nothing is saved at the Archive
        assert any("refused" in e for e in res["errors"])
        assert any("could not be given a day" in n for n in res["notes"])          # the months with no notice on any copy
    finally:
        store.close()


def test_the_config_reads_the_notices_before_the_minutes():
    ag = next(a for a in yaml.safe_load((ROOT / "config" / "agencies.yaml").read_text())["agencies"] if a["id"] == "macon-transit")
    us = [e["u"] if isinstance(e, dict) else e for e in ag["pages"]]
    assert us[-1] == MIN and us.index(G) < us.index(MIN)
    arch = [e for e in ag["pages"] if isinstance(e, dict) and e.get("archive")]
    assert len(arch) == 6 and all(e["reader"] == "notice_posts" for e in arch)
    assert {e["u"] for e in arch if e.get("post")} == {"https://mbcta.net/september-2025-board-meeting/", OCT}
