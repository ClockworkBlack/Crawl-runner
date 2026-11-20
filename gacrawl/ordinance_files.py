"""Ordinances downloaded by hand: imports/ordinances/ -> an Electo-ready set of local laws.

The owner downloads codes of ordinances, single ordinances, resolutions, zoning codes and charters from each
government's own site (or its code host) and drops the files in the private Crawl repository:

    imports/ordinances/<place>/<file>       <place> is a registry id (alpharetta, co-fulton) or the place's
                                            name (Alpharetta, Fulton County, City of Alpharetta)
    imports/ordinances/<id>__<file>          the top folder works too, with the id before "__"
    imports/ordinances/<file>                place worked out from the text when only one place is named
    imports/ordinances/**/sources.csv       optional: file,url,title,date,kind,note (where each file came from)
    imports/ordinances/GA/<county>/code of ordinances/{code.html, toc.json, manifest.json, nodes/}
                                            a downloader's output, dropped as is (the whole GA folder, or a .zip
                                            of it): every folder holding manifest.json or toc.json with code.html
                                            or nodes/ is ONE code, and the place is the first folder on its path
                                            that names a Georgia government (a bare name like "Fulton" is the county)

Files are read, never moved or changed: PDFs from their text layer (OCR only for scanned pages), Word (.docx),
saved web pages (.html/.htm), plain text (.txt/.md) and images (OCR). Each becomes one document split into
sections at its own headings (Sec. 2-14, ARTICLE IV, CHAPTER 5, ORDINANCE NO. ...).

`build()` writes, for Electo's data/ordinances/:
    index.json          every document: place, kind, title, source URL, date, pages, sections, review flags
    docs/<doc id>.json  one document's full text by section
    schema.md           what the fields mean
Documents are keyed by the file's SHA-256, so an unchanged file is not read again (the previous build is the cache)
and the same file uploaded twice is listed once.

A build can have a time limit (`max_minutes`). Scanned files take minutes each to OCR, and a workflow run killed at
its own limit writes and sends nothing, so the next run read the same files again (Nov 10-15, 2026: every run was
cut off and Electo got none of the new downloads). With a limit, the build starts no new file once the time is up,
keeps every document the previous build had for the files it did not get to, and writes index.json with those files
listed under `pending`; the next build starts from what this one wrote. Very large files (mostly scans) and scans
read only partly before are read after everything else, so a few big scans cannot crowd out the small files.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path

import yaml

READER_VERSION = 4          # bump when reading or splitting changes, so every file is read again
LARGE_FILE = 15_000_000     # bytes: files bigger than this (nearly always scans, minutes of OCR each) are read last
RETRY_GAIN_S = 60.0         # a scan read only partly is read again only when the new read can give it at least this
                            # much more OCR time than the last one had (the same time would read the same pages)
DOC_EXT = {".pdf", ".docx", ".html", ".htm", ".txt", ".md"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
SKIP_NAMES = {"readme.md", "sources.csv", ".gitkeep"}
SPLIT_PART = re.compile(r"^(.+)\.part(\d+)of(\d+)$", re.I)              # big.pdf.part01of03: one piece of a split upload
DROP_REPORT = re.compile(r"^report \d{4}-\d{2}-\d{2} \d{4}\.txt$", re.I)  # the downloader's own report, not a document
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


# governments with a code of ordinances that are not counties or municipalities (state authorities)
EXTRA_PLACES = [{"id": "jekyll-island-authority", "name": "Jekyll Island Authority", "kind": "authority",
                 "counties": ["Glynn"], "website": "https://www.jekyllisland.com/authority/"}]


def load_places(root: Path) -> list[dict]:
    reg = yaml.safe_load((root / "config" / "jurisdictions.yaml").read_text(encoding="utf-8"))
    return [j for j in (reg.get("counties") or []) + (reg.get("municipalities") or []) if j.get("id")] + EXTRA_PLACES


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

    def resolve_folder(self, label: str, hint: str = "") -> tuple[str | None, bool]:
        """A folder name -> (registry id, ambiguous?). Folders under GA/ are counties, so a bare name that is both a
        city and a county ("Jackson", "Monroe") is the county unless the code's own text says "City of <name>";
        Municode-style slugs (fulton_county, liberty_county_, athens-clarke_county) work too."""
        if not label or _norm(label) in NOISE_DIRS:
            return None, False
        if label in self.by_id:
            return label, False
        k = _norm(label)
        # "Unified Government of Georgetown-Quitman County Commission", "Consolidated Government of Columbus"
        k2 = re.sub(r"^(unified|consolidated) government of ", "", k)
        k2 = re.sub(r" (board of commissioners|county commission|commission|board of aldermen)$", "", k2).strip()
        if k2 != k:
            if k2 in self.by_key:
                return self.by_key[k2], False
            k = k2
        if k.endswith(" county"):
            return self.by_key.get(k) or self.by_key.get(k[: -len(" county")]), False
        county, other = self.by_key.get(k + " county"), self.by_key.get(k)
        if county and other and county != other:
            name = re.escape(self.by_id[other]["name"])
            if hint and re.search(rf"\b(city|town|village) of {name}\b", hint, re.I) \
                    and not re.search(rf"\b{name} County\b", hint, re.I):
                return other, True
            return county, True
        return county or other, False

    def from_text(self, text: str) -> list[str]:
        from .screens import match_jurisdictions

        return match_jurisdictions(text[:40000], self.places)


NOISE_DIRS = {"out", "ga", "georgia", "code of ordinances", "codes of ordinances", "code", "codes", "ordinances",
              "nodes", "downloads", "download", "municode", "files", "data", "output"}


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


def read_file(path: Path, data: bytes, ocr_budget_s: float = 900.0, stop_at: float | None = None) -> dict:
    """{text, markdown, pages, ocr_pages, mean_confidence, needs_review, title_hint} for one downloaded file.
    OCR stops after `ocr_budget_s` for this file, or at `stop_at` (time.monotonic()), the build's own time limit."""
    ext = path.suffix.lower()
    out = {"pages": None, "ocr_pages": [], "mean_confidence": None, "needs_review": [], "title_hint": ""}
    if ext == ".pdf":
        from .ingest import pdf_to_record

        rec = pdf_to_record(data, ocr_max_pages=5000, ocr_budget_s=ocr_budget_s, stop_at=stop_at)
        out.update(text=rec.text, markdown=rec.markdown, pages=rec.meta.get("pages"),
                   ocr_pages=rec.meta.get("ocr_pages") or [], mean_confidence=rec.meta.get("mean_confidence"))
        if rec.meta.get("ocr_pending"):
            out["needs_review"].append(f"{len(rec.meta['ocr_pending'])} scanned pages not read yet (time limit); "
                                       "they are read when a build can give the file more OCR time")
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


BUNDLE_MARKERS = ("manifest.json", "toc.json")


def is_bundle(d: Path) -> bool:
    """A downloader's saved code: manifest.json or toc.json beside code.html or a nodes/ folder."""
    names = {p.name.lower(): p for p in d.iterdir()} if d.is_dir() else {}
    return any(m in names for m in BUNDLE_MARKERS) and ("code.html" in names or ("nodes" in names and names["nodes"].is_dir()))


def _strings(obj, keys=None, depth=0) -> list[str]:
    """String values in a JSON value (only under `keys` when given), in document order."""
    out = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                if keys is None or k.lower() in keys:
                    out.append(v)
            else:
                out += _strings(v, keys, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            out += _strings(v, keys, depth + 1) if not isinstance(v, str) else ([v] if keys is None else [])
    return out


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def _html_md(html: str) -> tuple[str, str]:
    """(title, markdown) of a whole saved page: every part of the body, not only its biggest block."""
    from bs4 import BeautifulSoup

    from .extract import html_to_markdown

    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body = soup.body or soup
    for t in body.find_all(["script", "style", "noscript"]):
        t.decompose()
    _, md = html_to_markdown("<html><body><main>" + body.decode_contents() + "</main></body></html>")
    return title, md


TEXT_KEYS = {"html", "content", "text", "body", "content_html", "contenthtml", "markdown"}
HEAD_KEYS = {"title", "heading", "header", "label", "name", "number"}


def _natural(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.as_posix())]


def read_bundle(d: Path) -> dict:
    """One saved code: code.html when present (the whole code in one page), else the nodes/ files in order."""
    names = {p.name.lower(): p for p in d.iterdir()}
    manifest = _json(names["manifest.json"]) if "manifest.json" in names else None
    toc = _json(names["toc.json"]) if "toc.json" in names else None
    meta_txt = " ".join(_strings(manifest)) if manifest is not None else ""
    toc_titles = _strings(toc, HEAD_KEYS)[:3] if toc is not None else []
    title, md = "", ""
    if "code.html" in names:
        title, md = _html_md(names["code.html"].read_text(encoding="utf-8", errors="replace"))
    if len(md) < 500 and "nodes" in names:
        parts = []
        for f in sorted((p for p in names["nodes"].rglob("*") if p.is_file()), key=_natural):
            ext = f.suffix.lower()
            if ext in (".html", ".htm"):
                parts.append(_html_md(f.read_text(encoding="utf-8", errors="replace"))[1])
            elif ext == ".json":
                j = _json(f)
                if j is None:
                    continue
                heads, texts = _strings(j, HEAD_KEYS), _strings(j, TEXT_KEYS)
                block = ("## " + heads[0] + "\n\n" if heads else "")
                for t in texts:
                    block += (_html_md(t)[1] if "<" in t and ">" in t else t) + "\n\n"
                parts.append(block.strip())
            elif ext in (".txt", ".md"):
                parts.append(f.read_text(encoding="utf-8", errors="replace"))
        nodes_md = "\n\n".join(x for x in parts if x.strip())
        if len(nodes_md) > len(md):
            md = nodes_md
    text = re.sub(r"^#+\s*", "", md, flags=re.M)
    urls = [u for u in re.findall(r"https?://[^\s\"'<>]+", meta_txt)]
    return {"text": text, "markdown": md, "pages": None, "ocr_pages": [], "mean_confidence": None, "needs_review": [],
            "title_hint": title or (toc_titles[0] if toc_titles and len(toc_titles[0]) > 8 else ""),
            "meta_text": meta_txt + " " + title + " " + " ".join(toc_titles), "url": urls[0] if urls else ""}


# ----------------------------------------------------------------------------- Municode exports
# The downloader's folders are Municode Library exports: toc.json is the code's whole outline (Id, Heading, NodeDepth,
# Children), nodes/*.json hold the text of each "chunk group" (Docs[]: Id, Title, Content html) and code.html repeats
# the same text under <h2 id="NODE ID">. A section whose chunk group did not download has no text anywhere; it is kept
# in the outline (with its Municode link) so the reader can still open it on Municode.
MUNI = "https://library.municode.com/ga/{client}/codes/{product}"
INLINE_TAGS = {"b", "i", "u", "em", "strong", "span", "a", "sup", "sub", "small", "font", "abbr", "cite", "code", "s",
               "strike", "mark", "q", "var"}
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dt", "dd"}


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def html_text(frag: str) -> str:
    """A Municode content fragment -> plain text: one paragraph per line, "(a)" markers joined to their text,
    table rows as cells separated by " | ", images and scripts dropped."""
    from lxml import html as LH

    if not frag or not frag.strip():
        return ""
    try:
        root = LH.fragment_fromstring(frag, create_parent="div")
    except Exception:  # noqa: BLE001
        return _ws(re.sub(r"<[^>]+>", " ", frag))
    for bad in root.xpath("//script|//style|//img"):
        bad.drop_tree()
    lines: list[str] = []
    pending = ""

    def push(t: str, cls: str = ""):
        nonlocal pending
        t = _ws(t)
        if not t:
            return
        if cls.startswith("incr") and len(t) <= 12:      # "(a)", "1.", "(iv)": the marker of the next paragraph
            pending = (pending + " " + t).strip()
            return
        lines.append((pending + " " + t).strip() if pending else t)
        pending = ""

    def walk(el):
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "table":
            for tr in el.iter("tr"):
                cells = [_ws(td.text_content()) for td in tr if isinstance(td.tag, str) and td.tag in ("td", "th")]
                if any(cells):
                    push(" | ".join(cells))
            return
        if tag in BLOCK_TAGS:
            push(el.text_content(), el.get("class") or "")
            return
        # a container: runs of text and inline elements are one line ("Footnotes: --- (<b>1</b>) ---")
        buf = [el.text or ""]
        for c in el:
            ct = c.tag if isinstance(c.tag, str) else ""
            if ct in INLINE_TAGS:
                buf += [c.text_content(), c.tail or ""]
            else:
                push("".join(buf))
                if ct != "br":
                    walk(c)
                buf = [c.tail or ""]
        push("".join(buf))

    walk(root)
    if pending:
        lines.append(pending)
    # Municode's footnote marker "Footnotes: --- (1) ---" -> "Footnote 1:"
    lines = [re.sub(r"^(?:Footnotes:\s*)?-{2,}\s*\(\s*(\d+)\s*\)\s*-{2,}$", r"Footnote \1:", l) for l in lines]
    return "\n".join(l for l in lines if l != "Footnotes:")


def is_municode(d: Path) -> bool:
    t = d / "toc.json"
    if not t.exists():
        return False
    try:
        with open(t, encoding="utf-8") as fh:
            head = fh.read(400)
    except OSError:
        return False
    return '"Heading"' in head and '"Id"' in head


def read_municode(d: Path) -> dict:
    """toc.json + nodes/ (or code.html) -> the code as an outline with text per node."""
    toc = json.loads((d / "toc.json").read_text(encoding="utf-8-sig"))
    manifest = _json(d / "manifest.json") if (d / "manifest.json").exists() else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    text: dict[str, str] = {}
    nodes_dir = d / "nodes"
    if nodes_dir.is_dir():
        for f in sorted(nodes_dir.glob("*.json")):
            j = _json(f)
            for doc in (j.get("Docs") if isinstance(j, dict) else None) or []:
                nid, c = doc.get("Id"), doc.get("Content") or ""
                if nid and c and nid not in text:
                    t = html_text(c)
                    if t:
                        text[nid] = t
    if (d / "code.html").exists():            # anything the nodes lacked
        h = (d / "code.html").read_text(encoding="utf-8", errors="replace")
        parts = re.split(r'<h2 id="([^"]+)">', h)
        for i in range(1, len(parts), 2):
            nid = parts[i]
            if nid in text:
                continue
            body = parts[i + 1].split("</h2>", 1)[-1]
            if "chunk-content" in body:
                t = html_text(body)
                if t:
                    text[nid] = t
    nodes: list[list] = []                    # [id, heading, depth, parent index, text]

    def walk(n, parent, depth):
        for c in n.get("Children") or []:
            idx = len(nodes)
            nodes.append([c.get("Id") or "", _ws(c.get("Heading") or ""), depth, parent, text.get(c.get("Id"), "")])
            walk(c, idx, depth + 1)

    walk(toc, -1, 1)
    has_kids = {n[3] for n in nodes}
    leaves = [i for i in range(len(nodes)) if i not in has_kids]
    with_text = sum(1 for i in leaves if nodes[i][4])
    client = manifest.get("client") or ""
    product = manifest.get("product_name") or toc.get("Heading") or ""
    return {"nodes": nodes, "client": client, "product": product,
            "downloaded": (manifest.get("downloaded_at") or "")[:10], "product_id": manifest.get("product_id"),
            "job_id": manifest.get("job_id"), "leaves": len(leaves), "leaves_with_text": with_text,
            "text": "\n".join(n[1] + "\n" + n[4] for n in nodes if n[4]),
            "meta_text": " ".join(str(v) for v in manifest.values()) + " " + (toc.get("Heading") or "")}


def municode_url(rel: str) -> str:
    """The code's Municode Library address from the export's folder names (GA/<client slug>/<product slug>)."""
    parts = [x for x in rel.split("/") if x]
    if len(parts) >= 2 and re.fullmatch(r"[a-z0-9_\-.]+", parts[-1]) and re.fullmatch(r"[a-z0-9_\-.]+", parts[-2]):
        return MUNI.format(client=parts[-2], product=parts[-1])
    return ""


def kind_of_product(product: str) -> str:
    p = product.lower()
    if re.search(r"zoning|development|land use|subdivision|ldc|udc|planning", p):
        return "zoning"
    if "charter" in p:
        return "charter"
    return "code"


def _municode_entry(f: Path, rel: str, sha: str, places: "Places") -> tuple[dict, dict]:
    r = read_municode(f)
    url = municode_url(rel)
    title = (r["client"] + " " + r["product"]).strip() or f.name
    e = {"sha256": sha, "title": title, "kind": kind_of_product(r["product"]), "product": r["product"],
         "client": r["client"], "downloaded": r["downloaded"], "adopted": "", "pages": None, "chars": len(r["text"]),
         "sections": sum(1 for n in r["nodes"] if n[4]), "nodes": len(r["nodes"]), "leaves": r["leaves"],
         "leaves_with_text": r["leaves_with_text"],
         "coverage": round(r["leaves_with_text"] / r["leaves"], 3) if r["leaves"] else 0.0,
         "ocr_pages": 0, "mean_confidence": None, "needs_review": [], "structured": True,
         "file_type": "Municode export (toc.json / nodes / code.html)"}
    if e["coverage"] < 1:
        e["needs_review"].append(f"the download has text for {r['leaves_with_text']} of {r['leaves']} sections; "
                                 "the rest are headings only (open them on Municode or download the code again)")
    doc = {"nodes": r["nodes"], "sections": [], "text_places": places.from_text(r["meta_text"] + " " + r["text"][:20000]),
           "hint": (r["meta_text"] + " " + r["text"][:3000])[:4000], "found_url": url}
    return e, doc


def bundle_digest(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted((p for p in d.rglob("*") if p.is_file()), key=lambda p: p.relative_to(d).as_posix()):
        h.update(f.relative_to(d).as_posix().encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


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
        return re.sub(r"\s*\|\s*Municode Library\s*$", "", info["title_hint"]).strip()[:200]
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
def _unzip(z: Path, dest: Path) -> list[str]:
    """Extract a dropped .zip (paths inside it kept; nothing outside dest). Returns problems found."""
    bad = []
    with zipfile.ZipFile(z) as zf:
        for m in zf.infolist():
            name = m.filename.replace("\\", "/")
            if m.is_dir() or name.startswith("__MACOSX/") or Path(name).name.startswith("._"):
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest.resolve()) + "/"):
                bad.append(f"{z.name}: skipped {name} (path outside the zip)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(target, "wb") as fh:
                fh.write(src.read())
    return bad


def inbox_units(inbox: Path, scratch: Path | None = None) -> tuple[list[tuple[str, Path, str]], list[str]]:
    """(kind, path, label) for everything in the inbox: ("bundle", folder, label) for a downloader's saved code,
    ("file", file, label) for a loose document. Zips are unpacked into `scratch` and walked the same way, labelled
    "<zip path>/<path inside>". Hidden folders and the inbox's own README/sources.csv are skipped."""
    import os
    import tempfile

    roots, problems = [(inbox, "")], []
    zips = sorted(p for p in inbox.rglob("*.zip") if p.is_file() and not any(x.startswith(".") for x in p.relative_to(inbox).parts))
    if zips:
        scratch = scratch or Path(tempfile.mkdtemp(prefix="ordzip-"))
        for i, z in enumerate(zips):
            dest = scratch / f"z{i}"
            dest.mkdir(parents=True, exist_ok=True)
            try:
                problems += _unzip(z, dest)
            except zipfile.BadZipFile:
                problems.append(f"{z.relative_to(inbox).as_posix()}: not a readable zip")
                continue
            roots.append((dest, z.relative_to(inbox).as_posix()))
    units = []
    for base, prefix in roots:
        for d, dirs, files in os.walk(base):
            dp = Path(d)
            rel = dp.relative_to(base).as_posix()
            if rel != "." and any(x.startswith(".") for x in dp.relative_to(base).parts):
                dirs[:] = []
                continue
            label = "/".join(x for x in (prefix, "" if rel == "." else rel) if x)
            if rel != "." and is_bundle(dp):
                units.append(("bundle", dp, label))
                dirs[:] = []
                continue
            dirs.sort()
            for f in sorted(files):
                fp = dp / f
                if f.lower() in SKIP_NAMES or DROP_REPORT.match(f) or f.startswith(".") or fp.suffix.lower() not in DOC_EXT | IMAGE_EXT:
                    continue
                units.append(("file", fp, "/".join(x for x in (label, f) if x)))
    # A file too big for a web upload arrives split, as <name>.part01of03 ... <name>.part03of03: join each complete
    # set into one file (in scratch) and read it under <folder>/<name>, as if it had been uploaded whole.
    sets: dict[tuple[Path, str], dict] = {}
    for base, prefix in roots:
        for fp in base.rglob("*"):
            m = SPLIT_PART.match(fp.name)
            if not m or not fp.is_file() or any(x.startswith(".") for x in fp.relative_to(base).parts):
                continue
            s = sets.setdefault((fp.parent, m.group(1)), {"of": int(m.group(3)), "parts": {}, "base": base, "prefix": prefix})
            s["parts"][int(m.group(2))] = fp
    for i, ((folder, name), s) in enumerate(sorted(sets.items(), key=lambda kv: str(kv[0]))):
        rel = folder.relative_to(s["base"]).as_posix()
        label = "/".join(x for x in (s["prefix"], "" if rel == "." else rel, name) if x)
        if sorted(s["parts"]) != list(range(1, s["of"] + 1)):
            have = ", ".join(str(n) for n in sorted(s["parts"]))
            problems.append(f"{label}: split into {s['of']} parts but only part(s) {have} are here; not read")
            continue
        if Path(name).suffix.lower() not in DOC_EXT | IMAGE_EXT:
            continue
        scratch = scratch or Path(tempfile.mkdtemp(prefix="ordzip-"))
        out = scratch / f"j{i}" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as fh:
            for n in range(1, s["of"] + 1):
                with open(s["parts"][n], "rb") as part:
                    while chunk := part.read(1 << 20):
                        fh.write(chunk)
        units.append(("file", out, label))
    return units, problems


def inbox_files(inbox: Path) -> list[Path]:
    return [p for k, p, _ in inbox_units(inbox)[0] if k == "file"]


def place_of(places: "Places", label: str, is_dir: bool, hint: str = "") -> tuple[str | None, str, list[str]]:
    """(id, how, folders that named nothing) from a unit's path: the first folder (top down) that names a place."""
    parts = [x for x in label.split("/") if x]
    if not is_dir:
        parts = parts[:-1]
    parts = [x[:-4] if x.lower().endswith(".zip") else x for x in parts]
    unknown = []
    for x in parts:
        jid, amb = places.resolve_folder(x, hint)
        if jid:
            return jid, "folder" + (" (city and county share the name: county unless the text says city)" if amb else ""), []
        if _norm(x) not in NOISE_DIRS and not re.search(r"\bcode|ordinance|zoning|charter|resolution", x, re.I):
            unknown.append(x)
    return None, "", unknown


def _clock() -> float:
    """The build's clock (time.monotonic; the tests replace it to run a time limit without waiting)."""
    return time.monotonic()


def _ocr_engine() -> bool:
    """Is the OCR engine installed? Without it no scanned page is read however long a build allows, so a scan read
    without it is read again whatever time it had."""
    import importlib.util
    import shutil

    return importlib.util.find_spec("pytesseract") is not None and shutil.which("tesseract") is not None


def _load_prev(prev: Path | None, ocr_budget_s: float = 900.0) -> dict[str, dict]:
    """sha256 -> {entry, doc, reuse, retry} for every document of the previous build.
    reuse: it stands as it is: read by this reader, and whole (or a scan that already had this build's OCR time per
    file, which would only read the same pages again). retry: a scan of this reader's that was cut short (the run's
    time ran out mid-file, or no OCR engine), read again when there is more time. Neither: an older reader's document,
    read again like a new file. The last two are kept in the new build until they are read again."""
    if not prev or not (prev / "index.json").exists():
        return {}
    idx = json.loads((prev / "index.json").read_text(encoding="utf-8"))
    out = {}
    for e in idx.get("documents", []):
        p = prev / "docs" / f"{e['id']}.json"
        if not p.exists():
            continue
        # a document kept from an older reader carries its own version, so the index's version does not vouch for it
        version = e.get("reader_version", idx.get("reader_version"))
        current = version == READER_VERSION
        if not current:
            e = {**e, "reader_version": version}
        reuse = current and (not e.get("incomplete") or (e.get("ocr_allowed_s") or 0) + RETRY_GAIN_S >= ocr_budget_s)
        out[e["sha256"]] = {"entry": e, "doc": json.loads(p.read_text(encoding="utf-8")), "reuse": reuse,
                            "retry": current and not reuse}
    return out


def build(root: Path, out: Path, prev: Path | None = None, log=print, ocr_budget_s: float = 900.0,
          max_minutes: float | None = None) -> dict:
    """Read the inbox into `out` (see the module notes). `prev`: an earlier build, whose documents are reused for
    unchanged files. `max_minutes`: no new file is started after this long; the files not read are in the report's
    `pending` (and index.json's), with the previous build's documents for them kept."""
    import shutil
    import tempfile

    deadline = _clock() + max_minutes * 60 if max_minutes is not None else None
    inbox = root / "imports" / "ordinances"
    inbox.mkdir(parents=True, exist_ok=True)
    places = Places(load_places(root))
    srcs = load_sources(inbox)
    prev_docs = _load_prev(prev, ocr_budget_s)
    scratch = Path(tempfile.mkdtemp(prefix="ordzip-"))
    try:
        units, problems = inbox_units(inbox, scratch)
        return _build(units, problems, places, srcs, prev_docs, out, log, ocr_budget_s, deadline)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _queue_rank(unit: dict, p: dict | None) -> int:
    """The order files are read in: documents reused as they are (no reading), then new or changed files, then very
    large files, then scans read only partly before, each group in inbox order. A run with a time limit then spends
    it on the files Electo has nothing for, and big scans cannot hold up the small files behind them."""
    if p and p["reuse"]:
        return 0
    if p and p["retry"]:
        return 3
    return 2 if unit["size"] > LARGE_FILE else 1


def _build(units, problems, places, srcs, prev_docs, out: Path, log, ocr_budget_s: float,
           deadline: float | None = None) -> dict:
    docs_dir = out / "docs"
    if docs_dir.exists():
        for p in docs_dir.glob("*.json"):
            p.unlink()
    docs_dir.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    report = {"files": sum(1 for k, *_ in units if k == "file"), "saved_codes": sum(1 for k, *_ in units if k == "bundle"),
              "documents": 0, "read": 0, "reused": 0, "finished": 0, "pending": [], "duplicates": [], "unplaced": [],
              "errors": list(problems), "unknown_folders": [], "ambiguous": [], "incomplete_downloads": []}
    # every unit's digest first, in inbox order (the first file with given bytes is the document, later ones are its
    # duplicates), so the reading order can put what needs no reading first and the big scans last
    groups: dict[str, dict] = {}
    for kind, f, rel in units:
        bundle = kind == "bundle"
        try:
            sha = bundle_digest(f) if bundle else hashlib.sha256(f.read_bytes()).hexdigest()
            size = 0 if bundle else f.stat().st_size
        except OSError as exc:
            report["errors"].append(f"{rel}: {exc}")
            continue
        if sha in groups:
            groups[sha]["files"].append(rel)
            report["duplicates"].append(rel)
            continue
        groups[sha] = {"kind": kind, "path": f, "rel": rel, "files": [rel], "size": size}
    queue = sorted(groups.items(), key=lambda kv: _queue_rank(kv[1], prev_docs.get(kv[0])))
    # what index.json lists comes out in inbox order whatever the reading order, so the same documents always make
    # the same file (no new commit in Electo for a different order)
    pos = {rel: i for i, (_, _, rel) in enumerate(units)}
    engine = _ocr_engine()
    out_of_time = False
    for sha, unit in queue:
        kind, f, rel = unit["kind"], unit["path"], unit["rel"]
        bundle = kind == "bundle"
        src = srcs.get(rel) or srcs.get("name:" + f.name.lower()) or {}
        p = prev_docs.get(sha)
        note = ""
        if p and p["reuse"]:
            e, doc = dict(p["entry"]), p["doc"]
            report["reused"] += 1
            note = " (reused)"
        else:
            left = None if deadline is None else deadline - _clock()
            allowed = ocr_budget_s if left is None else max(0.0, min(ocr_budget_s, left))
            if (left is not None and left <= 0) or (p and p["retry"]
                                                    and allowed < (p["entry"].get("ocr_allowed_s") or 0) + RETRY_GAIN_S):
                # time is up (or too short to read more of this scan than last time): the next build reads it, and
                # until then the previous build's document for it stays
                if not out_of_time:
                    out_of_time = True
                    log("time limit reached: the files left are read by the next build")
                report["pending"].append(rel)
                if not p:
                    continue
                e, doc = dict(p["entry"]), p["doc"]
                note = " (not read again yet: the previous build's document is kept)"
            else:
                read = _read_unit(unit, sha, places, ocr_budget_s=allowed, stop_at=deadline, report=report, log=log)
                if read is None:          # unreadable: the previous build's document, if any, stays
                    if not p:
                        continue
                    e, doc = dict(p["entry"]), p["doc"]
                    note = " (could not read: the previous build's document is kept)"
                else:
                    e, doc = read
                    report["read"] += 1
                    if e.get("incomplete"):
                        # the OCR time this read had: with no engine nothing could be read, so it counts as none
                        e["ocr_allowed_s"] = round(allowed) if engine else 0
                    if e.get("incomplete") and e["ocr_allowed_s"] + RETRY_GAIN_S < ocr_budget_s:
                        report["pending"].append(rel)       # the run's time ran out mid-file: read again next build
                        note = (" (partly read: the time limit came mid-file)" if engine
                                else " (scanned pages not read: no OCR engine)")
                    else:
                        report["finished"] += 1
        if e.get("structured") and (e["leaves"] <= 2 or e["coverage"] < 0.5):
            report["incomplete_downloads"].append(f"{rel}: text for {e['leaves_with_text']} of {e['leaves']} sections")
        # the place: the first folder on the path that names one, then "<id>__" prefix, sources.csv, the text
        jid, how, unknown = place_of(places, rel, bundle, doc.get("hint", ""))
        if not jid and not bundle and "__" in f.name:
            jid, how = places.resolve(f.name.split("__", 1)[0]), "file name"
        if not jid and src.get("place"):
            jid, how = places.resolve(src["place"]), "sources.csv"
        if not jid:
            tp = doc.get("text_places") or []
            if len(tp) == 1:
                jid, how = tp[0], "text"
        if not jid:
            report["unknown_folders"] += [u for u in unknown if u not in report["unknown_folders"]]
        if jid and "share the name" in how:
            report["ambiguous"].append(f"{rel} -> {places.by_id[jid]['name']}")
        j = places.by_id.get(jid) if jid else None
        # what the owner wrote in sources.csv wins over what was read
        url = src.get("url", "") or doc.get("found_url", "")
        e.update({"id": f"{jid or 'unplaced'}-{sha[:12]}", "jurisdiction": jid or "", "place": j["name"] if j else "",
                  "place_kind": j.get("kind") if j else "", "counties": (j.get("counties") or ([j["name"]] if j and j.get("kind") == "county" else [])) if j else [],
                  "place_from": how if jid else "", "files": list(unit["files"]), "url": url,
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
            "text_places": doc.get("text_places", []), "hint": doc.get("hint", ""), "found_url": doc.get("found_url", ""),
            **({"nodes": doc["nodes"], "product": e.get("product", ""), "client": e.get("client", ""),
                "downloaded": e.get("downloaded", "")} if doc.get("nodes") else {})},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(f"{rel}: {e['place'] or 'place unknown'} · {e['kind']} · {e['sections']} sections" + note)
    docs = sorted(entries.values(), key=lambda e: (e["place"] or "~", e["kind"], e["title"], pos[e["files"][0]]))
    report["unplaced"].sort(key=pos.get)
    by_place: dict[str, dict] = {}
    for e in docs:
        if e["jurisdiction"]:
            b = by_place.setdefault(e["jurisdiction"], {"place": e["place"], "kinds": {}, "documents": 0})
            b["documents"] += 1
            b["kinds"][e["kind"]] = b["kinds"].get(e["kind"], 0) + 1
    report["documents"] = len(docs)
    report["places"] = len(by_place)
    idx = {"note": "Local laws (codes of ordinances, zoning codes, charters, single ordinances and resolutions) the "
                   "site owner downloaded by hand from each government's own site or its code host, read into sections. "
                   "Full text in docs/<id>.json. See schema.md.",
           "reader_version": READER_VERSION, "places": dict(sorted(by_place.items())), "documents": docs,
           "unplaced": report["unplaced"], "pending": report["pending"]}
    # only content changes the files (no build time), so an unchanged inbox makes no new commit in Electo
    (out / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "schema.md").write_text(SCHEMA, encoding="utf-8")
    return report


def _read_unit(unit: dict, sha: str, places: "Places", ocr_budget_s: float, stop_at: float | None, report: dict,
               log) -> tuple[dict, dict] | None:
    """(index entry, doc) read from one file or saved code; None when it cannot be read (the error is reported)."""
    kind, f, rel = unit["kind"], unit["path"], unit["rel"]
    bundle = kind == "bundle"
    if bundle and is_municode(f):
        try:
            return _municode_entry(f, rel, sha, places)
        except Exception as exc:  # noqa: BLE001 — one unreadable code never stops the rest
            report["errors"].append(f"{rel}: {type(exc).__name__}: {exc}")
            log(f"{rel}: could not read ({type(exc).__name__}: {exc})")
            return None
    try:
        info = read_bundle(f) if bundle else read_file(f, f.read_bytes(), ocr_budget_s=ocr_budget_s, stop_at=stop_at)
    except Exception as exc:  # noqa: BLE001 — one unreadable file never stops the rest
        report["errors"].append(f"{rel}: {type(exc).__name__}: {exc}")
        log(f"{rel}: could not read ({type(exc).__name__}: {exc})")
        return None
    if bundle and len(info["text"]) < 200:
        report["errors"].append(f"{rel}: saved code with almost no text (is code.html or nodes/ empty?)")
    secs = sections(info["markdown"])
    title = _title(info, f)
    e = {"sha256": sha, "title": title,
         "kind": (lambda k: "code" if bundle and k in ("other", "ordinance", "resolution", "charter") else k)(
             guess_kind(title, info["text"], len(secs))),
         "adopted": adopted_date(info["text"]), "pages": info["pages"], "chars": len(info["text"]),
         "sections": len(secs), "ocr_pages": len(info["ocr_pages"]), "mean_confidence": info["mean_confidence"],
         "needs_review": info["needs_review"],
         "file_type": "saved code (code.html / toc.json / nodes)" if bundle else f.suffix.lower().lstrip(".")}
    if info.get("incomplete"):
        e["incomplete"] = True
    doc = {"sections": secs, "text_places": places.from_text(info["text"]),
           "hint": (info.get("meta_text", "") + " " + title + " " + info["text"][:3000])[:4000],
           "found_url": info.get("url", "")}
    return e, doc


def report_markdown(rep: dict, limit: int = 100) -> str:
    """The build's report as Markdown (the workflow's job summary)."""
    lines = [f"### Downloaded ordinances: {rep['documents']} documents for {rep['places']} governments "
             f"({rep['saved_codes']} saved codes, {rep['files']} single files)",
             f"Read now: {rep['read']} · unchanged (not read again): {rep['reused']} · same file twice: {len(rep['duplicates'])}"]
    if rep.get("pending"):
        n = len(rep["pending"])
        lines += ["", f"**Not read yet, or only partly: the time limit came first** ({n}; the next build reads them, "
                  "starting from what this one wrote, and keeps the earlier build's documents for them until then):",
                  *[f"- `{x}`" for x in rep["pending"][:limit]], *([f"- and {n - limit} more"] if n > limit else [])]
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
    return "\n".join(lines) + "\n"


SCHEMA = """# Downloaded local laws (from Crawl's imports/ordinances/)

Written by `gacrawl ordinances build` from the files in the private Crawl repository's imports/ordinances/ (the build
runs on Crawl-runner, in runs of at most two hours that each start the next while files are pending), and pushed here
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
    `incomplete`: some scanned pages were not read (time limit); `ocr_allowed_s` is the OCR time that read had, and
    the file is read again when a build can give it more (the next one, when the build's time ran out mid-file)
  - `reader_version`: only on a document kept from a build with an older reader because this build did not read the
    file again (its time ran out first, or the file could not be read); a later build reads it again
  - `source`: always "downloaded by the site owner ..."; these are primary documents the owner opened
- `unplaced`: files or saved-code folders whose government could not be worked out (put them in a folder named for the place)
- `pending`: files the build's time limit left unread (or read only partly), in the order the next build reads them;
  their documents from the build before are kept in `documents` until then

## docs/<id>.json
`{id, jurisdiction, place, title, kind, adopted, url, files, sections: [{h, t}], text_places}`: `h` is the section
heading as printed (Sec. 2-14 ..., ARTICLE IV ...; a long stretch without headings is split into "(part n)"),
`t` its text with paragraphs separated by blank lines. `text_places` are the registry ids the text names; `hint` and
`found_url` are what the build read to place the document (a saved code's manifest, title and opening text).

## Municode exports (`structured: true`)
A Municode Library export (`GA/<client>/<product>/` with `toc.json`, `nodes/`, `code.html`, `manifest.json`) keeps the
code's whole outline. Its index entry adds `client`, `product`, `downloaded` (date of the download), `nodes`, `leaves`
(sections with no subsections), `leaves_with_text`, `coverage` (their share) and `url` (its Municode page; a section
is `url + "?nodeId=" + node id`). Its doc has `nodes: [[node id, heading, depth (1 = top), parent index (-1 = top),
text]]` in the code's order instead of `sections`; text is plain, one paragraph per line, table rows as
`cell | cell`, and empty where the download had none (open that section on Municode).

A downloader's saved code (`GA/<county>/code of ordinances/` with `code.html`, `toc.json`, `manifest.json`,
`nodes/`) is one document, `file_type` "saved code ...", `kind` code (or zoning), its `files` entry is the folder.
"""
