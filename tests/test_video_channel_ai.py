import json

from gacrawl import channel_decisions
from gacrawl.channel_ai import run as channel_run, same_quality
from gacrawl.discover_ai import claim_core
from gacrawl.transcripts import official_channel
from gacrawl.video_ai import analyze, check_item

PART = ("good evening everyone the meeting will come to order motion to approve the consent agenda "
        "second all in favor the motion carries five to zero member smith said we need a new roof on the middle school")


class FakeLLM:
    def __init__(self, outs):
        self.outs, self.logged = list(outs), []

    def chat(self, msgs, max_tokens=400):
        return self.outs.pop(0)

    def log(self, path, rec):
        self.logged.append(rec)

    def model_name(self):
        return "fake"

    def check(self, line, source, max_words=60, new_share=0.3, allow=()):
        from gacrawl.llm import check
        return check(line, source, max_words, new_share, allow)


def _decisions(tmp_path, body):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "channel_decisions.yaml").write_text(body, encoding="utf-8")
    channel_decisions._CACHE.clear()


def test_owner_decision_wins_over_the_name_checks(tmp_path, monkeypatch):
    _decisions(tmp_path, "channels:\n  UCabc:\n    title: Fannin Rebel TV\n    status: confirmed\n    decided: '2026-11-08'\n"
                         "  UCbad:\n    title: Riverdale City News\n    status: rejected\n    decided: '2026-11-08'\n")
    monkeypatch.setattr(channel_decisions, "_root", lambda: tmp_path)
    ok, why = official_channel({"id": "UCabc", "title": "Fannin Rebel TV", "handle": "@fan"}, "Fannin County Schools", "")
    assert ok and why.startswith("hand-checked")
    ok, _ = official_channel({"id": "UCbad", "title": "City of Riverdale", "handle": "@riverdale"}, "Riverdale",
                             "linked from the official website (https://riverdalega.gov)")
    assert not ok
    ok, _ = official_channel({"id": "UCzzz", "title": "Fan Club", "handle": ""}, "Fannin", "")
    assert not ok


def test_transcript_lines_must_be_copied():
    it, _ = check_item({"type": "vote", "who": "", "text": "the motion carries five to zero"}, PART)
    assert it and it["type"] == "vote"
    it, why = check_item({"type": "vote", "text": "the board voted to raise taxes"}, PART)
    assert it is None and "not copied" in why
    it, _ = check_item({"type": "statement", "who": "Member Smith", "text": "we need a new roof on the middle school"}, PART)
    assert it["who"] == "Member Smith" and it["needs_review"]
    it, _ = check_item({"type": "statement", "who": "Jane Doe", "text": "we need a new roof on the middle school"}, PART)
    assert it["who"] == ""


def test_analyze_keeps_checked_items_and_a_checked_summary(tmp_path):
    llm = FakeLLM(['{"type": "motion", "who": "", "text": "motion to approve the consent agenda"}\n'
                   '{"type": "vote", "who": "", "text": "the board unanimously raised pay"}',
                   "The board took up a motion to approve the consent agenda."])
    a = analyze(PART, "Board meeting", "2026-11-02", llm, str(tmp_path / "log.jsonl"))
    assert [i["type"] for i in a["items"]] == ["motion"] and a["summary"].startswith("The board")


def test_channel_auto_only_when_as_good_as_the_owners(tmp_path, monkeypatch):
    _decisions(tmp_path, "channels:\n  UCok:\n    title: JCS Streaming\n    jurisdiction: sd-city-jefferson\n    status: confirmed\n"
                         "    decided: '2026-11-08'\n    by: owner\n")
    rows = [
        {"id": "v1", "jurisdiction": "sd-pike", "url": "https://youtu.be/1", "title": "Pike County Board of Education Meeting May 2026",
         "channel": {"id": "UCpike", "title": "PCS Live"}, "reason": "linked from the official website (https://pike.k12.ga.us), but ..."},
        {"id": "v2", "jurisdiction": "sd-pike", "url": "https://youtu.be/2", "title": "Pike County Board of Education Work Session",
         "channel": {"id": "UCpike", "title": "PCS Live"}, "reason": "linked from the official website (https://pike.k12.ga.us), but ..."},
        {"id": "v3", "jurisdiction": "sd-lee", "url": "https://youtu.be/3", "title": "Friday night football highlights",
         "channel": {"id": "UClee", "title": "Trojan Sports"}, "reason": "no official page links to this channel"},
    ]
    (tmp_path / "data" / "rumor_mill").mkdir(parents=True)
    (tmp_path / "data" / "rumor_mill" / "videos.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    ok, why = same_quality(rows[:2], "Pike")
    assert ok, why
    ok, why = same_quality(rows[2:], "Lee")
    assert not ok and len(why) == 2
    llm = FakeLLM(['{"official": true, "why": "linked from the district site and carries its board meetings"}',
                   '{"official": true, "why": "a school channel"}'])
    st = channel_run(tmp_path, deadline=1e18, log=lambda *a: None, llm=llm)
    assert st["auto"] == 1 and st["review"] == 1
    d = channel_decisions.load(tmp_path)
    assert d["UCpike"]["status"] == "confirmed" and d["UCok"]["by"] == "owner" and "UClee" not in d
    assert "UClee" in (tmp_path / "data" / "channel_review.md").read_text()
    assert any(x.get("task") == "channel_decision" and x["label"] == "confirmed" for x in llm.logged)


def test_rumor_headline_as_search_words():
    assert claim_core("Mayor accused of steering contracts to donor - WSB-TV") == "Mayor accused of steering contracts to donor"
    assert claim_core("Short | Site") == "Short Site"   # too short to drop a site name; punctuation out
