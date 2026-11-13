"""General Assembly adapter against a local stub of legis.ga.gov's data service: token, members, details,
legislation search (POST JSON), roll calls; incremental re-runs; per-candidate records in Electo's shapes."""
import http.server
import json
import threading
from pathlib import Path

import pytest
import requests

from gacrawl.fetch import Fetcher
from gacrawl.names import name_variants
from gacrawl.sources import legis
from gacrawl.store import Store

MEMBERS = {1: [{"id": 5025, "fullName": "Pat Local", "name": {"first": "Pat", "last": "Local", "nickname": None}, "party": 0,
                "district": {"chamberType": 1, "number": 43}, "districtNumber": 43, "city": "Marietta", "sessionId": 1033, "dateVacated": None}],
           2: [{"id": 77, "fullName": "Kim Post", "name": {"first": "Kim", "last": "Post", "nickname": None}, "party": 1,
                "district": {"chamberType": 2, "number": 7}, "districtNumber": 7, "city": "Rome", "sessionId": 1033, "dateVacated": None}]}
DETAIL = {5025: {"displayName": "Pat Local", "title": "Minority Caucus Treasurer", "occupation": "Business Owner", "biography": None, "party": 0, "chamber": 1,
                 "districtNumber": 43, "committeeMemberships": [{"committee": {"id": 92, "name": "Governmental Affairs"}, "role": "Member"}],
                 "sessionsInService": [{"session": {"id": 1033, "description": "2025-2026 Regular Session"}, "chamber": 1, "title": None,
                                        "committeeMemberships": [{"committee": {"id": 92, "name": "Governmental Affairs"}, "role": "Member"}]},
                                       {"session": {"id": 1031, "description": "2023-2024 Regular Session"}, "chamber": 1, "title": None, "committeeMemberships": []}]},
          77: {"displayName": "Kim Post", "title": None, "occupation": "Teacher", "party": 1, "chamber": 2, "districtNumber": 7, "committeeMemberships": [],
               "sessionsInService": [{"session": {"id": 1033, "description": "2025-2026 Regular Session"}, "chamber": 2, "committeeMemberships": []}]}}
BILLS = [{"legislationId": 69281, "actVetoNumber": "123", "caption": "Pediatric Health Safe Storage Act; enact", "documentType": 1, "chamberType": 1, "number": 1, "suffix": "",
          "session": {"id": 1033, "description": "2025-2026 Regular Session"}, "status": "House Date Signed by Governor", "statusDate": "05/01/2025",
          "sponsors": [{"memberId": 5025, "name": "Local, Pat ", "sequence": 1, "sponsorType": 1, "chamber": 0, "district": "43rd"},
                       {"memberId": 77, "name": "Post, Kim ", "sequence": 2, "sponsorType": 3, "chamber": 1, "district": "7th"}],
          "houseCommittee": {"name": "Public Safety"}, "senateCommittee": None},
         {"legislationId": 70000, "actVetoNumber": "", "caption": "Income tax; reduce rate", "documentType": 1, "chamberType": 1, "number": 111, "suffix": "",
          "session": {"id": 1033, "description": "2025-2026 Regular Session"}, "status": "House Second Readers", "statusDate": "01/20/2025",
          "sponsors": [{"memberId": 77, "name": "Post, Kim ", "sequence": 1, "sponsorType": 1, "chamber": 1, "district": "7th"}], "houseCommittee": None, "senateCommittee": None}]
VOTES = {69281: {"detail": {"id": 69281, "votes": [{"id": 12265, "number": 80, "caption": "PASSAGE", "date": "2025-03-05T11:51:00", "name": "House Vote #80", "yea": 1, "nay": 1, "notVoting": 0, "excused": 0},
                                                   {"id": 12266, "number": 81, "caption": "MOTION TO ENGROSS", "date": "2025-03-05T10:00:00", "name": "House Vote #81", "yea": 0, "nay": 1}]},
                 "rolls": {12265: [{"member": {"id": 5025, "name": "LOCAL, 43RD"}, "memberVoted": 0}, {"member": {"id": 77, "name": "POST, 7TH"}, "memberVoted": 1}],
                           12266: [{"member": {"id": 5025, "name": "LOCAL, 43RD"}, "memberVoted": 1}]}}}
CALLS = []


class H(http.server.BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = self.path
        CALLS.append(p)
        if p == "/robots.txt":
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(b"User-agent: *\nAllow: /\n"); return
        if p.endswith("/authentication/token"):
            return self._json("tok-123")
        if self.headers.get("Authorization") != "Bearer tok-123":
            return self._json({"error": "unauthorized"}, 401)
        if "/members/list/" in p:
            return self._json(MEMBERS[int(p.split("chamber=")[1])])
        if "/members/detail/" in p:
            return self._json(DETAIL[int(p.split("/members/detail/")[1].split("?")[0])])
        if "/legislation/detail/" in p:
            return self._json(VOTES[int(p.rsplit("/", 1)[1])]["detail"])
        if "/Vote/detail/" in p:
            vid = int(p.rsplit("/", 1)[1])
            for lid, d in VOTES.items():
                if vid in d["rolls"]:
                    return self._json({"votes": d["rolls"][vid]})
        self._json({}, 404)

    def do_POST(self):
        CALLS.append("POST " + self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if "/Legislation/Search/100/" in self.path:
            page = int(self.path.rsplit("/", 1)[1])
            rows = BILLS if (page == 0 and body.get("sessionId") == 1033) else []
            return self._json({"results": rows, "resultCount": len(BILLS) if body.get("sessionId") == 1033 else 0})
        self._json({}, 404)

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


def test_legislature_update_and_people(server, fetcher, tmp_path, monkeypatch):
    monkeypatch.setattr(legis, "API", server + "/api/")
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    cfg = {"sessions": {"current": [1033], "history": [1031, 1033], "names": {1031: "2023-2024", 1033: "2025-2026"},
                        "years": {1031: [2023, 2024], 1033: [2025, 2026]}},
           "votes": {"key_bills": [{"session": 1033, "bill": "HB 1", "title": "Safe storage"}], "all_passed_in_current": True, "max_bills_per_run": 5},
           "members": {"recheck_days": 30}}
    import yaml

    (root / "config" / "legislature.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(root / "data")
    out = legis.update(fetcher, store, root, log=lambda *_: None)
    assert out["members"] == 2 and out["member_details"] == 2 and out["bills"] == 2 and out["votes"] == 2 and 1033 in out["sessions"]
    lg = {r["meta"]["name"]: r for r in store.query("kind='legislator'")}
    assert lg["Pat Local"]["meta"]["party"] == "D" and lg["Pat Local"]["meta"]["title"] == "Minority Caucus Treasurer"
    assert lg["Pat Local"]["url"].endswith("/members/house/5025?session=1033") and len(lg["Pat Local"]["meta"]["sessions"]) == 2
    bills = {r["meta"]["number"]: r for r in store.query("kind='bill'")}
    assert bills["HB 1"]["meta"]["enacted"] and bills["HB 1"]["meta"]["sponsors"][0]["role"] == "lead sponsor"
    assert bills["HB 1"]["meta"]["sponsors"][1]["role"] == "Senate sponsor" and bills["HB 1"]["url"].endswith("/legislation/69281")
    votes = {r["meta"]["VoteId"]: r for r in store.query("kind='vote'")}
    assert votes[12265]["meta"]["substantive"] and not votes[12266]["meta"]["substantive"]
    assert {v["person"]: v["vote"] for v in votes[12265]["meta"]["votes"]} == {"Pat Local": "Yea", "Kim Post": "Nay"}
    assert votes[12265]["meta"]["EventBodyName"] == "Georgia House" and votes[12265]["published"] == "2025-03-05"
    # second run: members re-listed (current session) but details not re-read; legislation re-listed; votes not re-fetched
    before = len(CALLS)
    out2 = legis.update(fetcher, store, root, log=lambda *_: None)
    assert out2["member_details"] == 0 and out2["votes"] == 0 and out2["bills"] == 0
    assert not any("/Vote/detail/" in c for c in CALLS[before:]) and sum("/members/detail/" in c for c in CALLS[before:]) == 0
    # per-candidate records in Electo's shapes
    cands = [{"id": "pat-local", "name": "Pat Local", "aliases": [], "electo_key": "PAT LOCAL", "variants": name_variants("Pat Local"),
              "races": [{"office": "State House District 43", "d": "2024-11-05", "outcome": "won"}]},
             {"id": "kim-post", "name": "Kim Post", "aliases": [], "electo_key": "KIM POST", "variants": name_variants("Kim Post"), "races": []},
             {"id": "nobody", "name": "Sam Third", "aliases": [], "variants": name_variants("Sam Third"), "races": []}]
    people = legis.legis_people(store, cands, root)
    assert set(people) == {"PAT LOCAL", "KIM POST"}
    pl = people["PAT LOCAL"]
    assert pl["member"]["chamber"] == "House" and pl["member"]["district"] == 43 and len(pl["member"]["terms"]) == 2
    assert pl["member"]["terms"][1]["from"] == "2023-01-01" and pl["committees"][0]["name"] == "Governmental Affairs"
    assert pl["votes"] == [{"bill": "HB 1", "session": "2025-2026", "title": "Pediatric Health Safe Storage Act; enact", "vote": "Yea", "d": "2025-03-05",
                            "stage": "House Vote #80 Passage (1-1)", "u": server.replace(server, "https://www.legis.ga.gov") + "/legislation/69281", "basis": "roll call", "src": []}]
    assert pl["bills"][0]["number"] == "HB 1" and pl["bills"][0]["role"] == "lead sponsor" and pl["bills"][0]["enacted"]
    kp = people["KIM POST"]
    assert {b["role"] for b in kp["bills"]} == {"lead sponsor", "Senate sponsor"} and kp["votes"][0]["vote"] == "Nay"
    # profile supplement folds the legislature record in: votes, bills, committee affiliation, leadership title
    from gacrawl import stances

    for f in ("topics.yaml", "issues.yaml"):
        (root / "config" / f).write_text((Path(__file__).resolve().parent.parent / "config" / f).read_text())
    prof = stances.profile_supplement({**cands[0], "jurisdictions": [], "office": "State House District 43", "party": "D"}, store, root, legis=pl)
    assert prof["votes"][0]["vote"] == "Yea" and prof["bills"][0]["number"] == "HB 1" and prof["inference_inputs"] >= 2
    assert {a["org"] for a in prof["affiliations"]} == {"House Governmental Affairs Committee", "Georgia House leadership"}
    assert prof["sources"][prof["votes"][0]["src"][0]]["u"].endswith("/legislation/69281")
    store.close()


def test_roll_calls_of_every_bill_that_reached_the_floor(server, fetcher, tmp_path, monkeypatch):
    """A closed session's bill that crossed to the other chamber has a floor vote to read; one that never left
    committee is not asked about; the budget still holds, and what is left is counted."""
    import yaml
    from gacrawl.store import Record

    monkeypatch.setattr(legis, "API", server + "/api/")
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    cfg = {"sessions": {"current": [1033], "history": [1031, 1033], "names": {1031: "2023-2024", 1033: "2025-2026"},
                        "years": {1031: [2023, 2024], 1033: [2025, 2026]}},
           "votes": {"key_bills": [], "all_passed_in_current": False, "all_floor_votes": True, "max_bills_per_run": 1},
           "members": {"recheck_days": 30}}
    (root / "config" / "legislature.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(root / "data")
    store.kv_set("legis:legislation:1031", "2026-01-01")        # the closed session's bill list was read before
    for lid, number, status in ((555, "HB 20", "Senate Read and Referred"), (556, "HB 21", "House Second Readers"),
                                (557, "SB 30", "Senate Passed/Adopted")):
        store.add(Record(legis.SOURCE, "bill", f"https://www.legis.ga.gov/legislation/{lid}", title=number, key=f"{legis.SOURCE}|bill|{lid}",
                         meta={"legislation_id": lid, "number": number, "session": 1031, "session_name": "2023-2024", "caption": "x",
                               "status": status, "status_date": "2024-03-01", "chamber": "House" if number.startswith("H") else "Senate", "sponsors": []}))
    store.flush()
    roll = {"detail": {"id": 555, "votes": [{"id": 900, "caption": "PASSAGE", "date": "2024-02-20T10:00:00", "name": "House Vote #5", "yea": 1, "nay": 1}]},
            "rolls": {900: [{"member": {"id": 5025, "name": "LOCAL, 43RD"}, "memberVoted": 0}, {"member": {"id": 77, "name": "POST, 7TH"}, "memberVoted": 1}]}}
    monkeypatch.setitem(VOTES, 555, roll)
    monkeypatch.setitem(VOTES, 557, {"detail": {"id": 557, "votes": []}, "rolls": {}})
    before = len(CALLS)
    out = legis.update(fetcher, store, root, log=lambda *_: None)
    asked = [c for c in CALLS[before:] if "/legislation/detail/" in c]
    # three bills reached the floor (the current session's signed HB 1 and the closed session's two); newest session first
    assert out["vote_bills"] == 1 and out["vote_bills_waiting"] == 2 and [c.rsplit("/", 1)[1] for c in asked] == ["69281"]
    out2 = legis.update(fetcher, store, root, log=lambda *_: None)         # later runs read the ones left
    out3 = legis.update(fetcher, store, root, log=lambda *_: None)
    assert (out2["vote_bills"], out2["vote_bills_waiting"], out3["vote_bills"], out3["vote_bills_waiting"]) == (1, 1, 1, 0)
    assert {r["meta"]["VoteId"] for r in store.query("kind='vote'")} == {12265, 12266, 900}
    assert not any(c.endswith("/556") or c.endswith("/70000") for c in CALLS[before:] if "/legislation/detail/" in c)
    before = len(CALLS)
    out4 = legis.update(fetcher, store, root, log=lambda *_: None)         # a closed session's bill is read once
    assert out4["vote_bills"] == 0 and not any("/legislation/detail/" in c for c in CALLS[before:])


def test_an_unreachable_site_gets_the_second_try_too(tmp_path, monkeypatch):
    """Nov 12: a connection timeout stopped the legislature step and, not being a refusal, left no marker, so the run's
    later second try never came. Unreachable now leaves the marker; a clean try clears it."""
    import argparse

    from gacrawl import cli, config

    cfg = {"state_dir": tmp_path / "state"}
    monkeypatch.setattr(config, "make_store", lambda c: Store(tmp_path / "data"))
    monkeypatch.setattr(cli, "_fetcher", lambda c, d=None: None)
    args = argparse.Namespace(action="update", max_minutes=1, max_bills=1, if_refused=False)
    marker = cfg["state_dir"] / "legis_refused.json"
    monkeypatch.setattr(legis, "update", lambda *a, **k: {"votes": 0, "error": "https://www.legis.ga.gov/api/authentication/token (unreachable (ConnectTimeout))"})
    cli.cmd_legis(cfg, args)
    assert marker.exists() and "unreachable" in json.loads(marker.read_text())["why"]
    monkeypatch.setattr(legis, "update", lambda *a, **k: {"votes": 3})
    cli.cmd_legis(cfg, args)
    assert not marker.exists()
    monkeypatch.setattr(legis, "update", lambda *a, **k: {"votes": 0, "error": "robots.txt disallows /api/"})   # not a passing fault
    cli.cmd_legis(cfg, args)
    assert not marker.exists()


def test_check_roll_call():
    ok = [{"person_id": 1, "vote": "Yea", "party": "R"}, {"person_id": 2, "vote": "Nay", "party": "D"}, {"person_id": 3, "vote": "Excused", "party": "D"}]
    c = legis.check_roll_call({"yea": 1, "nay": 1, "not_voting": 1, "excused": 0}, ok, "House")
    assert c == {"ok": True, "listed": 3, "issues": []}                     # excused and not voting are taken together
    short = legis.check_roll_call({"yea": 150, "nay": 20, "not_voting": 0, "excused": 0}, ok[:2], "House")
    assert not short["ok"] and "Yea: 1 listed, 150 reported" in short["issues"]
    twice = legis.check_roll_call({"yea": 2, "nay": 0}, [ok[0], ok[0]], "Senate")
    assert not twice["ok"] and any("twice" in i for i in twice["issues"])
    big = legis.check_roll_call({"yea": 60}, [{"person_id": i, "vote": "Yea", "party": "R"} for i in range(60)], "Senate")
    assert not big["ok"] and any("56 seats" in i for i in big["issues"])


def test_a_roll_call_that_does_not_add_up_is_kept_marked_and_read_again(server, fetcher, tmp_path, monkeypatch):
    import yaml
    from gacrawl.store import Record

    monkeypatch.setattr(legis, "API", server + "/api/")
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    cfg = {"sessions": {"current": [1033], "history": [1031, 1033], "names": {1031: "2023-2024", 1033: "2025-2026"},
                        "years": {1031: [2023, 2024], 1033: [2025, 2026]}},
           "votes": {"key_bills": [], "all_passed_in_current": False, "all_floor_votes": True, "max_bills_per_run": 5},
           "members": {"recheck_days": 30}}
    (root / "config" / "legislature.yaml").write_text(yaml.safe_dump(cfg))
    store = Store(root / "data")
    store.add(Record(legis.SOURCE, "bill", "https://www.legis.ga.gov/legislation/558", title="HB 40", key=f"{legis.SOURCE}|bill|558",
                     meta={"legislation_id": 558, "number": "HB 40", "session": 1031, "session_name": "2023-2024", "caption": "x",
                           "status": "Senate Passed/Adopted", "status_date": "2024-03-01", "chamber": "House", "sponsors": []}))
    store.flush()
    monkeypatch.setitem(VOTES, 558, {"detail": {"id": 558, "votes": [{"id": 901, "caption": "PASSAGE", "date": "2024-02-20T10:00:00",
                                                                     "name": "House Vote #6", "yea": 150, "nay": 20}]},
                                     "rolls": {901: [{"member": {"id": 5025, "name": "LOCAL, 43RD"}, "memberVoted": 0}]}})
    assert legis.votes_waiting(store, root) == 1
    outs = [legis.update(fetcher, store, root, log=lambda *_: None, votes_only=True) for _ in range(4)]
    v = store.query("kind='vote'")
    assert len(v) == 1 and v[0]["meta"]["check"]["ok"] is False and "Yea: 1 listed, 150 reported" in v[0]["meta"]["check"]["issues"]
    assert [o.get("failed_check", 0) for o in outs] == [1, 1, 1, 0]           # read three times, then left alone
    assert legis.votes_waiting(store, root) == 0 and store.kv_get("legis:votes_try:558") == "3"
    assert all(o["members"] == 0 and o["bills"] == 0 for o in outs)          # votes only: no member or bill lists read
