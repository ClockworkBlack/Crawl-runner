"""Endorsements, scorecards and questionnaires -> per-candidate support records -> profile supplement. Fictional."""
import json
from pathlib import Path

from gacrawl import derive, guides, registry, stances
from gacrawl.names import name_variants
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent
ENDORSE_PAGE = """2026 General Election Endorsements
The Georgia Chamber is proud to endorse Pat Local for City Council Post 2.
- Dana Reed – Mayor of Canton
- Kim Post (State House District 23)
Click here to donate.
"""
SCORECARD = """2025 Legislative Scorecard
| Legislator | Party | District | Score |
| --- | --- | --- | --- |
| Kim Post | R | House 23 | 92% |
| Pat Local | D | Senate 7 | 45% |
"""
QUESTIONNAIRE = """Canton City Council Post 2 — Candidate Questionnaire
Question 1: What is your top priority for the city?
Pat Local: Fixing our roads and keeping taxes flat. I will fight for a six-year paving plan funded by the existing SPLOST.
Dana Reed: Public safety. We need more officers on patrol and a new fire station on Elm Street.

Question 2: Do you support the proposed data center?
Pat Local: No, not without a water study. I oppose rezoning that parcel until the county finishes the study.
"""


def test_parsers():
    e = {x["name"]: x["office"] for x in guides.parse_endorsements(ENDORSE_PAGE)}
    assert e == {"Pat Local": "City Council Post 2", "Dana Reed": "Mayor of Canton", "Kim Post": "State House District 23"}
    r = {x["name"]: x["rating"] for x in guides.parse_ratings(SCORECARD)}
    assert r == {"Kim Post": "92%", "Pat Local": "45%"}
    q = guides.parse_questionnaire(QUESTIONNAIRE)
    assert [x["question"] for x in q] == ["What is your top priority for the city?", "Do you support the proposed data center?"]
    assert q[1]["answers"][0]["name"] == "Pat Local" and q[1]["answers"][0]["answer"].startswith("No, not without")
    assert any(s["id"] == "guide:ga-chamber" and s["kind"] == "guide" for s in registry.guide_sources(ROOT))


def test_guides_derive_into_profiles(tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    for f in ("jurisdictions.yaml", "outlets.yaml", "topics.yaml", "issues.yaml"):
        (root / "config" / f).write_text((ROOT / "config" / f).read_text())
    cand = {"id": "pat-local", "name": "Pat Local", "jurisdiction": "canton", "jurisdictions": ["canton"], "office": "City Council Post 2",
            "electo_key": "PAT LOCAL", "party": "D"}
    (root / "config" / "candidates.yaml").write_text(json.dumps({"candidates": [cand]}))
    store = Store(root / "data")
    for sid, name, kind, url, text in (("guide:ga-chamber", "Georgia Chamber of Commerce", "endorsements", "https://chamber.example/endorsements", ENDORSE_PAGE),
                                       ("guide:gcv", "Georgia Conservation Voters", "scorecard", "https://gcv.example/scorecard-2025", SCORECARD),
                                       ("guide:lwv-ga", "League of Women Voters of Georgia", "questionnaire", "https://lwv.example/canton-post-2", QUESTIONNAIRE)):
        store.add(Record(sid, "guide", url, title=text.splitlines()[0], text=text, published="2026-09-15", channel="html", evidence="primary",
                         meta={"guide_name": name, "guide_kind": kind, "opened": True}))
    store.flush()
    out = derive.run(store, only={"guides"}, context={"root": root, "cfg": {"electo": {"levels": []}}}, log=lambda *_: None)
    assert out["guides"]["derived"] == 3 + 1 + 2 + 1 + 2       # 3 endorsements (+1 candidate link), 2 ratings (+1 link), 2 answers
    conns = {(r["meta"]["type"], r["meta"]["party"]): r for r in store.read("cand:pat-local", "connection")}
    assert conns[("endorsement", "Georgia Chamber of Commerce")]["meta"]["basis"] == "endorser's own page"
    assert conns[("rating", "Georgia Conservation Voters")]["meta"]["rating"] == "45%"
    answers = store.read("cand:pat-local", "guide_answer")
    assert len(answers) == 2 and all(a["meta"]["guide"] == "League of Women Voters of Georgia" for a in answers)
    unmatched = [r for r in store.query("kind='endorsement'") if not r["meta"]["matched"]]
    assert {r["meta"]["name"] for r in unmatched} == {"Dana Reed", "Kim Post"}       # listed for review, not invented
    c = {**cand, "aliases": [], "websites": [], "social": [], "variants": name_variants("Pat Local")}
    prof = stances.profile_supplement(c, store, root)
    assert prof["endorsements"] == ["Georgia Chamber of Commerce (2026)"] and prof["ratings"][0]["rating"] == "45%"
    qa = [p for p in prof["positions"] if p["basis"].startswith("questionnaire")]
    assert qa and any("water study" in p["stance"] for p in qa) and any(p["topic"] in ("Growth and development", "Energy and utilities", "Water") for p in qa)
    assert "Georgia Conservation Voters 45%" in prof["ideology"] and "endorsed by Georgia Chamber of Commerce" in prof["ideology"]
    assert prof["inference_inputs"] >= 5
    store.close()
