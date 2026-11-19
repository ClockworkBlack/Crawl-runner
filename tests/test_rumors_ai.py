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
    assert keep == {"named": False, "same_person": None, "quote": "", "attribution": "none", "opinion": True}


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


def test_a_failed_reading_is_not_asked_again_until_the_text_changes(tmp_path, monkeypatch):
    from gacrawl import llm
    (tmp_path / "data" / "rumor_mill").mkdir(parents=True)
    rm = RumorMill(tmp_path)
    text = "Commissioner Lee Ward voted against the budget, saying it raised taxes. " * 4
    rm.add("lee-ward", "https://news.example.com/a", "Ward and the budget", "article", meta={"text": text})
    rm.flush()
    asked = []
    monkeypatch.setattr(llm, "_LOGGED", {})
    monkeypatch.setattr(llm, "chat", lambda msgs, **k: (asked.append(1), '{"named": true, "quote": "Ward resigned.", "attribution": "none"}')[1])
    cands = [{"id": "lee-ward", "name": "Lee Ward"}]
    st = rumors_ai.run(tmp_path, cands, time.monotonic() + 60, log=lambda *a: None)
    assert st["read"] == 1 and st["kept"] == 0 and len(asked) == 1
    st = rumors_ai.run(tmp_path, cands, time.monotonic() + 60, log=lambda *a: None)          # the same text: not asked
    assert st["read"] == 0 and st["same"] == 1 and len(asked) == 1
    rm = RumorMill(tmp_path)
    rm.all()[0]["meta"]["text"] = text + "Ward later told reporters the vote was about schools."
    rm._dirty = True
    rm.flush()
    st = rumors_ai.run(tmp_path, cands, time.monotonic() + 60, log=lambda *a: None)          # new text: asked again
    assert st["read"] == 1 and len(asked) == 2



# ---------------------------------------------------------------- namesakes (Nov 19, 2026 audit: 7 of 30 rated items)
# the pages' own text, as the Rumor Mill kept it for the items the audit found under the wrong person
LEWIS = ("… You, Baby.” Later, John Anderson found chart success with “Mississippi Moon.” ## Videos by American Songwriter "
         "White was more than a songwriter, though. His recording credits were also impressive. He recorded with Jerry Lee "
         "Lewis and a long list of Memphis legends for Lewis’ album *Southern Roots: Back Home to Memphis.*He also toured "
         "with Steppenwolf, Sly & the Family Stone, Creedence Clearwater Revival, Waylon Jennings, Joe Cocker, and Eric Clapton. …")
CRUZ = ("… delivers remarks in the viewing area in Emancipation Hall following the 2025 Presidential Inauguration ceremony at "
        "the U.S. Capitol. Standing behind Trump are from left, Speaker of the House Mike Johnson, R-La., his wife, Kelly "
        "Johnson, House Majority Leader Rep. Steve Scalise, R-La. and his wife, Jennifer Scalise. (Jasper Colt, USA TODAY) "
        "President-elect Donald Trump attends his inauguration in the U.S. Capitol Rotunda on January 20, 2025 in Washington, …")
HAMILTON = ("… Wamp](https://newschannel9.com/topic/Weston%20Wamp) - [Deadlocked vote](https://newschannel9.com/topic/Deadlocked%20vote) "
            "HAMILTON COUNTY, Tenn. — County Commission Chairman Ken Smith and Vice Chairman Lee Helton will keep their "
            "leadership roles for now after attempts by Democrats to replace them ended in deadlocked votes. For chair, all "
            "the Republican members voted for current chair Ken Smith. Democrat …")
DAWSON = ("… Point\" will appreciate the classic show's iconic love triangle, the timeless '90s vibes, the existential angst, "
          "and the sense of realism the show tried (but didn't always succeed) to maintain. Created by \"Scream\" writer "
          "Kevin Williamson, \"Dawson's Creek\" stars James Van Der Beek as Dawson Leery, an aspiring filmmaker living in "
          "Capeside, Massachusetts. He's joined by his friends Joey Potter (Katie Holmes), who has a secret crush on him …")
SCREAM = ("… massive cliffhanger. But, as Chucky once said, \"You just can't keep a Good Guy down.\" Mancini teased that he is "
          "working on a new movie that will pick up where the show left off. ## Scream: The TV Series MTV Inspired by Kevin "
          "Williamson and Wes Craven's hit \"Scream\" film franchise, MTV's \"Scream: The TV Series\" translated the "
          "meta-slasher formula into a serialized teen drama …")
MEMPHIS = ("… None We Make It; Framing Memphis’s Black Sound; Hooks School of Photography; and A Revelatory Movement: On Social "
           "and Political Uplift. The exhibition will be accompanied by an illustrated catalogue featuring essays by C. Rose "
           "Smith, Assistant Curator of Photography, Memphis Art Museum, and scholars of photography Dr. Rikki Byrd. …")
# who they are on Georgia's ballots (Electo's races)
WILLIAMSON = {"id": "kevin-williamson", "name": "Kevin Williamson", "office": "County BOE - Post 3", "counties": ["Banks"],
              "races": [{"office": "County BOE - Post 3 (Republican primary)", "county": "Banks"},
                        {"office": "County BOE - Post 3", "county": "Banks"}]}
LEE_LEWIS = {"id": "lee-lewis", "name": "Lee Lewis", "office": "County Commissioner Chairman (Republican primary)",
             "counties": ["Appling"], "races": [{"office": "County Commissioner Chairman (Republican primary)", "county": "Appling"}]}
KELLY = {"id": "kelly-johnson", "name": "Kelly Johnson", "office": "State Senate District 35 (Democratic primary)",
         "races": [{"office": "State Senate District 35 (Democratic primary)", "county": ""}]}
KEN = {"id": "ken-smith", "name": "Ken Smith", "office": "Mayor - Bartow (elected 2025-11-04)", "jurisdiction": "bartow"}
JURIS = {"bartow": {"id": "bartow", "name": "Bartow", "kind": "town", "counties": ["Jefferson"]},
         "co-banks": {"id": "co-banks", "name": "Banks County", "kind": "county"},
         "homer": {"id": "homer", "name": "Homer", "kind": "town", "counties": ["Banks"]}}


def test_the_question_says_who_the_person_is_and_where():
    place = rumors_ai.place_of(WILLIAMSON, JURIS)
    assert place == ["Banks County"]
    assert rumors_ai.who(WILLIAMSON, place) == "a Georgia candidate or official: County BOE - Post 3 (Banks County, Georgia)"
    # a hand-written entry: the town is in the office's own name, its county comes from the registry
    assert rumors_ai.place_of(KEN, JURIS) == ["Jefferson County"]
    assert "Mayor - Bartow (elected 2025-11-04) (Jefferson County, Georgia)" in rumors_ai.who(KEN, rumors_ai.place_of(KEN, JURIS))
    assert rumors_ai.who(KELLY, rumors_ai.place_of(KELLY, JURIS)).endswith("State Senate District 35 (Democratic primary) (Georgia)")
    # a presidential primary's candidates are on Georgia's ballot but serve elsewhere
    assert "national figure" in rumors_ai.who({"name": "Ted Cruz", "office": "President of the United States"})
    assert "not known" in rumors_ai.who({}) and "Georgia" in rumors_ai.who({})
    q = rumors_ai.ASK.format(name="Kevin Williamson", who=rumors_ai.who(WILLIAMSON, place), domain="tvline.com", text=DAWSON)
    assert '"same_person"' in q and "Banks County, Georgia" in q and "someone else with the same name" in q
    assert rumors_ai.VERSION >= 2          # the readings of version 1 (no same_person) are read again


def test_same_person_is_kept_next_to_named():
    base = {"named": True, "quote": "Dickens said the party was abandoning Black voters.", "attribution": "named source"}
    for given, want in ((True, True), (False, False), (None, None), ("false", False), ("true", True), ("unknown", None)):
        keep, why = verdict({**base, "same_person": given}, "Andre Dickens", TEXT)
        assert not why and keep["same_person"] is want and list(keep)[:2] == ["named", "same_person"]
    keep, _ = verdict(base, "Andre Dickens", TEXT)                        # an answer without it keeps today's rating
    assert keep["same_person"] is None
    # the Scream writer under a Banks County school board member: the model's false is kept
    keep, why = verdict({"named": True, "same_person": False, "attribution": "named source",
                         "quote": "MTV Inspired by Kevin Williamson and Wes Craven's hit \"Scream\" film franchise, MTV's "
                                  "\"Scream: The TV Series\" translated the meta-slasher formula into a serialized teen drama"},
                        "Kevin Williamson", SCREAM)
    assert not why and keep["same_person"] is False


def test_a_quote_inside_a_longer_name_is_not_them():
    q = "He recorded with Jerry Lee Lewis and a long list of Memphis legends for Lewis’ album *Southern Roots: Back Home to Memphis.*"
    keep, why = verdict({"named": True, "same_person": True, "quote": q, "attribution": "named source"}, "Lee Lewis", LEWIS)
    assert not why and keep["same_person"] is False
    # the same quote when the text also names Lee Lewis on his own: the quote is about someone else, so it is refused
    both = LEWIS + " Lee Lewis, who is running for commission chairman, said he grew up on that record."
    keep, why = verdict({"named": True, "same_person": True, "quote": q, "attribution": "named source"}, "Lee Lewis", both)
    assert keep is None and "Jerry Lee Lewis" in why[0]
    # "not named" is right when the name is only inside a longer one (it used to be refused: the text has both words)
    keep, why = verdict({"named": False, "quote": "", "attribution": "none"}, "Lee Lewis", LEWIS)
    assert not why and keep["named"] is False and keep["same_person"] is None
    # a title before the name is not a longer name
    assert not rumors_ai._longer_only("Commissioner Lee Lewis said the budget was late.", "Lee Lewis")


def test_plain_namesakes_are_settled_without_asking():
    given = {"jerry", "kelly", "kevin", "ken", "rose", "mike", "emergency"} - {"emergency"}
    ga = ["Banks County", "Homer", "Jefferson County", "Bartow", "Appling County"]
    # "Jerry Lee Lewis" under Lee Lewis
    assert "Jerry Lee Lewis" in rumors_ai.namesake("Lee Lewis", LEWIS, ga, "americansongwriter.com", given=given,
                                                   office=LEE_LEWIS["office"])
    assert "Jerry Lee Lewis" in rumors_ai.namesake("Lee Lewis", LEWIS, ga)           # no list of given names at hand
    # a Louisiana congressman's wife under a State Senate candidate
    assert "R-La." in rumors_ai.namesake("Kelly Johnson", CRUZ, ga, "yahoo.com", "Trump praises Ted Cruz amid 2028 "
                                         "presidential speculation", given, KELLY["office"])
    # the Hamilton County, Tennessee commission chairman under the mayor of Bartow
    assert "Tenn." in rumors_ai.namesake("Ken Smith", HAMILTON, ga, "newschannel9.com", given=given, office=KEN["office"])
    # the writer of "Scream" (the show he created is set in Massachusetts)
    assert "Massachusetts" in rumors_ai.namesake("Kevin Williamson", DAWSON, ga, "tvline.com", given=given,
                                                 office=WILLIAMSON["office"])
    # left to the model: nothing names another state (the Memphis curator, the Scream piece) ...
    assert rumors_ai.namesake("Rose Smith", MEMPHIS, ga, "artdaily.com", given=given, office="Alapaha City Council - Post 4") == ""
    assert rumors_ai.namesake("Kevin Williamson", SCREAM, ga, "tvline.com", given=given, office=WILLIAMSON["office"]) == ""
    # ... the office is not known (a job with no Electo people), or is the presidency (Ted Cruz of Texas is the one on
    # Georgia's 2016 ballot) ...
    assert rumors_ai.namesake("Kelly Johnson", CRUZ, ga, "yahoo.com", given=given) == ""
    tx = "Sen. Ted Cruz of Texas said on Sunday he would not rule out a 2028 run for president, his aides told reporters."
    assert rumors_ai.namesake("Ted Cruz", tx, ga, given=given, office="President of the United States") == ""
    # ... or the text names a Georgia place: Georgia, a county, a town of the person's county, R-Ga.
    home = ("Banks County school board member Kevin Williamson, who moved here from Ohio in 2004, said the board would "
            "take up the budget at its next meeting.")
    assert rumors_ai.namesake("Kevin Williamson", home, ga, given=given, office=WILLIAMSON["office"]) == ""
    town = "Kevin Williamson of Homer, who coached in Ohio for a decade, spoke at the meeting on the school budget."
    assert rumors_ai.namesake("Kevin Williamson", town, ga, given=given, office=WILLIAMSON["office"]) == ""
    mcc = ("Representative and former Emergency Physician Rich McCormick (R-Ga.) commended the investment, highlighting "
           "that strengthening hospitals is vital.")
    assert rumors_ai.namesake("Rich McCormick", mcc, ga, given=given, office="U.S. House District 7") == ""
    # another state's governor beside them in one sentence is not where they are
    bottoms = ("You had David Crowley elected as Wisconsin's first Black governor, Keisha Lance Bottoms, the country's "
               "first Black woman governor, Byron Donalds, who was elected to become Florida's first Black governor.")
    assert rumors_ai.namesake("Keisha Lance Bottoms", bottoms, ga, given=given, office="Governor") == ""
    # national news names Georgia's statewide and federal officials beside other states' without saying Georgia: the
    # state check is for offices below the statewide level (by the office's name, or by the level of its races)
    for nm, text, office in (
            ("Lucy McBath", "Lucy McBath and Jahana Hayes of Connecticut won their primaries on Tuesday, keeping both "
                            "seats.", MCBATH["office"]),
            ("Jon Ossoff", "Sens. Jon Ossoff and Mark Kelly, D-Ariz., introduced the bill to cap insulin prices for "
                           "veterans.", OSSOFF["office"]),
            ("Jon Ossoff", "Sen. Jon Ossoff and Sen. Ron Johnson of Wisconsin asked the agency for the records.",
             OSSOFF["office"]),
            ("Keisha Lance Bottoms", "David Crowley elected governor in Wisconsin, Keisha Lance Bottoms, the country's "
                                     "first Black woman governor.", "Governor (elected Nov. 3, 2026)")):
        assert rumors_ai.namesake(nm, text, ga, given=given, office=office) == "", (nm, text)
    psc = {"name": "Pat Doe", "office": "PSC - District 3", "races": [{"office": "PSC - District 3", "county": "",
                                                                        "level": "statewide"}]}
    tx = "Pat Doe of Texas said the utility's rates would rise next year, the commission's filing shows."
    assert rumors_ai.namesake("Pat Doe", tx, ga, office=psc["office"], level=rumors_ai.office_level(psc)) == ""
    assert "Texas" in rumors_ai.namesake("Pat Doe", tx, ga, office=psc["office"], level="local")
    # ... while a longer name is someone else at any level
    assert "Jerry Lee Lewis" in rumors_ai.namesake("Lee Lewis", LEWIS, ga, office="U.S. Senate", level="federal")
    # a surname alone is not the person ("Sheriff Smith" under Ken Smith), and a state that is also a given name and is
    # followed by a surname is a person ("Virginia Jones"); "Richmond, Virginia" and "Virginia Gov." are the state
    assert rumors_ai.namesake("Ken Smith", "Sheriff Smith said deputies went to Tennessee to pick up the suspect last "
                                           "week.", ga, office="Sheriff") == ""
    assert rumors_ai.namesake("Ken Smith", "Commissioners Ken Smith and Virginia Jones voted to approve the budget on "
                                           "Tuesday.", ga, office="County Commission") == ""
    assert "Virginia" in rumors_ai.namesake("Ken Smith", "Ken Smith, a lawyer in Richmond, Virginia, said he would "
                                                         "appeal.", ga, office="County Commission")
    assert "Virginia" in rumors_ai.namesake("Ken Smith", "Virginia Gov. Glenn Youngkin and Ken Smith toured the plant.",
                                            ga, office="County Commission")


# Electo's races for them (the county results row of a statewide race is marked "local")
JOHN_KING = {"id": "john-king", "name": "John King", "office": "Commissioner of Insurance", "jurisdiction": "co-bacon",
             "counties": ["Bacon"], "levels": ["local", "statewide"],
             "races": [{"office": "Commissioner of Insurance (Republican primary)", "county": "", "level": "statewide"},
                       {"office": "Commissioner of Insurance", "county": "", "level": "statewide"},
                       {"office": "Commissioner of Insurance", "county": "Bacon", "level": "local"}]}
FITZ = {"id": "fitz-johnson", "name": "Fitz Johnson", "office": "Public Service Commission District 3",
        "jurisdiction": "co-baldwin", "counties": ["Baldwin"], "levels": ["local", "statewide"],
        "races": [{"office": "Public Service Commission District 3", "county": "", "level": "statewide"},
                  {"office": "PSC - District 3", "county": "Baldwin", "level": "local"}]}
DICKENS = {"id": "andre-dickens", "name": "Andre Dickens", "office": "Atlanta Mayor (elected 2025-11-04)",
           "jurisdiction": "atlanta", "counties": ["DeKalb", "Fulton"], "levels": ["local"],
           "races": [{"office": "Mayor, City of Atlanta", "county": "", "level": "local"},
                     {"office": "Atlanta Mayor", "county": "DeKalb", "level": "local"},
                     {"office": "Atlanta Mayor", "county": "Fulton", "level": "local"}]}
MICHAEL_SMITH = {"id": "michael-smith", "name": "Michael Smith", "jurisdiction": "co-lowndes", "counties": ["Lowndes"],
                 "office": "State Representative, District 41; State House District 41 (elected Nov. 3, 2026)",
                 "levels": ["legislative", "local"],
                 "races": [{"office": "State House District 41", "county": "", "level": "legislative"},
                           {"office": "County Commission - District 5", "county": "Lowndes", "level": "local"}]}
OSSOFF = {"id": "jon-ossoff", "name": "Jon Ossoff", "office": "U.S. Senate (elected Nov. 3, 2026)", "levels": ["federal"],
          "races": [{"office": "U.S. Senate", "county": "", "level": "federal"}]}
MCBATH = {"id": "lucy-mcbath", "name": "Lucy McBath", "levels": ["federal"],
          "office": "U.S. Representative, District 6; U.S. House District 6 (elected Nov. 3, 2026)",
          "races": [{"office": "U.S. House District 6", "county": "", "level": "federal"}]}
MORE_JURIS = {**JURIS, "co-bacon": {"id": "co-bacon", "name": "Bacon County", "kind": "county"},
              "co-baldwin": {"id": "co-baldwin", "name": "Baldwin County", "kind": "county"},
              "co-lowndes": {"id": "co-lowndes", "name": "Lowndes County", "kind": "county"},
              "atlanta": {"id": "atlanta", "name": "Atlanta", "kind": "city", "counties": ["DeKalb", "Fulton"]}}


def test_a_statewide_or_district_office_is_put_in_no_county():
    # John King, Commissioner of Insurance, shares his name with CNN's; the question put him in Bacon County (his race's
    # one county results row, and the jurisdiction Electo gave him from it), where the model was told to say false
    assert rumors_ai.office_level(JOHN_KING) == "statewide" and rumors_ai.place_of(JOHN_KING, MORE_JURIS) == []
    assert "Bacon" not in rumors_ai.who(JOHN_KING, rumors_ai.place_of(JOHN_KING, MORE_JURIS))
    assert rumors_ai.who(JOHN_KING, []) == "a Georgia candidate or official: Commissioner of Insurance (Georgia; a statewide office)"
    # the races alone say so, for an office whose name does not
    no_name = {**JOHN_KING, "office": "Insurance Commissioner"}
    no_name["races"] = [dict(r, office="Insurance Commissioner") for r in JOHN_KING["races"]]
    assert rumors_ai.office_level(no_name) == "statewide" and rumors_ai.place_of(no_name, MORE_JURIS) == []
    assert rumors_ai.place_of(FITZ, MORE_JURIS) == []                 # not Baldwin County (a "PSC - District 3" row)
    assert rumors_ai.office_level(OSSOFF) == "federal" and "federal office" in rumors_ai.who(OSSOFF, [])
    assert rumors_ai.office_level(MCBATH) == "federal"
    # a legislator's district spans counties: not the county of the commission seat they held before
    assert rumors_ai.office_level(MICHAEL_SMITH) == "legislative" and rumors_ai.place_of(MICHAEL_SMITH, MORE_JURIS) == []
    hand = {"name": "Michael Smith", "office": "State Representative, District 41", "jurisdiction": "co-lowndes",
            "races": MICHAEL_SMITH["races"]}                          # the hand-written office's words match the ballot's
    assert rumors_ai.office_level(hand) == "legislative" and rumors_ai.place_of(hand, MORE_JURIS) == []
    # a local office in two counties is in both (it used to say the last one)
    assert rumors_ai.place_of(DICKENS, MORE_JURIS) == ["DeKalb County", "Fulton County"]
    # an office that names its county does not repeat it
    judge = {"name": "Eric Richardson", "office": "Judge - Fulton County State Court (Richardson)", "jurisdiction": "co-fulton",
             "races": [{"office": "Judge - Fulton County State Court (Richardson)", "county": "Fulton", "level": "local"}]}
    assert rumors_ai.place_of(judge, MORE_JURIS) == []
    # the hand-written and local entries keep their places
    assert rumors_ai.office_level(KEN) == "" and rumors_ai.place_of(KEN, JURIS) == ["Jefferson County"]
    assert rumors_ai.place_of(WILLIAMSON, JURIS) == ["Banks County"]
    # the place Electo writes after a shared name is kept when the office does not say it already
    assert rumors_ai.place_of({"name": "Pat Roe (Homer)", "office": "City Council - Post 2"}, JURIS) == ["Homer"]
    assert rumors_ai.place_of({"name": "Andrew Ferguson (Demorest)", "office": "Council Member - Demorest"}, JURIS) == []


def _rumor_root(tmp_path, rows):
    (tmp_path / "data" / "rumor_mill").mkdir(parents=True)
    (tmp_path / "data" / "rumor_mill" / "rumors.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_run_settles_namesakes_and_keeps_same_person(tmp_path, monkeypatch):
    from gacrawl import llm
    old = {"v": 1, "m": "older model", "at": "2026-11-15", "named": True, "quote": "x", "attribution": "named source",
           "opinion": False}
    georgia = ("Banks County Board of Education member Kevin Williamson said Tuesday the board would hold a hearing on "
               "the school budget in Homer next month, and that parents can write to the board before then. ") * 2
    _rumor_root(tmp_path, [
        {"id": "r00857", "candidate_id": "lee-lewis", "claim": "On This Day in 2018, the Swamp Rock Pioneer", "url": "https://a.example/1",
         "domain": "americansongwriter.com", "status": "unverified", "last_seen": "2026-11-15", "meta": {"text": LEWIS}, "ai": old},
        {"id": "r00866", "candidate_id": "kevin-williamson", "claim": "Horror TV shows based on slasher movies", "url": "https://b.example/2",
         "domain": "tvline.com", "status": "unverified", "last_seen": "2026-11-14", "meta": {"text": SCREAM}, "ai": old},
        {"id": "r00900", "candidate_id": "kevin-williamson", "claim": "Board sets budget hearing", "url": "https://c.example/3",
         "domain": "bankscountynews.example", "status": "unverified", "last_seen": "2026-11-13", "meta": {"text": georgia}},
    ])
    asked = []

    def chat(msgs, **k):
        asked.append(msgs[1]["content"])
        if "Scream" in msgs[1]["content"]:
            return ('{"named": true, "quote": "MTV Inspired by Kevin Williamson and Wes Craven\'s hit \\"Scream\\" film '
                    'franchise", "same_person": false, "attribution": "none", "opinion": false}')
        return ('{"named": true, "quote": "Banks County Board of Education member Kevin Williamson said Tuesday the board '
                'would hold a hearing on the school budget in Homer next month, and that parents can write to the board '
                'before then.", "same_person": true, "attribution": "named source", "opinion": false}')

    monkeypatch.setattr(llm, "_LOGGED", {})
    monkeypatch.setattr(llm, "chat", chat)
    # the given names come from everyone on the ballots: "Jerry" is one because a Jerry ran
    cands = [WILLIAMSON, LEE_LEWIS, {"id": "jerry-moore", "name": "Jerry Moore"}]
    st = rumors_ai.run(tmp_path, cands, time.monotonic() + 60, log=lambda *a: None)
    assert st["rule"] == 1 and st["read"] == 2 and st["kept"] == 2 and st["left"] == 0
    assert len(asked) == 2 and all("County BOE - Post 3 (Banks County, Georgia)" in q for q in asked)
    by = {r["id"]: r for r in RumorMill(tmp_path).all()}
    # Jerry Lee Lewis: not asked, settled as someone else (the reading of version 1 is replaced)
    a = by["r00857"]["ai"]
    assert a["v"] == rumors_ai.VERSION and a["m"] == "rule" and a["same_person"] is False and a["named"] is True
    assert "Jerry Lee Lewis" in a["why"] and "Jerry Lee Lewis" in a["quote"]
    # the Scream writer: read again (version 1 had no same_person), the model's false kept beside named
    assert by["r00866"]["ai"]["v"] == rumors_ai.VERSION and by["r00866"]["ai"]["same_person"] is False
    assert by["r00900"]["ai"]["same_person"] is True
    log = [json.loads(x) for x in (tmp_path / "data" / "train" / "rumor_log.jsonl").read_text().splitlines()]
    assert {(x["task"], x["rumor"], x["same_person"]) for x in log} == {
        ("rumor_rule", "r00857", False), ("rumor_reading", "r00866", False), ("rumor_reading", "r00900", True)}
    assert next(x for x in log if x["task"] == "rumor_rule")["model"] == "rule"
    # the next run has nothing left to read
    st = rumors_ai.run(tmp_path, cands, time.monotonic() + 60, log=lambda *a: None)
    assert st["todo"] == 0 and len(asked) == 2


def test_run_asks_about_a_senator_beside_another_state_and_quotes_the_rule_sentence(tmp_path, monkeypatch):
    from gacrawl import llm
    nav = "- [County Commission](https://n.example/t/cc) - [Ken Smith](https://n.example/t/ks) - [Partisanship](https://n.example/t/p)\n"
    senate = ("Sens. Jon Ossoff and Mark Kelly, D-Ariz., introduced the bill on Tuesday to cap what veterans pay for "
              "insulin, and said the agency had not answered their letters. ") * 2
    ferguson = ("Council Member Andrew Ferguson said the city would repave the square before the festival, and that the "
                "council would vote on the paving contract next month. ") * 2
    _rumor_root(tmp_path, [
        {"id": "r1", "candidate_id": "jon-ossoff", "claim": "Senators introduce insulin bill", "url": "https://n.example/1",
         "domain": "news.example", "status": "unverified", "last_seen": "2026-11-18", "meta": {"text": senate}},
        {"id": "r2", "candidate_id": "ken-smith", "claim": "Commission keeps its chairman", "url": "https://n.example/2",
         "domain": "newschannel9.com", "status": "unverified", "last_seen": "2026-11-17", "meta": {"text": nav + HAMILTON}},
        {"id": "r3", "candidate_id": "andrew-ferguson-demorest", "claim": "Square to be repaved", "url": "https://n.example/3",
         "domain": "news.example", "status": "unverified", "last_seen": "2026-11-16", "meta": {"text": ferguson}},
    ])
    asked = []

    def chat(msgs, **k):
        asked.append(msgs[1]["content"])
        return '{"named": false, "quote": "", "same_person": null, "attribution": "none", "opinion": false}'

    monkeypatch.setattr(llm, "_LOGGED", {})
    monkeypatch.setattr(llm, "chat", chat)
    fer = {"id": "andrew-ferguson-demorest", "name": "Andrew Ferguson (Demorest)", "office": "Council Member - Demorest",
           "jurisdiction": "demorest"}
    st = rumors_ai.run(tmp_path, [OSSOFF, KEN, fer], time.monotonic() + 60, log=lambda *a: None)
    # the senator is asked about, not settled as someone else; the Tennessee commissioner is
    assert st["rule"] == 1 and len(asked) == 2 and any("Jon Ossoff" in q and "federal office" in q for q in asked)
    by = {r["id"]: r for r in RumorMill(tmp_path).all()}
    assert by["r2"]["ai"]["m"] == "rule" and by["r2"]["ai"]["same_person"] is False
    # its quote is the sentence the rule rests on, not the page's list of links that names him first
    assert "HAMILTON COUNTY, Tenn." in by["r2"]["ai"]["quote"] and "Chairman Ken Smith" in by["r2"]["ai"]["quote"]
    # Electo's "(Demorest)" is not part of the name the question asks about
    q = next(x for x in asked if "Ferguson" in x)
    assert q.startswith("Person: Andrew Ferguson\n") and "someone called Andrew Ferguson," in q and "(Demorest)" not in q


def test_run_still_settles_namesakes_when_the_model_fails(tmp_path, monkeypatch):
    from gacrawl import llm
    text = "Commissioner Lee Ward voted against the budget, saying it raised taxes. " * 4
    _rumor_root(tmp_path, [
        {"id": "r1", "candidate_id": "lee-ward", "claim": "Ward and the budget", "url": "https://n.example/a",
         "status": "unverified", "last_seen": "2026-11-18", "meta": {"text": text}},
        {"id": "r2", "candidate_id": "lee-lewis", "claim": "On This Day", "url": "https://n.example/b",
         "domain": "americansongwriter.com", "status": "unverified", "last_seen": "2026-11-17", "meta": {"text": LEWIS}}])
    calls = []

    def chat(msgs, **k):
        calls.append(1)
        raise ConnectionError("the model server is down")

    monkeypatch.setattr(llm, "_LOGGED", {})
    monkeypatch.setattr(llm, "chat", chat)
    cands = [{"id": "lee-ward", "name": "Lee Ward"}, LEE_LEWIS, {"id": "jerry-moore", "name": "Jerry Moore"}]
    st = rumors_ai.run(tmp_path, cands, time.monotonic() + 60, log=lambda *a: None)
    # the model was tried once; the item after it, which needs no model, was settled all the same
    assert len(calls) == 1 and st["rule"] == 1 and st["read"] == 0 and st["no_office"] == 0 and st["left"] == 1
    assert {r["id"]: r.get("ai", {}).get("m") for r in RumorMill(tmp_path).all()} == {"r1": None, "r2": "rule"}


def test_run_says_when_the_office_is_unknown(tmp_path, monkeypatch):
    from gacrawl import llm
    text = "Commissioner Lee Ward voted against the budget, saying it raised taxes. " * 4
    _rumor_root(tmp_path, [{"id": "r1", "candidate_id": "lee-ward", "claim": "Ward and the budget", "url": "https://n.example/a",
                            "status": "unverified", "last_seen": "2026-11-18", "meta": {"text": text}}])
    monkeypatch.setattr(llm, "_LOGGED", {})
    monkeypatch.setattr(llm, "chat", lambda msgs, **k: '{"named": true, "quote": "Commissioner Lee Ward voted against the '
                                                       'budget, saying it raised taxes.", "attribution": "none"}')
    lines = []
    st = rumors_ai.run(tmp_path, [{"id": "lee-ward", "name": "Lee Ward"}], time.monotonic() + 60, log=lines.append)
    assert st["no_office"] == 1 and st["kept"] == 1
    assert any(x.startswith("::warning") and "without the person's office" in x for x in lines)
