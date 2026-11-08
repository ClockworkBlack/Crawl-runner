import json
import time

from gacrawl import donors_ai, llm
from gacrawl.store import Record, Store


def _setup(tmp_path):
    s = Store(tmp_path / "data")
    s.add(Record("cand:a", "contribution", "https://docquery.fec.gov/1", key="k1",
                 meta={"type": "contribution", "name": "AFLAC PAC", "amount": 5000, "date": "2026-01-01", "recipient": "Ann Able",
                       "candidate_id": "a", "donor_entity": "e1", "filing_source": "CCDR"}))
    s.add(Record("org", "organization", "https://api.open.fec.gov/v1/committee/C1/", key="org|e1",
                 meta={"entity_id": "e1", "name": "AFLAC PAC", "connected_org": "AFLAC INCORPORATED",
                       "sources": [{"t": "FEC committee C1", "u": "https://api.open.fec.gov/v1/committee/C1/", "registry": "fec"}],
                       "what": [{"text": "Registered with the FEC as a PAC - qualified, Unauthorized, Corporation",
                                 "src": "https://api.open.fec.gov/v1/committee/C1/", "basis": "FEC registration"}]}))
    s.flush()
    return s


def test_kept_when_supported(tmp_path, monkeypatch):
    s = _setup(tmp_path)
    answers = iter(["AFLAC PAC is registered with the FEC as a qualified PAC connected to AFLAC Incorporated.", "OK"])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(answers))
    st = donors_ai.run(s, tmp_path, time.monotonic() + 60, log=lambda *a: None)
    a = json.loads(s.kv_get("donor_about"))["e1"]
    assert st["kept"] == 1 and a["t"].startswith("AFLAC PAC") and a["u"] == ["https://api.open.fec.gov/v1/committee/C1/"]
    # nothing changed: not read again
    assert donors_ai.run(s, tmp_path, time.monotonic() + 60, log=lambda *a: None)["todo"] == 0


def test_dropped_when_it_adds_things(tmp_path, monkeypatch):
    s = _setup(tmp_path)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "AFLAC PAC is a powerful lobby that backs conservative candidates in 2026.")
    donors_ai.run(s, tmp_path, time.monotonic() + 60, log=lambda *a: None)
    a = json.loads(s.kv_get("donor_about"))["e1"]
    assert "t" not in a and a["why"]
