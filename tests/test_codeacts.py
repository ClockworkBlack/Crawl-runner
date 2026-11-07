"""Enacted acts -> exact Code changes: the as-passed PDF read with its strike/underline marks, instructions parsed
into targets, Code Revision word edits, effective dates (including one act moving another's), and the crawl step
against a stub of legis.ga.gov (detail + document), read once per act."""
import http.server
import json
import threading
from pathlib import Path

import pytest
import requests

from gacrawl import codeacts
from gacrawl.codeacts import S, U, parse_act, parse_effective, parse_instruction, parse_targets, pick_version, read_pdf
from gacrawl.fetch import Fetcher
from gacrawl.sources import legis
from gacrawl.store import Record, Store

FX = Path(__file__).parent / "fixtures" / "acts"


def act(name):
    return parse_act(read_pdf((FX / name).read_bytes()))


def clauses(a):
    return [(s["n"], c) for s in a["sections"] for c in s["clauses"]]


def test_marks_give_enacted_and_amended_text():
    st = read_pdf((FX / "hb974.pdf").read_bytes())
    i = st.raw.find("provided that at least one such feature")
    assert i > 0
    after, before = st.text(i - 40, i + 80, S), st.text(i - 40, i + 80, U)
    assert "provided that at least one such feature" in after and "provided that" not in before
    assert st.line_at(i) == 24 and st.page_at(i) == 2


def test_hb974_targets_and_passages():
    cl = clauses(act("hb974.pdf"))
    by = {n: c for n, c in cl}
    # whole section, with its number line first
    assert by["1"]["ins"]["cite"] == "21-2-372" and by["1"]["paras"][0] == {"k": "cite", "a": "21-2-372."}
    p = by["1"]["paras"][1]
    assert p["a"].endswith("is a visible watermark that identifies the ballot as an official Georgia ballot.") and \
        p["b"].endswith("make the ballot identifiable to a particular elector.")
    # a subsection revised, a subsection added
    assert by["2"]["ins"]["cite"] == "21-2-379.23" and by["2"]["ins"]["targets"][0]["labels"] == [["b"]]
    assert by["3"]["ins"]["cite"] == "21-2-386" and by["3"]["ins"]["targets"][0]["new"] and by["3"]["paras"][0]["a"].startswith("(g) ")
    assert by["4"]["ins"]["targets"][0]["labels"] == [["j.1"]]
    # a new Code section
    assert by["6"]["ins"].get("new_section") and by["6"]["paras"][0]["a"] == "21-2-498.1."
    assert by["1"]["lines"][0] == 17


def test_sb3ex_moves_another_acts_effective_date():
    a = act("sb3ex.pdf")
    assert a["effective"]["default"] == {"approval": True}
    by = {n: c for n, c in clauses(a)}
    assert by["3"]["ins"]["targets"] == [{"noun": "P", "labels": [["a", "4"]], "parent": ["a"], "new": None, "intro": None}]
    u = by["4"]["ins"]["uncodified"]
    assert u["bill"] == "SB 189" and u["year"] == 2024 and u["approved"] == "2024-05-06"
    assert u["effective"]["sections"] == {"7": {"on": "2028-01-01"}}


def test_code_revision_word_edits():
    a = act("hb475.pdf")
    items = {it["cite"]: it for _, c in clauses(a) for it in c.get("words", [])}
    e = items["21-2-498"]["edits"][0]
    assert (e["op"], e["units"], e["find"], e["repl"]) == ("replace", [["a", "1"]], "is when", "means")
    e = items["21-2-50"]["edits"][0]
    assert (e["op"], e["units"], e["where"], e["anchor"]) == ("delete_comma", [["a", "11"]], "following", "in each year")
    e = items["21-2-92"]["edits"][0]
    assert (e["op"], e["find"], e["where"], e["anchor"]) == ("delete", "shall be", "preceding", "able to read,")
    assert items["21-5-7.1"]["edits"][0]["all"]                      # "both times the term appears"
    assert [x["units"] for x in items["21-2-381"]["edits"]] == [[["a", "1", "C", "iii", "II"]], [["a", "1", "C", "iii", "III"]]]
    assert [x["anchor"] for x in items["24-13-96"]["edits"]] == ["shall not", "such summons"]
    assert not items["9-9-69"]["understood"]                          # "inserting a quotation mark": left for review


def test_instruction_forms():
    t = parse_targets("by revising subsection (a) and paragraph (2) of subsection (b) of Code Section 20-2-1181, as follows:")
    assert [(x["noun"], x["labels"]) for x in t] == [("SS", [["a"]]), ("P", [["b", "2"]])]
    t = parse_targets("by revising division (12)(A)(iii) as follows:")
    assert t[0]["labels"] == [["12", "A", "iii"]] and t[0]["noun"] == "DV"
    t = parse_targets("by revising paragraphs (1) through (4) of subsection (c) as follows:")
    assert t[0]["labels"] == [["c", "1"], ["c", "2"], ["c", "3"], ["c", "4"]]
    t = parse_targets("by adding a new paragraph to subsection (b) to read as follows:")
    assert t[0]["new"] and t[0]["parent"] == ["b"] and t[0]["labels"] == []
    ins = parse_instruction("Said chapter is further amended in said Code section as effective on January 1, 2027, by revising "
                            "subsection (a) as follows:", {"cite": "21-2-139"})
    assert ins["cite"] == "21-2-139" and ins["as_effective"] == "2027-01-01"
    ins = parse_instruction("Said chapter is further amended by repealing Code Section 20-2-670, relating to transfers, in its "
                            "entirety and enacting a new Code section to read as follows:", {})
    assert ins["cite"] == "20-2-670" and "repealing" in ins["verbs"] and "enacting" in ins["verbs"]


def test_effective_dates_per_section():
    e = parse_effective("(a) This section and Sections 12 and 14 of this Act shall become effective upon its approval by the "
                        "Governor or upon its becoming law without such approval. (b) Sections 1, 2, 3, 3.1, 5, 8, 10, and 11 of "
                        "this Act shall become effective on July 1, 2024. (c) Sections 4, 6, and 9 of this Act shall become "
                        "effective on January 1, 2025. (d) Section 7 of this Act shall become effective on July 1, 2026.")
    assert "default" not in e and e["sections"]["3.1"] == {"on": "2024-07-01"} and e["sections"]["12"] == {"approval": True}
    assert e["sections"]["7"] == {"on": "2026-07-01"}
    e = parse_effective("(a) Except as provided for in subsection (b) of this section, this Act shall become effective on July 1, "
                        "2025. (b) Section 4 of this Act shall become effective on January 1, 2027, following the ratification "
                        "of an amendment to the Constitution of Georgia.")
    assert e["default"] == {"on": "2025-07-01"} and e["sections"]["4"]["on"] == "2027-01-01" and "contingent" in e["sections"]["4"]


def test_pick_version():
    vs = [{"id": 1, "name": "LC 39 3517", "versionNumber": 1}, {"id": 2, "name": "HB 475/AP*", "versionNumber": 3, "isCurrent": False},
          {"id": 3, "name": "LC 39 3517/a", "versionNumber": 2, "isCurrent": True}]
    assert pick_version(vs)["id"] == 2
    assert pick_version([{"id": 5, "name": "LC 47 3110S", "versionNumber": 4, "isCurrent": True}, {"id": 4, "name": "x", "versionNumber": 1}])["id"] == 5


# ---------------------------------------------------------------- the crawl step, against a stub
PDF = (FX / "sb129.pdf").read_bytes()
CALLS = []


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", status=200):
        self.send_response(status); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        CALLS.append(self.path)
        if self.path == "/robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        if self.path.endswith("/authentication/token"):
            return self._send(json.dumps("tok").encode())
        if "/legislation/detail/64202" in self.path:
            return self._send(json.dumps({"id": 64202, "session": {"id": 1031, "library": "20232024"},
                                          "versions": [{"id": 219682, "name": "SB 129/AP", "versionNumber": 3}, {"id": 1, "name": "LC", "versionNumber": 1}],
                                          "statusHistory": [{"date": "2023-05-04T10:00:00", "name": "Senate Date Signed by Governor"}]}).encode())
        if "/legislation/document/20232024/219682" in self.path:
            return self._send(PDF, "application/pdf")
        self._send(b"{}", status=404)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_run_reads_each_act_once(server, tmp_path, monkeypatch):
    monkeypatch.setattr(legis, "API", server + "/api/")
    monkeypatch.setattr(codeacts, "API", server + "/api/")
    s = requests.Session(); s.trust_env = False
    f = Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)
    store = Store(tmp_path / "data")
    store.add(Record("ga:legis", "bill", "https://www.legis.ga.gov/legislation/64202", title="SB 129", key="ga:legis|bill|64202",
                     meta={"legislation_id": 64202, "number": "SB 129", "session": 1031, "session_name": "2023-2024 Regular Session",
                           "enacted": True, "act": "341", "caption": "Elections; risk-limiting audits", "status_date": "2023-05-04"}))
    store.add(Record("ga:legis", "bill", "https://www.legis.ga.gov/legislation/1", title="HB 1", key="ga:legis|bill|1",
                     meta={"legislation_id": 1, "number": "HB 1", "session": 1031, "enacted": False}))
    store.flush()
    root = tmp_path / "proj"; (root / "config").mkdir(parents=True)
    out = codeacts.run(f, store, root, log=lambda *_: None)
    assert out["acts"] == 1 and out["parsed"] == 1 and out["errors"] == 0
    r = store.query("kind='code_act'")[0]
    m = r["meta"]
    assert m["bill"] == "SB 129" and m["act"] == "341" and m["signed"] == "2023-05-04" and m["version"]["name"] == "SB 129/AP"
    assert m["document_url"].endswith("/api/legislation/document/20232024/219682")
    cites = {c["ins"].get("cite") for s_ in m["sections"] for c in s_["clauses"] if c.get("ins")}
    assert "21-2-498" in cites
    n = len(CALLS)
    out = codeacts.run(f, store, root, log=lambda *_: None)
    assert out["parsed"] == 0 and out["skipped"] == 1 and not any("/document/" in c for c in CALLS[n:])
    p = tmp_path / "code_acts.json"
    assert codeacts.export_code_acts(store, p) == 1
    d = json.loads(p.read_text())
    assert d["acts"][0]["bill"] == "SB 129" and "opened_url" not in d["acts"][0]
    store.close()


def test_unit_lists_and_legislation_export(tmp_path):
    t = parse_targets("by revising subsections (b), (c), and (d) as follows:")
    assert t[0]["labels"] == [["b"], ["c"], ["d"]]
    store = Store(tmp_path / "data")
    store.add(Record("ga:legis", "bill", "https://www.legis.ga.gov/legislation/64202", title="SB 129", key="ga:legis|bill|64202",
                     meta={"legislation_id": 64202, "number": "SB 129", "session": 1031, "enacted": True, "act": "341", "caption": "Elections; audits",
                           "status": "Senate Date Signed by Governor", "status_date": "2023-05-04",
                           "sponsors": [{"name": "Watson, Ben", "sequence": 2}, {"name": "Williams, Rick", "sequence": 1}]}))
    store.add(Record("ga:legis", "bill", "https://www.legis.ga.gov/legislation/9", title="HR 9", key="ga:legis|bill|9",
                     meta={"legislation_id": 9, "number": "HR 9EX", "session": 1034, "caption": "Condolences", "status": "House Read and Adopted",
                           "status_date": "2026-06-23", "sponsors": []}))
    store.add(Record("ga:legis", "bill", "https://www.legis.ga.gov/legislation/1", title="HB 1", key="ga:legis|bill|1",
                     meta={"legislation_id": 1, "number": "HB 1", "session": 1029}))
    store.flush()
    p = tmp_path / "legislation.json"
    assert codeacts.export_legislation(store, p) == 2
    items = sorted(json.loads(p.read_text())["items"], key=lambda x: x[0])
    assert items[0] == [9, "1034", "H", "R", 9, "EX", "Condolences", "House Read and Adopted", "06/23/2026", "", "", 0]
    assert items[1] == [64202, "1031", "S", "B", 129, "", "Elections; audits", "Senate Date Signed by Governor", "05/04/2023", "341",
                        "Williams, Rick; Watson, Ben", 1]
    store.close()


def test_legis_token_is_asked_again_after_a_refusal(monkeypatch):
    """legis.ga.gov refuses a token now and then (23 acts failed in a row one evening): asked again after a short
    wait; if it keeps refusing, TokenRefused, and codeacts stops for the run instead of failing every act."""
    import pytest

    from gacrawl.sources.legis import Legis, TokenRefused

    answers = [401, 401, 200]

    class R:
        def __init__(self, status):
            self.status = status

        def json(self):
            return "tok"

    class F:
        def get(self, url, **kw):
            return R(answers.pop(0))

        def deadline(self):
            return None
    monkeypatch.setattr(Legis, "TOKEN_WAITS", (0, 0, 0))
    assert Legis(F(), log=lambda *_: None).token() == "tok"
    answers[:] = [401, 401, 401]
    with pytest.raises(TokenRefused):
        Legis(F(), log=lambda *_: None).token()
