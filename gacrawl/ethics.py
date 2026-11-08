"""Georgia state and local campaign money from the State Ethics Commission (the Georgia Government Transparency and
Campaign Finance Commission), whose public search site is media.ethics.ga.gov. The owner confirmed (Nov 7) that the
crawler may use its search forms, slowly, one candidate at a time, and read its log of newly filed reports.

The site has no robots.txt and its pages say robots "all"; it offers no API or bulk download (the Commission's
records page is gone), and its searches are ASP.NET forms: a page is asked for, its form filled in as a person would
(every field the page itself carries, plus the name typed in) and sent back. Nothing is guessed: every address the
reader follows is a link or form on a page it opened, starting from the search pages the Commission's home page
links to.

    explore    one name search and one contribution search for a few known filers, every page saved to
               data/ethics_explore/ with a summary of its forms, links and tables, to build the parser on real pages
"""
from __future__ import annotations

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


def run(fetcher, store, root, candidates, deadline) -> dict:
    """The reader proper is built on the pages explore() saves; until then nothing is collected."""
    return {"ethics": "explore first"}


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
