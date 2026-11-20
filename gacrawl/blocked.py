"""What happens when a site won't let crawlers in.

Some hosts (Municode's code library, for one) sit behind bot protection that
answers every automated request with a challenge page. The crawler does not
try to defeat that; it would be unauthorized access, and it would get the
project's addresses banned everywhere the same protection is used. Instead:

  1. the host goes on a block list (state/blocked.json) for `ttl_days`, so the
     crawler stops hitting it immediately and spends the budget elsewhere;
  2. each blocked source is re-routed to the next best place the same records
     live (ALTERNATIVES below), when one is known;
  3. what can't be re-routed becomes a manual task (`gacrawl tasks`): a short
     list of URLs to capture by hand and feed to `gacrawl ocr`, plus a ready-to-
     send open records request (Georgia Open Records Act, O.C.G.A. 50-18-70)
     for the clerk, since a city's code of ordinances is a public record the
     city must provide.
"""
from __future__ import annotations

import datetime as dt
import json
from .registry import load_json, write_json_atomic
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

CHALLENGE_MARKERS = (
    "cf-chl", "challenge-platform", "cf-browser-verification", "Access blocked",
    "Just a moment...", "__cf_chl", "Attention Required! | Cloudflare", "px-captcha", "_Incapsula_Resource",
    "distil_r_captcha", "Request unsuccessful. Incapsula", "awswaf", "DataDome", "Sorry, you have been blocked",
)
CLOUDFLARE_CODE = re.compile(r"error code: 10\d\d\b")     # Cloudflare's bare refusals ("error code: 1020": access denied by a rule)

# A copy of a page kept by the Internet Archive (gacrawl/wayback.py RAW and SHOW). What it holds is the page as that
# day's visitor got it, and for a site behind a firewall that is often the firewall's challenge page. That is the
# copied site refusing the Archive's crawler on that day, not the Archive refusing this one. Judged as the Archive's
# own, one such copy (Simbli's policy list as saved on May 27, 2024, an Imperva shell) put web.archive.org on the block
# list for a week on Nov. 19, 2026: every reader of archived copies (refused sources, school boards, the state
# authorities read from the Archive, lapsed campaign sites) found the Archive off from then on, and the Georgia World
# Congress Center Authority reached the site with no meetings. The Archive's own refusals are a 429 or a 5xx
# (gacrawl/wayback.py switches off for the run on those) or a plain 403.
ARCHIVE_COPY = re.compile(r"^https?://web\.archive\.org/web/\d{4,14}(?:[a-z]{2}_)?/\S", re.I)
ARCHIVE_HOSTS = ("web.archive.org",)
# a page that is only a firewall's challenge or refusal, by what it says near its top (a copy is read for these whatever
# status it came with): the copy is passed over and an older one tried
CHALLENGE_PAGE = re.compile(r"request unsuccessful\. incapsula|incapsula incident|pardon our interruption|"
                            r"attention required! \| cloudflare|just a moment\.\.\.|checking your browser before|"
                            r"verify you are human|are you a robot|px-captcha|sorry, you have been blocked|"
                            r"the requested url was rejected|request rejected", re.I)
# Imperva's script is on every page of a site behind it, the real ones too: it marks a challenge only on a page that
# says next to nothing else
IMPERVA_SCRIPT = re.compile(r"_incapsula_resource", re.I)
_NOT_TEXT = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<[^>]+>", re.S | re.I)
REPLAY_HEADERS = ("memento-datetime",)          # the Archive's own mark on a copy it replays (and x-archive-orig-*)


def is_archive_copy(url: str) -> bool:
    return bool(ARCHIVE_COPY.match(url or ""))


def replayed(headers) -> bool:
    """Do an answer's headers say it is a copy the Archive replays (Memento-Datetime, X-Archive-Orig-...)? Its own
    refusals and error pages carry neither."""
    return any(str(k).lower() in REPLAY_HEADERS or str(k).lower().startswith("x-archive-orig-") for k in (headers or {}))


def challenge_copy(text) -> bool:
    """Is this copy of a page its site's firewall challenge, not the page? Short pages only: a real page can quote
    these words, a challenge shell is little else. (Text, or a file's bytes: a PDF or a zip is never one.)"""
    if isinstance(text, (bytes, bytearray)):
        if len(text) >= 20000:
            return False
        text = bytes(text).decode("utf-8", "replace")
    t = text or ""
    if len(t) >= 20000:
        return False
    if CHALLENGE_PAGE.search(t[:6000]):
        return True
    return bool(IMPERVA_SCRIPT.search(t[:6000])) and len(_NOT_TEXT.sub(" ", t).split()) < 40

# where else the same records live, by blocked host
ALTERNATIVES = {
    "simbli.eboardsolutions.com": [
        "the district's own website (Board of Education pages often post the agenda, minutes or a meeting video link)",
        "the district's YouTube channel, when its website links one (official channels only)",
        "an open records request to the superintendent's office for agendas and approved minutes",
        "manual capture: open the Simbli meeting in a browser, save/print to PDF, drop it in imports/screens/",
        "ask eBoard Solutions (Simbli) or the Georgia School Boards Association whether districts can publish a feed",
    ],
    "library.municode.com": [
        "the jurisdiction's own website (many post the code or recent ordinances as PDFs; the :site source picks those up)",
        "eCode360 or American Legal, if the jurisdiction also publishes there (the seed step records those links)",
        "an open records request to the city/county clerk for the current code (template in data/manual_tasks.md)",
        "manual capture: open the Municode page in a browser, save/print to PDF, run `gacrawl ocr` on it",
    ],
    "api.municode.com": ["same as library.municode.com"],
    "query.wikidata.org": ["run `gacrawl seed` from the crawler itself (its honest User-Agent is what Wikimedia asks for); "
                           "requests through shared/rotating proxies are refused"],
}

RECORDS_REQUEST = """Subject: Open Records Request - current Code of Ordinances ({name})

To the Records Custodian / City Clerk, {name}:

Under the Georgia Open Records Act (O.C.G.A. § 50-18-70 et seq.), I request an
electronic copy of the current, complete Code of Ordinances of {name},
including the charter, all codified ordinances and any ordinances adopted since
the last codification supplement. A PDF or Word file is preferred; a link to a
download is fine. If there will be any cost for this request, please send an
estimate before proceeding; no charges are approved in advance.

The Act requires a response within three business days. Thank you.

{requester}
"""


class Blocked(Exception):
    def __init__(self, host: str, reason: str):
        super().__init__(f"{host}: {reason}")
        self.host, self.reason = host, reason


def looks_blocked(status: int, body: bytes | str, url: str = "", headers=None) -> str | None:
    """Reason string if this response is a bot-protection challenge, else None. `url`: the address asked for; when it
    is a copy kept by the Internet Archive, a challenge in its body is the copied site's, of another day and another
    crawler (ARCHIVE_COPY above), and is no refusal by the host that served the copy. That holds for a copy served
    as a page (200), and for one replayed with its own refusal's status (a saved 403) when the answer's `headers` say
    it is a replay; a 403, 429 or 503 at a copy's address without that mark is the Archive's own answer."""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
    head = text[:6000]
    copy = is_archive_copy(url) and (status == 200 or replayed(headers))
    if not copy and status in (401, 403, 429, 503) and (any(m in head for m in CHALLENGE_MARKERS) or CLOUDFLARE_CODE.search(head)):
        return f"HTTP {status} bot-protection challenge"
    if not copy and status == 200 and len(text) < 4000 and ("_Incapsula_Resource" in head or "Request unsuccessful. Incapsula" in head):
        return "HTTP 200 bot-protection challenge (Imperva)"   # a short challenge shell served as a normal page
    if status == 403 and len(head) < 2000 and re.search(r"access (denied|blocked)|forbidden", head, re.I):
        return "HTTP 403 access denied"
    return None


def not_a_refusal(reason: str, host: str = "", urls=()) -> bool:
    """A block filed under a rule since withdrawn, not honoured any more:
      - for a plain 401/403 on robots.txt alone ("robots.txt: HTTP 403 access denied"). That is a server that does not
        serve the file, not one that refuses the crawler (RFC 9309: no robots.txt, the site is open); such entries
        were written until Nov. 20, 2026. A challenge page on robots.txt is a refusal;
      - for a challenge page served as a page (200) on the Internet Archive's host, met only at copies' addresses
        (`urls`: the addresses the entry was filed for): each was a copy of a page that was its own site's challenge
        (ARCHIVE_COPY above), filed as the Archive's until Nov. 20, 2026. A copy served as a page can no longer file
        one. An entry for the Archive's own answer is honoured: a 403 or 429 (at a copy's address too), or anything at
        the index, the front door or robots.txt."""
    if (host.lower() in ARCHIVE_HOSTS and reason.startswith("HTTP 200 bot-protection challenge") and urls
            and all(is_archive_copy(u) for u in urls)):
        return True
    return reason.startswith("robots.txt:") and "challenge" not in reason


class BlockList:
    def __init__(self, path: Path, ttl_days: int = 7):
        self.path = Path(path)
        self.ttl = dt.timedelta(days=ttl_days)
        self._lock = threading.Lock()
        self.data: dict[str, dict] = load_json(self.path)

    @staticmethod
    def host(url: str) -> str:
        return urlsplit(url).netloc.lower()

    def is_blocked(self, url: str) -> dict | None:
        h = self.host(url)
        rec = self.data.get(h)
        if not rec:
            return None
        ttl = dt.timedelta(days=float(rec["ttl_days"])) if rec.get("ttl_days") else self.ttl
        if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(rec["last_seen"]) > ttl:
            return None  # expired: try again
        if not_a_refusal(rec.get("reason") or "", h, rec.get("urls") or ()):
            return None  # filed under a rule since withdrawn: asked again now
        return rec

    def add(self, url: str, reason: str, source_id: str | None = None, ttl_days: float | None = None) -> dict:
        """`ttl_days`: how long this entry holds when not the list's own time (a block worked out by counting plain
        refusals is tried again after a day; a challenge or a refused front door keeps the list's 7)."""
        h = self.host(url)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        with self._lock:
            rec = self.data.setdefault(h, {"first_seen": now, "count": 0, "urls": [], "sources": []})
            rec.update(reason=reason, last_seen=now, count=rec["count"] + 1)
            if ttl_days:
                rec["ttl_days"] = ttl_days
            else:
                rec.pop("ttl_days", None)
            if url not in rec["urls"]:
                rec["urls"] = (rec["urls"] + [url])[-20:]
            if source_id and source_id not in rec["sources"]:
                rec["sources"].append(source_id)
            rec["alternatives"] = ALTERNATIVES.get(h.removeprefix("www."), [
                "check whether the site offers an API, RSS feed or bulk download (the discover step tries these)",
                "manual capture + `gacrawl ocr`, or an open records request (template in data/manual_tasks.md)"])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, self.data, indent=2)
        return rec

    def note_source(self, host: str, source_id: str):
        with self._lock:
            rec = self.data.get(host)
            if rec is not None and source_id not in rec["sources"]:
                rec["sources"].append(source_id)
                write_json_atomic(self.path, self.data, indent=2)

    def clear(self, host: str):
        with self._lock:
            if self.data.pop(host, None) is not None:
                write_json_atomic(self.path, self.data, indent=2)


def manual_tasks_md(blocklist: BlockList, jurisdictions: list[dict], requester: str = "[your name / organization]") -> str:
    """Markdown checklist of what needs a human: blocked code libraries, jurisdictions with no website, etc."""
    by_id = {j["id"]: j for j in jurisdictions}
    lines = ["# Manual tasks", "", "Generated by `gacrawl tasks`. Things the crawler can't do by itself.", ""]
    if blocklist.data:
        lines += ["## Blocked hosts", ""]
        for host, rec in sorted(blocklist.data.items()):
            lines.append(f"### {host}")
            lines.append(f"- reason: {rec['reason']} (seen {rec['count']}x, last {rec['last_seen'][:10]})")
            lines.append("- what to do instead:")
            lines += [f"  - {a}" for a in rec.get("alternatives", [])]
            if rec.get("sources"):
                lines.append("- affected sources: " + ", ".join(rec["sources"]))
            if rec.get("urls"):
                lines.append("- capture by hand, then `gacrawl ocr <file> --source <jurisdiction> --kind ordinance --url <url>`:")
                lines += [f"  - {u}" for u in rec["urls"]]
            lines.append("")
    municode_only = [j for j in jurisdictions
                     if not any(j.get("portals", {}).get(k) for k in ("ecode360", "amlegal")) and j.get("municode")]
    if municode_only:
        lines += ["## Codes of ordinances (Municode)", "",
                  f"Municode blocks crawlers. {len(municode_only)} jurisdictions have a confirmed Municode code link and no "
                  "other code library on record. Capture by hand (`gacrawl ocr`) or send the records request below. "
                  "Jurisdictions not listed here have no confirmed code link yet; `gacrawl seed` records one when the "
                  "official website links to it.", ""]
        for j in sorted(municode_only, key=lambda x: -(x.get("population") or 0)):
            lines.append(f"- [ ] {j['name']} (pop. {j.get('population') or '?'}): {j['municode']}")
        lines.append("")
    no_site = [j for j in jurisdictions if not j.get("website")]
    if no_site:
        lines += ["## No official website found", "", "Add one in config/overrides.yaml if it exists; otherwise records come "
                  "from the county, GMA profile and open records requests.", ""]
        lines += [f"- [ ] {j['name']} ({j['kind']}, pop. {j.get('population') or '?'})" for j in no_site]
        lines.append("")
    lines += ["## Open records request template", "", "```", RECORDS_REQUEST.format(name="<jurisdiction>", requester=requester), "```"]
    return "\n".join(lines)
