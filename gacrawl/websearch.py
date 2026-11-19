"""Web search for the discovery step (gacrawl/discover_ai.py): DuckDuckGo through the free `ddgs` library. No account,
no API key, no card, nothing that ties the searches to the owner (the library sends no email or identity).

`ddgs` is unofficial (it reads DuckDuckGo's result pages), so it can be rate-limited or break when DuckDuckGo changes
something. The step is built for that: queries are spaced out (MIN_GAP seconds), a failed query is retried with growing
waits, a run asks at most RUN_CAP fresh queries, a query asked in the last CACHE_DAYS days is answered from the cache
(state/websearch.json), and when search keeps failing it returns [] so discovery falls back to the outlets' own search
and pages already opened instead of stopping the run. Results from sites the project never uses (config/outlets.yaml
`exclude`: Ballotpedia, AI-generated summary portals, Sway ...) are dropped before anything opens them.

"Nothing found" is not always an answer. An engine that turns the runner away answers with a page that holds no
results, which ddgs reports exactly like a search nobody matches ("No results found."). From Nov. 13 to 18, 2026 every
one of 1,585 searches came back that way (DuckDuckGo alone, from GitHub's runners), each was kept for 30 days as
"searched, nothing there", and the callers counted it as a look that found nothing: no county election report was
ever found (0 of 159 counties, tries used up), and candidates' web look-ups were marked done. So:
  - an empty answer from the first engine is asked again of the others in FALLBACK, in order, and what they give is
    kept only when it carries the query's quoted phrases (the reason DuckDuckGo is asked first: other engines drop
    a quoted name);
  - an empty answer is believed only once some search of this run has found something (the engines are answering):
    until then it is held back, kept out of the cache, and `settled(q)` is False, so a caller does not count it as
    a look (it asks again next run; a believed answer then comes from the cache);
  - after CANARY_AFTER empty answers with nothing found yet, or EMPTY_STOP in a row at any time, one search that
    always has results (CANARY) is asked: results mean the empty answers were answers; nothing means the engines
    are not answering, the run's searching ends (`left()` is 0), none of those answers is kept, and the run says
    so in a warning.

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
# ddgs 9 asks several engines by default ("auto": Wikipedia, Bing, Brave, ...), and some of them ignore a quoted name
# (Nov 8: '"adam cleveland" state house district 48' came back as Adam of Genesis and a film called Adam). DuckDuckGo
# alone keeps the phrase; an older ddgs without the option is asked the old way.
BACKEND = os.environ.get("WEBSEARCH_BACKEND", "duckduckgo")
# asked, in order, when BACKEND finds nothing (empty or "" for none)
FALLBACK = [b.strip() for b in os.environ.get("WEBSEARCH_FALLBACK", "brave,yahoo,mojeek").split(",") if b.strip()]
EMPTY_STOP = int(os.environ.get("WEBSEARCH_EMPTY_STOP", "12"))   # empty answers in a row before the engines are checked again
CANARY_AFTER = 3        # empty answers at the start of a run before the engines are checked
CANARY = "Georgia Secretary of State elections"     # a search with results on every engine: nothing back = not answering
CACHE_V = 2             # cache entries written since empty answers are checked; an older empty entry is not believed


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def quoted_phrases(q: str) -> list[str]:
    """The phrases a query puts in double quotes, lower case."""
    import re

    return [m.strip().lower() for m in re.findall(r'"([^"]{2,})"', q) if m.strip()]


def carries_phrases(q: str, row: dict) -> bool:
    """A result that holds every quoted phrase of the query in its title, snippet or address (a query with no quoted
    phrase is carried by any result). Only asked of the fallback engines, which may ignore the quotes."""
    hay = " ".join(str(row.get(k) or "") for k in ("title", "snippet", "url")).lower()
    return all(ph in hay for ph in quoted_phrases(q))


class Search:
    def __init__(self, state_dir: Path, exclude: set[str] | None = None, log=print, cap: int | None = None):
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
        self.st.setdefault("months", {}); self.st.setdefault("cache", {}); self.st.setdefault("health", {})
        # an empty answer kept before empty answers were checked may be an engine's refusal: asked again
        self.st["cache"] = {k: v for k, v in self.st["cache"].items()
                            if isinstance(v, dict) and (v.get("results") or v.get("v") == CACHE_V)}
        self.exclude = {d.lower() for d in (exclude or set())}
        self.log = log
        self.run_n = 0
        self.cap = int(cap or RUN_CAP)      # this run's queries (a step can ask for its own)
        self._said = False
        self._last = 0.0
        self._fails = 0
        self._alive = False                 # a search of this run found something: the engines are answering
        self._empties = 0                   # empty answers in a row
        self._held: dict[str, str] = {}     # empty answers not believed yet: {query: day}
        self._streak: list[str] = []        # the queries of the current run of empty answers
        self._refused = False
        self.unanswered = 0                 # empty answers this run that were never believed (for the run's summary)

    @property
    def month(self) -> str:
        return dt.date.today().strftime("%Y-%m")

    def used(self) -> int:
        return int(self.st["months"].get(self.month, 0))

    def left(self) -> int:
        if self._DDGS is None or self._fails >= GIVE_UP_AFTER or self.refused():
            return 0
        return max(0, self.cap - self.run_n)

    def refused(self) -> bool:
        """The engines answer every search with nothing, the one that always has results too: taken as a refusal of
        this address, not as answers."""
        return self._refused

    def settled(self, q: str) -> bool:
        """True when this query has an answer to rely on (in the cache: results, or an empty answer given while the
        engines were answering). False for a search that could not run, failed, or came back empty with nothing yet
        showing that the engines answer: the caller must not count it as a look that found nothing."""
        q = " ".join(q.split())[:200].lower()
        c = self.st["cache"].get(q)
        return bool(c) and (dt.date.today() - dt.date.fromisoformat(c["at"])).days < CACHE_DAYS

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
                elif self.refused():
                    self.log(f"web search: {self._empties} searches in a row came back with nothing, and so did one that "
                             "always has results: taken as a refusal of this address, not as answers; no more searches "
                             "this run and none of them is kept")
                else:
                    self.log(f"web search: this run's cap reached ({self.run_n}/{self.cap})")
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
                    raw = self._ask(d, q, count)
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
        today = dt.date.today().isoformat()
        if res:
            self._alive, self._empties, self._streak = True, 0, []
            self.st["health"] = {**self.st.get("health", {}), "found": _now(), "refused_runs": 0}
            self.st["cache"][q.lower()] = {"at": today, "results": res, "v": CACHE_V}
            self._believe()         # the engines answer: the empty answers held back so far were answers too
        else:
            self._empties += 1
            self._streak.append(q.lower())
            self._held[q.lower()] = today
            if self._alive and self._empties < EMPTY_STOP:
                self._believe()
            elif self._empties >= (EMPTY_STOP if self._alive else CANARY_AFTER):
                # nothing yet shows the engines answer (or a long run of nothing): one search that always has
                # results tells an engine with nothing to say about these from one that says nothing at all
                if self._canary():
                    self._alive, self._empties, self._streak = True, 0, []
                    self._believe()
                else:
                    for sq in self._streak:      # none of it is an answer, the ones already believed included
                        if not (self.st["cache"].get(sq) or {}).get("results"):
                            self.st["cache"].pop(sq, None)
                    self.unanswered += len(self._streak)
                    self._held, self._streak, self._alive, self._refused = {}, [], False, True
                    h = self.st.get("health", {})       # for the coverage report: when it last answered, when it last refused
                    self.st["health"] = {**h, "refused": _now(), "refused_runs": int(h.get("refused_runs") or 0) + 1}
        return [x for x in res if not self._excluded(x["url"])]

    def _believe(self) -> None:
        for hq, at in self._held.items():
            self.st["cache"].setdefault(hq, {"at": at, "results": [], "v": CACHE_V})
        self._held = {}

    def _canary(self) -> bool:
        """One search every engine has results for: True when something comes back."""
        try:
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 1))
            self._last = time.monotonic()
            self.st["months"][self.month] = self.used() + 1
            with self._DDGS() as d:
                return bool(self._ask(d, CANARY, 5))
        except Exception:  # noqa: BLE001 — refused, timed out, "no results": not answering
            return False

    def _ask(self, d, q: str, count: int) -> list:
        """BACKEND's results; when it finds nothing, the FALLBACK engines' in turn (only results that carry the
        query's quoted phrases). [] when none of them finds anything; an error of BACKEND itself is raised (retried
        by the caller), a fallback engine's error only moves on to the next one."""
        if not BACKEND or getattr(self, "_no_backend", False):
            return self._text(d, q, count, None)
        try:
            raw = self._text(d, q, count, BACKEND)
        except TypeError:                # this ddgs has no backend option
            self._no_backend = True
            return self._text(d, q, count, None)
        except Exception as e:           # it has, but not this engine by that name
            if "no results" in str(e).lower():
                raw = []
            elif "backend" in str(e).lower() or "engine" in str(e).lower():
                self._no_backend = True
                return self._text(d, q, count, None)
            else:
                raise
        if raw:
            return raw
        for be in FALLBACK:
            if be == BACKEND or be in getattr(self, "_dead", ()):
                continue
            try:
                more = self._text(d, q, count, be)
            except Exception as e:       # noqa: BLE001 — no results there either, refused, or no such engine
                if "no results" not in str(e).lower() and "timed out" not in str(e).lower():
                    self._dead = {*getattr(self, "_dead", ()), be}      # not asked again this run
                continue
            more = [w for w in more if carries_phrases(q, {"title": w.get("title"), "snippet": w.get("body"),
                                                           "url": w.get("href") or w.get("url")})]
            if more:
                if not getattr(self, "_said_fallback", False):
                    self.log(f"web search: {BACKEND} found nothing; {be} answered (results kept only with the quoted words)")
                    self._said_fallback = True
                return more
        return []

    @staticmethod
    def _text(d, q: str, count: int, backend: str | None) -> list:
        kw = {"region": REGION, "safesearch": "moderate", "max_results": count}
        if backend:
            kw["backend"] = backend
        return list(d.text(q, **kw) or [])

    def save(self) -> None:
        # empty answers nothing vouched for are not kept (asked again next run)
        self.unanswered += len(self._held)
        self._held = {}
        if self.refused() and not getattr(self, "_said_dead", False):
            self._said_dead = True
            self.log(f"::warning title=Web search::the searches of this run came back with nothing ({self.unanswered} of them), "
                     "and so did one that always has results: the search engines are refusing this address or changed "
                     "their pages. Nothing was recorded as searched; the look-ups stay owed")
        cut = (dt.date.today() - dt.timedelta(days=CACHE_DAYS)).isoformat()
        self.st["cache"] = {k: v for k, v in self.st["cache"].items() if v.get("at", "") >= cut}
        try:
            self.p.write_text(json.dumps(self.st, ensure_ascii=False, separators=(",", ":")))
        except OSError:
            pass


def health_line(state_dir: Path) -> tuple[str, bool]:
    """(one line for the coverage report, whether it needs attention): when the web search last found something and
    when it last answered nothing at all. It feeds the county election reports, candidates' web look-ups and
    discovery, none of which can tell on their own that the engine has stopped answering."""
    try:
        st = json.loads((Path(state_dir) / "websearch.json").read_text())
    except (OSError, ValueError):
        return "- Web search: no state yet (state/websearch.json)", False
    h = st.get("health") or {}
    cache = [v for v in (st.get("cache") or {}).values() if isinstance(v, dict)]
    found = h.get("found") or max((v.get("at", "") for v in cache if v.get("results")), default="")
    refused = h.get("refused") or ""
    bad = bool(refused) and refused > found
    line = (f"- Web search (county election reports, candidates' web look-ups, discovery): answers on file "
            f"**{sum(1 for v in cache if v.get('results')):,}** with results, {sum(1 for v in cache if not v.get('results')):,} "
            f"with none; last found something **{found[:16] or 'never'}**")
    if refused:
        line += f"; last answered nothing at all (taken as a refusal, nothing recorded) **{refused[:16]}**"
    if bad:
        line += (f" - **not answering**: {int(h.get('refused_runs') or 1)} run(s) in a row; the look-ups that need it "
                 "are waiting, not written off")
    return line, bad
