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
VOTES = {69281: {"detail": {"id": 69281, "votes": [{"id": 12265, "number": 80, "caption": "PASSAGE", "date": "2025-03-05T11:51:00", "name": "House Vote #80", "yea": 150, "nay": 20, "notVoting": 5, "excused": 5},
                                                   {"id": 12266, "number": 81, "caption": "MOTION TO ENGROSS", "date": "2025-03-05T10:00:00", "name": "House Vote #81", "yea": 90, "nay": 80}]},
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
                            "stage": "House Vote #80 Passage (150-20)", "u": server.replace(server, "https://www.legis.ga.gov") + "/legislation/69281", "basis": "roll call", "src": []}]
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
