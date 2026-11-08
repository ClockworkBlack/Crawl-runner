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


def explore(fetcher, out: Path, names=(("Burns", "Jon"), ("Kemp", "Brian")), log=print, max_pages: int = 14) -> dict:
    """Save what the search pages answer for a few known filers, following the result's View buttons and the
    report and contribution links behind them, to build the parser on the real pages."""
    out.mkdir(parents=True, exist_ok=True)
    seen: list[dict] = []

    def keep(tag: str, r):
        p = out / f"{len(seen):02d}-{tag}.html"
        p.write_text(r.text, encoding="utf-8")
        s = summary(r.text, r.url)
        s.update({"file": p.name, "status": r.status, "tag": tag})
        seen.append(s)
        log(f"ethics explore: {tag}: HTTP {r.status} {r.url} ({len(s['postbacks'])} postbacks, {len(s['tables'])} tables)")
        return s

    for u, tag in ((CONTRIB_SEARCH, "contrib-form"), (NAME_SEARCH, "name-form")):
        keep(tag, fetcher.get(u, use_cache=False))
    for last, first in names[:1]:
        r = submit(fetcher, NAME_SEARCH, {"txtLast": last, "txtFirst": first}, button="Search")
        keep(f"name-{last}", r)
        frontier, n, done = [(r, 0)], 0, set()
        while frontier and n < max_pages:
            page, depth = frontier.pop(0)
            if depth >= 4:
                continue
            soup = extract.soup_of(page.text)
            nav = {a.get("href") for a in soup.select("table a[href]") if "ctl00_" not in (a.get("id") or "")}
            # what to press: postback links and plain links whose text says view, report, contribution ...
            acts = []
            for a in soup.find_all("a", href=True):
                t, h = a.get_text(" ", strip=True), a["href"]
                m = PB.search(h)
                if m and (FOLLOW.search(t) or FOLLOW.search(m.group(1))):
                    acts.append(("pb", m.group(1), m.group(2), t))
                elif h.lower().endswith(".pdf") or (not m and FOLLOW.search(t) and "media.ethics.ga.gov" in urljoin(page.url, h)
                                                     and not re.search(r"(?i)by(name|office|contribution|expenditure)|reportlog|main search|maps", urljoin(page.url, h))):
                    acts.append(("get", urljoin(page.url, h), "", t))
            for kind, a1, a2, t in acts[:3]:
                key = (kind, a1, a2)
                if key in done:
                    continue
                done.add(key)
                try:
                    r2 = postback(fetcher, page.text, page.url, a1, a2) if kind == "pb" else fetcher.get(a1, use_cache=False)
                except Exception as e:  # noqa: BLE001
                    log(f"ethics explore: {t[:30]}: {type(e).__name__}: {str(e)[:80]}")
                    continue
                n += 1
                if "pdf" in (r2.content_type or ""):
                    log(f"ethics explore: {t[:30]}: a PDF ({len(r2.content)} bytes) {r2.url}")
                    continue
                keep(f"d{depth + 1}-{re.sub(r'[^a-z]+', '', t.lower())[:16]}", r2)
                frontier.append((r2, depth + 1))
    (out / "index.json").write_text(json.dumps(seen, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"pages": len(seen), "out": str(out)}


def run(fetcher, store, root, candidates, deadline) -> dict:
    """The reader proper is built on the pages explore() saves; until then nothing is collected."""
    return {"ethics": "explore first"}
