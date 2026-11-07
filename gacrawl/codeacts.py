"""`gacrawl codeacts`: the exact Code changes made by every enacted act of the 2023-2026 sessions, read from the
General Assembly's own as-passed text, so Electo can show the Official Code of Georgia as it reads today.

For each enacted bill (bill records with an Act number, from `gacrawl legis`):
  legislation/detail/{id}                         versions, status history (approval date)
  legislation/document/{library}/{versionId}      the as-passed PDF ("/AP" version, else the newest)

The PDF is read character by character with its drawing layer: Georgia acts print words removed from the Code
struck through and words added underlined. Each character is kept with its mark, so every quoted passage yields
  after   the text as enacted (struck words dropped)        -> the new Code text
  before  the text the act amended (underlined words dropped) -> checked against the current Code text before applying
Instructions ("Said chapter is further amended in Code Section 21-2-386 ... by revising subsection (b) as follows:")
are parsed into targets (Code section, unit path, verb), and Code Revision style word edits ("in paragraph (a)(1), by
replacing "is when" with "means"") into exact find/replace operations. Effective-date sections are parsed per act
section (including acts that change another act's effective date, such as SB 3EX moving SB 189 § 7).

Nothing is guessed here: applying the operations (and verifying each against the Code text it amends) is Electo's
job, since the base text (Release 86) lives there. Records: kind `code_act`, one per act, meta = the parsed act.
Export: electo/code_acts.json.
"""
from __future__ import annotations

import bisect
import datetime as dt
import io
import json
import re
import time
from pathlib import Path

from .fetch import Blocked, Disallowed, OutOfTime
from .store import Record, Store

PARSER_VERSION = 3          # bump to re-parse stored acts (the PDF is fetched again)
SOURCE = "ga:legis"
API = "https://www.legis.ga.gov/api/"
SITE = "https://www.legis.ga.gov"
N, S, U = 0, 1, 2           # plain, struck, underlined

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
DATE_RX = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})")
CITE = r"\d{1,2}[A-Z]?-\d{1,3}[A-Z]?-\d{1,4}(?:\.\d+)*"
CITE_RX = re.compile(CITE)
LABEL = r"\((?:[A-Za-z]{1,6}|\d{1,3})(?:\.\d+)*\)"
LEAD_RX = re.compile(r"^((?:" + LABEL + r"\s*)+)")
SECTION_HDR = re.compile(r"^\s*SECTION\s+(\d+[A-Za-z]?(?:[-.]\d+)*[A-Za-z]?)\.\s*$")
PART_HDR = re.compile(r"^\s*PART\s+([IVXLC]+|\d+)\s*$")
CITE_LINE = re.compile(r"^\s*(" + CITE + r")\.\s*(.*)$")
STRUCT_LINE = re.compile(r"^\s*(ARTICLE|Article|PART|Part|SUBPART|Subpart|CHAPTER|Chapter)\s+(\d+[A-Z]?(?:\.\d+)?|[IVXLC]+)\s*$")
NOUNS = {"subsection": "SS", "subsections": "SS", "paragraph": "P", "paragraphs": "P", "subparagraph": "SP",
         "subparagraphs": "SP", "division": "DV", "divisions": "DV", "subdivision": "SD", "subdivisions": "SD"}
BIG = {"article": "article", "articles": "article", "part": "part", "parts": "part", "subpart": "subpart",
       "subparts": "subpart", "chapter": "chapter", "chapters": "chapter"}


# ============================================================================ PDF -> marked lines
class Stream:
    """The enacting part of an act as one string with a mark per character and the bill line of each offset."""

    def __init__(self):
        self.chars: list[str] = []
        self.marks: list[int] = []
        self.starts: list[int] = []      # offset where each line starts
        self.nums: list[int] = []        # bill line number of each line
        self.pages: list[int] = []

    def add_line(self, num: int, page: int, cells: list[tuple[str, int]]):
        self.starts.append(len(self.chars))
        self.nums.append(num)
        self.pages.append(page)
        for ch, m in cells:
            self.chars.append(ch)
            self.marks.append(m)
        self.chars.append("\n")
        self.marks.append(N)

    @property
    def raw(self) -> str:
        if not hasattr(self, "_raw"):
            self._raw = "".join(self.chars)
        return self._raw

    def line_at(self, off: int) -> int:
        i = bisect.bisect_right(self.starts, off) - 1
        return self.nums[max(0, i)] if self.nums else 0

    def page_at(self, off: int) -> int:
        i = bisect.bisect_right(self.starts, off) - 1
        return self.pages[max(0, i)] if self.pages else 0

    def text(self, a: int, b: int, drop: int | None = None) -> str:
        """Text of [a, b): drop=S gives the enacted text, drop=U the amended text, None everything."""
        if drop is None:
            return self.raw[a:b]
        return "".join(c for c, m in zip(self.chars[a:b], self.marks[a:b]) if m != drop)

    def marked(self, a: int, b: int) -> bool:
        return any(m != N for m, c in zip(self.marks[a:b], self.chars[a:b]) if not c.isspace())


QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'", " ": " ", "ﬁ": "fi", "ﬂ": "fl"}


def read_pdf(data: bytes) -> Stream:
    """Numbered lines of a Georgia bill PDF, each character with its strike/underline mark."""
    import pdfplumber

    st = Stream()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            marks = [((r["x0"], r["x1"], (r["top"] + r["bottom"]) / 2)) for r in page.rects + page.lines
                     if (r["bottom"] - r["top"]) < 2 and (r["x1"] - r["x0"]) > 2]
            marks.sort(key=lambda m: m[2])
            mys = [m[2] for m in marks]
            chars = sorted(page.chars, key=lambda c: (c["bottom"], c["x0"]))
            rows: list[list[dict]] = []
            base = None
            for c in chars:
                if base is None or c["bottom"] - base > 2.5:
                    rows.append([])
                    base = c["bottom"]
                rows[-1].append(c)
            for row in rows:
                row.sort(key=lambda c: c["x0"])
                # bill line number: digits in the left margin
                k, num = 0, ""
                while k < len(row) and row[k]["x1"] <= 68 and (row[k]["text"].isdigit() or row[k]["text"].isspace()):
                    num += row[k]["text"].strip()
                    k += 1
                if not num or k >= len(row) or not num.isdigit():
                    continue
                body = row[k:]
                top = min(c["top"] for c in body)
                bot = max(c["bottom"] for c in body)
                lo, hi = bisect.bisect_left(mys, top - 1), bisect.bisect_right(mys, bot + 4)
                near = marks[lo:hi]
                cells: list[tuple[str, int]] = []
                prev = None
                for c in body:
                    t = QUOTES.get(c["text"], c["text"])
                    cx, h = (c["x0"] + c["x1"]) / 2, (c["bottom"] - c["top"]) or 1
                    m = N
                    for x0, x1, y in near:
                        if x0 - 0.5 <= cx <= x1 + 0.5:
                            if c["top"] + 0.3 * h <= y <= c["bottom"] - 0.12 * h:
                                m = S
                                break
                            if c["bottom"] - 0.12 * h < y <= c["bottom"] + 3.5:
                                m = U
                    if prev is not None and c["x0"] - prev[0]["x1"] > 1.2 and not t.isspace() and not prev[0]["text"].isspace():
                        cells.append((" ", prev[1] if prev[1] == m else N))
                    cells.append((t, m))
                    prev = (c, m)
                st.add_line(int(num), pno, cells)
    return st


# ============================================================================ act structure
def norm_ws(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return s.strip()


def flat(s: str) -> str:
    s = re.sub(r"-\n(?=[a-z])", "-", s)          # "state-\nwide" -> "state-wide"
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str) -> str | None:
    m = DATE_RX.search(s or "")
    if not m:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def _blocks(raw: str, start: int, end: int) -> list[tuple[int, int]]:
    """Quoted passages [open+1, close) in raw[start:end]: a double quote opened after "follows:" (or "to read:")
    and closed by the next double quote (Georgia acts use single quotes inside quoted Code text)."""
    out, i = [], start
    opener = re.compile(r"(?:follows|to read|the following)\s*:\s*\"")
    while True:
        m = opener.search(raw, i, end)
        if not m:
            return out
        a = m.end()
        b = raw.find('"', a, end)
        if b < 0:
            b = end
        # consecutive passages for one instruction: "(d) ..." "(f) ..."
        while b < end:
            nx = re.match(r'\s*"', raw[b + 1:b + 40])
            if not nx:
                break
            c = raw.find('"', b + 1 + nx.end(), end)
            if c < 0:
                break
            b = c
        out.append((a, b))
        i = b + 1


def split_sections(st: Stream) -> list[dict]:
    raw = st.raw
    m = re.search(r"BE\s+IT\s+ENACTED\s+BY\s+THE\s+GENERAL\s+ASSEMBLY\s+OF\s+GEORGIA\s*:", raw)
    start = m.end() if m else 0
    blocks = _blocks(raw, start, len(raw))
    inside = lambda off: any(a <= off < b for a, b in blocks)  # noqa: E731
    heads = []
    pos = start
    for line in raw[start:].split("\n"):
        mm = SECTION_HDR.match(line)
        if mm and not inside(pos):
            heads.append((pos, pos + len(line), mm.group(1)))
        pos += len(line) + 1
    secs = []
    for i, (a, b, n) in enumerate(heads):
        e = heads[i + 1][0] if i + 1 < len(heads) else len(raw)
        secs.append({"n": n, "a": b + 1, "b": e, "blocks": [(x, y) for x, y in blocks if b < x < e]})
    return secs


def _is_para_start(line: str, prev: str) -> bool:
    s = line.lstrip('"').lstrip()
    if CITE_LINE.match(s) and len(s) < 40:
        return True
    if STRUCT_LINE.match(s):
        return True
    m = LEAD_RX.match(s)
    if not m:
        return False
    rest = s[m.end():].lstrip()
    if not rest or rest[0].isupper() or rest[0] in "'\"(" or rest[0].isdigit() or rest.startswith("$"):
        return True
    p = prev.rstrip()
    return bool(re.search(r"(?:[.:;]|;\s*(?:and|or))$", p)) or not p


def block_paragraphs(st: Stream, a: int, b: int) -> list[dict]:
    """Paragraphs of a quoted passage: k ('p' text | 'cite' section number | 'struct' article/part line | 'head' its
    heading), a (enacted text), b (amended text, only when marks make it differ), m (has marks)."""
    raw = st.raw
    lines, pos = [], a
    for ln in raw[a:b].split("\n"):
        lines.append((pos, pos + len(ln)))
        pos += len(ln) + 1
    paras: list[list[tuple[int, int]]] = []
    kinds: list[str] = []
    prev = ""
    after_struct = False
    for x, y in lines:
        line = raw[x:y]
        if not line.strip():
            continue
        s = line.strip().lstrip('"')
        if STRUCT_LINE.match(s):
            paras.append([(x, y)]); kinds.append("struct"); after_struct = True
        elif CITE_LINE.match(s) and len(s) < 40:
            cm = CITE_LINE.match(s)
            paras.append([(x, y)]); kinds.append("cite"); after_struct = False
            if cm.group(2).strip() and not re.match(r"^Reserved\.?$", cm.group(2).strip(), re.I):
                kinds[-1] = "cite_text"      # "21-2-1. Text..." on one line
        elif after_struct and not LEAD_RX.match(s):
            if kinds[-1] == "head":
                paras[-1].append((x, y))
            else:
                paras.append([(x, y)]); kinds.append("head")
        elif not paras or _is_para_start(line, prev) or kinds[-1] in ("cite", "struct", "head"):
            after_struct = False
            paras.append([(x, y)]); kinds.append("p")
        else:
            paras[-1].append((x, y))
        prev = line
    out = []
    for segs, k in zip(paras, kinds):
        x, y = segs[0][0], segs[-1][1]
        aft = re.sub(r"\s+([,.;:])", r"\1", flat(st.text(x, y, S))).strip('"').strip()
        bef = re.sub(r"\s+([,.;:])", r"\1", flat(st.text(x, y, U))).strip('"').strip()
        mk = st.marked(x, y)
        if k == "cite_text":
            cm = CITE_LINE.match(aft)
            out.append({"k": "cite", "a": cm.group(1) + "."} if cm else {"k": "p", "a": aft})
            rest = (cm.group(2) if cm else "").strip()
            if rest:
                d = {"k": "p", "a": rest}
                bm = CITE_LINE.match(bef)
                if mk and bm and bm.group(2).strip() != rest:
                    d["b"] = bm.group(2).strip()
                out.append(d)
            continue
        d = {"k": k, "a": aft}
        if mk:
            d["m"] = 1
            if bef != aft:
                d["b"] = bef
        out.append(d)
    return out


# ---------------------------------------------------------------------------- instructions
def _label_groups(s: str) -> list[list[str]]:
    """'(a) and (c)' -> [['a'], ['c']]; '(a)(1)' -> [['a','1']]; '(1) through (4)' -> expanded when numeric."""
    groups, out = re.findall(r"((?:\([^()\s]{1,8}\))+)", s), []
    for g in groups:
        out.append(re.findall(r"\(([^()\s]{1,8})\)", g))
    if re.search(r"\)\s*through\s*\(", s) and len(out) == 2 and len(out[0]) == len(out[1]) == 1:
        x, y = out[0][0], out[1][0]
        if x.isdigit() and y.isdigit():
            out = [[str(i)] for i in range(int(x), int(y) + 1)]
        elif len(x) == len(y) == 1 and x.isalpha() and y.isalpha():
            out = [[chr(i)] for i in range(ord(x), ord(y) + 1)]
    return out


UNIT_RX = re.compile(r"\b(?:the\s+(?:introductory\s+(?:language|text)\s+of\s+)?)?(?:a\s+)?(?:new\s+)?"
                     r"(subsections?|paragraphs?|subparagraphs?|divisions?|subdivisions?)\b"
                     r"((?:(?:\s*(?:,|and|or|through))*\s*(?:\([^()\s]{1,8}\))+)*)")
OF_RX = re.compile(r"^\s*of\s+(subsection|paragraph|subparagraph|division|subdivision)\s+((?:\([^()\s]{1,8}\))+)")


def parse_targets(clause: str) -> list[dict]:
    """Unit targets of one instruction clause: [{noun, labels:[[...]], parent:[...], new, intro}]."""
    out = []
    consumed = 0
    for m in UNIT_RX.finditer(clause):
        if m.start() < consumed:
            continue                     # part of the previous target's "of subsection (a)" chain
        noun = NOUNS[m.group(1).lower()]
        groups = _label_groups(m.group(2) or "")
        new = bool(re.search(r"\bnew\s+$", clause[max(0, m.start() - 8):m.start() + 4])) or "new " in m.group(0)
        intro = "introductory" in m.group(0)
        tail = clause[m.end():]
        parent: list[str] = []
        chain = []
        while True:
            om = OF_RX.match(tail)
            if not om:
                break
            chain.append(re.findall(r"\(([^()\s]{1,8})\)", om.group(2)))
            tail = tail[om.end():]
        for g in reversed(chain):
            parent += g
        consumed = len(clause) - len(tail)
        # "paragraph (a)(11)": a compound group is a full path; its last label is the unit
        labels = []
        for g in groups:
            if len(g) > 1:
                labels.append(g)
            else:
                labels.append(parent + g)
        if not groups and new:
            # "a new paragraph to subsection (b)" / "to paragraph (2) of subsection (a)"
            tm = re.match(r"^\s*(?:to|in|of|into)\s+(subsection|paragraph|subparagraph|division|subdivision)\s+((?:\([^()\s]{1,8}\))+)"
                          r"((?:\s+of\s+(?:subsection|paragraph|subparagraph)\s+(?:\([^()\s]{1,8}\))+)*)", clause[m.end():])
            if tm:
                p = re.findall(r"\(([^()\s]{1,8})\)", tm.group(2))
                rest = [re.findall(r"\(([^()\s]{1,8})\)", g) for g in re.findall(r"of\s+\w+\s+((?:\([^()\s]{1,8}\))+)", tm.group(3) or "")]
                for g in reversed(rest):
                    parent += g
                parent = parent + p if len(p) == 1 else p
        out.append({"noun": noun, "labels": labels, "parent": parent, "new": new or None, "intro": intro or None})
    return out


VERB_RX = re.compile(r"\b(?:by|and)\s+(revising|adding|repealing\s+and\s+reserving|repealing|striking|deleting|replacing|inserting|"
                     r"designating|redesignating|enacting|amending|renumbering)\b", re.I)


def parse_instruction(text: str, ctx: dict) -> dict:
    """One instruction (the words before a quoted passage, or a whole section without one)."""
    t = flat(text)
    ins: dict = {"text": t}
    # the act-level target: a Code section, said Code section, an article/part/chapter, or an uncodified Act
    m = re.search(r"\b(?:Code\s+Sections?)\s+(" + CITE + r")", t)
    said = re.search(r"\bsaid\s+Code\s+section\b", t, re.I)
    new_sec = re.search(r"\b(?:adding|enacting)\s+(?:a\s+)?new\s+Code\s+sections?\b", t, re.I)
    if m:
        ins["cite"] = m.group(1)
    elif said and ctx.get("cite"):
        ins["cite"] = ctx["cite"]
    if new_sec:
        ins["new_section"] = True
        # "by repealing Code Section X ... and enacting a new Code section" keeps X; a placement ("a new Code section
        # immediately following Code Section X") or a mention in the caption does not make X the target
        if m and not re.search(r"(?:revising|repealing(?:\s+and\s+reserving)?)\s+Code\s+Section\s+" + CITE, t):
            ins.pop("cite", None)
    big = re.search(r"\bby\s+(?:revising|adding\s+a\s+new|repealing(?:\s+and\s+reserving)?)\s+(Article|Part|Subpart|Chapter)s?\s*"
                    r"(\d+[A-Z]?(?:\.\d+)?)?", t, re.I)
    if big and not ins.get("cite"):
        ins["scope"] = {"kind": BIG[big.group(1).lower()], "n": big.group(2)}
        loc = re.search(r"(?:of|in)\s+(Chapter|Article|Part)\s+(\d+[A-Z]?(?:\.\d+)?)\s+(?:of\s+(Chapter|Title)\s+(\d+[A-Z]?))?", t[big.end():])
        if loc:
            ins["scope"]["of"] = [loc.group(1).lower(), loc.group(2)] + ([loc.group(3).lower(), loc.group(4)] if loc.group(3) else [])
    tm = re.search(r"Title\s+(\d+[A-Z]?)\s+of\s+the\s+Official\s+Code", t)
    if tm:
        ins["title"] = tm.group(1)
    cm = re.search(r"Chapter\s+(\d+[A-Z]?)\s+of\s+Title\s+(\d+[A-Z]?)", t)
    if cm:
        ins["chapter"] = [cm.group(2), cm.group(1)]
    em = re.search(r"\bas\s+effective\s+(?:on|until)\s+(" + DATE_RX.pattern + ")", t)
    if em:
        ins["as_effective"] = parse_date(em.group(1))
        if re.search(r"as\s+effective\s+until", t):
            ins["as_effective_until"] = True
    # an uncodified Act (another act's own sections, such as its effective-date section)
    um = re.search(r"\bAn\s+Act\b[^()]*?\(Ga\.\s*L\.\s*(\d{4}),\s*p\.\s*(\d+)[^)]*\)(?:,\s*([HS][BR]\s*\d+\w*))?", t)
    if um and not ins.get("cite") and "Official Code" not in t[:um.start() + 1]:
        ap = re.search(r"approved\s+(" + DATE_RX.pattern + ")", t[um.start():um.end()])
        ins["uncodified"] = {"ga_l": f"{um.group(1)}, p. {um.group(2)}", "year": int(um.group(1)), "bill": re.sub(r"\s+", " ", um.group(3) or ""),
                             "approved": parse_date(ap.group(1)) if ap else None}
        sm = re.search(r"\bSection\s+(\d+[A-Za-z]?(?:\.\d+)?)\b(?!\s+of\s+Title)", t[um.end():] or t)
        if sm:
            ins["uncodified"]["section"] = sm.group(1)
    verbs = [v.lower().replace("  ", " ") for v in re.findall(VERB_RX, t)]
    ins["verbs"] = [re.sub(r"\s+", " ", v) for v in verbs]
    ins["targets"] = parse_targets(t)
    return ins


# ---------------------------------------------------------------------------- Code Revision word edits
Q = r'"([^"]*)"'
PUNCT = {"semicolon": ";", "period": ".", "comma": ",", "colon": ":"}
WORD_OPS = [
    ("replace", re.compile(r"\breplacing\s+" + Q + r"\s+with\s+" + Q)),
    ("replace_rev", re.compile(r"\bsubstituting\s+" + Q + r"\s+for\s+" + Q)),
    ("end_punct", re.compile(r"\breplacing\s+the\s+(semicolon|period|comma|colon)(?:\s+at\s+the\s+end)?\s+with\s+(?:a\s+)?(?:(semicolon|period|comma|colon)\b|" + Q + ")")),
    ("end_punct_rev", re.compile(r"\bsubstituting\s+(?:a\s+)?(semicolon|period|comma|colon)\s+for\s+the\s+(semicolon|period|comma|colon)")),
    ("delete_comma", re.compile(r"\bdeleting\s+the\s+comma\s+(following|preceding|after|before)\s+" + Q)),
    ("insert_comma", re.compile(r"\binserting\s+a\s+comma\s+(following|preceding|after|before)\s+" + Q)),
    ("insert", re.compile(r"\binserting\s+" + Q + r"\s+(following|preceding|after|before)\s+" + Q)),
    ("delete", re.compile(r"\bdeleting\s+" + Q + r"(?:\s+(following|preceding|after|before)\s+" + Q + r")?")),
    ("reserve_section", re.compile(r"\brepealing\s+and\s+reserving\s+said\s+Code\s+section")),
    ("repeal_section", re.compile(r"\brepealing\s+said\s+Code\s+section(?:\s+in\s+its\s+entirety)?")),
    ("repeal_unit", re.compile(r"\brepealing\s+(?:and\s+reserving\s+)?(subsections?|paragraphs?|subparagraphs?|divisions?|subdivisions?)\s*((?:(?:\s*(?:,|and|or|through))*\s*(?:\([^()\s]{1,8}\))+)+)")),
]
UNSUPPORTED = re.compile(r"\b(?:quotation\s+marks?|redesignat\w+|designation|renumber\w+|transpos\w+|moving|striking\s+the\s+subsection)\b", re.I)
UNIT_PH = re.compile(r"\b(?:in|at\s+the\s+end\s+of|in\s+the\s+undesignated\s+text\s+(?:at\s+the\s+end\s+)?of|in\s+the\s+introductory\s+(?:language|text)\s+of)\s+"
                     r"(?:the\s+)?(subsections?|paragraphs?|subparagraphs?|divisions?|subdivisions?)\s*"
                     r"((?:(?:\s*(?:,|and|or|through))*\s*(?:\([^()\s]{1,8}\))+)+)|\bin\s+said\s+Code\s+section\b")
UNIT_MORE = re.compile(r"\s*,?\s*(?:and\s+)?(?:in\s+)?(?:the\s+introductory\s+(?:language|text)\s+of\s+)?(?:subsections?|paragraphs?|subparagraphs?|divisions?|subdivisions?)\s*"
                       r"((?:(?:\s*(?:,|and|or|through))*\s*(?:\([^()\s]{1,8}\))+)+)")
ALL_RX = re.compile(r"^[^.;]{0,40}?\b(?:both\s+times|each\s+time|every\s+time|all\s+times|wherever|each\s+place|everywhere|all\s+places)", re.I)


def parse_word_items(text: str) -> list[dict]:
    """'... (1) Code Section 9-9-28, ..., in subparagraph (c)(1)(A), by replacing "e-mail" with "email".'
    -> [{cite, as_effective, edits:[{units:[[...]], op, ...}], understood}]. Each edit's unit is the unit phrase right
    before it ("in paragraph (a)(3), by replacing"), else one right after it ("by deleting the comma following "x" in
    subsection (b)"), else the previous edit's."""
    t = flat(text)
    items = re.split(r"(?:^|(?<=[:.;\x00\"]))\s*\(\d+\)\s+(?=Code\s+Section\s+" + CITE + ")", t)
    out = []
    for it in items:
        cm = re.match(r"\s*Code\s+Section\s+(" + CITE + r")", it)
        if not cm:
            continue
        rec = {"cite": cm.group(1), "text": it.strip()[:600], "edits": []}
        em = re.search(r"\bas\s+effective\s+(?:on|until)\s+(" + DATE_RX.pattern + ")", it[:300])
        if em:
            rec["as_effective"] = parse_date(em.group(1))
        body = it[cm.end():]
        start = 0
        rel = re.search(r",\s*relating\s+to\b", body)
        if rel:
            e = re.search(r",\s*(?:(?:in|at\s+the\s+end\s+of)\s+(?:the\s+)?(?:undesignated|introductory|subsections?|paragraphs?|subparagraphs?|divisions?|subdivisions?|said)|by\s+(?:replacing|deleting|inserting|striking|revising|repealing|substituting))", body[rel.end():])
            start = rel.end() + (e.start() if e else 0)
        units = []
        for m in UNIT_PH.finditer(body, start):
            if not m.group(1):
                units.append((m.start(), m.end(), [[]]))
                continue
            intro = "introductory" in m.group(0)
            groups = [g + ["#intro"] if intro else g for g in _label_groups(m.group(2))]
            end = m.end()
            # "in the introductory language of subsection (b), paragraph (b)(3), and subsection (e)"
            while True:
                more = UNIT_MORE.match(body, end)
                if not more:
                    break
                gi = "introductory" in more.group(0)
                groups += [g + ["#intro"] if gi else g for g in _label_groups(more.group(1))]
                end = more.end()
            units.append((m.start(), end, groups))
        ops = []
        for op, rx in WORD_OPS:
            for mm in rx.finditer(body, start):
                ops.append((mm.start(), mm.end(), op, mm))
        ops.sort(key=lambda x: x[0])
        # drop matches nested in an earlier match ('deleting "x"' inside a longer phrase)
        kept, end = [], -1
        for o in ops:
            if o[0] >= end:
                kept.append(o)
                end = o[1]
        prev_units, prev_end = [[]], start
        for i, (a, b, op, mm) in enumerate(kept):
            before = [u for u in units if prev_end <= u[0] and u[1] <= a]
            nxt_a = kept[i + 1][0] if i + 1 < len(kept) else len(body)
            after = [u for u in units if b <= u[0] < nxt_a and re.match(r"^[\s,]*$", body[b:u[0]] + " ") and u[0] - b < 6]
            if before:
                cur = before[-1][2]
            elif after:
                cur = after[0][2]
            else:
                cur = prev_units
            g = mm.groups()
            e = {"units": cur, "op": op, "all": bool(ALL_RX.search(body[b:b + 80]))}
            if op == "replace":
                e.update(find=g[0], repl=g[1])
            elif op == "replace_rev":
                e.update(op="replace", find=g[1], repl=g[0])
            elif op == "end_punct":
                e.update(find=PUNCT[g[0]], repl=PUNCT.get(g[1] or "", g[2] if g[2] is not None else ""))
            elif op == "end_punct_rev":
                e.update(op="end_punct", find=PUNCT[g[1]], repl=PUNCT[g[0]])
            elif op in ("delete_comma", "insert_comma"):
                e.update(where=g[0], anchor=g[1])
            elif op == "insert":
                e.update(text=g[0], where=g[1], anchor=g[2])
            elif op == "delete":
                e.update(find=g[0], where=g[1], anchor=g[2])
            elif op == "repeal_unit":
                e.update(units=_label_groups(g[1]), reserve="reserving" in mm.group(0))
            rec["edits"].append(e)
            if op in ("delete_comma", "insert_comma", "insert"):
                # 'inserting a comma following "shall not" and "such summons"'
                k = b
                while True:
                    more = re.match(r'\s*(?:,\s*)?(?:and\s+)?"([^"]*)"', body[k:k + 200])
                    if not more or (i + 1 < len(kept) and k + more.end() > kept[i + 1][0]):
                        break
                    rec["edits"].append(dict(e, anchor=more.group(1)))
                    k += more.end()
            prev_units, prev_end = cur, b
        rest = re.sub(r"\s+", " ", body[start:])
        rec["understood"] = bool(rec["edits"]) and not UNSUPPORTED.search(rest)
        out.append(rec)
    return out


# ---------------------------------------------------------------------------- effective dates
def parse_effective(text: str) -> dict:
    """{default: {on|approval|contingent}, sections: {n: {...}}} from an act's effective-date section text."""
    t = flat(text)
    out: dict = {"text": t[:1200]}
    clauses = re.split(r"(?:^|(?<=[.:]))\s*\([a-z]\)\s+", t) if re.search(r"(?:^|[.:])\s*\(b\)\s", t) else [t]
    for cl in clauses:
        if "effective" not in cl:
            continue
        when: dict = {}
        d = parse_date(cl.split("effective", 1)[1])
        if re.search(r"effective\s+upon\s+(?:its\s+)?approval", cl):
            when["approval"] = True
        elif d:
            when["on"] = d
        if re.search(r"ratif|contingent|only\s+if|provided\s+that|if\s+an\s+amendment", cl, re.I):
            when["contingent"] = cl[:400]
        if not when:
            continue
        who = cl.split("shall become effective")[0]
        nums = []
        sm = re.search(r"\bSections?\s+((?:\d+[A-Za-z]?(?:[-.]\d+)*[A-Za-z]?(?:,\s*|\s*,?\s*and\s+|\s+through\s+)?)+)\s+of\s+this\s+Act", who)
        if sm:
            for x in re.split(r",\s*|\s*,?\s*and\s+", sm.group(1)):
                x = x.strip()
                rg = re.match(r"(\d+)\s+through\s+(\d+)$", x)
                if rg:
                    nums += [str(i) for i in range(int(rg.group(1)), int(rg.group(2)) + 1)]
                elif x:
                    nums.append(x)
        if not nums and re.search(r"\bThis\s+Act\b|\bthis\s+Act\s*$|remaining|all\s+other|Except\s+as", who):
            out.setdefault("default", when)
        for n in nums:
            out.setdefault("sections", {})[n] = when
    return out


ITEM_SPLIT = re.compile(r"(?:^|(?<=[:.;\x00\"]))\s*\(\d+\)\s+(?=(?:Code\s+Sections?\s+" + CITE + r"|Article\s+\d|Chapter\s+\d|Part\s+\d|Subpart\s+\d))")
HAS_WORD_OP = re.compile(r"\bby\s+(?:replacing|deleting|inserting|substituting)\b|\brepealing\s+(?:and\s+reserving\s+)?(?:said\s+Code\s+section|subsections?|paragraphs?|subparagraphs?|divisions?|subdivisions?)\s*(?:\(|in\b|$)")


def parse_act(st: Stream) -> dict:
    """The act's sections, each a list of clauses: {ins, paras, lines} for a quoted passage (or a repeal), or
    {words: [...]} for Code Revision word edits. Enumerated items ('(1) Code Section ...') are read one by one, each
    with its own target, whether or not it quotes text."""
    raw = st.raw
    secs = split_sections(st)
    out = {"sections": [], "effective": {}}
    actx: dict = {}                      # "said Code section" can point back to an earlier act section
    for sec in secs:
        body_a, body_b = sec["a"], sec["b"]
        parts, pos = [], body_a
        for k, (x, y) in enumerate(sec["blocks"]):
            parts.append(raw[pos:x - 1])
            parts.append(f" \x00{k}\x00 ")
            pos = y + 1
        parts.append(raw[pos:body_b])
        s = flat("".join(parts))
        clauses: list[dict] = []
        sec_lines = [st.line_at(body_a), st.line_at(max(body_a, body_b - 2))]
        if not sec["blocks"] and "shall become effective" in s and not re.search(r"\b(?:is|are)\s+(?:further\s+)?amended\b", s):
            out["effective"] = parse_effective(s)
            out["sections"].append({"n": sec["n"], "lines": sec_lines, "clauses": [], "effective_section": True})
            continue
        if not sec["blocks"] and re.search(r"All\s+laws\s+and\s+parts\s+of\s+laws\s+in\s+conflict", s) and len(s) < 300:
            out["sections"].append({"n": sec["n"], "lines": sec_lines, "clauses": []})
            continue
        pieces = ITEM_SPLIT.split(s)
        ctx: dict = dict(actx)
        for pi, piece in enumerate(pieces):
            if pi == 0 and len(pieces) > 1 and "\x00" not in piece:
                # the preamble ("Title 21 ... is amended in:") names the title, nothing to apply
                continue
            ctx = {} if len(pieces) > 1 else ctx
            segs = re.split(r"\x00(\d+)\x00", piece)
            texts, keys = segs[0::2], [int(k) for k in segs[1::2]]
            for i, k in enumerate(keys):
                ins_text = texts[i]
                x, y = sec["blocks"][k]
                if i == 0 and HAS_WORD_OP.search(ins_text.split(" as follows")[0]) and re.search(r"Code\s+Section\s+" + CITE, ins_text):
                    w = [it for it in parse_word_items(ins_text) if it["edits"]]
                    for it in w:   # word edits before the revised passage in the same item
                        it["edits"] = [e for e in it["edits"] if e["op"] != "repeal_section"]
                    if any(it["edits"] for it in w):
                        clauses.append({"words": w, "lines": [st.line_at(x), st.line_at(x)], "page": st.page_at(x)})
                ins = parse_instruction(ins_text, ctx)
                if not ins.get("cite") and not ins.get("new_section") and not ins.get("scope") and ctx.get("cite"):
                    ins["cite"] = ctx["cite"]
                if ins.get("cite"):
                    ctx["cite"] = ins["cite"]
                paras = block_paragraphs(st, x, y)
                cites = [p["a"].rstrip(".") for p in paras if p["k"] == "cite"]
                if cites:
                    ctx["cite"] = cites[-1]
                cl = {"ins": ins, "lines": [st.line_at(x), st.line_at(max(x, y - 1))], "page": st.page_at(x), "paras": paras}
                u = ins.get("uncodified")
                if u:
                    txt = " ".join(p["a"] for p in paras if p["k"] == "p")
                    if "effective" in txt:
                        u["effective"] = parse_effective(txt)
                clauses.append(cl)
            tail = texts[-1]
            if not tail.strip():
                continue
            here = sec_lines
            if HAS_WORD_OP.search(tail) and re.search(r"Code\s+Section\s+" + CITE, piece):
                src_text = tail if keys else piece
                if keys and not re.match(r"\s*Code\s+Section", src_text):
                    src_text = "Code Section " + (ctx.get("cite") or "") + ", " + src_text if ctx.get("cite") else src_text
                w = parse_word_items(src_text if re.match(r"\s*(?:\(\d+\)\s+)?Code\s+Section", src_text) else piece)
                if w:
                    clauses.append({"words": w, "lines": here})
                    continue
            if re.search(r"\brepeal", tail, re.I) and (re.search(r"Code\s+Section|said\s+Code\s+section|Article|Part|Chapter", tail) or ctx.get("cite")):
                ins = parse_instruction(tail, ctx)
                if not ins.get("cite") and ctx.get("cite") and not ins.get("scope"):
                    ins["cite"] = ctx["cite"]
                clauses.append({"ins": ins, "lines": here})
        out["sections"].append({"n": sec["n"], "lines": sec_lines, "clauses": clauses})
        if ctx.get("cite"):
            actx = {"cite": ctx["cite"]}
    return out


# ============================================================================ the crawl
def pick_version(versions: list[dict]) -> dict | None:
    """The as-passed version ('/AP'), else the enrolled ('/ENR'), else the current or newest one."""
    if not versions:
        return None
    for rx in (r"/AP\b", r"\bENR\b|/ENR", r"AS\s+PASSED"):
        hit = [v for v in versions if re.search(rx, v.get("name") or "", re.I)]
        if hit:
            return max(hit, key=lambda v: v.get("versionNumber") or 0)
    cur = [v for v in versions if v.get("isCurrent")]
    return cur[0] if cur else max(versions, key=lambda v: v.get("versionNumber") or 0)


def approval_date(det: dict, fallback: str | None) -> str | None:
    for h in det.get("statusHistory") or []:
        name = (h.get("name") or "").lower()
        if "signed by governor" in name or "effective date" in name or "became law without" in name:
            return (h.get("date") or "")[:10] or fallback
    return fallback


def load_config(root) -> dict:
    from .sources.legis import load_config as lc

    cfg = lc(root)
    return cfg.get("code_acts") or {}


def run(fetcher, store: Store, root, deadline: float | None = None, log=print, limit: int | None = None,
        only: list[int] | None = None) -> dict:
    """Read and parse the as-passed text of enacted acts not parsed yet (or parsed by an older parser)."""
    from .sources.legis import Legis, TokenRefused

    cc = load_config(root)
    sessions = {int(s) for s in (cc.get("sessions") or [1031, 1032, 1033, 1034])}
    api = Legis(fetcher, log)
    out = {"acts": 0, "parsed": 0, "skipped": 0, "errors": 0, "clauses": 0, "pending": 0, "listed": 0}
    # the bill list comes from `gacrawl legis`; list a session here when that has not happened in the last week
    week = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    names = {1031: "2023-2024 Regular Session", 1032: "2023 Special Session", 1033: "2025-2026 Regular Session", 1034: "2026 Special Session"}
    for sid in sorted(sessions):
        if only or (store.kv_get(f"legis:legislation:{sid}") or "") >= week:
            continue
        try:
            for x in api.legislation(sid):
                if store.add(api.bill_record(x, names.get(sid, str(sid)))):
                    out["listed"] += 1
            store.kv_set(f"legis:legislation:{sid}", dt.date.today().isoformat())
            store.flush()
        except (Blocked, Disallowed):
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"  codeacts: listing session {sid}: {type(exc).__name__}: {exc}"[:200])
    todo = []
    for b in store.iter_query("source_id=? AND kind='bill'", (SOURCE,)):
        m = b["meta"]
        if not m.get("enacted") or int(m.get("session") or 0) not in sessions:
            continue
        if only and m["legislation_id"] not in only:
            continue
        todo.append(m)
    out["acts"] = len(todo)
    todo.sort(key=lambda m: (m.get("status_date") or ""), reverse=True)    # newest first
    for i, m in enumerate(todo):
        lid = m["legislation_id"]
        key = f"{SOURCE}|code_act|{lid}"
        have = store.kv_get(f"codeacts:{lid}")
        if have and json.loads(have).get("v") == PARSER_VERSION:
            out["skipped"] += 1
            continue
        if (deadline is not None and time.monotonic() > deadline) or (limit is not None and out["parsed"] >= limit):
            out["pending"] += 1
            continue
        try:
            det = api.call(f"legislation/detail/{lid}")
            if not det:
                raise RuntimeError("no detail")
            ver = pick_version(det.get("versions") or [])
            lib = (det.get("session") or {}).get("library")
            if not ver or not lib:
                raise RuntimeError("no version or library")
            url = f"{API}legislation/document/{lib}/{ver['id']}"
            r = fetcher.get(url, use_cache=False)
            if r.status != 200 or not r.content.startswith(b"%PDF"):
                raise RuntimeError(f"document HTTP {r.status}")
            st = read_pdf(r.content)
            act = parse_act(st)
        except (Blocked, Disallowed):
            raise
        except OutOfTime:
            break                      # the budget is spent (or the host's next turn is past it): the rest waits for the next run
        except TokenRefused as exc:
            log(f"  codeacts: {exc} after retries; the {len(todo) - i} acts left wait for the next run")
            out["token_refused"] = True
            break
        except Exception as exc:  # noqa: BLE001
            out["errors"] += 1
            log(f"  codeacts {m.get('number')} ({lid}): {type(exc).__name__}: {exc}"[:220])
            continue
        signed = approval_date(det, m.get("status_date"))
        n_cl = sum(len(s["clauses"]) for s in act["sections"])
        meta = {"legislation_id": lid, "bill": m.get("number"), "session": m.get("session"), "session_name": m.get("session_name"),
                "act": m.get("act"), "caption": m.get("caption"), "signed": signed, "status": m.get("status"),
                "version": {"id": ver["id"], "name": ver.get("name"), "number": ver.get("versionNumber")},
                "document_url": url, "page_url": f"{SITE}/legislation/{lid}", "parser": PARSER_VERSION,
                "effective": act["effective"], "sections": act["sections"], "opened": True, "opened_url": url}
        # the quoted text lives in meta; the searchable text is the caption and the Code sections the act changes
        cites = sorted({c["ins"]["cite"] for s in act["sections"] for c in s["clauses"] if (c.get("ins") or {}).get("cite")} |
                       {it["cite"] for s in act["sections"] for c in s["clauses"] for it in c.get("words") or []})
        text = f"{m.get('number')} ({m.get('session_name')}), Act {m.get('act')}: {m.get('caption') or ''}\nCode sections: " + ", ".join(cites)
        store.add(Record(SOURCE, "code_act", meta["page_url"], title=f"{m.get('number')} ({m.get('session_name')}), Act {m.get('act')}: {m.get('caption') or ''}"[:300],
                         text=text[:400000], published=signed, channel="api", jurisdiction="ga", evidence="primary", key=key, meta=meta))
        store.kv_set(f"codeacts:{lid}", json.dumps({"v": PARSER_VERSION, "version": ver["id"], "at": dt.date.today().isoformat()}))
        out["parsed"] += 1
        out["clauses"] += n_cl
        if out["parsed"] % 20 == 0:
            store.flush()
    store.flush()
    return out


def export_code_acts(store: Store, out_path: Path) -> int:
    acts = []
    for r in store.iter_query("source_id=? AND kind='code_act'", (SOURCE,)):
        m = dict(r["meta"])
        for k in ("opened", "opened_url", "parser"):
            m.pop(k, None)
        acts.append(m)
    acts.sort(key=lambda a: (a.get("signed") or "", a.get("bill") or ""))
    out_path.write_text(json.dumps({
        "note": "Every enacted 2023-2026 act's Code changes, parsed from the as-passed text on legis.ga.gov: per act section, "
                "the instruction (target Code section, unit, verb), the quoted Code text as enacted (a) and as it read before "
                "(b, when struck/underlined marks differ), Code Revision word edits, and the act's effective dates. Apply in "
                "effective-date order and check each 'b' against the text being amended before using 'a'.",
        "parser": PARSER_VERSION, "acts": acts}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(acts)


def export_legislation(store: Store, out_path: Path) -> int:
    """Every bill and resolution of the 2023-2026 sessions in Electo's acts.json item shape:
    [id, session, H|S, B|R, number, suffix, caption, status, status date (MM/DD/YYYY), act/veto, sponsors, enacted 0/1]."""
    sessions = {1031: "2023-2024 Regular Session", 1032: "2023 Special Session", 1033: "2025-2026 Regular Session", 1034: "2026 Special Session"}
    items = []
    for r in store.iter_query("source_id=? AND kind='bill'", (SOURCE,)):
        m = r["meta"]
        sid = int(m.get("session") or 0)
        if sid not in sessions:
            continue
        num = m.get("number") or ""
        mm = re.match(r"([HS])([BR])\s+(\d+)(\w*)", num)
        if not mm:
            continue
        st = (m.get("status") or "").strip()
        act = str(m.get("act") or "").strip()
        enacted = bool(re.search(r"signed by governor|effective without signature|became law|act \d", st, re.I)) or \
            (bool(act) and not re.search(r"veto", st, re.I))
        if mm.group(2) != "B":
            enacted = enacted and bool(re.search(r"signed by governor", st, re.I))
        sp = [s.get("name") or "" for s in sorted(m.get("sponsors") or [], key=lambda s: s.get("sequence") or 0)]
        sps = "; ".join(sp[:3]) + (" and %d more" % (len(sp) - 3) if len(sp) > 3 else "")
        d = m.get("status_date") or ""
        d = f"{d[5:7]}/{d[8:10]}/{d[:4]}" if re.match(r"\d{4}-\d\d-\d\d", d) else d
        items.append([m["legislation_id"], str(sid), mm.group(1), mm.group(2), int(mm.group(3)), mm.group(4), (m.get("caption") or "").strip(),
                      st, d, act, sps, 1 if enacted else 0])
    items.sort(key=lambda x: (x[1], x[2], x[3], x[4], x[5]))
    out_path.write_text(json.dumps({"note": "Every 2023-2026 bill and resolution as legis.ga.gov lists it (status, act number, "
                                            "sponsors), in Electo's acts.json item shape.",
                                    "sessions": {str(k): v for k, v in sessions.items()}, "items": items,
                                    "checked": max((r for r in [store.kv_get("legis:legislation:1033") or ""]), default="")},
                                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(items)
