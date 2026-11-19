"""PDFs read from several threads at once (the crawl reads several hosts in parallel): PDFium is not thread-safe, and
two documents inside it at the same moment ended whole crawl shards with a segmentation fault (33 of them, Nov. 7-18,
2026). Every PDFium call is made, and every PDFium object closed, under one lock."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor

from gacrawl import ingest


def _pdf(n, pages=6):
    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    for p in range(pages):
        for y in range(30):
            c.drawString(40, 800 - y * 18, f"document {n} page {p} line {y}: the council voted to approve the minutes")
        c.showPage()
    c.save()
    return b.getvalue()


def test_one_document_inside_pdfium_at_a_time(monkeypatch):
    import pypdfium2 as pdfium

    inside, worst, guard = [0], [0], threading.Lock()
    real = pdfium.PdfDocument

    class Watched(real):
        def __init__(self, *a, **k):
            with guard:
                inside[0] += 1
                worst[0] = max(worst[0], inside[0])
            super().__init__(*a, **k)

        def close(self):
            super().close()
            with guard:
                inside[0] -= 1

    monkeypatch.setattr(pdfium, "PdfDocument", Watched)
    docs = [_pdf(i) for i in range(6)]

    def one(i):
        r = ingest.pdf_to_record(docs[i % len(docs)], url=f"https://x.example/{i}.pdf", ocr_max_pages=0)
        assert f"document {i % len(docs)} page 5 line 29" in r.text and r.meta["pages"] == 6
        return 1

    with ThreadPoolExecutor(12) as ex:
        assert sum(ex.map(one, range(120))) == 120
    assert worst[0] == 1 and inside[0] == 0      # never two at once; every document closed where it was opened


def test_a_page_is_drawn_under_the_lock_and_outlives_it():
    texts, render = ingest._pdf_pages(_pdf(1, pages=2))
    assert len(texts) == 2 and "document 1 page 1" in texts[1]
    img = render(1)
    assert img.size[0] > 1000 and img.getpixel((0, 0)) is not None
