"""Is this a person's name, and is this a usable e-mail address? Shared by the readers that take people and contacts
from government pages (school superintendents, board members and principals in gacrawl/schools.py; chiefs and
sheriffs in gacrawl/police.py; records contacts in gacrawl/seed.py and the police export).

Page phrases went out as people (Nov 2026 bundle): "Dear Hornet Family", "Valdosta State University" and
"Administrative Assistant" as superintendents, "REQUEST TO APPEAR BEFORE", "Mike Blackerby is in", "To Date With
Your" as a sheriff, "Quick Links", "Hiring New", and the town's own name ("West Point", "Sugar Hill") as police chiefs.
And addresses that cannot be written to: "%20jastevens@...", "//openrecords@...", "u003eclerk@...", "test@test.gov",
another state's county ("noreply@co.walla-walla.wa.us").
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# words that join a sentence, never a name's: "Mike Blackerby is in", "Woods to share their", "To Date With Your"
FUNCTION_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "before", "by", "can", "dear", "for", "from", "has", "have", "he",
    "her", "his", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our", "she", "that", "the", "their",
    "them", "they", "this", "to", "us", "was", "we", "were", "with", "you", "your", "who", "what", "when",
    "where", "how", "why", "not", "all", "about", "after", "up", "out", "date", "share", "recognized", "named", "new",
    "hiring", "quick", "links", "welcome", "meet", "message", "request", "appear", "please", "click", "here", 
    "view", "contact", "join", "apply", "now", "today", "congratulations",
}
# name particles written in lower case ("Ludwig van Beethoven", "Maria de la Cruz")
PARTICLES = {"de", "del", "della", "der", "di", "da", "du", "la", "le", "van", "von", "y", "bin", "ibn", "st."}
# words of an organisation, a place or a job, never of a person's name
ORG_WORDS = re.compile(
    r"\b(university|college|school|schools|academy|institute|county|city|town|state|states|georgia|department|dept|"
    r"office|board|district|assistant|administrative|administration|administrator|secretary|family|families|children|"
    r"students?|parents?|staff|faculty|center|centre|association|church|foundation|inc|llc|corp|company|services?|"
    r"elementary|middle|high|primary|police|sheriff'?s?|precinct|division|bureau|unit|team|committee|council|"
    r"commission|authority|agency|library|museum|hospital|clinic|recreation|government|public|safety|"
    r"citizens?|community|residents?|programs?|news|events?|calendar|menu|directory|employment|careers?|jobs?|"
    r"links?|resources?|forms?|portal|online|website|email|phone|fax|hornets?)\b", re.I)


def _word_ok(x: str) -> bool:
    """One word of a name: an initial ("Q."), or a capital then letters, apostrophes and hyphens ("D'Jaris",
    "McNeal", "Bárbara", "Smith-Jones"), a comma after it allowed."""
    x = x.rstrip(",")
    if re.fullmatch(r"[^\W\d_]\.", x):
        return x[0].isupper()
    x = x.rstrip(".")
    return bool(x) and x[0].isupper() and all(c.isalpha() or c in "'’-" for c in x) and x[-1].isalpha()

SUFFIXES = re.compile(r"^(Jr|Sr|II|III|IV|V|Ed\.?D|Ph\.?D|M\.?D|Esq)\.?,?$", re.I)


def _core(s: str) -> str:
    return re.sub(r"[^a-z]", "", re.sub(r"\b(county|city|town|of|the|consolidated|government|unified)\b", "", (s or "").lower()))


def plausible_person(name: str, jurisdiction: str = "") -> bool:
    """A person's name as a page prints it: two to five words, each capitalised (a lower-case particle aside), none a
    sentence's joining word or an organisation's, not a phrase in capitals with no name's shape, not the place's own
    name, and at least two words longer than an initial."""
    n = re.sub(r"\s+", " ", (name or "").strip(" ,.;:-–|"))
    n = re.sub(r"^(?:Dr|Mr|Mrs|Ms|Rev|Hon)\.?\s+", "", n)
    w = n.split()
    if not 2 <= len(w) <= 5 or len(n) > 60:
        return False
    if any(x.lower().rstrip(",") in FUNCTION_WORDS or (not re.fullmatch(r"\w\.", x) and x.lower().strip(".,") in FUNCTION_WORDS)
           for x in w):
        return False
    if ORG_WORDS.search(n):
        return False
    body = [x for x in w if not SUFFIXES.match(x)]
    if not body or SUFFIXES.match(w[0]):
        return False
    for x in body:
        if x.lower() in PARTICLES:
            continue
        if not _word_ok(x) or len(x) > 20:
            return False
    full = [x for x in body if len(x.strip(".,'’")) > 1 and x.lower() not in PARTICLES]
    if len(full) < 2:
        return False
    # all in capitals: a heading's words unless each is a name's length ("JOHN SMITH" yes, "REQUEST TO APPEAR" no,
    # which the function words already refuse)
    if all(x.isupper() for x in full) and (len(full) > 3 or any(len(x) > 12 for x in full)):
        return False
    if jurisdiction and _core(n) and (_core(n) == _core(jurisdiction) or _core(n) in _core(jurisdiction) and len(_core(n)) >= 6):
        return False
    return True


def archived_original(url: str) -> str:
    """The address an Internet Archive copy is of ("https://web.archive.org/web/2024.../https://x.org/a" -> the
    latter), or "" when the url is not an archive copy."""
    p = urlsplit(url or "")
    if not p.netloc.lower().endswith("archive.org"):
        return ""
    m = re.match(r"^/web/[^/]+/(.+)$", p.path + (("?" + p.query) if p.query else ""))
    if not m:
        return ""
    orig = m.group(1)
    return orig if re.match(r"^https?://", orig) else "http://" + orig


def _host(u: str) -> str:
    return urlsplit(u if "//" in (u or "") else "http://" + (u or "")).netloc.lower().removeprefix("www.")


def own_page(url: str, *sites: str) -> bool:
    """A name read from `url` is believed: the page is not an archive copy, or it is the archive's copy of one of the
    place's own pages (one of `sites`)."""
    orig = archived_original(url)
    if not orig:
        return True
    h = _host(orig)
    for s in sites:
        sh = _host(s)
        if sh and (h == sh or h.endswith("." + sh) or sh.endswith("." + h)):
            return True
    return False


# ----------------------------------------------------------------------------- e-mail addresses
EMAIL_SHAPE = re.compile(r"[a-z0-9._+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
PLACEHOLDER_DOMAINS = re.compile(r"(^|\.)(test|example|domain|email|yourdomain|sample)\.(gov|com|org|net|us)$")
PLACEHOLDER_LOCAL = re.compile(r"^(test|example|name|user|someone|email|noreply|no-reply|donotreply|do-not-reply|"
                               r"mailer-daemon|postmaster)$")
# another state's government domain: "co.walla-walla.wa.us", "something.fl.us"; Georgia's ".ga.us" is kept
OTHER_STATE = re.compile(r"\.(?!ga\.)[a-z]{2}\.us$")


def clean_email(addr: str) -> str:
    """The address as it can be written to, or "": a link's leftovers taken off ("%20", "//", "u003e" from an escaped
    "\\u003e", "mailto:"), lower-cased, and refused when it is a placeholder (test@test.gov) or another state's."""
    a = (addr or "").strip().lower()
    a = re.sub(r"%(?:20|09|0a|0d|3c|3e)", "", a)
    a = re.sub(r"^(?:mailto:|//|/|u003[ce]|\\u003[ce]|&gt;|&lt;|[<>\"'(\[\s])+", "", a)
    a = a.split("?")[0].rstrip(".,;:)>]\"'")
    if not EMAIL_SHAPE.fullmatch(a):
        return ""
    local, dom = a.split("@", 1)
    if PLACEHOLDER_DOMAINS.search(dom) or PLACEHOLDER_LOCAL.match(local) or OTHER_STATE.search(dom):
        return ""
    return a


def clean_emails(addrs) -> list[str]:
    out: list[str] = []
    for x in addrs or []:
        a = clean_email(x)
        if a and a not in out:
            out.append(a)
    return out
