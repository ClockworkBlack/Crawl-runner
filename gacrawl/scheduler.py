"""Runs hundreds of sources politely inside a time budget.

- Each source has a cadence from its jurisdiction's tier (a: daily, b: weekly,
  c: monthly by default). A source is *due* when its last successful run is
  older than its cadence (or it has never run).
- Due sources are ordered most-overdue first, big jurisdictions before small.
- `workers` threads run sources concurrently; the per-host rate limiter keeps
  any single site at one request per interval no matter how many workers.
- Progress (state/progress.json) survives between runs, so a run that hits its
  time budget simply resumes where it left off next time.
"""
from __future__ import annotations

import datetime as dt
import json
from .registry import write_json_atomic
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import channels, topics, transcripts  # noqa: F401 — transcripts registers its page hook
from .blocked import Blocked
from .fetch import Disallowed, Fetcher, OutOfTime
from .polite import Schedule
from .registry import TIER_CADENCE_DAYS
from .store import Store


TAGGED = {"ordinance", "matter", "agenda_item", "vote", "document", "meeting"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Progress:
    """Per-source (or per-candidate) run state. Updates are written to the file at most every `write_every`
    seconds and on flush(): thousands of updates a run used to rewrite a 300 KB file each time."""

    def __init__(self, path: Path, write_every: float = 2.0):
        self.path = Path(path)
        self.data: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._lock = threading.Lock()
        self._dirty = False
        self._written = 0.0
        self.write_every = write_every

    def get(self, source_id: str) -> dict:
        return self.data.get(source_id, {})

    def update(self, source_id: str, **fields):
        with self._lock:
            rec = self.data.setdefault(source_id, {})
            rec.update(fields)
            self._dirty = True
            if time.monotonic() - self._written >= self.write_every:
                self._write()

    def flush(self):
        with self._lock:
            if self._dirty:
                self._write()

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        write_json_atomic(tmp, self.data, indent=1)
        tmp.replace(self.path)
        self._dirty, self._written = False, time.monotonic()

    def __del__(self):
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            pass


STRUCTURED_HINT = ("legistar", "civicclerk", "boarddocs", "granicus", "youtube", "legis", "wp_json", "feed")


# Readers that changed how deep they read. A source last completed by an older version (the old CivicClerk reader saw
# 15 meetings, the old YouTube reader 200 videos) is due at once and read from the start, whatever its cadence; the
# version is stamped on its progress when the new reader completes it.
READER_VERSIONS = {"civicclerk": "2", "youtube": "2"}


def reader_of(src: dict, hit: dict | None = None) -> str:
    ep = ((hit or {}).get("chosen") or {}).get("endpoint") or src.get("endpoint") or ""
    return ep.split(":", 1)[0] if ":" in ep else ""


def stale_reader(src: dict, p: dict, hit: dict | None = None) -> bool:
    want = READER_VERSIONS.get(reader_of(src, hit))
    return bool(want) and p.get("reader") != want


def due(sources: list[dict], progress: Progress, cadence_days: dict | None = None, now: dt.datetime | None = None,
        force: bool = False, cache: "channels.ChannelCache | None" = None, wanted: set[str] | None = None) -> list[dict]:
    """Sources that should run now, in the order a short run should take them:
      1. a source stopped mid-way last time (partial) or never finished resumes first, then the most overdue;
      2. within that, sources with candidates Electo is building profiles for (`wanted` jurisdictions), bigger
         jurisdictions (tier a, b, c), and structured channels (a meeting API or feed yields more per minute than
         walking pages) go before the rest, so a run that cannot reach everything reaches what matters most."""
    cadence_days = {**TIER_CADENCE_DAYS, **(cadence_days or {})}
    now = now or _now()
    wanted = wanted or set()
    scored = []
    for s in sources:
        p = progress.get(s["id"])
        cad = dt.timedelta(days=float(cadence_days.get(s.get("tier", "b"), 7)))
        last_ok = dt.datetime.fromisoformat(p["last_ok"]) if p.get("last_ok") else None
        if last_ok and stale_reader(s, p, cache.get(s["id"]) if cache else None):
            last_ok = None                  # completed by an older, shallower reader: due now, from the start
        last_try = dt.datetime.fromisoformat(p["last_run"]) if p.get("last_run") else None
        status = p.get("status", "") or ""
        if not force:
            if last_ok and now - last_ok < cad:
                continue
            # a source that keeps failing backs off: wait a quarter cadence (at least 6h) between retries
            if last_try and not last_ok and status.startswith(("error", "blocked", "stopped")) \
                    and now - last_try < max(dt.timedelta(hours=6), cad / 4):
                continue
            # a blocked or refused host is left alone for a full cadence even after it once worked
            if last_try and status.startswith(("blocked", "stopped")) and now - last_try < cad:
                continue
        overdue = (now - last_ok) / cad if last_ok else 99.0
        resume = status.startswith("partial") or (last_try is not None and not last_ok and not status.startswith(("error", "blocked", "stopped")))
        hit = cache.get(s["id"]) if cache else None
        ch = (hit or {}).get("chosen", {}).get("channel") or s.get("channel") or ""
        ep = (hit or {}).get("chosen", {}).get("endpoint") or s.get("endpoint") or ""
        structured = ch in ("api", "wp_json", "feed") or any(ep.startswith(h + ":") for h in STRUCTURED_HINT)
        key = (0 if resume else 1, -min(overdue, 99.0), 0 if s.get("jurisdiction") in wanted else 1,
               {"a": 0, "b": 1, "c": 2}.get(s.get("tier", "b"), 1), 0 if structured else 1, s["id"])
        scored.append((key, s))
    scored.sort(key=lambda t: t[0])
    return [t[1] for t in scored]


def run_sources(cfg: dict, sources: list[dict], fetcher: Fetcher, store: Store, cache: channels.ChannelCache,
                progress: Progress, deadline: float, force: bool = False, workers: int = 4, log=print,
                full: bool = False, slice_minutes: float | None = None) -> dict:
    """Crawl `sources` concurrently inside the time budget.

    force          ignore the off-peak window (cadence is handled by `due`)
    full           re-crawl from the beginning of history instead of a delta (also restarts interrupted backfills)
    slice_minutes  one source's share of the run (default: a sixth of the budget, at least 4 minutes); a source
                   that reaches it is marked partial and resumes next run (pages already read are skipped), so a
                   big archive never keeps the other sources from their turn
    A SIGINT (the workflow's watchdog) stops cleanly: sources in progress stop at their next request, nothing
    queued starts, and everything read so far is flushed.
    """
    import signal

    sch = Schedule.from_config(cfg.get("schedule", {}))
    api_anytime = cfg.get("schedule", {}).get("api_anytime", True)
    limits = cfg.get("limits", {})
    fr = cfg.get("frontier", {})
    summary: dict[str, str] = {}
    stop = threading.Event()
    fetcher.set_pool(max(16, workers * 2))  # one hot keep-alive connection per concurrent worker, with headroom
    fetcher.stop_at = deadline              # a source mid-way stops at its next request once the budget is spent
    budget = max(0.0, deadline - time.monotonic())
    slice_s = (slice_minutes * 60) if slice_minutes else max(240.0, budget / 6)

    def on_interrupt(signum, frame):
        if not stop.is_set():
            log("interrupted: stopping sources at their next request and flushing")
        stop.set()
        fetcher.stop_at = time.monotonic()
    try:
        prev_handler = signal.signal(signal.SIGINT, on_interrupt)
    except ValueError:
        prev_handler = None                 # not the main thread (tests): no handler

    def pages_ok() -> bool:
        return force or sch.is_good_time()

    def may_run(structured: bool) -> bool:
        return pages_ok() or (structured and api_anytime)

    def choose(src: dict) -> tuple[dict, list]:
        hit = cache.get(src["id"])
        if hit:
            return hit["chosen"], hit.get("probes", [])
        if src.get("channel"):
            return {"channel": src["channel"], "endpoint": src.get("endpoint", src["url"])}, []
        probes = channels.probe_source(fetcher, src)
        chosen = channels.choose(probes)
        if not chosen:
            raise RuntimeError("no working channel: " + ("; ".join(f"{p.channel}: {p.note}" for p in probes if p.note)
                                                         or "nothing answered"))
        cache.put(src["id"], chosen, probes)
        hit = cache.get(src["id"])
        return hit["chosen"], hit["probes"]

    def one(src: dict) -> tuple[str, str]:
        sid = src["id"]
        if stop.is_set() or time.monotonic() > deadline:
            return sid, "skipped: run time budget used up"
        fetcher.source_id = sid
        started = _now().isoformat(timespec="seconds")
        # Delta since the last *successful* run; None (full crawl) on the first run or with --full.
        # last_ok is stamped with this run's start, so records changed mid-crawl are caught next time.
        p0 = progress.get(sid)
        old_reader = stale_reader(src, p0, cache.get(sid))
        modified_since = None if (full or old_reader) else (p0.get("last_ok") or None)
        if full:
            store.clear_cursors(sid)
        if not pages_ok():
            # by day only a source already known to have an API or feed runs; the rest are not even probed (a
            # probe is page traffic too), they wait for the window
            hit = cache.get(sid)
            known = (hit or {}).get("chosen", {}).get("channel") or src.get("channel") or ""
            if known not in channels.STRUCTURED:
                progress.update(sid, status="waiting for off-peak window", last_run=started)
                return sid, "waiting for off-peak window (pages are read at night)"
        progress.update(sid, last_run=started, status="running")
        try:
            chosen, probes = choose(src)
        except OutOfTime:
            progress.update(sid, status="skipped: run time budget used up")
            return sid, "skipped: run time budget used up"
        except Blocked as exc:
            fetcher.blocklist.note_source(exc.host, sid)
            progress.update(sid, status=f"blocked: {exc}")
            return sid, f"blocked: {exc}"
        except Exception as exc:  # noqa: BLE001
            progress.update(sid, status=f"error choosing channel: {exc}"[:300])
            return sid, f"error choosing channel: {exc}"[:300]
        structured = chosen["channel"] in channels.STRUCTURED
        if not may_run(structured):
            progress.update(sid, status="waiting for off-peak window")
            return sid, f"waiting for off-peak window ({chosen['channel']})"
        ctx = channels.CrawlContext(frontier=store, cursors=store.cursors(sid), pages_ok=pages_ok(), probes=probes,
                                    leaf_ttl_days=float(fr.get("document_ttl_days", 3650)),
                                    page_revisit_days=float(fr.get("page_revisit_days", 7)))
        new = total = 0
        mode = "delta" if modified_since else ("resume" if ctx.cursors else "full")
        fetcher.thread_stop_at = min(deadline, time.monotonic() + slice_s)
        try:
            for item in channels.crawl(fetcher, src, chosen, limits, modified_since=modified_since, ctx=ctx):
                if isinstance(item, channels.Checkpoint):
                    store.set_cursor(sid, item.stream, item.value)
                    continue
                item.jurisdiction = item.jurisdiction or src.get("jurisdiction", "")
                item.evidence = item.evidence or "primary"
                item.source_id = sid  # adapters label records with their client; the source id is the stable owner
                if item.kind in TAGGED and "topics" not in item.meta:
                    item.meta["topics"] = topics.tag(item.title, (item.text or "")[:4000])
                for k in ("guide_name", "guide_kind", "level"):     # newsroom / guide sources label their records
                    if src.get(k) and k not in item.meta:
                        item.meta[k] = src[k]
                total += 1
                new += store.add(item)
                if stop.is_set() or time.monotonic() > deadline or not may_run(structured):
                    store.flush()
                    progress.update(sid, status=f"partial ({chosen['channel']} {mode}): {new} new / {total} seen",
                                    new=new, seen=total, fetched=ctx.stats["fetched"], skipped=ctx.stats["skipped_fresh"])
                    return sid, f"partial ({chosen['channel']} {mode}): {new} new / {total} seen"
            store.flush()
            if not modified_since:
                store.clear_cursors(sid, keep=("full",))  # backfill complete; later runs are deltas (the reader's
                                                           # "full pass done" marker stays)
            done = {"reader": READER_VERSIONS[reader_of(src, {"chosen": chosen})]} if reader_of(src, {"chosen": chosen}) in READER_VERSIONS else {}
            progress.update(sid, status="ok", last_ok=started, channel=chosen["channel"], mode=mode, new=new, seen=total,
                            fetched=ctx.stats["fetched"], skipped=ctx.stats["skipped_fresh"], **done)
            return sid, f"{chosen['channel']} {mode}: {new} new / {total} seen ({ctx.stats['fetched']} fetched, " \
                        f"{ctx.stats['skipped_fresh']} skipped as unchanged)"
        except OutOfTime as exc:
            store.flush()
            why = "time slice" if "slice" in str(exc) else "time budget"
            progress.update(sid, status=f"partial ({chosen['channel']} {mode}): {new} new / {total} seen ({why})",
                            new=new, seen=total, fetched=ctx.stats["fetched"], skipped=ctx.stats["skipped_fresh"])
            return sid, f"partial ({chosen['channel']} {mode}): {new} new / {total} seen ({why})"
        except Blocked as exc:
            store.flush()
            fetcher.blocklist.note_source(exc.host, sid)
            progress.update(sid, status=f"blocked: {exc}", new=new, seen=total)
            return sid, f"blocked after {total} records: {exc}"
        except Disallowed as exc:
            store.flush()
            progress.update(sid, status=f"stopped: robots.txt disallows {exc}")
            return sid, f"stopped: robots.txt disallows {exc}"
        except Exception as exc:  # noqa: BLE001
            store.flush()
            cache.invalidate(sid)
            progress.update(sid, status=f"error ({chosen['channel']}): {type(exc).__name__}: {exc}"[:300])
            return sid, f"error ({chosen['channel']}): {type(exc).__name__}: {exc}"[:300]

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    skipped = 0
    try:
        futures = {pool.submit(one, s): s for s in sources}
        for fut in as_completed(futures):
            sid, msg = fut.result()
            summary[sid] = msg
            if msg.startswith("skipped: run time budget"):
                skipped += 1                      # counted, not listed: hundreds of lines said the same thing
            else:
                log(f"{sid}: {msg}")
            if time.monotonic() > deadline:
                stop.set()
    except KeyboardInterrupt:
        stop.set()
        fetcher.stop_at = time.monotonic()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)   # queued sources are dropped; running ones stop at their next request
        store.flush()
        progress.flush()
        fetcher.stop_at = None
        if prev_handler is not None:
            signal.signal(signal.SIGINT, prev_handler)
    for s in sources:
        if s["id"] not in summary and stop.is_set():
            summary[s["id"]] = "skipped: run time budget used up"
            skipped += 1
    if skipped:
        log(f"{skipped} sources not reached this run (time budget); they keep their place in the order for next time")
    return summary
