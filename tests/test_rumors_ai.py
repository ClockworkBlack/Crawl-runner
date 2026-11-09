from gacrawl.rumors_ai import _json, verdict

TEXT = "Atlanta Mayor Andre Dickens wrote a letter to the DNC. Dickens said the party was abandoning Black voters. Others disagreed."


def test_kept_when_quote_is_exact():
    keep, why = verdict(_json('{"named": true, "quote": "Dickens said the party was abandoning Black voters.", '
                              '"attribution": "named source", "opinion": false}'), "Andre Dickens", TEXT)
    assert keep and not why and keep["attribution"] == "named source"


def test_dropped_when_quote_is_made_up_or_name_is_there():
    keep, why = verdict({"named": True, "quote": "Dickens resigned.", "attribution": "anonymous"}, "Andre Dickens", TEXT)
    assert keep is None and "not copied" in why[0]
    keep, why = verdict({"named": False, "quote": "", "attribution": "none"}, "Andre Dickens", TEXT)
    assert keep is None and "has the name" in why[0]
    keep, _ = verdict({"named": False, "quote": "", "attribution": "none", "opinion": True}, "Austin Scott", TEXT)
    assert keep == {"named": False, "quote": "", "attribution": "none", "opinion": True}


# ---------------------------------------------------------------- each item's page, read once (fetch_texts)
import json  # noqa: E402
import time  # noqa: E402

from gacrawl import rumors_ai  # noqa: E402
from gacrawl.candidates import RumorMill  # noqa: E402
from gacrawl.fetch import Disallowed  # noqa: E402


def test_excerpt_keeps_the_paragraphs_that_name_the_person():
    paras = [f"Paragraph {i} is about the county budget and nothing else, at some length to fill the page." for i in range(60)]
    paras[40] = "Commissioner Lee Ward voted against the budget, saying it raised taxes."
    paras[41] = "Ward later told reporters the vote was about schools."
    ex = rumors_ai.excerpt("\n\n".join(paras), "Lee Ward")
    assert "Lee Ward voted against" in ex and "Ward later told" in ex and "Paragraph 39" in ex     # the one before, too
    assert "Paragraph 0 " not in ex and len(ex) <= rumors_ai.TEXT_MAX
    # a page that never names them keeps its opening (the reading can then say "not named")
    assert rumors_ai.excerpt("\n".join(paras[:5]), "Pat Nobody").startswith("Paragraph 0")


class _Page:
    def __init__(self, url, status=200, html="", ctype="text/html; charset=utf-8"):
        self.url, self.status, self.content_type = url, status, ctype
        self.text = html


class _Pages:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, **kw):
        self.asked.append(url)
        p = self.pages[url]
        if isinstance(p, Exception):
            raise p
        return p


def test_fetch_texts_reads_each_page_once(tmp_path):
    (tmp_path / "data" / "rumor_mill").mkdir(parents=True)
    rows = [{"id": "r1", "candidate_id": "lee-ward", "claim": "Ward voted no", "url": "https://news.example/a", "status": "unverified",
             "last_seen": "2026-11-08"},
            {"id": "r2", "candidate_id": "lee-ward", "claim": "x", "url": "https://blocked.example/b", "status": "unverified",
             "last_seen": "2026-11-07"},
            {"id": "r3", "candidate_id": "lee-ward", "claim": "y", "url": "https://news.example/c", "status": "confirmed"},
            {"id": "r4", "candidate_id": "lee-ward", "claim": "z", "url": "https://news.example/d", "status": "unverified",
             "meta": {"text": "Long text already kept. " * 20}}]
    (tmp_path / "data" / "rumor_mill" / "rumors.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    body = "<html><body><article>" + "".join(f"<p>Story paragraph {i} about the county commission meeting.</p>" for i in range(8)) + \
           "<p>Commissioner Lee Ward voted against the budget.</p></article></body></html>"
    f = _Pages({"https://news.example/a": _Page("https://news.example/a", html=body),
                "https://blocked.example/b": Disallowed("robots.txt")})
    st = rumors_ai.fetch_texts(f, tmp_path, [{"id": "lee-ward", "name": "Lee Ward"}], time.monotonic() + 30, log=lambda *_: None)
    assert st == {"todo": 2, "read": 1, "kept": 1, "failed": 1, "left": 0}
    by = {r["id"]: r for r in RumorMill(tmp_path).all()}
    assert "Lee Ward voted against" in by["r1"]["meta"]["text"] and by["r1"]["meta"]["text_url"] == "https://news.example/a"
    assert by["r2"]["meta"]["text_error"].startswith("Disallowed") and by["r2"]["meta"]["text_tried"]
    assert "meta" not in by["r3"] or not by["r3"]["meta"].get("text_tried")       # confirmed by hand: never opened
    # the next run asks for nothing: one page kept, the refused one waits TEXT_RETRY_DAYS
    f.asked.clear()
    assert rumors_ai.fetch_texts(f, tmp_path, [], time.monotonic() + 30, log=lambda *_: None)["todo"] == 0 and not f.asked
