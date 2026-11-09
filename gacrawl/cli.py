"""gacrawl — command line entry point.

  gacrawl window                         is now a good time to crawl? when's the next one?
  gacrawl seed [--only ID] [--refresh] [--max-age-days D] [--max-minutes M] [--report]
                                         find every jurisdiction's website, meeting portal, code library
  gacrawl status                         registry coverage, crawl progress, database size
  gacrawl discover [--source ID]         probe API/RSS/sitemap/HTML channels and pick the fastest
  gacrawl run [--source ID] [--tier a|b|c] [--shard i/N] [--force] [--full] [--workers N] [--max-minutes M]
  gacrawl delta --since ISO --out FILE   copy what changed since ISO into a small database (for parallel runs)
  gacrawl merge DELTA.db... [--state DIR...]   apply deltas (and shard state) to the main database
  gacrawl search QUERY [--jurisdiction ID] [--kind K] [--limit N]   full-text search of everything crawled
  gacrawl tasks                          write data/manual_tasks.md (blocked hosts, captures, records requests)
  gacrawl blocked [--clear HOST]         list hosts that refused the crawler
  gacrawl ocr FILE... [--url U] [--source ID] [--kind K]
  gacrawl capture URL                    headless screenshot + OCR (robots-allowed pages only)
  gacrawl screens ingest [FILES...] [--candidate ID] [--note TEXT]   read imports/screens/ (ordinances/, posts/, text/)
  gacrawl screens capture URL [--device phone|desktop] [--selector CSS] [--source ID] [--kind K]
  gacrawl screens simulate [--out DIR]   write synthetic screenshots (fictional) to test the pipeline
  gacrawl screens box [--box ordinances|posts|text]                  list what's in the Screenshots box
  gacrawl ordinances build [--out DIR] [--prev DIR] [--report FILE]   read imports/ordinances/ (files downloaded by
                                         hand) into Electo's data/ordinances/ shape; --prev reuses an earlier build
  gacrawl ordinances list                what is in imports/ordinances/ and which place each file goes with
  gacrawl posts account-add|verify|mark|add|pull|list|export
  gacrawl electo import --path DIR       import people, council contests, profile links, code links from Electo
  gacrawl electo packets [--out DIR]     write Electo research packets only
  gacrawl candidates list|research [--candidate ID] [--max-minutes M] [--since DATE] [--skip step,step]
  gacrawl rumor list|add|corroborate|debunk|retract
  gacrawl records status|plan|contacts|send|check|follow-up [--limit N] [--dry-run] [--max-minutes M]
  gacrawl police [police|media|news|all] [--max-minutes M] [--only ID] [--refresh]   police departments, sheriffs, media tip lines; news: reported incidents
                                         Georgia Open Records Act requests for codes of ordinances
  gacrawl export [--out DIR] [--no-packets]   build the Electo import bundle
  gacrawl derive [--only NAME,...] [--full] [--max-minutes M]   parse crawled pages/documents into structured records
  gacrawl transcripts [--max-minutes M]  read transcripts for meetings that carry a video link; optional local transcription
  gacrawl finance [run|probe] [--candidate ID] [--max-minutes M] [--org-limit N]
                                         campaign finance: imports, posted CCDRs, FEC, entities, what the organizations are
  gacrawl sponsors [--max-minutes M]     fetch Legistar sponsor lists for stored ordinances/resolutions
  gacrawl profiles [--candidate ID] [--out DIR]   profile supplements for Electo (positions, ideology, support, career, money)
  gacrawl ocr pending [--max-minutes M]  finish scanned documents a crawl left partly unread (newest first)
  gacrawl backfill status|next [--minutes N --run-id ID --backfill]
                                         daytime backfill bookkeeping: minutes this month, yield, next run
  gacrawl codeacts [--max-minutes M] [--limit N] [--only ID,ID]
                                         enacted 2023-2026 acts parsed into exact Code changes (as-passed text,
                                         struck/underlined words) for Electo's current Code text
  gacrawl legis [update|people|all] [--max-minutes M] [--max-bills N]
                                         General Assembly members, bills, roll calls; per-candidate legislature records
  gacrawl schools seed|stats|sites|people|all [--max-minutes M] [--refresh]
                                         every Georgia school, college and district outline (NCES EDGE); GOSA and CCRPI
                                         statistics; school websites; board members, superintendents and principals
  gacrawl coverage                       what the data holds per Electo feature against the last run (zero or
                                         shrinking features flagged): data/coverage.md, state/coverage.json
  gacrawl seed --districts               school districts only: each answering site must be the district's own
"""
from __future__ import annotations

import argparse
import io
import datetime as dt
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from . import channels, config, registry
from .blocked import Blocked
from .fetch import Disallowed
from .polite import Schedule
from .store import Store



def _fetcher(cfg, deadline: float | None = None):
    """The crawler's fetcher with this command's budget: a wait for a host's next turn that would end after the
    deadline is not taken, and an item of several requests stops at the deadline (the crawl always did this;
    research, transcripts, finance, legislation and OCR used to check only between items)."""
    f = config.make_fetcher(cfg)
    f.stop_at = deadline
    return f

def cmd_window(cfg, args):
    sch = Schedule.from_config(cfg.get("schedule", {}))
    now = dt.datetime.now(dt.timezone.utc)
    good = sch.is_good_time(now)
    print(json.dumps({"good_time": good, "now_local": now.astimezone(sch.tz).isoformat(timespec="minutes"),
                      "next_good_time": None if good else sch.next_good_time(now).isoformat(timespec="minutes")}))


def cmd_seed(cfg, args):
    from .seed import gap_report, seed, seed_school_districts

    jur = registry.jurisdictions(config.ROOT)
    if args.report:
        # no requests: what the last seed could not give a working website, with override stanzas
        discovered = json.loads((cfg["state_dir"] / "discovered.json").read_text()) if (cfg["state_dir"] / "discovered.json").exists() else {}
        md, summary = gap_report(discovered, jur)
        out = config.ROOT / "data" / "seed_gaps.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(json.dumps({"report": str(out), **summary}))
        return
    fetcher = config.make_fetcher(cfg)
    deadline = time.monotonic() + float(args.max_minutes) * 60 if args.max_minutes else None
    govs = [j for j in jur if j["kind"] != "school_district"]
    districts = [j for j in jur if j["kind"] == "school_district"]
    # districts first: 180 of them against 700 cities and counties, and a changed district seed should not wait
    # behind the whole list for its turn
    if districts and (not args.only or any(j["id"] == args.only for j in districts)):
        seed_school_districts(fetcher, districts, cfg["state_dir"] / "discovered.json", workers=args.workers or cfg.get("workers", 6),
                              only=args.only, refresh=args.refresh, max_age_days=args.max_age_days, deadline=deadline)
    if not getattr(args, "districts", False) and (not args.only or any(j["id"] == args.only for j in govs)):
        seed(fetcher, govs, cfg["state_dir"] / "discovered.json", workers=args.workers or cfg.get("workers", 6),
             only=args.only, refresh=args.refresh, max_age_days=args.max_age_days, deadline=deadline)
    cmd_status(cfg, args)


def cmd_status(cfg, args):
    from .scheduler import Progress

    jur = registry.jurisdictions(config.ROOT)
    srcs = registry.all_sources(config.ROOT, cfg.get("limits"))
    prog = Progress(cfg["state_dir"] / "progress.json")
    portals = Counter(k for j in jur for k in j.get("portals", {}))
    st = Counter((prog.get(s["id"]).get("status") or "never run").split(":")[0].split(" (")[0] for s in srcs)
    print(json.dumps({
        "jurisdictions": len(jur), "counties": sum(1 for j in jur if j["kind"] == "county"),
        "municipalities": sum(1 for j in jur if j["kind"] != "county"),
        "with_website": sum(1 for j in jur if j.get("website")),
        "seeded": sum(1 for j in jur if j.get("checked_at")),
        "portals_found": dict(portals), "tiers": dict(Counter(j["tier"] for j in jur)),
        "crawl_sources": len(srcs), "source_status": dict(st),
        "blocked_hosts": list(config.make_fetcher(cfg).blocklist.data.keys()),
        "database": _db_stats(cfg),
    }, indent=1))


def _db_stats(cfg) -> dict:
    path = Path(cfg.get("db_path") or Path(cfg["data_dir"]) / "crawl.db")
    if not path.exists():
        return {"path": str(path), "records": 0, "note": "no database yet (created by the first run)"}
    store = config.make_store(cfg)
    try:
        size = store.path.stat().st_size if store.path.exists() else 0
        return {"path": str(store.path), "mb": round(size / 1e6, 1), "records": store.total(),
                "by_kind": store.counts("kind")}
    finally:
        store.close()


def _sources(cfg, only: str | None, tier: str | None = None, shard: str | None = None):
    from .shard import in_shard, parse_shard

    srcs = registry.all_sources(config.ROOT, cfg.get("limits"))
    sh = parse_shard(shard)
    srcs = [s for s in srcs if in_shard(s, sh)]
    if only:
        srcs = [s for s in srcs if s["id"] == only or s["id"].split(":")[0] == only]
        if not srcs:
            sys.exit(f"no source or jurisdiction with id {only!r} (run `gacrawl seed` first?)")
    if tier:
        srcs = [s for s in srcs if s.get("tier") == tier]
    return srcs


def cmd_discover(cfg, args):
    fetcher = config.make_fetcher(cfg)
    cache = channels.ChannelCache(cfg["state_dir"] / "channels.json", cfg.get("recheck_days", 7))
    for src in _sources(cfg, args.source):
        try:
            probes = channels.probe_source(fetcher, src)
            chosen = channels.choose(probes)
            if chosen:
                cache.put(src["id"], chosen, probes)
            print(f"{src['id']}: using {chosen.channel + ' -> ' + chosen.endpoint if chosen else 'NOTHING WORKS'}")
            for p in probes:
                mark = "ok " if p.ok else "-- "
                print(f"   {mark}{p.channel:<8} {p.latency*1000:7.0f} ms  {p.items:>5} items  {p.endpoint} {p.note}")
        except Exception as exc:  # noqa: BLE001
            print(f"{src['id']}: FAILED {exc}")


def cmd_run(cfg, args):
    from .scheduler import Progress, due, owned_endpoints, run_sources

    fetcher = config.make_fetcher(cfg)
    cache = channels.ChannelCache(cfg["state_dir"] / "channels.json", cfg.get("recheck_days", 7))
    store = config.make_store(cfg)
    progress = Progress(cfg["state_dir"] / "progress.json")
    deadline = time.monotonic() + float(args.max_minutes or cfg.get("max_minutes", 300)) * 60
    import os

    allsrc = _sources(cfg, args.source, args.tier, args.shard)
    if args.kind:
        allsrc = [s for s in allsrc if s.get("kind") == args.kind]
    yt = lambda s: str(s.get("endpoint") or "").startswith("youtube:")  # noqa: E731
    if not os.environ.get("YOUTUBE_API_KEY"):
        allsrc = [s for s in allsrc if not yt(s)]       # kept due until there is a key to list them with
    wanted: set[str] = set()
    try:
        from .candidates import load_candidates

        wanted = {c.get("jurisdiction") for c in load_candidates(config.ROOT, cfg) if c.get("jurisdiction")}
    except Exception:  # noqa: BLE001 — ordering only
        pass
    srcs = due(allsrc, progress, cfg.get("cadence_days"), force=bool(args.force or args.full or args.source), cache=cache, wanted=wanted)
    if os.environ.get("YOUTUBE_API_KEY") and not store.kv_get("youtube:keyed"):
        # first run with a key: list every channel once, even ones an earlier keyless run marked as crawled
        have = {s["id"] for s in srcs}
        srcs += [s for s in allsrc if yt(s) and s["id"] not in have]
        store.kv_set("youtube:keyed", time.strftime("%Y-%m-%d"))
    print(f"{len(srcs)} sources due" + (f" (shard {args.shard})" if args.shard else ""))
    try:
        summary = run_sources(cfg, srcs, fetcher, store, cache, progress, deadline, force=args.force,
                              workers=args.workers or cfg.get("workers", 4), full=args.full,
                              slice_minutes=args.slice_minutes or cfg.get("source_slice_minutes"),
                              owned=owned_endpoints(registry.all_sources(config.ROOT, cfg.get("limits"))))
    finally:
        progress.flush()
        store.close()
    tally = Counter(v.split(":")[0].split(" (")[0] for v in summary.values())
    print(json.dumps({"ran": len(summary), "outcomes": dict(tally)}, indent=1))


def cmd_delta(cfg, args):
    from .shard import write_delta

    store = config.make_store(cfg)
    try:
        print(json.dumps(write_delta(store, args.since, Path(args.out)), indent=1))
    finally:
        store.close()


def cmd_purge(cfg, args):
    """Delete the records of the sources listed in config/purge.yaml (a portal that belongs to another place)."""
    from .registry import load_yaml

    entries = load_yaml(config.ROOT / "config" / "purge.yaml") or []
    store = config.make_store(cfg)
    out = {}
    try:
        for e in entries:
            sid = (e or {}).get("source")
            if not sid:
                continue
            ch = (e or {}).get("channel")      # only what came through this channel (the source itself stays)
            if ch:
                n = store.db.execute("SELECT count(*) FROM records WHERE source_id=? AND channel=?", (sid, ch)).fetchone()[0]
                if args.dry_run or not n:
                    out[f"{sid} ({ch})"] = {"records": n, "deleted": False}
                    continue
                out[f"{sid} ({ch})"] = {**store.purge_source(sid, channel=ch), "deleted": True}
                continue
            n = store.db.execute("SELECT count(*) FROM records WHERE source_id=?", (sid,)).fetchone()[0]
            m = store.db.execute("SELECT count(*) FROM records WHERE source_id LIKE 'cand:%' AND "
                                 "json_extract(meta, '$.source')=?", (sid,)).fetchone()[0]
            if args.dry_run or not (n or m):
                out[sid] = {"records": n, "citing_mentions": m, "deleted": False}
                continue
            out[sid] = {**store.purge_source(sid), "deleted": True}
    finally:
        store.close()
    print(json.dumps(out, indent=1))


def cmd_merge(cfg, args):
    from .shard import merge_delta, merge_state

    store = config.make_store(cfg)
    try:
        for d in args.deltas:
            print(json.dumps(merge_delta(store, Path(d))))
    finally:
        store.close()
    if args.state:
        print(json.dumps(merge_state(cfg["state_dir"], [Path(x) for x in args.state])))


def cmd_search(cfg, args):
    store = config.make_store(cfg)
    try:
        where, params = "1", ()
        if args.jurisdiction:
            where, params = "r.jurisdiction=?", (args.jurisdiction,)
        if args.kind:
            where += " AND r.kind=?"
            params += (args.kind,)
        match = args.query if any(op in args.query for op in ('"', " OR ", " AND ", "NEAR(")) else Store.fts_phrase(args.query)
        for r in store.search(match, where, params, limit=args.limit):
            print(f"{(r.get('published') or '')[:10]:<10} {r['kind']:<12} {r['jurisdiction']:<22} {r['title'][:70]}")
            print(f"           {r['url']}")
            print(f"           {r['snippet'][:220]}")
    finally:
        store.close()


def cmd_tasks(cfg, args):
    from .blocked import BlockList, manual_tasks_md

    md = manual_tasks_md(BlockList(cfg["state_dir"] / "blocked.json"), registry.jurisdictions(config.ROOT),
                         requester=cfg.get("requester", "[your name / organization]"))
    out = cfg["data_dir"] / "manual_tasks.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"wrote {out} ({md.count(chr(10))} lines)")


def cmd_blocked(cfg, args):
    from .blocked import BlockList

    bl = BlockList(cfg["state_dir"] / "blocked.json")
    if args.clear:
        bl.clear(args.clear)
        print(f"cleared {args.clear}")
        return
    for host, rec in sorted(bl.data.items()):
        print(f"{host}: {rec['reason']} (x{rec['count']}, last {rec['last_seen'][:10]}) sources={len(rec.get('sources', []))}")
        for a in rec.get("alternatives", []):
            print(f"   -> {a}")


def cmd_ocr(cfg, args):
    from .ingest import finish_pending_ocr, image_to_record, pdf_to_record

    store = config.make_store(cfg)
    if args.files == ["pending"]:
        try:
            if args.count:      # how many documents wait for OCR (the workflow installs the OCR engine only if any do)
                print(store.db.execute("SELECT count(*) FROM records WHERE meta LIKE '%\"ocr_pending\"%' AND kind IN ('document','ordinance')").fetchone()[0])
                return
            deadline = time.monotonic() + float(args.max_minutes or 8) * 60
            print(json.dumps(finish_pending_ocr(store, _fetcher(cfg, deadline), deadline), indent=1))
        finally:
            store.close()
        return
    for f in args.files:
        p = Path(f)
        fn = pdf_to_record if p.suffix.lower() == ".pdf" else image_to_record
        rec = fn(p, source_id=args.source, kind=args.kind, url=args.url or "")
        rec.jurisdiction = args.source.split(":")[0]
        rec.evidence = "primary"
        store.add(rec)
        out = p.with_suffix(p.suffix + ".md")
        out.write_text(rec.markdown, encoding="utf-8")
        conf = rec.meta.get("mean_confidence")
        print(f"{p.name}: {len(rec.text)} chars, confidence {conf}, review {len(rec.meta.get('needs_review', []))} lines -> {out}")
    store.close()


def cmd_capture(cfg, args):
    args.action, args.files, args.device, args.selector, args.kind = "capture", [args.url], "desktop", None, "page"
    cmd_screens(cfg, args)


def cmd_ordinances(cfg, args):
    from . import ordinance_files as of

    root = config.ROOT
    if args.action == "list":
        inbox = root / "imports" / "ordinances"
        places = of.Places(of.load_places(root))
        units, problems = of.inbox_units(inbox) if inbox.exists() else ([], [])
        for kind, f, rel in units:
            jid, how, _ = of.place_of(places, rel, kind == "bundle")
            print(f"{rel}{'/ (saved code)' if kind == 'bundle' else ''}\t"
                  f"{places.by_id[jid]['name'] if jid else '(place from the text, or unknown)'}")
        for x in problems:
            print("problem:", x)
        print(f"{sum(k == 'bundle' for k, *_ in units)} saved codes, {sum(k == 'file' for k, *_ in units)} files")
        return
    out = Path(args.out)
    out = out if out.is_absolute() else root / out
    rep = of.build(root, out, Path(args.prev) if args.prev else None, ocr_budget_s=args.ocr_minutes * 60)
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in rep.items()}))
    if args.report:
        lines = [f"### Downloaded ordinances: {rep['documents']} documents for {rep['places']} governments "
                 f"({rep['saved_codes']} saved codes, {rep['files']} single files)",
                 f"Read now: {rep['read']} · unchanged (not read again): {rep['reused']} · same file twice: {len(rep['duplicates'])}"]
        if rep["unknown_folders"]:
            lines += ["", "**Folders that are not a place name or id** (rename them, e.g. `alpharetta`, `co-fulton`, "
                      "`Fulton County`):", *[f"- `{x}`" for x in rep["unknown_folders"]]]
        if rep["ambiguous"]:
            lines += ["", "**City and county share the name; filed under the county** (rename the folder "
                      "`City of <name>` if it is the city's code):", *[f"- `{x}`" for x in rep["ambiguous"]]]
        if rep.get("incomplete_downloads"):
            lines += ["", f"**Downloads with text for under half their sections** ({len(rep['incomplete_downloads'])}; "
                      "download these codes again):", *[f"- `{x}`" for x in rep["incomplete_downloads"]]]
        if rep["unplaced"]:
            lines += ["", "**Files with no place** (put them in a folder named for the place):", *[f"- `{x}`" for x in rep["unplaced"]]]
        if rep["errors"]:
            lines += ["", "**Could not read:**", *[f"- {x}" for x in rep["errors"]]]
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_screens(cfg, args):
    from . import screens
    from .blocked import Blocked

    if args.action == "simulate":
        out = Path(args.out or (config.ROOT / "simulated_screens"))  # never the real inbox by default
        for f in screens.simulate(out):
            print(f)
        return
    store = config.make_store(cfg)
    try:
        if args.action == "ingest":
            from .candidates import load_candidates

            inbox = screens.Inbox(config.ROOT, store, load_candidates(config.ROOT, cfg), jurisdictions=registry.jurisdictions(config.ROOT))
            files = [Path(f) for f in args.files] if args.files else None
            print(json.dumps(inbox.ingest(files, candidate_id=args.candidate, note=args.note or "",
                                          move=files is None), indent=1))
        elif args.action == "capture":
            for url in args.files:
                try:
                    rec = screens.capture_record(url, config.make_fetcher(cfg), source_id=args.source or "capture",
                                                 device=args.device, selector=args.selector, kind=args.kind,
                                                 save_to=cfg["data_dir"] / "screens")
                except (Disallowed, Blocked) as exc:
                    print(f"{url}: not captured ({type(exc).__name__}: {exc}). This site doesn't allow automated access; "
                          f"take the screenshot yourself and put it in imports/screens/.")
                    continue
                if not rec:
                    print(f"{url}: page did not load")
                    continue
                store.add(rec)
                print(f"{url}: {len(rec.text)} chars from {rec.meta['text_from']}")
        elif args.action == "box":
            box = screens.Inbox.box_export(store)
            for name, items in box.items():
                if args.box and name != args.box:
                    continue
                print(f"== {name} ({len(items)})")
                for it in items[:50]:
                    print(f"  {it['status']:<13} {', '.join(it['jurisdictions']) or '-':<22} {it['title'][:70]}")
    finally:
        store.close()


def cmd_posts(cfg, args):
    from .verified_posts import Registry

    reg = Registry(cfg["data_dir"] / "verified")
    if args.action == "account-add":
        row = reg.add_account(args.candidate, args.profile, args.proof or "", args.office or "", args.jurisdiction or "")
        if args.candidate_id:
            reg.mark_candidate(row["id"], args.candidate_id)
        print(json.dumps(row, indent=2))
    elif args.action == "verify":
        # accounts checked within the week keep their status (--all re-checks every proof page now)
        for r in reg.verify_accounts(config.make_fetcher(cfg), recheck_days=0 if args.all else 7):
            print(f"{r['status']:<11} {r['id']:<40} {r.get('check_note','')}")
    elif args.action == "mark":
        reg.mark_account(args.account, args.status, args.note or "")
    elif args.action == "add":
        print(json.dumps(reg.add_post(args.url, args.text or "", args.posted_at, args.account, args.screenshot,
                                      topics=args.topics.split(",") if args.topics else None,
                                      archived_url=args.archived or ""), indent=2, ensure_ascii=False))
    elif args.action == "pull":
        print(f"{reg.pull(config.make_fetcher(cfg), args.since)} posts pulled")
    elif args.action == "list":
        for r in reg.posts():
            print(f"{r['status']:<12} {r.get('candidate',''):<24} {r.get('posted_at') or '':<25} {r['post_url']}")
    elif args.action == "export":
        n = reg.export_csv(Path(args.out), only_verified=not args.all)
        print(f"wrote {n} rows to {args.out}")


def cmd_candidates(cfg, args):
    from .candidates import Researcher, load_candidates
    from .scheduler import Progress

    cands = load_candidates(config.ROOT, cfg)
    if args.candidate:
        cands = [c for c in cands if c["id"] == args.candidate or c.get("electo_key") == args.candidate]
        if not cands:
            sys.exit(f"no candidate {args.candidate!r} (config/candidates.yaml or `gacrawl electo import`)")
    if args.action == "list":
        for c in cands[: args.limit or len(cands)]:
            print(f"{c['id']:<40} {c.get('office','')[:40]:<40} {c.get('jurisdiction','')}")
        print(f"{len(cands)} candidates")
        return
    store = config.make_store(cfg)
    r = Researcher(config.ROOT, cfg, config.make_fetcher(cfg), store)
    skip = set(args.skip.split(",")) if args.skip else set()
    try:
        if args.candidate:
            for c in cands:
                print(f"== {c['id']}")
                print("   " + json.dumps(r.research(c, since=args.since, skip=skip)))
            return
        opts = cfg.get("candidates", {})
        deadline = time.monotonic() + float(args.max_minutes or opts.get("max_minutes", 60)) * 60
        progress = Progress(cfg["state_dir"] / "candidate_progress.json")
        last = cfg["state_dir"] / "research_last.json"
        last.unlink(missing_ok=True)       # a run that is killed leaves no yield behind, not the previous run's
        res = r.research_many(cands[: args.limit] if args.limit else cands, args.since, deadline, progress, skip,
                              cadence_days=float(opts.get("cadence_days", 14)),
                              recent_cadence_days=float(opts.get("recent_cadence_days", 3)),
                              workers=args.workers or opts.get("workers", 6))
        print(json.dumps(res, indent=1))
        last.write_text(json.dumps(res))   # the backfill (and the run summary) read this run's yield
    finally:
        store.close()


def cmd_electo(cfg, args):
    if args.action == "import":
        from .electo import import_electo

        if not args.path:
            sys.exit("--path: an Electo checkout (the folder with ga/ and research/)")
        import_electo(Path(args.path), config.ROOT)
    elif args.action == "packets":
        from .candidates import RumorMill, load_candidates
        from .electo import write_packets

        store = config.make_store(cfg)
        try:
            cands = load_candidates(config.ROOT, cfg)
            rumors = RumorMill(config.ROOT).all()
            have = set(s for s in store.sources() if s.startswith("cand:")) | {f"cand:{r['candidate_id']}" for r in rumors}
            write_packets(store, [c for c in cands if f"cand:{c['id']}" in have], rumors,
                          Path(args.out or (config.ROOT / "dist" / "electo" / "packets")))
        finally:
            store.close()


def cmd_rumor(cfg, args):
    from .candidates import RumorMill

    rm = RumorMill(config.ROOT)
    if args.action == "list":
        for r in rm.all():
            if args.candidate and r["candidate_id"] != args.candidate:
                continue
            print(f"{r['id']} {r['status']:<12} {r['candidate_id']:<20} {r['domain']:<28} {r['claim'][:70]}")
    elif args.action == "add":
        row = rm.add(args.candidate, args.url, args.claim or "", origin="manual", author=args.author or "")
        rm.flush()
        print(json.dumps(row, indent=2))
    else:
        status = {"corroborate": "corroborated", "debunk": "debunked", "retract": "retracted"}[args.action]
        row = rm.set_status(args.id, status, args.evidence or "", args.note or "")
        print(json.dumps(row, indent=2) if row else f"no rumor {args.id}")


def cmd_records(cfg, args):
    from .openrecords import Requests, discover_contacts, draft, plan, write_review

    reqs = Requests(config.ROOT, cfg)
    jur = registry.jurisdictions(config.ROOT)
    if args.action == "contacts":
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        n = discover_contacts(_fetcher(cfg, deadline), jur, cfg["state_dir"] / "discovered.json", deadline)
        print(f"checked {n} jurisdictions for a records contact")
        return
    if args.action == "test":
        r = reqs.self_test(jur, to=args.to or "")
        print(json.dumps(r, indent=1))
        if not str(r["smtp"]).startswith("ok") or str(r["imap"]).startswith("failed"):
            raise SystemExit(1)
        return
    if args.action == "deadlines":
        print(json.dumps(reqs.deadlines(), indent=1))
        return
    if args.action == "status":
        print(json.dumps({"sending_enabled": reqs.enabled, "why_not": reqs.why_not() or None, **reqs.summary()}, indent=1))
        return
    store = config.make_store(cfg)
    try:
        if args.action == "plan":
            reviews: list = []
            items = plan(jur, reqs.tracker, store, int(cfg.get("openrecords", {}).get("cooldown_days", 180)),
                         reqs.contacts, reviews)
            write_review(config.ROOT, reviews)
            for it in items[: args.limit or 25]:
                print(f"{it['how']:<6} {it['jurisdiction']['id']:<24} {it['to']}  (found on {it['found_on']})")
            print(f"{len(items)} due; {sum(i['how'] == 'email' for i in items)} by email, "
                  f"{sum(i['how'] == 'portal' for i in items)} portal-only; {len(reviews)} need an address checked "
                  "(data/openrecords/review.md)")
            if items and args.show:
                print("\n" + "\n".join(draft(items[0]["jurisdiction"], reqs.requester or "[requester]",
                                             reqs.mailbox.sender or "", reqs.organization)))
        elif args.action == "send":
            print(json.dumps(reqs.send_batch(jur, store, limit=args.limit, dry_run=args.dry_run), indent=1))
        elif args.action == "check":
            print(json.dumps(reqs.check_replies(), indent=1))
        elif args.action == "follow-up":
            print(json.dumps(reqs.follow_ups(), indent=1))
    finally:
        store.close()


def cmd_derive(cfg, args):
    from . import derive

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes) * 60 if args.max_minutes else None
        only = set(args.only.split(",")) if args.only else None
        print(json.dumps(derive.run(store, only, {"jurisdictions": registry.jurisdictions(config.ROOT), "root": config.ROOT,
                                                  "cfg": cfg}, deadline, full=args.full), indent=1))
    finally:
        store.close()


def cmd_transcripts(cfg, args):
    from . import transcripts

    store = config.make_store(cfg)
    deadline = time.monotonic() + float(args.max_minutes or 20) * 60
    fetcher = _fetcher(cfg, deadline)
    try:
        out = {"meetings": transcripts.from_meetings(fetcher, store, limit=int(args.limit or 60), deadline=deadline)}
        out["recordings"] = transcripts.transcribe_recordings(fetcher, store, cfg, deadline)
        print(json.dumps(out, indent=1))
    finally:
        store.close()


def cmd_finance(cfg, args):
    """Campaign finance: imports, posted CCDRs, FEC, entity resolution, organization lookups.
    export-kv / apply: the FEC re-read done by the parallel fec-reread job reaches the main database (its lines as a
    delta database, its lookups and cursors as a small JSON), and the old lines of every committee it read in full go."""
    from .candidates import load_candidates
    from .finance import Finance

    store = config.make_store(cfg)
    if args.action == "export-kv":
        try:
            with store._lock:
                rows = store.db.execute("SELECT k, v FROM kv WHERE k IN ('fec_ids2','fec_totals') OR k LIKE 'fec:a2:%'").fetchall()
            Path(args.kv).write_text(json.dumps({r[0]: r[1] for r in rows}))
            print(f"finance: {len(rows)} FEC lookups and cursors -> {args.kv}")
        finally:
            store.close()
        return
    if args.action == "export-money":
        # the Donors tab's donors.json and the profiles (their money summaries), for Electo's money-only import
        from .donors import export_donors
        from .profiles import build_profiles, write_profiles

        try:
            out = Path(args.out or "dist/money")
            out.mkdir(parents=True, exist_ok=True)
            cands = load_candidates(config.ROOT, cfg)
            print(json.dumps({"donors": export_donors(store, config.ROOT, cands, out / "donors.json")}))
            profiles, _ = build_profiles(config.ROOT, store, cfg)
            print(json.dumps({"profiles": write_profiles(profiles, out / "profiles", state_dir=cfg["state_dir"])}))
        finally:
            store.close()
        return
    if args.action == "apply":
        from .shard import merge_delta

        try:
            if args.delta and Path(args.delta).exists():
                print(json.dumps(merge_delta(store, Path(args.delta))))
            kv = json.loads(Path(args.kv).read_text()) if args.kv and Path(args.kv).exists() else {}
            for k, v in kv.items():
                if k in ("fec_ids2", "fec_totals"):
                    cur = json.loads(store.kv_get(k) or "{}")
                    cur.update(json.loads(v))           # the parallel job's lookups are the newer ones
                    store.kv_set(k, json.dumps(cur, separators=(",", ":")))
                elif k.startswith("fec:a2:") and (store.kv_get(k) or "") < v:
                    store.kv_set(k, v)
            gone = 0
            for k in kv:
                if k.startswith("fec:a2:"):          # a cursor is set only when the committee was read in full
                    gone += store.delete_where("kind='contribution' AND json_extract(meta,'$.committee')=? AND "
                                               "json_extract(meta,'$.filing_source')='FEC' AND key NOT LIKE 'fec2|%'", (k[7:],))
            store.flush()
            print(f"finance: the FEC re-read applied; {gone} old lines of re-read committees removed")
        finally:
            store.close()
        return
    try:
        # FEC lines read before the FEC's own fields were kept are not counted (gacrawl/money.py) until each
        # committee is read again: while any remain, the FEC re-read gets up to an hour, people with the most first
        with store._lock:
            legacy = store.db.execute(
                "SELECT json_extract(meta,'$.candidate_id') AS c, count(*) AS n FROM records WHERE kind='contribution' AND "
                "json_extract(meta,'$.filing_source')='FEC' AND json_extract(meta,'$.committee') IS NOT NULL AND "
                "json_extract(meta,'$.fec_v') IS NULL GROUP BY c ORDER BY n DESC").fetchall()
        pending = {r["c"]: r["n"] for r in legacy if r["c"]}
        minutes = float(args.max_minutes or 30)
        if pending:
            minutes = max(minutes, 60.0)
            print(f"finance: {sum(pending.values())} FEC lines of {len(pending)} people await the re-read with the FEC's fields")
        elif args.reread_only:
            print("finance: no FEC lines await a re-read")
            return
        deadline = time.monotonic() + minutes * 60
        fin = Finance(config.ROOT, cfg, _fetcher(cfg, deadline), store)
        if args.action == "probe":
            print(json.dumps(fin.probe_commission(), indent=1))
            return
        cands = load_candidates(config.ROOT, cfg)
        if args.candidate:
            cands = [c for c in cands if c["id"] == args.candidate or c.get("electo_key") == args.candidate]
        if pending:     # the re-read first, largest first
            cands.sort(key=lambda c: -pending.get(c["id"], 0))
        if args.reread_only:
            cands = [c for c in cands if c["id"] in pending]
        print(json.dumps(fin.run(cands, since=args.since or cfg.get("limits", {}).get("since", "2016-01-01"),
                                 deadline=deadline, org_limit=int(args.org_limit or fin.opts.get("org_limit", 40))), indent=1))
    finally:
        store.close()


def cmd_sponsors(cfg, args):
    from .sources.legistar import fill_sponsors

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        tokens = {k: v for k, v in (cfg.get("legistar_tokens") or {}).items()}
        print(json.dumps(fill_sponsors(_fetcher(cfg, deadline), store, limit=int(args.limit or 2000), deadline=deadline,
                                       tokens=tokens), indent=1))
    finally:
        store.close()


def cmd_legis(cfg, args):
    """Georgia General Assembly: members, legislation, roll calls; then per-candidate legislature records."""
    from .candidates import load_candidates
    from .sources.legis import legis_people, update

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        out = {}
        if args.action in ("update", "all"):
            out["update"] = update(_fetcher(cfg, deadline), store, config.ROOT, deadline=deadline, max_bills=args.max_bills)
        if args.action in ("reasons", "vetoes"):
            # the Governor's own veto statements (official pages: inside the off-peak window only)
            from .bill_reasons import vetoes

            if Schedule.from_config(cfg.get("schedule", {})).is_good_time(dt.datetime.now(dt.timezone.utc)):
                out["vetoes"] = vetoes(_fetcher(cfg, deadline), store, config.ROOT, cfg["state_dir"], deadline)
            else:
                out["vetoes"] = "outside the off-peak window: the Governor's pages are read at night"
        if args.action in ("video", "histories", "hearings"):
            # committee hearing video: each bill's dated committee steps (the data service, any time), then the official
            # recordings of that committee on those days (channel pages: inside the off-peak window)
            from .legis_video import histories, link_videos

            if args.action != "hearings":
                # at most half the time on bill histories, so linking and the hearings get the rest
                half = (time.monotonic() + (deadline - time.monotonic()) / 2) if (deadline and args.action == "video") else deadline
                out["histories"] = histories(_fetcher(cfg, half), store, half)
            if args.action == "hearings":
                from .legis_video import hearings

                out["hearings"] = hearings(_fetcher(cfg, deadline), store, cfg, deadline)
            if args.action == "video":
                out["video"] = link_videos(_fetcher(cfg, deadline), store, config.ROOT, deadline,
                                           pages=not getattr(args, "no_pages", False))
                if getattr(args, "with_hearings", False):
                    from .legis_video import hearings

                    out["hearings"] = hearings(_fetcher(cfg, deadline), store, cfg, deadline)
        if args.action == "reasons":
            # why decided bills passed, were vetoed or died: what trusted outlets reported, through their own news APIs
            from .bill_reasons import research

            out["reasons"] = research(_fetcher(cfg, deadline), store, config.ROOT, cfg["state_dir"], deadline, limit=getattr(args, "max_bills", None))
        if args.action in ("budgets", "all"):
            out["budgets"] = schools.budgets(f, config.ROOT, store, deadline=deadline)
        if args.action in ("people", "all"):
            people = legis_people(store, load_candidates(config.ROOT, cfg), config.ROOT)
            p = cfg["state_dir"] / "legis_people.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(people, ensure_ascii=False), encoding="utf-8")
            out["people"] = {"matched": len(people), "with_votes": sum(1 for v in people.values() if v["votes"]),
                             "with_bills": sum(1 for v in people.values() if v["bills"])}
        print(json.dumps(out, indent=1))
    finally:
        store.close()


def cmd_schools(cfg, args):
    """Schools, colleges, district outlines, official statistics, school websites and the people who run them."""
    from . import schools

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        out = {}
        f = _fetcher(cfg, deadline)
        if args.action in ("seed", "all"):
            out["seed"] = schools.seed(f, config.ROOT, store, deadline=deadline)
        if args.action in ("stats", "all"):
            out["stats"] = schools.stats(f, config.ROOT, store, deadline=deadline, refresh=args.refresh)
        if args.action in ("sites", "all"):
            out["sites"] = schools.sites(f, config.ROOT, store, deadline=deadline)
        if args.action in ("people", "all"):
            out["people"] = schools.people(config.ROOT, store)
        print(json.dumps(out, indent=1))
    finally:
        store.close()


def cmd_codeacts(cfg, args):
    """Enacted 2023-2026 acts: their as-passed text parsed into exact Code changes (for Electo's current Code text)."""
    from . import codeacts

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        only = [int(x) for x in args.only.split(",")] if args.only else None
        try:
            out = codeacts.run(_fetcher(cfg, deadline), store, config.ROOT, deadline=deadline, limit=args.limit, only=only)
        except (Disallowed, Blocked) as exc:
            out = {"error": f"{type(exc).__name__}: {exc}", "note": "legis.ga.gov refused or its robots.txt could not be read; "
                                                                   "nothing read this run, tried again next run"}
        print(json.dumps(out, indent=1))
    finally:
        store.close()


def cmd_news(cfg, args):
    """Georgia political news for Electo's News tab: outlets' news APIs and feeds, the last ten days."""
    from .newsdigest import Digest

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 6) * 60
        stats = Digest(_fetcher(cfg, deadline), store, config.ROOT, cfg["state_dir"]).run(deadline)
        if getattr(args, "export", None):
            from .newsdigest import export_news

            out = Path(args.export)
            out.parent.mkdir(parents=True, exist_ok=True)
            stats["export"] = export_news(store, out, out.with_name("news_text.json"))
    finally:
        store.close()
    print(json.dumps({"news": stats}))


def cmd_donors_ai(cfg, args):
    """The open model's two-sentence summaries of the organizations and committees that gave money (donor profiles)."""
    from .donors_ai import run

    store = config.make_store(cfg)
    try:
        print(json.dumps(run(store, config.ROOT, time.monotonic() + float(args.max_minutes or 20) * 60)))
    finally:
        store.close()


def cmd_rumors_ai(cfg, args):
    """The open model's reading of each Rumor Mill item (is the person named, what it rests on), for the rating."""
    from .candidates import load_candidates
    from .rumors_ai import run

    cands = load_candidates(config.ROOT, cfg)
    print(json.dumps(run(config.ROOT, cands, time.monotonic() + float(args.max_minutes or 10) * 60)))


def cmd_video_ai(cfg, args):
    """Official meeting videos' captions as transcripts, and the open model's reading of what each meeting did."""
    from .video_ai import run

    store = config.make_store(cfg)
    try:
        print(json.dumps(run(store, config.ROOT, time.monotonic() + float(args.max_minutes or 20) * 60,
                             captions_only=args.captions_only)))
    finally:
        store.close()


def cmd_channel_ai(cfg, args):
    """The open model's reading of unconfirmed meeting-video channels, taught by the owner's channel decisions."""
    from .channel_ai import run

    print(json.dumps(run(config.ROOT, time.monotonic() + float(args.max_minutes or 5) * 60)))


def cmd_federal_bills(cfg, args):
    """Every U.S. House and Senate bill and resolution since 2015 (GovInfo Bill Status), or (--ai) the open model's plain
    descriptions of the ones people look at most."""
    from . import federal_bills

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        if args.ai:
            print(json.dumps(federal_bills.ai_run(store, config.ROOT, deadline)))
        else:
            print(json.dumps(federal_bills.run(_fetcher(cfg, deadline), store, deadline)))
    finally:
        store.close()


def cmd_discover_ai(cfg, args):
    """The open model's discovery: new people in the news, sites for people and places with none (DuckDuckGo search),
    each page judged by the model and checked; auto-added when as good as accepted links, else data/seed_review.md."""
    from .candidates import load_outlets
    from .discover_ai import Discovery
    from .websearch import Search

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 30) * 60
        _, exclude = load_outlets(config.ROOT)
        d = Discovery(_fetcher(cfg, deadline), store, config.ROOT, cfg["state_dir"], Search(cfg["state_dir"], exclude))
        if args.decisions_only:
            n = d.apply_decisions(Path(args.decisions_only))
            d.finish()
            print(json.dumps({"discover": {"decisions": n}}))
            return
        stats = d.run(deadline)
    finally:
        store.close()
    print(json.dumps({"discover": stats}))


def cmd_ethics(cfg, args):
    """Georgia state and local campaign money from the State Ethics Commission's search site (gacrawl/ethics.py)."""
    from . import ethics

    f = config.make_fetcher(cfg)
    if args.action == "explore":
        print(json.dumps(ethics.explore(f, Path(args.out or "data/ethics_explore"))))
        return
    if args.action == "bulk":
        print(json.dumps(ethics.bulk_probe(Path(args.out or "data/ethics_probe"))))
        return
    if args.action == "clicks":
        print(json.dumps(ethics.click_probe(Path(args.out or "data/ethics_probe"))))
        return
    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 60) * 60
        from .candidates import load_candidates

        cands = load_candidates(config.ROOT, cfg)
        st = ethics.run(_fetcher(cfg, deadline), store, config.ROOT, cands, deadline)
        # donor and recipient ids for the new lines now (the finance step ran before this one), so this run's export
        # already puts them in profiles' money, the connections graph and the donor profiles
        from .finance import Finance

        st["entities"] = Finance(config.ROOT, cfg, None, store).resolve_all(cands)
        print(json.dumps(st))
    finally:
        store.close()


def cmd_coverage(cfg, args):
    """What the data holds per Electo feature, against the last run: state/coverage.json and data/coverage.md."""
    import os

    from .coverage import run

    store = config.make_store(cfg)
    try:
        out = run(config.ROOT, store)
    finally:
        store.close()
    print(f"coverage: {len(out['zero'])} features at zero, {len(out['shrank'])} shrank since the last run "
          f"(data/coverage.md{' in the private repository' if os.environ.get('PUBLIC_RUNNER') == 'true' else ''})")
    if os.environ.get("GITHUB_ACTIONS") and os.environ.get("PUBLIC_RUNNER") != "true":
        for k in out["zero"]:
            print(f"::warning title=coverage::{k} is at zero")
        for k in out["shrank"]:
            print(f"::warning title=coverage::{k} shrank since the last run")


def cmd_summary(cfg, args):
    """A Markdown summary of the run that just finished (the workflow appends it to the job summary)."""
    from .summary import build

    store = None
    try:
        store = config.make_store(cfg) if (Path(cfg.get("db_path") or Path(cfg["data_dir"]) / "crawl.db")).exists() else None
        md = build(config.ROOT, cfg["state_dir"], store, args.since, args.title or "Crawl run")
    finally:
        if store is not None:
            store.close()
    if args.out:
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(md)
    print(md)


def cmd_probe(cfg, args):
    """Look at sites the way the crawler does (raw page, links, forms; with --browser the requests the page makes)."""
    from .probe import probe

    accept = {"json": "application/json", "xml": "application/xml", "html": "text/html"}.get(args.accept or "", "")
    f = config.make_fetcher(cfg)
    if args.timeout:      # for the connection (TLS handshake) and the answer alike, on every host probed
        f.host_timeouts = {urlsplit(u).netloc.lower(): float(args.timeout) for u in args.urls}
    probe(f, args.urls, Path(args.out), browser=args.browser, accept=accept, grep=args.grep or "")


def cmd_backfill(cfg, args):
    """Daytime backfill bookkeeping (gacrawl/backfill.py): `status`, or `next` at the end of a run."""
    from . import backfill
    from .candidates import load_candidates
    from .scheduler import Progress

    def never():
        try:
            prog = Progress(cfg["state_dir"] / "candidate_progress.json")
            return sum(1 for c in load_candidates(config.ROOT, cfg) if not prog.get(f"cand:{c['id']}").get("last_ok"))
        except Exception:  # noqa: BLE001
            return None
    if args.action == "status":
        st = backfill.load_state(config.ROOT)
        print(json.dumps({"plan": backfill.plan(config.ROOT), "never_researched": never(),
                          "state": {k: v for k, v in st.items() if k != "history"}, "recent": st.get("history", [])[-5:]}, indent=1))
        return
    rp = cfg["state_dir"] / "research_last.json"
    research = json.loads(rp.read_text()) if rp.exists() else None
    total, cache = None, None
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if args.repo and token:
        # the month's minutes as GitHub bills them, every run of every workflow and branch in this repository
        st = backfill.load_state(config.ROOT)
        month_start = dt.datetime.now(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev = (st.get("run_minutes") or {})
        try:
            total, cache = backfill.actions_minutes(args.repo, token, month_start,
                                                    cache=prev.get("runs") if prev.get("month") == f"{month_start:%Y-%m}" else None)
        except Exception as exc:  # noqa: BLE001 — the run's own tally still counts
            print(f"could not count this month's Actions minutes: {type(exc).__name__}: {exc}"[:200])
    out = backfill.next_run(config.ROOT, int(args.minutes or 0), bool(args.backfill), research,
                            never() if args.backfill else None, run_id=args.run_id or "", month_total=total, run_cache=cache)
    print(json.dumps(out, indent=1))
    if args.github_output:
        with open(args.github_output, "a") as f:
            f.write(f"go={'true' if out.get('go') else 'false'}\n")


def cmd_profiles(cfg, args):
    """Profile supplements (positions, ideology markers, endorsements, ratings, votes, bills, career, money)."""
    from .profiles import build_profiles, write_profiles

    store = config.make_store(cfg)
    try:
        profiles, bundle = build_profiles(config.ROOT, store, cfg)
        if args.candidate:
            for p in profiles:
                if p["candidate_id"] == args.candidate or p.get("key") == args.candidate:
                    print(json.dumps(p, indent=1, ensure_ascii=False))
            return
        out = Path(args.out or (config.ROOT / "dist" / "electo" / "profiles"))
        n = write_profiles(profiles, out, state_dir=cfg["state_dir"])
        thin = sum(1 for p in profiles if p["inference_inputs"] < 2)
        print(json.dumps({"profiles": n, "thin": thin, "graph": bundle["graph"]}, indent=1))
    finally:
        store.close()


def cmd_police(cfg, args):
    """Police departments, sheriffs and media tip lines from official pages (Electo's Policing tab)."""
    from .police import discover_media, discover_police

    deadline = time.monotonic() + float(args.max_minutes or 10) * 60
    if args.action in ("police", "all"):
        n = discover_police(_fetcher(cfg, deadline), registry.jurisdictions(config.ROOT), cfg["state_dir"], deadline,
                            only=args.only, refresh=args.refresh)
        print(f"read the police section of {n} jurisdictions")
    if args.action in ("media", "all"):
        n = discover_media(_fetcher(cfg, deadline), config.ROOT, cfg["state_dir"], deadline, refresh=args.refresh)
        print(f"checked {n} outlets for news-tip addresses")
    if args.action == "news":
        from .policing_news import research

        store = config.make_store(cfg)
        try:
            print(json.dumps(research(_fetcher(cfg, deadline), store, config.ROOT, cfg["state_dir"], deadline, limit=args.limit), indent=1))
        finally:
            store.close()


def cmd_export(cfg, args):
    if getattr(args, "policing_only", False):
        # police.json, media.json, records_contacts.json from state alone (no database): the police job patches these
        # three into the published bundle between nightly exports
        from .police import export_policing

        out = Path(args.out or (config.ROOT / "dist" / "electo"))
        out.mkdir(parents=True, exist_ok=True)
        counts = export_policing(config.ROOT, out, registry.jurisdictions(config.ROOT))
        m = out / "manifest.json"
        if m.exists():
            man = json.loads(m.read_text())
            man["policing"] = counts
            m.write_text(json.dumps(man, indent=1, ensure_ascii=False))
        print(json.dumps(counts))
        return
    from .export import export

    store = config.make_store(cfg)
    try:
        export(config.ROOT, Path(args.out or (config.ROOT / "dist" / "electo")), store=store, cfg=cfg,
               packets=not args.no_packets)
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gacrawl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("window")
    s = sub.add_parser("seed"); s.add_argument("--only"); s.add_argument("--refresh", action="store_true")
    s.add_argument("--report", action="store_true", help="write data/seed_gaps.md (no requests): what still has no working site, by cause")
    s.add_argument("--workers", type=int); s.add_argument("--max-age-days", type=float); s.add_argument("--max-minutes", type=float)
    s.add_argument("--districts", action="store_true", help="school districts only (each runner run: a changed district seed is worked off at once)")
    sub.add_parser("status")
    et = sub.add_parser("ethics", help="Georgia campaign money from the State Ethics Commission's search site")
    et.add_argument("action", nargs="?", default="run", choices=["run", "explore", "clicks", "bulk"])
    et.add_argument("--out"); et.add_argument("--max-minutes", type=float, default=60)
    da = sub.add_parser("discover-ai", help="the open model's discovery: new people, sites for people and places with none")
    da.add_argument("--max-minutes", type=float, default=30)
    da.add_argument("--decisions-only", help="only apply the owner's decisions in this copy of data/seed_review.md")
    dai = sub.add_parser("donors-ai", help="the open model's summaries of organizations that gave money (donor profiles)")
    dai.add_argument("--max-minutes", type=float, default=20)
    rai = sub.add_parser("rumors-ai", help="the open model's reading of the Rumor Mill items (for Rumor Has It's ratings)")
    rai.add_argument("--max-minutes", type=float, default=10)
    vai = sub.add_parser("video-ai", help="official meeting videos' captions as transcripts, read by the open model")
    vai.add_argument("--max-minutes", type=float, default=20)
    vai.add_argument("--captions-only", action="store_true", help="transcripts only, no model")
    cai = sub.add_parser("channel-ai", help="the open model's reading of unconfirmed meeting channels (taught by the owner's decisions)")
    cai.add_argument("--max-minutes", type=float, default=5)
    fbp = sub.add_parser("federal-bills", help="U.S. House and Senate bills since 2015 (GovInfo Bill Status bulk data)")
    fbp.add_argument("--max-minutes", type=float, default=20)
    fbp.add_argument("--ai", action="store_true", help="the open model's plain descriptions instead of reading")
    nw = sub.add_parser("news", help="Georgia political news for the News tab (outlets' news APIs and feeds)")
    nw.add_argument("--max-minutes", type=float, default=6)
    nw.add_argument("--export", help="also write the digest (Electo's news.json format) to this path")
    sub.add_parser("coverage", help="what the data holds per Electo feature, against the last run (data/coverage.md)")
    pg = sub.add_parser("purge", help="delete the records of the sources listed in config/purge.yaml")
    pg.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("discover"); d.add_argument("--source")
    r = sub.add_parser("run"); r.add_argument("--source"); r.add_argument("--force", action="store_true")
    r.add_argument("--full", action="store_true", help="re-crawl from the start of history (no delta)")
    r.add_argument("--shard", help="i/N: only sources whose host hashes to shard i of N")
    r.add_argument("--max-minutes", type=float); r.add_argument("--workers", type=int); r.add_argument("--tier", choices=["a", "b", "c"])
    r.add_argument("--kind", help="only sources of this kind (video = YouTube channels, ...)")
    r.add_argument("--slice-minutes", type=float, help="one source's share of the run (default: a sixth of the budget, at least 4)")
    dl = sub.add_parser("delta"); dl.add_argument("--since", required=True); dl.add_argument("--out", required=True)
    mg = sub.add_parser("merge"); mg.add_argument("deltas", nargs="*"); mg.add_argument("--state", nargs="*", default=[])
    se = sub.add_parser("search"); se.add_argument("query"); se.add_argument("--jurisdiction"); se.add_argument("--kind")
    se.add_argument("--limit", type=int, default=20)
    sub.add_parser("tasks")
    b = sub.add_parser("blocked"); b.add_argument("--clear")
    o = sub.add_parser("ocr"); o.add_argument("files", nargs="+", help="files to read, or 'pending' to finish documents left partly unread by a crawl")
    o.add_argument("--url"); o.add_argument("--max-minutes", type=float)
    o.add_argument("--count", action="store_true", help="pending: print how many documents wait for OCR and exit")
    o.add_argument("--source", default="manual"); o.add_argument("--kind", default="ocr")
    c = sub.add_parser("capture"); c.add_argument("url"); c.add_argument("--source", default="capture")
    sc = sub.add_parser("screens"); sc.add_argument("action", choices=["ingest", "capture", "simulate", "box"])
    sc.add_argument("files", nargs="*", help="ingest: image files (default: imports/screens/); capture: URLs")
    sc.add_argument("--candidate"); sc.add_argument("--note"); sc.add_argument("--device", choices=["phone", "desktop"], default="phone")
    sc.add_argument("--selector"); sc.add_argument("--source"); sc.add_argument("--kind", default="page"); sc.add_argument("--out")
    sc.add_argument("--box", choices=["ordinances", "posts", "text"])
    od = sub.add_parser("ordinances", help="ordinances downloaded by hand (imports/ordinances/) -> Electo's data/ordinances/")
    od.add_argument("action", choices=["build", "list"]); od.add_argument("--out", default="dist/ordinances")
    od.add_argument("--prev", help="an earlier build (Electo's data/ordinances/): files read before are not read again")
    od.add_argument("--report", help="also write a Markdown report here (the workflow's job summary)")
    od.add_argument("--ocr-minutes", type=float, default=15.0, help="OCR time per scanned document")
    p = sub.add_parser("posts"); p.add_argument("action", choices=["account-add", "verify", "mark", "add", "pull", "list", "export"])
    for flag in ("--candidate", "--candidate-id", "--profile", "--proof", "--office", "--jurisdiction", "--account", "--status",
                 "--note", "--url", "--text", "--posted-at", "--screenshot", "--topics", "--archived"):
        p.add_argument(flag)
    p.add_argument("--since", default="2016-01-01"); p.add_argument("--out", default="verified_posts.csv")
    p.add_argument("--all", action="store_true", help="verify: re-check every proof page now (otherwise those older than the recheck period)")
    el = sub.add_parser("electo"); el.add_argument("action", choices=["import", "packets"]); el.add_argument("--path")
    el.add_argument("--out")
    ca = sub.add_parser("candidates"); ca.add_argument("action", choices=["list", "research"]); ca.add_argument("--candidate")
    ca.add_argument("--workers", type=int, help="candidates researched at once (default: candidates.workers, 6)")
    ca.add_argument("--since", default="2016-01-01"); ca.add_argument("--max-minutes", type=float); ca.add_argument("--limit", type=int)
    ca.add_argument("--skip", help="comma list: mentions,votes,posts,statements,articles,leads,social_mentions,connections")
    ru = sub.add_parser("rumor"); ru.add_argument("action", choices=["list", "add", "corroborate", "debunk", "retract"])
    for flag in ("--candidate", "--url", "--claim", "--author", "--id", "--evidence", "--note"):
        ru.add_argument(flag)
    e = sub.add_parser("export"); e.add_argument("--out"); e.add_argument("--no-packets", action="store_true"); e.add_argument("--policing-only", action="store_true")
    po = sub.add_parser("police", help="police departments, sheriffs and media tip lines from official pages"); po.add_argument("action", nargs="?", default="all", choices=["police", "media", "news", "all"]); po.add_argument("--max-minutes", type=float); po.add_argument("--only"); po.add_argument("--refresh", action="store_true"); po.add_argument("--limit", type=int)
    dv = sub.add_parser("derive"); dv.add_argument("--only"); dv.add_argument("--full", action="store_true"); dv.add_argument("--max-minutes", type=float)
    tr = sub.add_parser("transcripts"); tr.add_argument("--max-minutes", type=float); tr.add_argument("--limit", type=int)
    fi = sub.add_parser("finance"); fi.add_argument("action", nargs="?", default="run", choices=["run", "probe", "export-kv", "apply", "export-money"])
    fi.add_argument("--delta"); fi.add_argument("--kv"); fi.add_argument("--out")
    fi.add_argument("--candidate"); fi.add_argument("--since"); fi.add_argument("--max-minutes", type=float); fi.add_argument("--org-limit", type=int)
    fi.add_argument("--reread-only", action="store_true", help="only the FEC re-read of lines kept before the FEC's fields were (nothing when none remain)")
    sp = sub.add_parser("sponsors"); sp.add_argument("--max-minutes", type=float); sp.add_argument("--limit", type=int)
    pf = sub.add_parser("profiles"); pf.add_argument("--candidate"); pf.add_argument("--out")
    lg = sub.add_parser("legis"); lg.add_argument("action", nargs="?", default="all", choices=["update", "people", "reasons", "vetoes", "video", "histories", "hearings", "all"])
    lg.add_argument("--max-minutes", type=float); lg.add_argument("--max-bills", type=int)
    lg.add_argument("--no-pages", action="store_true", help="video: channels' APIs only, not the legislature's pages")
    lg.add_argument("--with-hearings", action="store_true", help="video: then read the linked hearings' caption files")
    sh = sub.add_parser("schools"); sh.add_argument("action", nargs="?", default="all", choices=["seed", "stats", "sites", "budgets", "people", "all"])
    sh.add_argument("--max-minutes", type=float); sh.add_argument("--refresh", action="store_true", help="stats: re-read files already read")
    sm = sub.add_parser("summary"); sm.add_argument("--since"); sm.add_argument("--title"); sm.add_argument("--out")
    pb = sub.add_parser("probe"); pb.add_argument("urls", nargs="+"); pb.add_argument("--out", default="probe")
    pb.add_argument("--browser", action="store_true")
    pb.add_argument("--accept", choices=["json", "xml", "html"], help="ask for this content type (APIs answer differently by Accept)")
    pb.add_argument("--timeout", type=float, help="read timeout in seconds (default: the crawler's own)")
    pb.add_argument("--grep", help="text files: print up to 25 lines matching this regular expression")
    bf = sub.add_parser("backfill"); bf.add_argument("action", choices=["status", "next"]); bf.add_argument("--minutes", type=int)
    bf.add_argument("--run-id"); bf.add_argument("--backfill", action="store_true"); bf.add_argument("--github-output")
    bf.add_argument("--repo", help="owner/name: count the month's Actions minutes of every run in it (needs GH_TOKEN)")
    cx = sub.add_parser("codeacts"); cx.add_argument("--max-minutes", type=float); cx.add_argument("--limit", type=int)
    cx.add_argument("--only", help="comma list of legislation ids")
    rq = sub.add_parser("records"); rq.add_argument("action", choices=["status", "plan", "contacts", "send", "check", "follow-up", "test", "deadlines"])
    rq.add_argument("--to", help="test: send the sample here instead of the project mailbox")
    rq.add_argument("--limit", type=int); rq.add_argument("--dry-run", action="store_true")
    rq.add_argument("--max-minutes", type=float); rq.add_argument("--show", action="store_true", help="plan: print the first draft")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    import faulthandler

    try:
        faulthandler.enable()       # a crash in a C extension prints the Python stack instead of a bare "Segmentation fault"
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        pass                        # stderr with no file descriptor (tests capture it)
    cfg = config.load()
    {"window": cmd_window, "seed": cmd_seed, "status": cmd_status, "discover": cmd_discover, "run": cmd_run,
     "delta": cmd_delta, "merge": cmd_merge, "search": cmd_search, "tasks": cmd_tasks, "blocked": cmd_blocked,
     "ocr": cmd_ocr, "capture": cmd_capture, "screens": cmd_screens, "ordinances": cmd_ordinances, "posts": cmd_posts, "electo": cmd_electo, "candidates": cmd_candidates,
     "rumor": cmd_rumor, "export": cmd_export, "records": cmd_records,
     "derive": cmd_derive, "purge": cmd_purge, "transcripts": cmd_transcripts, "finance": cmd_finance, "sponsors": cmd_sponsors,
     "profiles": cmd_profiles, "legis": cmd_legis, "schools": cmd_schools, "codeacts": cmd_codeacts, "backfill": cmd_backfill, "probe": cmd_probe, "summary": cmd_summary, "police": cmd_police,
     "coverage": cmd_coverage, "news": cmd_news, "discover-ai": cmd_discover_ai, "donors-ai": cmd_donors_ai, "rumors-ai": cmd_rumors_ai, "video-ai": cmd_video_ai, "channel-ai": cmd_channel_ai, "federal-bills": cmd_federal_bills, "ethics": cmd_ethics}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
