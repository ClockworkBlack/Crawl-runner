"""Turn HTML into clean, ingestion-ready Markdown, and find feeds/APIs/links in a page."""
from __future__ import annotations

import re
from urllib.parse import urljoin as _urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

def urljoin(base: str, href: str) -> str:
    """urllib's urljoin, but a malformed link ("http://[broken") gives "" instead of a ValueError that used to end
    a whole source's crawl (co-wilcox: "Invalid IPv6 URL")."""
    try:
        return _urljoin(base, href)
    except ValueError:
        return ""


NOISE = ["script", "style", "noscript", "nav", "footer", "header", "iframe", "svg", "button", "aside",
         "input", "select", "option", "textarea"]
# A <form> is unwrapped, not deleted: ASP.NET WebForms (Legistar's public pages, CivicPlus, DotNetNuke, SharePoint)
# wrap the whole page in one form, which used to extract to nothing. Only its controls (above) are dropped.
UNWRAP = ["form"]
ORDINANCE_HEADING = re.compile(
    r"^\s*(?:(?:Sec(?:tion)?\.?|§)\s*\d[\w\-.]*|ARTICLE\s+[IVXLC\d]+|CHAPTER\s+\d+[A-Z]?|DIVISION\s+\d+|ORDINANCE\s+(?:NO\.?|NUMBER)\s*[\w\-]+)",
    re.I,
)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def main_node(soup: BeautifulSoup) -> Tag:
    for sel in ["main", "article", "[role=main]", "#content", "#main", ".content", "#maincontent"]:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return node
    # largest text block, but only when it holds most of the page's prose (a small page is read whole)
    body = soup.body or soup
    total = len(body.get_text(" ", strip=True))
    best, best_len = body, 0
    for div in soup.find_all(["main", "article", "div", "section"]):
        n = len(div.get_text(" ", strip=True))
        links = sum(len(a.get_text(strip=True)) for a in div.find_all("a"))
        score = n - 2 * links
        if score > best_len:
            best, best_len = div, score
    if best is not body and len(best.get_text(" ", strip=True)) < 0.6 * total:
        return body
    return best


def _table_md(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).replace("|", "\\|") for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _inline(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name == "br":
        return "\n"
    inner = "".join(_inline(c) for c in node.children)
    if node.name in ("strong", "b") and inner.strip():
        return f"**{inner.strip()}**"
    if node.name in ("em", "i") and inner.strip():
        return f"*{inner.strip()}*"
    if node.name == "a" and node.get("href") and inner.strip():
        return f"[{inner.strip()}]({node['href']})"
    return inner


def html_to_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    """Returns (title, markdown)."""
    soup = soup_of(html)
    title = (soup.title.get_text(strip=True) if soup.title else "") or ""
    for tag in soup(NOISE):
        tag.decompose()
    for tag in soup(UNWRAP):
        tag.unwrap()
    if base_url:
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(base_url, a["href"])
    root = main_node(soup)
    blocks: list[str] = []

    def walk(node: Tag):
        for child in node.children:
            if isinstance(child, NavigableString):
                t = str(child).strip()
                if t:
                    blocks.append(t)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                txt = child.get_text(" ", strip=True)
                if txt:
                    blocks.append("#" * int(name[1]) + " " + txt)
            elif name == "table":
                md = _table_md(child)
                if md:
                    blocks.append(md)
            elif name in ("ul", "ol"):
                items = []
                for i, li in enumerate(child.find_all("li", recursive=False), 1):
                    txt = re.sub(r"\s+", " ", _inline(li)).strip()
                    if txt:
                        items.append(f"{i}. {txt}" if name == "ol" else f"- {txt}")
                if items:
                    blocks.append("\n".join(items))
            elif name in ("p", "blockquote", "pre"):
                txt = re.sub(r"[ \t]+", " ", _inline(child)).strip()
                if txt:
                    if ORDINANCE_HEADING.match(txt) and len(txt) < 160:
                        blocks.append("### " + txt)
                    else:
                        blocks.append(("> " + txt) if name == "blockquote" else txt)
            else:
                walk(child)

    walk(root)
    md = "\n\n".join(b for b in blocks if b)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return title, md


DATE_META = ("article:published_time", "og:published_time", "datePublished", "date", "dc.date", "dc.date.issued",
             "pubdate", "publish-date", "publication_date", "sailthru.date", "parsely-pub-date", "citation_date")
TEXT_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|"
                       r"Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(20\d\d|19\d\d)\b|\b(\d{1,2})/(\d{1,2})/(20\d\d)\b|"
                       r"\b(20\d\d)-(\d\d)-(\d\d)\b")
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


TEXT_DATE_MAX_AGE_YEARS = 15     # a date read from the page's words, not its metadata, is believed only this far back


def page_date(html: str, text: str = "", fetched: str | None = None) -> str | None:
    """When a page says it was published: meta tags, <time datetime>, JSON-LD, else the first date in its opening text.
    A date from the text is only the page's own when it is plausible: not more than TEXT_DATE_MAX_AGE_YEARS before the
    page was fetched (`fetched`, ISO; today when not given) and not more than a year after. The first date of a page's
    words is often one it mentions: profile items were dated 1956-01-12 and 2001-09-11 that way (Nov 2026)."""
    return page_date_info(html, text, fetched)["date"]


# where a record's date came from (meta.date_from): the page's own markup says when it was published ("meta", "time",
# "jsonld"); only the first date in its words ("text": the weakest, a date the page may only mention); the feed or API
# item ("feed"); the source's own data (no date_from: an agenda system's meeting date, a filing's date, a PDF's fields)
DATE_FROM = ("meta", "time", "jsonld", "text", "feed", "listing", "archive")


def page_date_info(html: str, text: str = "", fetched: str | None = None) -> dict:
    """{"date", "from", "set_aside"}: the date page_date gives, where it was read ("meta", "time", "jsonld", "text" or
    ""), and a date of the page's words that was not believed (too old or too far ahead of `fetched`)."""
    soup = soup_of(html[:300000])
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").lower()
        if key in {k.lower() for k in DATE_META}:
            v = (m.get("content") or "").strip()
            d = _iso10(v)
            if d:
                return {"date": d, "from": "meta", "set_aside": None}
    t = soup.find("time", attrs={"datetime": True})
    if t:
        d = _iso10(t["datetime"])
        if d:
            return {"date": d, "from": "time", "set_aside": None}
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        mm = re.search(r'"datePublished"\s*:\s*"([^"]+)"', sc.get_text() or "")
        if mm:
            d = _iso10(mm.group(1))
            if d:
                return {"date": d, "from": "jsonld", "set_aside": None}
    d = text_date(text or soup.get_text(" ", strip=True))
    if d:
        if _plausible_text_date(d, fetched):
            return {"date": d, "from": "text", "set_aside": None}
        return {"date": None, "from": "", "set_aside": d}
    return {"date": None, "from": "", "set_aside": None}


def text_date(text: str) -> str | None:
    """The first date in a page's opening words (what page_date falls back to), as ISO, or None."""
    mm = TEXT_DATE.search((text or "")[:1500])
    return _iso10(mm.group(0)) if mm else None


def _plausible_text_date(d: str, fetched: str | None = None) -> bool:
    import datetime as dt

    try:
        day = dt.date.fromisoformat(d[:10])
        ref = dt.date.fromisoformat(fetched[:10]) if fetched else dt.date.today()
    except ValueError:
        return False
    return ref.replace(year=ref.year - TEXT_DATE_MAX_AGE_YEARS, day=min(ref.day, 28)) <= day <= ref + dt.timedelta(days=366)


def _iso10(v: str) -> str | None:
    v = (v or "").strip()
    m = re.match(r"(20\d\d|19\d\d)-(\d\d)-(\d\d)", v)
    if m:
        return m.group(0)
    m = TEXT_DATE.search(v)
    if not m:
        return None
    if m.group(1):
        mo = MONTHS.get(m.group(1).lower()[:3])
        return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}" if mo else None
    if m.group(4):
        return f"{m.group(6)}-{int(m.group(4)):02d}-{int(m.group(5)):02d}"
    return f"{m.group(7)}-{m.group(8)}-{m.group(9)}"


def main_text(html: str) -> str:
    """Plain text of a page's main content (markdown stripped of link targets and emphasis)."""
    _, md = html_to_markdown(html)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)
    t = re.sub(r"[*#>`]+", "", t)
    return re.sub(r"[ \t]+", " ", t).strip()


FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/feed+json")


def find_feed_links(html: str, base_url: str) -> list[str]:
    soup = soup_of(html)
    out = []
    for link in soup.find_all("link", rel=True, href=True):
        rel = " ".join(link["rel"]) if isinstance(link["rel"], list) else link["rel"]
        if "alternate" in rel and link.get("type", "").lower() in FEED_TYPES:
            out.append(urljoin(base_url, link["href"]))
    for a in soup.find_all("a", href=True):
        h = a["href"].lower()
        if any(k in h for k in ("rss.aspx", "rssfeed", "/rss", "/feed", ".rss", "atom.xml")):
            out.append(urljoin(base_url, a["href"]))
    return list(dict.fromkeys(out))


def find_api_hints(html: str, base_url: str) -> dict:
    soup = soup_of(html)
    hints = {}
    for link in soup.find_all("link", rel=True, href=True):
        rel = " ".join(link["rel"]) if isinstance(link["rel"], list) else link["rel"]
        if "https://api.w.org/" in rel:
            hints["wp_json"] = urljoin(base_url, link["href"])
    m = re.search(r"https?://([a-z0-9\-]+)\.legistar\.com", html, re.I)
    if m:
        hints["legistar_client"] = m.group(1).lower()
    return hints


def links(html: str, base_url: str) -> list[str]:
    soup = soup_of(html)
    return [u for u in dict.fromkeys(urljoin(base_url, a["href"]).split("#")[0] for a in soup.find_all("a", href=True)) if u]
