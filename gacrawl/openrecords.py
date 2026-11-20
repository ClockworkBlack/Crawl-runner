"""Georgia Open Records Act requests for codes of ordinances, sent from a project mailbox.

plan       which jurisdictions to ask: cities, towns, counties and consolidated governments only (school districts
           have board policies, not ordinances), the code isn't crawlable (no eCode360 / American Legal source and
           no ordinance records yet), and no request in the cool-down period. Largest first.
vet        which address: a clerk or open-records address on the jurisdiction's own domain. Department addresses
           (police, sheriff, fire, 911, health, courts, newsroom, HR, finance, utilities...), other domains, test
           and no-reply addresses are dropped; a person's own or a general info@ address goes to the review list
           (data/openrecords/review.md) until approved in config/private/openrecords.yaml. A portal is used when there is
           no clerk/records address; links that are images or PDFs are not portals.
test       `records test`: one clearly marked sample letter to the project mailbox (or --to), then an IMAP login
           and a look for it in the inbox. No clerk is contacted and the tracker is untouched.
draft      one request per jurisdiction (O.C.G.A. § 50-18-70 et seq.), signed with the project name.
send       a small daily batch over SMTP. Off unless `openrecords.enabled` is true AND the mailbox secrets
           exist. Never to a guessed address; never twice inside the cool-down; stops for the day on a bounce
           or SMTP error.
check      reads replies over IMAP (matched by the [ORR-<id>] tag in the subject or the thread headers), each from
           its own text above the quoted original: acknowledgement, cost estimate, records attached (documents go
           to imports/ordinances/ as <id>__<name>, where the ordinances workflow reads them and sends them to
           Electo) or linked, bounce, denial. A letter, invoice, estimate or draft is not the Code, nor is a file
           with a name that says nothing unless the reply says it is the code, names the request (and nothing else
           as what it sends) or only says "see attached", nor anything in a note whose greeting names someone else,
           nor are images, unless several page scans come with a reply saying they are the code's pages (held in
           data/openrecords/attachments/, the owner told); icons, logos and signature images are dropped. A
           person's reply that names nothing else is an answer ("Will do."); a message about something else that
           lands in the thread, or an out-of-office reply, changes nothing (the owner told of the first).
follow-up  one polite follow-up in the same thread when the 3-business-day deadline has passed by a few days and
           no one but a mail system has written in the thread.
withdraw   a place that has since got a downloaded code (a folder in imports/ordinances/) is skipped by plan and
           follow-up; an open request to it is withdrawn with one short note in the same thread, so the clerk
           doesn't spend time (or charge) for a code we have. Portal-only entries are just closed.

Mailbox secrets (environment): ORR_SMTP_HOST, ORR_SMTP_PORT (465 SSL or 587 STARTTLS), ORR_USER, ORR_PASSWORD,
ORR_FROM (the project address), ORR_IMAP_HOST (optional, for replies; port 993).
Tracker: data/openrecords/requests.json (committed, so the history is visible in the repo).
"""
from __future__ import annotations

import datetime as dt
import email
import email.utils
import functools
import imaplib
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.header import decode_header, make_header
from pathlib import Path

SUBJECT = "Open Records Request: current Code of Ordinances [ORR-{jid}]"
BODY = """To the Records Custodian, {name}:

Under the Georgia Open Records Act, O.C.G.A. § 50-18-70 et seq., I request an electronic copy of:

1. The current, complete Code of Ordinances of {name}, including the charter and all codified ordinances; and
2. Any ordinances adopted since the most recent codification supplement that are not yet included in it.

An electronic copy (PDF or Word) by email is preferred, or a link to download it. If the code is already
available online in full, a link to that copy will satisfy this request.

If there will be any cost for this request, please send an estimate before proceeding; no charges are
approved in advance.

The Act asks for a response within three business days. Thank You for Your help.

{signature}
{reply_line}"""
WITHDRAW = """To the Records Custodian, {name}:

Regarding the Open Records Act request below, sent {sent_on}, for the Code of Ordinances of {name}: we have since
obtained a copy, so I am withdrawing the request. No further work or response is needed. Thank You for Your time.

{signature}
"""
FOLLOW_UP = """To the Records Custodian, {name}:

I am following up on the Open Records Act request below, sent {sent_on}, for an electronic copy of the current
Code of Ordinances of {name}. I have not yet received a response. Could You let me know its status?

Thank You,
{signature}
"""
STATUSES = ("planned", "sent", "acknowledged", "cost_estimate", "fulfilled", "partial", "denied", "bounced",
            "followed_up", "portal_only", "error", "withdrawn")
OPEN = ("sent", "acknowledged", "followed_up", "cost_estimate", "partial", "error")


def business_days_after(d: dt.date, n: int) -> dt.date:
    """n business days after d (weekends skipped; state holidays not counted, so this can be a day early)."""
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Tracker:
    def __init__(self, root: Path):
        self.path = root / "data" / "openrecords" / "requests.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    def get(self, jid: str) -> dict:
        return self.data.setdefault(jid, {"jurisdiction": jid, "history": []})

    def log(self, jid: str, status: str, note: str = "", **fields):
        r = self.get(jid)
        r.update(fields)
        r["status"] = status
        r["updated_at"] = _now()
        r["history"].append({"at": r["updated_at"], "status": status, "note": note[:500]})


ASKABLE_KINDS = ("city", "town", "county", "consolidated")
# a department's own records desk, not the clerk who keeps the code
DEPARTMENT = re.compile(r"(^|[^a-z])(pd|dkpd|lppd|police|sheriff|fire|dcfr|e?911|ems|jail|health|boh|dph|court|"
                        r"superior|probate|magistrate|newsroom|news|media|press|pio|hr|humanresources|finance|"
                        r"utilit|water|tax|occtax|boa|assessor|tag|elections?|voter|planning|zoning|permits?|"
                        r"subscri|webmaster)", re.I)
DEPT_PAGE = re.compile(r"/(courts?|clerk-of-court|police|sheriff|fire|health|media-relations|newsroom|"
                       r"municipal-court[^/]*)(/|$)", re.I)
JUNK = re.compile(r"(^|[^a-z])(test|example|noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster)"
                  r"([^a-z]|$)", re.I)
STRONG = re.compile(r"record|orr|foia|clerk", re.I)
GENERIC = re.compile(r"^(info|contact|cityhall|admin|office|general|citizeninput|customerservice|help)$", re.I)
NOT_PORTAL = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|docx?)(\?|$)", re.I)


def _base(host: str) -> str:
    host = (host or "").lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-3:]) if host.endswith((".ga.us", ".ga.gov")) and len(parts) > 3 else ".".join(parts[-2:])


def _clean(addr: str) -> str:
    a = re.sub(r"^(?://|u003e|mailto:)+", "", (addr or "").strip().lower())
    return a if re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", a) else ""


def classify_email(addr: str, j: dict, page: str = "") -> tuple[str, str]:
    """('ok' | 'review' | 'drop', why) for one published address."""
    from urllib.parse import urlsplit

    a = _clean(addr)
    if not a:
        return "drop", "not an address"
    local, dom = a.split("@", 1)
    if JUNK.search(a):
        return "drop", "test / no-reply address"
    site = j.get("website") or page
    hosts = {_base(urlsplit(u).netloc) for u in (site, page) if u}
    same_name = lambda d, h: d.split(".")[-2:-1] == h.split(".")[-2:-1] and d.endswith((".gov", ".us", ".com", ".org", ".net"))
    if not any(dom == h or dom.endswith("." + h) or same_name(dom, h) for h in hosts if h):
        return "drop", f"another domain ({dom})"
    strong_records = re.search(r"record|orr|foia", local)
    if DEPARTMENT.search(local) and not re.fullmatch(r"(open)?records?|orr|foia|clerk|cityclerk|countyclerk", local):
        return "drop", "a department's address"
    if page and DEPT_PAGE.search(urlsplit(page).path) and not strong_records:
        return "review", "listed on a department's page"
    if STRONG.search(local):
        return "ok", "clerk / records address"
    if GENERIC.match(local):
        return "review", "general inbox"
    return "review", "a person's own address"


def vet(j: dict, approved: dict | None = None, portals: dict | None = None) -> dict:
    """How this jurisdiction would be asked: {'how': email|portal|review|none, 'to', 'why', 'candidates'}."""
    rc = j.get("records") or {}
    if j["id"] in (approved or {}):
        return {"how": "email", "to": approved[j["id"]], "why": "approved in config/private/openrecords.yaml", "candidates": []}
    if j["id"] in (portals or {}):
        return {"how": "portal", "to": portals[j["id"]], "why": "portal set in config/private/openrecords.yaml", "candidates": []}
    cands = []
    for e in rc.get("emails") or []:
        a = _clean(e)
        if a and a not in [c[0] for c in cands]:
            cands.append((a, *classify_email(a, j, rc.get("page") or "")))
    ok = sorted((c for c in cands if c[1] == "ok"), key=lambda c: (not re.search(r"record|orr|foia", c[0]), len(c[0])))
    portal = (rc.get("portal") or "").rstrip("\\/ ") or None
    if portal and (NOT_PORTAL.search(portal) or re.search(r"police|sheriff|fire|court|health|jail", portal.split("//")[-1].split("/")[0], re.I)):
        portal = None   # an image / PDF, or a department's own portal (a sheriff's, say)
    if ok:
        return {"how": "email", "to": ok[0][0], "why": ok[0][2], "candidates": cands}
    if portal:
        return {"how": "portal", "to": portal, "why": "official portal", "candidates": cands}
    if any(c[1] == "review" for c in cands):
        return {"how": "review", "to": None, "why": "no clerk / records address", "candidates": cands}
    return {"how": "none", "to": None, "why": "no usable address" if cands else "no contact found", "candidates": cands}


def load_contacts(root: Path) -> dict:
    """config/private/openrecords.yaml (kept only in the private repository; contacts are people's addresses):
    approved: {jid: address}, portals: {jid: url}, skip: [jid, ...], ask_anyway: [jid, ...]. config/openrecords.yaml is read if present."""
    p = root / "config" / "private" / "openrecords.yaml"
    if not p.exists():
        p = root / "config" / "openrecords.yaml"
    if not p.exists():
        return {"approved": {}, "portals": {}, "skip": []}
    import yaml

    d = yaml.safe_load(p.read_text()) or {}
    return {"approved": {k: _clean(v) for k, v in (d.get("approved") or {}).items() if _clean(v)},
            "portals": {k: str(v).strip() for k, v in (d.get("portals") or {}).items() if str(v).startswith("http")},
            "skip": list(d.get("skip") or []), "ask_anyway": list(d.get("ask_anyway") or [])}


CLERKS_FILE = "data/openrecords/clerks.json"
# an address is offered to Electo's drafter when it was printed on an official page (the place's own site, or the GMA,
# ACCG, county or regional commission directory); one only a third-party page prints stays in the worklist
CLERK_EMAIL_OK = ("high", "medium")


def load_clerks(root: Path) -> dict:
    """{jurisdiction id: what was found} from data/openrecords/clerks.json: the clerk (or whoever keeps the code) of
    each place with no code on file, looked up by hand on the place's own site and the official directories. Every
    fact carries the page it was read on. Nobody is asked because of this file: a request goes out only to an address
    under `approved:` in config/private/openrecords.yaml."""
    import json as _json

    try:
        return (_json.loads((root / CLERKS_FILE).read_text()) or {}).get("clerks") or {}
    except (OSError, ValueError):
        return {}


def clerk_contact(c: dict) -> dict | None:
    """One clerks.json entry as the export's records contact: the addresses rated high or medium (best first), the
    clerk's name and title, city hall's phone, hours and mailing address, each with its page. None when the entry
    has nothing to reach anyone by."""
    rank = {"high": 0, "medium": 1}
    rows = sorted((e for e in c.get("emails") or [] if e.get("confidence") in CLERK_EMAIL_OK and _clean(e.get("address"))),
                  key=lambda e: rank[e["confidence"]])
    emails = list(dict.fromkeys(_clean(e["address"]) for e in rows))
    clerk, phone = c.get("clerk") or {}, c.get("phone") or {}
    hours, mail = c.get("hours") or {}, c.get("mailing_address") or c.get("street_address") or {}
    form = (c.get("records_request") or {}).get("url")
    if not (emails or phone.get("number") or form):
        return None
    out = {"emails": emails}
    if clerk.get("name"):
        out["clerk"] = {k: clerk[k] for k in ("name", "title", "src") if clerk.get(k)}
    if phone.get("number"):
        out["phone"] = {k: phone[k] for k in ("number", "label", "src") if phone.get(k)}
    if hours.get("text"):
        out["hours"] = hours["text"]
    if mail.get("text"):
        out["mail"] = mail["text"]
    if form and str(form).startswith("http") and not NOT_PORTAL.search(str(form)):
        out["portal"] = form
    src = (rows[0].get("src") if rows else None) or clerk.get("src") or phone.get("src")
    if src:
        out["page"] = src
    if rows:
        out["email_from"] = [{"address": _clean(e["address"]), "whose": e.get("whose"), "confidence": e["confidence"],
                              "src": e.get("src")} for e in rows]
    if c.get("checked"):
        out["checked"] = c["checked"]
    return out


def write_review(root: Path, reviews: list[dict]) -> Path:
    p = root / "data" / "openrecords" / "review.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Open records: addresses to check", "",
             "These places would be asked for their Code of Ordinances, but no clerk or open-records address was "
             "found (only a person's own address, a general inbox, or addresses that were dropped). Check the page, then add the right address under `approved:` in `config/openrecords.yaml` "
             "(or the id under `skip:`). Rewritten on every plan / send.", ""]
    for r in reviews:
        j = r["jurisdiction"]
        lines.append(f"## {j['name']} (`{j['id']}`): {r['why']}")
        if (j.get("records") or {}).get("page"):
            lines.append(f"- page: {j['records']['page']}")
        for a, verdict, why in r["candidates"]:
            lines.append(f"- {a}: {'not used' if verdict == 'drop' else 'needs a check'} ({why})")
        lines.append("")
    p.write_text("\n".join(lines) if reviews else "# Open records: addresses to check\n\nNothing to check.\n")
    return p


CODE_MIN = 25          # ordinance records that make a real code, not a stray ordinance or two
CODES_FILE = "state/ordinance_codes.json"


def ordinance_counts(store) -> dict:
    """{jurisdiction: ordinance records} from the database (what `gacrawl records codes` writes to CODES_FILE)."""
    with store._lock:
        return {j: n for j, n in store.db.execute(
            "SELECT jurisdiction, COUNT(*) FROM records WHERE kind='ordinance' AND jurisdiction <> '' GROUP BY 1")}


_CODES: dict | None = None


def _codes_on_file(root: Path | None = None) -> dict:
    """The crawl's count of ordinance records per place (CODES_FILE, written by the merge job): what records-send reads
    instead of the 15 GB database, which no longer fits on a private repository's runner (records-send failed at the
    download on Nov 16, 17 and 18, 2026, and sent no follow-ups)."""
    global _CODES
    if _CODES is None:
        import json as _json

        p = (root or Path(__file__).resolve().parent.parent) / CODES_FILE
        try:
            _CODES = (_json.loads(p.read_text()) or {}).get("counts") or {}
        except (OSError, ValueError):
            _CODES = {}
    return _CODES


def crawlable_code(j: dict, store=None) -> bool:
    if any(j.get("portals", {}).get(k) for k in ("ecode360", "amlegal")):
        return True
    if store is not None:
        with store._lock:
            n = store.db.execute("SELECT COUNT(*) FROM records WHERE jurisdiction=? AND kind='ordinance'", (j["id"],)).fetchone()[0]
        return n >= CODE_MIN
    return int(_codes_on_file().get(j["id"]) or 0) >= CODE_MIN


# ----------------------------------------------------------------------------- places with a downloaded code
def _inbox_paths(root: Path) -> list[str]:
    """Every file under imports/ordinances/, relative to it. Read from git when it can be (works without the files
    checked out, so a job can skip the multi-gigabyte download), else from the disk."""
    import subprocess

    inbox = root / "imports" / "ordinances"
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only", "HEAD", "--", "imports/ordinances"],
                             capture_output=True, text=True, timeout=120, check=True).stdout
        paths = [p[len("imports/ordinances/"):] for p in out.splitlines() if p.startswith("imports/ordinances/")]
        if paths:
            return paths
    except (OSError, subprocess.SubprocessError):
        pass
    return [p.relative_to(inbox).as_posix() for p in inbox.rglob("*") if p.is_file()] if inbox.exists() else []


def downloaded_places(root: Path, places=None) -> set[str]:
    """Registry ids that already have a downloaded code or ordinance file in imports/ordinances/, from the folder
    names alone (nothing is read). The downloader's GA/<Municode slug>/ folders name a city by its bare name
    (`franklin`) and a county with `_county` (`franklin_county`), so a bare name is the city when there is one.
    Files in a zip, or loose files whose place is only in their text, are not counted (they can't be placed
    without reading them); at worst such a place is asked for a code it already has."""
    from . import ordinance_files as _of

    places = places or _of.Places(_of.load_places(root))
    have: set[str] = set()
    roots: dict[str, set[str]] = {}   # id -> the folders (or top-level files) that named it
    for rel in _inbox_paths(root):
        parts = [x for x in rel.split("/") if x]
        if len(parts) < 2:  # a loose file at the top: <id>__<name>.pdf
            m = re.match(r"(.+?)__", parts[0]) if parts else None
            jid = places.resolve(m.group(1)) if m else None
            if jid:
                have.add(jid)
                roots.setdefault(jid, set()).add("")
            continue
        for i, x in enumerate(parts[:-1]):
            if x.lower().endswith(".zip") or _of._norm(x) in _of.NOISE_DIRS:
                continue
            k = re.sub(r" (consolidated|unified) government$", "", _of._norm(x))  # webster_county_consolidated_government
            jid = None if k.endswith(" county") else places.resolve(k)
            jid = jid or places.resolve_folder(k)[0]
            if jid:
                have.add(jid)
                roots.setdefault(jid, set()).add("/".join(parts[: i + 1]))
                break
    # what the code finder saved counts only when it adds up to the code (a whole code or a chapter series): a place
    # with a few individual ordinances from its site, and nothing else, is still without its code
    from .codefinder import load_found

    # A file someone added by hand to the same folder (not one the finder saved) counts as a code.
    files_in: dict[str, set[str]] = {}
    for rel in _inbox_paths(root):
        top, _, rest = rel.partition("/")
        if rest and Path(rest).name.lower() != "sources.csv":
            files_in.setdefault(top, set()).add(rel)
    for jid, f in load_found(root).items():
        if f.get("saved") and not f.get("complete") and roots.get(jid) == {jid}:
            by_finder = {s["file"] for s in f["saved"]}
            if files_in.get(jid, set()) <= by_finder:
                have.discard(jid)
    # a consolidated government's code is its county's too (macon-bibb -> co-bibb), and the reverse
    for j in places.places:
        if j.get("kind") == "consolidated" and "-" in j["id"]:
            county = "co-" + j["id"].split("-", 1)[1]
            if county in places.by_id and (j["id"] in have or county in have):
                have |= {j["id"], county}
    return have


def plan(jurisdictions: list[dict], tracker: Tracker, store=None, cooldown_days: int = 180,
         contacts: dict | None = None, reviews: list | None = None, have: set | None = None,
         found: dict | None = None) -> list[dict]:
    """Jurisdictions due a request, largest first, each with how it would be sent. Places whose only addresses
    need a check are appended to `reviews` (when given) instead. `have`: ids with a downloaded code (skipped).
    `found`: codefinder's results (data/openrecords/found.json): its address is used when the seed found none,
    and a place whose site links to its code on a code library is skipped (the code is online)."""
    now = dt.datetime.now(dt.timezone.utc)
    contacts = contacts or {}
    out = []
    for j in sorted(jurisdictions, key=lambda j: -(j.get("population") or 0)):
        if j.get("kind") not in ASKABLE_KINDS or j["id"] in (contacts.get("skip") or []):
            continue
        if j.get("enabled") is False or crawlable_code(j, store) or j["id"] in (have or ()):
            continue
        f = (found or {}).get(j["id"]) or {}
        if f.get("libraries"):
            continue
        rec, frec = j.get("records") or {}, f.get("records") or {}
        if not (rec.get("emails") or rec.get("portal")) and (frec.get("emails") or frec.get("portal")):
            j = {**j, "records": frec}   # the seed found no address; the deeper look did
        t = tracker.data.get(j["id"], {})
        if t.get("sent_at") and (now - dt.datetime.fromisoformat(t["sent_at"])).days < cooldown_days:
            continue
        if t.get("status") in ("fulfilled", "denied") and t.get("updated_at") and \
                (now - dt.datetime.fromisoformat(t["updated_at"])).days < cooldown_days:
            continue
        v = vet(j, contacts.get("approved"), contacts.get("portals"))
        found_on = (j.get("records") or {}).get("page")
        if v["how"] in ("email", "portal"):
            out.append({"jurisdiction": j, "to": v["to"], "how": v["how"], "found_on": found_on, "why": v["why"]})
        elif reviews is not None and (v["how"] == "review" or v["candidates"]):
            reviews.append({"jurisdiction": j, **v})
    return out


def draft(j: dict, requester: str, reply_to: str = "", organization: str = "") -> tuple[str, str]:
    name = j["name"] if j["kind"] == "county" else (j["name"] if j["name"].lower().startswith(("city", "town"))
                                                      else f"the City of {j['name']}" if j["kind"] != "town"
                                                      else f"the Town of {j['name']}")
    if j["kind"] == "consolidated":
        name = j.get("official_name") or f"{j['name']}-{(j.get('consolidated_with') or '').replace(' County', '')} " \
                                           "consolidated government"
    subject = SUBJECT.format(jid=j["id"])
    signature = requester + (f"\n{organization}" if organization else "")
    body = BODY.format(name=name, signature=signature, reply_line=f"Reply to: {reply_to}" if reply_to else "")
    return subject, body.strip() + "\n"


class Mailbox:
    def __init__(self, env: dict | None = None):
        e = env or os.environ
        g = lambda k: (e.get(k) or "").strip() or None   # pasted secrets often carry a stray space or newline
        self.smtp_host, self.user = g("ORR_SMTP_HOST"), g("ORR_USER")
        self.password = (e.get("ORR_PASSWORD") or "").strip("\r\n\t ") or None
        self.port = int(g("ORR_SMTP_PORT") or 465)
        self.sender = g("ORR_FROM") or self.user
        self.imap_host = g("ORR_IMAP_HOST")

    @property
    def can_send(self) -> bool:
        return bool(self.smtp_host and self.user and self.password and self.sender)

    def _connect(self, port: int, timeout: int = 60):
        ctx = ssl.create_default_context()
        if port == 465:
            return smtplib.SMTP_SSL(self.smtp_host, port, context=ctx, timeout=timeout)
        s = smtplib.SMTP(self.smtp_host, port, timeout=timeout)
        s.starttls(context=ctx)
        return s

    def probe(self) -> list[dict]:
        """Try the configured port and both standard ones (no login, nothing sent): which ones answer."""
        out = []
        for port in dict.fromkeys([self.port, 465, 587]):
            mode = "SSL from the start" if port == 465 else "STARTTLS"
            try:
                s = self._connect(port, timeout=20)
                s.ehlo()
                ok = s.has_extn("auth")
                s.quit()
                out.append({"port": port, "mode": mode, "result": "answers" + ("" if ok else " (but offers no login)")})
            except Exception as exc:  # noqa: BLE001
                out.append({"port": port, "mode": mode, "result": f"{type(exc).__name__}: {exc}"[:160]})
        return out

    def send(self, msg: EmailMessage, smtp_factory=None):
        stage = "connect"
        s = smtp_factory() if smtp_factory else self._connect(self.port)
        try:
            stage = "login"
            s.login(self.user, self.password)
            stage = "send"
            s.send_message(msg)
        except Exception as exc:  # noqa: BLE001 — say which step failed
            raise type(exc)(*exc.args) if stage == "connect" else RuntimeError(f"at {stage}: {type(exc).__name__}: {exc}") from exc
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass

    def fetch_replies(self, since: dt.date, imap_factory=None) -> list[email.message.Message]:
        if not (self.imap_host or imap_factory):
            return []
        m = imap_factory() if imap_factory else imaplib.IMAP4_SSL(self.imap_host, 993)
        try:
            m.login(self.user, self.password)
            m.select("INBOX", readonly=True)
            typ, ids = m.search(None, "SINCE", since.strftime("%d-%b-%Y"))
            out = []
            for i in (ids[0].split() if ids and ids[0] else [])[-500:]:
                typ, data = m.fetch(i, "(RFC822)")
                if data and data[0]:
                    out.append(email.message_from_bytes(data[0][1]))
            return out
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass


def _decode(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:  # noqa: BLE001
        return s or ""


def _html_text(html: str) -> str:
    """The text of an HTML mail body, one line per paragraph / line break (for replies sent as HTML only)."""
    import html as _h

    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", html or "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|blockquote|table)\s*>", "\n", s)
    s = _h.unescape(re.sub(r"(?s)<[^>]+>", "", s))
    return "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.splitlines())


def _bodies(msg: email.message.Message, ctype: str) -> list[str]:
    out = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() == ctype and not p.get_filename():
            try:
                raw = p.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001
                continue
            try:
                out.append(raw.decode(p.get_content_charset() or "utf-8", "replace"))
            except LookupError:     # charset=unknown-8bit and other names Python doesn't know: read it as UTF-8
                out.append(raw.decode("utf-8", "replace"))
    return out


def _text(msg: email.message.Message) -> str:
    """The message's plain text; its HTML turned to text when it has no plain-text part."""
    parts = _bodies(msg, "text/plain")
    return "\n".join(parts) if parts else "\n".join(_html_text(h) for h in _bodies(msg, "text/html"))


# ----------------------------------------------------------------------------- reading a reply
# Nov 19, 2026 (Wadley): a city employee sent a zoning review letter for a developer to the ORR-wadley thread, with a
# 512x512 logo as its only attachment. Any .png counted as the Code (status fulfilled, the logo saved to
# imports/ordinances/ as Wadley's code, where the withdraw step and the ordinances build read it), and the letter's
# "$30" opened a cost notice. Replies are now read from their own text, attachments by what they are. Saving a file
# that is not the Code is the costly mistake (it closes the request, counts the place as having its code and goes into
# the build), so a file that is in doubt is held and the owner told.
DOC_TYPES = (".pdf", ".doc", ".docx", ".rtf", ".odt", ".txt", ".md", ".htm", ".html", ".zip")
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".heic", ".heif", ".webp", ".bmp")
_CT_EXT = {"application/pdf": ".pdf", "application/msword": ".doc", "application/rtf": ".rtf", "text/rtf": ".rtf",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
           "application/vnd.oasis.opendocument.text": ".odt", "application/zip": ".zip",
           "application/x-zip-compressed": ".zip", "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
           "image/tiff": ".tiff", "image/heic": ".heic"}
HELD_DIR = Path("data") / "openrecords" / "attachments"   # neither the ordinances build nor `have` reads it
PAGE_IMAGES = 3            # this many page-sized images, with a reply saying the code or its pages are attached, count
PAGE_PX = 800              # a page scan or photo: short side at least this, and not about square. The signature images
ICON_PX = 200              # seen (Nov 2026) run 32x32 to 535x540, one seal 2480x2619; under ICON_PX it is an icon
SHORT_REPLY = 200          # words: a reply this short is read whole for what it is about

# where the quoted original (our letter, or the thread before) starts in a reply
_QUOTE = re.compile(r"^[ \t]*(?:>|On\b[^\n]{0,250}(?:\n[^\n]{0,120})?\bwrote:|-{2,}[ \t]*(?:Original|Forwarded)[ \t]+"
                    r"Message[ \t]*-{2,}|Begin forwarded message:|_{8,}[ \t]*\n[ \t]*From:|(?P<hdr>From:[^\n]*\n"
                    r"(?:[^\n]*\n){0,4}?[ \t]*(?:Sent|Date|To|Subject):)|To the Records Custodian\b)", re.I | re.M)
_NOISE_LINE = re.compile(r"^[ \t]*(?:\[?(?:CAUTION|EXTERNAL(?: SENDER| EMAIL)?|WARNING)\b[^\n]*|[^\n]*\boriginated "
                         r"from outside\b[^\n]*|\[Some people who received this message[^\n]*)$", re.I | re.M)
_FOOTER = re.compile(r"^[ \t*]*(?:CONFIDENTIALITY (?:NOTICE|STATEMENT)|DISCLAIMER|PUBLIC RECORDS NOTICE|This "
                     r"(?:e-?mail|message|communication)\b[^\n]{0,80}\b(?:intended|confidential|privileged)|"
                     r"Sent from my (?:iPhone|iPad|Android))", re.I | re.M)
_GREETING = re.compile(r"^(?:(?:good\s+(?:morning|afternoon|evening|day)|hello|hi|hey|greetings|dear)\b[^\n]{0,60}|"
                       r"[^\n]{1,60}[,:])$", re.I)
# what our request is about, named specifically: counts anywhere in the reply's own text
ABOUT_SPECIFIC = re.compile(r"code\s+of\s+ordinances|codified\s+ordinances|codes?\s+(?:and|&)\s+ordinances|"
                            r"municipal\s+code|\bmunicode\b|amlegal|american\s+legal|ecode360|general\s*code\b|"
                            r"code\s*publishing|encodeplus|open[\s-]+records?|records?\s+requests?|(?-i:\bORR\b)|"
                            r"50-18-7\d", re.I)
# "your request", "your records request" (not "your request to rezone" / "for a variance"): our request, by the reply
_YOUR_REQUEST = re.compile(r"\byour\s+(?:open\s+records\s+|records\s+|orr\s+|recent\s+)?request\b(?!\s+(?:to|for)\b(?!"
                           r"\s+(?:(?:the|a|an|our|a\s+copy\s+of)\s+)?(?:copies|copy|records?|codes?|ordinances?|"
                           r"public|information|charter|current|complete|electronic)\b))", re.I)
# the words of a reply to a records request: count in its first lines, or anywhere in a short reply
_REPLY_WORDS = (r"codes?|ordinances?|charter|resolutions?|requests?|requested|requesting|records?|ask(?:ed|ing)\s+for|"
                r"web\s*site|on\s*line|download\w*|links?|digiti[sz]\w*|costs?|fees?|charges?|estimated?|invoice|pay|"
                r"payment|amount|per\s+page|acknowledge\w*|your\s+(?:letter|e-?mail|message|inquiry)|working\s+on\s+"
                r"(?:it|this|that|your)|get\s+back\s+to\s+you|(?:get|have|send)\s+(?:it|this|that|them)\s+(?:to|for)\s+"
                r"you|will\s+(?:send|forward|e-?mail|provide|mail)")
# words any message with a file uses ("Attached is a copy of the signed lease", "Here is the information for Monday's
# work session", "Scanned the documents you dropped off"): about the request only when the reply names no other matter
# (Nov 19 review: they made misdirected staff mail "about" the request, and its file the Code)
_GENERIC_WORDS = r"cop(?:y|ies)|documents?|information|attach\w*|scan\w*"
REPLY_WORDS = re.compile(rf"\b(?:{_REPLY_WORDS})\b|\$\s?\d", re.I)
ABOUT_WORDS = re.compile(rf"\b(?:{_REPLY_WORDS}|{_GENERIC_WORDS})\b|\$\s?\d", re.I)
# the reply names our request: then a file with a generic name ("O-2026-21_001.pdf", Perry, Nov 10) is what was
# asked for
_ASKED = re.compile(r"\b(?:requested|you\s+(?:asked|wanted)\s+for|you\s+requested|as\s+(?:you\s+)?requested|"
                    r"per\s+your\s+request)\b", re.I)
_PLEASANTRY = re.compile(r"^(?:thanks?\b|thank\s+you|my\s+pleasure|you'?re\s+(?:very\s+)?welcome|no\s+problem|"
                         r"received\b|got\s+it)", re.I)
_CODE_WORDS = re.compile(r"\b(?:codes?|codif\w*|ordinances?|charter|resolutions?)\b", re.I)
# codes that are not the Code of Ordinances ("code enforcement", "the building code", "zip code"): masked before
# any code word is counted (Nov 19 review: "Here are the code enforcement photos for 412 Elm St" read as the Code)
_OTHER_CODE = re.compile(
    r"\b(?:codes?|ordinances?)[\s-]+(?:enforcement|compliance|violations?|officers?|inspectors?|inspections?|cases?|"
    r"complaints?|citations?)\b|\b(?:building|fire|zip|postal|area|dress|promo(?:tional)?|access|qr|bar|tax|penal|"
    r"plumbing|electrical|mechanical|energy|residential|international|life[\s-]+safety|health|state|source|security|"
    r"verification|discount|coupon|color|country|account|billing|tracking|confirmation|reference|error|status|"
    r"property\s+maintenance|radio|alarm|gate|door|lock|keypad|signal|ten|10|dispatch|call|incident|offense|"
    r"vendor|item|product|fund|budget|payroll|job|ledger|gl|object|naics|sic)\s+codes?\b|"
    r"\bcodes?\s+of\s+(?:conduct|ethics|georgia|federal\s+regulations)\b|\bcode[\s-]*red\b|"
    # a business's, a club's or a school's charter (fifth Nov 19 review: "Charter Communications Franchise
    # Agreement.pdf" with "Please sign where highlighted", and "Rotary Club Charter.pdf", were saved as the Code)
    r"\bcharter(?:s|ed)?[\s-]+(?:communications?|spectrum|cable|tv|internet|broadband|bank|schools?|academy|"
    r"bus(?:es)?|flights?|boats?|fishing|planes?|jets?|airlines?|aviation|trips?|cruises?|members?|membership|night)\b|"
    r"\b(?:club|rotary|kiwanis|lions|civitan|optimist|exchange|lodge|troop|scouts?|league|association|sorority|"
    r"fraternity|school|academy|business|company|corporate|bus|flight|boat|fishing|airline|yacht)(?:'s)?"
    r"(?:[\s-]+(?:no\.?\s*|#\s*)?\d+)?[\s-]+charters?\b|"
    # a draft is not the adopted code (third Nov 19 review: "attached is the draft ordinance amending Chapter 14 ... for
    # Council's review" was saved as the Code), nor a model or a sample (fourth: "the GMA model ordinance on
    # short-term rentals", "the sample ordinance from our attorney")
    r"\b(?:draft|proposed|model|sample|template|example)\s+(?:[\w-]+\s+){0,3}?(?:codes?|ordinances?|resolutions?|"
    r"charter)\b", re.I)
# a staff member's paper for the council, a board or a colleague ("for Council's review on Monday", "before the work
# session", "Please send me any changes by Friday"): another matter's, whatever it is
_STAFF = re.compile(r"\bfor\s+(?:the\s+)?(?:mayor\s+and\s+)?(?:council|commission|commissioners|board|mayor|manager|"
                    r"committee)(?:'s|s'|’s)?\s+(?:review|consideration|approval|input|comments?)\b|\bwork\s+session\b|"
                    r"\bsend\s+me\s+(?:any|your)\s+(?:changes|edits|comments|corrections|revisions|feedback|thoughts)\b|"
                    r"\b(?:review|sign)\s+and\s+return\b", re.I)
# "for your review": a clerk may say it of the code too, so it only puts a file with a name that does not plainly
# name the code in doubt
_FOR_REVIEW = re.compile(r"\bfor\s+your\s+(?:review|approval|signature|consideration|comments?|edits)\b|"
                         r"\bplease\s+(?:sign|initial|execute|approve)\b", re.I)
_DRAFT_NAME = re.compile(r"\b(?:draft|proposed|redline[ds]?|red\s?lined?|markup|marked\s+up|model|sample|template|"
                         r"example|(?:1st|2nd|3rd|first|second|third|final)\s+reading|"
                         # never adopted, or no longer in force (fifth Nov 19 review: "Failed Ordinances 2026.pdf")
                         r"failed|defeated|vetoed|repealed|rescinded|replaced|superseded|tabled|denied|rejected|"
                         r"voted\s+down|old|previous|former|obsolete|outdated)\b", re.I)
# an ordinance not yet adopted, said of what is sent ("the ordinances for first reading at Tuesday's council meeting",
# "the ordinances the Council will consider Monday night"): fourth Nov 19 review, both saved as the Code
_NOT_ADOPTED = re.compile(r"\b(?:1st|2nd|3rd|first|second|third|final)\s+reading\b|\bwill\s+(?:consider|review|discuss|"
                          r"vote\s+on|hear|take\s+up|adopt|act\s+on)\b|\bto\s+be\s+(?:considered|adopted|voted|discussed|"
                          r"presented|heard|approved)\b|\bpublic\s+hearing\b|\bup\s+for\s+(?:adoption|approval|a\s+vote|"
                          r"consideration|discussion)\b|\bpending\s+(?:adoption|approval)\b|\bunder\s+consideration\b|"
                          r"\bfor\s+(?:adoption|approval|consideration)\b|\bagenda\b", re.I)
# an ordinance that was never adopted or is no longer in force, said in the sentence that sends it ("the ordinances
# that failed", "the ordinance the council voted down last night", "the 1998 code, which was replaced in 2015"; an
# "old", "previous" or "former" code is another word in its noun phrase): fifth Nov 19 review, all saved as the Code
_NOT_IN_FORCE = re.compile(r"\b(?:failed|defeated|vetoed|repealed|rescinded|replaced|superseded|supplanted|tabled|"
                           r"rejected|voted\s+(?:(?:it|them|this|that)\s+)?(?:down|against)|struck\s+down|did(?:\s+not|n't|n’t)\s+pass|"
                           r"never\s+(?:been\s+)?(?:adopted|passed|approved|enacted)|(?:was|were|has|have|had)\s+"
                           r"not\s+(?:been\s+)?(?:adopted|passed|approved|enacted)|no\s+longer\s+(?:in\s+(?:effect|"
                           r"force)|used|current|valid)|withdrawn|denied)\b", re.I)
# a staff paper, a draft or an ordinance not yet adopted, said anywhere in the reply (fifth Nov 19 review: "Here is
# the new ordinance. Can you put it on the December agenda?", "Attached is the ordinance. This is still a draft.",
# "Let me know what you think.", "The mayor still needs to sign it before it takes effect." were saved as the Code:
# the sign was in another sentence than the one that sends it). Then nothing is the Code by the reply's words, and a
# file by its name only when the name plainly names the code (_names_code_plainly)
_PENDING = re.compile("|".join(x.pattern for x in (_STAFF, _FOR_REVIEW, _NOT_ADOPTED)) + "|"
                      r"\b(?:drafts?|proposed|redline[ds]?|red-?lined?|marked\s+up|markup)\b|\bplease\s+review\b|"
                      r"\blet\s+me\s+know\s+(?:what\s+you\s+think|if\s+(?:you\s+have\s+|there\s+are\s+)?"
                      r"(?:any\s+)?(?:changes|edits|comments|corrections|revisions|suggestions|thoughts|feedback|"
                      r"concerns))\b|\bbefore\s+(?:it\s+goes\s+to\s+)?(?:the\s+)?(?:[\w'’]+\s+)?(?:council|"
                      r"commission|board|meeting|vote)\b|\bgo(?:es)?\s+to\s+(?:the\s+)?(?:council|commission|board)\b|"
                      r"\b(?:needs?|has|have)\s+to\s+(?:be\s+)?sign(?:ed)?\b|(?:\bnot|n't|n’t)\s+(?:yet\s+)?"
                      r"(?:been\s+)?signed\b|\bunsigned\b|\b(?:amended|revised|edited|changed|marked\s+up)\s+by\s+"
                      r"(?:the\s+|our\s+)?(?:city\s+|county\s+)?(?:attorney|lawyer|counsel|engineer|planner|manager|"
                      r"administrator|staff|committee)\b|\b(?:attorney|lawyer|counsel|engineer|planner)(?:'s|s'|’s)\s+"
                      r"(?:changes|edits|comments|revisions|markup|notes)\b|\b(?:changes|edits|comments|revisions|"
                      r"markup)\s+from\s+(?:the\s+|our\s+)?(?:city\s+|county\s+)?(?:attorney|lawyer|legal|counsel|"
                      r"engineer|planner|staff|manager)\b|\b(?:look|read)\s+(?:it|this|them|these|that)\s+over\b|"
                      r"\btake\s+a\s+look\b|\byour\s+(?:thoughts|input|feedback|edits)\b|\b(?:have|make|suggest)\s+"
                      r"any\s+(?:changes|edits|corrections|revisions)\b|\bwill\s+be\s+(?:considered|adopted|voted|"
                      r"heard|presented|discussed|approved|signed)\b|\b(?:are|is|we're)\s+(?:adopting|considering|"
                      r"voting\s+on|reviewing)\b|\b(?:next|upcoming|tonight's|tomorrow's)\s+(?:[\w'’]+\s+)?meeting\b|"
                      r"\bnot\s+(?:yet\s+)?(?:been\s+)?(?:adopted|approved|passed|final|effective|in\s+effect)\b|"
                      r"\bonce\s+(?:the\s+)?(?:mayor|chair\w*|council|commission|board)\s+(?:signs|adopts|approves|"
                      r"passes|votes)\b|\b(?:working\s+(?:copy|draft|version)|work\s+in\s+progress|preliminary)\b",
                      re.I)
# a conversation we never had (the project only writes): "Per our conversation", "the ordinance we talked about"
_CONVERSATION = re.compile(r"\b(?:we|you|i)\s+(?:talked|spoke|discussed|chatted)\b|\bas\s+(?:we\s+)?discussed\b|"
                           r"\b(?:per|following|after|from)\s+(?:our|your|the|this\s+morning's|today's)\s+(?:conversation|"
                           r"discussion|call|phone\s+call|meeting|visit|chat)\b|\bwhen\s+we\s+(?:met|spoke|talked)\b|"
                           # fifth Nov 19 review: "the code book I mentioned on the phone", "the ordinance I mentioned
                           # when you called yesterday"
                           r"\b(?:i|we)\s+mentioned\b|\bwhen\s+you\s+(?:called|came\s+(?:by|in)|stopped\s+by|were\s+"
                           r"(?:here|in))\b|\b(?:on|over)\s+the\s+phone\b|\byou\s+asked\s+(?:me\s+)?about\b", re.I)
# the reply is another document, for another matter ("Here's the complete letter", "the site plan for 412 Elm St");
# "your letter" is ours
_OTHER_DOC = re.compile(r"(?<!\byour\s)(?<!\bmy\s)\bletters?\b|\b(?:agendas?|(?:the|meeting|council|commission|board)"
                        r"\s+minutes|plats?|surveys?|permits?|contracts?|proposals?|bids?|flyers?|newsletters?|"
                        r"site\s+plans?|packets?|citations?|reports?|deeds?|easements?|variances?|rezonings?|"
                        r"subdivisions?)\b", re.I)
# another matter named: a street address, a parcel, a lot, a case
_MATTER = re.compile(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][A-Za-z.]*\s+){1,3}(?:St|Street|Rd|Road|Ave|Avenue|Blvd|"
                     r"Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Hwy|Highway|Way|Pl|Place|Pkwy|Parkway|Cir|Circle)\b\.?|"
                     r"\bparcels?\b|\blot\s+#?\d|\bcase\s+(?:no\.?|number|#)", re.I)
# where to come or send something (the office's own address), not another matter: "pick up a printed copy at City
# Hall, 101 N Main Street", "Payment can be mailed to 101 N Main St"; and parcel data in general ("For parcel data
# please see qPublic"). Third Nov 19 review: both read a real reply as "not about the request"
_PLACE_OF = re.compile(r"(?:\b(?:city|town|village)\s+hall|\bcourthouse|\bannex|\b(?:our|the|my)\s+(?:main\s+)?"
                       r"(?:office|building)|\b(?:drop\w*\s+(?:\w+\s+)?off|pick\w*\s+(?:\w+\s+)?up|come\s+by|stop\s+by|"
                       r"located|payable|remit\w*|on\s+file|kept|stored|(?:payments?|checks?)\s+(?:\w+\s+){0,3}?mail\w*)\b"
                       r"[^.;\n]{0,40}?\b(?:at|to))"
                       r"[\s,]*(?:(?:located\s+)?at\s+)?$", re.I)
_PARCEL_DATA = re.compile(r"\s+(?:data|maps?|search\w*|viewer|look-?ups?|layers?|gis)\b", re.I)
_SUBJECT_LINE = re.compile(r"^[ \t]*(?:subject|re)[ \t]*:[ \t]*(\S[^\n]*)$", re.I | re.M)
# a pasted "Re:" line that names our request: "RE: Request dated 11/10/2026", "Re: Your letter", "RE: Records"
_RE_OURS = re.compile(r"^(?:(?:your|our|the|this|records?|public\s+records|information|document)\s+)?(?:request|"
                      r"records?\b)", re.I)
_RE_OURS_STRONG = re.compile(r"\[ORR-|\byour\s+(?:letter|e-?mail|message|inquiry)\b", re.I)
# file names: a name names the code when it says code(s), codified, ordinance(s), charter, Municode, the enabling
# legislation (a county's charter) or an ordinance number ("Ord 2024-03", "O-2026-21_001.pdf"): not a resolution, a
# title, a section, a chapter, an article, an appendix or a supplement (fourth Nov 19 review: "Title VI Plan
# 2026.pdf", "Section 8 Inspections Nov 2026.pdf", "Comp Plan Chapter 3.pdf", "Herald Article Nov 2026.pdf", "SPLOST
# Legislation 2026.pdf" and "Resolution 2026-14 LMIG.pdf" were saved as the Code on their names alone). A name for a
# letter, an invoice or another matter, with no code word, is not the code ("ORR Response Letter 11-18-2026.pdf")
# Municode alone is no code ("Municode Recodification Proposal.pdf"), and "P.O. 2026-114.pdf" (a purchase order) or
# "C.O. 12-3" no ordinance number (fifth Nov 19 review: both saved as the Code)
_CODE_NAME = re.compile(r"\b(?:codes?|codif\w*|ordinances?|charters?|enabling\s+(?:legislation|acts?)|"
                        r"ord\s+(?:no\s+)?\d+|(?<!\b[a-z]\s)(?<!\b[a-z]\s\s)o\s*\d{2,4}\s+\d{1,4})\b", re.I)
# only words a records office's own papers, or another matter's, are named by: an ordinance's title often lacks the
# word "ordinance" (Flowery Branch, Nov 10: "788_Establish_Compliance_with_Senate_Bill_447.pdf",
# "789_New_Digital_Review_Process_in_Planning.pdf"), so subjects such as a bill, a review or a permit are not here.
# The clerk's response, cover letter or transmittal is sometimes one PDF with the code in it ("Open Records
# Response.pdf" with "attached is the Code of Ordinances"); a bill or another matter's paper never is.
_PAPER_NAME = r"letters?|response|responses|acknowledg\w*|notices?|notifications?|memo\w*|cover|transmittal"
_NEVER_CODE_NAME = re.compile(r"\b(?:invoices?|estimates?|quotes?|quotations?|receipts?|billing|statements?|forms?|"
                              r"w-?9|payments?|denial|parcels?|plats?|surveys?|site\s+plans?|agendas?|minutes|deeds?|"
                              r"rezoning|photos?|packets?|drafts?|proposed|proposals?)\b", re.I)
_NOT_CODE_NAME = re.compile(rf"\b(?:{_PAPER_NAME})\b|" + _NEVER_CODE_NAME.pattern, re.I)
# "attached is our code", "here are the ordinances", "I scanned the code": the code is what is sent
_VERB_ATTACH = r"attach\w*|enclos\w*|scann?(?:ed|ing)?|scans"
_VERB_NOW = _VERB_ATTACH + r"|here(?:'s|\s+is|\s+are)|please\s+find|find\s+(?:below|herewith)|includ(?:ed|ing)|" \
                           r"uploaded|forward(?:ed|ing)"
_VERB_ANY = _VERB_NOW + r"|send\w*|sent|mail(?:ed|ing)?|e-?mail(?:ed|ing)|provid\w*|deliver\w*|get\s+(?:it|this|" \
                        r"them|that)?\s*to\s+you"
_NOT_SENT = re.compile(r"\b(?:links?|url|web\s*(?:site|page)|address|location)\b|" + _OTHER_DOC.pattern, re.I)
_AND_END = re.compile(r"(?:\band|&|\bplus|\balong\s+with|\bas\s+well\s+as|\btogether\s+with)\s+(?:(?:the|our|a|"
                      r"an|its|their|this|that|these|all|of|current|complete|full|entire|updated|latest|new|copy|copies|"
                      r"city|county|town|[a-z]+'s)\s+){0,4}$", re.I)
# a code word that is only the modifier of another thing: "the zoning ordinance review letter", "a code question"
_MODIFIES = re.compile(r"^\s+(?:[a-z]+\s+)?(?:letters?|reviews?|notices?|photos?|pictures?|violations?|citations?|"
                       r"hearings?|variances?|cases?|complaints?|applications?|permits?|questions?|issues?|sections?|"
                       r"sheets?|schools?)\b", re.I)
_CODE_HOST = re.compile(r"(?:^|\.)(?:municode\.com|amlegal\.com|ecode360\.com|codepublishing\.com|generalcode\.com|"
                        r"encodeplus\.com|iccsafe\.org|1drv\.ms|onedrive\.live\.com|sharepoint\.com|dropbox\.com|"
                        r"drive\.google\.com|docs\.google\.com|box\.com|wetransfer\.com)$", re.I)
_ONLINE = re.compile(r"\b(?:municode|american\s+legal|amlegal|ecode360|general\s+code|on\s*line|(?:our|the|its)\s+"
                     r"(?:\w+(?:'s)?\s+){0,2}?web\s*(?:site|page)|available\s+(?:for|to)\s+download)\b", re.I)
_NOT_ONLINE = re.compile(r"\b(?:not|n't|no\s+longer)\s+(?:currently\s+|yet\s+)?(?:available\s+)?(?:on\s*line|on\s+"
                         r"(?:our|the)\s+web)", re.I)
_DENIED = re.compile(r"\b(?:denied|deny|denying|exempt|no responsive records|do not have|does not have|don't have)\b",
                     re.I)
_AUTO_SUBJECT = re.compile(r"^\s*(?:automatic reply|auto(?:matic)?[- ]?(?:reply|response)|out of (?:the )?office)\b",
                           re.I)
# an automatic reply that says the person is away ("I will respond to your request when I return" is not an answer)
_AWAY = re.compile(r"\bout\s+of\s+(?:the\s+)?office\b|\bon\s+(?:vacation|annual\s+leave|leave|holiday)\b|\baway\s+from"
                   r"\s+(?:the|my)\s+(?:office|desk)\b|\blimited\s+access\s+to\s+(?:my\s+)?e-?mail\b|\bwhen\s+I\s+"
                   r"return\b|\bupon\s+my\s+return\b|\bno\s+longer\s+(?:with|employed)\b", re.I)
_ICON_NAME = re.compile(r"(?:^|[\W_\d])(?:logo|icon|facebook|fb|twitter|instagram|insta|linkedin|youtube|bluesky|"
                        r"threads\w*|tiktok|nextdoor|pinterest|social|seal|emblem|badge|banner|signature|sig|"
                        r"outlook-\w+)(?:[\W_\d]|$)", re.I)
# where the signature starts: a sign-off line ("Thank you,", "*Thanks,*", "Regards,", "Have a great day!")
_SIGN_OFF = re.compile(r"^[*_\s]*(?:thanks?(?:\s+(?:so\s+much|again|you))?|thank\s+you(?:\s+(?:so\s+much|again|kindly|"
                       r"very\s+much))?|(?:best|kind|kindest|warm|warmest|many|with)?\s*regards|best(?:\s+wishes)?|"
                       r"all\s+the\s+best|sincerely(?:\s+yours)?|yours\s+(?:truly|sincerely)|respectfully(?:\s+(?:yours|"
                       r"submitted))?|cordially|cheers|blessings|have\s+a\s+(?:great|good|nice|blessed|wonderful)\s+"
                       r"(?:day|weekend|week|evening|one)(?:\s+ahead)?|-- ?)[*_\s,.!]*$", re.I)
# a signature line with no sign-off above it: a phone number, an address, a link, or a short line of names and titles
_SIG_INFO = re.compile(r"@|https?://|\bwww\.|\.(?:gov|com|org|net|us)\b|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b|"
                       r"^[ \t]*(?:[PFTMCO]|ph|tel|fax|cell|office|direct|phone)[ \t]*[:.]", re.I)
_SIG_SMALL = {"of", "and", "the", "for", "to", "at", "in", "de", "&", "|"}
# a bill-pay line in a signature ("Online bill payment is now available", "Pay your water bill online") is not a cost
_BILL_PAY = re.compile(r"\bpay\s+(?:your\s+)?(?:\w+\s+){0,2}bills?\b|\b(?:utility|water|sewer|tax)\s+(?:bill\s+)?"
                       r"pay(?:ments?)?\b|\bbill\s+pay(?:ments?)?\b", re.I)
# salutations, and the people a greeting may name besides the one it is to
_SALUTE = frozenset("good morning afternoon evening day hello hi hey greetings dear sir sirs madam requester requestor "
                    "all everyone team folks there to whom it may concern mr mrs ms miss dr and or".split())
# capitalized words of a greeting line that name no one ("As promised,", "Good morning and Happy Friday,", "Yes sir,")
_NOT_A_NAME = _SALUTE | frozenset("again you y'all yall ladies gentlemen friends howdy morning afternoon evening happy "
                                  "merry hope as per following re regarding yes no ok okay sure absolutely certainly "
                                  "will do thanks thank fyi note update just also this that i we our the a "
                                  "monday tuesday wednesday thursday friday saturday sunday holidays christmas new year "
                                  "thanksgiving promised discussed below above".split())
_INLINE_GREETING = re.compile(r"(?i)(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening))\s+([^,:!\n]{1,40})[,:!-]")
# a closing to someone: "Thanks Tom!", "Thank you Mrs. Johnson." (not the sign-off "Thanks, Jane" or "Thanks Jane")
_THANKS_NAME = re.compile(r"(?i:thanks|thank\s+you)\s+([A-Z][a-z'’-]+\.?(?:\s+[A-Z][a-z'’-]+)?)\s*[!.]$")
NOT_ABOUT = "not about this request"
CLOSED = ("fulfilled", "withdrawn", "denied")
ACK_FROM = ("planned", "sent", "followed_up", "error", "bounced", "acknowledged")


def _real_text(s: str) -> bool:
    """Whether s holds a sentence (not just a title such as MEMORANDUM, a letterhead or a greeting)."""
    for ln in _NOISE_LINE.sub("", s).splitlines():
        w = re.findall(r"[A-Za-z0-9']+", ln)
        if len(w) >= 8 or (len(w) >= 2 and re.search(r"[.!?]\s*$", ln.strip())):
            return True
    return False


def own_text(body: str, ours=()) -> str:
    """What the sender wrote: the reply above the quoted original (our letter, or the thread before it), without
    external-sender banners or a confidentiality footer. A From:/To:/Date: block with nothing written above it is the
    sender's own memo header, not a quote, unless it is ours (from our address or name, our [ORR-] subject) or
    Outlook's reply header (Sent:)."""
    t = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    ours = [o.lower() for o in ours if o and len(o) > 3]
    for m in _QUOTE.finditer(t):
        if m.group("hdr") and not _real_text(t[:m.start()]):
            block = t[m.start():m.start() + 600].split("\n\n")[0]
            first = block.split("\n", 1)[0].lower()
            if not ("[orr-" in block.lower() or re.search(r"^[ \t]*Sent:", block, re.I | re.M) or
                    any(o in first for o in ours)):
                continue
        t = t[:m.start()]
        break
    t = _NOISE_LINE.sub("", t)
    f = _FOOTER.search(t)
    if f and t[:f.start()].strip():
        t = t[:f.start()]
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _mask(s: str) -> str:
    """s with the codes that are not the Code of Ordinances ("code enforcement", "building code") blanked out."""
    return _OTHER_CODE.sub(" ~ ", s or "")


def _is_greeting(line: str) -> bool:
    return bool(_GREETING.match(line)) and len(line.split()) <= 6 and \
        not (ABOUT_SPECIFIC.search(line) or ABOUT_WORDS.search(_mask(line)))


def _head(own: str) -> str:
    """The reply's first two lines after the greeting: where a reply says what it is."""
    lines = [ln.strip() for ln in own.splitlines() if ln.strip()]
    while lines and _is_greeting(lines[0]):
        lines.pop(0)
    return " ".join(lines[:2])[:300]


def _sig_line(line: str) -> bool:
    """A line of a signature: a phone number, an address, a link, or a few words of names and titles ("Jane Doe, CMC",
    "City Clerk", "101 N Main Street | Millen, GA 30477")."""
    w = re.findall(r"[A-Za-z][A-Za-z.'&-]*|\|", line)
    if _MONEY.search(line):
        return False                # "The fee of $25 can be paid at www.millenga.gov/pay" is the reply
    if _SIG_INFO.search(line):
        return len(w) <= 10
    return 0 < len(w) <= 8 and not re.search(r"[.!?]\s*$", line) and \
        all(x[0].isupper() or x.lower() in _SIG_SMALL for x in w)


def _parts(own: str) -> tuple[list[str], list[str]]:
    """The reply's own text as (greeting lines, body lines), the body without the sign-off and the signature below it,
    or, with no sign-off, without the trailing lines of names, titles, addresses and phone numbers."""
    lines = [ln.strip() for ln in (own or "").splitlines() if ln.strip()]
    greeting = []
    while lines and _is_greeting(lines[0]) and not _SIGN_OFF.match(lines[0]):   # "Thank you," is no greeting
        greeting.append(lines.pop(0))
    for i, ln in enumerate(lines):
        # "Thanks!" first, then the message, is not a sign-off: at the top it is one only with a signature under it
        if _SIGN_OFF.match(ln) and (i > 0 or all(_sig_line(x) for x in lines[1:])):
            lines = lines[:i]
            break
    while len(lines) > 1 and _sig_line(lines[-1]):
        lines.pop()
    return greeting, lines


def _unsigned(own: str) -> str:
    """The reply's own text without its sign-off and signature (where a bill-pay line or a street address lives)."""
    greeting, body = _parts(own)
    return "\n".join(greeting + body)


def _addressee(own: str, ours=()) -> str:
    """Whom the reply's greeting names when it is someone other than us (our sender, requester or organization):
    "Mayor", "Tom" ("Hi Tom,"), "Mrs. Johnson"; '' for "Good morning,", "Dear Requestor,", "Mr. Electo," (ours), "As
    promised," or no greeting. A note meant for them is not an answer to us, and nothing in it is our Code (third
    Nov 19 review: "Hi Mrs. Johnson, Attached are the records you requested." was saved as the Code)."""
    greeting, body = _parts(own)
    inline = _INLINE_GREETING.match(body[0]) if body else None
    mine = {w for o in ours for w in re.findall(r"[a-z]+", (o or "").lower()) if len(w) > 2}
    for g in greeting + ([inline.group(0)] if inline else []):
        if ABOUT_SPECIFIC.search(g) or REPLY_WORDS.search(_mask(g)):
            continue                    # "Dear Open Records Requester,"
        words = re.findall(r"[A-Za-z][A-Za-z'’-]*", g)
        lone = re.fullmatch(r"(?i)\s*(?:hi|hello|hey|dear)\s+([a-z][a-z'-]*)\s*[,:!-]?\s*", g)   # "hi tom,"
        names = [w for w in words if w.lower() not in _NOT_A_NAME and (w[0].isupper() or lone)]
        if names and not any(n.lower() in mine or (len(n) > 3 and any(n.lower() in m for m in mine)) for n in names):
            return re.sub(r"(?i)^\s*(?:good\s+(?:morning|afternoon|evening|day)|hello|hi|hey|greetings|dear|to)\b"
                          r"[\s,]*", "", g).strip(" ,:;!-")[:60] or g.strip()[:60]
    for ln in (own or "").splitlines():    # a closing "Thanks Tom!" names them too (fifth Nov 19 review)
        c = _THANKS_NAME.match(ln.strip())
        names = [w for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", c.group(1)) if w.lower() not in _NOT_A_NAME and
                 w.lower() not in ("so", "much", "very", "kindly", "lot", "bunch", "ton")] if c else []
        if names and not any(n.lower() in mine or (len(n) > 3 and any(n.lower() in m for m in mine)) for n in names):
            return c.group(1)
    return ""


_GROUP = frozenset("all everyone everybody team folks y'all yall guys".split())


def _group(own: str) -> str:
    """A greeting to a group ("Hi all,", "Good morning everyone,", "Team,"): staff mail, not a reply to us. Its files are
    held (fourth Nov 19 review: "Hi all, Attached is the resolution for Monday." was saved as the Code)."""
    greeting, body = _parts(own)
    inline = _INLINE_GREETING.match(body[0]) if body else None
    for g in greeting + ([inline.group(0)] if inline else []):
        words = re.findall(r"[a-z']+", g.lower().replace("’", "'"))
        hit = [w for w in words if w in _GROUP]
        if hit and (words[0] in _SALUTE or len(words) == len(hit)):
            return hit[0]
    return ""


# What a reply says it sends, read clause by clause (fourth Nov 19 review: a file was the Code on its name alone, or on
# a cover note's words, so misdirected staff mail with "Title VI Plan 2026.pdf" or "Scan_001.pdf" and "See attached."
# was saved as the Code). The clauses: "attached is X", "here are X", "please find X attached", "I have attached X",
# "X is enclosed", and a line that is only X ("The Code of Ordinances, as requested.", a subject "Code of Ordinances -
# Millen"). X is the code when it is a code noun (code, ordinances, charter, codification, the enabling legislation)
# with nothing beside it but determiners, the place's name and words such as current, complete, adopted, uncodified
# or "a copy of", and nothing after it says it is for another matter ("for first reading", "from our attorney", "for
# the board"). Any other word makes X another thing ("the Title VI Plan", "Sylvania's alcohol ordinance", "the code
# sheet for the new alarm system", "our open records request form is attached"); the clerk's own letter, response or
# notice is a paper; "the records", "the documents you requested", "it" name nothing; nor does a bare "the code" or
# "the codes" (a keypad's, a gate's): a code is "our code", the place's, the municipal code (_names_the_code).
_SAY_CODE = frozenset("code codes ordinance ordinances charter charters codification".split())
# "city", "county", "Georgia" are not here: a city's code is "the city code", a county's "the county code", and the
# Georgia code is the state's (fifth Nov 19 review: "the county code" for a city, "the city code" for a county and
# "the Georgia code" were each the Code). The place's own kind words are in _ok_words; Municode only qualifies a code
_SAY_ALLOWED = frozenset("""
the a an our my your its their this that these those all any both each every some few several
entire whole full complete current updated up-to-date latest newest recent most official adopted codified uncodified
municipal electronic digital requested revised new existing published printed local general unified consolidated
remaining additional following enabling other provided passed enacted amended approved municode
copy copies pdf pdfs version versions scan set sets of book books volume volumes vol part parts zip word
searchable folder export printout download
one two three four five six seven eight nine ten first second third fourth fifth final last next
""".split())
_CITY_WORDS = frozenset("city town village municipality".split())
_COUNTY_WORDS = frozenset("county co".split())
_STATE_WORDS = frozenset("georgia ga".split())
_SAY_NEUTRAL = frozenset("""
records record documents document docs doc files file information info attachment attachments items item materials
material it them this these those everything what same request requests openrecords publicrecords copy copies pdf pdfs
scan scans page pages part parts rest remainder one ones
""".split())
_SAY_PAGES = frozenset("pages page scans scan".split())
_SAY_PART = frozenset("part parts it them the a first second third fourth fifth next final last rest remainder one of "
                      "volume volumes vol".split())
_SAY_LINK = frozenset("link links url urls website websites site webpage hyperlink library".split())
_SAY_PAPER = frozenset("letter letters response responses reply acknowledgment acknowledgement acknowledgments "
                       "acknowledgements notice notices notification notifications memo memorandum cover "
                       "transmittal".split())
_SAY_DET = frozenset("the a an this these those our your my their its".split())
# a determiner after these goes on with the phrase ("copies of all our ordinances"); after any other word it starts a
# new one ("the ordinances the Council will consider")
_SAY_QUANT = frozenset("of all both each every any some several few most none one two three four five".split())
# words that end the noun phrase of what is sent ("the code you requested", "the ordinances adopted since ...", "the
# article from the Herald", "the records responsive to your request")
_SAY_END = frozenset("""
for from to about on regarding re concerning with by at in into per under since after before until through within via
including excluding except that which who whom whose where when while if as so because but though although
you we i they he she it's there here responsive pursuant related relating attached enclosed included scanned below
above is are was were will would has have had can could may might should shall be been being thanks thank please let
and & plus or , : ; -
""".split())
# after "for", "from", "on", "about", "regarding", "with": a phrase of only these words is ours ("for your files",
# "from Municode", "on file", "for the City of Millen", "with all amendments"); any other word is another matter ("for
# first reading", "for the board", "from our attorney", "on short-term rentals", "with the attorney's changes")
_SAY_BENIGN = frozenset("""
you your yours reference records record files file use information convenience review request requests openrecords
publicrecords the this that our a an of city county town village municipality municode website web site online library
office clerk clerk's electronic download purposes research project georgia ga american legal code codes ordinances
ordinance charter copy copies pdf all amendments amendment supplements supplement updates through latest current
index appendices appendix
""".split())
_SAY_CHECK = frozenset("for from on about regarding concerning re relating related with".split())
_SAY_NOT = frozenset("""not never cannot can't couldn't won't wouldn't don't didn't doesn't isn't aren't wasn't weren't
haven't hasn't unable will would shall should could can may might once if unless until when after before to no""".split())
_SAY_BE = frozenset("is are was were been 's".split())
_SAY_ADV = frozenset("now also all hereby herewith both each just here".split())
_SAY_SKIP = frozenset("is are was were please find see you you'll will also here herewith hereto herein below , : -".split())
_SAY_CLAUSE = frozenset("""i we you they he she it's there please let thank thanks if which that who as so but feel hope
have has is are will would can could should do does did i'm we're i'll we'll i've we've sorry""".split())
_SAY_STOP = frozenset(", ; : - but so while because although though if when see find please here here's".split())
_SAY_TOKEN = re.compile(r"~|[a-z0-9]+(?:['./-][a-z0-9]+)*|[,:;&]|(?<=\s)-(?=\s)")


class _Ok(frozenset):
    """Words a reply may use of what it sends besides _SAY_ALLOWED: the place's (`place`: its name and its own kind
    words) and our own names'."""
    place = frozenset()


def _ok_words(place: str = "", ours=()) -> _Ok:
    """The place's name and its own kind words (city, town for a city; county for a county; both for a consolidated
    government, "Athens-Clarke County"; both when the place is not known), and the words of our own names but the
    kind and state words ("Georgia Civic Records Project" makes no code the Georgia code)."""
    words = {w for w in re.findall(r"[a-z]+", (place or "").lower()) if len(w) > 1}
    county = "county" in words
    words |= (_COUNTY_WORDS if county or not words else set()) | \
        (_CITY_WORDS if not county or "-" in place or not words else set())
    mine = {w for o in ours for w in re.findall(r"[a-z]+", (o or "").lower()) if len(w) > 1}
    ok = _Ok(words | (mine - _CITY_WORDS - _COUNTY_WORDS - _STATE_WORDS))
    ok.place = frozenset(words)
    return ok


def _names_the_code(np: list[str], ok: frozenset) -> bool:
    """A noun phrase with a code noun names the Code: the ordinances, the charter, the codification, or a code that is
    "our code", the place's ("the city code", "Millen's code", "the City of Millen code"), the municipal code or the
    code book; not a bare "the code" or "the codes" (fifth Nov 19 review: "The new keypad at the water plant is
    installed. Attached is the code." and "Here are the codes. Please don't share them." were the Code)."""
    place = getattr(ok, "place", frozenset())
    return any((w in _SAY_CODE and w not in ("code", "codes")) or w in ("legislation", "act", "acts", "our",
                                                                          "municipal", "municode") or
               w.removesuffix("'s") in place or (w in ("book", "books") and k and np[k - 1] == "code")
               for k, w in enumerate(np))


class Said(tuple):
    """What a reply says it sends: code (the code, the charter or the ordinances), other (another thing: a plan, a
    form, an article, an ordinance for first reading), paper (the clerk's own letter, response or notice), pages
    (pages or scans, of what not said)."""
    __slots__ = ()

    def __new__(cls, code=False, other=False, paper=False, pages=False):
        return super().__new__(cls, (code, other, paper, pages))

    code = property(lambda s: s[0])
    other = property(lambda s: s[1])
    paper = property(lambda s: s[2])
    pages = property(lambda s: s[3])


def _say_np(toks: list[str]) -> tuple[list[str], list[str]]:
    """A noun phrase at the start of toks and what follows it: "the code you requested" -> (the code, you requested);
    "the ordinances the Council will consider" -> (the ordinances, ...)."""
    np = []
    for t in toks:
        if t in _SAY_END or (np and t in _SAY_DET and np[-1] not in _SAY_QUANT):
            break
        np.append(t)
    return np, toks[len(np):]


def _say_matter(post: list[str], ok: frozenset) -> bool:
    """What follows a noun phrase (or a verb: "See attached for Monday's meeting") says it is for another matter: a
    "for", "from", "on", "with" ... phrase with a word not in _SAY_BENIGN."""
    if not post or post[0] not in _SAY_CHECK:
        return False
    for w in post[1:7]:
        if w in (",", ";", "and", "&", "that", "which", "you", "we", "i", "as", "so", "but", "if", "-"):
            break
        base = w.removesuffix("'s")
        if not (w in _SAY_BENIGN or w in _SAY_ALLOWED or w in _SAY_CODE or base in ok or re.fullmatch(r"[\d.,/-]+", w)):
            return True
    return False


def _say_kind(toks: list[str], ok: frozenset, bad: bool) -> str:
    """What the noun phrase at the start of toks names: 'code', 'other', 'paper', 'pages', 'neutral', 'link', 'bare' (a
    bare "the code": _names_the_code) or ''.
    `ok`: words of the place's and our own names; `bad`: the sentence says the thing is not adopted, is for the
    council or names another matter."""
    np, post = _say_np(toks)
    if not np:
        return ""
    if any(w in _SAY_LINK for w in np):
        return "link"
    content, code = [], False
    place = getattr(ok, "place", frozenset())
    for k, w in enumerate(np):
        base = w.removesuffix("'s")
        if w in _SAY_CODE or (w in ("legislation", "act", "acts") and k and np[k - 1] == "enabling"):
            code = True
        elif not (w in _SAY_ALLOWED or w in _SAY_NEUTRAL or base in ok or base in _SAY_ALLOWED or
                  re.fullmatch(r"[\d.,/-]+", w) or (w in _STATE_WORDS and k and np[k - 1] in place)):
            content.append(w)           # "the City of Millen, Georgia Code of Ordinances" is the place's
    matter = bad or _say_matter(post, ok)
    if not (content or code or matter) and post and post[0] in ("from", "on"):
        k = next((n for n, w in enumerate(post[:5]) if w == "of"), 0)
        if k:                           # "a PDF export from Municode of our full code"
            return _say_kind(np + post[k:], ok, bad)
    if content:
        return "paper" if all(w in _SAY_PAPER for w in content) and not matter else "other"
    if code:
        return "other" if matter else "code" if _names_the_code(np, ok) else "bare"
    if any(w in _SAY_PAGES for w in np):
        return "other" if matter else "pages"
    return "neutral"


def _say_list(toks: list[str], ok: frozenset, bad: bool) -> list[str]:
    """The kinds of the things listed at the start of toks ("the Code of Ordinances and the Zoning Ordinance", "the
    code, the charter, and the zoning ordinance"), up to the first piece that is no noun phrase ("our code, sorry for
    the delay", "our code and let me know ...")."""
    pieces, cur, seps = [], [], []
    for t in toks:
        if t in (",", "and", "&", "plus", "or"):
            pieces.append(cur)
            seps.append(t)
            cur = []
        else:
            cur.append(t)
    pieces.append(cur)
    out = []
    for k, piece in enumerate(pieces):
        while piece and piece[0] in ("also", "both", "either", "then"):
            piece = piece[1:]
        if not piece:
            continue
        if k and (piece[0] in _SAY_CLAUSE or (seps[k - 1] == "," and not (
                piece[0] in _SAY_DET or piece[0] in _SAY_ALLOWED or piece[0] in _SAY_CODE))):
            break
        kind = _say_kind(piece, ok, bad)
        if not kind:
            break
        out.append(kind)
    return out


def _say_before(toks: list[str], end: int, plural: bool, ok: frozenset, bad: bool) -> list[str]:
    """The subject of "... is attached" (toks[end] is the be-verb): the last noun phrase of the clause before it, or,
    with a plural verb, every one listed ("The Code of Ordinances and the Charter are attached"). A clause that holds
    no noun phrase ("Our code, which was updated in 2019, is attached") gives way to the one before it."""
    for _ in range(3):
        start = end
        while start > 0 and toks[start - 1] not in _SAY_STOP:
            start -= 1
        pieces, cur = [], []
        for t in toks[start:end]:
            if t in ("and", "&", "plus"):
                pieces.append(cur)
                cur = []
            else:
                cur.append(t)
        pieces.append(cur)
        kinds = []
        for piece in pieces:
            while piece and piece[0] in ("also", "both", "either", "then"):
                piece = piece[1:]
            kind = "" if not piece or piece[0] in _SAY_CLAUSE else _say_kind(piece, ok, bad)
            if kind:
                kinds.append(kind)
        if kinds:
            return kinds if plural else kinds[-1:]
        if start == 0:
            break
        end = start - 1
    return []


def _say_object(toks: list[str], k: int, ok: frozenset, bad: bool) -> list[str]:
    """What an active verb at toks[k - 1] sends: "attached is our code", "attached please find our code", "attached
    for your review is the code", "attached to this e-mail are the ordinances"."""
    while k < len(toks):
        if toks[k] in _SAY_SKIP:
            k += 1
        elif toks[k] == "for" and k + 1 < len(toks) and toks[k + 1] == "your":
            k += 3
        elif toks[k] == "to" and k + 2 < len(toks) and toks[k + 1] in ("this", "the", "my", "your") and \
                toks[k + 2] in ("email", "e-mail", "message", "letter", "reply"):
            k += 3
        else:
            break
    if _say_matter(toks[k:], ok):
        return ["other"]                # "See attached for Monday's meeting", "Attached for the board"
    return _say_list(toks[k:], ok, bad)


def _say_part_of_code(toks: list[str], ok: frozenset, bad: bool) -> bool:
    """"The code is too large to email in one piece, so here is part 1 of 4.": the sentence's subject is the code, and
    what it sends is only a part of it."""
    _, post = _say_np(toks)
    if _say_kind(toks, ok, bad) not in ("code", "bare") or not post or post[0] not in (
            "is", "are", "was", "has", "will", "can", "cannot", "can't", "won't", "exceeds"):
        return False
    k = next((n for n, x in enumerate(toks) if x in ("here", "here's", "attached", "enclosed")), None)
    rest = [w for w in toks[k:] if w not in ("here", "here's", "is", "are", "attached", "enclosed", ",")] if k else []
    return bool(rest) and all(w in _SAY_PART or re.fullmatch(r"[\d.,/-]+", w) for w in rest)


def _said(text: str, place: str = "", ours=(), attach_only: bool = False) -> Said:
    """What the text says it sends (see _SAY_CODE). attach_only: only "attached", "enclosed" or "scanned" count ("here
    is" alone is not: "Here is the link to our Code"), and no line that is only a name: what images need."""
    ok = _ok_words(place, ours)
    # "the Code" (capitalized, not in an all-caps line) names the Code; "the code" may be a keypad's
    t = re.sub(r"\b[Tt]he(\s+)(?=Codes?\b)", r"our\1", _mask(text or "")).lower().replace("’", "'")
    t = re.sub(r",\s*(georgia|ga)\b", r" \1", t)    # "the City of Millen, Georgia Code of Ordinances"
    t = re.sub(r"\b(?:as\s+well\s+as|along\s+with|together\s+with|in\s+addition\s+to)\b", " & ", t)
    t = re.sub(r"\b(open|public)\s+records\b", r"\1records", t)
    code = other = paper = pages = False
    for s in re.split(r"(?<=[.!?;])\s+|\n", t):
        s = re.sub(r"\([^()]*\)", " ", s)
        toks = _SAY_TOKEN.findall(s)
        if not toks:
            continue
        bad = bool(_NOT_ADOPTED.search(s) or _NOT_IN_FORCE.search(s) or _STAFF.search(s) or _names_matter(s))
        kinds, verbs = [], 0
        for i, w in enumerate(toks):
            prev = toks[i - 1] if i else ""
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            window = toks[max(0, i - 3):i]
            window = window[max((n + 1 for n, x in enumerate(window) if x in (",", ":", ";")), default=0):]
            if w in ("attached", "enclosed", "attaching", "enclosing", "scanned", "included"):
                if any(x in _SAY_NOT for x in window):
                    continue
                j = i - 1
                while j >= 0 and toks[j] in _SAY_ADV:
                    j -= 1
                if j >= 0 and toks[j] in _SAY_BE and w in ("attached", "enclosed", "included", "scanned"):
                    verbs += 1                      # "our code is attached", "the ordinances have been scanned"
                    b = j - 1 if toks[j] == "been" and j and toks[j - 1] in ("has", "have") else j
                    kinds += _say_before(toks, b, toks[j] in ("are", "were") or toks[b] == "have", ok, bad)
                    continue
                if w == "included" and prev not in ("have", "has", "i've", "we've", "i", "we"):
                    continue                        # "including the City Charter, is available online" is no send
                if w in ("attached", "enclosed", "scanned") and prev and prev not in _SAY_DET and \
                        prev not in _SAY_CLAUSE and prev not in _SAY_STOP and prev not in _SAY_BE and \
                        prev not in _SAY_ADV and prev not in ("see", "find", "been") and \
                        nxt in ("", ",", ";", "and", "but", "so", "for", "to", "here", "herewith", "hereto", "with",
                                "as", "please", "which", "that"):
                    verbs += 1                      # "the enabling legislation attached", "Ordinances attached."
                    kinds += _say_before(toks, i, False, ok, bad)
                    continue
                verbs += 1
                kinds += _say_object(toks, i + 1, ok, bad)
            elif attach_only:
                continue
            elif w == "here's" or (w == "here" and nxt in ("is", "are")):
                verbs += 1
                kinds += _say_object(toks, i + (1 if w == "here's" else 2), ok, bad)
            elif w == "find" and (prev == "please" or nxt in ("attached", "enclosed", "below", "herewith")):
                # "please find", "you will find attached"; not "I trust you will find the records provided responsive"
                if not any(x in ("not", "can't", "cannot", "couldn't", "unable") for x in window):
                    verbs += 1
                    kinds += _say_object(toks, i + 1, ok, bad)
            elif w in ("sending", "forwarding") and prev in ("am", "are", "is", "i'm", "we're", "i", "we"):
                verbs += 1
                kinds += _say_object(toks, i + 1, ok, bad)
        if not verbs and not attach_only and not s.rstrip().endswith("?"):
            # a line that is only the code: "The Code of Ordinances, as requested.", "Code of Ordinances - Millen"
            rest = [w for w in _SAY_TOKEN.findall(_ASKED.sub(" ", _YOUR_REQUEST.sub(" ", s)))
                    if w not in (",", "-", ":", "&", "and", "as", "per", "here", "thanks", "thank", "you")]
            if rest and any(w in _SAY_CODE for w in rest) and not bad and all(
                    w in _SAY_CODE or w in _SAY_ALLOWED or w in ok or re.fullmatch(r"[\d.,/-]+", w) for w in rest):
                kinds.append("code" if _names_the_code(rest, ok) else "bare")
        if kinds and set(kinds) == {"neutral"} and _say_part_of_code(toks, ok, bad):
            kinds.append("code")
        code = code or "code" in kinds
        other = other or "other" in kinds
        paper = paper or "paper" in kinds
        pages = pages or "pages" in kinds
    return Said(code, other, paper, pages)


def _names_matter(text: str) -> bool:
    """A street address, a parcel, a lot or a case named in the text (another matter), unless the address is where to
    come or send something ("at City Hall, 101 N Main Street") or the parcel is parcel data in general."""
    text = text or ""
    for m in _MATTER.finditer(text):
        if m.group(0)[:1].isdigit() and _PLACE_OF.search(text[max(0, m.start() - 80):m.start()]):
            continue
        if m.group(0).lower().startswith("parcel") and _PARCEL_DATA.match(text[m.end():]):
            continue
        return True
    return False


def _sends_draft(text: str) -> bool:
    """The text says a draft or a proposed paper is what it sends ("Attached is the draft code update", "the proposed
    ordinance is attached"), not only that one exists ("Attached is our code. A draft update is pending.")."""
    return bool(re.search(rf"\b(?:{_VERB_NOW})\b[^.;:\n]{{0,40}}?\b(?:draft|proposed)\b|\b(?:draft|proposed)\b[^.;:\n]"
                          r"{0,50}?\b(?:is|are)\s+(?:now\s+|also\s+)?(?:attached|enclosed)\b", text or "", re.I))


def _code_word(text: str) -> bool:
    """A code word naming a code (not code enforcement or a building code, nor a modifier: "the zoning ordinance review
    letter", "a code question")."""
    m = _mask(text)
    return any(not _MODIFIES.match(m[c.end():]) for c in _CODE_WORDS.finditer(m))


def _own_subject(subj: str, jid: str) -> str:
    """The subject without our own (Re: Open Records Request: current Code of Ordinances [ORR-x]) and reply /
    external-sender prefixes. A subject the clerk wrote keeps its words ("RE: Code of Ordinances - Millen")."""
    s = re.sub(r"\s+", " ", subj or "")   # a folded subject: "Code of\n Ordinances"
    s = re.sub(r"\[ORR-[a-z0-9-]*\]?|open records request\s*:?(?:\s*(?:current\s+)?code of ordinances)?", " ", s,
               flags=re.I)
    s = re.sub(r"\[(?:external[^\]]*|ext)\]|\b(?:re|fw|fwd|aw)\s*:", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" :-")


def _sends(text: str, verbs: str = _VERB_NOW, obj: re.Pattern = _CODE_WORDS) -> bool:
    """Whether the text says the code itself is what is sent ("Attached is our code", "Here are the ordinances and the
    letter", "Our code is attached"), not another document about it ("the letter on what the City Code allows"), a
    link to it ("Here is the link to our Code") or a code word as a modifier ("the zoning ordinance review letter")."""
    vre = re.compile(rf"\b(?:{verbs})\b", re.I)
    for s in re.split(r"(?<=[.!?;])\s+|\n", _mask(text)):
        for v in vre.finditer(s):
            win = s[v.end():v.end() + 90]
            c = obj.search(win)
            if c and not _MODIFIES.match(win[c.end():]) and \
                    (not _NOT_SENT.search(win[:c.start()]) or _AND_END.search(win[:c.start()])):
                return True
        for c in obj.finditer(s):                       # "our code is attached", "the ordinances are enclosed"
            before = s[:c.start()]
            if re.match(r"[^.;:]{0,50}?\b(?:is|are|was|were|been)\s+(?:now\s+|also\s+)?(?:attached|enclosed|included|"
                        r"scanned)\b|\s*(?:attached|enclosed)\b|[^.;:]{0,30}?\b(?:is|are)\s+(?:all\s+)?(?:in|within)\s+"
                        r"(?:the|this|that)\s+(?:attached|enclosed)\b", s[c.end():], re.I) and \
                    not _MODIFIES.match(s[c.end():]) and (not _NOT_SENT.search(before) or _AND_END.search(before)):
                return True
    return False


def code_attached(text: str, place: str = "", ours=()) -> bool:
    """The text says the code, or its pages, are attached or were scanned ("Attached are the pages of our
    ordinances", "I scanned the code"): "here is" alone is not enough ("Here is the link to our Code"). Pages count
    only as the code's: the sentence or the reply's first lines name the code, and the sentence names no other matter
    (Nov 19 review: "Attached are the scanned pages for 412 Elm St." counted four photos as the Code), nor are they
    something else ("photos of the code book pages that got wet", fourth review)."""
    head = _head(text)
    named = _code_word(head) or bool(ABOUT_SPECIFIC.search(_mask(head)))
    for s in re.split(r"(?<=[.!?;])\s+|\n", text or ""):
        if _names_matter(s):
            continue                # "Attached are the code pages for parcel 12": another matter's
        said = _said(s, place, ours, attach_only=True)
        if said.code or (said.pages and not said.other and (named or _code_word(s) or ABOUT_SPECIFIC.search(_mask(s)))):
            return True
    return False


def _name_words(name: str) -> str:
    """A file name as words: "ORR_Letter-11.18.pdf" -> "ORR Letter 11 18", "MillenCode" -> "Millen Code"."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])|[_+.\-]+", " ", Path(name).stem)


def _name_text(name: str) -> str:
    """_name_words, with codes that are not the Code of Ordinances (code enforcement, a draft ordinance) masked."""
    return _mask(_name_words(name))


@functools.lru_cache(maxsize=1)
def _ga_places() -> tuple[tuple[str, ...], ...]:
    """Georgia's counties and municipalities (config/jurisdictions.yaml, shipped with the code), each as the words of
    its name without "County": the places a file name may name besides the one asked."""
    try:
        text = (Path(__file__).resolve().parents[1] / "config" / "jurisdictions.yaml").read_text(encoding="utf-8")
    except OSError:
        return ()
    names = {tuple(re.findall(r"[a-z]+", re.sub(r"\s+county\s*$", "", n.strip(" '\"").lower())))
             for n in re.findall(r"^\s*-?\s*name:\s*(.+?)\s*$", text, re.M)}
    return tuple(n for n in names if n)


_NAME_PLAIN = _SAY_ALLOWED | _SAY_CODE | frozenset("of and".split())
_NAME_ROOT = frozenset("code codes ordinances charter charters codification".split())


def _names_other_place(name: str, place: str = "") -> bool:
    """The file's name is another place's code or the state's: "Jenkins County Code.pdf", "Sylvania Ordinances.pdf",
    "City of Sylvania Code.pdf" or "County Code.pdf" for the city of Millen; "Millen City Code.pdf" for Jenkins County;
    "Georgia Code 36-35-3.pdf" or "OCGA 36-35-3.pdf" for anyone (fifth Nov 19 review: the first, the fifth and the
    sixth were saved as the Code). "Millen GA Code.pdf" is Millen's."""
    words = re.findall(r"[a-z]+|\d+", _name_words(name).lower())
    if re.search(r"\bo ?c ?g ?a\b", " ".join(words)):
        return True
    ok = _ok_words(place)
    toks = list(words)
    for part in re.split(r"-", (place or "").lower()):     # the place's own name, marked "@"
        core = [w for w in re.findall(r"[a-z]+", part) if w not in _COUNTY_WORDS]
        for i in range(len(toks) - len(core) + 1 if core else 0):
            if toks[i:i + len(core)] == core:
                toks[i:i + len(core)] = ["@"] * len(core)
    other = lambda w: w != "@" and not w.isdigit() and w not in _NAME_PLAIN and (place or w in _STATE_WORDS)  # noqa
    for i, w in enumerate(toks):
        before = toks[i - 1] if i else ""
        if w in ("county", "co", "city", "town", "village"):
            if w not in ok.place or (before and other(before)) or \
                    (toks[i + 1:i + 2] == ["of"] and len(toks) > i + 2 and other(toks[i + 2])):
                return True             # another kind of place's code, or another place's ("Screven County Code")
        if w in _STATE_WORDS and toks[i + 1:i + 2] and toks[i + 1] in ("code", "codes", "annotated") and \
                not (before == "@" or (not place and before and before not in _NAME_PLAIN)):
            return True                 # "Georgia Code 36-35-3": the state's
    if not place:
        return False
    nxt = _NAME_ROOT | {"ordinance", "county", "co", "city", "town", "ga", "georgia", "s"}
    for other_place in _ga_places():
        n = len(other_place)
        for i in range(len(toks) - n + 1):
            if tuple(toks[i:i + n]) == other_place and (
                    (toks[i + n:i + n + 1] and toks[i + n] in nxt) or
                    toks[max(0, i - 2):i] in (["city", "of"], ["town", "of"]) or
                    (i + n == len(toks) and i and toks[i - 1] in _SAY_CODE | {"of"})):
                return True             # "Sylvania Ordinances.pdf", "Sylvania's Code", "Code of Ordinances - Sylvania"
    return False


def _names_code(name: str, place: str = "") -> bool:
    """The file's name names the code (_CODE_NAME), not as the modifier of another thing ("Fine Schedule by Code
    Section.pdf", "Charter School Application.pdf"), and is no draft, model, ordinance for first reading or code no
    longer in force, nor another place's code (_names_other_place). `place`: the place's name."""
    n = _name_text(name)
    return any(not _MODIFIES.match(n[c.end():]) for c in _CODE_NAME.finditer(n)) and \
        not _NOT_CODE_NAME.search(n) and not _DRAFT_NAME.search(_name_words(name)) and \
        not _names_other_place(name, place)


def _names_code_plainly(name: str, place: str = "") -> bool:
    """A name that is the code and nothing else: a code, the ordinances or the charter, with only the place's name,
    its own kind word, numbers and words such as current, complete or part beside it: "Code of Ordinances.pdf",
    "Millen GA Code.pdf", "City Charter.pdf", "Charter_2026.pdf" (not "Zoning_Ordinances.pdf", "Sign Ordinance.docx",
    "Municode Recodification Proposal.pdf"). Only such a name is the Code when the reply reads as a staff paper or a
    draft (_PENDING)."""
    if not _names_code(name, place):
        return False
    ok = _ok_words(place)
    words = re.findall(r"[a-z]+|\d+", _name_text(name).lower())
    return any(w in _NAME_ROOT for w in words) and \
        all(w.isdigit() or w in _NAME_PLAIN or w in ok or w in _STATE_WORDS for w in words)


def _not_code_name(name: str) -> bool:
    n = _name_text(name)
    return bool(_NOT_CODE_NAME.search(n)) and not _CODE_NAME.search(n)


def _paper_name(name: str) -> bool:
    """Named as the clerk's response, cover letter or transmittal only ("Open Records Response.pdf", "Cover
    Letter.pdf"): not an invoice, an estimate or another matter's paper."""
    n = _name_text(name)
    return _not_code_name(name) and not _NEVER_CODE_NAME.search(n) and not _MATTER.search(n)


def _not_in_force(own: str, subj_own: str = "") -> str:
    """Words anywhere in the reply above its signature (and its subject) that say what it sends was never adopted or is
    no longer in force (_NOT_IN_FORCE), or ''. Fifth Nov 19 review's verifier: "Attached is the ordinance. It was voted
    down last month." was saved as the Code, the sign being in the next sentence. Then no file is the Code, not even a
    plainly named one; the owner's held-documents notice lists them."""
    m = _NOT_IN_FORCE.search(f"{subj_own}\n{_unsigned(own)}")
    return m.group(0) if m else ""


def _pending(own: str, subj_own: str = "") -> str:
    """The words of the reply above its signature (and its subject) that say it is a staff paper, a draft or an
    ordinance not yet adopted (_PENDING), or ''."""
    m = _PENDING.search(f"{subj_own}\n{_unsigned(own)}")
    return m.group(0) if m else ""


def _code_quote(text: str, place: str = "", ours=()) -> str:
    """The sentence of the reply that says the code (or its pages) is what it sends: what the owner's notice quotes."""
    parts = [p for p in re.split(r"(?<=[.!?;])\s+|\n", text or "") if p.strip()]
    hit = next((p for p in parts if _said(p, place, ours).code), None) or \
        next((p for p in parts if code_attached(p, place, ours)), None) or (text or "")
    return re.sub(r"\s+", " ", hit).strip()[:240]


def _names_other_doc(head: str) -> bool:
    """The first lines name another document as what the message is, the code at most its topic."""
    return bool(_OTHER_DOC.search(head)) and not _sends(head, _VERB_ANY)


def _names_ours(v: str) -> bool:
    """Whether a pasted "Subject:" / "Re:" line names our request, not another matter."""
    return bool(ABOUT_SPECIFIC.search(v) or _RE_OURS_STRONG.search(v) or _YOUR_REQUEST.search(v) or
                ((_RE_OURS.search(v) or _CODE_WORDS.search(v)) and not (_MATTER.search(v) or _OTHER_DOC.search(v))))


def about_request(own: str, subj_own: str = "", doc_names=(), attached: int = 0, ours=(), auto: bool = False,
                  place: str = "") -> bool:
    """Whether a reply in a request's thread is about the request (the Code of Ordinances), from its own text.
    A person's message in the request's own thread is about it unless something says it is for something else: a
    misdirected message names something else and mentions a code or a request only in passing (the Wadley letter:
    "what the City Code allows on this property", "before the request can move forward"). Reading a real answer
    ("Will do.", "I have forwarded this to our city attorney.") as not about the request leaves it 'sent', and the
    follow-up would then tell a clerk who answered that no response came (third Nov 19 review), so:
    1. the request named specifically in the first lines (the Code of Ordinances, Municode, open records, a records
       request, "your request"): about;
    2. a document named as the code ("Smallville_Ordinances.pdf"), when the first lines don't say the message is
       another document or a staff member's paper ("for Council's review", "Please send me any changes"); or named
       as the code and nothing else ("Code of Ordinances.pdf": "Please see the attached letter" is then a cover
       letter), when no pasted Subject line names another matter and the first lines are no staff paper; either
       only when the text names nothing else as what it sends ("Here is Sylvania's alcohol ordinance"): about;
    3. a pasted "Subject:" / "Re:" line for another matter (the Wadley letter's "Subject: Majestic Crown Cottage
       Court, Parcel W004-062 ... Zoning Review"): not about;
    4. the first lines name another document (a letter, a site plan, a report) and send no code: not about, unless a
       short reply names the request specifically, or the first lines name no other matter (an address, a parcel,
       code enforcement, a draft ordinance) and name a code or a cost (the clerk's own letter: "Please see the
       attached letter. The cost for copies of the code is $35.00"); a document named for the request ("ORR
       Letter.pdf") is about it too;
    5. a code word in the first lines (not code enforcement, a building or zip code, a draft), or a records reply's
       words: about;
    6. documents attached and the text saying the code is among them ("Attached is our city code ..."): about;
    7. the request named specifically anywhere: about;
    8. the first lines name another matter (an address that is not the office's own, a parcel, code enforcement, a
       draft, another document) or are a staff member's paper: not about;
    9. a short reply with a records reply's words or a pleasantry: about; another matter named above the signature
       ("Hope you're well. Attached is the review for 412 Elm St."): not about;
    10. otherwise a person's reply is about the request ("Will do.", "Could you tell me what this is for?", "See
       below from our attorney."), unless its greeting names someone other than us ("Mayor,", "Hi Tom,"); an
       automatic reply (auto) that names nothing is not. Such a reply's file is the Code only if doc_is_code says
       so. `attached` (how many files the reply carries) is no longer read: a reply is about the request with or
       without one; `ours`: our sender, requester and organization, whom a greeting may name."""
    masked = _mask(own)
    words = len(re.findall(r"[A-Za-z0-9']+", own))
    head = f"{subj_own}\n{_head(own)}"
    mhead = _mask(head)
    if ABOUT_SPECIFIC.search(mhead) or _YOUR_REQUEST.search(mhead):
        return True
    staff = bool(_STAFF.search(mhead)) or (_sends_draft(head) and not _sends(head))   # a staff paper, a draft
    other_head = _names_other_doc(mhead)
    other_subject = any(not _names_ours(_mask(v)) for v in _SUBJECT_LINE.findall(own))
    other_matter = mhead != head or _names_matter(head)     # code enforcement, a draft, a street address, a parcel
    sent = _said(f"{subj_own}\n{own}", place, ours) if doc_names else Said()
    sends_other = sent.other and not sent.code
    if any(_names_code(n, place) for n in doc_names) and not (other_head or staff or sends_other):
        return True
    if any(_names_code_plainly(n, place) or ABOUT_SPECIFIC.search(_name_text(n)) for n in doc_names) and \
            not (other_subject or staff or sends_other):
        return True     # "Please see the attached letter." with "Code of Ordinances.pdf" or "ORR Letter.pdf"
    if other_subject:
        return False
    if other_head and not (words <= SHORT_REPLY and ABOUT_SPECIFIC.search(masked)):
        # a letter and nothing else for another matter (not "the letter for the Smith rezoning. The fee is $30")
        letter = all(re.fullmatch(r"letters?", d.group(0), re.I) for d in _OTHER_DOC.finditer(mhead))
        return letter and not (other_matter or staff) and bool(_code_word(mhead) or reply_costs(head))
    if _CODE_WORDS.search(mhead) or REPLY_WORDS.search(mhead):
        return True
    if doc_names and _sends(own):
        return True
    if ABOUT_SPECIFIC.search(masked):
        return True
    if other_matter or staff or _OTHER_DOC.search(head):
        return False    # the first lines name another matter (not the signature's street address or job title)
    if words <= SHORT_REPLY and (REPLY_WORDS.search(masked) or _PLEASANTRY.match(_head(own))):
        return True
    if _names_matter(_unsigned(own)):
        return False    # "Hope you're well. Attached is the review for 412 Elm St.": a note on another matter
    # nothing says it is for something else: a person's reply in the request's own thread is about the request
    return not auto and not _addressee(own, ours)


def doc_is_code(name: str, own: str, subj_own: str = "", ours=(), place: str = "") -> str:
    """Whether a document in a reply about the request is the Code: only on positive evidence that it is (fourth Nov
    19 review: a file was the Code on its name alone, or with a generic name on a cover note's words, so misdirected
    staff mail was saved as the Code, the request closed and no notice sent). The evidence is (a) the file's name names
    the code (_CODE_NAME: code, ordinances, charter, an ordinance number; not a draft, a model or one for first
    reading), or (b) the reply says the code, the charter or the ordinances are what it sends (_said: "Attached is
    our code" with "Scan_0001.pdf"). Either way nothing in the reply may say it is for another matter: it names
    something else as what it sends ("Please see attached Title VI Plan for your files", "Here is Sylvania's alcohol
    ordinance"), refers to a conversation we never had, or is a staff paper ("for Council's review", "for your
    review", "let me know what you think", "the agenda", "a draft", anywhere above the signature: then only a name
    that is plainly the code counts). A name for a letter, an invoice or another matter is never the code. Everything
    else, "See attached." with "Scan_001.pdf" included, is held for the owner. Returns why the file is the Code:
    'name' (a), 'words' (b), or '' (it is not). `place`: the place's name, whose words a reply may use of what it
    sends; `ours`: our sender, requester and organization."""
    said = f"{subj_own}\n{own}".strip()
    if _DRAFT_NAME.search(_name_words(name)) or _NOT_IN_FORCE.search(_name_words(name)) or _not_code_name(name) or \
            _MATTER.search(_name_text(name)):
        return ""
    if "~" in _name_text(name) and not _CODE_NAME.search(_name_text(name)):
        return ""               # the name is another code: "Rotary Club Charter.pdf", "Code Enforcement Photos.pdf"
    if _CONVERSATION.search(said) or _not_in_force(own, subj_own):
        return ""
    if _pending(own, subj_own) and not _names_code_plainly(name, place):
        return ""
    s = _said(said, place, ours)
    if s.other and not s.code:
        return ""               # the reply names something else as what it sends
    if _names_code(name, place):
        return "name"           # (a)
    return "words" if s.code and not s.other else ""   # (b): "Attached are the code and the budget" leaves it in doubt


def code_documents(docs: list[dict], own: str, subj_own: str = "", ours=(), place: str = "") -> list[dict]:
    """The documents of a reply about the request that are the Code (doc_is_code). None when the greeting names
    someone other than us ("Hi Mrs. Johnson, Attached are the records you requested.") or a group ("Hi all,"): the
    note is theirs. When the text says the code itself is attached and no document is named as it, or counted as it,
    the one document named as the clerk's response, cover letter or transmittal is it: clerks often send the letter
    and the code as one PDF ("Open Records Response.pdf" with "attached is the City of Millen Code of Ordinances"). An
    invoice or an estimate never is."""
    if _addressee(own, ours) or _group(own):
        return []
    out = []
    for a in docs:
        a["why"] = doc_is_code(a["name"], own, subj_own, ours, place)    # what the owner's notice says
        if a["why"]:
            out.append(a)
    said = f"{subj_own}\n{own}".strip()
    s = _said(said, place, ours)
    if not out and not s.other and (s.code or code_attached(said, place, ours)) and \
            not (_CONVERSATION.search(said) or _pending(own, subj_own) or _not_in_force(own, subj_own)):
        papers = [a for a in docs if _paper_name(a["name"])]
        if len(papers) == 1:
            out = papers
            out[0]["why"] = "words"
    return out


def _image_shape(name: str, data: bytes) -> str:
    """'icon' (a social-media icon, a small seal: never kept), 'page' (a page scan or photo) or 'other' (a logo, a
    signature card, a picture)."""
    size = None
    try:
        import io as _io

        from PIL import Image

        with Image.open(_io.BytesIO(data)) as im:
            size = im.size
    except Exception:  # noqa: BLE001 — not an image Pillow reads (HEIC without a plugin, bad data): judge by bytes
        pass
    if size:
        short, long_ = sorted(size)
        shape = "icon" if short < ICON_PX else "page" if short >= PAGE_PX and long_ >= short * 1.15 else "other"
    else:
        shape = "icon" if len(data) < 10_000 else "page" if len(data) >= 150_000 else "other"
    if shape != "page" and _ICON_NAME.search(Path(name).stem):
        return "icon"
    return shape


def _linked(own: str, links: list[str]) -> bool:
    """The reply points to the code (a code library or file-sharing link, a link to an ordinances page or file, or
    "it is on our website / online at Municode") instead of sending it."""
    from urllib.parse import urlsplit

    for u in links:
        p = urlsplit(u)
        if _CODE_HOST.search((p.hostname or "").lower()) or re.search(r"code|ordinance|charter|\.pdf\b|\.docx?\b",
                                                                      p.path + "?" + p.query, re.I):
            return True
    for s in re.split(r"(?<=[.!?])\s+|\n", own):
        if _ONLINE.search(s) and not _NOT_ONLINE.search(s) and re.search(
                r"\b(?:codes?|ordinances?|charter|information|records?|documents?|cop(?:y|ies)|this|it|they|them|"
                r"everything|all)\b", s, re.I):
            return True
    return False


def attachments(msg: email.message.Message) -> list[dict]:
    """The files a reply carries: {name, kind: doc | image, shape (images: icon | page | other), inline, data}. Other
    types (spreadsheets, calendar invites, ...) are left out. An image is inline (part of the message's layout: a
    logo or signature image) when it has a Content-ID and the body shows it by its cid: or it is not marked an
    attachment, and it is not page-sized: a page scan is never dropped as layout (an iPhone shows attached photos
    inline). Apple Mail marks attached photos Content-Disposition: inline with no Content-ID: those are attachments.
    A document is never inline: Exchange gives ordinary attachments a Content-ID, and a PDF is not a logo."""
    cids = set()
    for b in _bodies(msg, "text/html") + _bodies(msg, "text/plain"):
        cids |= {c.strip("<>").lower() for c in re.findall(r"cid:([^\s\"'<>\]\)]+)", b, re.I)}
    out = []
    for p in msg.walk():
        if p.is_multipart() or p.get_content_type() == "message/rfc822":
            continue
        name = _decode(p.get_filename() or "")
        disp = p.get_content_disposition()
        if not name and (p.get_content_maintype() == "text" or disp != "attachment"):
            continue
        if re.fullmatch(r"ATT\d{3,}\.(?:html?|txt)", name, re.I):
            continue        # a footer Exchange appends to a message as an attachment, not a document
        ext = Path(name).suffix.lower()
        if ext not in DOC_TYPES + IMAGE_TYPES:
            add = _CT_EXT.get(p.get_content_type())
            if not add:
                continue
            name, ext = (name or "attachment") + add, add
        kind = "doc" if ext in DOC_TYPES else "image"
        try:
            data = p.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            data = b""
        cid = (p.get("Content-ID") or "").strip().strip("<>").lower()
        shape = _image_shape(name, data) if kind == "image" else ""
        inline = kind == "image" and shape != "page" and (cid in cids or disp != "attachment") and bool(cid)
        out.append({"name": name, "kind": kind, "shape": shape, "inline": inline, "data": data})
    return out


def _place_of(jid: str, name: str) -> str:
    """The name the reply rules read a place by: its own, and for a consolidated government ("athens-clarke":
    "Athens") the county's too ("Athens-Clarke County"), whose code it also is."""
    mine = set(re.findall(r"[a-z]+", (name or "").lower()))
    extra = [w for w in re.findall(r"[a-z]+", jid or "") if w not in mine]
    return f"{name}-{' '.join(extra).title()} County" if extra and not (jid or "").startswith("co-") else name


def _is_auto_reply(m: email.message.Message, subj: str) -> bool:
    auto = (m.get("Auto-Submitted") or "").strip().lower()
    return (auto not in ("", "no") or bool(m.get("X-Autoreply") or m.get("X-Autorespond")) or
            (m.get("Precedence") or "").strip().lower() == "auto_reply" or bool(_AUTO_SUBJECT.match(subj or "")))


def _returned_ids(m: email.message.Message) -> str:
    """A bounce's returned original, as text: the Message-ID and Subject of an attached message/rfc822 part, and a
    text/rfc822-headers part (Postfix returns the original as an attachment, so its Message-ID is in no body)."""
    out = []
    for p in list(m.walk())[1:]:
        out += [str(p.get("Message-ID") or ""), str(p.get("Subject") or "")]
        if p.get_content_type() == "text/rfc822-headers":
            try:
                out.append((p.get_payload(decode=True) or b"").decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
    return " ".join(out)


class Requests:
    def __init__(self, root: Path, cfg: dict, mailbox: Mailbox | None = None, log=print, notify=None):
        self.root, self.log = root, log
        self.notify = notify or github_issue
        self.opts = (cfg or {}).get("openrecords", {})
        self.tracker = Tracker(root)
        self.mailbox = mailbox or Mailbox()
        self.requester = self.opts.get("requester") or (cfg or {}).get("requester") or ""
        self.organization = self.opts.get("organization") or ""
        self.contacts = load_contacts(root)
        self._have: set | None = None
        from .codefinder import load_found

        self.found = load_found(root)

    @property
    def have(self) -> set:
        """Places with a downloaded code in imports/ordinances/ (never asked; open requests to them are withdrawn)."""
        if self._have is None:
            try:
                self._have = downloaded_places(self.root)
            except Exception as exc:  # noqa: BLE001 — a bad folder must not stop the mailbox
                self.log(f"downloaded codes not read: {type(exc).__name__}: {exc}")
                self._have = set()
            # a place with only a stray ordinance on file (one golf-cart ordinance) is still asked for its code
            self._have -= set(self.contacts.get("ask_anyway") or [])
        return self._have

    @property
    def enabled(self) -> bool:
        return bool(self.opts.get("enabled")) and self.mailbox.can_send and bool(self.requester) and \
            not self.requester.startswith("[")

    def why_not(self) -> str:
        if not self.opts.get("enabled"):
            return "openrecords.enabled is false in config/settings.yaml"
        if not self.requester or self.requester.startswith("["):
            return "openrecords.requester (the name requests are signed with) is not set"
        if not self.mailbox.can_send:
            return "mailbox secrets missing (ORR_SMTP_HOST, ORR_USER, ORR_PASSWORD, ORR_FROM)"
        return ""

    def send_batch(self, jurisdictions: list[dict], store=None, limit: int | None = None, dry_run: bool = False,
                   smtp_factory=None) -> dict:
        limit = int(limit if limit is not None else self.opts.get("daily_limit", 10))
        cooldown = int(self.opts.get("cooldown_days", 180))
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()  # sent_at is UTC; compare like with like
        sent_today = sum(1 for t in self.tracker.data.values() if (t.get("sent_at") or "")[:10] == today)
        reviews: list = []
        due = plan(jurisdictions, self.tracker, store, cooldown, self.contacts, reviews, self.have, self.found)
        write_review(self.root, reviews)
        out = {"due": len(due), "sent": 0, "portal_only": 0, "needs_review": len(reviews),
               "dry_run": dry_run or not self.enabled, "errors": 0}
        if not dry_run and not self.enabled:
            out["why_not_sending"] = self.why_not()
        for item in due:
            j = item["jurisdiction"]
            if item["how"] == "portal":
                if self.tracker.data.get(j["id"], {}).get("status") != "portal_only":
                    self.tracker.log(j["id"], "portal_only", f"submit by hand at {item['to']}", portal=item["to"],
                                     found_on=item["found_on"])
                out["portal_only"] += 1
                continue
            if sent_today + out["sent"] >= limit:
                break
            subject, body = draft(j, self.requester, self.mailbox.sender or "", self.organization)
            if out["dry_run"]:
                self.log(f"[dry run] {j['id']} -> {item['to']} : {subject}")
                continue
            msg = EmailMessage()
            msg["From"] = email.utils.formataddr((self.requester, self.mailbox.sender))
            msg["To"] = item["to"]
            msg["Subject"] = subject
            msg["Date"] = email.utils.formatdate(localtime=False)
            mid = email.utils.make_msgid(domain=(self.mailbox.sender or "orr.local").split("@")[-1])
            msg["Message-ID"] = mid
            msg.set_content(body)
            try:
                self.mailbox.send(msg, smtp_factory)
            except Exception as exc:  # noqa: BLE001 — stop for today; don't hammer a failing server
                self.tracker.log(j["id"], "error", f"{type(exc).__name__}: {exc}")
                out["errors"] += 1
                break
            sent = dt.datetime.now(dt.timezone.utc)
            self.tracker.log(j["id"], "sent", f"to {item['to']}", to=item["to"], found_on=item["found_on"],
                             subject=subject, message_id=mid, sent_at=sent.replace(microsecond=0).isoformat(),
                             due=business_days_after(sent.date(), 3).isoformat(), name=j["name"])
            out["sent"] += 1
            self.log(f"sent {j['id']} -> {item['to']}")
        self.tracker.save()
        return out

    def check_replies(self, imap_factory=None, attachments_dir: Path | None = None, held_dir: Path | None = None) -> dict:
        """Read the replies to sent requests. A reply's own text (above the quoted original) says what it is:
        - a bounce: bounced (nothing saved);
        - not about the request (about_request: a message that says it is for something else, sent to this thread),
          or an out-of-office reply: the request is left as it was, its files are held (held_dir), and the owner is
          told once (not for an out-of-office). A person's reply that names nothing else is about the request;
        - documents: the Code (saved to imports/ordinances/ as <id>__<name>: the ordinances build reads them and the
          place then counts as having its code) -> fulfilled, only on positive evidence: the file's name names the
          code (code, ordinances, charter, an ordinance number), or the reply says the code, the charter or the
          ordinances are what it sends, and nothing in the reply says it is for another matter (doc_is_code). Every
          other document ("Scan_0042.pdf" with "See attached.", a resolution, a letter, an invoice, a draft) is held
          and the owner told once, with how to accept it (code_documents); so is every file of a note whose greeting
          names someone else or a group ("Hi Mrs. Johnson,", "Hi all,");
        - images: never the Code by themselves. PAGE_IMAGES or more page-sized ones (a scan, a phone photo), with the
          text saying the code or its pages are attached or scanned and no link to the code, count as it; any others
          are held, the owner told, and the request is partial when the text says the code or its pages are
          attached, else left as it was. Icons, and logos or signature images that are part of the message's layout,
          are dropped; a page-sized image never is (an iPhone shows attached photos inline).
        Every file saved as the Code is told to the owner: one notice per reply, naming the files, the rule that took
        each (its name, or the reply's words, quoted) and how to undo it. The rules read names and words and can be
        wrong; a wrong save closes the request and puts a paper that is not the Code into the build.
        Then from the text: a cost -> cost_estimate (owner told), the code linked or online -> partial, a refusal ->
        denied, a held document -> acknowledged, anything else -> acknowledged (a reply never moves a closed request
        back to acknowledged or partial)."""
        sent = {t["message_id"]: jid for jid, t in self.tracker.data.items() if t.get("message_id")}
        if not sent:
            return {"checked": 0}
        since = min(dt.date.fromisoformat(t["sent_at"][:10]) for t in self.tracker.data.values() if t.get("sent_at"))
        msgs = self.mailbox.fetch_replies(since - dt.timedelta(days=1), imap_factory)
        attachments_dir = attachments_dir or self.root / "imports" / "ordinances"
        held_dir = held_dir or self.root / HELD_DIR
        ours = (self.mailbox.sender or "", self.requester, self.organization)
        out = {"checked": len(msgs), "matched": 0, "attachments": 0}
        bump = lambda k, n=1: out.__setitem__(k, out.get(k, 0) + n)   # noqa: E731 — keys only when they happen
        for m in msgs:
            subj = _decode(m.get("Subject"))
            refs = f"{m.get('In-Reply-To', '')} {m.get('References', '')}"
            tag = re.search(r"\[ORR-([a-z0-9-]+)\]", subj)
            jid = tag.group(1) if tag else next((v for k, v in sent.items() if k in refs), None)
            frm = _decode(m.get("From")).lower()
            body = _text(m)
            if not jid and ("mailer-daemon" in frm or "postmaster" in frm):
                hay = f"{body}\n{_returned_ids(m)}"       # Postfix returns the original as an attachment
                jid = next((v for k, v in sent.items() if k in hay), None)
                tag = None if jid else re.search(r"\[ORR-([a-z0-9-]+)\]", hay)
                jid = jid or (tag.group(1) if tag else None)
            if not jid or jid not in self.tracker.data:
                continue
            mid = m.get("Message-ID", "")
            t = self.tracker.data[jid]
            if mid and mid in {r.get("message_id") for r in t.get("replies", [])}:
                continue
            out["matched"] += 1
            name, before = t.get("name", jid), t.get("status")
            place = _place_of(jid, name)
            own, subj_own = own_text(body, ours), _own_subject(subj, jid)
            said = f"{subj_own}\n{own}".strip()
            bounce = "mailer-daemon" in frm or "postmaster" in frm or "undeliverable" in subj.lower() or \
                "delivery status" in subj.lower()
            auto = not bounce and _is_auto_reply(m, subj)
            away = auto and bool(_AWAY.search(said))   # out of office
            atts = [] if bounce else attachments(m)       # a bounce carries the mail system's icon, never the code
            pages_said = code_attached(said, place, ours)
            docs = [a for a in atts if a["kind"] == "doc"]
            # images that can be something: not icons, not the layout's logos and signature images
            images = [a for a in atts if a["kind"] == "image" and a["shape"] != "icon" and not a["inline"]]
            about = bounce or (not away and about_request(own, subj_own, [a["name"] for a in docs],
                                                          len(docs) + len(images), ours, auto, place))
            # "Hi Mrs. Johnson,", "Hi all,": a note for someone else
            to = "" if bounce else (_addressee(own, ours) or _group(own))
            links = re.findall(r"https?://[^\s<>\")\]]+", own)[:10]
            # costs from what the sender wrote above the signature (not its "Online bill payment is now available")
            costs = reply_costs(f"{subj_own}\n{_unsigned(own)}") if about and not bounce else []
            linked = about and not bounce and _linked(own, links)
            code_docs = code_documents(docs, own, subj_own, ours, place) if about else []
            page_imgs = [a for a in images if a["shape"] == "page"]
            pages = about and not bounce and not code_docs and not linked and pages_said and not to and \
                len(page_imgs) >= PAGE_IMAGES and not _pending(own, subj_own) and not _not_in_force(own, subj_own)
            for a in page_imgs if pages else []:
                a["why"] = "pages"
            code_files = code_docs + (page_imgs if pages else [])
            keep = {id(a) for a in code_files}
            held_files = [a for a in docs + images if id(a) not in keep]
            held_docs = [a for a in held_files if a["kind"] == "doc"]
            files, held = self._save(jid, code_files, attachments_dir), self._save(jid, held_files, held_dir)
            note = ""
            if bounce:
                status = "bounced"
            elif not about:
                status, note = before, ("automatic reply" if away or _is_auto_reply(m, subj) else NOT_ABOUT)
            elif files:
                status = "fulfilled"
            elif costs:
                status = "cost_estimate"
            elif linked:
                status = "partial"
            elif _DENIED.search(own):
                status = "denied"
            elif held_docs:        # the clerk's letter, an invoice: an answer to read, not the Code
                status = "acknowledged" if before in ACK_FROM else before
            elif held:
                status = "partial" if pages_said and not to else before   # images alone are not the Code
            else:
                status = "acknowledged" if before in ACK_FROM else before
            if status in ("acknowledged", "partial") and before in CLOSED:
                status = before                     # "you're welcome" doesn't reopen a closed request
            reply = {"at": _now(), "from": frm[:120], "subject": subj[:200], "message_id": mid, "status": status,
                     "files": files, "links": links, "excerpt": (own or body)[:800]}
            if held:
                reply["held"] = held
            if note:
                reply["note"], reply["unrelated"] = note, True
            t.setdefault("replies", []).append(reply)
            extra = {"files": t.get("files", []) + files} if files else {}
            if held:
                extra["held"] = t.get("held", []) + held
            self.tracker.log(jid, status, f"reply from {frm[:80]}" + (f": {note}" if note else ""), **extra)
            out["attachments"] += len(files)
            if held:
                bump("held", len(held))
            # one notice per reply at most: a cost, something unrelated in the thread, or files to look at
            notice = None
            head = f"\n\n- From: {frm[:120]}\n- Subject: {subj[:200]}\n"
            saved = self._saved_lines(code_files, files, said, place, ours, before)
            also = "; files saved as the Code" if files else ""
            if costs:
                bump("cost_notices")
                notice = ("cost notice", f"Open records: {name} mentions a cost{also}",
                          f"**{name}** (`{jid}`) replied to the Code of Ordinances request with a possible charge. "
                          f"Nothing has been agreed or paid; " +
                          (f"the request is **{status}** (files were counted as the Code)." if files else
                           "the request waits for you.") + head +
                          f"- Amounts seen: {', '.join(costs) or 'none stated'}\n" + self._held_line(held) + saved +
                          "\n" + self._quote(own) +
                          "\n\nReply from the project mailbox to accept, narrow or decline. Tracker: "
                          "`data/openrecords/requests.json`.")
            elif note == NOT_ABOUT:
                bump("unrelated")
                notice = ("unrelated-message notice",
                          f"Open records: {name} sent something unrelated to the request thread",
                          f"**{name}** (`{jid}`): a message in the Code of Ordinances request thread does not read "
                          f"as a reply to the request (its own text is about something else), so the request was "
                          f"left as it was (**{before}**) and nothing in it was counted as the code. The "
                          f"sender may have meant it for someone else: you may want to tell them it reached the "
                          f"project's records address." + head + self._held_line(held) + "\n" +
                          self._quote(own or body) +
                          "\n\nIf it is the Code after all, move the files to `imports/ordinances/` and set the "
                          "request's status in `data/openrecords/requests.json`.")
            elif about and not bounce and pages:
                bump("image_notices")
                notice = ("images notice", f"Open records: {name} sent the code as page images to look at",
                          f"**{name}** (`{jid}`) replied with {len(page_imgs)} page images and no document. The "
                          f"reply says they are the code or its pages, so they were counted as it (saved to "
                          f"`imports/ordinances/`, where the ordinances build reads them by OCR; status "
                          f"**fulfilled**): check they are the whole code." + head + self._held_line(held) + saved +
                          "\n" + self._quote(own))
            elif about and not bounce and (held_docs or (held and not files)):   # a held letter: always told
                n_img = len(held_files) - len(held_docs)
                what = ("files" if held_docs and n_img else "images" if n_img else
                        "a document" if len(held_docs) == 1 else "documents")
                bump("image_notices" if not held_docs else "document_notices")
                why = []
                if to:
                    why.append(f"a greeting to {to}, not to the project, so none of its files was counted as the "
                               f"Code (the note may be meant for them)")
                if held_docs:
                    why.append(f"{len(held_docs)} document{'s' * (len(held_docs) != 1)} not counted as the Code (a "
                               f"document counts only when its name names the code, such as code, ordinances, charter "
                               f"or an ordinance number, or the reply says the code, the charter or the ordinances are "
                               f"what it sends, and nothing says it is for another matter): read "
                               f"{'it' if len(held_docs) == 1 else 'them'} (a letter may quote a cost, set a date or "
                               f"decline)")
                if n_img:
                    why.append(f"{n_img} image{'s' * (n_img != 1)} (an image is not counted as the Code unless "
                               f"{PAGE_IMAGES} or more page scans come with a reply saying they are its pages)")
                notice = (("images notice" if not held_docs else "documents notice"),
                          f"Open records: {name} sent {what} to look at{also}",
                          f"**{name}** (`{jid}`) replied with " + " and ".join(why) + ". Held where neither the "
                          f"ordinances build nor the withdraw step reads them; the request is **{status}**"
                          f"{' (unchanged)' if status == before else ''}. If they are the Code (or its pages), move "
                          f"them, keeping their names, to `imports/ordinances/` and set the request's status in "
                          f"`data/openrecords/requests.json`." + head + self._held_line(held) + saved + "\n" +
                          self._quote(own))
            elif files:            # a save with nothing else to tell: told all the same, to be checked
                notice = ("saved notice", f"Open records: {name} sent its Code: check what was saved",
                          f"**{name}** (`{jid}`) replied, and the reply was read as sending the Code: its files were "
                          f"saved where the ordinances build reads them and the request is **{status}**. The rule "
                          f"reads names and words and can be wrong." + head + self._held_line(held) + saved + "\n" +
                          self._quote(own[:600]))
            if notice:
                self.tracker.log(jid, t["status"], f"{notice[0]}: {self.notify(*notice[1:])}")
        self.tracker.save()
        return out

    @staticmethod
    def _save(jid: str, items: list[dict], folder: Path) -> list[str]:
        """Write each attachment to folder as <id>__<name> (a repeated name gets _2, _3 ...); the names written."""
        names = []
        for a in items:
            folder.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{jid}__{a['name']}")
            stem, ext, i = Path(safe).stem, Path(safe).suffix[:12], 2
            safe = stem[:150 - len(ext)] + ext                  # a long name keeps its extension
            stem = Path(safe).stem
            while safe in names:
                safe, i = f"{stem}_{i}{ext}", i + 1
            (folder / safe).write_bytes(a["data"])
            names.append(safe)
        return names

    @staticmethod
    def _saved_lines(code_files: list[dict], files: list[str], said: str, place: str, ours, before: str) -> str:
        """What a notice says of the files saved as the Code: each one, the rule that took it (its name, or the
        reply's words, quoted), and how to undo it."""
        if not files:
            return ""
        quote = _code_quote(said, place, ours)
        rule = {"name": lambda a: f'its name ("{a["name"]}")',
                "pages": lambda a: f'the reply\'s words, with {PAGE_IMAGES} or more page images: "{quote}"'}
        lines = "".join(f"- Saved as the Code: `imports/ordinances/{f}`, by " +
                        rule.get(a.get("why"), lambda a: f'the reply\'s words: "{quote}"')(a) + "\n"
                        for a, f in zip(code_files, files))
        back = f"set the request's status back to **{before}** and " if before != "fulfilled" else ""
        return lines + (f"- To undo: move {'it' if len(files) == 1 else 'them'} to `{HELD_DIR.as_posix()}/`; in "
                        f"`data/openrecords/requests.json`, {back}add a note to its history saying why.\n")

    @staticmethod
    def _held_line(held: list[str]) -> str:
        return (f"- Held (not counted as the code): {', '.join(f'`{HELD_DIR.as_posix()}/{h}`' for h in held)}\n"
                if held else "")

    @staticmethod
    def _quote(text: str) -> str:
        return "> " + "\n> ".join((text or "(no text)")[:1500].splitlines())

    @staticmethod
    def answered(t: dict) -> bool:
        """Whether the request has had a reply about it (a message for something else, or an automatic reply, is
        not one)."""
        return any(not r.get("unrelated") for r in t.get("replies") or [])

    @staticmethod
    def heard_from(t: dict) -> bool:
        """Whether a person wrote in the request's thread (not an automatic reply or a bounce), whether or not it read
        as an answer: a message read as meant for something else may be a real answer misread, and "I have not yet
        received a response" to a clerk who answered is rude. The owner was told of it and can follow up by hand."""
        return any(r.get("note") != "automatic reply" and r.get("status") != "bounced" for r in t.get("replies") or [])

    def follow_ups(self, smtp_factory=None, after_business_days: int | None = None) -> dict:
        n = int(after_business_days if after_business_days is not None else self.opts.get("follow_up_after_business_days", 5))
        today = dt.datetime.now(dt.timezone.utc).date()
        out = {"due": 0, "sent": 0}
        for jid, t in self.tracker.data.items():
            if t.get("status") != "sent" or t.get("followed_up_at") or not t.get("sent_at") or jid in self.have or \
                    self.heard_from(t):   # a reply that left the status alone (images only, unrelated) still counts
                continue
            if today < business_days_after(dt.date.fromisoformat(t["sent_at"][:10]), n):
                continue
            out["due"] += 1
            if not self.enabled:
                continue
            msg = EmailMessage()
            msg["From"] = email.utils.formataddr((self.requester, self.mailbox.sender))
            msg["To"] = t["to"]
            msg["Subject"] = "Re: " + t["subject"]
            msg["In-Reply-To"] = msg["References"] = t["message_id"]
            msg["Message-ID"] = email.utils.make_msgid(domain=(self.mailbox.sender or "orr.local").split("@")[-1])
            msg.set_content(FOLLOW_UP.format(name=t.get("name", jid), sent_on=t["sent_at"][:10],
                                            signature=self.requester + (f"\n{self.organization}" if self.organization else "")))
            try:
                self.mailbox.send(msg, smtp_factory)
            except Exception as exc:  # noqa: BLE001
                self.tracker.log(jid, "sent", f"follow-up failed: {type(exc).__name__}: {exc}")
                break
            self.tracker.log(jid, "followed_up", "no response after the deadline", followed_up_at=_now())
            out["sent"] += 1
        self.tracker.save()
        return out

    def withdraw(self, smtp_factory=None) -> dict:
        """Close requests to places that now have a downloaded code: one short note in the same thread for a request
        that was emailed (only when sending is enabled), nothing to send for a portal-only entry."""
        out = {"due": 0, "withdrawn": 0, "closed": 0}
        for jid, t in self.tracker.data.items():
            if jid not in self.have:
                continue
            if t.get("status") in ("portal_only", "bounced"):
                # nothing reached the clerk (a portal never used, a letter that bounced): closed, nothing to send
                self.tracker.log(jid, "withdrawn", "a downloaded code is in imports/ordinances; nothing to submit")
                out["closed"] += 1
                continue
            if t.get("status") not in OPEN or not t.get("message_id") or not t.get("to"):
                continue
            out["due"] += 1
            if not self.enabled:
                continue
            msg = EmailMessage()
            msg["From"] = email.utils.formataddr((self.requester, self.mailbox.sender))
            msg["To"] = t["to"]
            msg["Subject"] = "Re: " + t["subject"]
            msg["In-Reply-To"] = msg["References"] = t["message_id"]
            msg["Message-ID"] = email.utils.make_msgid(domain=(self.mailbox.sender or "orr.local").split("@")[-1])
            msg.set_content(WITHDRAW.format(name=t.get("name", jid), sent_on=(t.get("sent_at") or "")[:10] or "earlier",
                                            signature=self.requester + (f"\n{self.organization}" if self.organization else "")))
            try:
                self.mailbox.send(msg, smtp_factory)
            except Exception as exc:  # noqa: BLE001 — try again next run
                self.tracker.log(jid, t["status"], f"withdrawal failed: {type(exc).__name__}: {exc}")
                break
            self.tracker.log(jid, "withdrawn", "a downloaded code is in imports/ordinances; request withdrawn",
                             withdrawn_at=_now())
            out["withdrawn"] += 1
        self.tracker.save()
        return out

    def self_test(self, jurisdictions: list[dict], to: str = "", wait_seconds: int = 90, smtp_factory=None,
                  imap_factory=None) -> dict:
        """Send one marked sample letter to the project mailbox (or `to`) and look for it over IMAP."""
        import time

        out = {"smtp": None, "imap": None, "seen_in_inbox": None}
        if not self.mailbox.can_send:
            out["smtp"] = "not tried: " + self.why_not() if self.opts.get("enabled") else \
                "not tried: mailbox secrets missing (ORR_SMTP_HOST, ORR_USER, ORR_PASSWORD, ORR_FROM)"
            return out
        to = to or self.mailbox.sender
        sample = next((i["jurisdiction"] for i in plan(jurisdictions, Tracker(self.root), None, 0, self.contacts)
                       if i["how"] == "email"), {"id": "sample", "name": "Sampleton", "kind": "city"})
        token = "selftest-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        subject, body = draft(sample, self.requester or "[requester]", self.mailbox.sender or "", self.organization)
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((self.requester or "Crawl", self.mailbox.sender))
        msg["To"] = to
        msg["Subject"] = f"[TEST {token}] " + subject.replace(f"[ORR-{sample['id']}]", "").strip()
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=(self.mailbox.sender or "orr.local").split("@")[-1])
        msg.set_content("TEST ONLY: no request is being made. This is the letter Crawl would send to "
                        f"{sample['name']}, delivered to the project mailbox to check the settings.\n\n" + body)
        out["port"] = self.mailbox.port
        try:
            self.mailbox.send(msg, smtp_factory)
            out["smtp"] = f"ok: sent to {to} as {self.mailbox.sender}"
        except Exception as exc:  # noqa: BLE001
            out["smtp"] = f"failed: {type(exc).__name__}: {exc}"
            if not smtp_factory:
                out["connection_check"] = self.mailbox.probe()
            return out
        if not (self.mailbox.imap_host or imap_factory):
            out["imap"] = "not tried: ORR_IMAP_HOST not set (replies won't be read)"
            return out
        end = time.monotonic() + wait_seconds
        while True:
            try:
                m = imap_factory() if imap_factory else imaplib.IMAP4_SSL(self.mailbox.imap_host, 993)
                try:
                    m.login(self.mailbox.user, self.mailbox.password)
                    out["imap"] = "ok: logged in"
                    m.select("INBOX", readonly=True)
                    typ, ids = m.search(None, "SUBJECT", f'"{token}"')
                    found = bool(ids and ids[0] and ids[0].split())
                finally:
                    try:
                        m.logout()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                out["imap"] = f"failed: {type(exc).__name__}: {exc}"
                return out
            if found or to != self.mailbox.sender or time.monotonic() > end:
                out["seen_in_inbox"] = found if to == self.mailbox.sender else "not checked (sent to another address)"
                return out
            time.sleep(10)

    def deadlines(self, gh=None) -> dict:
        """The Act asks for a response within 3 business days. Requests past that with no reply are listed in one
        GitHub issue (opened when the first one is late, so GitHub notifies you); each newly late place is added as
        a comment (also notified); the issue closes itself when none are late."""
        gh = gh or _gh_api
        repo = os.environ.get("GACRAWL_ISSUES_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
        today = dt.datetime.now(dt.timezone.utc).date()
        late = []
        for jid, t in self.tracker.data.items():
            if t.get("status") not in ("sent", "followed_up") or self.answered(t) or not t.get("due"):
                continue   # a message about something else, or an automatic reply, is not an answer
            due = dt.date.fromisoformat(t["due"])
            if today > due:
                days = sum(1 for i in range(1, (today - due).days + 1) if (due + dt.timedelta(days=i)).weekday() < 5)
                late.append((jid, t, days))
        late.sort(key=lambda x: -x[2])
        new = [x for x in late if not x[1].get("late_notified")]
        out = {"late": len(late), "newly_late": len(new)}
        if not repo:
            return out
        title = "Open records: past the 3-business-day deadline"
        try:
            issue = next((i for i in gh("GET", f"repos/{repo}/issues?state=open&per_page=100") or []
                          if i.get("title") == title and "pull_request" not in i), None)
            rows = "\n".join(f"| {t.get('name', jid)} (`{jid}`) | {(t.get('sent_at') or '')[:10]} | {t['due']} | {d} | "
                             f"{(t.get('followed_up_at') or '')[:10] or 'not yet'} |" for jid, t, d in late)
            body = ("Code of Ordinances requests with no reply after the Georgia Open Records Act's 3 business days "
                    "(O.C.G.A. § 50-18-71(b)(1)). Crawl sends one follow-up after "
                    f"{int(self.opts.get('follow_up_after_business_days', 5))} business days. State holidays are not "
                    "counted, so a deadline can read a day early.\n\n| Place | Asked | Answer due | Business days late | "
                    "Follow-up sent |\n| --- | --- | --- | --- | --- |\n" + rows +
                    f"\n\n_Updated {_now()[:16].replace('T', ' ')} UTC. Closes itself when none are late._")
            if late and not issue:
                issue = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body})
            elif issue and late:
                gh("PATCH", f"repos/{repo}/issues/{issue['number']}", {"body": body})
                if new:
                    gh("POST", f"repos/{repo}/issues/{issue['number']}/comments",
                       {"body": "Newly past the deadline: " + ", ".join(t.get("name", jid) for jid, t, _ in new)})
            elif issue and not late:
                gh("PATCH", f"repos/{repo}/issues/{issue['number']}", {"state": "closed",
                                                                      "body": body.split("| Place")[0] + "None late now."})
            for jid, t, _ in new:
                t["late_notified"] = _now()
            out["issue"] = (issue or {}).get("html_url")
        except Exception as exc:  # noqa: BLE001 — a notice failing must not stop the run
            out["error"] = str(exc)[:200]
        self.tracker.save()
        return out

    def summary(self) -> dict:
        from collections import Counter

        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        overdue = [jid for jid, t in self.tracker.data.items() if t.get("status") == "sent" and (t.get("due") or "9") < today]
        return {"by_status": dict(Counter(t.get("status") for t in self.tracker.data.values())), "overdue": overdue,
                "portal_only": [(jid, t.get("portal")) for jid, t in self.tracker.data.items() if t.get("status") == "portal_only"]}


def discover_contacts(fetcher, jurisdictions: list[dict], state_path: Path, deadline: float | None = None,
                      log=print) -> int:
    """Fill in records contacts for jurisdictions already seeded (one or two requests each)."""
    import time

    from .blocked import Blocked
    from .fetch import Disallowed
    from .seed import find_records_contact

    disc = json.loads(state_path.read_text()) if state_path.exists() else {}
    n = 0
    for j in sorted(jurisdictions, key=lambda j: -(j.get("population") or 0)):
        d = disc.get(j["id"])
        if not d or not d.get("website") or "records" in d or (deadline and time.monotonic() > deadline):
            continue
        try:
            r = fetcher.get(d["website"])
            rc = find_records_contact(fetcher, r.text, r.url) if r.status == 200 and "html" in r.content_type else None
        except (Disallowed, Blocked, Exception):  # noqa: BLE001
            rc = None
        d["records"] = rc or {}
        n += 1
        if rc:
            log(f"{j['id']}: {rc.get('emails') or rc.get('portal')}")
        if n % 20 == 0:
            state_path.write_text(json.dumps(disc, indent=1, ensure_ascii=False))
    state_path.write_text(json.dumps(disc, indent=1, ensure_ascii=False))
    return n


_MONEY = re.compile(r"\$\s?\d+(?:,\d{3})*(?:\.\d{2})?")   # "$1,250.00"; not the comma after "$30,"
_COST_WORDS = re.compile(r"\b(cost|costs|fee|fees|estimate|estimated|charge|charges|invoice|payment|pay|deposit|"
                         r"per page|prepay|prepayment)\b", re.I)


# a reply that says there is nothing to pay ("There is no fee for this request", "free of charge", "no cost to you").
# Nov 10: Flowery Branch's clerk wrote "There is no fee for this request" and an issue said they mentioned a cost
NO_COST = re.compile(r"\b(?:there\s+(?:is|will\s+be)\s+no\s+(?:fee|charge|cost)s?|no\s+(?:fee|charge|cost)s?\s+(?:for|to|is|will|associated|required)|"
                     r"free\s+of\s+charge|at\s+no\s+(?:cost|charge)|without\s+(?:charge|cost|a\s+fee)|no\s+(?:fee|charge|cost)\s*[.!]|"
                     r"(?:fee|charge|cost)s?\s+(?:has|have|is|are)\s+(?:been\s+)?waived)\b", re.I)


def _cost_text(text: str) -> str:
    """text without what says there is nothing to pay, amount and all ("The normal $25.00 copy fee has been waived.",
    "There is no fee for the first hour;"), and without a bill-pay line that names no amount ("Online bill payment is
    now available at www.millenga.gov", a signature's)."""
    keep = []
    for ln in (text or "").splitlines():
        keep += [p for p in re.split(r"(?<=[.!?;])\s+|,?\s+(?=\b(?:but|however|although|though)\b)", ln)
                 if p and not NO_COST.search(p) and not (_BILL_PAY.search(p) and not _MONEY.search(p))]
    return "\n".join(keep)


def mentions_cost(text: str) -> list[str]:
    """Dollar amounts in a reply that talks about paying ([] = no cost mentioned). A reply that talks about
    fees or payment with no amount yet still counts: ['amount not stated']; one that says there is nothing to pay, and
    names no amount, does not; an amount in a sentence saying it is waived or not charged is no cost (Nov 19 review:
    "The normal $25.00 copy fee has been waived" opened a cost notice)."""
    t = _cost_text(text)
    if not _COST_WORDS.search(t):
        return []
    amounts = list(dict.fromkeys(_MONEY.findall(t)))[:5]
    if amounts:
        return amounts
    if NO_COST.search(text or ""):
        return []
    return ["amount not stated"] if re.search(r"\b(fee|invoice|payment|deposit|prepay|cost estimate|estimated cost|"
                                              r"will cost|charge of|there is a charge)\b", t.lower()) else []


def reply_costs(own: str) -> list[str]:
    """Costs in a reply's own text: mentions_cost, or a dollar amount in a reply about the request with no cost word
    ("Approximately $100.00 to fulfill this request": the cost word was only ever in our quoted letter)."""
    return mentions_cost(own) or list(dict.fromkeys(_MONEY.findall(_cost_text(own))))[:5]


def github_issue(title: str, body: str) -> str:
    """Open an issue in this repository (GitHub then emails / pushes it to the owner). Returns its URL or why not."""
    import subprocess

    repo = os.environ.get("GACRAWL_ISSUES_REPO") or os.environ.get("GITHUB_REPOSITORY")   # the private data repository on the public runner
    if not repo:
        return "not sent (no GITHUB_REPOSITORY)"
    try:
        r = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/issues", "--input", "-"],
                           input=json.dumps({"title": title, "body": body}), capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout).get("html_url", "sent") if r.returncode == 0 else f"failed: {r.stderr.strip()[:200]}"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {exc}"


def _gh_api(method: str, path: str, body: dict | None = None):
    import subprocess

    cmd = ["gh", "api", "-X", method, path] + (["--input", "-"] if body is not None else [])
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {method} {path}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def send_letter(path: Path, mailbox: "Mailbox | None" = None, env: dict | None = None, smtp_factory=None) -> dict:
    """One letter from config/private/outbox/ (headers To: and Subject:, optionally In-Reply-To: and References: to answer
    in an existing thread, a blank line, then the body), sent from the project mailbox;
    a <letter>.sent file beside it records the sending, and a letter with one is never sent again. {FEC_KEY_LINE} in
    the body becomes the project's FEC key (FEC_API_KEY) or, without one here, an offer to send it."""
    e = env if env is not None else os.environ
    sent = path.with_name(path.name + ".sent")
    if sent.exists():
        return {"sent": False, "why": f"already sent ({sent.read_text().strip()})"}
    head, _, body = path.read_text(encoding="utf-8").partition("\n\n")
    h = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in head.splitlines()) if v.strip()}
    if not h.get("to") or not h.get("subject") or not body.strip():
        return {"sent": False, "why": "a letter needs To:, Subject:, a blank line and a body"}
    key = (e.get("FEC_API_KEY") or "").strip()
    body = body.replace("{FEC_KEY_LINE}", f"The key we would like upgraded: {key}" if key else
                        "If you need our key to find the account, reply to this message and we will send it.")
    mb = mailbox or Mailbox(e)
    if not mb.can_send:
        return {"sent": False, "why": "mailbox secrets missing (ORR_SMTP_HOST, ORR_USER, ORR_PASSWORD, ORR_FROM)"}
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = mb.sender, h["to"], h["subject"]
    msg["Reply-To"] = mb.sender
    if h.get("in-reply-to"):   # an answer in an existing thread (the clerk's reply's Message-ID)
        msg["In-Reply-To"] = h["in-reply-to"]
        msg["References"] = h.get("references") or h["in-reply-to"]
    msg.set_content(body.strip() + "\n")
    mb.send(msg, smtp_factory=smtp_factory)
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sent.write_text(f"{at} to {h['to']}\n")
    return {"sent": True, "to": h["to"], "subject": h["subject"], "at": at, "with_key": bool(key)}
