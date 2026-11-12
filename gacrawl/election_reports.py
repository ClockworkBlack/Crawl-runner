"""County election reports for the city seats the state's results system does not hold.

Georgia's results system (results.sos.ga.gov) holds county-run city elections from Nov. 2024 on, and its older
archive refuses automated readers. A seat last filled in 2021 or 2023 is therefore only in the county election
office's own report: the Election Summary Report every county's tabulator prints, posted as a PDF on the county's
site (or inside its election board's certification packet). Without it, a city's council list on Electo shows only
the seats filled since Nov. 2024 (Nov. 12, 2026: 121 of 278 cities had no mayor's race on file; Dunwoody showed three
of its seven seats).

For each county and each election in TARGETS, this step:
  1. searches the web (gacrawl.websearch: DuckDuckGo, cached 30 days, a cap a run) for the county's summary report
     of that election day;
  2. opens only official or archival copies: the county's own site, any .gov / .us / ga.gov host, or the University
     of Florida Election Lab's archive of local results (it keeps the counties' own PDFs);
  3. keeps the document only when it is that report (gacrawl.elections.is_summary_report) and names that election
     day under its heading;
  4. stores it as a `document` of `<county id>:elections`, which the elections deriver (gacrawl derive) reads into
     election_result records; the export sends them to Electo (elections.json -> crawl_elections.json -> the city
     council lists).

What was found or not, per county and election, is kept in kv `elections:report:<county>:<date>` and summed up by
`gacrawl elections reports --status` (and the coverage report). A county not found is searched again after
RETRY_DAYS, at most MAX_TRIES times; nothing is built from a guessed URL.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from urllib.parse import urlsplit

# the city general elections whose seats are still serving (four-year terms) and their runoffs
TARGETS = [("2023-11-07", "November 7, 2023", "general"), ("2021-11-02", "November 2, 2021", "general"),
           ("2023-12-05", "December 5, 2023", "runoff"), ("2021-11-30", "November 30, 2021", "runoff")]
RETRY_DAYS, MAX_TRIES = 30, 3
ARCHIVES = ("election.lab.ufl.edu",)        # keeps the counties' own result PDFs (University of Florida Election Lab)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December"]


def _official(url: str, county_host: str) -> bool:
    h = urlsplit(url).netloc.lower().removeprefix("www.")
    return bool(h) and (h == county_host or h.endswith(("." + county_host,)) or h.endswith((".gov", ".us", ".ga.gov")) or
                        h in ARCHIVES)


def _date_forms(iso: str) -> list[str]:
    d = dt.date.fromisoformat(iso)
    m = MONTHS[d.month - 1]
    return [f"{m} {d.day:02d}, {d.year}", f"{m} {d.day}, {d.year}", f"{d.month:02d}/{d.day:02d}/{d.year}", f"{d.month}/{d.day}/{d.year}"]


def is_report_of(text: str, iso: str) -> bool:
    """The county's summary report for this election day: the report's layout, and the day named near its heading."""
    from .elections import is_summary_report

    if not is_summary_report(text):
        return False
    i = text.find("Election Summary Report")
    head = text[max(0, i - 200):i + 800] if i >= 0 else text[:3000]
    return any(f.lower() in head.lower() for f in _date_forms(iso))


def queries(county: str, iso: str, label: str, kind: str, host: str) -> list[str]:
    name = county.removesuffix(" County")
    q = [f'"{name} County" Georgia "Election Summary Report" "{label}" pdf',
         f'site:{host} election summary report {label}' if host else "",
         f'"{name} County" Georgia {label} municipal {"runoff " if kind == "runoff" else ""}election results summary report']
    return [x for x in q if x]


def run(fetcher, store, root, state_dir, deadline: float | None = None, counties: list[str] | None = None, cap: int = 100,
        dry_run: bool = False, log=print) -> dict:
    from . import registry
    from .ingest import pdf_to_record
    from .store import Record
    from .websearch import Search

    out = {"searched": 0, "found": 0, "stored": 0, "not_found": 0, "skipped": 0, "kept": [], "errors": []}
    jur = [j for j in registry.jurisdictions(root) if j["kind"] == "county"]
    if counties:
        jur = [j for j in jur if j["id"] in counties]
    # counties with the most people first: most cities, most seats
    jur.sort(key=lambda j: -(j.get("population") or 0))
    s = Search(state_dir, log=log, cap=cap)
    today = dt.date.today()

    def late():
        return deadline is not None and time.monotonic() > deadline

    try:
        for iso, label, kind in TARGETS:
            for j in jur:
                if late() or s.left() <= 0:
                    raise StopIteration
                kk = f"elections:report:{j['id']}:{iso}"
                st = json.loads(store.kv_get(kk) or "{}")
                if st.get("url") or (st.get("tries", 0) >= MAX_TRIES) or \
                        (st.get("at") and (today - dt.date.fromisoformat(st["at"])).days < RETRY_DAYS):
                    out["skipped"] += 1
                    continue
                host = urlsplit(j.get("website") or "").netloc.lower().removeprefix("www.")
                seen, found = set(), None
                for q in queries(j["name"], iso, label, kind, host):
                    if found or s.left() <= 0 or late():
                        break
                    out["searched"] += 1
                    for r in s.search(q, count=10):
                        u = r.get("url") or ""
                        if u in seen or not _official(u, host):
                            continue
                        seen.add(u)
                        if not (u.lower().split("?")[0].endswith(".pdf") or "pdf" in (r.get("title") or "").lower()
                                or "showpublisheddocument" in u.lower() or "/files/" in u.lower()):
                            continue
                        try:
                            fr = fetcher.get(u)
                        except Exception as exc:  # noqa: BLE001 — refused or out of reach: the next result
                            out["errors"].append(f"{j['id']} {u}: {type(exc).__name__}"[:160])
                            continue
                        if fr.status != 200 or not fr.content[:5] == b"%PDF-":
                            continue
                        rec = pdf_to_record(fr.content, f"{j['id']}:elections", "document", url=fr.url,
                                            title=f"{j['name']} election summary report, {label}", ocr_max_pages=0)
                        text = (rec.text if rec else "") or ""
                        if not is_report_of(text, iso):
                            continue
                        found = (fr.url, rec, text)
                        break
                st = {"at": today.isoformat(), "tries": st.get("tries", 0) + (0 if found else 1)}
                if found:
                    u, rec, text = found
                    st["url"] = u
                    out["found"] += 1
                    out["kept"].append({"county": j["id"], "election": iso, "url": u})
                    if not dry_run:
                        rec.jurisdiction = j["id"]
                        rec.evidence = "primary"
                        rec.meta = {**(rec.meta or {}), "election_report": {"date": iso, "kind": kind, "county": j["name"],
                                                                            "archive": urlsplit(u).netloc in ARCHIVES},
                                    "opened": True}
                        out["stored"] += store.add(rec)
                    log(f"elections: {j['name']} {label}: {u}")
                else:
                    out["not_found"] += 1
                if not dry_run:
                    store.kv_set(kk, json.dumps(st))
    except StopIteration:
        pass
    finally:
        s.save()
        if not dry_run:
            store.flush()
    out["status"] = status(store, root) if not dry_run else None
    return out


def status(store, root) -> dict:
    """Per election: counties whose report is on file, not found yet, not searched yet."""
    from . import registry

    cos = [j["id"] for j in registry.jurisdictions(root) if j["kind"] == "county"]
    res = {}
    for iso, label, kind in TARGETS:
        got, miss, todo = [], [], []
        for c in cos:
            st = json.loads(store.kv_get(f"elections:report:{c}:{iso}") or "{}")
            (got if st.get("url") else miss if st else todo).append(c)
        res[iso] = {"found": len(got), "not_found": len(miss), "not_searched": len(todo), "missing": miss[:200]}
    return res
