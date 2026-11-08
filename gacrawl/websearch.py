"""Web search for the discovery step (gacrawl/discover_ai.py): DuckDuckGo through the free `ddgs` library. No account,
no API key, no card, nothing that ties the searches to the owner (the library sends no email or identity).

`ddgs` is unofficial (it reads DuckDuckGo's result pages), so it can be rate-limited or break when DuckDuckGo changes
something. The step is built for that: queries are spaced out (MIN_GAP seconds), a failed query is retried with growing
waits, a run asks at most RUN_CAP fresh queries, a query asked in the last CACHE_DAYS days is answered from the cache
(state/websearch.json), and when search keeps failing it returns [] so discovery falls back to the outlets' own search
and pages already opened instead of stopping the run. Results from sites the project never uses (config/outlets.yaml
`exclude`: Ballotpedia, AI-generated summary portals, Sway ...) are dropped before anything opens them.

    from gacrawl.websearch import Search
    s = Search(state_dir); s.search("Jane Doe Fulton County Commission")   -> [{"url", "title", "snippet"}]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
from pathlib import Path

RUN_CAP = int(os.environ.get("WEBSEARCH_RUN_CAP", "40"))
MIN_GAP = float(os.environ.get("WEBSEARCH_MIN_GAP", "3"))       # seconds between queries
RETRIES = int(os.environ.get("WEBSEARCH_RETRIES", "3"))
GIVE_UP_AFTER = 3                                                  # failed queries in a row before stopping for the run
CACHE_DAYS = 30
REGION = "us-en"


class Search:
    def __init__(self, state_dir: Path, exclude: set[str] | None = None, log=print):
        try:
            from ddgs import DDGS
            self._DDGS = DDGS
        except ImportError:
            self._DDGS = None
        self.p = Path(state_dir) / "websearch.json"
        try:
            self.st = json.loads(self.p.read_text())
        except (OSError, ValueError):
            self.st = {}
        self.st.setdefault("months", {}); self.st.setdefault("cache", {})
        self.exclude = {d.lower() for d in (exclude or set())}
        self.log = log
        self.run_n = 0
        self._said = False
        self._last = 0.0
        self._fails = 0

    @property
    def month(self) -> str:
        return dt.date.today().strftime("%Y-%m")

    def used(self) -> int:
        return int(self.st["months"].get(self.month, 0))

    def left(self) -> int:
        if self._DDGS is None or self._fails >= GIVE_UP_AFTER:
            return 0
        return max(0, RUN_CAP - self.run_n)

    def _excluded(self, url: str) -> bool:
        from .candidates import domain_of

        d = domain_of(url)
        return any(d == x or d.endswith("." + x) for x in self.exclude)

    def search(self, q: str, count: int = 8) -> list[dict]:
        q = " ".join(q.split())[:200]
        c = self.st["cache"].get(q.lower())
        if c and (dt.date.today() - dt.date.fromisoformat(c["at"])).days < CACHE_DAYS:
            return [r for r in c["results"] if not self._excluded(r["url"])]
        if self.left() <= 0:
            if not self._said:
                if self._DDGS is None:
                    self.log("web search: the ddgs package is not installed; discovery uses the outlets' own search and pages already opened")
                elif self._fails >= GIVE_UP_AFTER:
                    self.log("web search: DuckDuckGo kept failing; no more searches this run")
                else:
                    self.log(f"web search: this run's cap reached ({self.run_n}/{RUN_CAP})")
                self._said = True
            return []
        raw = None
        for attempt in range(RETRIES):
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 1))
            self._last = time.monotonic()
            try:
                with self._DDGS() as d:
                    raw = list(d.text(q, region=REGION, safesearch="moderate", max_results=count) or [])
                break
            except Exception as e:   # rate limit, timeout, no results, a change on DuckDuckGo's side
                if "no results" in str(e).lower():
                    raw = []
                    break
                self.log(f"web search: {type(e).__name__}; retry {attempt + 1}/{RETRIES}")
                time.sleep(5 * 2 ** attempt + random.uniform(0, 3))
        self.run_n += 1
        self.st["months"][self.month] = self.used() + 1
        if raw is None:
            self._fails += 1
            return []
        self._fails = 0
        res = [{"url": w.get("href") or w.get("url") or "", "title": w.get("title") or "", "snippet": w.get("body") or ""}
               for w in raw if (w.get("href") or w.get("url") or "").startswith("http")]
        self.st["cache"][q.lower()] = {"at": dt.date.today().isoformat(), "results": res}
        return [x for x in res if not self._excluded(x["url"])]

    def save(self) -> None:
        cut = (dt.date.today() - dt.timedelta(days=CACHE_DAYS)).isoformat()
        self.st["cache"] = {k: v for k, v in self.st["cache"].items() if v.get("at", "") >= cut}
        try:
            self.p.write_text(json.dumps(self.st, ensure_ascii=False, separators=(",", ":")))
        except OSError:
            pass
