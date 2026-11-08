"""Ordinances downloaded by hand: imports/ordinances/ -> an Electo-ready set of local laws.

The owner downloads codes of ordinances, single ordinances, resolutions, zoning codes and charters from each
government's own site (or its code host) and drops the files in the private Crawl repository:

    imports/ordinances/<place>/<file>       <place> is a registry id (alpharetta, co-fulton) or the place's
                                            name (Alpharetta, Fulton County, City of Alpharetta)
    imports/ordinances/<id>__<file>          the top folder works too, with the id before "__"
    imports/ordinances/<file>                place worked out from the text when only one place is named
    imports/ordinances/**/sources.csv       optional: file,url,title,date,kind,note (where each file came from)

Files are read, never moved or changed: PDFs from their text layer (OCR only for scanned pages), Word (.docx),
saved web pages (.html/.htm), plain text (.txt/.md) and images (OCR). Each becomes one document split into
sections at its own headings (Sec. 2-14, ARTICLE IV, CHAPTER 5, ORDINANCE NO. ...).

`build()` writes, for Electo's data/ordinances/:
    index.json          every document: place, kind, title, source URL, date, pages, sections, review flags
    docs/<doc id>.json  one document's full text by section
    schema.md           what the fields mean
Documents are keyed by the file's SHA-256, so an unchanged file is not read again (the previous build is the cache)
and the same file uploaded twice is listed once.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import yaml

READER_VERSION = 1          # bump when reading or splitting changes, so every file is read again
DOC_EXT = {".pdf", ".docx", ".html", ".htm", ".txt", ".md"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
SKIP_NAMES = {"readme.md", "sources.csv", ".gitkeep"}
KINDS = ("code", "zoning", "charter", "ordinance", "resolution", "other")
SECTION_TARGET = 6000       # characters: text with no headings of its own is cut into parts about this long
CODE_HOSTS = re.compile(r"(^|\.)(library\.municode\.com|municode\.com|codelibrary\.amlegal\.com|ecode360\.com|"
                        r"codepublishing\.com|generalcode\.com|encodeplus\.com|enCodePlus\.com|"
                        r"online\.encodeplus\.com|codes\.iccsafe\.org)$", re.I)


# ----------------------------------------------------------------------------- places
def _norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"^(city|town|village|consolidated government) of\s+", "", s.strip())
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def load_places(root: Path) -> list[dict]:
    reg = yaml.safe_load((root / "config" / "jurisdictions.yaml").read_text(encoding="utf-8"))
    return [j for j in (reg.get("counties") or []) + (reg.get("municipalities") or []) if j.get("id")]


class Places:
    def __init__(self, places: list[dict]):
        self.places = places
        self.by_id = {j["id"]: j for j in places}
        self.by_key: dict[str, str] = {}
        for j in places:
            for k in {_norm(j["id"]), _norm(j["id"].replace("co-", "") + " county") if j["id"].startswith("co-") else "",
                      _norm(j["name"])}:
                if k:
                    self.by_key.setdefault(k, j["id"])

    def resolve(self, label: str) -> str | None:
        if not label:
            return None
        if label in self.by_id:
            return label
        k = _norm(label)
        return self.by_key.get(k) or self.by_key.get(_norm(k.replace(" ga", "").replace(" georgia", "")))

    def from_text(self, text: str) -> list[str]:
        from .screens import match_jurisdictions

        return match_jurisdictions(text[:40000], self.places)


# ----------------------------------------------------------------------------- reading files
def _docx_lines(data: bytes) -> list[str]:
    """Paragraph text of a .docx (the document body, in order; tables row by row)."""
    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out = []
    for p in etree.fromstring(xml).iter(w + "p"):
        t = "".join((n.text or "") if n.tag == w + "t" else ("\t" if n.tag == w + "tab" else "")
                    for n in p.iter(w + "t", w + "tab"))
        out.append(t.strip())
    return out


def _lines_md(lines: list[str]) -> tuple[str, str]:
    from .ingest import lines_to_markdown

    rows = [{"block": 0, "par": i, "text": l, "conf": 100.0, "top": 0} for i, l in enumerate(lines) if l.strip()]
    return lines_to_markdown(rows)


def read_file(path: Path, data: bytes, ocr_budget_s: float = 900.0) -> dict:
    """{text, markdown, pages, ocr_pages, mean_confidence, needs_review, title_hint} for one downloaded file."""
    ext = path.suffix.lower()
    out = {"pages": None, "ocr_pages": [], "mean_confidence": None, "needs_review": [], "title_hint": ""}
    if ext == ".pdf":
        from .ingest import pdf_to_record

        rec = pdf_to_record(data, ocr_max_pages=5000, ocr_budget_s=ocr_budget_s)
        out.update(text=rec.text, markdown=rec.markdown, pages=rec.meta.get("pages"),
                   ocr_pages=rec.meta.get("ocr_pages") or [], mean_confidence=rec.meta.get("mean_confidence"))
        if rec.meta.get("ocr_pending"):
            out["needs_review"].append(f"{len(rec.meta['ocr_pending'])} scanned pages not read yet (time limit); "
                                       "they are read on the next build")
            out["incomplete"] = True
    elif ext == ".docx":
        out["text"], out["markdown"] = _lines_md(_docx_lines(data))
    elif ext in (".html", ".htm"):
        from .extract import html_to_markdown

        html = data.decode("utf-8", "replace")
        title, md = html_to_markdown(html)
        out["title_hint"] = title or ""
        out["markdown"] = md
        out["text"] = re.sub(r"^#+\s*", "", md, flags=re.M)
    elif ext in (".txt", ".md"):
        out["text"], out["markdown"] = _lines_md(data.decode("utf-8", "replace").splitlines())
    elif ext in IMAGE_EXT:
        from .ingest import image_to_record

        rec = image_to_record(data)
        out.update(text=rec.text, markdown=rec.markdown, pages=1, ocr_pages=[1],
                   mean_confidence=rec.meta.get("mean_confidence"))
        out["needs_review"] += [f"low-confidence line: {l}" for l in (rec.meta.get("needs_review") or [])[:10]]
    else:
        raise ValueError(f"unsupported file type {ext}")
    if out["ocr_pages"]:
        out["needs_review"].insert(0, f"read by OCR ({len(out['ocr_pages'])} page{'s' if len(out['ocr_pages']) != 1 else ''}"
                                      f", mean confidence {out['mean_confidence']}): check quotes against the file")
    return out


# ----------------------------------------------------------------------------- sections and kinds
HEADING = re.compile(r"^(#{1,4})\s+(.+)$")


def sections(markdown: str) -> list[dict]:
    """[{h, t}] split at the document's own headings; long stretches with none are cut at paragraph breaks."""
    secs: list[dict] = []
    cur = {"h": "", "t": []}
    for block in re.split(r"\n{2,}", markdown or ""):
        block = block.strip()
        if not block or block == "---":
            continue
        m = HEADING.match(block)
        if m and len(m.group(2)) < 200:
            if cur["t"] or cur["h"]:
                secs.append(cur)
            cur = {"h": m.group(2).strip(), "t": []}
            continue
        cur["t"].append(block)
    if cur["t"] or cur["h"]:
        secs.append(cur)
    out = []
    for s in secs:
        body, h, part = [], s["h"], 1
        size = 0
        for b in s["t"]:
            if size and size + len(b) > SECTION_TARGET:
                out.append({"h": h if part == 1 else f"{h or 'Text'} (part {part})".strip(), "t": "\n\n".join(body)})
                body, size, part = [], 0, part + 1
            body.append(b)
            size += len(b) + 2
        if body or s["h"]:
            out.append({"h": h if part == 1 else f"{h or 'Text'} (part {part})".strip(), "t": "\n\n".join(body)})
    return out


def guess_kind(title: str, text: str, n_secs: int) -> str:
    head = (title + "\n" + text[:6000]).lower()
    if re.search(r"\bzoning (ordinance|code|resolution)\b|\bunified development (code|ordinance)\b|\bdevelopment code\b", head):
        return "zoning"
    if re.search(r"\bcode of ordinances\b|\bcode of (the )?(city|town|county|laws)\b|\bmunicipal code\b|\bcounty code\b", head) \
            or n_secs >= 40:
        return "code"
    if re.search(r"\bcharter\b", head[:1500]):
        return "charter"
    if re.search(r"\bordinance\s+(no\.?|number|#)\s*[\w\-]+|\ban ordinance (to|of|amending|adopting)\b", head):
        return "ordinance"
    if re.search(r"\bresolution\s+(no\.?|number|#)\s*[\w\-]+|\ba resolution (to|of|of the|authorizing|adopting)\b", head):
        return "resolution"
    return "other"


DATE_RX = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
                     r"(\d{1,2}),?\s+(19\d\d|20\d\d)\b")
MONTHS = {m: i for i, m in enumerate(("January February March April May June July August September October November "
                                      "December").split(), 1)}


def adopted_date(text: str) -> str:
    """The date near 'adopted', 'approved' or 'effective' in the text, when there is one (ISO), else ''."""
    for m in re.finditer(r"\b(adopted|approved|enacted|effective|passed)\b", text[:200000], re.I):
        d = DATE_RX.search(text, m.end(), m.end() + 160)
        if d:
            return f"{d.group(3)}-{MONTHS[d.group(1).title()]:02d}-{int(d.group(2)):02d}"
    return ""


def _title(info: dict, path: Path) -> str:
    if info.get("title_hint"):
        return info["title_hint"][:200]
    for line in (info.get("text") or "").splitlines():
        line = line.strip()
        if 12 <= len(line) <= 200 and re.search(r"[A-Za-z]{4}", line):
            return line
    return path.stem.replace("_", " ").replace("-", " ")[:200]


# ----------------------------------------------------------------------------- sources.csv
def load_sources(inbox: Path) -> dict[str, dict]:
    """Rows from every sources.csv, keyed by the file's path relative to the inbox (and by bare name as a fallback)."""
    out: dict[str, dict] = {}
    for f in sorted(inbox.rglob("sources.csv")):
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                name = row.get("file")
                if not name:
                    continue
                rel = (f.parent / name).relative_to(inbox).as_posix()
                out[rel] = row
                out.setdefault("name:" + Path(name).name.lower(), row)
    return out


def _official(url: str, j: dict | None) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if CODE_HOSTS.search(host) or host.endswith(".gov") or host.endswith(".ga.us"):
        return True
    site = (urlparse((j or {}).get("website") or "").hostname or "").lower().removeprefix("www.")
    return bool(site) and (host == site or host.endswith("." + site))


# ----------------------------------------------------------------------------- the build
def inbox_files(inbox: Path) -> list[Path]:
    return sorted(p for p in inbox.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(inbox).parts)
                  and p.name.lower() not in SKIP_NAMES and p.suffix.lower() in DOC_EXT | IMAGE_EXT)


def _load_prev(prev: Path | None) -> dict[str, dict]:
    """sha256 -> (index entry, its doc) from the previous build, when that build read files the same way."""
    if not prev or not (prev / "index.json").exists():
        return {}
    idx = json.loads((prev / "index.json").read_text(encoding="utf-8"))
    if idx.get("reader_version") != READER_VERSION:
        return {}
    out = {}
    for e in idx.get("documents", []):
        p = prev / "docs" / f"{e['id']}.json"
        if p.exists() and not e.get("incomplete"):
            out[e["sha256"]] = {"entry": e, "doc": json.loads(p.read_text(encoding="utf-8"))}
    return out


def build(root: Path, out: Path, prev: Path | None = None, log=print, ocr_budget_s: float = 900.0) -> dict:
    inbox = root / "imports" / "ordinances"
    inbox.mkdir(parents=True, exist_ok=True)
    places = Places(load_places(root))
    srcs = load_sources(inbox)
    cache = _load_prev(prev)
    files = inbox_files(inbox)
    docs_dir = out / "docs"
    if docs_dir.exists():
        for p in docs_dir.glob("*.json"):
            p.unlink()
    docs_dir.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    report = {"files": len(files), "documents": 0, "read": 0, "reused": 0, "duplicates": [], "unplaced": [],
              "errors": [], "unknown_folders": []}
    for f in files:
        rel = f.relative_to(inbox).as_posix()
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha in entries:
            entries[sha]["files"].append(rel)
            report["duplicates"].append(rel)
            continue
        src = srcs.get(rel) or srcs.get("name:" + f.name.lower()) or {}
        # the place: folder, then "<id>__" prefix, then sources.csv, then the text itself
        parts = f.relative_to(inbox).parts
        jid, how = None, ""
        if len(parts) > 1:
            jid, how = places.resolve(parts[0]), "folder"
            if not jid and parts[0] not in report["unknown_folders"]:
                report["unknown_folders"].append(parts[0])
        if not jid and "__" in f.name:
            jid, how = places.resolve(f.name.split("__", 1)[0]), "file name"
        if not jid and src.get("place"):
            jid, how = places.resolve(src["place"]), "sources.csv"
        if sha in cache:
            e, doc = dict(cache[sha]["entry"]), cache[sha]["doc"]
            report["reused"] += 1
        else:
            try:
                info = read_file(f, data, ocr_budget_s=ocr_budget_s)
            except Exception as exc:  # noqa: BLE001 — one unreadable file never stops the rest
                report["errors"].append(f"{rel}: {type(exc).__name__}: {exc}")
                log(f"{rel}: could not read ({type(exc).__name__}: {exc})")
                continue
            secs = sections(info["markdown"])
            title = _title(info, f)
            e = {"sha256": sha, "title": title, "kind": guess_kind(title, info["text"], len(secs)),
                 "adopted": adopted_date(info["text"]), "pages": info["pages"], "chars": len(info["text"]),
                 "sections": len(secs), "ocr_pages": len(info["ocr_pages"]), "mean_confidence": info["mean_confidence"],
                 "needs_review": info["needs_review"], "file_type": f.suffix.lower().lstrip(".")}
            if info.get("incomplete"):
                e["incomplete"] = True
            doc = {"sections": secs, "text_places": places.from_text(info["text"])}
            report["read"] += 1
        if not jid:
            tp = doc.get("text_places") or []
            if len(tp) == 1:
                jid, how = tp[0], "text"
        j = places.by_id.get(jid) if jid else None
        # what the owner wrote in sources.csv wins over what was read
        url = src.get("url", "")
        e.update({"id": f"{jid or 'unplaced'}-{sha[:12]}", "jurisdiction": jid or "", "place": j["name"] if j else "",
                  "place_kind": j.get("kind") if j else "", "counties": (j.get("counties") or ([j["name"]] if j and j.get("kind") == "county" else [])) if j else [],
                  "place_from": how if jid else "", "files": [rel], "url": url,
                  "url_official": _official(url, j) if url else False, "note": src.get("note", ""),
                  "source": "downloaded by the site owner from the government's own site or its code host"})
        if src.get("title"):
            e["title"] = src["title"][:200]
        if src.get("date"):
            e["adopted"] = src["date"][:10]
        if (src.get("kind") or "").lower() in KINDS:
            e["kind"] = src["kind"].lower()
        if not jid:
            report["unplaced"].append(rel)
        entries[sha] = e
        (docs_dir / f"{e['id']}.json").write_text(json.dumps({
            "id": e["id"], "jurisdiction": e["jurisdiction"], "place": e["place"], "title": e["title"], "kind": e["kind"],
            "adopted": e["adopted"], "url": e["url"], "files": e["files"], "sections": doc["sections"],
            "text_places": doc.get("text_places", [])}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(f"{rel}: {e['place'] or 'place unknown'} · {e['kind']} · {e['sections']} sections"
            + (" (reused)" if sha in cache else ""))
    docs = sorted(entries.values(), key=lambda e: (e["place"] or "~", e["kind"], e["title"]))
    # a duplicate's extra file names were added after its doc was written
    for e in docs:
        if len(e["files"]) > 1:
            p = docs_dir / f"{e['id']}.json"
            d = json.loads(p.read_text(encoding="utf-8"))
            d["files"] = e["files"]
            p.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    by_place: dict[str, dict] = {}
    for e in docs:
        if e["jurisdiction"]:
            b = by_place.setdefault(e["jurisdiction"], {"place": e["place"], "kinds": {}, "documents": 0})
            b["documents"] += 1
            b["kinds"][e["kind"]] = b["kinds"].get(e["kind"], 0) + 1
    report["documents"] = len(docs)
    idx = {"note": "Local laws (codes of ordinances, zoning codes, charters, single ordinances and resolutions) the "
                   "site owner downloaded by hand from each government's own site or its code host, read into sections. "
                   "Full text in docs/<id>.json. See schema.md.",
           "reader_version": READER_VERSION, "places": dict(sorted(by_place.items())), "documents": docs,
           "unplaced": report["unplaced"]}
    # only content changes the files (no build time), so an unchanged inbox makes no new commit in Electo
    (out / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "schema.md").write_text(SCHEMA, encoding="utf-8")
    return report


SCHEMA = """# Downloaded local laws (from Crawl's imports/ordinances/)

Written by `gacrawl ordinances build` in the private Crawl repository whenever a file is added there, and pushed here
(`data/ordinances/`). Nothing in this folder is edited by hand: change the files or `sources.csv` in Crawl instead.

## index.json
- `places`: registry id -> `{place, documents, kinds: {kind: n}}`
- `documents[]`: one per distinct file (same bytes uploaded twice = one document, both names in `files`)
  - `id`: `<place id>-<first 12 of sha256>` (`unplaced-...` when no place could be worked out); the doc is `docs/<id>.json`
  - `jurisdiction`, `place`, `place_kind` (city/county/...), `counties`: the government, from Crawl's registry
  - `place_from`: how the place was known: `folder`, `file name`, `sources.csv` or `text` (the only place named in it)
  - `kind`: `code` (code of ordinances), `zoning`, `charter`, `ordinance`, `resolution`, `other`
  - `title`, `adopted` (ISO date when the text or sources.csv gives one, else empty)
  - `url`: where the owner downloaded it (sources.csv), `url_official`: the URL is on the government's own site,
    a .gov / .ga.us host or a known code host (Municode, American Legal, eCode360, Code Publishing, enCodePlus)
  - `pages`, `chars`, `sections`, `file_type`, `files` (paths under imports/ordinances/), `sha256`
  - `ocr_pages`, `mean_confidence`, `needs_review`: text read by OCR must be checked against the file before quoting;
    `incomplete`: some scanned pages were not read yet (they are on the next build)
  - `source`: always "downloaded by the site owner ..."; these are primary documents the owner opened
- `unplaced`: files whose government could not be worked out (put them in a folder named for the place)

## docs/<id>.json
`{id, jurisdiction, place, title, kind, adopted, url, files, sections: [{h, t}], text_places}`: `h` is the section
heading as printed (Sec. 2-14 ..., ARTICLE IV ...; a long stretch without headings is split into "(part n)"),
`t` its text with paragraphs separated by blank lines. `text_places` are the registry ids the text names.
"""
