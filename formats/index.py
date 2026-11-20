"""The registry's index: formats/README.md (for people) and formats/registry.json (for code and tests), written from
the schemas themselves, so the index never says other than they do. Run after any change to a schema:

    python3 -I formats/index.py            # rewrite README.md and registry.json
    python3 -I formats/index.py --check    # exit 1 if they are not what the schemas give (the tests run this)

The same file is in Crawl, Electo and LogView (formats/index.py); only REPO below differs, and it is found from the
folder the repository is checked out in."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPOS = {
    "Crawl": "the crawler: what it stores (the database's records, kv keys), keeps between runs (state/), is set by (config/), "
             "commits beside its state (data/) and hands Electo (the export bundle, dist/electo)",
    "Electo": "the site: every data file the page, the app and the import read or write (ga/, data/, research/), and what "
              "the built site serves (qa/site/pipeline)",
    "LogView": "the logs: every run's row, its details, the issues filed and the README digests",
}
OTHERS = {"Crawl": "ClockworkBlack/Crawl formats/", "Electo": "ClockworkBlack/Electo formats/", "LogView": "ClockworkBlack/LogView formats/"}
AREAS = {"bundle": "Export bundle (Crawl -> Electo)", "records": "Database records, by kind (meta keys)", "store": "Database tables and kv keys",
         "state": "State files (kept between runs)", "config": "Config", "data": "Data files committed with the state",
         "site": "Site data files", "logview": "LogView files"}


def repo_name() -> str:
    for name in REPOS:
        if (ROOT / ".git").exists() and name.lower() in ROOT.name.lower():
            return name
    if (ROOT / "gacrawl").is_dir():
        return "Crawl"
    if (ROOT / "ga").is_dir():
        return "Electo"
    return "LogView"


def data_glob(title: str) -> str:
    t = re.sub(r"<[^>]+>|\bNN\b", "*", title.split(" ")[0])
    return t


def entries() -> list[dict]:
    out = []
    for p in sorted(HERE.rglob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        rel = p.relative_to(HERE).as_posix()
        out.append({"schema": rel, "area": rel.split("/")[0] if "/" in rel else "", "title": s.get("title") or rel,
                    "data": data_glob(s.get("title") or ""), "description": (s.get("description") or "")[:300],
                    "writer": (s.get("x-writer") or "")[:300], "readers": (s.get("x-readers") or "")[:300] if isinstance(s.get("x-readers"), str) else "; ".join(s.get("x-readers") or [])[:300],
                    "version": s.get("x-version") or "", "pinned": bool(s.get("x-pinned")), "strict": _strict(s),
                    "issues": len(s.get("x-issues") or []), "widened": len(s.get("x-widened") or [])})
    return out


def _strict(s) -> str:
    """closed: every object of the format lists its keys (a key not listed is a fault); open: some part is open."""
    open_parts = []

    def walk(n, path):
        if not isinstance(n, dict):
            return
        if n.get("properties") and n.get("additionalProperties", True) is not False:
            open_parts.append(path)
        for k, sub in (n.get("properties") or {}).items():
            walk(sub, f"{path}.{k}")
        for k in ("items", "additionalProperties"):
            if isinstance(n.get(k), dict):
                walk(n[k], path + ("[]" if k == "items" else "{}"))
    walk(s, "$")
    return "closed" if not open_parts else "open"


RULES = """## How a format is changed (every change, by anyone)

1. **Read the format first.** Before writing code that writes or reads a file, a record kind, a kv key or a config key,
   find it below and read its schema. If it is not listed, it is new: add its schema in the same change.
2. **Change the schema in the same commit as the code.** A key added, renamed, dropped or given another type or meaning
   is a change of the format: the schema says it, the writer writes it, and every reader listed reads it. A change
   that readers cannot take at once gets a new version (`format`/`v` in the file and `x-version` in the schema).
3. **Keys and values come from fixed lists.** Where the code holds the list (levels, kinds, statuses, reasons, classes),
   the schema's `enum` is that list and a test compares the two (`x-pinned`). A value that is not on the list is
   a fault, or a new value added to both in one change.
4. **Dates are ISO 8601**: a day `YYYY-MM-DD`, a moment `YYYY-MM-DDTHH:MM:SSZ` (UTC). A file says when it was made
   (`as_of`, a day, or `at`/`built`, a moment) under the name its schema gives. New files use `as_of` and `at` only.
5. **Rebuild this index** (`python3 -I formats/index.py`) and run the tests (`formats` tests check every schema
   loads, every schema here is listed, every file in this repository that has a schema fits it, and the pinned
   lists match the code).
6. **Real data that does not fit** is either a fault of the data (fixed at its writer, and left out at the reader's
   door with a counted reason: Electo's ledger `ga/crawl/left_out.json`) or a fault of the schema (fixed here, with a
   line in its `x-widened` or `description` saying what was learned). Never silence a fault by loosening a schema
   without saying why.

Schemas are JSON Schema (draft 2020-12), the subset `formats/check.py` reads; it needs only the standard library:

    python3 -I formats/check.py formats/<area>/<name>.schema.json path/to/data.json

Annotations: `x-writer`/`x-readers` (the code), `x-version`, `x-documented-in`, `x-observed` (what the schema was
measured on), `x-issues` (inconsistencies known when the registry was made, Nov 20, 2026), `x-widened` (what real
data showed that the first draft lacked), `x-values` (values seen, not a closed list), `x-pinned` (its lists are the
code's), `x-columns` (the column names of positional rows).
"""


def readme(es: list[dict], repo: str) -> str:
    n_closed = sum(1 for e in es if e["strict"] == "closed")
    lines = [f"# Formats: {repo}", "",
             f"Every schema and format {REPOS[repo]}. One JSON Schema per format; this page and `registry.json` are "
             f"written from them by `formats/index.py`. {len(es)} formats ({n_closed} closed: every key listed; "
             f"{sum(1 for e in es if e['pinned'])} pinned to the code's own lists).", "",
             "The other registries: " + "; ".join(f"{k}: `{v}`" for k, v in OTHERS.items() if k != repo) + ".", "", RULES]
    by = {}
    for e in es:
        by.setdefault(e["area"], []).append(e)
    for area, rows in sorted(by.items()):
        lines += ["", f"## {AREAS.get(area, area)}", "", "| Format | Schema | Version | Keys | Written by |", "| --- | --- | --- | --- | --- |"]
        for e in rows:
            w = re.sub(r"\s+", " ", e["writer"]).replace("|", "/")[:110]
            ver = str(e["version"]).replace("|", "/")[:20]
            flags = e["strict"] + (", pinned" if e["pinned"] else "") + (f", {e['issues']} known issue(s)" if e["issues"] else "")
            lines.append(f"| `{e['title'][:80]}` | [{Path(e['schema']).name}]({e['schema']}) | {ver} | {flags} | {w} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    check = "--check" in (sys.argv[1:] if argv is None else argv)
    repo = repo_name()
    es = entries()
    want = {HERE / "README.md": readme(es, repo), HERE / "registry.json": json.dumps({"repo": repo, "formats": es}, indent=1, ensure_ascii=False) + "\n"}
    stale = [p.name for p, t in want.items() if not p.exists() or p.read_text(encoding="utf-8") != t]
    if check:
        if stale:
            print("formats index is stale: " + ", ".join(stale) + " (run python3 -I formats/index.py)")
            return 1
        print(f"formats index current: {len(es)} formats")
        return 0
    for p, t in want.items():
        p.write_text(t, encoding="utf-8")
    print(f"formats: {len(es)} schemas; README.md and registry.json written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
