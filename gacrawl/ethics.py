"""Georgia state and local campaign money from the State Ethics Commission (the Georgia Government Transparency and
Campaign Finance Commission), whose public search site is media.ethics.ga.gov. The owner confirmed (Nov 7) that the
crawler may use its search forms, slowly, one candidate at a time, and read its log of newly filed reports.

Reports filed 2022 on are in the two current systems (EFile, then PeachFile from 2026), read through the public JSON
API their own search pages call (run() below). The older site (2005-2021) has no robots.txt and its pages say robots
"all"; its searches are ASP.NET forms: a page is asked for, its form filled in as a person would
(every field the page itself carries, plus the name typed in) and sent back. Nothing is guessed: every address the
reader follows is a link or form on a page it opened, starting from the search pages the Commission's home page
links to.

    explore    one name search and one contribution search for a few known filers, every page saved to
               data/ethics_explore/ with a summary of its forms, links and tables, to build the parser on real pages
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urljoin

from . import extract

BASE = "https://media.ethics.ga.gov/search/Campaign/"
NAME_SEARCH = BASE + "Campaign_ByName.aspx"
CONTRIB_SEARCH = BASE + "Campaign_ByContributions.aspx"
REPORT_LOG = BASE + "Campaign_ReportLog.aspx"


def form_of(html: str, base_url: str, which: int = 0) -> dict:
    """The page's form: action, and every field with the value the page gives it (hidden fields, checked radios
    and boxes, selected options, text), as a browser would send it."""
    soup = extract.soup_of(html)
    forms = soup.find_all("form")
    if not forms:
        return {"action": base_url, "fields": {}, "buttons": []}
    f = forms[min(which, len(forms) - 1)]
    fields, buttons = {}, []
    for i in f.find_all("input"):
        n, t = i.get("name"), (i.get("type") or "text").lower()
        if not n:
            continue
        if t in ("submit", "button", "image"):
            buttons.append({"name": n, "value": i.get("value") or ""})
        elif t in ("radio", "checkbox"):
            if i.has_attr("checked"):
                fields[n] = i.get("value") or "on"
        else:
            fields[n] = i.get("value") or ""
    for s in f.find_all("select"):
        n = s.get("name")
        if not n:
            continue
        opt = s.find("option", selected=True) or s.find("option")
        fields[n] = (opt.get("value") if opt and opt.has_attr("value") else (opt.get_text(strip=True) if opt else "")) or ""
    for t in f.find_all("textarea"):
        if t.get("name"):
            fields[t["name"]] = t.get_text()
    return {"action": urljoin(base_url, f.get("action") or base_url), "fields": fields, "buttons": buttons}


def submit(fetcher, url: str, values: dict, button: str | None = None, page_html: str | None = None):
    """Open the page (unless its HTML is given), fill its form with `values` (field name endings are enough:
    'txtLast' matches 'ctl00$ContentPlaceHolder1$txtLast'), press `button` (a name ending), send it back."""
    if page_html is None:
        r = fetcher.get(url, use_cache=False)
        page_html, url = r.text, r.url
    F = form_of(page_html, url)
    data = dict(F["fields"])
    for k, v in values.items():
        hit = [n for n in list(data) + [b["name"] for b in F["buttons"]] if n == k or n.endswith("$" + k) or n.endswith(k)]
        data[hit[0] if hit else k] = v
    if button:
        b = next((b for b in F["buttons"] if b["name"].endswith(button) or b["value"] == button), None)
        if b:
            data[b["name"]] = b["value"]
    return fetcher.post(F["action"], data=data, headers={"Referer": url})


def summary(html: str, url: str) -> dict:
    soup = extract.soup_of(html)
    links = [{"text": a.get_text(" ", strip=True)[:80], "href": urljoin(url, a.get("href", ""))}
             for a in soup.find_all("a", href=True)]
    postbacks = [l for l in links if l["href"].startswith("javascript:__doPostBack")]
    tables = []
    for t in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True)[:60] for c in tr.find_all(["td", "th"])] for tr in t.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if len(rows) >= 2 and len(rows[0]) >= 3:
            tables.append({"id": t.get("id") or "", "rows": len(rows), "head": rows[:6]})
    F = form_of(html, url)
    return {"url": url, "title": (soup.title.get_text(strip=True) if soup.title else "")[:120],
            "form": {"action": F["action"], "fields": sorted(F["fields"])[:60], "buttons": F["buttons"][:20]},
            "links": [l for l in links if "media.ethics.ga.gov" in l["href"] and not l["href"].startswith("javascript")][-60:],
            "postbacks": postbacks[:40], "tables": tables[:8], "text": extract.main_text(html)[:1500]}


def postback(fetcher, html: str, url: str, target: str, argument: str = ""):
    """Press a link the page wires to __doPostBack(target, argument): its own form sent back with that event."""
    F = form_of(html, url)
    data = dict(F["fields"])
    data["__EVENTTARGET"], data["__EVENTARGUMENT"] = target, argument
    return fetcher.post(F["action"], data=data, headers={"Referer": url})


PB = re.compile(r"__doPostBack\('([^']+)','([^']*)'\)")
FOLLOW = re.compile(r"(?i)view|detail|report|contribut|itemi|schedule|disclosure|ccdr")


ONCLICK_TBD = re.compile(r"onGridViewRowSelected\('([^']+)'")


def explore(fetcher, out: Path, last: str = "Burns", first: str = "Jon", log=print) -> dict:
    """Save the newer e-filed report pages for one known filer: the name search, the candidate page, the first
    e-filed report (the address its View link opens, from the page itself) and whatever that page links to."""
    out.mkdir(parents=True, exist_ok=True)
    seen: list[dict] = []

    def keep(tag: str, r):
        p = out / f"{len(seen):02d}-{tag}.html"
        p.write_bytes(r.content)
        seen.append({"file": p.name, "status": r.status, "tag": tag, "type": r.content_type, "url": r.url})
        log(f"ethics explore: {tag}: HTTP {r.status} {r.content_type} {r.url} ({len(r.content)} bytes)")

    r = submit(fetcher, NAME_SEARCH, {"txtLast": last, "txtFirst": first}, button="Search")
    m = next((m for m in PB.finditer(r.text) if "lnkView" in m.group(1)), None)
    if not m:
        return {"pages": 0}
    cand = postback(fetcher, r.text, r.url, m.group(1), m.group(2))
    keep("candidate", cand)
    tbd = [u for u in ONCLICK_TBD.findall(cand.text) if "reportTBD" in u] or ONCLICK_TBD.findall(cand.text)
    for rel in tbd[:2]:
        rr = fetcher.get(urljoin(cand.url, rel), use_cache=False)
        keep("efiled", rr)
        if "html" in (rr.content_type or ""):
            soup = extract.soup_of(rr.text)
            for a in soup.find_all(["a", "iframe", "frame", "embed", "object"])[:80]:
                h = a.get("href") or a.get("src") or a.get("data") or ""
                if h and not h.startswith(("javascript", "#", "mailto")):
                    u = urljoin(rr.url, h)
                    if "ethics.ga.gov" in u and not re.search(r"(?i)By(Name|Office|Contributions|Expenditures)|ReportLog|ReportSummary|theethics|\.css|\.js|WebResource|images/", u):
                        keep("efiled-link", fetcher.get(u, use_cache=False))
            for m3 in list(PB.finditer(rr.text))[:3]:
                keep("efiled-pb", postback(fetcher, rr.text, rr.url, m3.group(1), m3.group(2)))
    (out / "index.json").write_text(json.dumps(seen, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"pages": len(seen), "efiled_reports_listed": len(tbd)}


# ----------------------------------------------------------------------------- the current systems' public API
# Both current systems (EFile 2022-2025 at recordsearch.ethics.ga.gov, PeachFile 2026 on at peachfile.ethics.ga.gov)
# are Angular pages over a public JSON API; their Contributions search calls GetTransactionDetails, which is the call
# the reader makes, the same way, with the same body the page sends (seen in data/ethics_probe/clicks.json).
SYSTEMS = {
    "peachfile": "https://api-peachfile.ethics.ga.gov/api/",
    "efile": "https://api-recordsearch.ethics.ga.gov/api/",
}
SINCE_YEAR = 2022
PAGE = 200
GA_SOURCE = "Georgia State Ethics Commission"
# Two Business Day reports repeat, early, gifts the next regular report lists again: never counted
TBD_REPORT = re.compile(r"(?i)two business day|2 business day|\btbd\b|24[- ]hour")
SKIP_SUBTYPE = re.compile(r"(?i)interest|return|refund|loan payment|loan forgiv")


def _body(year: str, page: int, size: int, sort: str = "asc", **kw) -> dict:
    b = {"pageNumber": page, "pageSize": size, "sortBy": "Transaction Date", "sortType": sort, "transactionTypeCode": "TCON",
         "filerName": "", "sourceName": "", "transactionAmountMax": None, "sourceTypeCode": "", "committeeType": "",
         "electionID": "", "reportName": "", "toDate": None, "fromDate": None, "byState": "", "electionType": "",
         "electionYear": year}
    b.update(kw)
    return b


def _api(fetcher, base: str, path: str, body):
    r = fetcher.post(base + path, json_body=body, headers={"Accept": "application/json", "Content-Type": "application/json"})
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status} from {path}: {(r.text or '')[:300]}")
    return json.loads(r.text)


def _nm(s: str) -> str:
    return re.sub(r"[^a-z]+", " ", (s or "").lower()).strip()


def _firsts(first: str) -> set[str]:
    from .finance import Finance

    nick = dict(Finance.NICK, chris={"christopher"}, jen={"jennifer"}, josh={"joshua"}, ron={"ronald"}, don={"donald"},
                ed={"edward"}, al={"albert", "alan"}, larry={"lawrence"}, jerry={"gerald"}, terry={"terrence"},
                brad={"bradford", "bradley"}, charlie={"charles"}, will={"william"}, billy={"william"}, bobby={"robert"},
                rob={"robert"}, johnny={"john"}, jack={"john"}, kate={"katherine", "kathryn"}, sue={"susan"}, nate={"nathan", "nathaniel"},
                zach={"zachary"}, alex={"alexander", "alexandra"}, fran={"frances", "francis"}, ken={"kenneth"}, ray={"raymond"})
    out = {first} | set(nick.get(first, ()))
    out |= {k for k, v in nick.items() if first in v}
    return out


def candidate_index(candidates) -> dict:
    """'first last' -> candidate (with nicknames both ways: Chris / Christopher), for names that only one Electo
    candidate carries (state and local offices)."""
    idx: dict[str, list] = {}
    for c in candidates:
        for v in {c.get("name", ""), *(c.get("variants") or []), *(c.get("aliases") or [])}:
            w = [x for x in _nm(v).split() if x not in ("jr", "sr", "ii", "iii", "iv")]
            if len(w) >= 2:
                for f in _firsts(w[0]):
                    k = f"{f} {w[-1]}"
                    if c not in idx.setdefault(k, []):
                        idx[k].append(c)
    return {k: v[0] for k, v in idx.items() if len(v) == 1}


def match(t: dict, cands: dict):
    """The Electo candidate a filing belongs to: the filer's own name when the filer is a candidate, or a full name
    inside a campaign committee's name ("Friends of Blake Tillery"), when exactly one candidate carries it."""
    w = _who(t)
    if w:
        ws = [x for x in w.split() if x not in ("jr", "sr", "ii", "iii", "iv")]
        for k in [w, f"{ws[0]} {ws[-1]}"] + ([f"{ws[1]} {ws[-1]}"] if len(ws) > 2 and len(ws[1]) > 2 else []):
            if k in cands:          # first name, or the middle name the person goes by
                return cands[k]
    ft = (t.get("filerTypeDesc") or t.get("filerTypeCode") or "").lower()
    if "leadership" in ft or "independent" in ft or "party" in ft:
        return None
    words = [x for x in _nm(t.get("filerName") or "").split() if x not in ("jr", "sr", "ii", "iii")]
    hits = {cands[f"{a} {b}"]["id"]: cands[f"{a} {b}"] for a, b in zip(words, words[1:]) if f"{a} {b}" in cands}
    hits.update({cands[f"{a} {c}"]["id"]: cands[f"{a} {c}"] for a, b, c in zip(words, words[1:], words[2:])
                 if len(b) <= 2 and f"{a} {c}" in cands})     # a middle initial
    return next(iter(hits.values())) if len(hits) == 1 else None


def _who(t: dict) -> str:
    """The filer's own name as 'first last' when the filer is a candidate, else ''."""
    fn, ln = (t.get("candidateFirstName") or "").strip(), (t.get("candidateLastName") or "").strip()
    if fn and ln:
        return _nm(fn + " " + ln)
    f = t.get("filerName") or ""
    ft = (t.get("filerTypeDesc") or "").lower()
    if ((t.get("filerTypeCode") or "").upper() in ("CAN", "C") or (ft.startswith("candidate") and "committee" not in ft)) and "," in f:
        last, first = f.split(",", 1)
        last = re.sub(r"(?i)\b(jr|sr|ii|iii|iv)\b\.?", "", last)
        return _nm(" ".join(first.replace(",", " ").split()) + " " + last) if first.split() else ""
    return ""


def kind_of(t: dict) -> str | None:
    sub = " ".join(str(t.get(k) or "") for k in ("transactionSubTypeDesc", "transactionCategory", "transactionTypeCodeDescription"))
    if SKIP_SUBTYPE.search(sub):
        return None
    if re.search(r"(?i)in[- ]?kind", sub):
        return "in_kind"
    if re.search(r"(?i)loan", sub):
        return "loan"
    return "contribution"


def record_of(system: str, t: dict, cands: dict):
    """One transaction as a contribution record, or None when it does not count (a Two Business Day report, interest,
    a return)."""
    from .finance import contribution_record

    if TBD_REPORT.search(t.get("reportName") or ""):
        return None
    kind = kind_of(t)
    if kind is None or t.get("transactionAmount") in (None, ""):
        return None
    unitem = bool(re.search(r"(?i)unitemi", (t.get("transactionSubTypeDesc") or "") + " " + (t.get("transactionCategory") or "")))
    payee = " ".join(x for x in (t.get("payeeFirstName"), t.get("payeeMiddleName"), t.get("payeeLastName")) if x).strip()
    name = (t.get("sourceName") or payee or "").strip()
    if payee and "," not in name and (t.get("transactionSourceTypeCode") or "").upper() in ("IND", "TIND"):
        name = f"{t.get('payeeLastName')}, {t.get('payeeFirstName')}".strip(", ")
    d = (t.get("transactionDate") or "")[:10]
    if re.match(r"\d\d/\d\d/\d{4}", d):
        d = f"{d[6:10]}-{d[0:2]}-{d[3:5]}"
    c = match(t, cands)
    recipient = c["name"] if c else (t.get("filerName") or "").strip()
    row = {"name": "" if unitem else name, "amount": float(t["transactionAmount"]), "date": d, "kind": kind,
           "employer": t.get("payeeEmployer") or "", "occupation": t.get("payeeOccupation") or "",
           "city": t.get("transactionSourceCity") or "", "state": (t.get("transactionSourceStateCode") or "")[:2],
           "election": t.get("electionTypeDescription") or "", "line": str(t.get("transactionId") or "")}
    url = ("https://peachfile.ethics.ga.gov/public/cf/contributions" if system == "peachfile"
           else "https://recordsearch.ethics.ga.gov/public/cf/contributions")
    rec = contribution_record(f"cand:{c['id']}" if c else "ga-ethics", url, row, recipient,
                              "candidate" if (c or _who(t)) else "committee", GA_SOURCE,
                              jurisdiction=(c or {}).get("jurisdiction", ""), channel="api")
    rec.key = f"ga2|{system}|{t.get('transactionId')}"
    rec.meta.update({"line": str(t.get("transactionId") or ""), "ga_system": system, "ga_filer": t.get("filerRegistrationGuid") or "", "filer_name": t.get("filerName") or "",
                     "filer_type": t.get("filerTypeDesc") or t.get("filerTypeCode") or "",
                     "report_name": t.get("reportName") or "", "report_id": t.get("filerReportId"),
                     "report_version": t.get("filerReportVersionId") or 1, "subtype": t.get("transactionSubTypeDesc") or "",
                     "source_type": t.get("transactionSource") or "", "transaction_id": t.get("transactionId"),
                     **({"unitemized": True} if unitem else {}),
                     **({"candidate_id": c["id"], "electo_key": c.get("electo_key")} if c else {})})
    return rec


def years(fetcher, base: str, log=print) -> list[str]:
    """The election years the system's search offers, newest first; '' (every year at once, as EFile's own search
    asks) when it offers none."""
    try:
        d = _api(fetcher, base, "PublicLookup/GetPublicTransactionElectionYearLookup", {"key": "electionyears", "isSelectReq": False})
    except Exception as e:  # noqa: BLE001
        log(f"ethics: election years from {base}: {str(e)[:200]}")
        return [""]
    ys = sorted({str(x.get("code") or x.get("name") or x.get("value") or "")[:4] for x in (d.get("data") or [])}, reverse=True)
    ys = [y for y in ys if y.isdigit() and SINCE_YEAR <= int(y) <= dt.date.today().year + 1]
    return ys or [""]


EXTRA = {"efile": {"filerRegistrationGuid": None}}


def shape(fetcher, system: str, base: str, year: str, log=print):
    """The page size and order this system answers (the search pages themselves ask for 10 at a time, newest first)."""
    last = ""
    for size in (100, 50, 25, 10):
        for sort in ("asc", "desc"):
            try:
                _api(fetcher, base, "PublicTransactionDetails/GetTransactionDetails", _body(year, 1, size, sort, **EXTRA.get(system, {})))
                return size, sort
            except Exception as e:  # noqa: BLE001
                last = str(e)[:300]
    log(f"ethics {system}: no page size answered; last: {last}")
    return None


def drop_superseded(store) -> int:
    """An amended report replaces the one it amends: lines from an older version of the same filer's same report go."""
    return store.delete_where(
        "kind='contribution' AND key LIKE 'ga2|%' AND EXISTS (SELECT 1 FROM records r2 WHERE r2.kind='contribution' "
        "AND r2.key LIKE 'ga2|%' AND json_extract(r2.meta,'$.ga_filer')=json_extract(records.meta,'$.ga_filer') "
        "AND json_extract(r2.meta,'$.report_name')=json_extract(records.meta,'$.report_name') "
        "AND json_extract(r2.meta,'$.ga_system')=json_extract(records.meta,'$.ga_system') "
        "AND (json_extract(r2.meta,'$.report_version') > json_extract(records.meta,'$.report_version') "
        "OR (json_extract(r2.meta,'$.report_version') = json_extract(records.meta,'$.report_version') "
        "AND json_extract(r2.meta,'$.report_id') > json_extract(records.meta,'$.report_id'))))")


def run(fetcher, store, root, candidates, deadline, log=print) -> dict:
    """Every contribution, in-kind gift and loan filed with the Commission since SINCE_YEAR, from both current systems,
    a page at a time (resumable: the page reached is kept per system and election year). A year read in full is read
    again from its newest lines each run (late and amended filings), up to the first page with nothing new."""
    import time

    cands = candidate_index([c for c in candidates if "federal" not in (c.get("levels") or [])])
    st = {"added": 0, "pages": 0, "skipped": 0, "years": {}}
    end = deadline
    for i, (system, base) in enumerate(SYSTEMS.items()):
        deadline = time.monotonic() + (end - time.monotonic()) / (len(SYSTEMS) - i)    # each system its share
        ys = years(fetcher, base, log)
        sh = shape(fetcher, system, base, ys[0], log)
        if not sh:
            continue
        size, order = sh
        log(f"ethics {system}: years {ys}, {size} lines a page, {order}")
        for y in ys:
            ck = f"ga_ethics:{system}:{y or 'all'}"
            cur = json.loads(store.kv_get(ck) or "{}")
            # a year read in full is read in full again once a month (filings that arrive late carry old dates)
            full = cur.get("complete", False) and cur.get("at", "") >= (dt.date.today() - dt.timedelta(days=30)).isoformat()
            page = 1 if full else int(cur.get("page", 1))
            sort = "desc" if full else order
            got = 0
            while time.monotonic() < deadline:
                try:
                    d = _api(fetcher, base, "PublicTransactionDetails/GetTransactionDetails", _body(y, page, size, sort, **EXTRA.get(system, {})))
                except Exception as e:  # noqa: BLE001
                    log(f"ethics {system} {y} page {page}: {type(e).__name__}: {str(e)[:160]}")
                    break
                items = (d.get("data") or {}).get("items") or []
                st["pages"] += 1
                new = 0
                for t in items:
                    rec = record_of(system, t, cands)
                    if rec is None:
                        st["skipped"] += 1
                        continue
                    if store.get_meta(rec.key) is None:
                        new += 1
                    if store.add(rec):
                        got += 1
                total = int((items[0].get("totalRows") if items else 0) or 0)
                if not full:
                    store.kv_set(ck, json.dumps({"page": page + 1, "total": total}))
                if len(items) < size or (full and new == 0):
                    if not full:
                        store.kv_set(ck, json.dumps({"complete": True, "total": total, "at": dt.date.today().isoformat()}))
                    break
                page += 1
            st["added"] += got
            st["years"][f"{system}:{y}"] = got
            log(f"ethics {system} {y}: {got} lines added or changed ({'refresh' if full else 'full read'}, page {page})")
            if time.monotonic() >= deadline:
                break
    st["superseded"] = drop_superseded(store)
    log(f"ethics: {st['added']} lines, {st['skipped']} not counted (Two Business Day reports, interest, returns), "
        f"{st['superseded']} replaced by amended reports")
    return st


def click_probe(out: Path, sites=("https://peachfile.ethics.ga.gov/", "https://recordsearch.ethics.ga.gov/"),
                clicks=("Download Bulk Data", "Contribution Information", "Filed Disclosure Reports", "Local Filings"),
                log=print) -> dict:
    """The Commission's current systems (PeachFile, 2026 on; EFile, 2022-2025) are browser apps that load their data
    from their own public API after a click. This opens each in headless Chrome, clicks the public data buttons as a
    person would, and records the API requests each click makes (address, method, request body, the start of the
    answer) and any file it offers to download, so the reader can call the same public endpoints."""
    import os
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    found = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel=os.environ.get("GACRAWL_BROWSER_CHANNEL") or None)
        for site in sites:
            for label in clicks:
                ctx = b.new_context(user_agent=os.environ.get("GACRAWL_USER_AGENT") or None, accept_downloads=True)
                page = ctx.new_page()
                reqs = []

                def on_resp(resp, reqs=reqs):
                    u = resp.url
                    if "/api/" not in u:
                        return
                    try:
                        body = resp.text()[:3000]
                    except Exception:  # noqa: BLE001
                        body = ""
                    reqs.append({"method": resp.request.method, "url": u, "status": resp.status,
                                 "post": (resp.request.post_data or "")[:2000], "answer": body})
                page.on("response", on_resp)
                downloads = []
                page.on("download", lambda d, downloads=downloads: downloads.append({"url": d.url, "name": d.suggested_filename}))
                try:
                    page.goto(site, wait_until="networkidle", timeout=60000)
                    n0 = len(reqs)
                    page.get_by_text(label, exact=False).first.click(timeout=15000)
                    page.wait_for_timeout(6000)
                    # a page that lists things: also press its first search / download button
                    for name in ("Search", "Download", "Export", "CSV"):
                        btn = page.get_by_role("button", name=name)
                        if btn.count():
                            btn.first.click(timeout=8000)
                            page.wait_for_timeout(6000)
                            break
                    rec = {"site": site, "click": label, "page": page.url, "requests": reqs[n0:], "downloads": downloads,
                           "text": page.inner_text("body")[:2500]}
                except Exception as e:  # noqa: BLE001
                    rec = {"site": site, "click": label, "error": f"{type(e).__name__}: {str(e)[:200]}", "requests": reqs}
                found.append(rec)
                log(f"click probe: {site} '{label}': {len(rec.get('requests', []))} API calls, {len(downloads)} downloads"
                    + (f" ({rec['error']})" if rec.get("error") else ""))
                ctx.close()
        b.close()
    (out / "clicks.json").write_text(json.dumps(found, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"clicks": len(found)}


def bulk_probe(out: Path, sites=("https://peachfile.ethics.ga.gov/", "https://recordsearch.ethics.ga.gov/"), log=print) -> dict:
    """On each system's Download Bulk Data page (reached by clicking its button, as a person would), click the first
    contribution file's download control and record the API call and the file it gives (name, size, first lines)."""
    import os
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    found = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel=os.environ.get("GACRAWL_BROWSER_CHANNEL") or None)
        for site in sites:
            ctx = b.new_context(user_agent=os.environ.get("GACRAWL_USER_AGENT") or None, accept_downloads=True)
            page = ctx.new_page()
            reqs = []
            page.on("request", lambda r, reqs=reqs: reqs.append({"method": r.method, "url": r.url, "post": (r.post_data or "")[:1500]})
                    if "/api/" in r.url else None)
            rec = {"site": site}
            try:
                page.goto(site, wait_until="networkidle", timeout=60000)
                page.get_by_text("Download Bulk Data", exact=False).first.click(timeout=15000)
                page.wait_for_timeout(5000)
                rec["page"] = page.url
                rec["html"] = page.content()[:60000]
                n0 = len(reqs)
                # the first clickable thing in the contributions row: a link, a button or an icon
                row = page.locator("text=Contributions, Loans and Interest Earned").first
                target = row.locator("xpath=ancestor::*[self::tr or contains(@class,'row') or contains(@class,'card')][1]")
                ctl = target.locator("a, button, mat-icon, i, [role=button], select").first
                rec["control"] = ctl.evaluate("e => e.outerHTML.slice(0, 500)")
                try:
                    with page.expect_download(timeout=60000) as dl:
                        ctl.click(timeout=10000)
                        page.wait_for_timeout(3000)
                        opts = page.locator("mat-option, option, [role=option]")
                        if opts.count():
                            rec["options"] = [opts.nth(i).inner_text()[:40] for i in range(min(opts.count(), 12))]
                            opts.first.click()
                            page.wait_for_timeout(2000)
                            btn = page.get_by_role("button", name="Download")
                            if btn.count():
                                btn.first.click()
                    d = dl.value
                    path = out / ("bulk-" + re.sub(r"[^a-z]+", "", site.split("//")[1].split(".")[0]) + "-" + d.suggested_filename)
                    d.save_as(str(path))
                    rec["download"] = {"url": d.url, "name": d.suggested_filename, "bytes": path.stat().st_size,
                                       "head": path.read_bytes()[:1500].decode("utf-8", "replace")}
                    if path.stat().st_size > 5_000_000:
                        path.unlink()          # keep only the sample in the record
                except Exception as e:  # noqa: BLE001
                    rec["download_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                rec["requests"] = reqs[n0:]
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            found.append(rec)
            log(f"bulk probe: {site}: {rec.get('download', {}).get('name') or rec.get('download_error') or rec.get('error')}; "
                f"{len(rec.get('requests', []))} API calls")
            ctx.close()
        b.close()
    (out / "bulk.json").write_text(json.dumps(found, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"sites": len(found)}
