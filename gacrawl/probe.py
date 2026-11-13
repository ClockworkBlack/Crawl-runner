"""`gacrawl probe URL...`: look at a site the way the crawler would, to build or fix a reader for it.

For each URL, with the crawler's own fetcher (robots.txt, rate limits, user agent, block list: a refused or
bot-protected site is reported, never worked around):
  <out>/<n>/fetch.html       the raw response body (and fetch.json: status, headers, final URL)
  <out>/<n>/links.json       every link and every form (action, method, fields) in the raw HTML
and with --browser, one visit in headless Chrome as a person's browser would make it:
  <out>/<n>/rendered.html    the page after its scripts ran
  <out>/<n>/network.json     the requests the page itself made (method, URL, type, status; JSON bodies of XHR and
                             fetch calls, truncated), which is where a script-built page's data comes from
The workflow's `probe` job runs this and uploads <out> as an artifact.
"""
from __future__ import annotations

import json
from .registry import write_json_atomic
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import extract
from .blocked import Blocked, looks_blocked
from .fetch import Disallowed, Fetcher


def _forms(html: str, base: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html, re.I | re.S):
        attrs, body = m.group(1), m.group(2)
        act = re.search(r'action\s*=\s*["\']([^"\']*)', attrs, re.I)
        meth = re.search(r'method\s*=\s*["\']([^"\']*)', attrs, re.I)
        fields = re.findall(r'<(?:input|select|textarea)\b[^>]*name\s*=\s*["\']([^"\']+)', body, re.I)
        out.append({"action": urljoin(base, act.group(1)) if act else base, "method": (meth.group(1) if meth else "get").lower(),
                    "fields": fields[:80]})
    return out


def probe(fetcher: Fetcher, urls: list[str], out: Path, browser: bool = False, log=print, accept: str = "",
          grep: str = "") -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for i, url in enumerate(urls):
        d = out / f"{i:02d}"
        d.mkdir(exist_ok=True)
        row = {"url": url}
        try:
            r = fetcher.get(url, headers={"Accept": accept} if accept else None, use_cache=False)
            (d / "fetch.html").write_bytes(r.content)
            write_json_atomic((d / "fetch.json"), {"url": r.url, "status": r.status, "headers": r.headers}, indent=1)
            html = r.text if "html" in r.content_type else ""
            onclick = re.findall(r'on(?:click|change)\s*=\s*["\']([^"\']{0,300})', html, re.I)
            (d / "links.json").write_text(json.dumps({"links": extract.links(html, r.url) if html else [],
                                                      "forms": _forms(html, r.url) if html else [],
                                                      "onclick": onclick[:200],
                                                      "scripts": re.findall(r'<script[^>]+src\s*=\s*["\']([^"\']+)', html, re.I)[:60]},
                                                     indent=1))
            row.update(status=r.status, bytes=len(r.content), final=r.url, content_type=r.content_type,
                       seconds=round(r.elapsed, 1))
            if "csv" in r.content_type or "text/plain" in r.content_type or r.url.lower().endswith(".csv"):
                text = r.content.decode("utf-8", "replace")
                row["body"] = text[:1500]                                  # a data file's header and first rows
                if grep:
                    rx = re.compile(grep, re.I)
                    row["matches"] = [l[:400] for l in text.splitlines() if rx.search(l)][:25]
            if grep and ("javascript" in r.content_type or r.url.lower().split("?")[0].endswith(".js")):
                # a script bundle is one long line: each match with the code around it (an app's API paths and calls)
                text = r.content.decode("utf-8", "replace")
                rx = re.compile(grep, re.I)
                row["matches"] = [text[max(0, m.start() - 160):m.end() + 240] for m in rx.finditer(text)][:60]
            if grep and html:
                # a page: each match with the markup around it (how a listing is built: table, list, heading, link)
                rx = re.compile(grep, re.I)
                # scripts are code and are left out, except the data a page ships for its own rendering (a Next.js
                # page's whole content is in its __NEXT_DATA__ script, not in the markup)
                flat = re.sub(r"\s+", " ", re.sub(r"(?is)<style\b.*?</style>|<script\b(?![^>]*(?:__NEXT_DATA__|application/(?:ld\+)?json))[^>]*>.*?</script>", " ", html))
                row["matches"] = [flat[max(0, m.start() - 220):m.end() + 320] for m in rx.finditer(flat)][:30]
            if "spreadsheetml" in r.content_type or "ms-excel" in r.content_type or r.url.lower().endswith((".xlsx", ".xls")):
                try:                                 # a spreadsheet: its sheets and first rows
                    import io

                    import openpyxl

                    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
                    row["sheets"] = {ws.title: [[("" if v is None else str(v))[:40] for v in rr][:80]
                                                for rr in ws.iter_rows(max_row=3, values_only=True)] for ws in wb.worksheets[:3]}
                except Exception as exc:  # noqa: BLE001
                    row["sheets"] = f"{type(exc).__name__}: {exc}"[:200]
            if "json" in r.content_type:
                row["body"] = r.text[:1500]          # the start of an API answer, readable from the log
                row["shape"] = _json_shape(r.text)   # and the shape of its first item: keys, nested lists and objects
        except (Disallowed, Blocked) as exc:
            row["refused"] = f"{type(exc).__name__}: {exc}"
            summary.append(row)
            log(json.dumps(row))
            continue
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"[-600:]      # the end of the message holds the cause
        if browser and not row.get("refused"):
            row.update(_browser_visit(fetcher, url, d))
        summary.append(row)
        log(json.dumps(row))
    write_json_atomic((out / "summary.json"), summary, indent=1)
    return summary


def _json_shape(text: str) -> dict:
    """What an API answer is made of: its top-level keys, how many items it lists, and the first item's keys with
    its nested lists and objects (where a reader finds files, links and sub-records)."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    out: dict = {}
    items = None
    if isinstance(data, dict):
        out["keys"] = list(data)[:40]
        for k in ("value", "items", "results", "data", "records"):
            if isinstance(data.get(k), list):
                items = data[k]
                out["list"] = k
                break
    elif isinstance(data, list):
        items = data
    if items is not None:
        out["count"] = len(items)
        if items and isinstance(items[0], dict):
            first = items[0]
            out["item_keys"] = list(first)[:80]
            out["nested"] = {k: json.dumps(v)[:400] for k, v in first.items() if isinstance(v, (list, dict))}
    return out


def _browser_visit(fetcher: Fetcher, url: str, d: Path) -> dict:
    from playwright.sync_api import sync_playwright

    from .fetch import launch_browser

    net: list[dict] = []
    with sync_playwright() as p:
        b = launch_browser(p)
        page = b.new_page(user_agent=fetcher.user_agent)

        def on_response(resp):
            req = resp.request
            row = {"method": req.method, "url": resp.url, "type": req.resource_type, "status": resp.status}
            if req.resource_type in ("xhr", "fetch"):
                if req.method != "GET":
                    row["post_data"] = (req.post_data or "")[:2000]
                try:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct or "xml" in ct or "text" in ct:
                        row["body"] = resp.text()[:20000]
                except Exception:  # noqa: BLE001
                    pass
            net.append(row)

        page.on("response", on_response)
        fetcher.limiter.wait(url, fetcher.robots.crawl_delay(url))
        try:
            # pages that keep a connection open never go "network idle": load, then give the scripts time
            resp = page.goto(url, wait_until="load", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(8000)
            status = resp.status if resp else 0
            html = page.content()
        except Exception as exc:  # noqa: BLE001
            b.close()
            return {"browser_error": f"{type(exc).__name__}: {exc}"[:300]}
        b.close()
    reason = looks_blocked(status, html.encode("utf-8", "ignore"))
    if reason:
        fetcher.blocklist.add(url, reason, "probe")
        return {"browser_refused": reason}
    (d / "rendered.html").write_text(html, encoding="utf-8")
    write_json_atomic((d / "network.json"), net, indent=1)
    host = urlsplit(url).netloc
    return {"browser_status": status, "rendered_bytes": len(html), "requests": len(net),
            "xhr": [n["url"] for n in net if n["type"] in ("xhr", "fetch") and urlsplit(n["url"]).netloc == host][:20]}
