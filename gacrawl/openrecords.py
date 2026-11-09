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
check      reads replies over IMAP (matched by the [ORR-<id>] tag in the subject or the thread headers):
           acknowledgement, cost estimate, records attached (PDFs go to imports/ordinances/ as <id>__<name>, where
           the ordinances workflow reads them and sends them to Electo) or linked, bounce, denial.
follow-up  one polite follow-up in the same thread when the 3-business-day deadline has passed by a few days.
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

The Act asks for a response within three business days. Thank you for your help.

{signature}
{reply_line}"""
WITHDRAW = """To the Records Custodian, {name}:

Regarding the Open Records Act request below, sent {sent_on}, for the Code of Ordinances of {name}: we have since
obtained a copy, so I am withdrawing the request. No further work or response is needed. Thank you for your time.

{signature}
"""
FOLLOW_UP = """To the Records Custodian, {name}:

I am following up on the Open Records Act request below, sent {sent_on}, for an electronic copy of the current
Code of Ordinances of {name}. I have not yet received a response. Could you let me know its status?

Thank you,
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
    approved: {jid: address}, portals: {jid: url}, skip: [jid, ...]. config/openrecords.yaml is read if present."""
    p = root / "config" / "private" / "openrecords.yaml"
    if not p.exists():
        p = root / "config" / "openrecords.yaml"
    if not p.exists():
        return {"approved": {}, "portals": {}, "skip": []}
    import yaml

    d = yaml.safe_load(p.read_text()) or {}
    return {"approved": {k: _clean(v) for k, v in (d.get("approved") or {}).items() if _clean(v)},
            "portals": {k: str(v).strip() for k, v in (d.get("portals") or {}).items() if str(v).startswith("http")},
            "skip": list(d.get("skip") or [])}


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


def crawlable_code(j: dict, store=None) -> bool:
    if any(j.get("portals", {}).get(k) for k in ("ecode360", "amlegal")):
        return True
    if store is not None:
        with store._lock:
            n = store.db.execute("SELECT COUNT(*) FROM records WHERE jurisdiction=? AND kind='ordinance'", (j["id"],)).fetchone()[0]
        return n >= 25  # a real code, not a stray ordinance or two
    return False


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
    for rel in _inbox_paths(root):
        parts = [x for x in rel.split("/") if x]
        if len(parts) < 2:  # a loose file at the top: <id>__<name>.pdf
            m = re.match(r"(.+?)__", parts[0]) if parts else None
            jid = places.resolve(m.group(1)) if m else None
            if jid:
                have.add(jid)
            continue
        for x in parts[:-1]:
            if x.lower().endswith(".zip") or _of._norm(x) in _of.NOISE_DIRS:
                continue
            k = re.sub(r" (consolidated|unified) government$", "", _of._norm(x))  # webster_county_consolidated_government
            jid = None if k.endswith(" county") else places.resolve(k)
            jid = jid or places.resolve_folder(k)[0]
            if jid:
                have.add(jid)
                break
    # a consolidated government's code is its county's too (macon-bibb -> co-bibb), and the reverse
    for j in places.places:
        if j.get("kind") == "consolidated" and "-" in j["id"]:
            county = "co-" + j["id"].split("-", 1)[1]
            if county in places.by_id and (j["id"] in have or county in have):
                have |= {j["id"], county}
    return have


def plan(jurisdictions: list[dict], tracker: Tracker, store=None, cooldown_days: int = 180,
         contacts: dict | None = None, reviews: list | None = None, have: set | None = None) -> list[dict]:
    """Jurisdictions due a request, largest first, each with how it would be sent. Places whose only addresses
    need a check are appended to `reviews` (when given) instead. `have`: ids with a downloaded code (skipped)."""
    now = dt.datetime.now(dt.timezone.utc)
    contacts = contacts or {}
    out = []
    for j in sorted(jurisdictions, key=lambda j: -(j.get("population") or 0)):
        if j.get("kind") not in ASKABLE_KINDS or j["id"] in (contacts.get("skip") or []):
            continue
        if j.get("enabled") is False or crawlable_code(j, store) or j["id"] in (have or ()):
            continue
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


def _text(msg: email.message.Message) -> str:
    parts = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() == "text/plain" and not p.get_filename():
            try:
                parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(parts)


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

    @property
    def have(self) -> set:
        """Places with a downloaded code in imports/ordinances/ (never asked; open requests to them are withdrawn)."""
        if self._have is None:
            try:
                self._have = downloaded_places(self.root)
            except Exception as exc:  # noqa: BLE001 — a bad folder must not stop the mailbox
                self.log(f"downloaded codes not read: {type(exc).__name__}: {exc}")
                self._have = set()
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
        due = plan(jurisdictions, self.tracker, store, cooldown, self.contacts, reviews, self.have)
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

    def check_replies(self, imap_factory=None, attachments_dir: Path | None = None) -> dict:
        sent = {t["message_id"]: jid for jid, t in self.tracker.data.items() if t.get("message_id")}
        if not sent:
            return {"checked": 0}
        since = min(dt.date.fromisoformat(t["sent_at"][:10]) for t in self.tracker.data.values() if t.get("sent_at"))
        msgs = self.mailbox.fetch_replies(since - dt.timedelta(days=1), imap_factory)
        attachments_dir = attachments_dir or self.root / "imports" / "ordinances"
        out = {"checked": len(msgs), "matched": 0, "attachments": 0}
        for m in msgs:
            subj = _decode(m.get("Subject"))
            refs = f"{m.get('In-Reply-To', '')} {m.get('References', '')}"
            tag = re.search(r"\[ORR-([a-z0-9-]+)\]", subj)
            jid = tag.group(1) if tag else next((v for k, v in sent.items() if k in refs), None)
            frm = _decode(m.get("From")).lower()
            body = _text(m)
            if not jid and ("mailer-daemon" in frm or "postmaster" in frm):
                jid = next((v for k, v in sent.items() if k in body), None)
            if not jid or jid not in self.tracker.data:
                continue
            mid = m.get("Message-ID", "")
            t = self.tracker.data[jid]
            if mid and mid in {r.get("message_id") for r in t.get("replies", [])}:
                continue
            out["matched"] += 1
            files = []
            for p in m.walk():
                fn = p.get_filename()
                if fn and fn.lower().endswith((".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg")):
                    attachments_dir.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{jid}__{_decode(fn)}")[:150]
                    (attachments_dir / safe).write_bytes(p.get_payload(decode=True) or b"")
                    files.append(safe)
            links = re.findall(r"https?://[^\s<>\")]+", body)[:10]
            low = body.lower()
            costs = mentions_cost(f"{subj}\n{body}")
            if "mailer-daemon" in frm or "postmaster" in frm or "undeliverable" in subj.lower() or "delivery status" in subj.lower():
                status = "bounced"
            elif files:
                status = "fulfilled"
            elif costs:
                status = "cost_estimate"
            elif re.search(r"\b(denied|deny|exempt|no responsive records|do not have)\b", low):
                status = "denied"
            elif links and re.search(r"code|ordinance|download|link", low):
                status = "partial"
            else:
                status = "acknowledged"
            t.setdefault("replies", []).append({"at": _now(), "from": frm[:120], "subject": subj[:200], "message_id": mid,
                                                "status": status, "files": files, "links": links, "excerpt": body[:800]})
            self.tracker.log(jid, status, f"reply from {frm[:80]}", **({"files": t.get("files", []) + files} if files else {}))
            out["attachments"] += len(files)
            if costs and "mailer-daemon" not in frm:
                out["cost_notices"] = out.get("cost_notices", 0) + 1
                name = t.get("name", jid)
                note = self.notify(
                    f"Open records: {name} mentions a cost",
                    f"**{name}** (`{jid}`) replied to the Code of Ordinances request with a possible charge. Nothing has "
                    f"been agreed or paid; the request waits for you.\n\n- From: {frm[:120]}\n- Subject: {subj[:200]}\n"
                    f"- Amounts seen: {', '.join(costs) or 'none stated'}\n\n> " + "\n> ".join(body[:1500].splitlines()) +
                    "\n\nReply from the project mailbox to accept, narrow or decline. Tracker: "
                    "`data/openrecords/requests.json`.")
                self.tracker.log(jid, t["status"], f"cost notice: {note}")
        self.tracker.save()
        return out

    def follow_ups(self, smtp_factory=None, after_business_days: int | None = None) -> dict:
        n = int(after_business_days if after_business_days is not None else self.opts.get("follow_up_after_business_days", 5))
        today = dt.datetime.now(dt.timezone.utc).date()
        out = {"due": 0, "sent": 0}
        for jid, t in self.tracker.data.items():
            if t.get("status") != "sent" or t.get("followed_up_at") or not t.get("sent_at") or jid in self.have:
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
            if t.get("status") == "portal_only":
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
            if t.get("status") not in ("sent", "followed_up") or t.get("replies") or not t.get("due"):
                continue
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


_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")
_COST_WORDS = re.compile(r"\b(cost|costs|fee|fees|estimate|estimated|charge|charges|invoice|payment|pay|deposit|"
                         r"per page|prepay|prepayment)\b", re.I)


def mentions_cost(text: str) -> list[str]:
    """Dollar amounts in a reply that talks about paying ([] = no cost mentioned). A reply that talks about
    fees or payment with no amount yet still counts: ['amount not stated']."""
    if not _COST_WORDS.search(text or ""):
        return []
    amounts = list(dict.fromkeys(_MONEY.findall(text)))[:5]
    if amounts:
        return amounts
    low = (text or "").lower()
    return ["amount not stated"] if re.search(r"\b(fee|invoice|payment|deposit|prepay|cost estimate|estimated cost|"
                                              r"will cost|charge of|there is a charge)\b", low) else []


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
