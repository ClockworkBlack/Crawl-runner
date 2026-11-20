"""A file too big for a web upload arrives in pieces (<name>.part01of03 ...): it is read as one file. Fictional data."""
from gacrawl.ordinance_files import inbox_units


def test_split_upload_is_joined_and_read_whole(tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "bigtown").mkdir(parents=True)
    data = b"%PDF-1.4 " + bytes(range(256)) * 50
    third = len(data) // 3
    for n, chunk in enumerate([data[:third], data[third:2 * third], data[2 * third:]], 1):
        (inbox / "bigtown" / f"Code.pdf.part{n:02d}of03").write_bytes(chunk)
    (inbox / "bigtown" / "Report 2026-11-19 2353.txt").write_text("ORDINANCE GRABBER REPORT")
    units, problems = inbox_units(inbox, tmp_path / "scratch")
    assert problems == []
    assert [(k, label) for k, _, label in units] == [("file", "bigtown/Code.pdf")]   # the report is not a document
    assert units[0][1].read_bytes() == data


def test_split_upload_missing_a_piece_is_reported_not_read(tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "bigtown").mkdir(parents=True)
    for n in (1, 3):
        (inbox / "bigtown" / f"Code.pdf.part{n:02d}of03").write_bytes(b"x")
    units, problems = inbox_units(inbox, tmp_path / "scratch")
    assert units == [] and "only part(s) 1, 3 are here" in problems[0]
