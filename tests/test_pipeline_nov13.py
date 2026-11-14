"""Nov 13, 2026 fixes: the export that ran the runner out of memory, packet sources, Georgia's runoff outcomes in the
election history, link text read as a sheriff's name, undated and mis-worded council roll calls, the coverage report's
FEC check, deriver versions and the job clock's export-only mode."""
import json
import os
import subprocess
from pathlib import Path

from gacrawl import derive, police
from gacrawl.electo import build_packet, source_key
from gacrawl.export import StoredRecords, candidate_file
from gacrawl.history import build as hbuild
from gacrawl.history.sources import medsl_contests
from gacrawl.minutes import meeting_date, parse_motions
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent


def _money(store, sid, n, committee="C00000001"):
    for i in range(n):
        store.add(Record(sid, "contribution", f"https://docquery.fec.gov/cgi-bin/fecimg/?2020{i:010d}", title=f"Donor {i}",
                         key=f"fec2|{committee}|{i}", meta={"type": "contribution", "name": f"DONOR {i}", "amount": float(10 + i),
                                                           "date": "2020-06-01", "filing_source": "FEC", "fec_v": 2,
                                                           "committee": committee, "candidate_id": sid[5:],
                                                           "sub_id": str(i), "line_number": "11AI"}))
    store.flush()


def test_streamed_records_and_capped_candidate_file(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    _money(store, "cand:jane-doe", 30)
    store.add(Record("cand:jane-doe", "contribution", "https://x.gov/hidden", key="hidden", evidence="unverified",
                     meta={"type": "contribution", "amount": 99999.0}))
    store.flush()
    n = store.counts("kind", "source_id=? AND evidence<>'unverified' AND kind IN ('contribution')", ("cand:jane-doe",))["contribution"]
    lines = StoredRecords(store, "cand:jane-doe", "contribution", n)
    assert len(lines) == 30 and bool(lines) and len(list(lines)) == 30 and len(list(lines)) == 30   # read again each time
    assert not StoredRecords(store, "cand:nobody", "contribution", 0) and list(StoredRecords(store, "cand:nobody", "contribution", 0)) == []
    import gacrawl.export as ex
    monkeypatch.setattr(ex, "CANDIDATE_FILE_LINES", 5)
    body = candidate_file({"id": "jane-doe"}, {"contribution": lines, "statement": [{"key": "k", "text": "t", "markdown": "t"}]})
    kept = [r["meta"]["amount"] for r in body["records"]["contribution"]]
    assert kept == [39.0, 38.0, 37.0, 36.0, 35.0]                      # the largest, never the unverified line
    assert body["capped"]["contribution"]["kept"] == 5 and body["capped"]["contribution"]["of"] == 30
    assert body["records"]["statement"] == [{"text": "t"}]           # other kinds as before (slimmed)


def test_packet_reads_streamed_money_and_cites_each_filing_page(tmp_path):
    store = Store(tmp_path / "data")
    _money(store, "cand:jane-doe", 250)
    c = {"id": "jane-doe", "name": "Jane Doe", "electo_key": "JANE DOE"}
    as_list = {"contribution": list(store.read("cand:jane-doe", "contribution"))}
    streamed = {"contribution": StoredRecords(store, "cand:jane-doe", "contribution", 250)}
    a, b = build_packet(c, as_list, [], set()), build_packet(c, streamed, [], set())
    assert [x["amount"] for x in a["finance"]] == [x["amount"] for x in b["finance"]] and len(b["finance"]) == 200
    assert b["finance"][0]["amount"] == 259.0
    # every line cites its own filing page (the query is the page): until Nov 13 all 200 cited the first page read
    assert len({b["sources"][x["src"][0]]["u"] for x in b["finance"]}) == 200 and len(b["sources"]) == 200
    assert source_key("https://www.ajc.com/a/?utm_source=x") == source_key("https://ajc.com/a") == "ajc.com/a"
    assert source_key("http://docquery.fec.gov/cgi-bin/fecimg/?1") != source_key("http://docquery.fec.gov/cgi-bin/fecimg/?2")


GA_SENATE = """year\tstate\tstate_po\toffice\tdistrict\tstage\tspecial\tcandidate\tparty_detailed\twritein\tcandidatevotes\ttotalvotes\tparty_simplified
2020\tGEORGIA\tGA\tUS SENATE\tstatewide\tgen\tFALSE\tDAVID A. PERDUE\tREPUBLICAN\tFALSE\t2462617\t4952175\tREPUBLICAN
2020\tGEORGIA\tGA\tUS SENATE\tstatewide\tgen\tFALSE\tJON OSSOFF\tDEMOCRAT\tFALSE\t2374519\t4952175\tDEMOCRAT
2020\tGEORGIA\tGA\tUS SENATE\tstatewide\tgen\tFALSE\tSHANE HAZEL\tLIBERTARIAN\tFALSE\t115039\t4952175\tLIBERTARIAN
2020\tALABAMA\tAL\tUS SENATE\tstatewide\tgen\tFALSE\tTOMMY TUBERVILLE\tREPUBLICAN\tFALSE\t1392076\t2325000\tREPUBLICAN
2020\tALABAMA\tAL\tUS SENATE\tstatewide\tgen\tFALSE\tDOUG JONES\tDEMOCRAT\tFALSE\t920478\t2325000\tDEMOCRAT
"""


def _contest(**kw):
    c = {"scope": "us", "st": "GA", "office": "U.S. Senate", "dist": "", "lvl": "federal", "p": "", "j": "", "t": "U.S. Senate · Georgia",
         "basis": "compiled", "src": {"t": "s", "u": "u"}}
    c.update(kw)
    return c


def test_georgia_needs_a_majority_and_the_top_two_go_to_the_runoff():
    cs = {c["st"]: c for c in medsl_contests(GA_SENATE, "senate", "https://doi.org/x")}
    assert cs["GA"]["o"][0][4] == "led" and cs["AL"]["o"][0][4] == "won"          # Georgia's majority rule; Alabama's plurality
    for c in cs.values():
        c["item"] = "medsl:senate:1:1"
    chosen = hbuild.choose(list(cs.values()))
    hbuild.link_runoffs(chosen)
    ga = next(c for c in chosen if c["st"] == "GA")
    assert [x[4] for x in ga["o"]] == ["runoff", "runoff", ""]                    # the federal files hold no January runoff


def test_a_runoff_filed_as_the_general_and_presidential_plurality():
    # MEDSL's 2008 Georgia Senate "general" is the December runoff; the FEC's general has no majority
    medsl = _contest(y=2008, date="2008-11-04", stage="general", tot=2137956, item="medsl:senate:1:1",
                     o=[["Saxby Chambliss", "R", 1228033, 0, "won"], ["Jim Martin", "D", 909923, 0, ""]])
    fec = _contest(y=2008, date="2008-11-04", stage="general", tot=3752485, item="fec:2008", basis="official",
                   o=[["Saxby Chambliss", "R", 1867097, 0, "won"], ["Jim Martin", "D", 1757393, 0, ""], ["Allen Buckley", "L", 127995, 0, ""]])
    audit = {}
    chosen = hbuild.choose([medsl, fec], audit)
    gen = next(c for c in chosen if c["stage"] == "general")
    run = next(c for c in chosen if c["stage"] == "general runoff")
    assert gen["tot"] == 3752485 and run["tot"] == 2137956 and run["chk"] == "filed as the general"
    assert audit["runoffs_filed_as_general"][0]["y"] == 2008
    for x in gen["o"]:                                    # the FEC's own "W" is the runoff's winner: Georgia's rule decides
        x[4] = "led" if x[0] == "Saxby Chambliss" else ""
    hbuild.link_runoffs(chosen)
    assert [x[4] for x in gen["o"][:2]] == ["runoff", "runoff"] and gen["ro_date"] == "2008"
    assert run["o"][0][4] == "won"


def test_seated_elsewhere_without_a_majority_is_led():
    # Louisiana's 3rd, 2004: the November leader had 32 percent and lost the December runoff
    kept = _contest(st="LA", t="U.S. House · Louisiana District 3", office="U.S. House", dist="3", y=2004, date="2004-11-02",
                    stage="general", tot=264620, item="fec:2004", basis="official",
                    o=[["Billy Tauzin III", "R", 84680, 0, "won"], ["Charlie Melancon", "D", 63339, 0, ""]])
    seated = _contest(st="LA", t="U.S. House · Louisiana District 3", office="U.S. House", dist="3", y=2004, date="2004-11-02",
                      stage="general", tot=None, item="legislators:h", basis="seated", o=[["Charles J. Melancon", "D", None, 0, "won"]])
    audit = {}
    chosen = hbuild.choose([kept, seated], audit)
    assert chosen[0]["o"][0][4] == "led" and chosen[0]["chk"] == "seated differs" and audit["seated_differs"][0]["now"] == "led"


def test_fec_footnote_marks_join_a_candidates_party_lines():
    c = _contest(st="NY", t="U.S. House · New York District 18", office="U.S. House", dist="18", y=2012, stage="general",
                 tot=276894, o=[["Nan Hayworth", "R", 133049, 1, "won"], ["Sean Patrick Maloney", "D", 132456, 0, ""],
                                ["Sean Patrick # Maloney", "O", 11389, 0, ""]])
    hbuild._merge_marked_lines(c)
    assert [(x[0], x[2], x[4]) for x in c["o"]] == [("Sean Patrick Maloney", 143845, "won"), ("Nan Hayworth", 133049, "")]


def test_link_words_are_never_a_name():
    for bad in ("Read More", "Learn More", "Click Here", "View Bio", "See More", "More Info"):
        assert not police._name_ok(police._tidy_name(bad)), bad
    for good in ("Patrick Labat", "Thomas More", "Herbert Read", "Mary Jo Smith"):
        assert police._name_ok(police._tidy_name(good)), good
    assert police._tidy_name("Email Janet Moon") == "Janet Moon"
    lines = ["Sheriff", "Read More", "Sheriff Patrick Labat"]
    assert police.find_chief(lines)["name"] == "Patrick Labat"


def test_minutes_heading_date_and_closing_lines():
    assert meeting_date("Minutes Regular Session: Monday, May 11th, 2026 at 7 PM ... approve the minutes for April 13th, 2026") == "2026-05-11"
    assert meeting_date("MINUTES OF THE CITY OF HULL MAYOR AND COUNCIL February 27,2025 The meeting") == "2025-02-27"
    assert meeting_date("Official Meeting Minutes ... February 15th, 2010 The Commission met") == "2010-02-15"
    assert meeting_date("x" * 700 + " May 1, 2026") is None                     # only the heading
    text = ("MINUTES OF THE CITY OF HULL MAYOR AND COUNCIL February 27,2025 Those present were the Mayor, and council members "
            "Mike McElroy, Paige Phillips, and Steven Hollingsworth. There being no further business the meeting was adjourned "
            "after a motion by Paige Phillips and a second by Steven Hollingsworth. Re pectfully submitted, Aa/laPoit andra "
            "Pou Hull City Clerk")
    mo = parse_motions(text)
    assert mo and not any("submitted" in m["motion"].lower() for m in mo)
    assert "adjourned" in mo[-1]["motion"]


def test_a_new_deriver_version_reads_everything_again(tmp_path):
    store = Store(tmp_path / "data")
    store.add(Record("x:site", "document", "https://x.gov/a", text="t", key="a"))
    store.flush()
    seen = []
    d = derive.Deriver("t_nov13", "kind='document'", lambda src, ctx: seen.append(src["id"]) or [], version="1")
    derive.REGISTRY.append(d)
    try:
        derive.run(store, only={"t_nov13"}, log=lambda *_: None)
        derive.run(store, only={"t_nov13"}, log=lambda *_: None)          # nothing changed: nothing read
        assert len(seen) == 1
        d.version = "2"
        derive.run(store, only={"t_nov13"}, log=lambda *_: None)          # a new version: read again, once
        derive.run(store, only={"t_nov13"}, log=lambda *_: None)
        assert len(seen) == 2
    finally:
        derive.REGISTRY.remove(d)


def test_job_clock_reserve_and_export_only():
    env = {**os.environ, "MERGE_LIMIT": "350", "MERGE_T0": "0"}
    run = lambda e, want: subprocess.run(["bash", str(ROOT / "scripts" / "budget.sh"), want], env=e, capture_output=True,  # noqa: E731
                                         text=True).stdout.strip()
    import time
    env["MERGE_T0"] = str(int(time.time()))
    assert run(env, "500") == "240"                                      # 350 - 110 reserved
    assert run({**env, "EXPORT_ONLY": "true"}, "25") == "0"
