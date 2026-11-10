"""Quotations a record attributes to a candidate become their stances (Nov 9: officeholders whose words live in press
releases, like the governor, had none)."""
from gacrawl.stances import Extractor

KEMP = {"id": "brian-kemp", "name": "Brian Kemp", "aliases": []}


def q(text, cand=KEMP):
    return Extractor(cand).attributed_quotes(text)


def test_said_after_and_the_quotation_that_goes_on():
    t = ("ATLANTA – Governor Brian Kemp today announced 375 new jobs. “For nearly 100 years, Blue Bird has been an incredible "
         "part of Georgia’s story, and that story will continue,” **said Governor Brian Kemp**. “We are proud when our "
         "long-time partners can grow and adapt to changing markets right here in the Peach State.”")
    out = q(t)
    assert len(out) == 2
    assert out[0].startswith("“For nearly 100 years") and out[0].endswith("continue.”")
    assert out[1].startswith("“We are proud when")


def test_someone_else_of_that_surname_or_name():
    t = ("Governor Brian Kemp and First Lady Marty Kemp visited the school. “Every child in Georgia deserves a safe place to "
         "learn and grow,” said First Lady Marty Kemp. “Our state is stronger when we invest in our communities,” said "
         "Commissioner Pat Wilson.")
    assert q(t) == []


def test_said_before_and_title_with_surname():
    t = ("Brian Kemp signed the budget on Monday. Kemp said, “This budget keeps our promise to cut taxes for every working "
         "family in Georgia.” Later, Gov. Kemp said in a statement: “We will keep spending in line with what Georgians can afford.”")
    out = q(t)
    assert out == ["“This budget keeps our promise to cut taxes for every working family in Georgia.”",
                   "“We will keep spending in line with what Georgians can afford.”"]


def test_bare_surname_needs_the_full_name_and_no_other_namesake():
    t = "“We need to fix the roads in this county before anything else,” Smith said at the meeting."
    assert q(t, {"id": "john-smith", "name": "John Smith", "aliases": []}) == []      # never named in full
    t2 = ("John Smith and Mary Smith spoke. “We need to fix the roads in this county before anything else,” Smith said.")
    assert q(t2, {"id": "john-smith", "name": "John Smith", "aliases": []}) == []     # which Smith?
    t3 = ("Commissioner John Smith spoke first. “We need to fix the roads in this county before anything else,” Smith said.")
    assert len(q(t3, {"id": "john-smith", "name": "John Smith", "aliases": []})) == 1


def test_ceremony_and_short_quotes_are_not_stances():
    t = ("“Congratulations to all those who worked hard to support this job creator and its people,” said Governor Brian Kemp. "
         "“Great news,” said Governor Brian Kemp.")
    assert q(t) == []


def test_profile_gets_quoted_positions(tmp_path):
    from gacrawl.store import Record, Store
    from gacrawl.stances import profile_supplement
    st = Store(tmp_path)
    url = "https://gov.georgia.gov/press-releases/2026-11-02/tax-relief"
    full = ("ATLANTA – Governor Brian Kemp today announced property tax relief. “Along with our partners in the General "
            "Assembly, we are delivering on our promise to make life more affordable for hardworking Georgians by providing "
            "needed relief for homeowners from property taxes,” said Governor Brian Kemp.")
    st.add(Record("gov-press", "press_release", url, title="Gov. Kemp Announces Homeowner Tax Relief", text=full,
                  published="Mon, 02 Nov 2026 10:00:00 -0500", evidence="primary"))
    st.add(Record("cand:brian-kemp", "mention", url, title="Gov. Kemp Announces Homeowner Tax Relief",
                  text="… “Along with our partners in the General Assembly, we are …,” **said Governor Brian Kemp** …",
                  published="Mon, 02 Nov 2026 10:00:00 -0500", evidence="primary",
                  meta={"record_kind": "press_release", "record_id": ""}))
    st.flush()
    p = profile_supplement({**KEMP, "office": "Governor", "jurisdictions": []}, st)
    pos = [x for x in p["positions"] if x["basis"].startswith("quoted in")]
    assert pos and pos[0]["topic"] == "Taxes and budget" and pos[0]["evidence"] == "primary"
    assert pos[0]["stance"].endswith("from property taxes.”") and pos[0]["d"] == "2026-11-02"
    assert p["sources"][pos[0]["src"][0]]["u"] == url


def test_headlines_on_their_site_are_not_their_stances():
    ex = Extractor({"id": "park-cannon", "name": "Park Cannon", "aliases": []})
    assert ex.issue_sentences("Georgia House OKs bill to limit transgender care for minors as GOP prevails in party-line vote") == []
    assert ex.own_sentences("The case targeted five provisions of the law, including one that bans watching votes and must be stopped.") == []
    assert ex.issue_sentences("Expand Medicaid to cover working families in Georgia") != []
    assert ex.own_sentences("I will fight to protect access to health care for every family.") != []


def test_business_environment_is_not_environmental():
    from gacrawl.stances import issues_of
    assert "Environment" not in issues_of("our pro-business environment is fostering sustained success for businesses")
    assert "Environment" in issues_of("We must protect the environment and our coastal marshes")


def test_a_letter_about_them_on_their_site_is_not_their_stance():
    ex = Extractor({"id": "amelia-inman", "name": "Amelia Inman", "aliases": []})
    assert ex.someone_elses("Amelia Inman is an excellent community leader and I really appreciate her board for recognizing me.")
    assert not ex.someone_elses("I'm Amelia Inman and I will fight for affordable housing in Lithonia.")
    assert not ex.someone_elses("I will fight for affordable housing in Lithonia.")
