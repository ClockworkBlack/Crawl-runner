"""imports/ordinances/ -> Electo's data/ordinances/: places from folders, file names, sources.csv or the text;
sections at the document's own headings; duplicates; reuse of an earlier build. Fictional ordinance text only."""
import io
import json
import shutil
import zipfile
from pathlib import Path

from gacrawl import ordinance_files as of

ROOT = Path(__file__).resolve().parent.parent

CODE_TXT = """CODE OF ORDINANCES OF THE CITY OF ALPHARETTA, GEORGIA

Chapter 1 GENERAL PROVISIONS

Sec. 1-1. How code designated and cited.
The ordinances embraced in the following chapters and sections shall constitute and be designated the Code.

Sec. 1-2. Definitions.
In the construction of this Code the following definitions shall be observed.

ARTICLE II. NOISE

Sec. 10-21. Loud noises prohibited.
No person shall make any unreasonably loud noise between 11:00 p.m. and 7:00 a.m.
"""

ORD_HTML = """<html><head><title>Ordinance No. 2024-07 Short-Term Rentals</title></head><body><main>
<h1>Ordinance No. 2024-07</h1><p>An ordinance amending Chapter 12 to regulate short-term rentals.</p>
<p>Sec. 12-5. Permit required.</p><p>No dwelling shall be rented for fewer than 30 days without a permit.</p>
<p>Adopted by the Board of Commissioners on March 4, 2024.</p></main></body></html>"""


def _docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", xml)
    return b.getvalue()


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "jurisdictions.yaml", root / "config" / "jurisdictions.yaml")
    inbox = root / "imports" / "ordinances"
    (inbox / "City of Alpharetta").mkdir(parents=True)
    (inbox / "co-fulton").mkdir()
    (inbox / "Not A Real Place").mkdir()
    (inbox / "City of Alpharetta" / "code.txt").write_text(CODE_TXT)
    (inbox / "City of Alpharetta" / "code copy.txt").write_text(CODE_TXT)          # same bytes: one document
    (inbox / "co-fulton" / "str.html").write_text(ORD_HTML)
    (inbox / "co-cobb__res.docx").write_bytes(_docx(["RESOLUTION NO. 2023-11",
                                                     "A resolution authorizing the purchase of fire apparatus.",
                                                     "Approved June 6, 2023."]))
    (inbox / "Not A Real Place" / "x.txt").write_text("Some text naming no place at all.")
    (inbox / "README.md").write_text("ignored")
    (inbox / "co-fulton" / "sources.csv").write_text(
        "file,url,title,date,kind,note\nstr.html,https://www.fultoncountyga.gov/ord-2024-07,,2024-03-05,,checked\n")
    return root


def test_build_places_kinds_sections_and_reuse(tmp_path):
    root = _project(tmp_path)
    out = tmp_path / "out"
    rep = of.build(root, out, log=lambda *_: None)
    assert rep["files"] == 5 and rep["documents"] == 4 and rep["duplicates"] == ["City of Alpharetta/code.txt"]
    assert rep["unknown_folders"] == ["Not A Real Place"] and rep["unplaced"] == ["Not A Real Place/x.txt"]
    idx = json.loads((out / "index.json").read_text())
    by_place = {d["jurisdiction"]: d for d in idx["documents"]}

    code = by_place["alpharetta"]
    assert code["kind"] == "code" and code["place_from"] == "folder" and len(code["files"]) == 2
    doc = json.loads((out / "docs" / f"{code['id']}.json").read_text())
    heads = [s["h"] for s in doc["sections"]]
    assert "Sec. 10-21. Loud noises prohibited." in heads and any(h.startswith("ARTICLE II") for h in heads)
    assert any("unreasonably loud noise" in s["t"] for s in doc["sections"])

    fulton = by_place["co-fulton"]
    assert fulton["kind"] == "ordinance" and fulton["adopted"] == "2024-03-05"      # sources.csv wins over the text
    assert fulton["url_official"] is True and fulton["note"] == "checked" and fulton["counties"] == ["Fulton County"]

    cobb = by_place["co-cobb"]
    assert cobb["place_from"] == "file name" and cobb["kind"] == "resolution" and cobb["adopted"] == "2023-06-06"
    assert idx["places"]["alpharetta"]["kinds"] == {"code": 1}
    assert (out / "schema.md").exists()

    # a second build reads nothing again and writes byte-identical files
    before = {p.name: p.read_bytes() for p in (out / "docs").glob("*.json")} | {"index": (out / "index.json").read_bytes()}
    prev = tmp_path / "prev"
    shutil.copytree(out, prev)
    rep2 = of.build(root, out, prev=prev, log=lambda *_: None)
    assert rep2["read"] == 0 and rep2["reused"] == 4
    after = {p.name: p.read_bytes() for p in (out / "docs").glob("*.json")} | {"index": (out / "index.json").read_bytes()}
    assert after == before


def test_place_names_resolve():
    p = of.Places(of.load_places(ROOT))
    assert p.resolve("alpharetta") == p.resolve("City of Alpharetta") == p.resolve("Alpharetta, GA") == "alpharetta"
    assert p.resolve("Fulton County") == p.resolve("fulton-county") == p.resolve("co-fulton") == "co-fulton"
    assert p.resolve("Nowhere Special") is None


def test_sections_split_long_unheaded_text():
    md = "\n\n".join(["word " * 300] * 10)        # ~15,000 characters, no headings
    secs = of.sections(md)
    assert len(secs) >= 2 and all(len(s["t"]) <= of.SECTION_TARGET + 2000 for s in secs)
    assert secs[1]["h"].endswith("(part 2)")


SAVED_CODE = """<html><head><title>Code of Ordinances | Jackson County, GA | Municode Library</title></head><body>
<nav><a href="#">Home</a></nav><div class="chunk"><h2>Chapter 6 - ANIMALS</h2>
<p>Sec. 6-1. Definitions.</p><p>Animal means any live vertebrate creature, domestic or wild.</p>
<p>Sec. 6-2. Running at large prohibited.</p><p>No owner shall allow any dog to run at large in the county.</p>
</div><div class="chunk"><h2>Chapter 14 - SOLID WASTE</h2><p>Sec. 14-1. Collection.</p>
<p>Solid waste shall be collected weekly by the county or its contractor.</p></div></body></html>"""


def _saved_code(d: Path, html=SAVED_CODE, nodes=None, url="https://library.municode.com/ga/jackson_county/codes/code_of_ordinances"):
    d.mkdir(parents=True)
    if html is not None:
        (d / "code.html").write_text(html)
    (d / "toc.json").write_text(json.dumps({"title": "Code of Ordinances", "children": [{"title": "Chapter 6 - ANIMALS"}]}))
    (d / "manifest.json").write_text(json.dumps({"client": "jackson_county", "url": url, "downloaded": "2026-11-07"}))
    (d / "nodes").mkdir()
    for name, body in (nodes or {"1.json": {"title": "Chapter 6", "content": "<p>node copy</p>"}}).items():
        (d / "nodes" / name).write_text(json.dumps(body))


def test_dropped_ga_folder_one_code_per_county(tmp_path):
    root = _project(tmp_path)
    inbox = root / "imports" / "ordinances"
    for p in list(inbox.iterdir()):      # only the downloader's layout this time
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    ga = inbox / "out" / "GA"
    _saved_code(ga / "Jackson" / "code of ordinances")                     # city and county share the name
    _saved_code(ga / "fulton_county" / "Code of Ordinances",
                html=SAVED_CODE.replace("Jackson County", "Fulton County"),
                url="https://library.municode.com/ga/fulton_county/codes/code_of_ordinances")
    # no code.html: the nodes are read in order (2 before 10)
    _saved_code(ga / "Cobb" / "code of ordinances", html=None, url="https://library.municode.com/ga/cobb_county",
                nodes={"10.json": {"title": "Sec. 2-10. Later.", "content": "<p>Second node text about permits.</p>"},
                       "2.json": {"title": "Sec. 2-2. Earlier.", "content": "<p>First node text about fees and charges.</p>" * 10}})
    out = tmp_path / "out"
    rep = of.build(root, out, log=lambda *_: None)
    assert rep["saved_codes"] == 3 and rep["files"] == 0 and rep["documents"] == 3 and not rep["errors"], rep
    idx = json.loads((out / "index.json").read_text())
    d = {e["jurisdiction"]: e for e in idx["documents"]}
    assert set(d) == {"co-jackson", "co-fulton", "co-cobb"}
    assert rep["ambiguous"] == ["out/GA/Jackson/code of ordinances -> Jackson County"]
    j = d["co-jackson"]
    assert j["kind"] == "code" and j["title"] == "Code of Ordinances | Jackson County, GA"
    assert j["url"].startswith("https://library.municode.com/") and j["url_official"] is True
    assert j["files"] == ["out/GA/Jackson/code of ordinances"]
    doc = json.loads((out / "docs" / f"{j['id']}.json").read_text())
    heads = [s["h"] for s in doc["sections"]]
    assert "Sec. 6-2. Running at large prohibited." in heads and "Chapter 14 - SOLID WASTE" in heads
    assert not any("node copy" in s["t"] for s in doc["sections"])          # code.html wins; nodes not read twice
    cobb = json.loads((out / "docs" / f"{d['co-cobb']['id']}.json").read_text())
    hs = [s["h"] for s in cobb["sections"]]
    assert hs.index("Sec. 2-2. Earlier.") < hs.index("Sec. 2-10. Later.")

    # the same GA folder as a zip: same documents, and a rebuild reuses them
    import zipfile as zf
    for p in list(inbox.iterdir()):
        shutil.make_archive(str(tmp_path / "GA"), "zip", inbox / "out")
        shutil.rmtree(p)
    shutil.move(str(tmp_path / "GA.zip"), inbox / "GA.zip")
    prev = tmp_path / "prev"
    shutil.copytree(out, prev)
    rep2 = of.build(root, out, prev=prev, log=lambda *_: None)
    assert rep2["documents"] == 3 and rep2["reused"] == 3 and rep2["read"] == 0
    idx2 = json.loads((out / "index.json").read_text())
    assert {e["jurisdiction"] for e in idx2["documents"]} == {"co-jackson", "co-fulton", "co-cobb"}
    assert any(f.startswith("GA.zip/GA/") for e in idx2["documents"] for f in e["files"])


def test_city_folder_wins_when_the_code_says_city():
    p = of.Places(of.load_places(ROOT))
    assert p.resolve_folder("Jackson")[0] == "co-jackson"
    city = p.resolve("Jackson")
    assert city and p.resolve_folder("Jackson", "Code of Ordinances of the City of Jackson, Georgia")[0] == city
    assert p.resolve_folder("athens-clarke_county")[0] in ("athens-clarke", "co-clarke")
    assert p.resolve_folder("liberty_county_")[0] == "co-liberty"
    assert p.resolve_folder("unified_government_of_georgetown_quitman_county_commission")[0] == "georgetown-quitman"
    assert p.resolve_folder("jekyll_island_authority")[0] == "jekyll-island-authority"
    assert p.resolve_folder("GA") == (None, False) and p.resolve_folder("code of ordinances") == (None, False)


def _municode_export(d: Path, client="Acworth", product="Code of Ordinances"):
    """A small Municode Library export: toc.json outline, one chunk group in nodes/, code.html with the same text."""
    d.mkdir(parents=True)
    toc = {"Id": "1", "Heading": product, "NodeDepth": -1, "Children": [
        {"Id": "CH14", "Heading": "Chapter 14 - ANIMALS AND FOWL", "NodeDepth": 1, "Children": [
            {"Id": "CH14_S14-1", "Heading": "Sec. 14-1. - Definitions.", "NodeDepth": 2, "Children": []},
            {"Id": "CH14_S14-2", "Heading": "Sec. 14-2. - Dogs at large.", "NodeDepth": 2, "Children": []}]},
        {"Id": "CH62", "Heading": "Chapter 62 - PERSONNEL", "NodeDepth": 1, "Children": [
            {"Id": "CH62_S62-1", "Heading": "Sec. 62-1. - Policy.", "NodeDepth": 2, "Children": []}]}]}
    (d / "toc.json").write_text(json.dumps(toc))
    (d / "manifest.json").write_text(json.dumps({"client": client, "product_id": 1, "product_name": product,
                                                 "job_id": 2, "node_count": 6, "downloaded_at": "2026-11-07T14:40:30"}))
    (d / "nodes").mkdir()
    docs = [{"Id": "CH14", "Title": "Chapter 14", "Content": ""},
            {"Id": "CH14_S14-1", "Title": "Sec. 14-1", "Content": '<div class="chunk-content"><p class="p0">Owner means '
             'the person who has title to an animal.</p><p class="historynote0">(Code 1983, § 4-2)</p></div>'},
            {"Id": "CH14_S14-2", "Title": "Sec. 14-2", "Content": '<div class="chunk-content"><p class="incr0">(a)</p>'
             '<p class="content1">No dog shall run at large.</p><table><tr><td>Offense</td><td>Fine</td></tr>'
             '<tr><td>First</td><td>$50</td></tr></table></div>'}]
    (d / "nodes" / "ch14.json").write_text(json.dumps({"Docs": docs}))
    (d / "code.html").write_text('<html><head><title>x</title></head><body><h2 id="CH14">Chapter 14</h2>'
                                 '<h2 id="CH62">Chapter 62</h2><h2 id="CH62_S62-1">Sec. 62-1.</h2></body></html>')


def test_municode_export_outline_text_links_coverage(tmp_path):
    root = _project(tmp_path)
    inbox = root / "imports" / "ordinances"
    for p in list(inbox.iterdir()):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    _municode_export(inbox / "GA" / "acworth" / "code_of_ordinances")
    out = tmp_path / "out"
    rep = of.build(root, out, log=lambda *_: None)
    assert rep["documents"] == 1 and not rep["errors"]
    e = json.loads((out / "index.json").read_text())["documents"][0]
    assert e["jurisdiction"] == "acworth" and e["structured"] and e["kind"] == "code"
    assert e["title"] == "Acworth Code of Ordinances" and e["downloaded"] == "2026-11-07"
    assert e["url"] == "https://library.municode.com/ga/acworth/codes/code_of_ordinances" and e["url_official"]
    assert (e["leaves"], e["leaves_with_text"]) == (3, 2) and e["coverage"] == 0.667 and e["needs_review"]
    doc = json.loads((out / "docs" / f"{e['id']}.json").read_text())
    ids = [n[0] for n in doc["nodes"]]
    assert ids == ["CH14", "CH14_S14-1", "CH14_S14-2", "CH62", "CH62_S62-1"]
    n = {x[0]: x for x in doc["nodes"]}
    assert n["CH14_S14-1"][2] == 2 and n["CH14_S14-1"][3] == 0              # depth, parent index
    assert n["CH14_S14-2"][4] == "(a) No dog shall run at large.\nOffense | Fine\nFirst | $50"
    assert n["CH14_S14-1"][4].endswith("(Code 1983, § 4-2)") and n["CH62_S62-1"][4] == ""


def test_municode_kind_from_product_and_report_of_thin_downloads(tmp_path):
    root = _project(tmp_path)
    inbox = root / "imports" / "ordinances"
    for p in list(inbox.iterdir()):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    _municode_export(inbox / "GA" / "douglas_county" / "unified_development_code", "Douglas County", "Unified Development Code")
    rep = of.build(root, tmp_path / "out", log=lambda *_: None)
    e = json.loads((tmp_path / "out" / "index.json").read_text())["documents"][0]
    assert e["jurisdiction"] == "co-douglas" and e["kind"] == "zoning"
    assert rep["incomplete_downloads"] == []                                  # 2 of 3 is not "thin"


def test_html_text_footnotes_and_inline_runs():
    t = of.html_text('<div class="chunk-content"><p class="p0">Text.</p><div class="footnote-content"><div>Footnotes: </div>'
                     ' --- (<b>1</b>) --- <p class="refcrossFN"><b>Cross reference— </b> Nuisances.</p></div></div>')
    assert t == "Text.\nFootnote 1:\nCross reference— Nuisances."
    assert of.html_text("<div>Some <b>bold</b> text<br>next line<p>para</p>tail</div>") == "Some bold text\nnext line\npara\ntail"


# ----------------------------------------------------------------------------- the time limit
# Nov 10-15, 2026: every ordinances run was killed at the job's 60-minute limit while OCRing scans, wrote and sent
# nothing, and so the next run (whose cache is what the last one sent) read the same files again. A build with a time
# limit ends on its own with what it read, and the next one goes on from there.
class _Clock:
    """A clock each file read moves on by `step` seconds, so a time limit runs without waiting."""

    def __init__(self, monkeypatch, step=100.0):
        self.now, self.step, self.calls = 1000.0, step, []
        real = of.read_file
        monkeypatch.setattr(of, "_clock", lambda: self.now)

        def read_file(path, data, **kw):
            self.calls.append(path.name)
            self.now += self.step
            return real(path, data, **kw)
        monkeypatch.setattr(of, "read_file", read_file)


def _docs(out):
    return {p.name: p.read_bytes() for p in (out / "docs").glob("*.json")}


def test_time_limit_writes_what_was_read_and_the_next_build_goes_on(tmp_path, monkeypatch):
    root = _project(tmp_path)
    whole = tmp_path / "whole"
    of.build(root, whole, log=lambda *_: None)                      # the same inbox with no limit, to compare
    clock = _Clock(monkeypatch)
    out = tmp_path / "out"
    rep = of.build(root, out, log=lambda *_: None, max_minutes=2.5)  # 150 s: two files of 100 s each are started
    assert rep["read"] == 2 and rep["finished"] == 2 and len(rep["pending"]) == 2 and rep["documents"] == 2
    idx = json.loads((out / "index.json").read_text())
    assert idx["pending"] == rep["pending"] and len(idx["documents"]) == 2
    assert not {f for e in idx["documents"] for f in e["files"]} & set(rep["pending"])
    assert "Not read yet" in of.report_markdown(rep) and all(f"`{x}`" in of.report_markdown(rep) for x in rep["pending"])

    # the next build starts from what this one wrote: the two documents are reused, the other two read
    prev = tmp_path / "prev"
    shutil.copytree(out, prev)
    clock.calls.clear()
    rep2 = of.build(root, out, prev=prev, log=lambda *_: None, max_minutes=2.5)
    assert rep2["reused"] == 2 and rep2["read"] == 2 and rep2["pending"] == []
    assert sorted(clock.calls) == sorted(Path(x).name for x in rep["pending"])
    assert _docs(out) == _docs(whole)
    a, b = json.loads((out / "index.json").read_text()), json.loads((whole / "index.json").read_text())
    assert a == b and a["pending"] == []


def test_time_limit_keeps_the_documents_an_older_reader_wrote(tmp_path, monkeypatch):
    root = _project(tmp_path)
    prev = tmp_path / "prev"
    of.build(root, prev, log=lambda *_: None)
    idx = json.loads((prev / "index.json").read_text())
    idx["reader_version"] = of.READER_VERSION - 1                    # the reader changed since: all are read again
    (prev / "index.json").write_text(json.dumps(idx))
    out = tmp_path / "out"
    rep = of.build(root, out, prev=prev, log=lambda *_: None, max_minutes=0)
    # no time to read any: Electo keeps all four documents (not none), each marked with the reader that wrote it
    assert rep["read"] == 0 and rep["documents"] == 4 and len(rep["pending"]) == 4
    kept = json.loads((out / "index.json").read_text())
    assert {e["reader_version"] for e in kept["documents"]} == {of.READER_VERSION - 1}
    assert _docs(out) == _docs(prev)
    # so the build after it still reads them again (the index's version does not vouch for them), and then they are current
    prev2 = tmp_path / "prev2"
    shutil.copytree(out, prev2)
    rep2 = of.build(root, out, prev=prev2, log=lambda *_: None)
    assert rep2["read"] == 4 and rep2["reused"] == 0 and rep2["pending"] == []
    assert not any("reader_version" in e for e in json.loads((out / "index.json").read_text())["documents"])


def _partly_read(monkeypatch, name="str.html"):
    """read_file as for a scan whose OCR stopped before the last page (time limit) for the file `name`."""
    real = of.read_file

    def read_file(path, data, **kw):
        info = real(path, data, **kw)
        if path.name == name:
            info["incomplete"] = True
            info["needs_review"] = ["3 scanned pages not read yet (time limit)"]
        return info
    monkeypatch.setattr(of, "read_file", read_file)


def test_scan_cut_short_by_the_time_limit_is_read_again_and_one_that_had_its_full_time_is_not(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(of, "_ocr_engine", lambda: True)
    _partly_read(monkeypatch)
    clock = _Clock(monkeypatch, step=90.0)
    out = tmp_path / "out"
    # 300 s: three files take 270 s, so str.html (last in the inbox) has 30 s of OCR left and stops partway
    rep = of.build(root, out, log=lambda *_: None, max_minutes=5)
    e = next(x for x in json.loads((out / "index.json").read_text())["documents"] if x["files"] == ["co-fulton/str.html"])
    assert e["incomplete"] and e["ocr_allowed_s"] == 30
    assert rep["read"] == 4 and rep["finished"] == 3 and rep["pending"] == ["co-fulton/str.html"]

    # a next build with less time than that for it keeps the document as it is (and it is still pending)
    prev = tmp_path / "prev"
    shutil.copytree(out, prev)
    clock.calls.clear()
    clock.now = 1000.0
    rep_short = of.build(root, tmp_path / "short", prev=prev, log=lambda *_: None, max_minutes=1)
    assert clock.calls == [] and rep_short["reused"] == 3 and rep_short["documents"] == 4
    assert rep_short["pending"] == ["co-fulton/str.html"] and rep_short["finished"] == 0

    # with the file's whole OCR time it is read again, and what it reads then is as much as it ever will
    rep2 = of.build(root, out, prev=prev, log=lambda *_: None)
    assert clock.calls == ["str.html"] and rep2["reused"] == 3 and rep2["finished"] == 1 and rep2["pending"] == []
    e2 = next(x for x in json.loads((out / "index.json").read_text())["documents"] if x["files"] == ["co-fulton/str.html"])
    assert e2["ocr_allowed_s"] == 900
    prev2 = tmp_path / "prev2"
    shutil.copytree(out, prev2)
    clock.calls.clear()
    rep3 = of.build(root, out, prev=prev2, log=lambda *_: None)
    assert clock.calls == [] and rep3["read"] == 0 and rep3["reused"] == 4
    # more OCR time per file (--ocr-minutes) reads it again
    rep4 = of.build(root, out, prev=prev2, log=lambda *_: None, ocr_budget_s=1800)
    assert clock.calls == ["str.html"] and rep4["read"] == 1


def test_scan_read_without_the_ocr_engine_is_read_again(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(of, "_ocr_engine", lambda: False)
    _partly_read(monkeypatch)
    out = tmp_path / "out"
    rep = of.build(root, out, log=lambda *_: None)
    e = next(x for x in json.loads((out / "index.json").read_text())["documents"] if x["files"] == ["co-fulton/str.html"])
    assert e["ocr_allowed_s"] == 0 and rep["pending"] == ["co-fulton/str.html"] and rep["finished"] == 3


def test_big_files_and_partly_read_scans_are_read_last(tmp_path, monkeypatch):
    root = _project(tmp_path)
    inbox = root / "imports" / "ordinances"
    (inbox / "City of Alpharetta" / "big.txt").write_text(CODE_TXT.replace("Code", "Big code") * 20)
    monkeypatch.setattr(of, "LARGE_FILE", 2000)
    monkeypatch.setattr(of, "_ocr_engine", lambda: True)
    _partly_read(monkeypatch, "co-cobb__res.docx")
    clock = _Clock(monkeypatch, step=1.0)
    out = tmp_path / "out"
    # 10 minutes: every file is read, but res.docx (first) stops at 600 s of OCR, short of its 900, so it is read again
    rep = of.build(root, out, log=lambda *_: None, max_minutes=10)
    # inbox order is res.docx, big.txt, code copy.txt, x.txt, str.html: the big file waits for the small ones
    assert clock.calls == ["co-cobb__res.docx", "code copy.txt", "x.txt", "str.html", "big.txt"]
    assert rep["pending"] == ["co-cobb__res.docx"]
    prev = tmp_path / "prev"
    shutil.copytree(out, prev)
    clock.calls.clear()
    (inbox / "Not A Real Place" / "y.txt").write_text("More text naming no place at all.")
    rep = of.build(root, out, prev=prev, log=lambda *_: None)
    # the scan read only partly goes after the new file
    assert clock.calls == ["y.txt", "co-cobb__res.docx"] and rep["pending"] == []


def test_index_order_does_not_depend_on_reading_order(tmp_path):
    # reused documents come first in the reading order now; the index must still list them as a build from scratch
    # does, or every partial run would rewrite Electo's index.json in a new order
    root = _project(tmp_path)
    folder = root / "imports" / "ordinances" / "City of Alpharetta"
    body = "AN ORDINANCE OF THE CITY OF ALPHARETTA AMENDING ITS CODE\n\nSection 1. {}\n"
    (folder / "z.txt").write_text(body.format("Parking on Main Street is limited to two hours."))
    prev = tmp_path / "prev"
    of.build(root, prev, log=lambda *_: None)
    (folder / "a.txt").write_text(body.format("Signs in the right of way are removed."))   # same title, earlier name
    out, whole = tmp_path / "out", tmp_path / "whole"
    rep = of.build(root, out, prev=prev, log=lambda *_: None)
    assert rep["read"] == 1 and rep["reused"] == 5
    of.build(root, whole, log=lambda *_: None)
    assert (out / "index.json").read_bytes() == (whole / "index.json").read_bytes()


def test_cli_build_with_a_time_limit_writes_status_and_report(tmp_path, monkeypatch):
    from gacrawl import cli

    root = _project(tmp_path)
    monkeypatch.setattr(cli.config, "ROOT", root)
    status, report = tmp_path / "status.json", tmp_path / "summary.md"
    args = cli.build_parser().parse_args(["ordinances", "build", "--out", str(tmp_path / "out"), "--max-minutes", "0",
                                          "--status", str(status), "--report", str(report)])
    cli.cmd_ordinances(None, args)                                    # returns normally: the job goes on to send
    s = json.loads(status.read_text())
    assert s["pending"] == 4 and s["finished"] == 0 and s["read"] == 0
    assert "Not read yet" in report.read_text() and "co-fulton/str.html" in report.read_text()


def test_ordinances_workflow_sends_partial_progress_and_starts_itself_again(tmp_path):
    import re
    import subprocess

    import yaml

    w = yaml.safe_load((ROOT / ".github" / "workflows" / "ordinances.yml").read_text())
    on = w.get("on", w.get(True))
    assert on.get("schedule"), "a daily run picks up pending files when a chain of runs stops"
    job = w["jobs"]["send"]
    perms = {**(w.get("permissions") or {}), **(job.get("permissions") or {})}
    assert perms.get("actions") == "write"
    steps = {s.get("name", ""): s for s in job["steps"]}
    read = next(s for n, s in steps.items() if "gacrawl ordinances build" in steps[n].get("run", ""))
    m = re.search(r"--max-minutes\s+(\d+)", read["run"])
    assert m and int(m.group(1)) + 15 <= job["timeout-minutes"], "the reading must end well before the job is killed"
    assert "--status" in read["run"]
    again = next(s for s in job["steps"] if "gh workflow run ordinances.yml" in s.get("run", ""))
    assert "steps.send.outputs.sent" in again["if"] and "FINISHED" in again["run"] and "PENDING" in again["run"]
    assert "sent=1" in steps["Send to Electo (data/ordinances/)"]["run"]
    ocr = next(s for s in job["steps"] if "tesseract-ocr" in s.get("run", ""))
    assert re.search(r"for \w+ in [^\n]*\n(?:.*\n)*?.*apt-get install", ocr["run"]), "the OCR engine install is retried"
    for i, s in enumerate(job["steps"]):
        if "run" in s:
            f = tmp_path / f"step{i}.sh"
            f.write_text(s["run"])
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, (s.get("name"), r.stderr)


# --- the workflow runs on the public runner, with the files read from the private repository -----------------------
# Until Nov 19, 2026 it ran in the private repository (paid minutes): a backlog of about 685 files meant chains of
# 75-minute runs there. Now it runs on Crawl-runner's free minutes, like the crawl and police.

WORKFLOWS = ROOT / ".github" / "workflows"


def _wf(name):
    import yaml

    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step(job, test):
    hits = [s for s in job["steps"] if test(s)]
    assert len(hits) == 1, len(hits)
    return hits[0]


def _tool(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def _bash(script, cwd, **env):
    import os
    import subprocess

    e = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "RUNNER_"))}
    e.update(env)
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=e, capture_output=True, text=True, timeout=120)


def test_ordinances_runs_on_the_public_runner_and_sync_code_carries_it():
    import re

    w = _wf("ordinances.yml")
    on = w.get("on", w.get(True))
    job = w["jobs"]["send"]
    # the private repository's starts (an upload, code-finder's and records-send's) end skipped there: no paid minutes
    assert job["if"] == "github.event.repository.private == false"
    # imports/ never exists in the runner repository: a push trigger on it could never fire there
    assert not any(p.startswith("imports/") for p in on["push"]["paths"]) and "gacrawl/ordinance_files.py" in on["push"]["paths"]
    hours = {h for c in on["schedule"] for h in c["cron"].split()[1].split(",")}
    assert len(hours) >= 4, "no upload starts a run here: the schedule reads new files several times a day"
    names = [s.get("name", "") for s in job["steps"]]
    files = next(i for i, n in enumerate(names) if n.startswith("The downloaded files"))
    ocr = next(i for i, n in enumerate(names) if n.startswith("OCR engine"))
    build = next(i for i, s in enumerate(job["steps"]) if "gacrawl ordinances build" in (s.get("run") or ""))
    assert files < ocr < build, "the files are copied before the OCR engine looks for scans, and before the build"
    assert _step(job, lambda s: s.get("uses", "").startswith("actions/checkout"))["with"]["persist-credentials"] is False
    # the next link and publish-site are started with this repository's own token, on this repository
    for s in job["steps"]:
        if "gh workflow run" in (s.get("run") or ""):
            assert s["env"]["GH_TOKEN"] == "${{ github.token }}", s["name"]
            assert re.search(r"gh workflow run \S+ -R ", s["run"]), s["name"]
    assert "GH_TOKEN" not in (job.get("env") or {})
    # sync-code copies the workflow to the runner (never the files), and keeps every run's log in the private repository
    sync = next(s["run"] for s in _wf("sync-code.yml")["jobs"]["sync"]["steps"] if s["name"].startswith("Copy the code"))
    assert "ordinances.yml" not in sync and "--exclude imports/" in sync and "pub/imports" in sync
    logs = next(s["run"] for s in _wf("sync-code.yml")["jobs"]["sync"]["steps"] if s["name"].startswith("Move finished"))
    assert '[ "$name" = "ordinances" ]' in logs


def test_ordinances_reads_the_private_files_into_its_own_runner_and_never_an_empty_copy(tmp_path):
    job = _wf("ordinances.yml")["jobs"]["send"]
    run = _step(job, lambda s: s.get("name", "").startswith("The downloaded files"))["run"]
    work, rt, private = tmp_path / "work", tmp_path / "rt", tmp_path / "private"
    (work / "config").mkdir(parents=True)
    rt.mkdir()
    shutil.copy(ROOT / "config" / "runner.json", work / "config" / "runner.json")
    (private / "imports" / "ordinances" / "alpharetta").mkdir(parents=True)
    (private / "imports" / "ordinances" / "alpharetta" / "code.html").write_text(ORD_HTML)
    (private / "imports" / "ordinances" / "README.md").write_text("# Downloaded ordinances\n")
    (private / "state").mkdir()
    (private / "state" / "police.json").write_text("{}")         # what the clone must not bring
    log = tmp_path / "git.log"
    _tool(tmp_path / "bin" / "git", '''echo "git $*" >> "$GIT_LOG"
case "$1" in
  clone) d="${@: -1}"; mkdir -p "$d/.git"; echo "url = $5" > "$d/.git/config"; [ -d "$PRIVATE/imports" ] && cp -r "$PRIVATE/imports" "$d/" ;;
esac
exit 0
''')
    env = dict(PATH=f"{tmp_path / 'bin'}:/usr/bin:/bin", GIT_LOG=str(log), PRIVATE=str(private), RUNNER_TEMP=str(rt),
               DATA_TOKEN="secret-data-token", HOME=str(tmp_path))
    r = _bash(run, work, **env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (work / "imports" / "ordinances" / "alpharetta" / "code.html").read_text() == ORD_HTML
    assert not (work / "state").exists() and not any(rt.iterdir()), "only the files stay: the clone, with its token, goes"
    assert "github.com/ClockworkBlack/Crawl.git" in log.read_text()     # config/runner.json data_repo
    assert "sparse-checkout set --no-cone /imports/ordinances/" in log.read_text()
    assert r.stdout.strip() == "1 files from the private repository's imports/ordinances/"
    assert "secret-data-token" not in r.stdout + r.stderr
    # a copy with no file (or no folder) stops the run before anything is read or sent
    shutil.rmtree(private / "imports" / "ordinances" / "alpharetta")
    r = _bash(run, work, **env)
    assert r.returncode == 1 and "::error title=Ordinances::no file came from the private repository" in r.stdout
    shutil.rmtree(private / "imports")
    r = _bash(run, work, **env)
    assert r.returncode == 1 and "::error title=Ordinances::no file came from the private repository" in r.stdout
    assert not (work / "imports" / "ordinances").exists()
    r = _bash(run, work, **{**env, "DATA_TOKEN": ""})
    assert r.returncode == 1 and "::error title=Ordinances::no DATA_TOKEN secret" in r.stdout


def _index(d: Path, n: int):
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(json.dumps({"documents": [{"id": f"x-{i}"} for i in range(n)], "places": {}}))


def test_ordinances_never_sends_a_build_with_no_document(tmp_path):
    import subprocess

    job = _wf("ordinances.yml")["jobs"]["send"]
    electo = tmp_path / "electo"
    run = _step(job, lambda s: s.get("id") == "send")["run"].replace("/tmp/electo", str(electo))
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    src, bare = tmp_path / "src", tmp_path / "electo.git"
    _index(src / "data" / "ordinances", 2)
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(g + ["-C", str(src), "add", "-A"], check=True)
    subprocess.run(g + ["-C", str(src), "commit", "-qm", "start"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(electo)], check=True)
    _tool(tmp_path / "bin" / "rsync", 'src="${@: -2:1}"; dst="${@: -1}"; rm -rf "$dst"; mkdir -p "$dst"; cp -a "$src". "$dst"\n')
    _tool(tmp_path / "bin" / "sleep", "exit 0\n")
    work = tmp_path / "work"
    _index(work / "dist" / "ordinances", 0)
    out, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    env = dict(PATH=f"{tmp_path / 'bin'}:/usr/bin:/bin", GITHUB_OUTPUT=str(out), GITHUB_STEP_SUMMARY=str(summary),
               HOME=str(tmp_path))
    out.write_text("")
    r = _bash(run, work, **env)
    assert r.returncode == 1 and "::error title=Ordinances::this build has no document and Electo has 2: not sent" in r.stdout
    assert out.read_text() == "" and json.loads((electo / "data" / "ordinances" / "index.json").read_text())["documents"]
    log = subprocess.run(["git", "-C", str(bare), "log", "--format=%s", "main"], capture_output=True, text=True).stdout
    assert log.splitlines() == ["start"]
    # a build with documents goes out, and the next steps see it sent
    _index(work / "dist" / "ordinances", 3)
    r = _bash(run, work, **env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.read_text() == "sent=1\n" and "Sent to Electo (data/ordinances/)" in summary.read_text()
    log = subprocess.run(["git", "-C", str(bare), "log", "--format=%s", "main"], capture_output=True, text=True).stdout
    assert log.splitlines()[0].startswith("Downloaded ordinances from Crawl: ")
