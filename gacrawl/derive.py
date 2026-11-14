"""`gacrawl derive`: turn crawled pages and documents into structured records.

Each deriver looks at records changed since its last run (a cursor in the kv
table), parses the ones it understands, and writes derived records whose
`meta.derived_from` is the source record's id. Re-running is safe: derived
records have natural keys, so a re-parse updates rather than duplicates, and a
source record that changes gets re-derived.

Derivers (in run order)
  elections    city/county results pages and PDFs -> election_result; qualifying lists -> qualifying
  minutes      council minutes -> agenda_item + vote (motions, movers, roll calls)
  ballot       referendum notices and sample ballots -> ballot_question
  press        press releases -> appointment
  guides       voter guides, questionnaires, endorsement pages -> questionnaire, endorsement
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .store import Record, Store


@dataclass
class Deriver:
    name: str
    where: str                         # SQL filter on source records (no 'cand:' sources unless stated)
    fn: Callable[[dict, dict], Iterable[Record]]   # (source record, context) -> derived records
    params: tuple = ()
    needs: dict = field(default_factory=dict)      # context keys the deriver wants (e.g. jurisdictions)
    # a change in what the deriver writes: a new version reads every source record again, once (resumably: a pass that
    # runs out of time keeps its place like any other)
    version: str = ""


REGISTRY: list[Deriver] = []


def register(d: Deriver):
    REGISTRY.append(d)
    return d


def run(store: Store, only: set[str] | None = None, context: dict | None = None, deadline: float | None = None,
        log=print, full: bool = False) -> dict:
    """Run derivers over changed records. `full` ignores cursors (re-derive everything)."""
    import importlib

    for mod in ("elections", "minutes", "ballot", "press", "guides"):  # registering imports, in run order
        try:
            importlib.import_module(f"gacrawl.{mod}")
        except ModuleNotFoundError as exc:
            if exc.name != f"gacrawl.{mod}":
                raise

    context = context or {}
    out = {}
    for d in REGISTRY:
        if only and d.name not in only:
            continue
        cursor = None if full else store.kv_get(f"derive:{d.name}")
        if d.version and store.kv_get(f"derive:{d.name}:v") != d.version:
            cursor = None                               # the new version's pass starts from the beginning
            store.kv_set(f"derive:{d.name}:v", d.version)
            log(f"{d.name}: version {d.version}: every source record is read again")
        started = store_now()
        seen = made = 0
        last = None          # the last source record this pass finished with
        partial = False
        for src in store.changed_since(cursor, d.where, d.params):
            if deadline and time.monotonic() > deadline:
                partial = True
                break
            seen += 1
            try:
                for rec in d.fn(src, context) or ():
                    rec.jurisdiction = rec.jurisdiction or src.get("jurisdiction", "")
                    rec.evidence = rec.evidence or src.get("evidence") or "primary"
                    rec.meta.setdefault("derived_from", src["id"])
                    rec.meta.setdefault("derived_url", src["url"])
                    made += store.add(rec)
            except Exception as exc:  # noqa: BLE001 — one bad document must not stop the run
                log(f"  {d.name}: {src['id']} {type(exc).__name__}: {exc}"[:300])
            last = src
        if not partial:
            store.kv_set(f"derive:{d.name}", started)   # the pass completed: everything changed before it started is done
        elif last and last.get("changed_at"):
            # out of time: keep the place reached, so the next run carries on instead of re-reading the same records.
            # Sources are read oldest change first; one second back, so records that changed in the same second as
            # the last one are seen again (a re-derive is an update, never a duplicate).
            store.kv_set(f"derive:{d.name}", _second_before(last["changed_at"]))
        store.flush()
        out[d.name] = {"scanned": seen, "derived": made, "partial": partial}
        log(f"{d.name}: {seen} scanned, {made} new/changed" + (" (out of time; resumes next run)" if partial else ""))
    return out


def _second_before(iso: str) -> str:
    import datetime as dt

    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return (t - dt.timedelta(seconds=1)).isoformat()


def store_now() -> str:
    from .store import utcnow

    return utcnow()
