"""Text as people read it and one date form (Nov 14, 2026 audit): entities, mis-decoded UTF-8, invisible characters,
feed dates; each case is one the bundle actually carried."""
import json

from gacrawl.store import Record, Store, norm_published
from gacrawl.textfix import fix_text, fix_tree, needs_fix


def test_fix_text_real_cases():
    cases = {
        "TundÃ© Ezekiel": "Tundé Ezekiel", "Vieux CarrÃ¨ Bistro": "Vieux Carrè Bistro", "McCookâ€™s pharmacy": "McCook’s pharmacy",
        "Shen&#233;e Johnson-Cummings": "Shenée Johnson-Cummings", "penalties&#8212;a fine": "penalties—a fine",
        "Georgia&#39;s": "Georgia's", "[&#8230;]": "[…]", "&amp;#8217;": "’", "Q&amp;A": "Q&A", "?a=1&amp;b=2": "?a=1&b=2",
        "a&nbsp;b": "a b", "Karen Mc­Neal": "Karen McNeal", "â€œquoteâ€\x9d": "“quote”",
    }
    for raw, want in cases.items():
        assert fix_text(raw) == want, raw
        assert fix_text(want) == want, want           # idempotent


def test_fix_text_leaves_good_text_and_markup_alone():
    for s in ("José", "São Paulo", "Müller", "naïve café", "Bárbara", "AT&T", "R&D;", "½ cup", "👨‍👩", "plain"):
        assert fix_text(s) == s
    # never turns text into markup: a page that inserts it unescaped is no worse off than before
    assert fix_text("&lt;script&gt;") == "&lt;script&gt;"
    assert fix_text("&#60;b&#62;") == "&lt;b&gt;"
    assert fix_text("&amp;lt;b&amp;gt;") == "&lt;b&gt;"
    assert fix_text("x&#0;y") == "xy"
    assert fix_text(None) is None and fix_text(3) == 3


def test_fix_tree_and_needs_fix():
    assert fix_tree({"k&amp;": ["Ã©", {"x": 1, "y": "&#8217;"}]}) == {"k&amp;": ["é", {"x": 1, "y": "’"}]}
    assert needs_fix('{"t":"a&#8217;b"}') and needs_fix(json.dumps({"n": "TundÃ©"}))   # escaped too
    assert needs_fix(json.dumps({"n": "Mc­Neal"})) and not needs_fix('{"t":"plain"}')


def test_norm_published():
    assert norm_published("Tue, 27 Oct 2026 15:05:21 -0500") == "2026-10-27T15:05:21-05:00"
    assert norm_published("Sat, 31 Oct 2026 18:36:26 GMT") == "2026-10-31T18:36:26+00:00"
    for s in ("2026-10-27", "2026-10-27T10:00:00Z"):
        assert norm_published(s) == s
    assert norm_published("") is None and norm_published(None) is None
    assert norm_published("nonsense") == "nonsense"


def test_records_are_written_and_read_normalized(tmp_path):
    st = Store(tmp_path)
    r = Record("s", "page", "https://x/1", title="Shen&#233;e", text="a&#8217;b", published="Tue, 27 Oct 2026 15:05:21 -0500")
    assert (r.title, r.text, r.published) == ("Shenée", "a’b", "2026-10-27T15:05:21-05:00")
    st.add(r)
    # a row stored before Nov 14, 2026, as it was
    st.db.execute("UPDATE records SET published='Fri, 09 May 2025 20:48:26 +0000', title='Georgia&#39;s'")
    st.flush()
    row = st.query()[0]
    assert row["title"] == "Georgia's" and row["published"] == "2025-05-09T20:48:26+00:00"
    assert st.normalize_published() == 1
    assert st.raw("1")[0]["published"] == "2025-05-09T20:48:26+00:00"
    assert st.normalize_published() == 0              # once per database


def test_connections_never_cut_a_date():
    from gacrawl.connections import _day
    assert _day("Sun, 22 Mar 2026 17:19:04 +0000") == "2026-03-22"
    assert _day("2026-03-22T10:00:00Z") == "2026-03-22" and _day("") == "" and _day("Sun, 22 Ma") == ""


def test_board_page_sentence_and_soft_hyphen():
    from gacrawl.schools import board_members_from_text
    text = ("School Board Members\nBruce Wilson\nChairman, District 3\nKaren Mc­Neal\nMember, District 1\n"
            "On March 26, 2026, the Board of Education appointed Mrs. Karen McNeal to fill the District 1 Vacancy.\n"
            "Robert Murkerson\nMember, District 4")
    got = [(x["name"], x["role"], x["seat"]) for x in board_members_from_text(text)]
    assert got == [("Bruce Wilson", "Chair", "3"), ("Karen McNeal", "Member", "1"), ("Robert Murkerson", "Member", "4")]


def test_federal_summary_entities():
    from xml.etree import ElementTree as ET

    from gacrawl.federal_bills import _t
    e = ET.fromstring("<b><title>Valor Earned Not Stolen Act&#38;#8212;2025</title></b>")
    assert _t(e, "title") == "Valor Earned Not Stolen Act—2025"


def test_history_garbled_name():
    from gacrawl.history.model import nice_name, ungarble_name
    assert nice_name("EMORY �_Ǌ�_ǊBO�_ǐ�_ǐ HEYWARD") == "Emory Bo Heyward"
    assert ungarble_name("Plain Name") == "Plain Name" and nice_name("JOHN H BUCHANAN JR") == "John H. Buchanan Jr."


def test_fix_inplace_changes_the_object_itself():
    from gacrawl.textfix import fix_inplace
    x = {"n": "A womanâ€™s space", "a": {"r": ["&#8217;", {"q": "Ã©"}]}, "k": 1}
    inner = x["a"]
    assert fix_inplace(x) is x and x["a"] is inner
    assert x == {"n": "A woman’s space", "a": {"r": ["’", {"q": "é"}]}, "k": 1}
