"""Text as people read it: HTML entities decoded and mis-decoded UTF-8 put back, for every string Crawl hands on.

Two faults reach records from the sites themselves (Nov 14, 2026 audit):
  - HTML entities left in plain text: WordPress's API returns titles as HTML ("Shen&#233;e Johnson-Cummings",
    "Georgia&#39;s"), the Library of Congress's summaries are HTML ("penalties&#8212;a fine"), feeds carry "[&#8230;]".
    Electo escapes what it shows, so a reader saw "&#8212;" itself.
  - UTF-8 read as Windows-1252 ("mojibake"): "TundÃ©" for "Tundé", "McCookâ€™s" for "McCook’s".

fix_text drops invisible characters inside words (soft hyphens, zero-width spaces), decodes entities except
the two that would make markup (&lt; and &gt; stay as they are, so a page that
inserts text unescaped is no worse off than before), turns a decoded no-break space into a space, and repairs a
mojibake run only when its bytes form valid UTF-8 (accented names written correctly, "José", "São", are left alone).
It is idempotent: fix_text(fix_text(s)) == fix_text(s). Electo's ga/textfix.py is the same code.
"""
from __future__ import annotations

import html
import re

_ENT = re.compile(r"&(#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")

# Windows-1252's characters for the bytes 0x80-0x9F (the five it leaves undefined arrive as the C1 controls themselves)
_CP1252 = {}
for _b in range(0x80, 0xA0):
    try:
        _CP1252[bytes([_b]).decode("cp1252")] = _b
    except UnicodeDecodeError:
        _CP1252[chr(_b)] = _b
for _b in range(0xA0, 0x100):
    _CP1252[chr(_b)] = _b
_CONT = "".join(re.escape(c) for c, b in _CP1252.items() if 0x80 <= b <= 0xBF)
_MOJI = re.compile("[\u00c2-\u00f4](?:[" + _CONT + "]){1,3}")
# invisible characters a page puts inside words: a soft hyphen cut "Karen Mc\u00adNeal" to "Karen Mc" (Early County's
# board page, Nov 2026), and in a name they split the person key. Zero-width joiners stay (emoji, Indic scripts).
_INVISIBLE = re.compile("[\u00ad\u200b\u2060\ufeff]")
_HINT = re.compile("&|[\u00c2-\u00f4\u00ad\u200b\u2060\ufeff]")


def _need(lead: int) -> int:
    return 2 if lead <= 0xDF else 3 if lead <= 0xEF else 4


def _demoji(m: re.Match) -> str:
    run = m.group(0)
    bs = bytes(_CP1252.get(c, 0) for c in run)
    n = _need(bs[0])
    if len(bs) < n:
        return run
    try:
        ch = bs[:n].decode("utf-8")
    except UnicodeDecodeError:
        return run
    if ch == " ":
        ch = " "
    return ch + run[n:]


def _unent(m: re.Match) -> str:
    raw = m.group(0)
    ch = html.unescape(raw)
    if ch == raw:
        return raw                      # not an entity HTML knows
    if ch == "<":
        return "&lt;"
    if ch == ">":
        return "&gt;"
    if ch == " ":
        return " "
    if ch == "\ufffd" or len(ch) == 1 and (ord(ch) < 32 and ch not in "\t\n\r" or 0x7F <= ord(ch) < 0xA0):
        return ""                       # a control character (or an invalid code point) written as an entity
    return ch


def fix_text(s):
    """The string as it should read; anything that is not a string is returned as it is."""
    if not isinstance(s, str) or not s or not _HINT.search(s):
        return s
    out = _INVISIBLE.sub("", s)
    out = _MOJI.sub(_demoji, out) if _MOJI.search(out) else out
    for _ in range(3):                  # "&amp;#8217;" was escaped twice
        if "&" not in out:
            break
        new = _ENT.sub(_unent, out)
        if new == out:
            break
        out = new
    return out


def fix_tree(x):
    """fix_text applied to every string value in a JSON-like tree (keys are left as they are: other files refer to them)."""
    if isinstance(x, str):
        return fix_text(x)
    if isinstance(x, list):
        return [fix_tree(v) for v in x]
    if isinstance(x, dict):
        return {k: fix_tree(v) for k, v in x.items()}
    return x


def fix_inplace(x):
    """fix_text on every string value of a JSON-like tree, changed where it is (no copy: for the export's largest
    objects, the money graph's nodes and patterns, Nov 14, 2026). Returns x."""
    stack = [x]
    while stack:
        y = stack.pop()
        if isinstance(y, dict):
            for k, v in y.items():
                if isinstance(v, str):
                    if _HINT.search(v):
                        y[k] = fix_text(v)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(y, list):
            for i, v in enumerate(y):
                if isinstance(v, str):
                    if _HINT.search(v):
                        y[i] = fix_text(v)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
    return x


_ESCAPED_LEAD = re.compile(r"\\u00[c-fC-F][0-9a-fA-F]")
_ESCAPED_INVISIBLE = re.compile(r"\\u(?:00[aA][dD]|200[bB]|2060|[fF][eE][fF][fF])")


def needs_fix(raw: str) -> bool:
    """A quick look at serialized text (JSON, possibly with non-ASCII written as \\u escapes): could anything in it
    need fix_text? False means fix_tree would change nothing."""
    return bool(raw) and bool(_ENT.search(raw) or _MOJI.search(raw) or _ESCAPED_LEAD.search(raw) or _INVISIBLE.search(raw)
                              or _ESCAPED_INVISIBLE.search(raw))
