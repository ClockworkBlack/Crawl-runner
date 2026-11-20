"""Sources that refuse the crawler, read from the Internet Archive's copies (`gacrawl archive refused`).

The owner's rule since Nov. 19, 2026: a public page that refuses the crawler (a 403, a bot-protection challenge, a
robots.txt refusal) may be read from the Wayback Machine's existing copies. 296 sources were refused when the rule
changed: 158 official websites (Atlanta, Cherokee, Houston, Newton and Walton counties, Smyrna, Dunwoody and the
others whose CivicPlus or Granicus firewalls refuse data-center addresses), 18 agenda centers, state pages (the
Attorney General's opinions and press, the Secretary of State's elections pages, the Ethics Commission's meetings)
and 107 school boards' Simbli policy manuals (read by gacrawl/simbli_archive.py, so left out here).

For each refused source, at most PAGES_PER_SOURCE of its own pages as last captured within RECENT_DAYS (agendas,
minutes, ordinances and council pages first, the source's own include and exclude rules applied), read at the
Archive's pace (gacrawl/wayback.py: one request every 4 s, the first 429 or refusal ends the run), each again after
RECHECK_DAYS. A capture that is the site's challenge page (many such firewalls turn the Archive away too) is left.
Records are the source's own kind (a PDF a `document`, so the minutes and elections derivers read it), with `url` the
Archive's copy, meta.original the site's address and meta.captured the capture day.

Still closed: a login or a paywall (401, 402) is not a refusal of a public page and is never read from an archive;
the Archive is never asked to capture a page (Save Page Now); a challenge is never solved and the crawler never
presents itself as anything else.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from urllib.parse import urlsplit

from .store import Record
from .wayback import CDX, RAW, SHOW, Wayback, day, host_of

PAGES_PER_SOURCE = 40
RECENT_DAYS = 730
RECHECK_DAYS = 30
INDEX_ROWS = 3000            # capture index rows asked for per source (the newest)
CHALLENGE = re.compile(r"incapsula|_incapsula_resource|request unsuccessful|pardon our interruption|attention required|"
                       r"cf-chl|just a moment\.\.\.|captcha|are you a robot|verify you are human|access denied|"
                       r"request rejected|the requested url was rejected", re.I)
CLOSED = re.compile(r"HTTP 40[12]\b|login|log in|sign in|paywall|subscri", re.I)
SKIP = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|json|xml|txt|zip|mp[34]|mov|woff2?)(?:$|\?)|/(?:feed|wp-json|"
                  r"wp-admin|wp-login|xmlrpc|cart|checkout|my-account|search|login|signin)\b|[?&](?:replytocom|share|print)=", re.I)
ELSEWHERE = ("simbli.eboardsolutions.com",)   # read by their own archive reader


def refused(progress: dict, sources: list[dict]) -> list[dict]:
    """The sources whose last run was refused (bot protection, a 403, robots.txt), not closed (a login, a paywall),
    and not read by another archive reader."""
    out = []
    for s in sources:
        st = (progress.get(s["id"]) or {}).get("status") or ""
        if not st.startswith(("blocked", "stopped: robots")) or "unreachable" in st or CLOSED.search(st):
            continue
        if not str(s.get("url") or "").startswith("http") or host_of(s["url"]) in ELSEWHERE:
            continue
        out.append(s)
    return out


def is_real(body: str) -> bool:
    """A captured page that is the site's own, not its firewall's challenge."""
    return bool(body) and len(body) >= 500 and not CHALLENGE.search(body[:20000])


class RefusedArchive:
    def __init__(self, fetcher, store, log=print, wayback: Wayback | None = None):
        self.store, self.log = store, log
        self.wb = wayback or Wayback(fetcher, log=log)
        self.stats = {"sources": 0, "pages": 0, "documents": 0, "challenge_captures": 0, "no_capture": 0}

    def captures(self, src: dict) -> list[dict]:
        """The newest capture of each of the source's own pages and PDFs within RECENT_DAYS: [{timestamp, original,
        mimetype}], agendas, minutes and council pages first."""
        from .channels import COLD, HOT, _filter_urls

        u = urlsplit(src["url"])
        base = u.netloc + (u.path if u.path not in ("", "/") else "/")
        since = (dt.date.today() - dt.timedelta(days=RECENT_DAYS)).strftime("%Y%m%d")
        params = [("url", base.rstrip("*") + "*"), ("output", "json"), ("fl", "urlkey,timestamp,original,mimetype"),
                  ("filter", "statuscode:200"), ("filter", "mimetype:(text/html|application/pdf)"), ("from", since),
                  ("limit", f"-{INDEX_ROWS}")]
        r = self.wb._get(CDX, params)
        if r is None or r.status != 200:
            return []
        try:
            rows = r.json()
        except ValueError:
            return []
        if not rows or not isinstance(rows, list):
            return []
        head, newest = rows[0], {}
        for row in rows[1:]:
            c = dict(zip(head, row))
            k = c.get("urlkey") or c.get("original")
            if not k or SKIP.search(c.get("original") or ""):
                continue
            if k not in newest or c["timestamp"] > newest[k]["timestamp"]:
                newest[k] = c
        keep = set(_filter_urls([c["original"] for c in newest.values()], src))
        caps = [c for c in newest.values() if c["original"] in keep]
        caps.sort(key=lambda c: (not HOT.search(c["original"]), bool(COLD.search(c["original"])), "pdf" not in c["mimetype"],
                                 -int(c["timestamp"])))
        return caps

    def source(self, src: dict) -> int:
        from . import extract

        sid, n = src["id"], 0
        caps = self.captures(src)
        if not caps:
            self.stats["no_capture"] += 1
            return 0
        limit = int(src.get("max_pages") or PAGES_PER_SOURCE)
        for cap in caps:
            if n >= min(limit, PAGES_PER_SOURCE) or self.wb.off:
                break
            key_doc = f"{sid}|archived|{cap['original']}"
            if self.store.has(key_doc):
                continue
            r = self.wb._get(RAW.format(ts=cap["timestamp"], url=cap["original"]))
            if r is None or r.status != 200:
                continue
            shown = SHOW.format(ts=cap["timestamp"], url=cap["original"])
            meta = {"archived": True, "captured": day(cap["timestamp"]), "original": cap["original"], "via": "wayback",
                    "opened": True, "archive_why": "refuses the crawler"}
            if "pdf" in (cap.get("mimetype") or "") or r.content[:5] == b"%PDF-":
                if r.content[:5] != b"%PDF-":
                    continue
                from .ingest import pdf_to_record

                try:
                    rec = pdf_to_record(r.content, source_id=sid, kind="document", url=shown, ocr_max_pages=0)
                except Exception:  # noqa: BLE001 — one unreadable file
                    continue
                rec.key, rec.jurisdiction, rec.evidence, rec.channel = key_doc, src.get("jurisdiction", ""), "primary", "archive"
                rec.meta = {**rec.meta, **meta}
                self.stats["documents"] += 1
            else:
                html = r.text
                if not is_real(html):
                    self.stats["challenge_captures"] += 1
                    continue
                title, md = extract.html_to_markdown(html, cap["original"])
                if not md or len(md) < 200:
                    continue
                from .channels import date_meta

                di = extract.page_date_info(html, md, fetched=day(cap["timestamp"]))
                meta = {**meta, **date_meta(di)}
                rec = Record(sid, src.get("kind") or "page", shown, title=title, text=md, markdown=md,
                             published=di["date"], jurisdiction=src.get("jurisdiction", ""),
                             channel="archive", evidence="primary", key=key_doc, meta=meta)
                self.stats["pages"] += 1
            self.store.add(rec)
            n += 1
        return n

    def run(self, sources: list[dict], deadline: float | None = None, max_sources: int = 10) -> dict:
        today = dt.date.today()

        def last(s):
            st = json.loads(self.store.kv_get(f"archive_src:{s['id']}") or "{}")
            return st.get("at") or ""

        tier = {"a": 0, "b": 1, "c": 2}
        todo = [s for s in sources if not last(s) or (today - dt.date.fromisoformat(last(s))).days >= RECHECK_DAYS]
        todo.sort(key=lambda s: (bool(last(s)), tier.get(s.get("tier") or "c", 3), last(s)))
        from .fetch import OutOfTime

        for s in todo[:max_sources]:
            if self.wb.off or (deadline and time.monotonic() > deadline):
                break
            try:
                n = self.source(s)
            except OutOfTime:
                # the run's time is up in the middle of a source (the fetcher takes no turn that ends after it). What
                # was read of the source is kept: each page is stored as it is read and is not asked for twice. The
                # source is not stamped, so the next run carries on with it. (Nov. 20, 2026, the first run with the
                # Archive read again: this ended the step with a traceback and exit 1 instead of its count.)
                self.stats["stopped"] = f"out of time in {s['id']}"
                self.log(f"archive (refused): {s['id']}: out of time; the next run carries on with it")
                break
            self.stats["sources"] += 1
            if self.wb.off:
                break                                # not stamped: asked again next run
            self.store.kv_set(f"archive_src:{s['id']}", json.dumps({"at": today.isoformat(), "pages": n}))
            self.log(f"archive (refused): {s['id']}: {n} pages from the Internet Archive's copies")
        self.store.flush()
        return {**self.stats, "refused_sources": len(sources), "due": len(todo), "archive_off": self.wb.off or ""}


def run(fetcher, store, root, state_dir, deadline: float | None = None, max_sources: int = 10, log=print) -> dict:
    from . import registry

    try:
        progress = json.loads((state_dir / "progress.json").read_text())
    except (OSError, ValueError):
        progress = {}
    srcs = refused(progress, registry.all_sources(root))
    return RefusedArchive(fetcher, store, log=log).run(srcs, deadline=deadline, max_sources=max_sources)
