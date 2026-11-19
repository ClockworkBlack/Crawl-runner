"""Which legislator record belongs to which candidate (legis.legis_people / match_member).

Nov. 2026: seven people who never sat in the legislature showed another person's Georgia House record, because a
candidate whose races named no State House or Senate seat took the first legislator of the same name. The members
and candidates below are the real ones from that audit, cut down to the fields matching reads."""
from gacrawl.names import name_variants
from gacrawl.sources import legis
from gacrawl.store import Record, Store

# member id: (full name as legis.ga.gov lists it, first, last, chamber, district, city, party)
MEMBERS = {
    4992: ("Lauren McDonald III", "Lauren", "McDonald III", "House", 26, "Cumming", "R"),
    4960: ("Noel Williams, Jr.", "Noel", "Williams, Jr.", "House", 148, "Cordele", "R"),
    836: ("Kevin Tanner", "Kevin", "Tanner", "House", 9, "Dawsonville", "R"),
    4970: ("Joe Campbell", "Joe", "Campbell", "House", 171, "Camilla", "R"),
    867: ("David Clark", "David", "Clark", "House", 100, "Buford", "R"),
    213: ("Richard Smith", "Richard", "Smith", "House", 139, "Columbus", "R"),
    857: ("Beth Beskin", "Beth", "Beskin", "House", 54, "Atlanta", "R"),
    4969: ("Bill Yearta", "Bill", "Yearta", "House", 152, "Sylvester", "R"),
    771: ("Pam Dickerson", "Pam", "Dickerson", "House", 113, "Conyers", "D"),
    781: ("Matt Hatchett", "Matt", "Hatchett", "House", 155, "Dublin", "R"),
    111: ("Mike Glanton", "Mike", "Glanton", "House", 75, "Jonesboro", "D"),
    719: ("Butch Miller", "Butch", "Miller", "Senate", 49, "Gainesville", "R"),
    4918: ("Jennifer Jordan", "Jennifer", "Jordan", "Senate", 6, "Atlanta", "D"),
}


def _store(tmp_path, members=MEMBERS):
    store = Store(tmp_path / "data")
    for mid, (name, first, last, chamber, district, city, party) in members.items():
        url = f"https://www.legis.ga.gov/members/{chamber.lower()}/{mid}?session=1033"
        store.add(Record(legis.SOURCE, "legislator", url, title=name, key=f"{legis.SOURCE}|legislator|{mid}",
                         meta={"member_id": mid, "name": name, "first": first, "last": last, "nickname": None, "chamber": chamber,
                               "district": district, "city": city, "party": party, "sessions": [], "committees": []}))
    store.flush()
    return store


def cand(key, name, offices, counties=(), jurisdictions=(), aliases=(), party="", split=False):
    """An Electo person in the shape candidate_from_person gives: each office a race, in the county it was run in."""
    races = [{"office": o, "county": counties[0] if counties else "", "d": "2026-05-19"} for o in offices]
    return {"id": key.lower().replace(" ", "-"), "electo_key": key, "name": name, "aliases": list(aliases), "party": party,
            "counties": list(counties), "jurisdictions": list(jurisdictions), "jurisdiction": (list(jurisdictions) or [""])[-1],
            "split": split, "races": races, "variants": name_variants(name, list(aliases))}


# the seven of the audit: none of them ever held a seat; each was given the member in the comment
WRONG = [
    cand("LAUREN BUBBA MCDONALD", "Lauren Bubba McDonald, Jr.", ["Public Service Commission District 4"], party="R"),     # 4992, III
    cand("HARVEY WILLIAMS", "Harvey Williams, Jr.", ["County Commission - District 1"], ["Atkinson"], ["co-atkinson"], party="I"),  # 4960
    cand("KEVIN TANNER (LAURENS)", "Kevin L. Tanner (Laurens County)", ["County Commission - District 5"], ["Laurens"], ["co-laurens"],
         party="R", split=True),                                                                                            # 836
    cand("JOE CAMPBELL (PEACHTREE CITY)", "Joe Campbell (Peachtree City)", ["Peachtree City - City Council - Post 3"], ["Fayette"],
         ["co-fayette", "peachtree-city"], split=True),                                                                     # 4970
    cand("DAVID KEITH CLARK", "David Keith Clark", ["Grantville City Council - Post 2"], ["Coweta"], ["co-coweta", "grantville"]),  # 867
    cand("DAVID CLARK (OGLETHORPE)", "David R. Clark (Oglethorpe County)", ["County Commission - District 3"], ["Oglethorpe"],
         ["co-oglethorpe"], party="R", split=True),                                                                         # 867
    cand("RUCKER SMITH", "R. Rucker Smith", ["Superior Court, Southwestern Circuit (Smith seat)"], aliases=["Richard Rucker Smith"]),  # 213
]

# legitimately the member: the seat in their races, or (a statewide or appellate race, no place) the same full name
RIGHT = {
    "BETH BESKIN": (cand("BETH BESKIN", "Beth Beskin", ["State House District 54", "State House District 54 (Republican primary)"], party="R"), 857),
    "ELIZABETH BETH BESKIN": (cand("ELIZABETH BETH BESKIN", 'Elizabeth "Beth" Beskin', ["Supreme Court (Bethel seat)"]), 857),
    "BILL YEARTA": (cand("BILL YEARTA", "Bill Yearta", ["State House District 152"], party="R"), 4969),
    "WILLIAM BILL YEARTA": (cand("WILLIAM BILL YEARTA", 'William J. "Bill" Yearta', ["State House District 152 (Republican primary)"], party="R"), 4969),
    "PAM DICKERSON": (cand("PAM DICKERSON", "Pam Dickerson", ["State House District 113 (Democratic primary)"], party="D"), 771),
    "PAMELA PAM DICKERSON": (cand("PAMELA PAM DICKERSON", 'Pamela "Pam" Dickerson', ["State House District 113"], party="D"), 771),
    "MATT HATCHETT": (cand("MATT HATCHETT", "Matt Hatchett", ["State House District 150", "State House District 155"], party="R"), 781),
    "MIKE GLANTON": (cand("MIKE GLANTON", "Mike Glanton", ["State House District 75"], party="D"), 111),
    "MIKE GLANTON (JR)": (cand("MIKE GLANTON (JR)", "Mike Glanton Jr.", ["State House District 75 (Democratic primary)"], party="D"), 111),
    "BUTCH MILLER": (cand("BUTCH MILLER", "Butch Miller", ["Lieutenant Governor (Republican primary)"], party="R"), 719),
    # the member himself: legis.ga.gov's 'Noel Williams, Jr.' used to be indexed as 'Jr. Noel Williams' and never found
    "NOEL WILLIAMS": (cand("NOEL WILLIAMS", "Noel W. Williams Jr.", ["State House District 148"], party="R"), 4960),
}


def test_a_non_legislator_never_gets_a_namesakes_record(tmp_path):
    store = _store(tmp_path)
    people = legis.legis_people(store, WRONG, None)
    assert people == {}, {k: v["member"]["name"] for k, v in people.items()}
    # the Pickens County clerk is not the senator from Atlanta either
    clerk = cand("JENNIFER EDWARDS JORDAN", "Jennifer Edwards Jordan", ["Clerk of Superior Court"], ["Pickens"], ["co-pickens"], party="R")
    assert legis.legis_people(store, [clerk], None) == {}
    store.close()


def test_the_same_person_keeps_their_record(tmp_path):
    store = _store(tmp_path)
    people = legis.legis_people(store, [c for c, _ in RIGHT.values()] + WRONG, None)
    assert {k: v["member"]["id"] for k, v in people.items()} == {k: mid for k, (_, mid) in RIGHT.items()}
    store.close()


def test_without_a_seat_the_place_must_agree(tmp_path):
    """Same full name, one member: a candidate who ran where the member lives is matched; one who ran elsewhere, or
    whose member page gives a place the registry does not know, is not."""
    store = _store(tmp_path)
    home = cand("JOE CAMPBELL (MITCHELL)", "Joe Campbell", ["County Commission - District 2"], ["Mitchell"], ["co-mitchell"])
    city = cand("JOE CAMPBELL (CAMILLA)", "Joe Campbell", ["Camilla City Council - Ward 1"], [], ["camilla"])
    away = cand("JOE CAMPBELL (FAYETTE)", "Joe Campbell", ["County Commission - District 2"], ["Fayette"], ["co-fayette"])
    son = cand("LAUREN MCDONALD (FORSYTH)", "Lauren McDonald III", ["Board of Education - District 1"], ["Forsyth"], ["co-forsyth"])
    assert set(legis.legis_people(store, [home, city, away, son], None)) == {"JOE CAMPBELL (MITCHELL)", "JOE CAMPBELL (CAMILLA)",
                                                                             "LAUREN MCDONALD (FORSYTH)"}
    store.close()
    store = _store(tmp_path / "b", {4970: ("Joe Campbell", "Joe", "Campbell", "House", 171, "Chestnut Mountain", "R")})
    assert legis.legis_people(store, [home], None) == {}
    store.close()


def test_never_the_first_of_several(tmp_path):
    """Two members of one name and no seat to tell them apart: neither. With no place to check, a name Electo knows
    two people by, or a member of the other party, is not matched either."""
    store = _store(tmp_path, {1: ("Pat Smith", "Pat", "Smith", "House", 1, "Rome", "R"), 2: ("Pat Smith", "Pat", "Smith", "Senate", 2, "Macon", "D"),
                              719: MEMBERS[719]})
    assert legis.legis_people(store, [cand("PAT SMITH", "Pat Smith", ["Public Service Commission District 1"])], None) == {}
    assert set(legis.legis_people(store, [cand("PAT SMITH", "Pat Smith", ["State Senate District 2"])], None)) == {"PAT SMITH"}
    other = cand("BUTCH MILLER", "Butch Miller", ["Lieutenant Governor (Democratic primary)"], party="D")
    split = cand("BUTCH MILLER", "Butch Miller", ["Public Service Commission District 2"], party="R", split=True)
    assert legis.legis_people(store, [other], None) == {} and legis.legis_people(store, [split], None) == {}
    store.close()


def test_a_shared_seat_goes_to_the_full_name(tmp_path):
    """A father and a son who both held one seat: the suffix decides, and a candidate whose name has neither gets none."""
    store = _store(tmp_path, {10: ("John Doe, Sr.", "John", "Doe, Sr.", "House", 10, "Rome", "R"),
                              11: ("John Doe Jr.", "John", "Doe Jr.", "House", 10, "Rome", "R")})
    people = legis.legis_people(store, [cand("JOHN DOE JR", "John Doe, Jr.", ["State House District 10"]),
                                        cand("JOHN DOE", "John Doe", ["State House District 10"])], None)
    assert {k: v["member"]["id"] for k, v in people.items()} == {"JOHN DOE JR": 11}
    store.close()


def test_names_written_with_a_comma_or_added_by_hand_are_found(tmp_path):
    """'Alvin Payton, Jr.' used to be looked up as 'Jr. Payton' only, and an alias added in config/candidates.yaml
    after the import made the variants was never looked up; with the seat in their races both are the member."""
    store = _store(tmp_path, {5096: ("Alvin Payton", "Alvin", "Payton", "House", 177, "Valdosta", "D"),
                              5048: ("Lynn Heffner", "Lynn", "Heffner", "House", 130, "Hogansville", "D")})
    payton = cand("ALVIN PAYTON", "Alvin Payton, Jr.", ["State House District 177"], party="D")
    gladney = {**cand("LYNN GLADNEY", "Lynn Gladney", ["State House District 130"], party="D"), "aliases": ["Lynn Heffner"]}
    assert payton["variants"] == ["Jr. Alvin Payton", "Jr. Payton"] and gladney["variants"] == ["Lynn Gladney"]
    people = legis.legis_people(store, [payton, gladney], None)
    assert {k: v["member"]["id"] for k, v in people.items()} == {"ALVIN PAYTON": 5096, "LYNN GLADNEY": 5048}
    store.close()


def test_full_names_keep_suffixes_and_middle_names():
    f = legis.full_names
    assert f(["Harvey Williams, Jr."]) == {("harveywilliams", "jr")} and f(["Noel Williams, Jr."]) == {("noelwilliams", "jr")}
    assert f(["Lauren McDonald III"]) == {("laurenmcdonald", "iii")} and f(["Lauren McDonald II"]) == {("laurenmcdonald", "ii")}
    assert not f(["Lauren McDonald Jr."]) & (f(["Lauren McDonald II"]) | f(["Lauren McDonald III"]))
    assert f(['Elizabeth "Beth" Beskin']) == {("elizabethbeskin", ""), ("bethbeskin", "")}
    assert f(["David Keith Clark"]) != f(["David Clark"]) and f(["DOE, JANE A"]) == {("janeadoe", "")}
    assert f(["JaNice VanNess"]) == f(["Janice Van Ness"]) and f(["Jane Doe, MD"]) == f(["Jane Doe"])
