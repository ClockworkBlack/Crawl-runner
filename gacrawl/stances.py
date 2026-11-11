"""Candidate history, ideology, stances and support — the inputs Electo's inference runs on.

Electo infers "what they may support next" only from a profile's sourced items: stated
positions, statements, ratings, endorsements, official votes and sponsored bills (it
needs at least two). This module turns everything the crawler has on a candidate into
exactly those items, each with the record it came from, and grades each by evidence:

  positions     sentences in the candidate's own words (campaign site, verified posts,
                meeting transcripts where the candidate is the speaker, questionnaire
                answers) or reported with attribution (a listed outlet quoting them),
                that take a stance on an issue (config/issues.yaml, config/topics.yaml)
  statements    the candidate's own sentences with a stance cue but no matched issue
  ideology      documented markers only: self-descriptions ("a conservative Republican"),
                caucus/organization memberships, interest-group ratings, endorsements,
                party. Composed into one sourced line; never a guess.
  affiliations  "member of the X Caucus", "serves on the board of Y", "former chair of Z"
  endorsements  received (and given), with who and when
  ratings       "A rating from the Georgia Chamber", "scored 100% with ..."
  votes         council roll calls (Legistar, minutes) and legislature votes
  bills         Legistar sponsorships and legislature bills (role sponsor / co-sponsor)
  ordinances    supported / opposed: votes and sponsorships on ordinances and resolutions

Nothing here is an inference: every item quotes or cites a record that was opened.
Unverified material (anything from a source that is neither the candidate's own
channel, an official record nor a listed outlet) never enters; it stays in the Rumor
Mill. Items parsed from transcripts or scans carry needs_review when speaker
attribution rests on a label the crawler read rather than a byline.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import yaml

from .config import ROOT
from .names import name_regex, name_variants
from .store import Store
from .topics import tag

STANCE_CUES = re.compile(
    r"\b(support\w*|oppos\w*|against|in favou?r|favou?r\w*|back(?:s|ed|ing)?|believe\w*|will (?:fight|work|push|vote|protect|"
    r"defend|expand|cut|lower|raise|fund|end|stop|ban|repeal|bring|keep|invest|secure|restore|hold|deliver|make|ensure|never|"
    r"always|continue|prioritize|champion|advocate)|would (?:vote|support|oppose|sign|veto|back)|voted? (?:for|against|to|no|yes)|"
    r"committed to|commit\w*|pledge\w*|promis\w*|priorit\w*|plan to|propos\w*|call\w* for|must\b|should\b|need\w* to|"
    r"stand\w* (?:for|with|against|up)|protect\w*|defend\w*|expand\w*|repeal\w*|ban\w*|fight\w*|cosponsor\w*|co-sponsor\w*|"
    r"introduc\w*|sponsor\w*|author\w*|reject\w*|condemn\w*|endorse\w*|urge\w*|demand\w*|cut\b|lower\b|eliminate\w*|"
    r"i am (?:for|against|committed)|i['’]m (?:for|against|committed)|i (?:want|will|won['’]t|refuse|pledge|promise|believe|"
    r"support|oppose|stand|voted|introduced|sponsored|cosponsored|co-sponsored|led|fought|secured|delivered))\b", re.I)
ISSUE_PAGE = re.compile(r"/(issues?|platform|priorit\w*|agenda|plan|vision|values|policy|policies|on-the-issues|where-i-stand|my-plan)\b", re.I)
CONTROVERSY = re.compile(
    r"\b(indict(?:ed|ment)|charged with|arrest(?:ed)?|plead(?:ed|s)? (?:guilty|not guilty|no contest)|convict(?:ed|ion)|"
    r"lawsuit|sued|sues|settl(?:ed|ement)|ethics (?:complaint|violation|commission|panel|board)|censure[ds]?|reprimand\w*|"
    r"fined|under investigation|investigat(?:ion|ed|ing) (?:into|of|by)|subpoena\w*|resign(?:ed|s)? (?:amid|after|following|under)|"
    r"misconduct|allegation\w*|alleged\w*|accused of|violat(?:ed|ion)s?|sanction(?:ed|s)|recall (?:effort|petition|election)|"
    r"audit (?:found|flagged|faulted)|conflict of interest|harass\w+|discriminat\w+|DUI|fraud\w*|embezzl\w+|briber?y|"
    r"kickback\w*|campaign finance (?:violation|complaint)|removed from (?:office|the ballot)|disqualif\w+|impeach\w*|"
    r"expelled|suspended|fired|terminated|grand jury|FBI|GBI|attorney general\'s office|district attorney\'s office|"
    r"scandal|controvers\w+|backlash|apologi[sz]ed|retract\w+|false(?:ly)? (?:claim|stat)\w*|defam\w+)\b", re.I)
# A controversy is kept only when the person is the subject of a strong cue in that sentence: "<name> was indicted",
# "charges against <name>", "an ethics complaint against <name>", "<name> resigned amid", "sued <name>". A keyword
# anywhere in a sentence that merely names them (an official swearing in a grand jury, an attorney general's own
# lawsuits, "settlement funds", a word like "suspended") is not a controversy.
STRONG_PASSIVE = (r"(?:indicted|charged(?: with)?|arrested|convicted|sued|fined|censured|reprimanded|admonished|subpoenaed|"
                  r"sanctioned|disbarred|impeached|investigated|accused(?: of)?|cited for|found (?:to have )?(?:violated|in violation)|"
                  r"removed from (?:office|the ballot|the board)|disqualified|expelled|placed on (?:administrative )?leave|"
                  r"under (?:federal |state |criminal )?investigation)")
STRONG_ACTIVE = (r"fac(?:es|ed|ing) (?:[\w-]+ ){0,3}(?:charges?|complaints?|lawsuits?|suits?|investigations?|allegations?|indictments?|recall|censure|sanctions?)|"
                 r"(?:pleaded|pleads|pled) (?:guilty|not guilty|no contest)|resign(?:ed|s) (?:amid|after|following|under)|apologi[sz]ed for")
STRONG_OF = (r"(?:indictment|charges?|lawsuit|suit|complaint|ethics (?:complaint|charges?|violations?)|investigation|probe|inquiry|"
             r"allegations?|accusations?|censure|reprimand|recall (?:effort|petition)|impeachment|arrest|conviction|subpoena)"
             r"\s+(?:filed\s+)?(?:against|into|of|targeting|involving)")
SPAM = re.compile(r"\b(slot|casino|togel|judi|thabet|dadu4d|bet88|poker online|sbobet|viagra|cialis|loan approval|crypto giveaway)\b", re.I)
PLACEHOLDER = re.compile(r"lorem ipsum|welcome to wordpress|this is your first post|hello world!?|sample page|coming soon|under construction|site is being built", re.I)
EN_COMMON = set("the and to of a in for is on that with as at by from be this are we our will it you your i my have has an or not he she his her they their was were can more".split())
LEGIS_DID = re.compile(r"\b(sponsor(?:ed|ing|s)?|co-?sponsor(?:ed|ing|s)?|filed|introduc(?:ed|ing|es)|authored|wrote the bill|"
                       r"voted (?:for|against|to)|champion(?:ed|ing|s)|fought (?:for|against|to)|fights (?:for|against)|"
                       r"supports?|opposes?|opposed|pushed (?:for|to)|led (?:the )?(?:fight|effort|push)|passed|secured funding|"
                       r"(?:house|senate) bill \d+|HB ?\d+|SB ?\d+|resolution)\b", re.I)
CONTROVERSY_SOFT = re.compile(r"\b(controvers\w+|backlash|apologi[sz]ed|retract\w+|allegation\w*|alleged\w*|accused of)\b", re.I)
FIRST_PERSON = re.compile(r"\b(i|i['’]m|i['’]ve|i['’]ll|my|we|we['’]re|we['’]ll|our|me)\b", re.I)
ATTRIB = re.compile(r"\b(said|says|told|wrote|posted|argued|added|called|explained|noted|stated|announced|vowed|pledged|"
                    r"promised|insisted|contended|maintained|responded|tweeted|declared|according to)\b", re.I)
NOISE_SENT = re.compile(r"\b(cookie|privacy policy|all rights reserved|click here|subscribe|newsletter|sign up|donate now|"
                        r"paid for by|menu|skip to|read more|share this|javascript|©|copyright|donate (?:here|today)|"
                        r"yard signs?|contact (?:us|details)|powered by|see all members|welcome to our group|my voter page|"
                        r"add to cart|us dollars|not available, please contact|log ?in to|create a poll|email\s+protected|media inquiries|check your voter registration|register to vote here)\b", re.I)
SELF_DESC = re.compile(
    r"\b(?:as\s+)?(?:a|an)\s+(?P<label>(?:(?:lifelong|proud|common[- ]sense|constitutional|fiscal|pragmatic|independent[- ]minded|"
    r"pro-?(?:life|choice|business|labor|growth|police|gun))\s+)?(?:conservative|progressive|moderate|liberal|libertarian|"
    r"populist|centrist|independent|democrat|republican|democratic socialist|socialist|constitutionalist)(?:\s+(?:republican|"
    r"democrat|independent))?)\b", re.I)
IDEOLOGY_MARKERS = {
    "conservative": r"\b(conservative|america first|maga|pro-?life|pro-?second amendment|pro-?2a|\b2a\b|limited government|law and order|"
                    r"back the blue|lower taxes|freedom caucus|tea party|constitutional carry|school choice|secure the border|"
                    r"family values|traditional values|god[- ]given rights|anti-?abortion)\b",
    "progressive": r"\b(progressive|democratic socialist|working families|pro-?choice|reproductive freedom|medicare for all|"
                   r"green new deal|living wage|union (?:member|household|strong)|black lives matter|lgbtq\+? rights|"
                   r"expand medicaid|common[- ]sense gun (?:laws|safety|reform)|climate action|racial justice|equity)\b",
    "moderate": r"\b(moderate|centrist|bipartisan|common[- ]sense|pragmatic|across the aisle|problem[- ]solver|"
                r"independent[- ]minded|reach(?:es|ing)? across|work(?:s|ing)? with both parties|middle ground)\b",
    "libertarian": r"\b(libertarian|free market|liberty|end the drug war|personal freedom|small government)\b",
    "populist": r"\b(populist|drain the swamp|anti-?establishment|outsider|career politicians|the people over|special interests)\b",
}
AFFIL = re.compile(
    r"\b(?P<role>member|chair(?:man|woman|person)?|vice[- ]chair|co-?chair|founder|co-?founder|president|vice president|"
    r"treasurer|secretary|director|board member|trustee|delegate|whip|caucus chair|past president|former chair(?:man|woman)?)"
    r"\s+(?:of|on|for)\s+(?:the\s+)?(?P<org>[A-Z][\w&'’.\- ]{3,70}?(?:Caucus|Association|Chamber(?: of Commerce)?|Union|Club|"
    r"Coalition|Committee|Board|Commission|Authority|Foundation|Federation|Society|Alliance|Council|League|Lodge|Rotary|Kiwanis|"
    r"Lions|NAACP|PTA|PTO|Party|Bar|Institute|Network|Fund|Center|Church|Ministries|Guard|Legion|VFW|Task Force|Delegation|"
    r"Group|Team|Board of (?:Directors|Education|Commissioners|Trustees)))\b", re.I)
SERVED = re.compile(r"\b(?:served|serves|serving|sat|sits|sitting)\s+(?:on|as|in)\s+(?:the\s+)?(?P<org>[A-Z][\w&'’.\- ]{3,70}?"
                    r"(?:Caucus|Association|Chamber(?: of Commerce)?|Union|Club|Coalition|Committee|Board|Commission|Authority|"
                    r"Foundation|Council|League|Task Force|Board of (?:Directors|Education|Commissioners|Trustees)))\b", re.I)
ENDORSED_BY = re.compile(r"\b(?:endorsed|backed|supported)\s+by\s+(?:the\s+)?(?P<who>[A-Z][\w&'’.\- ]{2,80}?)(?=[,.;:!)\n]|\s+and\s+|\s+for\s+|\s+in\s+|$)")
ENDORSEMENT_OF = re.compile(r"\b(?:endorsement|support|backing)\s+(?:of|from)\s+(?:the\s+)?(?P<who>[A-Z][\w&'’.\- ]{2,80}?)(?=[,.;:!)\n]|\s+and\s+|\s+for\s+|\s+in\s+|$)")
X_ENDORSES = re.compile(r"(?P<who>(?:The\s+)?[A-Z][\w&'’.\- ]{2,80}?)\s+(?:has\s+|have\s+|today\s+|formally\s+|officially\s+)?"
                        r"endorse[sd]?\s+(?P<whom>[A-Z][\w'’.\- ]{3,60}?)(?=[,.;:!)\n]|\s+for\s+|\s+in\s+|$)")
RATING = re.compile(r"\b(?:rated|received|earned|holds?|has|got|given)\s+(?:an?\s+)?[\"“]?(?P<rating>[A-F][+\-]?|\d{1,3}\s?%|\d{1,3}\s+percent|"
                    r"\d(?:\.\d)?/\d)[\"”]?\s+(?:rating|grade|score|lifetime (?:rating|score))?\s*(?:from|by|with)\s+(?:the\s+)?"
                    r"(?P<org>[A-Z][\w&'’.\- ]{2,70}?)(?=[,.;:!)\n]|\s+in\s+(?:20\d\d)|\s+for\s+|$)", re.I)
RATING2 = re.compile(r"(?P<org>(?:The\s+)?[A-Z][\w&'’.\- ]{2,70}?)\s+(?:gave|gives|rated|graded|scored|awarded)\s+(?:him|her|them|"
                     r"[A-Z][\w'’.\- ]{3,40}?)\s+(?:an?\s+)?[\"“]?(?P<rating>[A-F][+\-]?|\d{1,3}\s?%|\d{1,3}\s+percent)[\"”]?")
YEAR = re.compile(r"\b(20\d\d|19\d\d)\b")
SPEAKER = re.compile(r"^\s*(?:\[[0-9:]+\]\s*)?(?:<v\s+)?(?P<name>[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){0,3})\s*[:>]\s*(?P<said>.+)$")
# words a record puts in someone's mouth: a quotation (curly or straight quotes) and who said it
QUOTE_SPAN = re.compile(r"“([^“”]{30,700})”|\"([^\"“”]{30,700})\"")
QUOTE_KINDS = {"press_release", "page", "document", "feed_item", "article", "news_item", "statement", "official"}
TITLE_WORDS = (r"(?:Rep\.?|Representative|Sen\.?|Senator|Gov\.?|Governor|Lt\.?\s+Gov\.?|Lieutenant\s+Governor|Mayor|Commissioner|"
               r"Council\s?(?:wo)?man|Council\s?[Mm]ember|Chairman|Chairwoman|Chair|Judge|Justice|Sheriff|Chief|Attorney\s+General|"
               r"Superintendent|Solicitor(?:\s+General)?|District\s+Attorney|Clerk|Treasurer|Secretary(?:\s+of\s+State)?|"
               r"Congressman|Congresswoman|President|Speaker|Leader|Dr\.?|Mr\.?|Mrs\.?|Ms\.?)")
SAY = (r"(?:said|says|added|stated|noted|explained|remarked|continued|declared|emphasized|stressed|wrote|"
       r"told\s+[^,.;:“”\"]{2,40}?)")
# a news headline or report about what someone else did ("Georgia House OKs bill to limit ...", "The case targeted ..."),
# copied onto a candidate's site: not their stance unless it says I/we or names them (Electo tools/ai/llm.py not_theirs)
HEADLINE = re.compile(r"^(?:[A-Z][\w'’.&-]*,?\s+){1,6}?(?i:OKs?|pass(?:es|ed)?|approves?|approved|adopts?|adopted|votes?|voted|"
                      r"signs?|signed|vetoe?s|vetoed|announces?|announced|prevails?|rules?|ruled|unveils?|rejects?|rejected|blocks?|"
                      r"blocked|kills?|killed|advances?|advanced|sues|sued|wins?|won|loses?|lost|clears?|cleared|halts?|halted|"
                      r"strikes? down|upholds?|upheld|weighs?|considers?|debates?|launches?|launched|files?|filed|introduces?|"
                      r"introduced|backs?|backed|says|said|warns?|calls? for|urges?|pushes?|eyes|plans?)\b")
REPORTED = re.compile(r"^(?:The|A|An|This|That|These|Those|Its|Their)\s+(?:[\w'’-]+\s+){0,3}?(?:case|lawsuit|suit|court|judge|"
                      r"justices?|plaintiffs?|defendants?|ruling|order|decision|complaint|indictment|investigation|report|study|"
                      r"audit|poll|survey|bill|law|measure|legislature|senate|house|council|commission|board|agency|department|"
                      r"group|coalition|party|campaign|opponents?|critics?|supporters?|administration|governor|mayor)\b"
                      r"[^.;:]{0,60}?\b(?:\w+ed|said|says|argues|claims?|found|ruled|held|sought|seeks|would|will|has|had|is|"
                      r"was|were|are)\b", re.I)
CEREMONY = re.compile(r"(?:congratulations|thank you|thanks to|we thank|happy\b|welcome\b|we (?:mourn|grieve)|our (?:thoughts|prayers|hearts)|"
                      r"(?:marty|i) (?:and i )?(?:are|am) (?:saddened|heartbroken|deeply)|rest in peace)", re.I)
SOMEONE_SAID = re.compile(r"^\s*,?\s*(?:" + SAY + r")\s+(?:the\s+)?[A-Z]|^\s*,?\s*(?:[A-Z][\w.'’-]+\s+){1,4}" + SAY + r"\b")


@lru_cache(maxsize=2)
def _issues(path: str) -> tuple[tuple[str, re.Pattern], ...]:
    p = Path(path)
    if not p.exists():
        return ()
    d = yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    return tuple((name, re.compile(rx, re.I)) for name, rx in (d.get("issues") or {}).items())


# "environment" and "climate" that are not about nature ("our pro-business environment", "a climate for investment")
NOT_NATURE = re.compile(r"\b(?:pro-?\s?)?business(?:-friendly)?\s+(?:environment|climate)\b|\b(?:work|learning|regulatory|economic|"
                        r"political|security|threat|media|school|home|investment|tax)\s+(?:environment|climate)\b|"
                        r"\bclimate\s+(?:for|of)\s+(?:business|investment|growth|innovation|fear)\b", re.I)


def issues_of(text: str, root: Path | None = None) -> list[str]:
    hay = NOT_NATURE.sub(" ", (text or "")[:4000])
    out = [name for name, rx in _issues(str((root or ROOT) / "config" / "issues.yaml")) if rx.search(hay)]
    if not out:
        out = tag(hay, path=(root or ROOT) / "config" / "topics.yaml")
    return out


SENT_SPLIT = re.compile(r"(?<=[.!?])(?<![A-Z]\.)(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bGov\.)(?<!\bLt\.)(?<!\bSen\.)"
                        r"(?<!\bRep\.)(?<!\bNo\.)(?<!\bSt\.)(?<!\bJr\.)(?<!\bSr\.)(?<!\bvs\.)(?<!\bU\.S\.)\s+(?=[A-Z\"“(])")


MD_LINK = re.compile(r"!?\[\[?([^\[\]]*)\]?\]\([^)]*\)")      # [text](url), and [[email protected]](url) as some hosts write addresses
BULLET = re.compile(r"^(?:[-*+•]|\d{1,2}[.)])\s+")


def not_prose(line: str) -> bool:
    """A table row, a rule or lone mark, or a line that is nothing but links (a menu, a button, a footnote)."""
    t = line.strip()
    return (not t or t.startswith("|") or not re.search(r"[A-Za-z]", t)
            or bool(MD_LINK.search(t)) and len(re.sub(r"[^A-Za-z]", "", MD_LINK.sub("", t))) < 12)


def plain(line: str) -> str:
    """A line without its Markdown marks: a link is its words, emphasis marks go."""
    t = MD_LINK.sub(r"\1", line)
    return re.sub(r"\*{2,}|__|`|/\$|(?<![\w*])[*_](?=\S)|(?<=\S)[*_](?![\w*])", "", t).strip()


def prose_blocks(text: str) -> list[str]:
    """A page's paragraphs as plain prose, one string each. The crawler keeps pages as Markdown: headings, menus of
    links, table rows and rules are not prose and are left out (headlines() reads the headings), each list item is a
    block of its own, and link and emphasis marks are removed. Sentences are then cut inside one block only: read as
    one run of text, a page gave "sentences" like "...what matters most to families. #### Smart Growth ####
    Efficient, Transparent Government" and "- [HOME](...) - [ABOUT](...) # Where X Stands" (Nov 9: three in ten of the
    positions collected from campaign sites carried such page furniture)."""
    out: list[str] = []
    for para in re.split(r"\n[ \t]*\n", text or ""):
        cur: list[str] = []
        for ln in para.splitlines():
            t = ln.strip()
            if t.startswith("#") or not_prose(t):
                continue                                  # a heading, a table row, a rule, a menu or a button
            item = BULLET.match(t)
            t = plain(BULLET.sub("", t))
            if not t:
                continue
            if item and cur:                              # a list item starts a block of its own
                out.append(" ".join(cur))
                cur = []
            cur.append(t)
            if item:
                out.append(" ".join(cur))
                cur = []
        if cur:
            out.append(" ".join(cur))
    return out


def split_sentences(text: str) -> list[str]:
    """Sentences, without breaking after initials ("Brian P. Kemp") or common abbreviations, and never running from
    one paragraph, heading or list item into the next (prose_blocks)."""
    return [p.strip() for b in prose_blocks(text) for p in SENT_SPLIT.split(re.sub(r"\s+", " ", b)) if p.strip()]


@lru_cache(maxsize=1024)
def sentences(text: str) -> tuple[str, ...]:
    """Sentence split, memoized: each record's text is read by several extractors (positions, markers,
    affiliations, endorsements, ratings), and is split once."""
    return tuple(p for p in split_sentences(text) if 25 <= len(p) <= 420)


OTHER_SAID = re.compile(r"(?:^|[:.;]\s*)(?:Rep\.|Sen\.|Gov\.|Dr\.)?\s*(?!(?:He|She|They|It|We|The|This|That)\b)([A-Z][a-z]+)\s+"
                        r"(?:cited|said|says|told|argued|argues|noted|wrote|warned|listed|named|described|praised|criticized|added|explained|stated)\b")


class TranscriptSpeakers:
    """Speaker-labeled lines with a stance cue in each jurisdiction's transcripts, indexed by speaker surname,
    built once per jurisdiction per run (instead of re-reading every transcript for every candidate)."""

    def __init__(self, store: Store):
        self.store = store
        self._idx: dict[str, dict[str, list[tuple]]] = {}

    def lines(self, jid: str, last: str) -> list[tuple]:
        if jid not in self._idx:
            idx: dict[str, list[tuple]] = {}
            for t in self.store.iter_query("jurisdiction=? AND kind='transcript'", (jid,)):
                meta = t.get("meta") or {}
                tmin = {"url": t["url"], "title": t.get("title"), "kind": "transcript", "evidence": t.get("evidence"),
                        "published": meta.get("meeting_date") or t.get("published"), "fetched_at": t.get("fetched_at"),
                        "meta": {"body": meta.get("body"), "meeting_date": meta.get("meeting_date")}}
                for ln in (t.get("text") or "").splitlines():
                    if ":" not in ln[:80] and ">" not in ln[:80]:
                        continue                      # no speaker label: skip the regex
                    m = SPEAKER.match(ln)
                    if not m:
                        continue
                    said = m.group("said").strip()
                    if not (25 <= len(said) <= 600) or not STANCE_CUES.search(said):
                        continue
                    name = m.group("name").lower()
                    idx.setdefault(name.split()[-1], []).append((tmin, name, said))
            self._idx[jid] = idx
        return self._idx[jid].get(last, [])


def _clean_org(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" ,.;:'’\"“”")
    s = re.sub(r"^(?:the|a|an)\s+", "", s, flags=re.I)
    return s


def _split_names(tail: str) -> list[str]:
    """'Georgia Chamber of Commerce and Sheriff Dana Reed. I ...' -> both names (stops at the sentence end)."""
    tail = re.split(r"[.;:!?\n]|\s+(?:for|in|on|at|to|because|who|which)\s+", tail, maxsplit=1)[0]
    parts = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*", tail)
    out = []
    for p in parts:
        p = _clean_org(p)
        if p and p[0].isupper() and 3 <= len(p) <= 80:
            out.append(p)
    return out


def _d10(r: dict) -> str:
    d = r.get("published") or r.get("fetched_at") or ""
    if d and not d[:1].isdigit():          # a feed's date as written ("Wed, 21 Oct 2026 14:00:00 -0400")
        try:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(d).date().isoformat()
        except (TypeError, ValueError, IndexError):
            d = r.get("fetched_at") or ""
    return d[:10]


ORD_VOTE = re.compile(r"\b(ordinance|resolution)\b", re.I)


class SourceList:
    """Electo's sources[] with {t,u,d}; src indexes are 0-based."""

    def __init__(self):
        self.items: list[dict] = []
        self.index: dict[str, int] = {}

    def add(self, r: dict, title: str | None = None) -> int:
        u = r.get("url") or ""
        k = re.sub(r"^https?://(www\.)?", "", u).rstrip("/").lower()
        if k in self.index:
            return self.index[k]
        self.index[k] = len(self.items)
        self.items.append({"t": (title or r.get("title") or "").strip()[:200] or u, "u": u, "d": _d10(r),
                           "kind": r.get("kind"), "evidence": r.get("evidence") or "primary"})
        return self.index[k]


class Extractor:
    def __init__(self, cand: dict, root: Path | None = None):
        self.c = cand
        self.root = root or ROOT
        self.variants = cand.get("variants") or name_variants(cand["name"], cand.get("aliases", []))
        self.rx = name_regex(self.variants)
        self.last = (self.variants[0].split()[-1] if self.variants else cand["name"].split()[-1]).lower()
        self.first = (self.variants[0].split()[0] if self.variants else cand["name"].split()[0]).lower()

    def named(self, s: str) -> bool:
        """The sentence names the candidate: full name, or the surname alone (coverage switches to it after the
        first mention) when the surname is distinctive enough."""
        if self.rx.search(s):
            return True
        return len(self.last) >= 4 and bool(re.search(r"\b" + re.escape(self.last) + r"\b", s, re.I))

    # ------------------------------------------------------------ positions and statements
    def third_person_record(self, text: str) -> list[str]:
        """On their own site, sentences about them in the third person that say what they did or back
        ("Hutchinson is sponsoring legislation to remove ...", "Rep. X filed House Bill 1212, which would ...")."""
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s) or not self.named(s) or not LEGIS_DID.search(s) or len(s) < 40:
                continue
            out.append(s)
        return out[:15]

    def headlines(self, text: str) -> list[str]:
        """The site's own headings and slogans (# lines) of five or more words: what the campaign leads with."""
        out = []
        for line in (text or "").splitlines():
            t = line.strip()
            if not t.startswith("#"):
                continue
            t = re.sub(r"^#+\s*", "", t).strip(" .!")
            t = re.sub(r"[*_\[\]]", "", t)
            if len(t.split()) >= 5 and not NOISE_SENT.search(t) and not re.search(r"\b(cookie|menu|copyright|donate now|sign up|subscribe)\b", t, re.I):
                out.append(t[:240])
        return out[:8]

    def own_sentences(self, text: str) -> list[str]:
        """Sentences from the candidate's own channel that take a stance."""
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s) or not STANCE_CUES.search(s) or self.someone_elses(s):
                continue
            if ENDORSED_BY.search(s) and not FIRST_PERSON.search(s):
                continue
            out.append(s)
        return out

    def issue_sentences(self, text: str) -> list[str]:
        """Sentences and bullet lines from an issues page that name a policy area, even with no stance verb (an issues
        page's headings and bullets are the candidate's own statement of priorities)."""
        out = []
        for raw in re.split(r"\n+", text or ""):
            if not_prose(raw):
                continue                                  # a table row, a menu entry, a footnote link
            line = plain(re.sub(r"^[#>*\-\d.\s]+", "", raw))
            if not (24 <= len(line) <= 420) or NOISE_SENT.search(line) or ENDORSED_BY.search(line):
                continue
            if not issues_of(line, self.root):
                continue
            if self.someone_elses(line):
                continue                                  # a headline or report about others on their news page
            out.append(line)
        return out

    def someone_elses(self, s: str) -> bool:
        """A sentence on their own channel that is not their stance: a report of what others did, with no I/we and no
        mention of them; or someone speaking about them in the first person (a testimonial or a letter posted on their
        site: "Amelia Inman is an excellent community leader and I really appreciate her board ...")."""
        # another named person's words reported ("Au cited gun safety and tobacco tax legislation as two long-term
        # priorities." on a page that names this candidate too: Nov 11, in Kay Kirkpatrick's positions)
        for m in OTHER_SAID.finditer(s):
            if m.group(1).lower() not in (self.last.lower(), self.first.lower()):
                return True
        if FIRST_PERSON.search(s):
            L = re.escape(self.last)
            return bool(re.search(r"\b" + L + r"(?:['’]s\b|\s+(?:is|was|has|had|will|would|can|could|helped|gave|made|serves|served|"
                                  r"believes|works|worked|truly|really|always|recognized|supported|showed)\b)", s, re.I)
                        and not re.search(r"\b(?:i am|i['’]m|my name is|this is)\s+(?:\w+\s+){0,2}?" + L + r"\b", s, re.I))
        return not self.named(s) and bool(HEADLINE.match(s) or REPORTED.match(s))

    def controversies(self, text: str, own: bool = False) -> list[dict]:
        """Sentences that name the candidate and report an investigation, charge, lawsuit, ethics matter, sanction,
        resignation under pressure or the like: [{text, cues}]. A soft cue alone (controversy, backlash, apology)
        needs a second cue in the same sentence. The candidate's own channel is read too (a denial or apology is
        still a controversy, in their words)."""
        out, seen = [], set()
        if len(self.last) < 3:
            return out
        L = re.escape(self.last)
        subj = re.compile(r"\b" + L + r"\b[^.;:]{0,70}?\b(?:was|were|has been|had been|is|is being|being|got|been|,? who (?:was|is|has been))?\s*" + STRONG_PASSIVE + r"\b"
                          r"|\b" + L + r"\b[^.;:]{0,50}?\b(?:" + STRONG_ACTIVE + r")\b"
                          r"|\b" + STRONG_OF + r"\s+(?:[\w.'’-]+\s+){0,5}?" + L + r"\b"
                          r"|\b(?:sued|indicted|charged|investigating|censured|reprimanded|fined|subpoenaed|accused)\s+(?:[\w.'’-]+\s+){0,5}?" + L + r"\b", re.I)
        F = re.escape(self.first)
        TITLE = (r"(?:Rep\.?|Representative|Sen\.?|Senator|Gov\.?|Governor|Mayor|Commissioner|Councilm[ae]n|Council\s?member|"
                 r"Council Member|Chairman|Chairwoman|Chair|Judge|Sheriff|Chief|Attorney General|Superintendent|Solicitor|"
                 r"District Attorney|D\.A\.|Clerk|Treasurer|Lt\.? Gov\.?|Secretary|Congressman|Congresswoman)")
        # who: their full name (not as the first two words of someone else's longer name) or a title with their last name
        who = re.compile(r"\b(?i:" + F + r")\.?\s+(?:[A-Z]\.\s+|[A-Z][a-z]+\s+)?(?i:" + L + r")\b(?!\s+[A-Z][a-z]{2,}\b(?!\s+(?:said|says|was|is|has|had|of|on|in|for|told)))"
                         r"|\b" + TITLE + r"\s+(?:[A-Z][\w.'’-]+\s+){0,2}?(?i:" + L + r")\b")
        for s in sentences(text):
            if NOISE_SENT.search(s) or not self.named(s):
                continue
            if not who.search(s):
                continue
            if not subj.search(s):
                continue
            cues = sorted({m.group(1).lower() for m in CONTROVERSY.finditer(s)})
            if not cues or (len(cues) == 1 and CONTROVERSY_SOFT.search(cues[0])):
                continue
            if sum(ch.isalpha() for ch in s) < 0.6 * len(s):
                continue          # OCR noise
            k = re.sub(r"[^a-z]", "", s.lower())[:100]
            if k in seen:
                continue
            seen.add(k)
            out.append({"text": s[:480], "cues": cues[:6]})
        return out[:10]

    def reported_sentences(self, text: str) -> list[tuple[str, bool]]:
        """(sentence, attributed) from coverage: the candidate named with a stance cue; attributed when a speech
        verb or a quotation ties the words to them."""
        out = []
        sents = sentences(text)
        for i, s in enumerate(sents):
            if NOISE_SENT.search(s) or not STANCE_CUES.search(s):
                continue
            named = self.named(s)
            if not named:
                # a quoted sentence right after one naming the candidate ("..." he said.)
                prev = sents[i - 1] if i else ""
                if not (prev and self.rx.search(prev) and re.search(r"[\"“]", s) and ATTRIB.search(s)):
                    continue
            attributed = bool(ATTRIB.search(s)) or bool(re.search(r"[\"“][^\"”]{12,}[\"”]", s))
            out.append((s, attributed))
        return out

    def attributed_quotes(self, text: str) -> list[str]:
        """Quotations a record attributes to the candidate: "…," said Governor Brian Kemp. / Kemp said, "…" / the
        quotation that goes straight on after one of theirs ("…," said Kemp. "We are proud …"). Never the surname after
        someone else's first name (First Lady Marty Kemp), and a bare surname only when the record also names the
        candidate in full and no one else of that surname."""
        if len(self.last) < 3:
            return []
        if not hasattr(self, "_q"):
            firsts = sorted({v.split()[0] for v in self.variants if len(v.split()) > 1} | {self.first.title()}, key=len, reverse=True)
            F, L = "|".join(re.escape(f) for f in firsts), re.escape(self.last)
            spk = (r"(?:(?P<t>" + TITLE_WORDS + r")\s+)?(?:(?P<f>(?i:" + F + r"))\.?\s+(?:[A-Z]\.\s+|[A-Z][a-z]+\s+)?)?(?i:" + L + r")\b(?!['’]s)")
            self._q = (re.compile(r"^\s*,?\s*(?:" + SAY + r")\s+" + spk),
                       re.compile(r"^\s*,?\s*" + spk + r"(?:,\s[^,“”\"]{2,80},)?\s+" + SAY + r"\b"),
                       re.compile(spk + r"(?:,\s[^,“”\"]{2,80},)?\s+" + SAY + r"(?:\s+[a-z][\w-]*){0,4}?\s*[,:]?\s*$"),
                       re.compile(r"\b([A-Z][a-z]+)\s+(?:[A-Z]\.\s+)?(?i:" + L + r")\b"),
                       {f.lower() for f in firsts})
        after_say, after_name, before, others, firsts = self._q
        t = re.sub(r"\*\*|__", "", text or "")              # bold in excerpts (**said Governor Brian Kemp**)
        t = re.sub(r"[ \t]+", " ", t)

        def ok_speaker(m: re.Match, at: int) -> bool:
            if not m:
                return False
            start = at + m.start("t") if m.group("t") else at + m.start("f") if m.group("f") else None
            if start is None:                                # the surname alone
                pos = at + m.start()
                around = t[max(0, pos - 3000):pos + 400]
                if not self.rx.search(t[max(0, pos - 3000):pos]):
                    return False                             # not named in full before
                if any(w.lower() not in firsts and not re.fullmatch(TITLE_WORDS, w) for w in others.findall(around)):
                    return False                             # someone else of that surname is in the record
                start = pos
            prev = re.findall(r"([A-Za-z][\w.'’-]*)\s*$", t[max(0, start - 30):start])
            return not (prev and re.fullmatch(r"[A-Z][a-z]+", prev[0]) and prev[0].lower() not in firsts
                        and not re.fullmatch(TITLE_WORDS, prev[0]))

        out, chain_end, chained = [], -100, 0
        for m in QUOTE_SPAN.finditer(t):
            q = (m.group(1) or m.group(2) or "").strip()
            tail = t[m.end():m.end() + 140]
            mine = False
            a = after_say.match(tail) or after_name.match(tail)
            if a and ok_speaker(a, m.end()):
                mine, chained, chain_end = True, 0, m.end() + a.end()
            elif not SOMEONE_SAID.match(tail):
                b = before.search(t[max(0, m.start() - 160):m.start()])
                if b and ok_speaker(b, max(0, m.start() - 160)):
                    mine, chained, chain_end = True, 0, m.end()
                elif chained < 2 and 0 <= m.start() - chain_end <= 6 and re.fullmatch(r"[\s.,;:]*", t[chain_end:m.start()]):
                    mine, chained, chain_end = True, chained + 1, m.end()     # the quotation goes straight on
            if not mine:
                continue
            q = re.sub(r"\s+", " ", q).strip(" ,")
            if len(q) < 40 or "http" in q or NOISE_SENT.search(q) or sum(ch.isalpha() for ch in q) < 0.6 * len(q):
                continue
            if CEREMONY.match(q):
                continue                                     # congratulations, thanks, condolences: no stance in them
            if q[-1] not in ".!?":
                q += "."
            out.append("“" + q + "”")
        return list(dict.fromkeys(out))

    def transcript_utterances(self, text: str) -> list[str]:
        """Lines a transcript attributes to the candidate (speaker labels)."""
        out = []
        for ln in (text or "").splitlines():
            m = SPEAKER.match(ln)
            if not m:
                continue
            name = m.group("name").lower()
            if self.last in name and (self.first in name or len(name.split()) == 1 or name.endswith(self.last)):
                said = m.group("said").strip()
                if STANCE_CUES.search(said) and 25 <= len(said) <= 600:
                    out.append(said)
        return out

    # ------------------------------------------------------------ ideology, affiliations, endorsements, ratings
    def markers(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s):
                continue
            about_cand = bool(own and FIRST_PERSON.search(s)) or self.named(s) or (own and not re.search(r"\b(endorse|opponent)\b", s, re.I))
            if not about_cand:
                continue
            m = SELF_DESC.search(s)
            if m and (own or re.search(r"describ\w+ (?:him|her|them)self|calls? (?:him|her|them)self|self-described|identif\w+ as", s, re.I)):
                out.append({"marker": "self-description", "phrase": m.group("label").lower(), "text": s[:300]})
            for lab, rx in IDEOLOGY_MARKERS.items():
                mm = re.search(rx, s)
                if mm and (own and FIRST_PERSON.search(s) or (not own and self.named(s) and ATTRIB.search(s))):
                    out.append({"marker": lab, "phrase": mm.group(0).lower(), "text": s[:300]})
        return out

    def affiliations(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s):
                continue
            if not own and not self.named(s):
                continue
            for m in AFFIL.finditer(s):
                out.append({"org": _clean_org(m.group("org")), "role": m.group("role").lower(), "text": s[:300]})
            for m in SERVED.finditer(s):
                out.append({"org": _clean_org(m.group("org")), "role": "served on", "text": s[:300]})
        return out

    def endorsements(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s):
                continue
            named = self.named(s)
            if not own and not named:
                continue
            for rx in (ENDORSED_BY, ENDORSEMENT_OF):
                for m in rx.finditer(s):
                    for who in _split_names(s[m.start("who"):]):
                        out.append({"name": who, "direction": "received", "text": s[:300]})
            for m in X_ENDORSES.finditer(s):
                who, whom = _clean_org(m.group("who")), _clean_org(m.group("whom"))
                if self.named(whom):
                    out.append({"name": who, "direction": "received", "text": s[:300]})
                elif self.named(who) or (own and re.search(r"\b(i|we)\b", who, re.I)):
                    out.append({"name": whom, "direction": "given", "text": s[:300]})
        seen, uniq = set(), []
        for e in out:
            k = (e["name"].lower(), e["direction"])
            if e["name"] and len(e["name"]) > 2 and k not in seen and not self.rx.search(e["name"]):
                seen.add(k)
                uniq.append(e)
        return uniq

    def ratings(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if not own and not self.named(s):
                continue
            for rx in (RATING, RATING2):
                for m in rx.finditer(s):
                    org = _clean_org(m.group("org"))
                    if len(org) < 3 or re.search(r"\b(the|a|an)$", org, re.I):
                        continue
                    y = YEAR.search(s)
                    out.append({"org": org, "rating": m.group("rating").replace(" percent", "%").replace(" ", ""),
                                "year": y.group(1) if y else "", "text": s[:300]})
        return out


# ----------------------------------------------------------------------------- the profile supplement
def sentence_key(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())[:120]


def boilerplate(store: Store, min_candidates: int = 3) -> frozenset[str]:
    """Sentences found word for word on the websites of three or more different candidates: a site builder's filler
    ("Begin by discussing the primary theme or topic that you plan to cover ...", "Edit or delete it, then start
    writing!", an accessibility statement's blanks, a donation form's messages), never one candidate's own words.
    Built once per run from the stored pages (keys as sentence_key)."""
    seen: dict[str, set[str]] = {}
    for r in store.iter_query("kind='statement'"):
        sid = r.get("source_id") or ""
        for sent in set(split_sentences(r.get("text") or "")):
            if 25 <= len(sent) <= 420:
                seen.setdefault(sentence_key(sent), set()).add(sid)
    return frozenset(k for k, v in seen.items() if len(v) >= min_candidates)


def profile_supplement(cand: dict, store: Store, root: Path | None = None, outlets: set[str] | None = None,
                       money: dict | None = None, career: list[dict] | None = None, legis: dict | None = None,
                       speakers: TranscriptSpeakers | None = None, records: dict | None = None,
                       filler: frozenset[str] | None = None) -> dict:
    """Everything the crawler has on one candidate, in Electo's profile item shapes with src indexes.
    speakers: a TranscriptSpeakers shared across candidates (built on the fly when not given).
    records: the candidate's records by kind when the caller already read them ({} = none); else read here."""
    ex = Extractor(cand, root)
    speakers = speakers or TranscriptSpeakers(store)
    sid = f"cand:{cand['id']}"
    S = SourceList()
    positions, statements, markers, affils, endos, rates, votes, bills = [], [], [], [], [], [], [], []
    seen_pos: set[str] = set()
    controversies: list[dict] = []
    site = {"pages": 0, "urls": []}
    headlines: list[dict] = []
    seen_con: set[str] = set()

    def add_controversy(item: dict, r: dict, basis: str, evidence: str):
        k = re.sub(r"[^a-z]", "", item["text"].lower())[:100]
        if k in seen_con:
            return
        seen_con.add(k)
        controversies.append({"text": item["text"], "cues": item["cues"], "d": _d10(r), "src": [S.add(r)], "basis": basis, "evidence": evidence})

    def add_position(text: str, r: dict, basis: str, evidence: str, review: bool = False):
        key = sentence_key(text)
        if key in seen_pos or (filler and key in filler and basis.startswith("campaign site")):
            return                                        # said already, or a site builder's filler (boilerplate)
        seen_pos.add(key)
        topics = issues_of(text, root)
        i = S.add(r)
        item = {"stance": text[:420], "d": _d10(r), "src": [i], "basis": basis, "evidence": evidence}
        if review:
            item["needs_review"] = True
        if topics:
            for t in topics[:2]:
                positions.append({"topic": t, **item})
        else:
            statements.append({"d": _d10(r), "text": text[:420], "src": [i], "basis": basis, "evidence": evidence})

    def by_kind(kind: str) -> list[dict]:
        rows = records.get(kind, []) if records is not None else store.read(sid, kind)
        return sorted(rows, key=_d10, reverse=True)

    # own channels
    for r in by_kind("statement") + by_kind("post"):
        if r.get("evidence") == "unverified":
            continue
        basis = "campaign site" if r["kind"] == "statement" else f"verified {r.get('meta', {}).get('platform') or 'social'} post"
        txt = r.get("text") or ""
        if r["kind"] == "statement":
            host = re.sub(r"^https?://(www\.)?", "", r["url"]).split("/")[0].lower()
            if re.search(r"(?:^|\.)(?:wikipedia\.org|wikimedia\.org|wikidata\.org|ballotpedia\.org|ontheissues\.org|votesmart\.org|isidewith\.com)$", host):
                # an encyclopedia's or an issues digest's page about them is not their own channel (Nov 9: OnTheIssues pages
                # taken for Jim Barksdale's site gave him a congressman's floor votes and other candidates' endorsements)
                site["not_theirs"] = site.get("not_theirs", 0) + 1
                continue
            # a page that neither names them nor sits on a domain with their name is not their site (an expired
            # domain bought by someone else, a parked page): read nothing from it
            words = re.findall(r"[a-z]+", txt.lower())
            english = sum(w in EN_COMMON for w in words) / max(1, len(words))
            if not (ex.named(txt) or (len(ex.last) >= 3 and ex.last in host.replace("-", ""))) \
                    or (not ex.named(txt) and (SPAM.search(txt) or (len(words) > 30 and english < 0.12))):
                site["not_theirs"] = site.get("not_theirs", 0) + 1
                continue
            if PLACEHOLDER.search(txt) and len(words) < 120:
                site["placeholder"] = site.get("placeholder", 0) + 1     # a template page with nothing of theirs on it
                site["pages"] += 1
                if len(site["urls"]) < 3:
                    site["urls"].append(r["url"])
                continue
            site["pages"] += 1
            if len(site["urls"]) < 3:
                site["urls"].append(r["url"])
            if len(re.sub(r"[^A-Za-z]", "", txt)) < 40:
                site["unreadable"] = site.get("unreadable", 0) + 1     # a script-built page with no text to read
        own = ex.own_sentences(txt)[:40]
        for s in own:
            add_position(s, r, basis, "primary")
        if r["kind"] == "statement":
            for s in ex.third_person_record(txt):
                add_position(s, r, "campaign site (their record, as the site describes it)", "primary")
        # an issues page: its headings and bullets name priorities even without a stance verb, so a site that was
        # read always yields stances
        if r["kind"] == "statement" and (ISSUE_PAGE.search(r["url"]) or not own):
            for s in ex.issue_sentences(txt)[:25]:
                add_position(s, r, "campaign site (issues page)", "primary")
        if r["kind"] == "statement":
            for h in ex.headlines(txt):
                headlines.append({"text": h, "d": _d10(r), "src": [S.add(r)], "basis": "campaign site (headline)", "evidence": "primary"})
        for m in ex.markers(r.get("text") or "", own=True):
            markers.append({**m, "d": _d10(r), "src": [S.add(r)], "basis": basis, "evidence": "primary"})
        for a in ex.affiliations(r.get("text") or "", own=True):
            affils.append({**a, "d": _d10(r), "src": [S.add(r)], "basis": basis, "evidence": "primary"})
        for e in ex.endorsements(r.get("text") or "", own=True):
            endos.append({**e, "d": _d10(r), "src": [S.add(r)], "basis": basis + " (self-reported)", "evidence": "primary"})
        for rt in ex.ratings(r.get("text") or "", own=True):
            rates.append({**rt, "src": [S.add(r)], "basis": basis + " (self-reported)", "evidence": "primary"})
    # what they said at meetings: transcripts in their jurisdictions where they are the labeled speaker
    jids = [j for j in cand.get("jurisdictions", []) if j]
    per_transcript: dict[str, int] = {}
    for jid in jids:
        for tmin, name, said in speakers.lines(jid, ex.last):
            if not (ex.first in name or len(name.split()) == 1 or name.endswith(ex.last)):
                continue
            if per_transcript.get(tmin["url"], 0) >= 30 or (tmin["url"] not in per_transcript and len(per_transcript) >= 400):
                continue
            per_transcript[tmin["url"]] = per_transcript.get(tmin["url"], 0) + 1
            meta = tmin["meta"]
            label = f"said at {meta.get('body') or 'a meeting'} {meta.get('meeting_date') or _d10(tmin)}".strip()
            add_position(said, tmin, label, "primary", review=True)
    # coverage by listed outlets (reported): named + attributed
    for r in by_kind("article"):
        if r.get("evidence") != "reported":
            continue
        outlet = r.get("meta", {}).get("outlet") or ""
        for s, attributed in ex.reported_sentences(r.get("text") or "")[:12]:
            if attributed:
                add_position(s, r, f"reported by {outlet}".strip(), "reported")
        for m in ex.markers(r.get("text") or "", own=False):
            markers.append({**m, "d": _d10(r), "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
        for a in ex.affiliations(r.get("text") or "", own=False):
            affils.append({**a, "d": _d10(r), "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
        for e in ex.endorsements(r.get("text") or "", own=False):
            endos.append({**e, "d": _d10(r), "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
        for rt in ex.ratings(r.get("text") or "", own=False):
            rates.append({**rt, "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
        for c in ex.controversies(r.get("text") or ""):
            add_controversy(c, r, f"reported by {outlet}".strip(), "reported")
    # official records that name them (a .gov page, an ethics or court record the crawler opened) are primary evidence
    for r in by_kind("page") + by_kind("document") + by_kind("mention"):
        if r.get("evidence") != "primary":
            continue
        title = (r.get("title") or "").strip()
        if title and sum(ch.isalpha() or ch == " " for ch in title) < 0.8 * len(title):
            title = ""        # a scanned page's OCR title: name the record by its address instead
        for c in ex.controversies(r.get("text") or "")[:3]:
            add_controversy(c, r, "official record: " + (title or r["url"])[:80], "primary")
    # their own words in the records that name them: a quotation a government press release, page or document (or a
    # listed outlet's story) attributes to them. Until Nov 9 those records gave only controversies, so an officeholder
    # whose words live in releases (a governor, a mayor) had no stances at all. The record the mention came from is read
    # whole (the mention keeps two excerpts, which cut quotations short), by its address.
    quoted = 0
    for r in by_kind("mention"):
        m = r.get("meta") or {}
        txt = r.get("text") or ""
        if quoted >= 30 or m.get("record_kind") not in QUOTE_KINDS or not re.search(r"[“\"]", txt) or not ATTRIB.search(txt):
            continue
        try:
            rows = store.query("url=? AND kind=?", (r["url"], m.get("record_kind")), limit=4)
        except Exception:  # noqa: BLE001
            rows = []
        full = next((x for x in rows if x.get("id") == m.get("record_id")), rows[0] if rows else None) or r
        ev = "reported" if full.get("evidence") == "reported" else "primary"
        title = re.sub(r"\s+", " ", (full.get("title") or r.get("title") or "")).strip()
        if not title or sum(ch.isalpha() or ch == " " for ch in title) < 0.8 * len(title):
            title = re.sub(r"^https?://(www\.)?", "", r["url"]).split("/")[0]
        body = full.get("text") or txt
        if len(body) > 60000:          # a long document: the stretches around their name
            spans = [(max(0, x.start() - 2500), x.end() + 2500) for x in re.finditer(r"\b" + re.escape(ex.last) + r"\b", body, re.I)][:40]
            body = "\n".join(body[a:b] for a, b in spans)
        src = {**r, "url": full.get("url") or r["url"], "title": title, "published": full.get("published") or r.get("published")}
        for q in ex.attributed_quotes(body)[:6]:
            before_n = len(positions) + len(statements)
            add_position(q, src, ("quoted by " if ev == "reported" else "quoted in ") + title[:100], ev)
            quoted += (len(positions) + len(statements)) > before_n
    # questionnaires and guides (kind guide_answer, from gacrawl.guides) and endorsement records
    for r in by_kind("guide_answer"):
        m = r.get("meta", {})
        q, a = m.get("question") or "", r.get("text") or ""
        if a:
            add_position(f"{q.strip()} — {a.strip()}" if q else a.strip(), r, f"questionnaire: {m.get('guide') or r.get('title')}",
                         r.get("evidence") or "primary")
    for r in by_kind("connection"):
        m = r.get("meta", {})
        if m.get("type") == "endorsement" and m.get("party"):
            endos.append({"name": m["party"], "direction": m.get("direction", "received"), "text": (r.get("text") or "")[:300],
                          "d": _d10(r), "src": [S.add(r)], "basis": m.get("basis") or ("campaign site (self-reported)" if m.get("self_reported") else "endorser's own page"),
                          "evidence": r.get("evidence") or "primary"})
        elif m.get("type") in ("membership", "affiliation") and m.get("party"):
            affils.append({"org": m["party"], "role": m.get("role", "member"), "text": (r.get("text") or "")[:300], "d": _d10(r),
                           "src": [S.add(r)], "basis": m.get("basis") or "record", "evidence": r.get("evidence") or "primary"})
        elif m.get("type") == "rating" and m.get("party"):
            rates.append({"org": m["party"], "rating": m.get("rating", ""), "year": m.get("year", ""), "src": [S.add(r)],
                          "basis": m.get("basis") or "scorecard", "evidence": r.get("evidence") or "primary"})
    # votes (council roll calls found for this candidate; legislature votes passed in)
    ords_for, ords_against = [], []
    # the newest 400 roll calls, plus every vote on an ordinance or resolution (those are the "supported/opposed" lists)
    if records is not None:
        allv = sorted(records.get("vote", []), key=lambda r: ((r.get("published") or ""), r["id"]), reverse=True)
        recent = allv[:400]
        seen_ids = {r["id"] for r in recent}
        on_ords = [r for r in allv[400:] if ORD_VOTE.search(f"{r.get('title') or ''} {(r.get('meta') or {}).get('ordinance_ref') or ''}")][:600]
    else:
        recent = store.read_recent(sid, "vote", 400)
        seen_ids = {r["id"] for r in recent}
        on_ords = [r for r in store.read_recent(sid, "vote", 600, "lower(title) LIKE '%ordinance%' OR lower(title) LIKE '%resolution%' OR "
                                                           "meta LIKE '%\"ordinance_ref\": \"Ordinance%' OR "
                                                           "meta LIKE '%\"ordinance_ref\": \"Resolution%'")
                   if r["id"] not in seen_ids]
    for r in recent + on_ords:
        in_list = r["id"] in seen_ids
        m = r.get("meta", {})
        v = (m.get("vote") or "").lower()
        item = {"bill": m.get("matter_file") or "", "session": r.get("jurisdiction") or cand.get("jurisdiction") or "",
                "title": (r.get("title") or "")[:200], "vote": m.get("vote"), "d": _d10(r), "src": [S.add(r)],
                "result": m.get("result"), "topics": m.get("topics", []), "basis": m.get("basis") or "roll call"}
        if m.get("procedural"):
            item["procedural"] = True
        if in_list:
            votes.append(item)
        is_ord = bool(re.search(r"\b(ordinance|resolution)\b", f"{item['bill']} {item['title']}", re.I)) and not m.get("procedural")
        if is_ord and v in ("yes", "yea", "aye", "for"):
            ords_for.append(item)
        elif is_ord and v in ("no", "nay", "against"):
            ords_against.append(item)
    for r in by_kind("sponsorship"):
        m = r.get("meta", {})
        item = {"number": m.get("number") or m.get("MatterFile") or "", "session": m.get("session") or r.get("jurisdiction") or "",
                "caption": (r.get("title") or "")[:200], "role": m.get("role") or "sponsor", "d": _d10(r), "src": [S.add(r)],
                "enacted": m.get("enacted"), "status": m.get("status")}
        bills.append(item)
        if re.search(r"\b(ordinance|resolution)\b", f"{item['number']} {item['caption']}", re.I):
            ords_for.append({**item, "bill": item["number"], "title": item["caption"], "vote": "sponsor"})
    if legis:
        member = legis.get("member") or {}
        msrc = S.add({"url": member.get("u"), "title": f"Georgia General Assembly member page: {member.get('name')}", "kind": "legislator",
                      "evidence": "primary"}) if member.get("u") else None
        for v in legis.get("votes", []):
            votes.append({**v, "src": [S.add({"url": v.get("u"), "title": f"{v.get('bill')}: {v.get('title')}", "kind": "vote", "evidence": "primary"})] if v.get("u") else []})
        for b in legis.get("bills", []):
            bills.append({**b, "src": [S.add({"url": b.get("u"), "title": f"{b.get('number')}: {b.get('caption')}", "kind": "bill", "evidence": "primary"})] if b.get("u") else []})
        for cm in legis.get("committees", []):
            if cm.get("name"):
                affils.append({"org": f"{member.get('chamber') or 'General Assembly'} {cm['name']} Committee", "role": (cm.get("role") or "member").lower(),
                               "text": f"{cm.get('role') or 'Member'}, {cm['name']} ({cm.get('session') or ''})".strip(), "d": "",
                               "src": [msrc] if msrc is not None else [], "basis": "legislature member record", "evidence": "primary"})
        if member.get("title"):
            affils.append({"org": f"Georgia {member.get('chamber')} leadership", "role": member["title"].lower(), "text": member["title"], "d": "",
                           "src": [msrc] if msrc is not None else [], "basis": "legislature member record", "evidence": "primary"})
    # dedupe lists
    def uniq(items, keyf):
        seen, out = set(), []
        for x in items:
            k = keyf(x)
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    markers = uniq(markers, lambda m: (m["marker"], m["phrase"], m["text"][:60]))
    affils = uniq(affils, lambda a: (a["org"].lower(), a["role"]))
    endos = uniq(endos, lambda e: (e["name"].lower(), e["direction"]))
    rates = uniq(rates, lambda r: (r["org"].lower(), r["rating"], r.get("year", "")))
    party = cand.get("party") or (cand.get("last_race") or {}).get("party") or ""
    ideology, isrc = compose_ideology(party, markers, affils, rates, endos)
    n = len(positions) + len(statements) + len(rates) + (1 if endos else 0) + (1 if ideology else 0) + len(votes) + len(bills)
    depth = "full" if n >= 12 and positions else "partial" if n >= 4 else "thin" if n else "none"
    out = {
        "key": cand.get("electo_key") or cand.get("key"), "candidate_id": cand["id"], "name": cand["name"], "party": party,
        "as_of": max((x.get("d") or "" for x in S.items), default=""), "office": cand.get("office", ""), "jurisdictions": jids,
        "sources": S.items,
        "ideology": ideology, "isrc": isrc, "ideology_markers": markers[:40],
        "positions": positions[:80], "statements": statements[:40],
        "affiliations": affils[:40], "endorsements": [e["name"] + (f" ({e['d'][:4]})" if e.get("d") else "") for e in endos if e["direction"] == "received"][:40],
        "endorsement_items": endos[:60], "esrc": sorted({i for e in endos for i in e["src"]}),
        "ratings": [{"org": r["org"], "rating": r["rating"], "year": r.get("year", ""), "src": r["src"], "basis": r["basis"]} for r in rates][:40],
        "votes": votes[:300], "bills": bills[:120],
        "ordinances_supported": ords_for[:120], "ordinances_opposed": ords_against[:120],
        "money": money or {}, "career": career or [],
        "site": {**site, "positions": sum(1 for x in positions if x.get("basis", "").startswith("campaign site")),
                 "statements": sum(1 for x in statements if x.get("basis", "").startswith("campaign site")),
                 "headlines": headlines[:8]} if site["pages"] else None,
        "controversies": sorted(controversies, key=lambda c: c.get("d") or "", reverse=True)[:20],
        "inference_inputs": n, "depth": depth,
        "note": "Items quote or cite records the crawler opened; evidence is primary (own channel / official record) or "
                "reported (listed outlet). Nothing here is an inference. needs_review marks speaker-labeled transcript lines. "
                "site: the candidate's own website as read (pages, and how many positions and statements came from it). "
                "controversies: sentences from listed outlets or official records that name the person and report an "
                "investigation, charge, lawsuit, ethics matter, sanction or resignation under pressure; what was reported, "
                "not a finding.",
    }
    return out


def compose_ideology(party: str, markers: list[dict], affils: list[dict], rates: list[dict], endos: list[dict]) -> tuple[str, list[int]]:
    """One sourced line from documented markers only (no guessing from a party label)."""
    parts, src = [], []
    if party:
        parts.append({"D": "Democrat", "R": "Republican", "L": "Libertarian", "I": "Independent", "G": "Green"}.get(party, party))
    selfd = [m for m in markers if m["marker"] == "self-description"]
    if selfd:
        phr = sorted({m["phrase"] for m in selfd})
        parts.append("self-described " + " / ".join(phr[:3]))
        src += [i for m in selfd for i in m["src"]]
    lab = defaultdict(int)
    for m in markers:
        if m["marker"] != "self-description":
            lab[m["marker"]] += 1
            src += m["src"]
    if lab:
        parts.append("language used: " + ", ".join(f"{k} ({v})" for k, v in sorted(lab.items(), key=lambda kv: -kv[1])[:3]))
    cauc = [a for a in affils if re.search(r"caucus", a["org"], re.I)]
    if cauc:
        parts.append("caucus: " + "; ".join(sorted({a["org"] for a in cauc})[:3]))
        src += [i for a in cauc for i in a["src"]]
    if rates:
        parts.append("ratings: " + "; ".join(f"{r['org']} {r['rating']}" for r in rates[:4]))
        src += [i for r in rates[:4] for i in r["src"]]
    recv = [e for e in endos if e["direction"] == "received"]
    if recv:
        parts.append("endorsed by " + "; ".join(sorted({e["name"] for e in recv})[:5]))
        src += [i for e in recv[:5] for i in e["src"]]
    if len(parts) <= 1 and not src:
        return "", []
    return "; ".join(parts), sorted(set(src))
