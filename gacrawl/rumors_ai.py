"""The open model's reading of the Rumor Mill (data/rumor_mill/rumors.jsonl): what each unverified item actually says
about the person it was filed under, so the page's "Rumor Has It" tab can rate how likely it is to be true
(Electo ga/crawl_import.py rumors_from turns these readings and the corroboration it finds into the rating).

For each item with the outlet's text (kept when the crawler opened the page; never published), the model is told who
the person is (the office of their race, and for a local office its city and county, in Georgia) and answers five
questions, as JSON:
  named        does the text name someone of this name at all (an item can be filed under someone it never mentions)
  quote        the one sentence of the text that says the most about them, copied exactly
  same_person  is that the person described, or someone else with the same name: true, false, or null when the text
               does not say enough to tell. Items are found by searching for the name, and a search finds namesakes:
               in the Nov 19, 2026 audit 7 of 30 rated items were about someone else ("Scream" writer Kevin Williamson
               under a Banks County school board member, a Michigan football player under Rod Moore, "his wife,
               Kelly Johnson" beside a Louisiana congressman under a State Senate candidate). Electo hides an item
               read as false; null keeps the rating it had.
  attribution  what the claim about them rests on: "official record" (a filing, vote, court record, police report),
               "named source" (a person speaking on the record), "anonymous" (unnamed sources, "people familiar"),
               or "none" (the writer's own assertion or opinion)
  opinion      whether the piece is opinion or commentary rather than reporting
The answer is kept only when its quote is copied exactly from the text and names the person; otherwise the item is
read again when its text changes (the same question is not put to the same model twice: the training log remembers).
Saved in the row as "ai": {"v", "m", "named", "same_person", "quote", "attribution", "opinion", "at"}. A row already
confirmed or debunked by hand is never read.

Two plain cases are settled without asking (namesake()), both only in a text that names no Georgia place: every
mention of the full name is part of a longer name ("Jerry Lee Lewis" under Lee Lewis), or the sentence that names the
person in full names another state ("HAMILTON COUNTY, Tenn. - County Commission Chairman Ken Smith"; only when their
office is known and is below the statewide level, since national news names Georgia's governor and members of Congress
beside other states' officials without saying Georgia). That reading says same_person false, its "m" is "rule" (no
model read it) and its "why" says which case it was.

    gacrawl rumors-ai --max-minutes 10

Every call goes to data/train/rumor_log.jsonl (input, output, verdict) for training a model of our own later; the
readings settled without asking go there too, as task "rumor_rule".
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

# 2 (Nov 19, 2026): the question says who the person is (office, city, county) and asks same_person; every reading of
# version 1 is read again
VERSION = 2
ATTR = ("official record", "named source", "anonymous", "none")

SYSTEM = ("You read news and web articles for a civic information site and answer questions about them literally. You "
          "answer only from the article. You copy quotes exactly, character for character. You answer in JSON only.")
ASK = """Person: {name}
Who they are: {who}
The article was found by searching for the name, so it may be about someone else with the same name.

Article ({domain}):
{text}

Answer in JSON with these keys:
"named": true if the article names someone called {name}, else false.
"quote": if named, the one sentence from the article that says the most about the person of that name, copied exactly;
else "".
"same_person": if named, is that this person, the one under "Who they are"? true if the article ties them to that
office, that place or Georgia; false if it is plainly someone else with the same name (for example someone who lives,
works or holds office where this person does not, or a well-known writer, actor, athlete or musician); null if the
article does not say enough to tell. null if not named.
"attribution": what the article's claim about this person rests on: "official record" (a filing, vote, court or police
record), "named source" (a person quoted by name), "anonymous" (unnamed sources), or "none" (the writer's own assertion).
"opinion": true if the article is opinion or commentary rather than news reporting, else false."""


def _json(out: str) -> dict | None:
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _bare(name: str) -> str:
    """The name without the place Electo adds to tell two people apart ("David Thompson (Monticello)"), whose last
    word would otherwise be taken for the surname."""
    return _norm(re.sub(r"\([^)]*\)", " ", name or "")) or (name or "")


# ---------------------------------------------------------------- who the person is, and their namesakes
# a race for the presidency: its candidates are on Georgia's ballot but live and serve elsewhere (Ted Cruz, Bernie Sanders)
NATIONAL = re.compile(r"president of the united states|\bu\.\s?s\.\s+president|presidential", re.I)
# offices of the whole state or of Congress, by their names, for a person Electo has no races for (the hand-written
# config/candidates.yaml, which is all a job without Electo people has). On Electo's races they name no local office.
FEDERAL_OFFICE = re.compile(r"\bu\.\s?s\.|\bunited states\b|\bcongress", re.I)
STATEWIDE_OFFICE = re.compile(
    r"\bgovernor\b|\bsecretary of state\b|\battorney general\b|\bcommissioner of (?:agriculture|insurance|labor)\b"
    r"|\bstate school superintendent\b|\bpublic service commission\b|\bsupreme court\b|\bcourt of appeals\b", re.I)
WIDE = ("federal", "statewide")
LEVELS = ("federal", "statewide", "judicial", "legislative", "local", "school")       # the widest first


def _office_base(office: str) -> str:
    """An office without its parenthesised notes, in the ballots' words: config/candidates.yaml's "State
    Representative, District 100" is Electo's "State House District 100"."""
    base = (office or "").split(" (")[0].strip().lower()
    base = re.sub(r"^state representative,? district\b", "state house district", base)
    base = re.sub(r"^state senator,? district\b", "state senate district", base)
    return re.sub(r"^u\.\s?s\. representative,? district\b", "u.s. house district", base)


def _same_office(c: dict) -> list[dict]:
    """The person's races (Electo's) for the office they are filed under, any line of it ("U.S. Representative,
    District 6; U.S. House District 6"), with or without a county."""
    bases = {_office_base(x) for x in (c.get("office") or "").split(";")} - {""}
    return [r for r in (c.get("races") or []) if isinstance(r, dict) and _office_base(r.get("office") or "") in bases]


def office_level(c: dict) -> str:
    """How wide the person's office is: "federal", "statewide", "judicial", "legislative", "local", "school", or ""
    when nothing says. The office's own name settles the federal and statewide ones; otherwise the widest level of
    their races for this office (Electo's), else of all their races. A statewide race also has one county's results
    row marked "local" (John King, Commissioner of Insurance, has one for Bacon County), so the widest one is taken."""
    office = c.get("office") or ""
    if FEDERAL_OFFICE.search(office) or NATIONAL.search(office):
        return "federal"
    if STATEWIDE_OFFICE.search(office):
        return "statewide"
    have = {r.get("level") for r in _same_office(c) if r.get("level")} or {x for x in c.get("levels") or [] if x}
    return next((x for x in LEVELS if x in have), sorted(have)[0] if have else "")


def place_of(c: dict, juris: dict | None = None) -> list[str]:
    """Where the person's race is, as a reader of an article would know it: the city of their office (when the office's
    own name does not say it) and its county or counties. Georgia itself is added by who().

    Only a local office (or one nothing says the level of) has a place. A statewide or federal officer serves the
    whole state, and a district or circuit spans counties: until Nov 19, 2026 the question put John King,
    Commissioner of Insurance, in Bacon County (his race's one county results row, and the jurisdiction Electo gave
    him from it) and the model was told to answer false where the person does not hold office. The counties are
    those their races for this office were on the ballot in (every one: Atlanta's mayor is in DeKalb and Fulton), and
    none when one of those races has no county; with no such race, the jurisdiction's own county, else the person's
    only county."""
    juris = juris or {}
    low = (c.get("office") or "").split(";")[0].lower()
    level = office_level(c)
    if level not in ("", "local", "school"):
        return []
    out: list[str] = []
    j = juris.get(c.get("jurisdiction") or "") or {}
    if j.get("kind") in ("city", "town", "consolidated") and j.get("name") and j["name"].lower() not in low:
        out.append(j["name"])
    same = _same_office(c)
    counties: list[str] = []
    if same:
        if all(r.get("county") for r in same):
            counties = list(dict.fromkeys(r["county"] for r in same))
    elif j.get("kind") == "county":
        counties = [j.get("name") or ""]
    elif len(j.get("counties") or []) == 1:
        counties = [j["counties"][0]]
    elif len(c.get("counties") or []) == 1:
        counties = [c["counties"][0]]
    if len(counties) > 3:
        counties = []          # a district across many counties is no one place
    for county in counties:
        county = re.sub(r"(?i)\s+county$", "", county or "").strip()
        if county and f"{county} county".lower() not in low and f"{county} County" not in out:
            out.append(f"{county} County")
    # the place Electo writes after a name two people share ("Andrew Ferguson (Demorest)"), which the question's
    # "Person:" line leaves out; it is nearly always the office's town or its county already
    for x in re.findall(r"\(([^)]*)\)", c.get("name") or ""):
        x = _norm(x)
        if x and x.lower() not in low and x.lower() not in {o.lower() for o in out} \
                and f"{x} county".lower() not in {o.lower() for o in out}:
            out.append(x)
    return out


def who(c: dict, place: list[str] | None = None) -> str:
    """The person as the question describes them: the office (its first line) and where it is. A presidential
    candidate is said to be a national figure, so that a story set in their own state is not read as a namesake's,
    and a federal or statewide officer is said to be one, since national news names them beside other states'
    officials without saying Georgia. Without an office (a job that has no Electo people, or someone found in the news)
    the model is told it could be any office, the presidency included."""
    office = _norm((c.get("office") or "").split(";")[0])
    where = ", ".join([*(place or []), "Georgia"])
    if office and NATIONAL.search(office):
        return f"on Georgia's ballot for {office} (a national figure, who may live and hold office in another state)"
    level = office_level(c) if office else ""
    if level == "federal":
        return (f"a Georgia candidate or official: {office} (Georgia; a federal office, so national news may name them "
                "beside other states' officials)")
    if level == "statewide":
        return f"a Georgia candidate or official: {office} (Georgia; a statewide office)"
    if office:
        return f"a Georgia candidate or official: {office} ({where})"
    return (f"someone on a ballot or in public office in {where} (the office is not known here: it may be local, "
            "statewide or national, the presidency included)")


# words written before a name that are not a given name: "Mayor Ken Smith" is Ken Smith, "Jerry Lee Lewis" is not Lee
# Lewis. Used when no list of given names is at hand (given_names() is, in run()).
NOT_GIVEN = {
    "mayor", "commissioner", "commissioners", "chairman", "chairwoman", "chairperson", "chair", "vice", "councilman",
    "councilwoman", "councilmember", "councilor", "alderman", "council", "member", "board", "trustee", "sheriff", "deputy",
    "officer", "sergeant", "sgt", "lieutenant", "lt", "captain", "capt", "chief", "detective", "trooper", "agent",
    "investigator", "judge", "justice", "magistrate", "solicitor", "attorney", "clerk", "coroner", "treasurer",
    "secretary", "superintendent", "principal", "teacher", "coach", "professor", "prof", "doctor", "dr", "mr", "mrs",
    "ms", "miss", "rev", "reverend", "pastor", "bishop", "elder", "deacon", "father", "brother", "sister", "president",
    "director", "manager", "administrator", "governor", "gov", "senator", "sen", "representative", "rep", "congressman",
    "congresswoman", "speaker", "leader", "owner", "founder", "candidate", "candidates", "incumbent", "nominee",
    "challenger", "republican", "republicans", "democrat", "democrats", "democratic", "independent", "libertarian",
    "former", "late", "elect", "reelect", "vote", "thanks", "congrats", "honorable", "hon", "sir", "dean", "chancellor",
    "senior", "junior", "veteran", "colonel", "general", "major", "private", "class", "nurse", "physician", "surgeon",
    "dentist", "lawyer", "author", "artist", "businessman", "businesswoman", "farmer", "minister", "safety", "policy",
    "advertisement", "charge", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january",
    "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
    "today", "yesterday", "tonight", "tomorrow", "also", "but", "so", "then", "meanwhile", "however", "while", "said",
    "says", "asked", "according", "photo", "courtesy", "credit", "pictured", "left", "right", "from", "story", "watch",
    "video", "update", "updated", "posted", "contact", "call", "support", "endorse", "endorsed", "friends", "county",
    "city", "state", "district", "georgia", "atlanta",
}


def given_names(candidates: list[dict]) -> set[str]:
    """First names as Georgia's ballots print them (every candidate's first word, and their aliases'), the test of
    whether the word before a name starts a longer name: "Jerry" in "Jerry Lee Lewis" is one, "Physician" in "former
    Emergency Physician Rich McCormick" and "Class" in "Sgt. 1st Class Justin Jones" are not."""
    from .personname import FUNCTION_WORDS

    out = set()
    for c in candidates:
        for n in [c.get("name") or "", *(c.get("aliases") or [])]:
            w = re.findall(r"[A-Za-z]+", str(n))
            if w and len(w[0]) > 1:
                out.add(w[0].lower())
    return out - NOT_GIVEN - FUNCTION_WORDS


def _mentions(text: str, name: str, given: set | None = None) -> list[tuple[int, int, str]]:
    """Each mention of the person's full name (any of its forms: "John Thompson" and "J.D. Thompson" for John "J.D."
    Thompson), as (start, end, the longer name it is part of, or "")."""
    from .names import name_variants
    from .personname import FUNCTION_WORDS

    own = {w.lower() for w in re.findall(r"[A-Za-z]+", name or "")}
    out: list[tuple[int, int, str]] = []
    for v in name_variants(name or "") or [name or ""]:
        toks = re.findall(r"[A-Za-z0-9]+", v)
        if len(toks) < 2:
            continue
        for m in re.finditer(r"\b" + r"[\s.\-']+".join(re.escape(t) for t in toks) + r"\b", text or "", re.I):
            # a capitalised word right before the name, with nothing between but a space, that is a given name (or, with
            # no list of them, not a title, a role or a sentence's own word): a longer name. An all-capitals word ("BOE")
            # and a hyphenated one ("Governor-Elect") are left alone.
            w = re.search(r"(?:^|[\s(\"“])([A-Z][a-z]+) $", text[max(0, m.start() - 40):m.start()])
            g = (w.group(1) if w else "").lower()
            longer = bool(g) and g not in own and (g in given if given is not None else
                                                   g not in NOT_GIVEN and g not in FUNCTION_WORDS)
            out.append((m.start(), m.end(), f"{w.group(1)} {m.group(0)}" if longer else ""))
    return sorted(set(out))


def _longer_only(text: str, name: str, given: set | None = None) -> str:
    """The longer name when every mention of the person's full name in `text` is part of one ("" otherwise, and when
    the full name is not there at all)."""
    ms = _mentions(text, name, given)
    return ms[0][2] if ms and all(x[2] for x in ms) else ""


# the other 49 states (Washington is left out: a Washington dateline is the capital, where Georgia's members serve)
STATES = ("Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware", "Florida",
          "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
          "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
          "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
          "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
          "Vermont", "Virginia", "West Virginia", "Wisconsin", "Wyoming")
# the newspapers' abbreviations ("Hamilton County, Tenn.", "R-La.") and postal codes ("Toledo, OH"); the codes that are
# also words or letters after a name ("MD", "PA", "IN", "OR", "ME", "OK", "HI", "ID", "MA", "MS", "CO", "DO") are left out
AP_STATES = ("Ala", "Ariz", "Ark", "Calif", "Colo", "Conn", "Del", "Fla", "Ill", "Ind", "Kan", "Ky", "La", "Md", "Mass",
             "Mich", "Minn", "Miss", "Mo", "Mont", "Neb", "Nev", "N.H", "N.J", "N.M", "N.Y", "N.C", "N.D", "Okla", "Ore",
             "Pa", "R.I", "S.C", "S.D", "Tenn", "Vt", "Va", "Wash", "W.Va", "Wis", "Wyo")
POSTAL = ("AK", "AL", "AR", "AZ", "CA", "CT", "DE", "FL", "IA", "IL", "KS", "KY", "LA", "MI", "MN", "MO", "MT", "NC", "ND",
          "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY")
# states that are also given names: "Virginia" in "Commissioners Ken Smith and Virginia Jones" is a person. One of
# these is taken for the state only when no capitalised word follows it ("Virginia Gov. Glenn Youngkin" still is)
GIVEN_STATES = ("Virginia", "Montana", "Nevada", "Indiana")
OTHER_STATE = re.compile(
    # a state's name as a place, not a newspaper's or a university's ("New York Times", "University of Michigan",
    # "Ohio State") and not another person's ("Wisconsin's first Black governor" beside Keisha Lance Bottoms)
    r"(?<!University of )\b(?:" + "|".join(tuple(x for x in STATES if x not in GIVEN_STATES)
                                           + tuple(x.upper() for x in STATES)) + r"|(?:"
    + "|".join(GIVEN_STATES) + r")(?!\s+[A-Z][a-z]+(?![.\w])))\b"
    r"(?!\s+(?:State|Times|Post|Tribune|University|Avenue|Street|Road)\b|['’](?:s\b|\s|$))"
    r"|(?:,\s+|\b[RDI]-)(?:" + "|".join(re.escape(x) for x in AP_STATES + tuple(x.upper() for x in AP_STATES)) + r")\.(?![A-Za-z])"
    r"|(?:\b[A-Z][A-Za-z.]+,\s+|\b[RDI]-)(?:" + "|".join(POSTAL) + r")\b(?![A-Za-z'’-]|\.[A-Za-z])")
GEORGIA = re.compile(r"(?i:\bgeorgia(?:ns?)?\b|\batlanta\b)|\bGa\.|\bGA\.|,\s*GA\b|\b[RDI]-GA\b")


def _georgia(text: str, places=(), domain: str = "") -> bool:
    """Does the text (or the site) name Georgia, Atlanta, or one of the Georgia places given (the person's city and
    county, the other towns of their county, every county of the state)?"""
    d = (domain or "").lower()
    if "georgia" in d or "atlanta" in d or d.endswith((".ga.us", ".ga.gov")):
        return True
    if GEORGIA.search(text or ""):
        return True
    return any(p and re.search(r"\b" + re.escape(p) + r"\b", text or "") for p in places)


def namesake(name: str, text: str, places=(), domain: str = "", headline: str = "", given: set | None = None,
             office: str = "", level: str = "") -> str:
    """Why the text is plainly about someone else with this name, settled without asking the model ("" when it may be
    them). Only a text that names no Georgia place (nor its headline or site) is settled: there every mention of the
    full name is part of a longer name, or the sentence that names the person in full also names another state.

    The second only for an office below the statewide level (`level`, from office_level(); the office's name is
    looked at too). Ted Cruz of Texas is the Ted Cruz of Georgia's 2016 presidential primary, and national news names
    Georgia's governor, senators and members of Congress beside other states' officials without saying Georgia ("Sens.
    Jon Ossoff and Mark Kelly, D-Ariz., introduced the bill"). With no office known (no Electo people in the job) any
    of them could be. A surname alone ("Sheriff Smith said deputies went to Tennessee") is not enough either."""
    return _namesake(name, text, places, domain, headline, given, office, level)[0]


def _namesake(name, text, places=(), domain="", headline="", given=None, office="", level="") -> tuple[str, tuple | None]:
    """namesake(), and where in the text the mention it rests on is (start, end), for the reading's quote."""
    text = text or ""
    if _georgia(text + "\n" + (headline or ""), places, domain):
        return "", None
    spots = _mentions(text, name, given)
    longer = _longer_only(text, name, given)
    if longer:
        return f'the name is only part of a longer one ("{longer}") and the text names no Georgia place', spots[0][:2]
    office = office or ""
    if not office.strip() or level in WIDE or NATIONAL.search(office) or FEDERAL_OFFICE.search(office) \
            or STATEWIDE_OFFICE.search(office):
        return "", None
    for a, b, _ in spots:
        s, e = _sentence_span(text, a, b)
        m = OTHER_STATE.search(text, s, e)
        if m:
            return (f'the sentence naming them names another state ("{_norm(m.group(0)).strip(", ")}") and the text no '
                    "Georgia place"), (a, b)
    return "", None


def _sentence_span(text: str, a: int, b: int, limit: int = 400) -> tuple[int, int]:
    """Where the sentence around [a, b) starts and ends, at most limit/2 characters either side of it. A period after
    a short capitalised word or initials is an abbreviation ("Rep.", "Tenn.", "R-La.", "N.C."), not a sentence's end;
    the dots ("…") between a search result's snippets end one too."""
    ends = [0]
    for m in re.finditer(r"[.!?…]+[\"”’)]*(?=\s|$)|\n", text):
        word = (re.search(r"(\S+)$", text[:m.end()]) or [""])[0]
        if m.group(0).startswith(".") and re.fullmatch(r"[\"“(]?(?:[A-Z][A-Za-z]{0,3}|[A-Z]-[A-Z][a-z]{1,3}|(?:[A-Za-z]\.)+[A-Za-z])\.", word):
            continue
        ends.append(m.end())
    s = max(e for e in ends if e <= a)
    e = min([x for x in ends if x >= b] or [len(text)])
    return max(s, a - limit // 2), min(e, b + limit // 2)


def _sentence(text: str, a: int, b: int, limit: int = 400) -> str:
    """The sentence of `text` around [a, b), whitespace made single (what a reading settled without asking quotes)."""
    s, e = _sentence_span(text, a, b, limit)
    return _norm(text[s:e]).lstrip("… ").strip()[:limit]


def _same(v) -> bool | None:
    """The model's same_person: true, false, or None (null, missing, or anything else)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return {"true": True, "yes": True, "false": False, "no": False}.get(v.strip().lower())
    return None


def verdict(ans: dict | None, name: str, text: str, given: set | None = None,
            georgia: bool | None = None) -> tuple[dict | None, list[str]]:
    """The model's answer checked against the text: (reading to keep, reasons it was not kept). `georgia`: whether the
    text, its headline or its site name a Georgia place (run() works it out with the person's places; by default the
    text is looked at for Georgia and Atlanta alone)."""
    if not ans:
        return None, ["no JSON answer"]
    named = bool(ans.get("named"))
    quote = _norm(str(ans.get("quote") or ""))
    attr = str(ans.get("attribution") or "").lower().strip()
    same = _same(ans.get("same_person"))
    if georgia is None:
        georgia = _georgia(text)
    why = []
    if attr not in ATTR:
        why.append(f"attribution {attr!r} is not one of the four")
    bare = _bare(name)
    last = (bare.split() or [""])[-1].lower()
    if named:
        if not quote or quote.lower() not in _norm(text).lower():
            why.append("the quote is not copied from the text")
        elif len(last) > 2 and last not in quote.lower():
            why.append("the quote does not name the person")
        elif not georgia and _longer_only(quote, name, given):
            # the quote is about someone with a longer name ("He recorded with Jerry Lee Lewis" under Lee Lewis) in a
            # text that names no Georgia place: not them when the text names them nowhere else; when it does, the quote
            # is about the wrong person
            if _longer_only(text, name, given):
                same = False
            else:
                why.append(f'the quote names someone else ("{_longer_only(quote, name, given)}")')
    else:
        # a name the text carries only inside a longer one ("Jerry Lee Lewis") is rightly "not named"
        if len(last) > 2 and re.search(r"\b" + re.escape(last) + r"\b", text, re.I) and \
                re.search(r"\b" + re.escape(bare.split()[0]) + r"\b", text, re.I) and not _longer_only(text, name, given):
            why.append("the model said not named, but the text has the name")
        quote = ""
        same = None
    if why:
        return None, why
    return {"named": named, "same_person": same, "quote": quote[:400], "attribution": attr,
            "opinion": bool(ans.get("opinion"))}, []


def _sig(msgs: list) -> str:
    import hashlib
    return hashlib.sha1(json.dumps(msgs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _failed_before(log_path: Path, model: str) -> set[str]:
    """The questions (by signature) this model already answered with something the check refused (the training log)."""
    out: set[str] = set()
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("task") == "rumor_reading" and r.get("model") == model and isinstance(r.get("input"), list):
                    (out.discard if r.get("kept") else out.add)(_sig(r["input"]))
    except OSError:
        pass
    return out


TEXT_MAX = 3000          # what the reading sees of a page (run() reads at most this much)
TEXT_RETRY_DAYS = 30     # a page that could not be read is asked for again after this long


def excerpt(text: str, name: str, limit: int = TEXT_MAX) -> str:
    """The part of a page the model reads: every paragraph that names the person (by last name), each with the one
    before it for context, in page order, up to `limit` characters; a page that never names them keeps its opening
    (so the reading can say so). A long article used to be cut at its first 3,000 characters, before the person came up."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    last = (_bare(name).split() or [""])[-1]
    if len(last) <= 2 or not paras:
        return (text or "")[:limit]
    rx = re.compile(r"\b" + re.escape(last) + r"\b", re.I)
    hits = [i for i, p in enumerate(paras) if rx.search(p)]
    if not hits:
        return " ".join(paras)[:limit]
    keep = {j for i in hits for j in (i - 1, i, i + 1) if 0 <= j < len(paras)}
    # a name in a caption or a byline-like line ("Mayor / Andre Dickens") says nothing on its own: short excerpts are
    # widened with the paragraphs that follow the first mention, then those before it
    order = list(range(hits[0], len(paras))) + list(range(hits[0] - 1, -1, -1))
    for j in order:
        if sum(len(paras[i]) + 1 for i in keep) >= 1200:
            break
        keep.add(j)
    keep = sorted(keep)
    out, size = [], 0
    for i in keep:
        p = paras[i]
        if size + len(p) + 1 > limit:
            if i in hits and size < limit - 200:      # a naming paragraph is cut rather than left out
                out.append(p[:limit - size - 1])
            break
        out.append(p)
        size += len(p) + 1
    return "\n".join(out)


def fetch_texts(fetcher, root: Path, candidates: list[dict], deadline: float, log=print, limit: int | None = None) -> dict:
    """Open each unverified Rumor Mill item's page once (the crawler's own fetcher: robots.txt, spacing, back-off, the
    block list) and keep the part that names the person (excerpt) in its row's meta.text for run() to read. Items
    found before Nov 9 had only their headline; new ones keep the text research opened. A page that cannot be read
    (refused, gone, not HTML) is noted (meta.text_error) and asked for again after TEXT_RETRY_DAYS. The text is never
    published (export drops meta.text)."""
    from . import extract
    from .candidates import RumorMill
    from .fetch import Blocked, Disallowed, OutOfTime

    by_id = {c["id"]: c for c in candidates}
    rm = RumorMill(root)
    cutoff = (dt.date.today() - dt.timedelta(days=TEXT_RETRY_DAYS)).isoformat()
    todo = [r for r in rm.all() if r.get("status", "unverified") == "unverified" and r.get("url", "").startswith("http")
            and len((r.get("meta") or {}).get("text") or "") <= 200 and (r.get("meta") or {}).get("text_tried", "") < cutoff]
    todo.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    if limit:
        todo = todo[:limit]
    st = {"todo": len(todo), "read": 0, "kept": 0, "failed": 0}
    for r in todo:
        if time.monotonic() > deadline:
            break
        m = r.setdefault("meta", {})
        c = by_id.get(r.get("candidate_id")) or {}
        name = c.get("name") or r.get("candidate_id", "").replace("-", " ").title()
        try:
            res = fetcher.get(r["url"])
            st["read"] += 1
            if res.status != 200 or "html" not in (res.content_type or ""):
                raise ValueError(f"HTTP {res.status} {res.content_type or ''}".strip())
            text = extract.main_text(res.text)
            if len(text) < 200:
                raise ValueError("no article text on the page")
        except OutOfTime:
            break                     # not tried: it stays first in line next run
        except (Blocked, Disallowed, ValueError, Exception) as e:  # noqa: BLE001
            m["text_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            m["text_tried"] = dt.date.today().isoformat()
            rm._dirty = True
            st["failed"] += 1
            log(f"  rumor page {r['id']}: not read ({m['text_error']}): {r['url'][:100]}")
            continue
        m["text"] = excerpt(text, name)
        m["text_url"] = res.url
        m["text_tried"] = dt.date.today().isoformat()
        m.pop("text_error", None)
        rm._dirty = True
        st["kept"] += 1
        log(f"  rumor page {r['id']}: {len(m['text']):,} characters kept: {r['url'][:100]}")
    rm.flush()
    st["left"] = st["todo"] - st["kept"] - st["failed"]
    log(f"rumor pages: {st['kept']} read and kept, {st['failed']} could not be read, {st['left']} left for the next run")
    return st


def _jurisdictions(root: Path) -> dict:
    """The registry's places by id (names, kinds and counties for who() and the Georgia check); {} when it cannot be read."""
    try:
        from .registry import by_id

        return by_id(root)
    except Exception:  # noqa: BLE001  the readings go on without the place names
        return {}


def _georgia_places(c: dict, place: list[str], juris: dict) -> list[str]:
    """The Georgia names that tie a text to this person's part of the state: their city and county, their own
    jurisdictions (a town whose name is in the office, which place_of() leaves out of the question, is still theirs),
    the other towns of their county, and every county of the state ("Jefferson County" in "Jefferson County, Ala."
    makes the check ask the model rather than decide)."""
    counties = {re.sub(r"(?i)\s+county$", "", x).strip().lower() for x in place if x.lower().endswith(" county")}
    counties |= {str(x).strip().lower() for x in c.get("counties") or []}
    out = list(place) + [_norm(x) for x in re.findall(r"\(([^)]*)\)", c.get("name") or "")]
    for jid in [c.get("jurisdiction") or "", *(c.get("jurisdictions") or [])]:
        out.append((juris.get(jid) or {}).get("name") or "")
    for j in juris.values():
        n = j.get("name") or ""
        if j.get("kind") == "county":
            out.append(n)
        elif j.get("kind") in ("city", "town", "consolidated") and counties & {x.lower() for x in j.get("counties") or []}:
            out.append(n)
    return [x for x in dict.fromkeys(out) if x]


def run(root: Path, candidates: list[dict], deadline: float, log=print) -> dict:
    from . import llm
    from .candidates import RumorMill

    by_id = {c["id"]: c for c in candidates}
    juris = _jurisdictions(root)
    given = given_names(candidates)
    rm = RumorMill(root)
    rows = rm.all()
    todo = [r for r in rows if (r.get("meta") or {}).get("text") and r.get("status", "unverified") == "unverified"
            and (r.get("ai") or {}).get("v") != VERSION and len(r["meta"]["text"]) > 200]
    todo.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    log_path = root / "data" / "train" / "rumor_log.jsonl"
    failed = _failed_before(log_path, llm.model_name())
    st = {"todo": len(todo), "read": 0, "kept": 0, "same": 0, "rule": 0, "no_office": 0}
    down = False
    for r in todo:
        if time.monotonic() > deadline:
            break
        c = by_id.get(r.get("candidate_id")) or {}
        name = c.get("name") or r.get("candidate_id", "").replace("-", " ").title()
        text = r["meta"]["text"][:3000]
        place = place_of(c, juris)
        # the name as an article prints it: Electo's "(Demorest)" after a name that two people share is not part of it
        # (place_of() puts it in who(), and the Georgia check has it), and a model asked whether the article names
        # "Andrew Ferguson (Demorest)" rightly says no
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": ASK.format(name=_bare(name), who=who(c, place), domain=r.get("domain", ""),
                                                       text=text)}]
        places = _georgia_places(c, place, juris)
        why_not, spot = _namesake(name, text, places, r.get("domain", ""), r.get("claim", ""), given,
                                  c.get("office") or "", office_level(c))
        if why_not:
            # plainly a namesake: settled without the model (no quote to check, so no attribution or opinion either).
            # The quote is the sentence the rule rests on (the first mention can be a page's list of links)
            quote = _sentence(text, spot[0], spot[1]) if spot else ""
            r["ai"] = {"v": VERSION, "m": "rule", "at": dt.date.today().isoformat(), "named": True, "same_person": False,
                       "quote": quote, "why": why_not}
            rm._dirty = True
            st["rule"] += 1
            llm.log(str(log_path), {"model": "rule", "task": "rumor_rule", "rumor": r["id"], "person": name, "input": msgs,
                                    "output": "", "kept": True, "same_person": False, "why": [why_not], "seconds": 0})
            log(f"  not them: {r['id']} {name}: {r.get('claim', '')[:60]}  [{why_not}]")
            continue
        if _sig(msgs) in failed:
            # this exact question got an answer that failed the check: the model is greedy and would give it again, so
            # the item waits until its text changes instead of taking the newest items' place on every run
            st["same"] += 1
            continue
        if down:
            continue                  # the model failed: the rest still get the checks above, which need no model
        t0 = time.time()
        try:
            out = llm.chat(msgs, max_tokens=260)
        except Exception as e:  # noqa: BLE001
            log(f"rumors ai: {type(e).__name__}: {str(e)[:200]}")
            down = True
            continue
        if not c.get("office"):
            st["no_office"] += 1
        keep, why = verdict(_json(out), name, text, given,
                            _georgia(text + "\n" + r.get("claim", ""), places, r.get("domain", "")))
        st["read"] += 1
        if keep:
            r["ai"] = {"v": VERSION, "m": llm.model_name(), "at": dt.date.today().isoformat(), **keep}
            rm._dirty = True
            st["kept"] += 1
        llm.log(str(log_path), {"task": "rumor_reading", "rumor": r["id"], "person": name, "input": msgs, "output": out,
                                "kept": bool(keep), "same_person": (keep or {}).get("same_person"), "why": why,
                                "seconds": round(time.time() - t0, 1)})
        log(f"  {'kept' if keep else 'dropped'} ({time.time() - t0:.0f}s): {r['id']} {name}: {r.get('claim', '')[:60]}"
            + (f"  [{'; '.join(why[:2])}]" if why else "")
            + ("  [not the same person]" if keep and keep.get("same_person") is False else ""))
    rm.flush()
    st["left"] = st["todo"] - st["read"] - st["same"] - st["rule"]
    log(f"rumors ai: {st['read']} items read, {st['kept']} readings kept, {st['rule']} settled as namesakes without "
        f"asking; {st['left']} left for the next run"
        + (f" ({st['same']} not asked again: the same text failed the check before)" if st["same"] else ""))
    if st["no_office"] and st["no_office"] * 2 > st["read"]:
        # Nov 15-18, 2026: 655 of 657 questions said only the name (model-work.yml loads no Electo people), so the model
        # could not have told the candidate from a namesake. Say so where the job's log is read.
        log(f"::warning title=Rumor readings::{st['no_office']} of {st['read']} items were asked without the person's office "
            "(no state/electo_people.json here?): same_person is then judged on the name and Georgia alone")
    return st
