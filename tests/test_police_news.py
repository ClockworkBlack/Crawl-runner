"""Police news: the officers an article names, and the article read without its page furniture.

Nov 19 the Policing tab listed a TV station's bureau chief, two judges, two prosecutors and three deputy coroners as
officers, and 230 of 478 excerpts carried "**TRENDING STORIES:**" link lists, "####" headings and bold marks. The
examples below are the phrases those entries came from.
"""
import json
from pathlib import Path

from gacrawl import extract
from gacrawl import policing_news as pn


def _names(text):
    return [(n["name"], n["rank"]) for n in pn.names_in(text)]


def test_a_rank_of_another_office_names_no_officer():
    # a newsroom's bureau chief, judges, prosecutors, coroners: none of them is a police officer
    for text in [
        "One parent who spoke with **Channel 2 Gwinnett County Bureau Chief Matt Johnson** said she couldn’t understand it.",
        "Channel 2 Gwinnett County Bureau Chief Matt Johnson has learned that there have been changes since the deadly crash.",
        "He was sentenced to 108 months in prison by U.S. District Court Chief Judge R. Stan Baker.",
        "The case was prosecuted by Chief Assistant District Attorney Caley McDonald with assistance from the office.",
        "Deputy Chief Assistant District Attorney Jane Roe said the evidence was overwhelming.",
        "Chief Superior Court Judge Cynthia Adams sentenced Bering on Wednesday to life in prison.",
        "Perry was pronounced dead by Deputy Coroner Richard Robinson. Deputy Coroner Luann Stone also responded.",
        "Jane Roe, Deputy Coroner, said the man died at the scene.",
        "Officers requested assistance from the Cobb police Major Crimes Unit, which is investigating.",
        "The shooting happened after a Major League Baseball Game at Truist Park.",
        "Gwinnett Fire Capt. Ryan Myers said two people died. Fire Chief John Smith agreed.",
        "Chief Financial Officer Jane Smith said the settlement was paid. Code Enforcement Officer Ann Lee was there.",
        "Deputy Chief Ballard said the deputy was placed on leave.",
    ]:
        assert _names(text) == [], text


def test_police_ranks_still_name_their_officers():
    text = ("Marietta police said Officer John Doe shot a man. Chief of Police Jane Q. Doe placed him on leave; "
            "Sgt. Al B. Smith, a supervisor, was also named. Petitioners want a recall election for Sheriff Gerald Couch. "
            "The judge heard from the Court. Officer Brock Marks testified.")
    assert _names(text) == [("John Doe", "Officer"), ("Jane Q. Doe", "Chief of Police"), ("Al B. Smith", "Sgt"),
                            ("Gerald Couch", "Sheriff"), ("Brock Marks", "Officer")]


def test_a_police_post_or_unit_before_the_rank_keeps_the_officer():
    # a department's spokesperson, team, section or chief of staff, then the officer's own rank: the look-back for
    # another office's title ("Chief Information Officer", "Staff Sgt.") rejected the sergeant after "Information
    # Officer", the one after "Team", and so on, and the officer was lost
    for text, want in [
        ("Public Information Officer Cpl. Juan Madera said the man was shot.", ("Juan Madera", "Cpl")),
        ("Savannah Police Department Public Information Officer Sgt. Jane Doe said the officer was shot.", ("Jane Doe", "Sgt")),
        ("SWAT Team Sgt. John Doe fired the shot.", ("John Doe", "Sgt")),
        ("Narcotics Team Investigator John Doe was suspended.", ("John Doe", "Investigator")),
        ("Special Operations Section Lt. John Doe was fired.", ("John Doe", "Lt")),
        ("Chief of Staff Maj. John Doe said the officer was suspended.", ("John Doe", "Maj")),
    ]:
        assert _names(text) == [want], text
    # the export holds a stored name to the same rule
    savannah = "Savannah Police Department Public Information Officer Sgt. Jane Doe said the officer was shot."
    assert pn.recheck_names([{"name": "Jane Doe", "rank": "Sgt"}], savannah) == [{"name": "Jane Doe", "rank": "Sgt"}]
    # another service's post before the rank, or the army's staff sergeant, is still another office's
    for text in ["Fire Investigator Lt. John Doe said the fire was set.", "Court Officer Sgt. John Doe said.",
                 "Staff Sgt. John Doe was shot.", "Army Staff Maj. John Doe said.", "Rescue Team Capt. John Doe said."]:
        assert _names(text) == [], text


def test_two_word_ranks_are_read_whole():
    # "Chief" was tried before "Chief Deputy", and a deputy jailer was listed as "Jailer Michael Davis"
    assert _names("Chief Deputy Gary Taylor said the jail was locked down.") == [("Gary Taylor", "Chief Deputy")]
    assert _names("The sheriff’s office says Deputy Jailer Michael Davis died on Tuesday.") == [("Michael Davis", "Deputy Jailer")]
    assert _names("Sergeant Lieutenant Chris Bryant was there.") == [("Chris Bryant", "Lieutenant")]


def test_names_with_inner_capitals_and_possessives():
    assert _names("A grand jury indicted Deputy Michael McGuffey on Wednesday.") == [("Michael McGuffey", "Deputy")]
    assert _names("Contact Newnan police Ofc. Zack DeBisschop at 770-254-2355.") == [("Zack DeBisschop", "Ofc")]
    assert _names("Police Chief Mike Allen’s decision was criticized.") == [("Mike Allen", "Police Chief")]
    assert _names("Sheriff Gary Long Deputy Rylee Willis was charged.")[0] == ("Gary Long", "Sheriff")
    # a capital inside any word, a double surname, an apostrophe: the whole word or nobody, never its front
    assert _names("Cobb Police Chief Stuart VanHoozer said the officer was placed on leave.") == [("Stuart VanHoozer", "Police Chief")]
    assert _names("Officer JaMarcus Smith was fired.") == [("JaMarcus Smith", "Officer")]
    assert _names("Police Chief Joyce Dent-Smith said the review continues.") == [("Joyce Dent-Smith", "Police Chief")]
    assert _names("Officer Shaquille O’Neal and Det. Ana DeLaCruz responded.") == [("Shaquille O’Neal", "Officer"),
                                                                                    ("Ana DeLaCruz", "Det")]
    assert _names("Officer John McCOY and Police Chief Joyce Dent-MCCOY were there.") == []
    assert _names("Officer John Doe-led teams searched the woods.") == [("John Doe", "Officer")]


def test_state_patrol_ranks_and_the_rank_after_a_comma():
    assert _names("Trooper First Class Jacob Thompson was driving.") == [("Jacob Thompson", "Trooper First Class")]
    assert _names("Sergeant First Class Ann Lee was there.") == [("Ann Lee", "Sergeant First Class")]
    # the rank after the comma begins the next person's name, or the name before it is another office's person
    assert _names("Judge Jane Roe, Deputy Mark Lee said.") == [("Mark Lee", "Deputy")]
    assert _names("Bureau Chief Matt Johnson, Officer Mike Lee said the man fled.") == [("Mike Lee", "Officer")]
    assert _names("Superior Court Judge Jane Roe, Deputy.") == []
    assert _names("The board heard from Jane Roe, Deputy Chief of the department.") == [("Jane Roe", "Deputy Chief")]


def test_names_the_old_reader_cut_short_are_completed_or_left_out():
    """The old reader stopped a name word at its second capital and after a hyphen. Nov 19 the Policing tab listed
    "Zack De" (Newnan, the excerpt reads "Ofc. Zack DeBisschop") and "Joyce Dent-" (Columbus, the excerpt does not
    print the name); trimmed of its hyphen, "Joyce Dent" would look whole."""
    def kept(names, excerpt):
        return [(n["name"], n["rank"]) for n in pn.recheck_names(names, excerpt)]

    debisschop = ("The crash remains under investigation. Anyone with information is asked to contact Newnan police "
                  "Ofc. Zack DeBisschop at [770-254-2355](tel:770-254-2355).")
    assert kept([{"name": "Zack De", "rank": "Ofc"}], debisschop) == [("Zack DeBisschop", "Ofc")]
    columbus = "COLUMBUS, Ga. (WTVM) - A resident brought a complaint accusing Police Chief Stoney Mathis of retaliation."
    assert kept([{"name": "Joyce Dent-", "rank": "Police Chief"}, {"name": "Stoney Mathis", "rank": "Police Chief"}],
                columbus) == [("Stoney Mathis", "Police Chief")]
    assert kept([{"name": "Joyce Dent-", "rank": "Police Chief"}], "Police Chief Joyce Dent-Smith spoke.") == [
        ("Joyce Dent-Smith", "Police Chief")]
    # cut, and the excerpt does not show the rest: left out
    for stored in ("Zack De", "Stuart Van", "Michael Mc", "Shaquille O’"):
        assert kept([{"name": stored, "rank": "Officer"}], "Police said the officer was placed on leave.") == [], stored
    assert kept([{"name": "Stuart Van", "rank": "Police Chief"}], "Cobb Police Chief Stuart VanHoozer spoke.") == [
        ("Stuart VanHoozer", "Police Chief")]
    assert kept([{"name": "Shaquille O’", "rank": "Officer"}], "Officer Shaquille O’Neal’s car") == [("Shaquille O’Neal", "Officer")]
    # an apostrophe after a whole word is a possessive ("Officer John Jones’ lawyer"), not a cut
    for excerpt in ("Officer John Jones’ lawyer said.", "Police said the officer was placed on leave."):
        assert kept([{"name": "John Jones’", "rank": "Officer"}], excerpt) == [("John Jones", "Officer")], excerpt
    # a short surname that is whole: kept, whether the excerpt prints it or not
    assert kept([{"name": "Thanh Le", "rank": "Officer"}], "Officer Thanh Le said.") == [("Thanh Le", "Officer")]
    assert kept([{"name": "Thanh Le", "rank": "Officer"}], "Police said the officer was placed on leave.") == [("Thanh Le", "Officer")]
    assert kept([{"name": "Thanh Le", "rank": "Officer"}], "Officer Thanh Le and John LeBlanc.") == [("Thanh Le", "Officer")]


PAGE ="""<html><head><title>Gwinnett officers fired</title></head><body><article>
<h1>Gwinnett officers fired after camera misuse</h1>
<p><strong>GWINNETT COUNTY, Ga.</strong> - Three Gwinnett County police officers were fired and charged for misusing license
plate cameras, police said.</p>
<p><strong>Channel 2 Gwinnett County Bureau Chief Matt Johnson</strong> reported that Cpl. Matthew Lau’s lawyer declined to comment.</p>
<p><strong>TRENDING STORIES:</strong></p>
<ul><li><a href="https://www.wsbtv.com/news/a/"><strong>Deputy Jailer Michael Davis dies unexpectedly</strong></a></li>
<li><a href="https://www.wsbtv.com/news/b/"><strong>Shaky Knees Festival reopens after severe weather moves out</strong></a></li></ul>
<h4>Lawrenceville vape sale leads to double homicide</h4>
<p>Officer Brock Marks said the department's review continues.</p>
<p><em>Copyright 2026 WTOC. All rights reserved.</em></p>
<p>[<strong>SIGN UP:</strong><a href="https://membercenter.wsbtv.com/newsletters"><strong>WSB-TV Daily Headlines Newsletter</strong></a>]
©2026 Cox Media Group</p>
</article></body></html>"""


def test_article_text_leaves_out_page_furniture():
    _, md = extract.html_to_markdown(PAGE, "https://www.wsbtv.com/news/x/")
    assert "**TRENDING STORIES:**" in md and "](https://" in md and "####" in md
    t = pn.article_text(md)
    for chrome in ("TRENDING", "**", "](", "#", "Shaky Knees", "double homicide", "Copyright", "SIGN UP", "©"):
        assert chrome not in t, chrome
    assert "Three Gwinnett County police officers were fired" in t and "Officer Brock Marks said" in t
    # the trending story's headline named a deputy jailer: not this article's officer
    assert [n["name"] for n in pn.names_in(t)] == ["Matthew Lau", "Brock Marks"]
    ex = pn.excerpt_of(t, "Gwinnett County Police Department", 300)
    assert "fired" in ex and "*" not in ex and "[" not in ex and "Matt Johnson" in ex


def test_an_incident_cue_only_in_another_storys_link_is_no_incident():
    md = ("Police had the area blocked off with crime scene tape at the Gwinnett County apartment complex.\n\n"
          "**TRENDING STORIES:**\n\n"
          "- [**Atlanta USPS employee, former college football standout, dies at 35**](https://www.wsbtv.com/news/local/a/)")
    assert pn.CUE.search(md) and not pn.CUE.search(pn.article_text(md))


# excerpts as the old reader stored them (one line of Markdown)
STORED_TRENDING = ("…lights on once our crew arrived, but police did have the area blocked off with crime scene tape. **TRENDING "
                   "STORIES:** - [**Shaky Knees Festival reopens after severe weather moves out**](https://www.wsbtv.com/news/local/"
                   "hot-saturday-ahead-with-strong-storms/2RFSFPBBXVHQVAHGSCMLTXQHEY/) - [**Atlanta USPS employee, former college "
                   "football standout, dies at 35**](https://www.wsbtv.com/news/local/atlanta/atlanta-usps-employee/IPAKRIMFNFD4LHGPYQR2S5OGQA/)"
                   " - [**Man posing as NFL player may have dozens more catfishing victims, authorities say**](https://www.wsbtv.com/"
                   "news/national/man-posing-nfl-player-may-have-dozens-more-catfishing-victims-authorities-say…")
STORED_BRIEF = ("article Trudon Kassansa booking photo (Credit: Gwinnett County Sheriff's Office) #### The Brief - Police arrested "
                "a man after a deadly hit-and-run on Killian Hill Road near Windcrest Drive in Lilburn. **GWINNETT COUNTY, Ga.** -")
STORED_RELATED = ("was arrested over alleged misuse of Flock systems. (Lowndes County Jail) ##### RELATED COVERAGE ###### Tift County "
                  "deputy demoted after sheriff’s office says he misused data from FLOCK cameras ######")
STORED_COPYRIGHT = ("“The risk of injury or death is too great to be complacent.” *Copyright 2026 WTOC. All rights reserved.* "
                    "[SIGN UP:**[**WSB-TV Daily Headlines Newsletter**](https://membercenter.wsbtv.com/newsletters)**]** ©2026 Cox Media Group")
STORED_SHARE = ("- [Facebook](https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fwww.decaturish.com%2Fnews%2Fdekalb-officer-shot) "
                "- [Twitter](https://twitter.com/intent/tweet?&text=DeKalb%20officer%20shot) - [Email](mailto:?subject=x)")


def test_stored_excerpts_are_cleaned():
    a = pn.clean_excerpt(STORED_TRENDING)
    assert a == "…lights on once our crew arrived, but police did have the area blocked off with crime scene tape."
    b = pn.clean_excerpt(STORED_BRIEF)
    assert "####" not in b and "**" not in b and "The Brief - Police arrested a man" in b and b.endswith("GWINNETT COUNTY, Ga.")
    c = pn.clean_excerpt(STORED_RELATED)
    assert c == "was arrested over alleged misuse of Flock systems. (Lowndes County Jail)"
    d = pn.clean_excerpt(STORED_COPYRIGHT)
    assert d == "“The risk of injury or death is too great to be complacent.”"
    assert pn.clean_excerpt(STORED_SHARE) == ""             # nothing but share buttons
    for x in (a, b, c, d, "Officer [the deputy] said *nothing* new."):
        assert pn.clean_excerpt(x) == x.replace("*", "")    # clean text is left as it is


def _add(st, url, excerpt, names):
    from gacrawl.store import Record

    st.add(Record("police:co-gwinnett", "police_incident", url, title="Story", text=excerpt, published="2026-10-02",
                  channel="html", jurisdiction="co-gwinnett", evidence="reported",
                  meta={"department": "Gwinnett County Police Department", "department_kind": "police", "outlet": "wsbtv.com",
                        "names": names, "cues": ["shot"]}))


def test_export_holds_names_stored_before_to_the_rules(tmp_path: Path):
    """Incidents stored by the old reader are never read again: the export cleans their excerpts and drops, mends or
    keeps each stored name by today's rules."""
    from gacrawl.store import Store

    st = Store(tmp_path / "data")
    _add(st, "https://www.wsbtv.com/1", "**Channel 2 Gwinnett County Bureau Chief Matt Johnson** reported that Cpl. Matthew "
         "Lau’s lawyer declined to comment. A grand jury indicted Deputy Michael McGuffey on Wednesday. Contact **Ofc. Zack "
         "DeBisschop**. **TRENDING STORIES:** - [**A**](https://www.wsbtv.com/a/) - [**B**](https://www.wsbtv.com/b/)",
         [{"name": "Matt Johnson", "rank": "Chief"}, {"name": "Matthew Lau’s", "rank": "Cpl"}, {"name": "Michael Mc", "rank": "Deputy"},
          {"name": "Zack De", "rank": "Ofc"}])
    _add(st, "https://www.wsbtv.com/2", "Chief Deputy Gary Taylor and Deputy Jailer Michael Davis were named. Chief Judge R. Stan "
         "Baker presided; Chief Assistant District Attorney Jane Roe and Deputy Coroner Luann Stone testified. Sheriff Gary Long spoke.",
         [{"name": "Deputy Gary Taylor", "rank": "Chief"}, {"name": "Jailer Michael Davis", "rank": "Deputy"},
          {"name": "Judge R. Stan", "rank": "Chief"}, {"name": "Assistant District Attorney", "rank": "Chief"},
          {"name": "Coroner Luann Stone", "rank": "Deputy"}, {"name": "Crimes Unit", "rank": "Major"},
          {"name": "Chief Ballard", "rank": "Deputy"}, {"name": "Jeremy Mc", "rank": "Sgt"}, {"name": "Gary Long", "rank": "Sheriff"},
          {"name": "Joyce Dent-", "rank": "Police Chief"}])
    st.flush()
    out = tmp_path / "police_incidents.json"
    pn.export_incidents(st, out, [{"id": "co-gwinnett", "name": "Gwinnett County"}])
    st.close()
    D = json.loads(out.read_text())["departments"][0]
    people = {P["name"]: P["ranks"] for P in D["people"]}
    assert people == {"Matthew Lau": ["Cpl"], "Michael McGuffey": ["Deputy"], "Gary Taylor": ["Chief Deputy"],
                      "Michael Davis": ["Deputy Jailer"], "Gary Long": ["Sheriff"], "Zack DeBisschop": ["Ofc"]}
    for i in D["incidents"]:
        assert "**" not in i["excerpt"] and "](" not in i["excerpt"] and "TRENDING" not in i["excerpt"]


def test_outlet_api_stories_are_read_without_furniture(tmp_path: Path):
    from gacrawl.store import Store

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "outlets.yaml").write_text("outlets: [gwinnettdailypost.com]\n")
    st = Store(tmp_path / "data")
    news = pn.PoliceNews(None, st, tmp_path, log=lambda *a: None)
    body = ("<p>Gwinnett County police said Officer Jarrell Greene was suspended after a pursuit in Lawrenceville, Ga.</p>"
            "<p><strong>Channel 2 Gwinnett County Bureau Chief Matt Johnson</strong> was at the scene.</p>"
            "<p><strong>TRENDING STORIES:</strong></p><ul><li><a href='https://x.example/a'>Deputy Jailer Michael Davis dies</a></li>"
            "<li><a href='https://x.example/b'>Another story</a></li></ul>")
    only_link = ("<p>Gwinnett County police held a toy drive in Lawrenceville, Ga.</p><p><strong>TRENDING STORIES:</strong></p>"
                 "<ul><li><a href='https://x.example/c'>Man shot on I-85</a></li><li><a href='https://x.example/d'>More</a></li></ul>")
    stories = [{"url": "https://www.gwinnettdailypost.com/1", "title": "Officer suspended", "date": "2026-10-02",
                "outlet": "gwinnettdailypost.com", "text": extract.html_to_markdown(body, "https://www.gwinnettdailypost.com/1")[1]},
               {"url": "https://www.gwinnettdailypost.com/2", "title": "Toy drive", "date": "2026-10-03",
                "outlet": "gwinnettdailypost.com", "text": extract.html_to_markdown(only_link, "https://www.gwinnettdailypost.com/2")[1]}]
    news.area_apis = lambda j, county: {"gwinnettdailypost.com": "https://www.gwinnettdailypost.com/wp-json"}
    news.wp_search = lambda phrase, apis: stories
    dept = {"name": "Gwinnett County Police Department", "kind": "police"}
    n = news._from_outlet_apis("co-gwinnett", dept, "Gwinnett County", {"name": "Gwinnett County", "kind": "county"},
                               "Gwinnett County police", "Gwinnett")
    st.flush()
    recs = list(st.iter_query("kind='police_incident'"))
    st.close()
    assert n == 1 and [r["url"] for r in recs] == ["https://www.gwinnettdailypost.com/1"]
    meta = recs[0]["meta"] if isinstance(recs[0]["meta"], dict) else json.loads(recs[0]["meta"])
    assert meta["names"] == [{"name": "Jarrell Greene", "rank": "Officer"}] and meta["cues"] == ["pursuit", "suspended"]
    assert "**" not in recs[0]["text"] and "TRENDING" not in recs[0]["text"] and "Michael Davis" not in recs[0]["text"]
