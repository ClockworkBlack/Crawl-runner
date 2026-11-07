"""Screenshot / scanned image / PDF -> clean text in the best shape for ingestion.

Output for every input is one Record with:
  text      plain text, reading order, hyphenation repaired
  markdown  paragraphs + ordinance-aware headings (Sec. 2-14, ARTICLE IV, ...)
  meta      mean OCR confidence, low-confidence lines to review, sha256 of the
            original file, page count, engine, and how it was captured

Screenshots are for content a person captured themselves (posts, pages a
crawler isn't allowed to fetch, image-only notices). The automated `capture`
path only runs on URLs robots.txt allows; it exists for pages whose text is
locked in images or canvas, not to get around a site's rules.
"""
from __future__ import annotations

import hashlib
import io
import re
import statistics
from pathlib import Path

from .extract import ORDINANCE_HEADING
from .store import Record

LOW_CONF = 70


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preprocess(img):
    """Grayscale, upscale small captures, boost contrast, binarize. Big accuracy win on phone screenshots."""
    from PIL import ImageOps, ImageFilter

    img = ImageOps.exif_transpose(img).convert("L")
    if img.width < 1600:
        scale = 1600 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    # dark-mode screenshots: invert so text is dark on light
    hist = img.histogram()
    dark = sum(hist[:128]) > sum(hist[128:])
    if dark:
        img = ImageOps.invert(img)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _lines_from_tesseract(img) -> list[dict]:
    import pytesseract

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--oem 1 --psm 3")
    lines: dict[tuple, dict] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        ln = lines.setdefault(key, {"words": [], "confs": [], "top": data["top"][i], "left": data["left"][i]})
        ln["words"].append(word)
        ln["confs"].append(conf)
    out = []
    for key in sorted(lines, key=lambda k: (k[0], k[1], k[2])):
        ln = lines[key]
        out.append({"block": key[0], "par": key[1], "text": " ".join(ln["words"]),
                    "conf": statistics.mean(ln["confs"]), "top": ln["top"]})
    return out


def lines_to_markdown(lines: list[dict]) -> tuple[str, str]:
    """Group OCR lines into paragraphs; repair hyphenated line breaks; mark ordinance headings."""
    paras: list[list[str]] = []
    last = None
    heading_open = False
    for ln in lines:
        key = (ln["block"], ln["par"])
        is_heading = bool(ORDINANCE_HEADING.match(ln["text"])) and len(ln["text"]) < 160
        # headings always stand alone, even when OCR puts them in a body paragraph
        if key != last or is_heading or heading_open:
            paras.append([])
            last = key
        paras[-1].append(ln["text"])
        heading_open = is_heading
    md_blocks, txt_blocks = [], []
    for p in paras:
        joined = ""
        for piece in p:
            if joined.endswith("-") and piece[:1].islower():
                joined = joined[:-1] + piece
            else:
                joined = (joined + " " + piece).strip()
        joined = re.sub(r"\s+", " ", joined)
        if not joined:
            continue
        # OCR often splits one sentence across "paragraphs": rejoin mid-sentence breaks
        if (txt_blocks and md_blocks and not md_blocks[-1].startswith("#")
                and not re.search(r"[.:;!?)]$", txt_blocks[-1]) and joined[:1].islower()):
            txt_blocks[-1] += " " + joined
            md_blocks[-1] += " " + joined
            continue
        txt_blocks.append(joined)
        if ORDINANCE_HEADING.match(joined) and len(joined) < 160:
            md_blocks.append("### " + joined)
        elif len(joined) < 80 and joined.isupper() and len(joined.split()) <= 10:
            md_blocks.append("## " + joined.title())
        elif re.match(r"^\(?[a-z0-9]{1,3}[\).]\s", joined):
            md_blocks.append("- " + joined)
        else:
            md_blocks.append(joined)
    return "\n\n".join(txt_blocks), "\n\n".join(md_blocks)


def image_to_record(path_or_bytes, source_id: str = "manual", kind: str = "ocr", url: str = "",
                    title: str = "", captured_by: str = "manual", meta: dict | None = None) -> Record:
    from PIL import Image

    data = Path(path_or_bytes).read_bytes() if not isinstance(path_or_bytes, (bytes, bytearray)) else bytes(path_or_bytes)
    img = preprocess(Image.open(io.BytesIO(data)))
    lines = _lines_from_tesseract(img)
    text, md = lines_to_markdown(lines)
    confs = [l["conf"] for l in lines]
    return Record(
        source_id=source_id, kind=kind, url=url, channel="ocr",
        title=title or (text.split("\n")[0][:120] if text else ""),
        text=text, markdown=md,
        meta={
            **(meta or {}),
            "engine": "tesseract",
            "captured_by": captured_by,
            "sha256": _sha256(data),
            "mean_confidence": round(statistics.mean(confs), 1) if confs else 0.0,
            "needs_review": [l["text"] for l in lines if l["conf"] < LOW_CONF][:50],
            "file": str(path_or_bytes) if not isinstance(path_or_bytes, (bytes, bytearray)) else None,
        },
    )


OCR_MAX_PAGES = 60      # scanned pages OCR'd per document during a crawl (a 300-page scan would hold a worker for many minutes)
OCR_BUDGET_S = 90.0     # and at most this long per document during a crawl; the rest is finished by `gacrawl ocr pending`
OCR_DPI = 300


def _pdf_pages(data: bytes):
    """(page texts, renderer) for a PDF: PDFium (C, fast, runs outside the interpreter lock) first, pdfplumber
    when PDFium cannot open the file. The renderer draws one page as a PIL image for OCR."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        texts = []
        for i in range(len(pdf)):
            tp = pdf[i].get_textpage()
            texts.append(tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))

        def render(i: int):
            return pdf[i].render(scale=OCR_DPI / 72).to_pil()
        return texts, render
    except Exception:  # noqa: BLE001 — damaged or unusual file: the slower reader may still manage
        pass
    import pdfplumber

    pdf = pdfplumber.open(io.BytesIO(data))
    texts = [(pg.extract_text() or "") for pg in pdf.pages]

    def render(i: int):
        return pdf.pages[i].to_image(resolution=OCR_DPI).original
    return texts, render


def pdf_to_record(path_or_bytes, source_id: str = "manual", kind: str = "document", url: str = "",
                  title: str = "", meta: dict | None = None, ocr_min_chars: int = 40,
                  ocr_max_pages: int | None = None, ocr_budget_s: float | None = None, stop_at: float | None = None) -> Record:
    """Text layer first (exact, fast); OCR only the pages that are scans, at most `ocr_max_pages` of them and for at
    most `ocr_budget_s` (OCR_MAX_PAGES / OCR_BUDGET_S by default; `stop_at`, monotonic, ends OCR at the run's
    deadline). Pages left unread are listed in meta.ocr_pending (and meta.ocr_truncated is set) so a later
    `gacrawl ocr pending` can finish the document."""
    import time

    import os

    # GACRAWL_OCR_PAGES=0 (the workflow's crawl jobs) leaves every scanned page to `gacrawl ocr pending` in the
    # merge job, so the crawl runners need no OCR engine installed
    ocr_max_pages = int(os.environ.get("GACRAWL_OCR_PAGES", OCR_MAX_PAGES)) if ocr_max_pages is None else ocr_max_pages
    ocr_budget_s = OCR_BUDGET_S if ocr_budget_s is None else ocr_budget_s
    data = Path(path_or_bytes).read_bytes() if not isinstance(path_or_bytes, (bytes, bytearray)) else bytes(path_or_bytes)
    texts, render = _pdf_pages(data)
    pages_md, pages_txt, ocr_pages, pending, confs = [], [], [], [], []
    t0 = time.monotonic()
    for i, txt in enumerate(texts, 1):
        if len(txt.strip()) >= ocr_min_chars:
            lines = [{"block": 0, "par": n, "text": l.strip(), "conf": 100.0, "top": 0} for n, l in enumerate(_reflow(txt))]
        elif (len(ocr_pages) >= ocr_max_pages or time.monotonic() - t0 > ocr_budget_s
              or (stop_at is not None and time.monotonic() > stop_at)):
            pending.append(i)
            lines = [{"block": 0, "par": 0, "text": txt.strip(), "conf": 0.0, "top": 0}] if txt.strip() else []
        else:
            try:
                lines = _lines_from_tesseract(preprocess(render(i - 1)))
            except Exception:  # noqa: BLE001 — a page that will not render or OCR is left pending, the rest go on
                pending.append(i)
                lines = []
            else:
                ocr_pages.append(i)
                confs += [l["conf"] for l in lines]
        t, m = lines_to_markdown(lines)
        pages_txt.append(t)
        pages_md.append(m)
    text = "\n\n".join(pages_txt)
    return Record(
        source_id=source_id, kind=kind, url=url, channel="ocr" if ocr_pages else "pdf",
        title=title or (text.split("\n")[0][:120] if text else ""),
        text=text, markdown="\n\n---\n\n".join(pages_md),
        meta={**(meta or {}), "sha256": _sha256(data), "pages": len(texts), "ocr_pages": ocr_pages,
              "mean_confidence": round(statistics.mean(confs), 1) if confs else None,
              **({"ocr_truncated": True, "ocr_pending": pending[:400]} if pending else {})},
    )


def finish_pending_ocr(store, fetcher, deadline: float | None = None, log=print, per_doc_pages: int = 400) -> dict:
    """Documents whose scanned pages were left unread during a crawl (meta.ocr_pending), newest first: fetch the
    file again (it is static; robots and spacing apply as always) and read it whole, with a generous page budget
    but inside this step's deadline. Each finished document replaces its record (same key, new text)."""
    import time

    out = {"checked": 0, "finished": 0, "still_pending": 0}
    rows = store.query("meta LIKE '%\"ocr_pending\"%' AND kind IN ('document','ordinance')", order="published DESC, rid DESC", limit=400)
    for r in rows:
        if deadline is not None and time.monotonic() > deadline:
            break
        out["checked"] += 1
        try:
            res = fetcher.get(r["url"])
        except Exception as exc:  # noqa: BLE001
            log(f"  ocr: {r['url']}: {type(exc).__name__}: {exc}"[:200])
            continue
        if res.status != 200 or not (res.content_type == "application/pdf" or res.content[:5] == b"%PDF-"):
            continue
        left = (deadline - time.monotonic()) if deadline is not None else 3600.0
        rec = pdf_to_record(res.content, source_id=r["source_id"], kind=r["kind"], url=r["url"], title=r["title"],
                            meta={k: v for k, v in r["meta"].items() if k not in ("ocr_pending", "ocr_truncated")},
                            ocr_max_pages=per_doc_pages, ocr_budget_s=max(10.0, left), stop_at=deadline)
        rec.jurisdiction, rec.evidence, rec.published = r.get("jurisdiction", ""), r.get("evidence") or "primary", r.get("published")
        rec.key = r.get("key") or rec.key
        store.add(rec)
        if rec.meta.get("ocr_pending"):
            out["still_pending"] += 1
        else:
            out["finished"] += 1
    store.flush()
    return out


def _reflow(txt: str) -> list[str]:
    """PDF text layers break every visual line; rebuild paragraphs on blank lines / headings."""
    paras, cur = [], []
    for raw in txt.splitlines():
        line = raw.strip()
        if not line:
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        if ORDINANCE_HEADING.match(line) and cur:
            paras.append(" ".join(cur))
            cur = []
        if cur and cur[-1].endswith("-") and line[:1].islower():
            cur[-1] = cur[-1][:-1] + line
        else:
            cur.append(line)
        if ORDINANCE_HEADING.match(line) and len(line) < 160:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


def capture(url: str, fetcher, source_id: str = "capture", wait_ms: int = 2500) -> Record:
    """Render a page in a headless browser and OCR a full-page screenshot.

    Only for pages robots.txt allows whose text isn't in the DOM (image notices,
    canvas viewers). Refuses disallowed URLs.
    """
    from .fetch import Disallowed

    if not fetcher.allowed(url):
        raise Disallowed(url, fetcher.robots.why(url))
    fetcher.limiter.wait(url, fetcher.robots.crawl_delay(url))
    from playwright.sync_api import sync_playwright

    from .fetch import launch_browser

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(user_agent=fetcher.user_agent, viewport={"width": 1280, "height": 2000})
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(wait_ms)
        png = page.screenshot(full_page=True)
        browser.close()
    rec = image_to_record(png, source_id=source_id, url=url, captured_by="browser")
    return rec
