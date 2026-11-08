"""Web search for the discovery step (gacrawl/discover_ai.py): Brave Search's API, with the key in the BRAVE_API_KEY
secret. Brave keeps its own index and its API is a paid service with a key, so this is no scraping of a search page
and no way around anyone's bot rules; the searches come from the runner, never from Electo's visitors, and carry
nothing about the owner (a generic User-Agent, no email).

Spending stays inside Brave's free monthly credit: at most MONTHLY_CAP searches a calendar month (state/websearch.json
counts them), RUN_CAP a run, and a query asked in the last CACHE_DAYS days is answered from the cache. Results from
sites the project never uses (config/outlets.yaml `exclude`: Ballotpedia, AI-generated summary portals, Sway ...) are
dropped before anything opens them. Without a key, search() returns [] and says so once.

    from gacrawl.websearch import Search
    s = Search(state_dir); s.search("Jane Doe Fulton County Commission")   -> [{"url", "title", "snippet"}]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

import requests

API = "https://api.search.brave.com/res/v1/web/search"
MONTHLY_CAP = int(os.environ.get("WEBSEARCH_MONTHLY_CAP", "900"))   # Brave: $5 per 1,000 searches, $5 free each month
RUN_CAP = int(os.environ.get("WEBSEARCH_RUN_CAP", "40"))
CACHE_DAYS = 30
UA = "GACivicCrawler/0.3 (civic research; +https://github.com/ClockworkBlack/Crawl-runner)"


class Search:
    def __init__(self, state_dir: Path, exclude: set[str] | None = None, log=print):
        self.key = os.environ.get("BRAVE_API_KEY", "").strip()
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

    @property
    def month(self) -> str:
        return dt.date.today().strftime("%Y-%m")

    def used(self) -> int:
        return int(self.st["months"].get(self.month, 0))

    def left(self) -> int:
        return max(0, min(MONTHLY_CAP - self.used(), RUN_CAP - self.run_n)) if self.key else 0

    def _excluded(self, url: str) -> bool:
        from .candidates import domain_of

        d = domain_of(url)
        return any(d == x or d.endswith("." + x) for x in self.exclude)

    def search(self, q: str, count: int = 8) -> list[dict]:
        q = " ".join(q.split())[:200]
        c = self.st["cache"].get(q.lower())
        if c and (dt.date.today() - dt.date.fromisoformat(c["at"])).days < CACHE_DAYS:
            return [r for r in c["results"] if not self._excluded(r["url"])]
        if not self.key:
            if not self._said:
                self.log("web search: no BRAVE_API_KEY secret; discovery uses the outlets' own search and pages already opened")
                self._said = True
            return []
        if self.left() <= 0:
            if not self._said:
                self.log(f"web search: this month's cap reached ({self.used()}/{MONTHLY_CAP}) or this run's ({self.run_n}/{RUN_CAP})")
                self._said = True
            return []
        wait = 1.1 - (time.monotonic() - self._last)     # one request a second at most
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            r = requests.get(API, params={"q": q, "count": count, "country": "us", "search_lang": "en", "safesearch": "moderate"},
                             headers={"X-Subscription-Token": self.key, "Accept": "application/json", "User-Agent": UA},
                             timeout=20)
        except requests.RequestException as e:
            self.log(f"web search: {type(e).__name__}")
            return []
        self.run_n += 1
        self.st["months"][self.month] = self.used() + 1
        if r.status_code != 200:
            self.log(f"web search: HTTP {r.status_code}")
            return []
        res = [{"url": w.get("url") or "", "title": w.get("title") or "", "snippet": w.get("description") or ""}
               for w in ((r.json().get("web") or {}).get("results") or []) if (w.get("url") or "").startswith("http")]
        self.st["cache"][q.lower()] = {"at": dt.date.today().isoformat(), "results": res}
        return [x for x in res if not self._excluded(x["url"])]

    def save(self) -> None:
        cut = (dt.date.today() - dt.timedelta(days=CACHE_DAYS)).isoformat()
        self.st["cache"] = {k: v for k, v in self.st["cache"].items() if v.get("at", "") >= cut}
        try:
            self.p.write_text(json.dumps(self.st, ensure_ascii=False, separators=(",", ":")))
        except OSError:
            pass
