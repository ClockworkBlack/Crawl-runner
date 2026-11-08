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
