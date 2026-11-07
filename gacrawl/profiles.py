"""`gacrawl profiles`: one profile supplement per candidate, in Electo's profile item shapes.

profiles/<key>.json  ideology (+isrc), positions, statements, ratings, endorsements (+esrc),
                     votes, bills, ordinances supported/opposed, affiliations, career, money,
                     sources[{t,u,d}] — every item with src indexes into sources, exactly as
                     Electo's inferEvidence() reads a profile. `inference_inputs` is the count
                     Electo would see (it infers only from profiles with two or more).
profiles/index.json  per-candidate counts and depth, thinnest first, so research can be aimed
                     at the candidates Electo cannot yet infer anything for.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .candidates import load_candidates
from .careers import CareerIndex, career_for, summary as career_summary
from .connections import build, money_for, patterns
from .electo import slug
from .entities import EntityIndex
from .stances import SourceList, TranscriptSpeakers, profile_supplement
from .store import Store


class ProfileBuilder:
    """Everything shared by the profiles of one run (entity index, connections graph and patterns, career and
    transcript indexes), built once; `profile(c, records)` then builds one candidate's supplement, from records
    the caller already read when given (the export reads each candidate's records once for all its outputs)."""

    def __init__(self, root: Path, store: Store, cfg: dict | None, cands: list[dict], legis: dict | None = None):
        self.root, self.store, self.legis = root, store, legis or {}
        self.ix = EntityIndex(root / "state" / "entities.json", store=store)
        for c in cands:
            self.ix.add_candidate(c)
        self.g = build(store, self.ix)
        self.pats = patterns(self.g, store)
        self.g.index_patterns(self.pats)
        self.ix.flush()
        self.cand_sources = {s for s in store.sources() if s.startswith("cand:")}
        self.careers_ix, self.speakers = CareerIndex(store), TranscriptSpeakers(store)
        self.inputs: dict[str, int] = {}

    def profile(self, c: dict, records: dict | None = None) -> dict | None:
        has_records = f"cand:{c['id']}" in self.cand_sources
        if not has_records and not c.get("races"):
            return None
        money = money_for(self.g, self.store, self.ix, c["id"], self.pats) if has_records else {}
        lg = self.legis.get(c.get("electo_key") or "", None)
        prof = profile_supplement(c, self.store, self.root, money=money, legis=lg, speakers=self.speakers,
                                  records=records if records is not None else (None if has_records else {}))
        S = SourceList()
        S.items, S.index = prof["sources"], {k: i for i, k in enumerate(_keys(prof["sources"]))}
        prof["career"] = career_for(c, self.store, S, prof.get("affiliations"), (lg or {}).get("member"), index=self.careers_ix)
        prof["career_summary"] = career_summary(prof["career"])
        prof["sources"] = S.items
        prof["as_of"] = max((x.get("d") or "" for x in S.items), default="")   # newest dated source, not the run date
        # a file is worth writing when the crawler adds something Electo does not already hold (its own race archive)
        prof["has_new"] = bool(has_records or lg or prof["inference_inputs"] or money or
                               any(e["kind"] != "election" for e in prof["career"]))
        self.inputs[c["id"]] = prof["inference_inputs"]
        return prof

    def finish(self) -> None:
        # research order reads this (candidates Electo can't infer for yet go first); kept in the database
        self.store.kv_set("profile_inputs", json.dumps(self.inputs, separators=(",", ":")))
        self.store.flush()

    def bundle(self) -> dict:
        return {"graph": {"nodes": len(self.g.nodes), "edges": len(self.g.edges), "patterns": len(self.pats)},
                "_graph": self.g, "_patterns": self.pats}


def build_profiles(root: Path, store: Store, cfg: dict | None = None, candidates: list[dict] | None = None,
                   legis: dict | None = None, log=print) -> tuple[list[dict], dict]:
    """Returns (profiles, connections bundle)."""
    cands = candidates or load_candidates(root, cfg)
    pb = ProfileBuilder(root, store, cfg, cands, legis)
    out = [p for p in (pb.profile(c) for c in cands) if p]
    pb.finish()
    return out, pb.bundle()


def _keys(sources: list[dict]) -> list[str]:
    import re

    return [re.sub(r"^https?://(www\.)?", "", s.get("u") or "").rstrip("/").lower() for s in sources]


def write_profiles(profiles: list[dict], out_dir: Path, log=print, state_dir: Path | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    index, written = [], 0
    for p in profiles:
        name = slug(p.get("key") or p["candidate_id"])
        if p.get("has_new", True):
            (out_dir / f"{name}.json").write_text(json.dumps({k: v for k, v in p.items() if k != "has_new"}, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
            written += 1
        index.append({"key": p.get("key"), "candidate_id": p["candidate_id"], "name": p["name"],
                      "file": f"{name}.json" if p.get("has_new", True) else None,
                      "depth": p["depth"], "inference_inputs": p["inference_inputs"],
                      "counts": {k: len(p.get(k) or []) for k in ("positions", "statements", "ratings", "endorsement_items", "votes", "controversies",
                                                                  "bills", "affiliations", "career", "sources")},
                      "money": {"total_raised": (p.get("money") or {}).get("total_raised", 0),
                                "donors": len((p.get("money") or {}).get("donors") or [])}})
    index.sort(key=lambda x: (x["inference_inputs"], x["name"]))
    (out_dir / "index.json").write_text(json.dumps({
        "note": "Profile supplements in Electo's item shapes. inference_inputs counts what Electo's inference would see; "
                "profiles with fewer than 2 are listed first so research can be aimed at them. file is null when the "
                "crawler has nothing beyond Electo's own race archive for that person yet.",
        "profiles": index, "written": written, "thin": sum(1 for x in index if x["inference_inputs"] < 2)}, indent=1), encoding="utf-8")
    log(f"wrote {written} profile supplements ({len(index)} indexed) to {out_dir}")
    return written
