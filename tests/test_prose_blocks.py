"""Sentences are cut inside one paragraph of a page, never across its headings, menus, tables and list items
(Nov 9: three in ten of the positions collected from campaign sites carried page furniture, and the offline AI on
Electo was handed lines like "# Supporters of X Public · 1 member" to infer from). Fictional pages."""
from gacrawl.stances import NOISE_SENT, Extractor, prose_blocks, sentences, split_sentences

PAGE = (
    "- [HOME](https://janedoe.example/home)\n- [ISSUES](https://janedoe.example/issues)\n\n"
    "# Where JANE DOE Stands on the ISSUES\n\n#### Smart Growth\n\n"
    "Jane Doe is committed to raising the standard of living across District 2 by focusing on what matters most to families.\n\n"
    "#### Efficient, Transparent Government\n\n"
    "I will fight to **lower property taxes** for seniors. See [my plan](https://janedoe.example/plan) for the county's roads.\n\n"
    "| Education | Georgia State University ( BS ) |\n\n"
    "- I support fully funding our public schools\n- I oppose the new landfill on Mill Road\n\n"
    "Public\n\n·\n\n1 member\n\n"
    "For media inquiries please email: [[email protected]](https://janedoe.example/cdn-cgi/l/email-protection)\n\n"
    "Donate Here\n\nGov. Brian P. Kemp signed the bill. Rep. Smith voted no on it last year.\n"
)


def test_no_sentence_runs_across_a_heading_menu_or_table():
    ss = split_sentences(PAGE)
    assert "Jane Doe is committed to raising the standard of living across District 2 by focusing on what matters most to families." in ss
    assert "I will fight to lower property taxes for seniors." in ss and "See my plan for the county's roads." in ss
    assert not [s for s in ss if "#" in s or "](" in s or "**" in s or "|" in s or "HOME" in s or "Stands on the ISSUES" in s]


def test_list_items_are_sentences_of_their_own_and_initials_do_not_split():
    ss = split_sentences(PAGE)
    assert "I support fully funding our public schools" in ss and "I oppose the new landfill on Mill Road" in ss
    assert "Gov. Brian P. Kemp signed the bill." in ss and "Rep. Smith voted no on it last year." in ss
    assert prose_blocks("One line\nwrapped onto the next.\n\nAnother paragraph.") == ["One line wrapped onto the next.", "Another paragraph."]


def test_buttons_and_protected_addresses_are_not_statements():
    kept = [s for s in sentences(PAGE) if not NOISE_SENT.search(s)]
    assert not [s for s in kept if "Donate" in s or "email" in s or "member" in s]
    own = Extractor({"id": "jane-doe", "name": "Jane Doe"}).own_sentences(PAGE)
    assert "I will fight to lower property taxes for seniors." in own and all("#" not in s for s in own)


def test_site_builder_filler_is_not_a_candidates_statement(tmp_path):
    """A sentence found word for word on three candidates' sites is the site builder's ("Begin by discussing the
    primary theme ..."); each candidate's own sentence stays."""
    from gacrawl.stances import boilerplate, profile_supplement, sentence_key
    from gacrawl.store import Record, Store

    filler_s = "Begin by discussing the primary theme or topic that you plan to cover, and support it with examples."
    store = Store(tmp_path / "data")
    for i, own in enumerate(("I support repaving every county road by 2030.", "I oppose the new landfill on Mill Road.", "I support a property tax cap for seniors.")):
        store.add(Record(f"cand:c{i}", "statement", f"https://person{i}.example/issues", title="Issues", channel="html", evidence="primary",
                         text=f"# Person {i} for Georgia\n\n{filler_s}\n\nPerson {i} here. {own}\n"))
    store.flush()
    f = boilerplate(store)
    assert sentence_key(filler_s) in f and sentence_key("I oppose the new landfill on Mill Road.") not in f
    prof = profile_supplement({"id": "c1", "name": "Person 1"}, store, filler=f)
    texts = [x["stance"] for x in prof["positions"]] + [x["text"] for x in prof["statements"]]
    assert any("landfill on Mill Road" in t for t in texts) and not any("primary theme" in t for t in texts)
    store.close()


def test_issue_digest_is_not_their_site(tmp_path):
    """OnTheIssues and the like collect many people's records on their own pages: never the candidate's own channel."""
    from gacrawl.stances import profile_supplement
    from gacrawl.store import Record, Store
    st = Store(tmp_path)
    st.add(Record("cand:jim-barksdale", "statement", "https://ontheissues.org/SENATE/Jim_Barksdale.htm", title="Jim Barksdale on the Issues",
                  text="Jim Barksdale on the issues. Voted YES on banning partial-birth abortion except to save mother's life.",
                  evidence="primary"))
    st.flush()
    p = profile_supplement({"id": "jim-barksdale", "name": "Jim Barksdale", "aliases": [], "jurisdictions": []}, st)
    assert not p["positions"] and not p["statements"]
