"""Minutes -> motions and votes; Legistar sponsors; stances / ideology / endorsements / ratings / career;
the profile supplement Electo's inference reads; export of profiles and connections. Fictional names; local stubs."""
import http.server
import json
import threading
from pathlib import Path

import pytest
import requests

from gacrawl import derive, minutes, stances
from gacrawl.candidates import Researcher
from gacrawl.fetch import Fetcher
from gacrawl.names import name_variants
from gacrawl.sources.legistar import fill_sponsors
from gacrawl.store import Record, Store

MINUTES = """CITY OF CANTON
REGULAR COUNCIL MEETING MINUTES
October 20, 2026

Members present: Mayor Dana Reed, Council Member Pat Local, Council Member Kim Post, Council Member Lee Ward, Council Member Sam Third. Absent: none.

4. Ordinance No. 2026-14, amending Chapter 18 regarding short-term rentals.
Motion by Council Member Local, seconded by Council Member Post, to adopt Ordinance No. 2026-14. Motion carried 3-1 (Ward opposed).

5. Resolution 2026-40, FY27 budget amendment.
Council Member Ward moved to approve Resolution 2026-40, seconded by Council Member Third. Ayes: Ward, Third, Post. Nays: Local. Motion passed.

6. Rezoning application Z-26-07 for 12 acres on Elm Street for a data center.
A motion to table the rezoning was made by Council Member Third, seconded by Council Member Local. The motion carried unanimously.

7. Discussion of the parks master plan. No action was taken.

8. Alcohol license for Elm Street Tavern. Motion: Post. Second: Third. Roll call: Local - Yes; Post - Yes; Third - Yes; Ward - No. Motion carried 3-1.

9. Motion by Council Member Ward to adjourn. Seconded by Council Member Local. Vote: 4-0.
"""

SITE_ABOUT = """About Pat Local. I am a lifelong conservative Republican and a member of the Cherokee County Republican Party.
I served on the Canton Planning Commission from 2014 to 2018 and was elected to the City Council in 2019.
I will fight to lower property taxes and keep our millage rate flat. I oppose the proposed data center on Elm Street until a water study is done.
I believe every child deserves a great public school, and I support raising teacher pay.
Proud to be endorsed by the Georgia Chamber of Commerce and Sheriff Dana Reed. I earned an A rating from the NRA in 2024.
Click here to donate now. Paid for by Friends of Pat Local."""

ARTICLE = """CANTON — Council member Pat Local said Tuesday she would vote against the rezoning for the Acme data center.
"We cannot support this without a water study," Local told the Tribune. The Georgia Chamber endorsed Local in September.
Her opponent, Kim Post, supports the project. Local describes herself as a fiscal conservative."""

TRANSCRIPT = """[00:00:03] MAYOR: Good evening.
[00:31:40] PAT LOCAL: I cannot support a data center on that parcel without a water study.
[00:32:10] KIM POST: I support it, we need the tax base.
[01:10:02] CLERK: Motion passes."""

LEGISTAR = {
    "/v1/canton/matters/5/sponsors": [{"MatterSponsorName": "Pat Local", "MatterSponsorNameId": 11, "MatterSponsorSequence": 1},
                                      {"MatterSponsorName": "Kim Post", "MatterSponsorNameId": 12, "MatterSponsorSequence": 2}],
    "/v1/canton/matters/6/sponsors": [],
}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            body, ct = b"User-agent: *\nAllow: /\n", "text/plain"
        elif path in LEGISTAR:
            body, ct = json.dumps(LEGISTAR[path]).encode(), "application/json"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def test_minutes_motions_and_votes():
    ms = minutes.parse_motions(MINUTES)
    assert [m["ordinance_ref"] for m in ms[:2]] == ["Ordinance 2026-14", "Resolution 2026-40"]
    m4 = ms[0]
    assert m4["mover"] == "Local" and m4["seconder"] == "Post" and m4["result"] == "passed" and m4["tally"] == "3-1"
    v = {x["person"]: (x["vote"], x["basis"]) for x in m4["votes"]}
    assert v == {"Lee Ward": ("no", "named"), "Pat Local": ("yes", "moved"), "Kim Post": ("yes", "seconded"), "Sam Third": ("yes", "tally")}
    assert "Dana Reed" not in v                                   # the mayor presides; 3-1 adds up without her
    m5 = ms[1]
    assert {x["person"]: x["vote"] for x in m5["votes"]} == {"Lee Ward": "yes", "Sam Third": "yes", "Kim Post": "yes", "Pat Local": "no"}
    m6 = ms[2]
    assert m6["unanimous"] and m6["motion"].startswith("table the rezoning")
    assert {x["person"] for x in m6["votes"]} == {"Pat Local", "Kim Post", "Lee Ward", "Sam Third"}
    assert next(x for x in m6["votes"] if x["person"] == "Kim Post")["basis"] == "unanimous"
    m8 = ms[3]
    assert m8["motion"] == "Alcohol license for Elm Street Tavern" and {x["person"]: x["vote"] for x in m8["votes"]}["Lee Ward"] == "no"
    assert ms[4]["procedural"] and ms[4]["result"] == "passed"
    assert all(not m["procedural"] for m in ms[:4])


def test_profile_supplement_end_to_end(server, fetcher, tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    for f in ("jurisdictions.yaml", "outlets.yaml", "topics.yaml", "issues.yaml", "finance.yaml", "organizations.yaml"):
        (root / "config" / f).write_text((Path(__file__).resolve().parent.parent / "config" / f).read_text())
    store = Store(root / "data")
    # official records: minutes PDF, a Legistar ordinance (sponsors to fill), a transcript
    store.add(Record("canton:site", "document", "https://canton.example/minutes/2026-10-20.pdf", title="October 20, 2026 Council Minutes",
                     text=MINUTES, published="2026-10-20", jurisdiction="canton", channel="pdf", evidence="primary", meta={"doc_type": "minutes"}))
    base = server + "/v1/canton"
    for mid, title, mtype in ((5, "An ordinance to require a water study for data centers", "Ordinance"), (6, "Approving the FY27 budget", "Resolution")):
        store.add(Record("canton:legistar", "ordinance" if mtype == "Ordinance" else "matter", f"{base}/matters/{mid}", title=title, text=title,
                         published="2026-09-01", jurisdiction="canton", channel="api", evidence="primary",
                         meta={"MatterId": mid, "MatterFile": f"26-{mid}", "MatterTypeName": mtype, "MatterStatusName": "Adopted",
                               "MatterEnactmentDate": "2026-09-15", "MatterLastModifiedUtc": "2026-09-16T00:00:00", "provider": "legistar", "opened": True}))
    store.add(Record("canton:granicus", "transcript", "https://canton.example/TranscriptViewer.php?clip_id=77", title="Transcript",
                     text=TRANSCRIPT, published="2026-10-20", jurisdiction="canton", evidence="primary",
                     meta={"body": "City Council", "meeting_date": "2026-10-20", "video_page": "https://canton.example/MediaPlayer.php?clip_id=77"}))
    store.flush()
    # derive minutes -> votes; fill sponsors from the (stub) Legistar API
    out = derive.run(store, only={"minutes"}, log=lambda *_: None)
    assert out["minutes"]["derived"] == 10                         # 5 motions -> 5 agenda items + 5 votes
    sp = fill_sponsors(fetcher, store, log=lambda *_: None)
    assert sp == {"checked": 2, "with_sponsors": 1}
    ords = {r["meta"]["MatterId"]: r for r in store.query("kind IN ('ordinance','matter')")}
    assert ords[5]["meta"]["sponsors"][0]["name"] == "Pat Local" and ords[5]["meta"]["opened_url"].endswith("/matters/5/sponsors")
    assert fill_sponsors(fetcher, store, log=lambda *_: None)["checked"] == 0          # not re-fetched
    # the candidate: own site statement, a reported article, then research (no network steps)
    cand = {"id": "pat-local", "name": "Pat Local", "aliases": [], "websites": [], "social": [], "jurisdictions": ["canton"],
            "jurisdiction": "canton", "office": "City Council Post 2", "electo_key": "PAT LOCAL", "party": "R",
            "races": [{"eid": "2023Nov", "d": "2023-11-07", "office": "City Council Post 2 - Canton", "county": "Cherokee", "party": "",
                       "votes": 812, "pct": 61.2, "outcome": "won", "level": "local", "u": "https://results.sos.ga.gov/x/1"},
                      {"eid": "2019Nov", "d": "2019-11-05", "office": "City Council Post 2 - Canton", "county": "Cherokee", "party": "",
                       "votes": 500, "pct": 55.0, "outcome": "won", "level": "local", "u": "https://results.sos.ga.gov/x/2"}],
            "variants": name_variants("Pat Local")}
    store.add(Record("cand:pat-local", "statement", "https://patlocal.example/about", title="About Pat", text=SITE_ABOUT,
                     published="2026-08-01", jurisdiction="canton", channel="html", evidence="primary", meta={"candidate_id": "pat-local", "opened": True}))
    store.add(Record("cand:pat-local", "article", "https://tribune.example/2026/10/local-data-center", title="Local opposes data center",
                     text=ARTICLE, published="2026-10-21", jurisdiction="canton", channel="html", evidence="reported",
                     meta={"candidate_id": "pat-local", "outlet": "tribune.example", "opened": True}))
    store.flush()
    r = Researcher(root, {}, fetcher, store, log=lambda *_: None)
    counts = r.research(cand, skip={"posts", "statements", "articles", "leads", "social_mentions", "connections"})
    assert counts["vote"] == 5 and counts["sponsorship"] == 1 and counts["mention"] >= 1
    votes = {v["title"][:21]: v["meta"] for v in store.read("cand:pat-local", "vote")}
    assert votes["adopt Ordinance No. 2"]["vote"] == "yes" and votes["adopt Ordinance No. 2"]["basis"] == "moved"
    assert votes["approve Resolution 20"]["vote"] == "no" and votes["approve Resolution 20"]["basis"] == "named"
    # the profile supplement
    prof = stances.profile_supplement(cand, store, root)
    topics = {p["topic"] for p in prof["positions"]}
    assert "Taxes and budget" in topics and "Education" in topics and ("Energy and utilities" in topics or "Growth and development" in topics)
    own = [p for p in prof["positions"] if p["basis"] == "campaign site"]
    said = [p for p in prof["positions"] if p["basis"].startswith("said at City Council")]
    rep = [p for p in prof["positions"] if p["basis"].startswith("reported by")]
    assert own and said and rep and said[0]["needs_review"] and said[0]["d"] == "2026-10-20"
    assert all(p["evidence"] == "reported" for p in rep) and any("water study" in p["stance"] for p in rep)
    assert "lifelong conservative republican" in prof["ideology"] and prof["ideology"].startswith("Republican; self-described")
    assert "NRA A" in prof["ideology"] and "Georgia Chamber" in prof["ideology"] and prof["isrc"]
    assert {e for e in prof["endorsements"]} >= {"Georgia Chamber of Commerce (2026)", "Sheriff Dana Reed (2026)", "Georgia Chamber (2026)"}
    assert prof["ratings"][0] == {"org": "NRA", "rating": "A", "year": "2024", "src": [0], "basis": "campaign site (self-reported)"}
    assert {a["org"] for a in prof["affiliations"]} >= {"Cherokee County Republican Party", "Canton Planning Commission"}
    assert [v["vote"] for v in prof["votes"]].count("yes") >= 3 and prof["bills"][0]["number"] == "26-5" and prof["bills"][0]["role"] == "sponsor"
    assert {o["bill"] for o in prof["ordinances_supported"]} == {"Ordinance 2026-14", "26-5"}
    assert {o["bill"] for o in prof["ordinances_opposed"]} == {"Resolution 2026-40"}
    assert prof["site"]["pages"] == 1 and prof["site"]["positions"] >= 1 and prof["site"]["urls"] == ["https://patlocal.example/about"]
    assert prof["controversies"] == []


    assert prof["inference_inputs"] >= 10 and prof["depth"] == "full"
    # sources: every src index points at an opened record; none unverified
    for p in prof["positions"] + prof["statements"]:
        for i in p["src"]:
            assert prof["sources"][i]["u"].startswith("http") and prof["sources"][i]["evidence"] in ("primary", "reported")
    # career timeline
    from gacrawl.careers import career_for, summary

    S = stances.SourceList()
    S.items = prof["sources"]
    S.index = {k: i for i, k in enumerate(k for k in [s["u"].replace("https://", "").rstrip("/").lower() for s in S.items])}
    career = career_for(cand, store, S, prof["affiliations"])
    kinds = [(e["kind"], e["d"][:4]) for e in career]
    assert ("election", "2019") in kinds and ("election", "2023") in kinds
    assert any(e["kind"] == "bio" and e["body"] == "Canton Planning Commission" and e["d"] == "2014" and e["to"] == "2018" for e in career)
    assert any(e["kind"] == "bio" and e["body"] == "City Council" and e["how"].startswith("elected") and e["d"] == "2019" for e in career)
    att = next(e for e in career if e["kind"] == "attendance")
    assert att["d"] == "2026-10-20" and att["jurisdiction"] == "canton"
    assert summary(career).startswith("won City Council Post 2 - Canton (2019); City Council Post 2 - Canton (2023)")
    # export: profiles/, connections.json, packets carry the profile
    from gacrawl.export import export

    (root / "config" / "candidates.yaml").write_text(json.dumps({"candidates": [{k: v for k, v in cand.items() if k != "variants"}]}))
    man = export(root, tmp_path / "dist", log=lambda *_: None, store=store, cfg={"electo": {"levels": []}})
    assert man["profile_supplements"] == 1 and man["connections"]["edges"] >= 1
    pk = json.loads((tmp_path / "dist" / "packets" / "pat-local.json").read_text())
    assert pk["profile"]["depth"] == "full" and pk["counts"]["inference_inputs"] >= 10
    idx = json.loads((tmp_path / "dist" / "profiles" / "index.json").read_text())
    assert idx["profiles"][0]["candidate_id"] == "pat-local" and idx["thin"] == 0
    # unchanged data -> byte-identical files (so Electo's repo only sees real changes)
    export(root, tmp_path / "dist2", log=lambda *_: None, store=store, cfg={"electo": {"levels": []}})
    for rel in ("packets/pat-local.json", "profiles/pat-local.json", "profiles/index.json", "connections.json"):
        assert (tmp_path / "dist" / rel).read_bytes() == (tmp_path / "dist2" / rel).read_bytes(), rel
    store.close()


def test_issue_page_and_controversies(tmp_path):
    root = Path(__file__).resolve().parent.parent
    cand = {"id": "kim-post", "name": "Kim Post", "aliases": [], "websites": [], "social": [], "jurisdictions": ["canton"], "jurisdiction": "canton",
            "office": "City Council", "electo_key": "KIM POST", "party": "D", "races": [], "variants": name_variants("Kim Post")}
    store = Store(tmp_path / "data")
    # an issues page written as headings and bullets, with no stance verbs at all
    store.add(Record("cand:kim-post", "statement", "https://kimpost.example/issues", title="Issues", published="2026-08-01",
                     text="# Issues\n\n## Public safety\n- Fully staffed police and fire departments with competitive pay\n"
                          "## Property taxes\n- A homestead exemption that keeps seniors in their homes\n## Schools\n- Smaller class sizes in every elementary school\n",
                     jurisdiction="canton", channel="html", evidence="primary", meta={"candidate_id": "kim-post", "opened": True}))
    store.add(Record("cand:kim-post", "article", "https://tribune.example/2026/09/post-ethics", title="Ethics complaint filed", published="2026-09-12",
                     text="Canton Council Member Kim Post faces an ethics complaint filed with the state ethics commission over a 2025 campaign "
                          "finance report, the Tribune has learned. Post said the report was amended. The council meets Tuesday.",
                     jurisdiction="canton", channel="html", evidence="reported", meta={"candidate_id": "kim-post", "outlet": "tribune.example", "opened": True}))
    store.add(Record("cand:kim-post", "article", "https://blog.example/rant", title="rumor", published="2026-09-13",
                     text="Kim Post was arrested, a blog claims.", jurisdiction="canton", channel="html", evidence="unverified",
                     meta={"candidate_id": "kim-post", "outlet": "blog.example", "opened": True}))
    store.flush()
    prof = stances.profile_supplement(cand, store, root)
    assert prof["site"]["pages"] == 1 and prof["site"]["positions"] >= 2
    assert all(p["basis"] == "campaign site (issues page)" for p in prof["positions"]) and {p["topic"] for p in prof["positions"]} & {"Public safety", "Taxes and budget", "Education"}
    assert prof["inference_inputs"] >= 2
    c = prof["controversies"]
    assert len(c) == 1 and c[0]["evidence"] == "reported" and c[0]["basis"] == "reported by tribune.example" and "ethics" in " ".join(c[0]["cues"])
    assert prof["sources"][c[0]["src"][0]]["u"].startswith("https://tribune.example/")


def test_controversy_needs_the_person_as_subject():
    from gacrawl.stances import Extractor
    import gacrawl.stances as S
    class E(S.Extractor):
        def __init__(self):
            self.last, self.first, self.variants = "carr", "chris", ["chris carr"]
        def named(self, s):
            return "carr" in s.lower()
    e = E()
    keep = ["A grand jury indicted Chris Carr on two counts in 2025.", "Attorney General Carr was fined $5,000 by the State Ethics Commission.",
            "The ethics complaint against Attorney General Chris Carr was dismissed.", "Chris Carr faces a lawsuit from former staff over the firing."]
    drop = ["Carr went to court when she filed a federal lawsuit.", "Judge Carr swore in the grand jury on January 21.",
            "Carr used settlement funds from the opioid case for treatment.", "The pigment is suspended in a base, Carr wrote.",
            "Carr said the DeKalb ethics board needs applicants.",
            "Chris Carr Flanagan, 30, of Gainesville, is charged with making harassing phone calls.",
            "Police charged Carr, 30, of Gainesville, with theft."]
    assert all(e.controversies(t) for t in keep), [t for t in keep if not e.controversies(t)]
    assert not any(e.controversies(t) for t in drop), [t for t in drop if e.controversies(t)]
