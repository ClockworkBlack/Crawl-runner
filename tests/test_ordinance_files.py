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
