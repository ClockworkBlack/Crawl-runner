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
