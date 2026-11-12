"""Campaign finance: who gives, who receives, and what the organizations are.

Where the money records come from
  imports/finance/*.csv       exports from the Georgia Government Transparency and Campaign Finance
                              Commission's e-filing system (or a clerk's spreadsheet). A file named
                              <candidate-id>.csv is that candidate's; any other file needs a recipient column.
  crawled CCDR documents      Campaign Contribution Disclosure Reports that a city or county posts
                              (doc_type finance_report): the PDF text is parsed into contributions,
                              expenditures, loans and in-kind items. Parsed-from-PDF rows are flagged
                              needs_review (layouts vary).
  FEC API                     federal candidates and PACs: committees, itemized receipts (schedule A),
                              disbursements (schedule B: who a PAC gives to), committee details
                              (type, designation, connected organization, treasurer).
  the Commission's portal     config/finance.yaml `georgia_commission.search_url` is used only when a
                              person has confirmed the URL template (the crawler never guesses one);
                              `probe_commission` lists the links the portal's front page offers so that
                              can be confirmed.

What an organization is (kind organization, one record per resolved entity)
  FEC committee details, ProPublica Nonprofit Explorer (IRS 990 data: subsection, NTEE activity
  code, revenue), OpenCorporates (with an API key: incorporation, officers), the organization's
  own site (config/organizations.yaml websites: the about page and any officer/board list), and
  hand notes in that file. Every field carries the url it came from.

Who runs them / who is part of them: treasurers and connected organizations (FEC), officers
(OpenCorporates, org sites, hand notes). Who receives their money: schedule B for FEC
committees; for Georgia PACs, their own CCDRs when posted or imported.

Everything here is primary evidence (official filings and registries) except org-site text,
which is the organization's own description of itself (evidence primary, basis self-description).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from .registry import write_json_atomic
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import yaml

from .entities import EntityIndex, committee_candidate, is_org, norm_org, norm_person
from .fetch import Blocked, Disallowed, Fetcher, OutOfTime
from .store import Record, Store

FEC = "https://api.open.fec.gov/v1"
PROPUBLICA = "https://projects.propublica.org/nonprofits/api/v2"
OPENCORPORATES = "https://api.opencorporates.com/v0.4"

DATE_RX = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
ISO_RX = re.compile(r"\b(20\d\d|19\d\d)-(\d\d)-(\d\d)\b")
AMOUNT_RX = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?\b")
SECTION_RX = re.compile(r"^\s*(?:(?:schedule|section|part)\s+[A-Z0-9]+[:.\s-]*)?(?P<sec>contributions?(?:\s+received)?|"
                        r"itemized\s+contributions?|in[- ]kind(?:\s+contributions?)?|expenditures?|disbursements?|"
                        r"itemized\s+expenditures?|loans?(?:\s+received)?|investments?|common\s+source|"
                        r"summary|totals?)\b", re.I)
HEADER_FIELDS = {
    "candidate": re.compile(r"\b(?:candidate|filer)(?:'s)?\s+name\s*[:\-]\s*(?P<v>[^\n]{3,80})", re.I),
    "committee": re.compile(r"\b(?:committee|pac|organization)\s+name\s*[:\-]\s*(?P<v>[^\n]{3,100})", re.I),
    "office": re.compile(r"\boffice\s+sought\s*[:\-]\s*(?P<v>[^\n]{3,80})", re.I),
    "period": re.compile(r"\b(?:reporting|report)\s+period\s*[:\-]\s*(?P<v>[^\n]{5,80})", re.I),
    "report_type": re.compile(r"\breport\s+type\s*[:\-]\s*(?P<v>[^\n]{3,80})", re.I),
    "election_year": re.compile(r"\belection\s+year\s*[:\-]\s*(?P<v>20\d\d)", re.I),
}
_VAL = r"(?P<v>.+?)(?=\s{2,}|\s+(?:employer|occupation|date|amount|address|election|description|purpose|city|state|zip)\s*:|\n|\||$)"
LABEL_RX = {
    "name": re.compile(r"(?:contributor|payee|lender|vendor|name)\s*(?:name)?\s*[:\-]\s*" + _VAL, re.I),
    "employer": re.compile(r"employer\s*[:\-]\s*" + _VAL, re.I),
    "occupation": re.compile(r"occupation\s*[:\-]\s*" + _VAL, re.I),
    "address": re.compile(r"address\s*[:\-]\s*" + _VAL, re.I),
    "purpose": re.compile(r"(?:purpose|description)\s*[:\-]\s*" + _VAL, re.I),
    "election": re.compile(r"election\s*[:\-]\s*" + _VAL, re.I),
}
CITY_STATE_RX = re.compile(r"([A-Za-z.' ]{2,40}),?\s+(GA|Georgia|AL|FL|SC|TN|NC|DC|NY|CA|TX|VA)\b\.?\s*(\d{5})?", re.I)
NTEE = {"A": "arts, culture and humanities", "B": "education", "C": "environment", "D": "animal-related",
        "E": "health care", "F": "mental health", "G": "diseases and disorders", "H": "medical research",
        "I": "crime and legal", "J": "employment", "K": "food, agriculture and nutrition", "L": "housing and shelter",
        "M": "public safety and disaster relief", "N": "recreation and sports", "O": "youth development",
        "P": "human services", "Q": "international affairs", "R": "civil rights, social action and advocacy",
        "S": "community improvement and capacity building", "T": "philanthropy and grantmaking",
        "U": "science and technology", "V": "social science", "W": "public and societal benefit",
        "X": "religion", "Y": "mutual and membership benefit", "Z": "unknown"}
SUBSECTION = {"3": "501(c)(3) charity (no political campaign activity allowed)",
              "4": "501(c)(4) social welfare organization (may lobby and do some political activity)",
              "5": "501(c)(5) labor or agricultural organization", "6": "501(c)(6) business league or chamber of commerce",
              "7": "501(c)(7) social or recreational club", "8": "501(c)(8) fraternal beneficiary society",
              "19": "501(c)(19) veterans organization", "27": "527 political organization"}


def _d10(s: str | None) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    m = ISO_RX.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = DATE_RX.search(s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if len(y) == 4 else (2000 + int(y) if int(y) < 70 else 1900 + int(y))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _amount(s: str | None) -> float | None:
    if s is None:
        return None
    t = re.sub(r"[^\d.\-()]", "", str(s))
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


# ----------------------------------------------------------------------------- CSV exports
def parse_finance_csv(path: Path) -> list[dict]:
    """Tolerant reader for campaign-finance exports: finds contributor, amount, date, employer, recipient columns by name."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in reader.fieldnames or []}

        def col(*keys):
            for k in keys:
                for lc, orig in cols.items():
                    if k == lc or k in lc:
                        return orig
            return None

        name_c = col("contributor name", "contributor", "payee", "lender", "name")
        amt_c, date_c = col("amount", "total"), col("date")
        emp_c, occ_c, city_c = col("employer"), col("occupation"), col("city")
        state_c, type_c = col("state"), col("type", "category", "kind")
        rec_c = col("recipient", "candidate name", "candidate", "committee name", "filer")
        purpose_c, elec_c = col("purpose", "description"), col("election")
        for r in reader:
            if not name_c or not (r.get(name_c) or "").strip():
                continue
            amt = _amount(r.get(amt_c)) if amt_c else None
            kind = (r.get(type_c) or "").strip().lower() if type_c else ""
            rows.append({"name": r.get(name_c, "").strip(), "amount": amt, "date": _d10(r.get(date_c)) if date_c else None,
                         "employer": (r.get(emp_c) or "").strip() if emp_c else "",
                         "occupation": (r.get(occ_c) or "").strip() if occ_c else "",
                         "city": (r.get(city_c) or "").strip() if city_c else "",
                         "state": (r.get(state_c) or "").strip() if state_c else "",
                         "kind": _kind_of(kind), "recipient": (r.get(rec_c) or "").strip() if rec_c else "",
                         "purpose": (r.get(purpose_c) or "").strip() if purpose_c else "",
                         "election": (r.get(elec_c) or "").strip() if elec_c else ""})
    return rows


def _kind_of(s: str) -> str:
    s = (s or "").lower()
    if re.search(r"expend|disburse|payment|paid", s):
        return "expenditure"
    if "loan" in s:
        return "loan"
    if "in-kind" in s or "in kind" in s or "inkind" in s:
        return "in_kind"
    return "contribution"


# ----------------------------------------------------------------------------- CCDR text (PDF)
FINANCE_PARSE_V = 2      # 2: blank forms and guides refused, a report must name its filer, form text is no donor
BLANK_FORM = re.compile(r"\b(edition|instructions?|how to (complete|file)|guide(book)?|manual|affidavit|candidate forms|"
                        r"blank form|sample form|frequently asked|this form (is|must)|qualifying (packet|forms))\b", re.I)
NOT_A_DONOR = re.compile(r"\b(commission|campaign finance act|edition|piedmont avenue|state ethics|page \d+ of|"
                         r"instructions?|schedule [a-z]\b|total (contributions|this period))", re.I)


def looks_like_finance_report(title: str, url: str, text: str) -> bool:
    """A filed disclosure report, not the Commission's blank forms, guide or instructions (a city that posts the
    candidate packet posts those too; read as a report, a form's headings became donors)."""
    s = f"{title} {url}".lower()
    head = (text or "")[:3000].lower()
    if BLANK_FORM.search(f"{title} {url}") or BLANK_FORM.search(head[:1500]):
        return False
    if re.search(r"campaign[ _-]?(contribution|finance|disclosure)|contribution[ _-]?disclosure|\bccdr\b", s):
        return True
    return bool(re.search(r"campaign contribution disclosure report|contributions received|itemized contributions", head)
                and re.search(r"\b(contributor|employer|occupation|amount)\b", head))


def header_of(text: str) -> dict:
    head = (text or "")[:4000]
    out = {}
    for k, rx in HEADER_FIELDS.items():
        m = rx.search(head)
        if m:
            out[k] = re.sub(r"\s+", " ", m.group("v")).strip(" :.-")
    return out


def _pick_amount(line: str, avoid: tuple[int, int]) -> tuple[float | None, int, int]:
    """The money amount on a line: prefers $-prefixed / decimal figures, skips the date, zips and street numbers."""
    best = None
    for m in AMOUNT_RX.finditer(line):
        if m.end() > avoid[0] and m.start() < avoid[1]:
            continue
        raw = m.group(0)
        digits = m.group(1)
        if re.fullmatch(r"\d{5}", digits) and not m.group(2):       # zip code
            continue
        tail = line[m.end():m.end() + 12]
        if not m.group(2) and "$" not in raw and "," not in digits and re.match(r"\s+[A-Za-z]", tail) and not re.match(r"\s*(?:general|primary|runoff|special)", tail, re.I):
            continue                                                 # "3200 Presidential Dr": a street number
        v = _amount(raw)
        if v is None or v < 1:
            continue
        strong = "$" in raw or bool(m.group(2)) or "," in digits
        cand = (strong, m.start(), v, m)
        if best is None or cand[:2] > best[:2]:
            best = cand
    return (best[2], best[3].start(), best[3].end()) if best else (None, -1, -1)


def _header_columns(line: str) -> dict | None:
    low = line.lower()
    if "amount" in low and ("name" in low or "contributor" in low or "payee" in low) and ("date" in low):
        return {"occ_emp": "occupation" in low and "employer" in low, "purpose": "purpose" in low or "description" in low,
                "election": "election" in low}
    return None


SKIP_NAME = re.compile(r"^(?:report|reporting|period|date|candidate|office|committee|total|sub-?total|page|balance|sum|cash|"
                       r"beginning|ending|net|name|contributor|payee)\b|:$", re.I)


def parse_ccdr_text(text: str) -> list[dict]:
    """Rows of {name, amount, date, employer, occupation, city, state, kind, purpose, election, line} from report text.
    Two layouts: labeled blocks ('Contributor Name: ... Employer: ... Date: ... Amount: ...') and table rows
    (name ... date ... amount on one line, with employer/occupation on the same or following lines)."""
    rows: list[dict] = []
    section = "contribution"
    cols: dict = {"occ_emp": True, "purpose": True, "election": True}
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    i = 0
    while i < len(lines):
        ln = lines[i]
        sm = SECTION_RX.match(ln)
        if sm and len(ln.strip()) < 60:
            sec = sm.group("sec").lower()
            section = ("expenditure" if re.search(r"expend|disburs", sec) else "loan" if "loan" in sec
                       else "in_kind" if "kind" in sec else "contribution" if "contribution" in sec else section)
            if re.search(r"summary|total|common|investment", sec):
                section = "skip"
            i += 1
            continue
        hc = _header_columns(ln)
        if hc:
            cols = hc
            i += 1
            continue
        if section == "skip" or not ln.strip():
            i += 1
            continue
        lab = LABEL_RX["name"].search(ln)
        if lab and re.match(r"\s*(?:contributor|payee|lender|vendor)", ln, re.I):
            j = i + 1
            while j < len(lines) and j < i + 10 and lines[j].strip() and not re.match(r"\s*(?:contributor|payee|lender|vendor)", lines[j], re.I):
                j += 1
            block = "\n".join(lines[i:j])
            row = {"name": lab.group("v").strip(), "kind": section, "line": i + 1}
            for k in ("employer", "occupation", "address", "purpose", "election"):
                m = LABEL_RX[k].search(block)
                if m:
                    row[k] = m.group("v").strip()
            dm = re.search(r"date[^:\n]*[:\-]\s*([^\n|]+)", block, re.I)
            am = re.search(r"amount[^:\n]*[:\-]\s*\$?\s*([\d,]+(?:\.\d{2})?)", block, re.I)
            row["date"] = _d10(dm.group(1)) if dm else None
            row["amount"] = _amount(am.group(1)) if am else None
            _city_state(row)
            if row["name"] and (row["amount"] is not None or row["date"]):
                rows.append(row)
            i = j
            continue
        dm = DATE_RX.search(ln) or ISO_RX.search(ln)
        if dm:
            amount, a0, a1 = _pick_amount(ln, (dm.start(), dm.end()))
            if amount is not None:
                before = ln[:min(dm.start(), a0)].strip(" |\t-")
                segs = [x.strip() for x in re.split(r"\s{2,}|\t|\|", before) if x.strip()]
                name = segs[0] if segs else ""
                if (not name or len(name) < 3) and i:
                    prev = lines[i - 1].strip()
                    if prev and not DATE_RX.search(prev) and len(prev) < 80 and not SECTION_RX.match(prev):
                        name = re.split(r"\s{2,}|\t|\|", prev)[0].strip()
                        segs = [name] + segs
                if name and not SKIP_NAME.search(name):
                    row = {"name": name.strip(" ,"), "amount": amount, "date": _d10(dm.group(0)), "kind": section,
                           "line": i + 1, "employer": "", "occupation": ""}
                    after = [x.strip() for x in re.split(r"\s{2,}|\t|\|", ln[max(dm.end(), a1):]) if x.strip()]
                    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    extra = []
                    if nxt and not DATE_RX.search(nxt) and not SECTION_RX.match(nxt) and len(nxt) < 120 and (
                            re.search(r"\b(GA|Georgia)\b", nxt) or re.search(r"employer|occupation", nxt, re.I)):
                        extra = [x.strip() for x in re.split(r"\s{2,}|\t|\|", nxt) if x.strip()]
                    for seg in segs[1:] + after + extra:
                        if CITY_STATE_RX.search(seg) and re.search(r"\d", seg):
                            row.setdefault("address", seg)
                        elif cols.get("occ_emp") and "/" in seg and not row["employer"]:
                            occ, emp = (x.strip() for x in seg.split("/", 1))
                            row["occupation"], row["employer"] = occ, emp
                        elif re.match(r"(?:general|primary|runoff|special)\b", seg, re.I):
                            row["election"] = seg
                        elif LABEL_RX["employer"].search(seg):
                            row["employer"] = LABEL_RX["employer"].search(seg).group("v").strip()
                        elif LABEL_RX["occupation"].search(seg):
                            row["occupation"] = LABEL_RX["occupation"].search(seg).group("v").strip()
                        elif section == "expenditure" and not row.get("purpose"):
                            row["purpose"] = seg
                        elif section != "expenditure" and not row["occupation"] and len(seg) < 40 and not re.search(r"\d", seg):
                            row["occupation"] = seg
                    _city_state(row, " ".join(after + extra))
                    rows.append(row)
        i += 1
    return rows


def _city_state(row: dict, extra: str = ""):
    s = " ".join(x for x in (row.get("address", ""), extra) if x)
    m = CITY_STATE_RX.search(s)
    if m:
        row.setdefault("city", m.group(1).strip().title())
        row.setdefault("state", m.group(2).upper()[:2] if len(m.group(2)) == 2 else "GA")


# ----------------------------------------------------------------------------- records
def contribution_record(source_id: str, url: str, row: dict, recipient: str, recipient_kind: str,
                        filing_source: str, jurisdiction: str = "", needs_review: bool = False,
                        report: dict | None = None, channel: str = "import") -> Record:
    name = (row.get("name") or "").strip()
    kind = row.get("kind") or "contribution"
    label = {"contribution": "Contribution", "expenditure": "Expenditure", "loan": "Loan", "in_kind": "In-kind contribution"}[kind]
    title = f"{label}: {name}" + (f" ${row['amount']:,.2f}" if row.get("amount") is not None else "")
    key = f"{source_id}|contribution|{url}|{name.lower()}|{row.get('date') or ''}|{row.get('amount') or ''}|{row.get('line', '')}"
    return Record(source_id, "contribution", url, title=title[:200], text=json.dumps(row, ensure_ascii=False),
                  published=row.get("date"), channel=channel, evidence="primary", jurisdiction=jurisdiction, key=key,
                  meta={"type": kind, "name": name, "amount": row.get("amount"), "date": row.get("date"),
                        "employer": row.get("employer", ""), "occupation": row.get("occupation", ""),
                        "city": row.get("city", ""), "state": row.get("state", ""), "purpose": row.get("purpose", ""),
                        "election": row.get("election", ""), "recipient": recipient, "recipient_kind": recipient_kind,
                        "donor_is_org": is_org(name), "filing_source": filing_source, "needs_review": needs_review,
                        "report": report or {}, "opened": True})


# ----------------------------------------------------------------------------- the Finance runner
def part_read(store) -> dict:
    """{committee id: candidate id or ''}: committees whose FEC read stopped part-way (a place kept in fec:a2r) and was
    not finished since (a place kept for an older starting date than the committee's cursor allows is stale)."""
    with store._lock:
        rows = store.db.execute("SELECT k, v FROM kv WHERE k LIKE 'fec:a2r:%' AND v != ''").fetchall()
    out = {}
    for r in rows:
        com = r[0][8:]
        cursor = store.kv_get(f"fec:a2:{com}") or ""
        try:
            floor = json.loads(r[1]).get("floor") or ""
        except ValueError:
            continue
        if cursor:
            # read in full since: only a later read's place (from the cursor less 60 days) is still current
            try:
                if floor != (dt.date.fromisoformat(cursor) - dt.timedelta(days=60)).isoformat():
                    continue
            except ValueError:
                continue
        with store._lock:
            c = store.db.execute("SELECT json_extract(meta,'$.candidate_id') FROM records WHERE kind='contribution' AND "
                                 "json_extract(meta,'$.committee')=? AND json_extract(meta,'$.candidate_id') IS NOT NULL LIMIT 1",
                                 (com,)).fetchone()
        out[com] = (c[0] if c else "") or ""
    return out


class Finance:
    def __init__(self, root: Path, cfg: dict, fetcher: Fetcher | None, store: Store, log=print):
        self.root, self.cfg, self.f, self.store, self.log = root, cfg, fetcher, store, log
        p = root / "config" / "finance.yaml"
        self.opts = (yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}) if p.exists() else {}
        self.orgs_cfg = self._load_orgs()
        self.entities = EntityIndex(root / "state" / "entities.json", store=store)
        raw = store.kv_get("orgs")                       # organization lookups: when each was last checked
        legacy = root / "state" / "orgs.json"
        self.org_state = json.loads(raw) if raw else (json.loads(legacy.read_text()) if legacy.exists() else {})

    def _load_orgs(self) -> dict:
        p = self.root / "config" / "organizations.yaml"
        d = (yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}) if p.exists() else {}
        out = {}
        for o in d.get("organizations") or []:
            if o.get("name"):
                out[o["name"].lower()] = o
                for a in o.get("aliases") or []:
                    out[a.lower()] = o
        return out

    def flush(self):
        self.entities.flush()
        self.store.kv_set("orgs", json.dumps(self.org_state, ensure_ascii=False, separators=(",", ":")))
        self.store.flush()

    # ------------------------------------------------------------------ 1. imports
    def from_imports(self, candidates: list[dict]) -> int:
        by_id = {c["id"]: c for c in candidates}
        by_name = {}
        for c in candidates:
            for v in c.get("variants") or [c["name"]]:
                by_name[v.lower()] = c
        n = 0
        d = self.root / "imports" / "finance"
        if not d.exists():
            return 0
        for p in sorted(d.glob("*.csv")):
            cand = by_id.get(p.stem)
            for row in parse_finance_csv(p):
                rcp = cand
                if not rcp and row.get("recipient"):
                    rcp = by_name.get(row["recipient"].lower()) or by_name.get((committee_candidate(row["recipient"]) or "").lower())
                recipient = rcp["name"] if rcp else row.get("recipient", "")
                sid = f"cand:{rcp['id']}" if rcp else f"fin:import"
                rec = contribution_record(sid, f"file://{p.name}", row, recipient, "candidate" if rcp else "committee",
                                          "Georgia Government Transparency and Campaign Finance Commission export (import)",
                                          jurisdiction=(rcp or {}).get("jurisdiction", ""))
                if rcp:
                    rec.meta["candidate_id"] = rcp["id"]
                    if rcp.get("electo_key"):
                        rec.meta["electo_key"] = rcp["electo_key"]
                if self.store.add_new(rec):
                    n += 1
        return n

    # ------------------------------------------------------------------ 2. crawled CCDR documents
    def from_documents(self, candidates: list[dict], limit: int = 500) -> dict:
        """Parse posted disclosure reports (documents with doc_type finance_report, or whose text looks like one)."""
        by_name = {}
        for c in candidates:
            for v in c.get("variants") or [c["name"]]:
                by_name[v.lower()] = c
        out = {"reports": 0, "rows": 0}
        # a document parsed by an earlier version of the parser is parsed again (its old lines replaced): a fix to
        # the parser reaches every report already read, not only new ones
        rows = self.store.query("kind='document' AND (meta LIKE '%\"doc_type\": \"finance_report\"%' OR lower(title) LIKE "
                                "'%contribution%' OR lower(title) LIKE '%disclosure%') AND "
                                "coalesce(json_extract(meta,'$.finance_parse_v'),0) < ?", (FINANCE_PARSE_V,), limit=limit)
        for d in rows:
            text = d.get("text") or ""
            meta = dict(d["meta"])
            meta["finance_parsed"] = dt.date.today().isoformat()
            meta["finance_parse_v"] = FINANCE_PARSE_V
            self.store.delete_where("kind='contribution' AND url=? AND json_extract(meta,'$.filing_source')="
                                    "'Campaign Contribution Disclosure Report posted by the jurisdiction'", (d["url"],))
            if looks_like_finance_report(d.get("title") or "", d["url"], text):
                head = header_of(text)
                who = head.get("candidate") or head.get("committee") or ""
                cand = by_name.get(who.lower()) or by_name.get((committee_candidate(who) or "").lower()) if who else None
                # a report names its filer; without one the lines cannot be anyone's money (a blank form, a guide)
                parsed = [r for r in parse_ccdr_text(text) if len(r.get("name") or "") <= 90 and not NOT_A_DONOR.search(r.get("name") or "")] if who else []
                sid = f"cand:{cand['id']}" if cand else d["source_id"]
                for row in parsed:
                    rec = contribution_record(sid, d["url"], row, (cand or {}).get("name") or who,
                                              "candidate" if cand else ("committee" if who else "unknown"),
                                              "Campaign Contribution Disclosure Report posted by the jurisdiction",
                                              jurisdiction=d.get("jurisdiction") or "", needs_review=True, report=head,
                                              channel="pdf")
                    if cand:
                        rec.meta["candidate_id"] = cand["id"]
                        if cand.get("electo_key"):
                            rec.meta["electo_key"] = cand["electo_key"]
                    if self.store.add(rec):
                        out["rows"] += 1
                meta["finance_rows"] = len(parsed)
                meta["finance_filer"] = who
                out["reports"] += 1
            self.store.add(Record(d["source_id"], d["kind"], d["url"], title=d.get("title") or "", text=text,
                                  markdown=d.get("markdown") or "", published=d.get("published"), channel=d.get("channel") or "",
                                  jurisdiction=d.get("jurisdiction") or "", evidence=d.get("evidence") or "", meta=meta,
                                  key=d.get("key")))
        return out

    # ------------------------------------------------------------------ 3. FEC (federal)
    def _fec(self, path: str, params: dict | None = None) -> dict | None:
        if not self.f:
            return None
        key = os.environ.get("FEC_API_KEY", "").strip() or "DEMO_KEY"
        try:
            r = self.f.get(f"{FEC}{path}", params={**(params or {}), "api_key": key}, use_cache=False)
            for wait in (60, 120, 240):
                # the key's hourly allowance used up (two jobs on one key, or a burst): wait for it rather than end the
                # run's federal reading on the first refusal
                if r.status != 429 or key == "DEMO_KEY":
                    break
                self.log(f"  FEC API: the key's hourly allowance is used up; waiting {wait} s")
                time.sleep(wait)
                r = self.f.get(f"{FEC}{path}", params={**(params or {}), "api_key": key}, use_cache=False)
        except (Blocked, Disallowed):
            return None
        if r.status != 200:
            # a refusal is said out loud once a run (it used to read as "nothing found": with no FEC_API_KEY secret
            # the shared DEMO_KEY is refused within a few calls, and no federal money was read at all)
            if r.status in (400, 404, 422):
                # the API turned down this one question (a name or a parameter it cannot take); the others go on.
                # Nov 10: a 422 read "federal money is not read this run" though every other request was answered
                self.fec_bad = getattr(self, "fec_bad", 0) + 1
                if self.fec_bad <= 3:
                    shown = {k: v for k, v in (params or {}).items() if k != "api_key"}
                    self.log(f"  FEC API: HTTP {r.status} for {path} {shown}; this request is skipped")
                return None
            self.fec_refused = getattr(self, "fec_refused", 0) + 1
            if self.fec_refused == 1:
                why = " (no FEC_API_KEY secret: the shared DEMO_KEY allows only a few calls)" if key == "DEMO_KEY" else ""
                self.log(f"::warning title=FEC API::the FEC API answered HTTP {r.status}{why}; federal money is not read this run")
            return None
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return None

    # nicknames campaigns use for the names the FEC files under (Lucy McBath files as "MCBATH, LUCIA KAY")
    NICK = {"lucy": {"lucia", "lucille"}, "hank": {"henry"}, "jim": {"james"}, "jimmy": {"james"}, "rick": {"richard"},
            "buddy": {"earl"}, "mike": {"michael"}, "bill": {"william"}, "bob": {"robert"}, "chip": {"charles"},
            "tom": {"thomas"}, "tony": {"anthony"}, "joe": {"joseph"}, "dan": {"daniel"}, "dave": {"david"},
            "drew": {"andrew"}, "andy": {"andrew"}, "chuck": {"charles"}, "ted": {"edward", "theodore"}, "pat": {"patrick", "patricia"},
            "liz": {"elizabeth"}, "beth": {"elizabeth"}, "kathy": {"katherine", "kathleen"}, "jon": {"jonathan"}, "matt": {"matthew"},
            "greg": {"gregory"}, "ben": {"benjamin"}, "sam": {"samuel"}, "nick": {"nicholas"}, "doug": {"douglas"},
            "barry": {"barry"}, "vic": {"victor"}, "kelly": {"kelly"}, "tim": {"timothy"}, "steve": {"steven", "stephen"}}

    def fec_candidate_ids(self, c: dict) -> list[str]:
        """Every FEC candidate id of this person (a House and a Senate run have separate ids): given in config
        (fec_id / fec_ids), else found once by last name among Georgia candidates and matched on the first name (or
        its nickname, or a shared first three letters), the district when the office names one, and the office; a
        name two different people match is left unmatched (logged) rather than guessed. Remembered 30 days."""
        hand = ((self.opts.get("fec") or {}).get("candidate_ids") or {}) if isinstance(getattr(self, "opts", None), dict) else {}
        given = [x for x in ([c.get("fec_id")] + list(c.get("fec_ids") or []) + list(hand.get(c.get("id"), []) or [])) if x]
        if given:
            return list(dict.fromkeys(given))
        office = (c.get("office") or "").lower()
        if not re.search(r"u\.?s\.? (house|senate|representative|congress)|congress", office):
            return []
        cache = json.loads(self.store.kv_get("fec_ids2") or "{}")
        hit = cache.get(c["id"])
        if hit and hit.get("checked", "") >= (dt.date.today() - dt.timedelta(days=30)).isoformat():
            return hit.get("ids") or []
        parts = [w for w in re.sub(r"[\"'().,]", " ", c["name"]).split() if w.lower() not in ("jr", "sr", "ii", "iii", "iv", "dr", "mr", "ms", "mrs")]
        if len(parts) < 2:
            return []
        last = parts[-1].lower()
        firsts = {w.lower() for w in parts[:-1]} | {a.split()[0].lower() for a in (c.get("aliases") or []) if a.split()}
        firsts |= {n for f in list(firsts) for n in self.NICK.get(f, ())}
        dm = re.search(r"district\s+(\d+)", office)
        want_district = f"{int(dm.group(1)):02d}" if dm else ""
        want_office = "S" if "senate" in office and "state senate" not in office else ("H" if re.search(r"house|representative|congress", office) else "")
        d = self._fec("/candidates/search/", {"q": last, "state": "GA", "per_page": 100})
        if d is None:
            return []
        people: dict[str, dict] = {}
        for r in d.get("results", []):
            nm = (r.get("name") or "").upper()
            if nm.split(",")[0].strip().lower().replace(" ", "") != last.replace("-", "").replace(" ", "") and \
                    nm.split(",")[0].strip().lower() != last:
                continue
            given_names = {w.lower().strip(".'") for w in re.split(r"[\s,]+", nm.split(",", 1)[1] if "," in nm else "") if w}
            first_ok = bool(firsts & given_names) or any(f[:3] == g[:3] for f in firsts for g in given_names if len(f) >= 3 and len(g) >= 3)
            if not first_ok:
                continue
            score = 1
            if want_district and r.get("office") == "H" and (r.get("district") or "") == want_district:
                score += 2
            if want_office and r.get("office") == want_office:
                score += 1
            key = re.sub(r"\b(mr|ms|mrs|dr|jr|sr|iii|ii)\b\.?", "", nm.lower()).strip()
            P = people.setdefault(key.split(",")[0] + "|" + " ".join(sorted(given_names))[:40], {"ids": [], "score": 0, "name": nm})
            P["ids"].append(r["candidate_id"])
            P["score"] = max(P["score"], score)
        ranked = sorted(people.values(), key=lambda P: -P["score"])
        ids: list[str] = []
        if ranked and (len(ranked) == 1 or ranked[0]["score"] > ranked[1]["score"]):
            ids = sorted(set(ranked[0]["ids"]))
        elif ranked:
            self.log(f"  fec {c['id']}: {len(ranked)} FEC candidates match equally ({', '.join(P['name'] for P in ranked[:4])}); "
                     f"set fec_ids in config/candidates.yaml")
        cache[c["id"]] = {"ids": ids, "checked": dt.date.today().isoformat(), "fec_name": ranked[0]["name"] if ids else ""}
        self.store.kv_set("fec_ids2", json.dumps(cache, separators=(",", ":")))
        return ids

    # the fields of an FEC itemized receipt that decide how it counts (memo lines, earmarks, amendments)
    FEC_FIELDS = ("sub_id", "transaction_id", "back_reference_transaction_id", "memo_code", "memo_text", "receipt_type",
                  "receipt_type_full", "line_number", "entity_type", "conduit_name", "conduit_committee_id",
                  "conduit_committee_name", "file_number", "image_number", "amendment_indicator", "report_type",
                  "two_year_transaction_period", "contributor_id", "is_individual", "contribution_receipt_amount")

    def fec(self, c: dict, since: str, deadline: float | None = None) -> int:
        """Itemized receipts (schedule A) of every committee of every FEC id of the candidate, with every field
        that decides how a line counts (memo code, earmark and conduit, amendment, the FEC's own line id). Each line is
        kept under the FEC's own id for it (committee + transaction id, which an amendment keeps), so a line read again
        replaces the earlier copy. Seek pagination, newest first, stopping at a per-committee cursor (60 days of
        overlap). A committee read in full under this scheme drops its lines from before it (read without these
        fields, so no memo line could be told from a gift). Then the committee's official totals per cycle."""
        n = 0
        for cid in self.fec_candidate_ids(c):
            d = self._fec("/committees/", {"candidate_id": cid, "per_page": 20})
            for com in (d or {}).get("results", []):
                role = self.COMMITTEE_ROLE.get((com.get("designation") or "").upper(), "campaign")
                if role == "skip":
                    # a joint fundraising committee (its money reaches the campaign as a transfer, counted there; the
                    # rest goes to the party and other members) or one the candidate does not authorize: not the
                    # candidate's money. Lines kept from it earlier go, so they are neither counted nor left waiting
                    gone = self.store.delete_where("kind='contribution' AND json_extract(meta,'$.committee')=? AND "
                                                   "json_extract(meta,'$.filing_source')='FEC'", (com["committee_id"],))
                    if gone:
                        self.log(f"  fec {com['committee_id']} ({com.get('designation_full') or com.get('designation')}): "
                                 f"{gone} lines removed; not {c['name']}'s campaign money")
                    continue
                n += self._fec_committee(c, com, since, deadline, role=role)
                self._fec_totals(com["committee_id"])
        return n

    # FEC committee designations: P principal campaign committee, A authorized by the candidate (both the campaign's
    # money); D leadership PAC (the candidate's PAC: its money is its own, shown as a committee the candidate runs, not
    # as what the campaign raised); J joint fundraising, U unauthorized, B lobbyist PAC: not the candidate's money
    COMMITTEE_ROLE = {"P": "campaign", "A": "campaign", "D": "leadership", "J": "skip", "U": "skip", "B": "skip"}

    def _fec_committee(self, c: dict, com: dict, since: str, deadline: float | None, role: str = "campaign") -> int:
        com_id = com["committee_id"]
        ck = f"fec:a2:{com_id}"
        rk = f"fec:a2r:{com_id}"              # where a read that ran out of time stopped (resumed next time)
        cursor = self.store.kv_get(ck)
        floor = since[:10]
        if cursor:
            floor = max(floor, (dt.date.fromisoformat(cursor) - dt.timedelta(days=60)).isoformat())
        params = {"committee_id": com_id, "per_page": 100, "sort": "-contribution_receipt_date", "min_date": floor}
        newest, n, complete = cursor or "", 0, False
        try:
            resume = json.loads(self.store.kv_get(rk) or "{}")
        except ValueError:
            resume = {}
        if resume and resume.get("floor") != floor:
            # a place kept for a read from another starting date (the committee was read in full since): stale
            self.store.kv_set(rk, "")
            resume = {}
        if resume.get("floor") == floor and resume.get("last"):
            # a big committee (a Senate campaign files hundreds of thousands of lines) takes several runs: carry on
            # from where the last one stopped instead of starting again from the newest line every time
            params.update(resume["last"])
            newest = max(newest, resume.get("newest") or "")
            self.log(f"  fec {com_id}: resuming the read where it stopped ({resume.get('pages', 0)} pages in)")
        pages = int(resume.get("pages") or 0) if resume.get("floor") == floor else 0
        leader = role == "leadership"
        for _ in range(2000):
            if deadline and time.monotonic() > deadline:
                break
            rr = self._fec("/schedules/schedule_a/", params)
            if rr is None:
                break
            rows = rr.get("results", [])
            for s in rows:
                date = (s.get("contribution_receipt_date") or "")[:10] or None
                newest = max(newest, date or "")
                row = {"name": s.get("contributor_name") or "", "amount": s.get("contribution_receipt_amount"), "date": date,
                       "employer": s.get("contributor_employer") or "", "occupation": s.get("contributor_occupation") or "",
                       "city": s.get("contributor_city") or "", "state": s.get("contributor_state") or "",
                       "kind": "contribution", "line": s.get("sub_id") or ""}
                rec = contribution_record(f"cand:{c['id']}", s.get("pdf_url") or f"{FEC}/schedules/schedule_a/?committee_id={com_id}",
                                          row, (com.get("name") or c["name"]) if leader else c["name"],
                                          "committee" if leader else "candidate", "FEC", jurisdiction=c.get("jurisdiction", ""), channel="api")
                tid = s.get("transaction_id") or s.get("sub_id") or ""
                rec.key = f"fec2|{com_id}|{tid}"
                # a leadership PAC's money is the PAC's (a committee the candidate runs), not what the campaign raised
                rec.meta.update({"candidate_id": None if leader else c["id"], "electo_key": None if leader else c.get("electo_key"),
                                 **({"leadership_pac_of": c["id"], "leadership_pac_of_key": c.get("electo_key")} if leader else {}),
                                 "committee": com_id, "committee_designation": com.get("designation") or "",
                                 "committee_name": com.get("name"), "fec_v": 2,
                                 "contributor_committee_id": s.get("contributor_id")
                                 if (s.get("entity_type") or "") in ("PAC", "COM", "PTY", "ORG", "CCM") else None})
                rec.meta.update({f: s.get(f) for f in self.FEC_FIELDS if s.get(f) not in (None, "")})
                old = self.store.get_meta(rec.key)
                if old is not None:
                    # read again: the FEC's fields come from this reading (an amendment may change them); what the
                    # crawler added to the line since (donor, recipient and employer entities) stays
                    for k, v in old.items():
                        if k not in rec.meta and k.endswith(("_entity", "_of")):
                            rec.meta[k] = v
                if self.store.add(rec):
                    n += 1
            last = (rr.get("pagination") or {}).get("last_indexes") or {}
            pages += 1
            if len(rows) < 100 or not last:
                complete = True
                break
            params = {**params, **last}
            self.store.kv_set(rk, json.dumps({"floor": floor, "last": last, "newest": newest, "pages": pages}))
        if newest and complete:
            self.store.kv_set(ck, newest)
            self.store.kv_set(rk, "")
        if complete and not cursor:
            # this committee is now read in full with the FEC's fields: its lines from the earlier scheme go
            gone = self.store.delete_where("kind='contribution' AND json_extract(meta,'$.committee')=? AND "
                                           "json_extract(meta,'$.filing_source')='FEC' AND key NOT LIKE 'fec2|%'", (com_id,))
            if gone:
                self.log(f"  fec {com_id}: {gone} lines from before the FEC's fields were kept, replaced")
        return n

    def reread_orphans(self, loaded: set, since: str, deadline: float | None) -> int:
        """FEC lines kept before the FEC's own fields that the candidates' own pass did not reach: a committee the FEC
        does not list under the candidate (leadership PACs are listed by sponsor, not candidate), or a person no longer
        researched (a retired member, a candidate no longer on Electo). Read again by their committee id, so they count
        correctly and the re-read can finish (they used to wait forever, and the re-read job ran every six hours).
        `loaded`: candidates to leave to their own pass (empty: every committee still holding old lines)."""
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT json_extract(meta,'$.committee') AS k, json_extract(meta,'$.candidate_id') AS c, "
                "max(json_extract(meta,'$.recipient')) AS r, max(json_extract(meta,'$.committee_name')) AS cn, count(*) AS n "
                "FROM records WHERE kind='contribution' AND json_extract(meta,'$.filing_source')='FEC' AND "
                "json_extract(meta,'$.committee') IS NOT NULL AND json_extract(meta,'$.fec_v') IS NULL GROUP BY k, c ORDER BY n DESC").fetchall()
        n = 0
        for r in rows:
            if deadline and time.monotonic() > deadline:
                break
            if r["c"] in loaded:
                continue
            d = self._fec(f"/committee/{r['k']}/")
            com = ((d or {}).get("results") or [{}])[0] or {}
            com = {"committee_id": r["k"], "name": com.get("name") or r["cn"] or "", "designation": com.get("designation") or ""}
            role = self.COMMITTEE_ROLE.get(com["designation"].upper(), "campaign") if d else "campaign"
            if role == "skip":
                self.store.delete_where("kind='contribution' AND json_extract(meta,'$.committee')=? AND "
                                        "json_extract(meta,'$.filing_source')='FEC'", (r["k"],))
                continue
            c = {"id": r["c"] or f"fec-{r['k']}", "name": r["r"] or com["name"], "jurisdiction": ""}
            self.log(f"  fec {r['k']}: {r['n']} old lines of {c['name']} read again by committee")
            n += self._fec_committee(c, com, since, deadline, role=role)
            self._fec_totals(r["k"])
        return n

    def fec_totals_all(self, deadline: float | None = None) -> int:
        """The official per-cycle totals of every federal committee whose lines are kept (one call each, at most weekly):
        what the Donors tab shows as a federal campaign's money, since the itemized lines leave out gifts under $200 and
        are only as complete as the read. They used to be fetched only after a committee's lines were read in full, so
        most campaigns (Ossoff's Senate committee among them) had none."""
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT DISTINCT json_extract(meta,'$.committee') AS k FROM records WHERE kind='contribution' AND "
                "json_extract(meta,'$.filing_source')='FEC' AND (json_extract(meta,'$.candidate_id') IS NOT NULL OR "
                "json_extract(meta,'$.leadership_pac_of') IS NOT NULL)").fetchall()
        n = 0
        for r in rows:
            if not r["k"] or getattr(self, "fec_refused", 0) or (deadline and time.monotonic() > deadline):
                continue
            before = (json.loads(self.store.kv_get("fec_totals") or "{}").get(r["k"]) or {}).get("checked")
            self._fec_totals(r["k"])
            n += (json.loads(self.store.kv_get("fec_totals") or "{}").get(r["k"]) or {}).get("checked") != before
        if n:
            self.log(f"  fec: official totals of {n} committees fetched")
        return n

    def _fec_totals(self, com_id: str) -> None:
        """The committee's official totals per two-year cycle (receipts, contributions, itemized individual gifts,
        money from other committees), against which what the crawler holds is checked."""
        T = json.loads(self.store.kv_get("fec_totals") or "{}")
        e = T.get(com_id) or {}
        if e.get("checked", "") >= (dt.date.today() - dt.timedelta(days=7)).isoformat():
            return
        d = self._fec(f"/committee/{com_id}/totals/", {"per_page": 20})
        if d is None:
            return
        cyc = {}
        for r in d.get("results", []):
            cyc[str(r.get("cycle"))] = {k: r.get(k) for k in ("receipts", "contributions", "individual_contributions",
                                                              "individual_itemized_contributions", "individual_unitemized_contributions",
                                                              "other_political_committee_contributions", "transfers_from_other_authorized_committee",
                                                              "contribution_refunds", "coverage_start_date", "coverage_end_date",
                                                              "disbursements", "committee_name")}
        T[com_id] = {"checked": dt.date.today().isoformat(), "cycles": cyc}
        self.store.kv_set("fec_totals", json.dumps(T, separators=(",", ":")))

    LINKS = ("donor_entity", "recipient_entity", "employer_entity", "committee_of")

    def misplaced_links(self) -> list[int]:
        """Lines (rids) whose entity ids this database's registry doesn't hold, or holds for a different name.

        Entity ids are a counter kept in each copy of the database, so an id given out anywhere else means nothing, or
        someone else, here: a pass that stopped before saving its registry (Nov 9: the Georgia money step crashed
        partway and about 16,000 donor lines kept ids the next run handed to other people), or a line linked on another
        copy and merged in (the parallel FEC re-read's hand-off carries its own copy's ids). Names are compared the
        way resolution compares them (gacrawl.entities' normal forms), against every name the entity was found under.
        A recipient given by candidate id is the candidate whatever the committee's name, so only its id is checked."""
        ents = self.entities.entities
        known: dict[str, set] = {}

        def names(eid):
            if eid not in known:
                e = ents[eid]
                known[eid] = {x for a in [e.get("name") or ""] + list(e.get("aliases") or [])
                              for x in (norm_person(a), norm_org(a)) if x}
            return known[eid]

        def fits(eid, name):
            if not eid:
                return True
            if eid not in ents:
                return False
            forms = {norm_person(name), norm_org(name)} - {""} if name else set()
            return not forms or bool(forms & names(eid))

        bad = []
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT rid, json_extract(meta,'$.donor_entity'), json_extract(meta,'$.name'), "
                "json_extract(meta,'$.recipient_entity'), json_extract(meta,'$.recipient'), json_extract(meta,'$.candidate_id'), "
                "json_extract(meta,'$.employer_entity'), json_extract(meta,'$.employer'), json_extract(meta,'$.committee_of') "
                "FROM records WHERE kind='contribution' AND meta LIKE '%\"donor_entity\"%'").fetchall()
        for rid, de, dn, rc, rn, cid, ee, en, co in rows:
            if not (fits(de, dn) and fits(rc, None if cid else rn) and fits(ee, en) and fits(co, None)):
                bad.append(rid)
        return bad

    def resolve_all(self, candidates: list[dict]) -> dict:
        """Give every donor, recipient and organization an entity id (meta.donor_entity / recipient_entity), and link
        again every line whose ids this database doesn't hold for it (misplaced_links)."""
        for c in candidates:
            self.entities.add_candidate(c)
        redo = self.misplaced_links()
        if redo:
            self.log(f"  finance: {len(redo)} lines name entity ids this database doesn't hold for them; linking them again")
        n = 0
        try:
            for r in self.store.iter_query("kind='contribution' AND (meta NOT LIKE '%\"donor_entity\"%' OR "
                                           "rid IN (SELECT value FROM json_each(?)))", (json.dumps(redo),)):
                m = dict(r["meta"])
                for k in self.LINKS:                 # a line linked again starts clean (a stale field would linger)
                    m.pop(k, None)
                name = m.get("name") or ""
                kind = "org" if m.get("donor_is_org") else "person"
                cand_name = committee_candidate(name) if kind == "org" else None
                m["donor_entity"] = self.entities.resolve(name, kind, {"employer": m.get("employer"), "city": m.get("city"),
                                                                       "state": m.get("state"), "occupation": m.get("occupation")},
                                                          url=r["url"])
                if cand_name:
                    m["committee_of"] = self.entities.resolve(cand_name, "person", url=r["url"])
                rcp = m.get("recipient") or ""
                if m.get("candidate_id"):
                    m["recipient_entity"] = self.entities.by_candidate(m["candidate_id"])
                elif rcp:
                    m["recipient_entity"] = self.entities.resolve(rcp, "committee" if is_org(rcp) else "person", url=r["url"])
                if m.get("employer") and not m.get("donor_is_org"):
                    m["employer_entity"] = self.entities.resolve(m["employer"], "org", url=r["url"])
                self.store.add(Record(r["source_id"], r["kind"], r["url"], title=r.get("title") or "", text=r.get("text") or "",
                                      published=r.get("published"), channel=r.get("channel") or "", jurisdiction=r.get("jurisdiction") or "",
                                      evidence=r.get("evidence") or "", meta=m, key=r.get("key")))
                n += 1
        finally:
            # Saved even when the loop stops early (an error, or the step's watchdog SIGINT). The store commits the
            # lines as it goes, and they name these entity ids; ids made since the last save would otherwise be lost
            # and handed to other names by the next run (new ids are a counter). Nov 9: the Georgia money step's pass
            # crashed partway (a donor of initials only) without saving, and the next run reused its ids.
            self.entities.flush()
        return {"resolved": n, "relinked": len(redo), "entities": len(self.entities.entities)}

    # ------------------------------------------------------------------ 5. what the organizations are
    REG_V = 2            # the registry lookups' version: a change makes every organization due again
    ORG_RECHECK_DAYS = 30

    def org_donors(self) -> list[tuple[str, float, dict]]:
        """Organizations and committees that gave money, biggest first: (entity id, total given, what the lines say:
        FEC committee ids, states, cities)."""
        tot: dict[str, float] = defaultdict(float)
        seen: dict[str, dict] = {}
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT json_extract(meta,'$.donor_entity'), json_extract(meta,'$.amount'), json_extract(meta,'$.type'), "
                "json_extract(meta,'$.contributor_committee_id'), json_extract(meta,'$.state'), json_extract(meta,'$.city') "
                "FROM records WHERE kind='contribution'").fetchall()
        for eid, amt, typ, cid, st, city in rows:
            if not eid or typ == "expenditure" or not amt or amt <= 0:
                continue
            e = self.entities.entities.get(eid)
            if not e or e.get("kind") not in ("org", "committee"):
                continue
            tot[eid] += amt
            s = seen.setdefault(eid, {"fec": defaultdict(int), "states": defaultdict(int), "cities": defaultdict(int)})
            if cid:
                s["fec"][cid] += 1
            if st:
                s["states"][str(st).upper()[:2]] += 1
            if city:
                s["cities"][str(city).strip().lower()] += 1
        out = []
        for eid, t in tot.items():
            s = seen[eid]
            out.append((eid, t, {"fec": sorted(s["fec"], key=lambda c: -s["fec"][c]),
                                 "state": max(s["states"], key=s["states"].get) if s["states"] else "",
                                 "city": max(s["cities"], key=s["cities"].get) if s["cities"] else ""}))
        out.sort(key=lambda x: -x[1])
        return out

    def enrich_orgs(self, deadline: float | None = None, limit: int | None = None, site_budget: int = 40,
                    api_budget: int = 60) -> dict:
        """What every organization that gave money is, from the bulk registries (gacrawl/registries.py): the FEC's
        committee files (by the FEC id on its money lines, else exact name), the State Ethics Commission's committee
        and candidate registrations, and the IRS's exempt-organization file for its state; then its own website's
        about page when a registry or a hand note gives the site. Biggest donors first; one is marked looked up only
        when every registry it needed could be read (else it is tried again next run)."""
        from . import registries as R

        out = {"due": 0, "done": 0, "with_facts": 0, "fec": 0, "ga": 0, "irs": 0, "site": 0}
        cutoff = (dt.date.today() - dt.timedelta(days=self.ORG_RECHECK_DAYS)).isoformat()
        donors = self.org_donors()
        due = [(eid, t, s) for eid, t, s in donors
               if (self.org_state.get(eid) or {}).get("v") != self.REG_V or (self.org_state.get(eid) or {}).get("checked", "") < cutoff]
        out["donors"], out["due"] = len(donors), len(due)
        if limit:
            due = due[:limit]
        if not due or not self.f:
            return out
        fec = R.FecFiles(self.f, log=self.log).load()
        try:
            ga = R.GaRegistry(R.ga_registry(self.f, self.store, log=self.log))
        except Exception as e:  # noqa: BLE001
            self.log(f"  registries: State Ethics Commission: {type(e).__name__}: {str(e)[:160]}")
            ga = R.GaRegistry([])
        irs = R.IrsFiles(self.f, self.store, log=self.log)
        wanted: dict[str, set] = defaultdict(set)
        for eid, _, s in due:
            e = self.entities.entities[eid]
            # no state on its lines: a Georgia filing (the State Ethics Commission's lines often leave it out)
            st = s["state"] or (e.get("attrs") or {}).get("state") or "GA"
            names = [e["name"]] + list(e.get("aliases") or [])[:6]
            for n in names:
                wanted[st.upper()].add(R.reg_key(n))
            c = fec.match([R.clean(n) for n in names], s["fec"], st.upper()) if fec.ok else None
            if c and R.connected(c.get("connected") or ""):      # a PAC's connected organization is looked for too
                wanted[st.upper()].add(R.reg_key(R.connected(c["connected"])))
        irs.prepare(wanted, deadline)
        n_api = n_site = 0
        try:
            for eid, total, s in due:
                if deadline and time.monotonic() > deadline:
                    break
                try:
                    n_api, n_site = self._enrich_one(eid, total, s, fec, ga, irs, out, n_api, n_site, api_budget, site_budget)
                except OutOfTime:
                    break
                except Exception as exc:  # noqa: BLE001 — one organization's odd registry row never stops the rest
                    self.log(f"  organization {eid}: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            self.flush()
        self.log(f"  organizations: {out['done']} of {out['due']} due looked up ({out['donors']} gave money); "
                 f"{out['with_facts']} found in a registry (FEC {out['fec']}, State Ethics Commission {out['ga']}, IRS {out['irs']}), "
                 f"{out['site']} own sites read")
        return out

    def _enrich_one(self, eid, total, s, fec, ga, irs, out, n_api, n_site, api_budget, site_budget):
        """One organization's lookup (enrich_orgs): its registry facts, an organization record, its state."""
        from . import registries as R

        e = self.entities.entities[eid]
        names = list(dict.fromkeys([R.clean(e["name"])] + [R.clean(a) for a in (e.get("aliases") or [])][:6]))
        st = (s["state"] or (e.get("attrs") or {}).get("state") or "GA").upper()
        info = {"entity_id": eid, "name": names[0], "aliases": names[1:], "org_kind": "committee" if e["kind"] == "committee" else "",
                "what": [], "officers": [], "treasurer": "", "connected_org": "", "website": "", "ein": "", "fec_id": "",
                "city": "", "state": "", "recipients": [], "sources": [], "notes": [], "registries": []}
        hand = self.orgs_cfg.get(names[0].lower()) or next((self.orgs_cfg[a.lower()] for a in names if a.lower() in self.orgs_cfg), None)
        if hand:
            info["website"] = hand.get("website") or ""
            info["org_kind"] = hand.get("kind") or info["org_kind"]
            if hand.get("what"):
                info["what"].append({"text": hand["what"], "src": hand.get("src") or "config/organizations.yaml", "basis": "hand note"})
            for o in hand.get("officers") or []:
                info["officers"].append({"name": o.get("name"), "role": o.get("role", ""), "src": o.get("src") or hand.get("src") or "config/organizations.yaml"})
        from .entities import PAC_WORDS

        complete = fec.ok
        c = fec.match(names, s["fec"], st) if fec.ok else None
        if c:
            what, officers, fields = fec.facts(c)
            info["what"] += what
            info["officers"] += officers
            info.update({k: v for k, v in fields.items() if v})
            info["treasurer"] = officers[0]["name"] if officers else ""
            info["sources"].append({"t": f"FEC committee file ({c['id']})", "u": c["src"], "registry": "fec"})
            info["registries"].append("fec")
            out["fec"] += 1
            if api_budget and n_api < api_budget and os.environ.get("FEC_API_KEY"):
                n_api += 1          # the FEC's own page of the committee: since when it files, its website, whom it pays
                src = f"{FEC}/committee/{c['id']}/"
                det = ((self._fec(f"/committee/{c['id']}/") or {}).get("results") or [{}])[0]
                if det.get("first_file_date"):
                    info["what"].append({"text": f"Filing with the FEC since {det['first_file_date'][:4]}", "src": src,
                                         "basis": "FEC registration"})
                if det.get("custodian_of_records_name"):
                    info["officers"].append({"name": det["custodian_of_records_name"], "role": "custodian of records", "src": src})
                info["website"] = info["website"] or (det.get("website") or "")
                info["recipients"] = self._fec_recipients(c["id"])
        elif not fec.ok and n_api < api_budget and (e["kind"] == "committee" or s["fec"] or PAC_WORDS.search(names[0])):
            # the FEC's files could not be read this run: the API's name search, as before the files (Nov 9)
            n_api += 1
            self._enrich_fec(info)
            if info["fec_id"]:
                info["registries"].append("fec")
                out["fec"] += 1
        g = ga.match(names) if ga.ok else []
        if not ga.ok:
            complete = False
        if g:
            what, officers, fields = R.GaRegistry.facts(g)
            info["what"] += what
            info["officers"] += officers
            info["city"], info["state"] = info["city"] or fields["city"], info["state"] or fields["state"]
            info["sources"].append({"t": "Georgia State Ethics Commission committee registration", "u": what[0]["src"], "registry": "ga_ethics"})
            info["registries"].append("ga_ethics")
            out["ga"] += 1
        if st:
            if not irs.looked_for(R.reg_key(names[0]), st):
                complete = False            # its state's IRS file could not be read this run
            r = irs.match(names, st, s["city"])
            if r:
                what, fields = R.IrsFiles.facts(r)
                info["what"] += what
                info["ein"] = fields["ein"]
                info["city"], info["state"] = info["city"] or fields["city"], info["state"] or fields["state"]
                info["sources"].append({"t": f"IRS exempt organizations, {st} (EIN {fields['ein']})", "u": r["src"], "registry": "irs"})
                info["registries"].append("irs")
                out["irs"] += 1
            elif info["connected_org"]:
                r = irs.match([info["connected_org"]], st)
                if r:
                    what, _ = R.IrsFiles.facts(r, label=f"Connected organization {info['connected_org']}: ")
                    info["what"] += what
                    info["sources"].append({"t": f"IRS exempt organizations, {st} (connected organization)", "u": r["src"], "registry": "irs"})
            if not irs.looked_for(R.reg_key(names[0]), st) and n_api < api_budget and not info["ein"] \
                    and not re.search(r"\b(llc|l\.l\.c|llp|lllp|pllc|p\.c|pc|lp)\b\.?", names[0], re.I):
                # its state's IRS file could not be read this run: ProPublica's copy of the same IRS data, one name
                n_api += 1
                self._enrich_propublica(info)
                if info["ein"]:
                    info["registries"].append("irs")
                    out["irs"] += 1
        self._enrich_opencorporates(info)
        if info["website"] and n_site < site_budget:
            n_site += 1
            try:
                self._enrich_site(info)
                out["site"] += 1
            except OutOfTime:
                raise
            except Exception as exc:  # noqa: BLE001
                info["notes"].append(f"site: {type(exc).__name__}")
        seen = set()
        info["officers"] = [o for o in info["officers"] if o.get("name") and not (o["name"].lower() in seen or seen.add(o["name"].lower()))]
        kind = info["org_kind"] or ("committee" if info["fec_id"] or "ga_ethics" in info["registries"] else "nonprofit" if info["ein"]
                                    else "company" if any(x.get("registry") == "opencorporates" for x in info["sources"]) else "unknown")
        info["org_kind"] = kind
        summary = "; ".join(w["text"] for w in info["what"][:4])
        self.store.add(Record("org", "organization", info["sources"][0]["u"] if info["sources"] else f"entity:{eid}",
                              title=info["name"], text=summary or info["name"], channel="api" if info["sources"] else "derived",
                              evidence="primary", key=f"org|{eid}",
                              meta={**info, "given": round(total, 2), "opened": bool(info["sources"]),
                                    "opened_url": info["sources"][0]["u"] if info["sources"] else ""}))
        if complete:
            self.org_state[eid] = {"checked": dt.date.today().isoformat(), "v": self.REG_V, "kind": kind, "sources": len(info["sources"])}
        e.setdefault("attrs", {})["org_kind"] = kind
        out["done"] += 1
        out["with_facts"] += bool(info["registries"])
        return n_api, n_site

    def org_queue(self, limit: int = 50, recheck_days: int = 90) -> list[str]:
        """Organization entities not yet looked up (or stale), biggest money first."""
        totals: dict[str, float] = {}
        weight = {"donor_entity": 1.0, "recipient_entity": 1.0, "employer_entity": 0.25}   # who gave/received comes first
        for r in self.store.iter_query("kind='contribution'"):
            m = r["meta"]
            for key, w in weight.items():
                eid = m.get(key)
                if eid and self.entities.entities.get(eid, {}).get("kind") in ("org", "committee"):
                    totals[eid] = totals.get(eid, 0) + w * abs(m.get("amount") or 0)
        cutoff = (dt.date.today() - dt.timedelta(days=recheck_days)).isoformat()
        due = [eid for eid in totals if (self.org_state.get(eid) or {}).get("checked", "") < cutoff]
        due.sort(key=lambda e: -totals[e])
        return due[:limit]

    def enrich(self, eid: str) -> dict:
        """Look an organization up everywhere available; write an `organization` record; return it."""
        e = self.entities.entities[eid]
        name = e["name"]
        info = {"entity_id": eid, "name": name, "aliases": e.get("aliases", []), "org_kind": "committee" if e["kind"] == "committee" else "",
                "what": [], "officers": [], "treasurer": "", "connected_org": "", "website": "", "ein": "", "fec_id": "",
                "city": "", "state": "", "recipients": [], "sources": [], "notes": []}
        hand = self.orgs_cfg.get(name.lower()) or next((o for a, o in self.orgs_cfg.items() if a in [x.lower() for x in e.get("aliases", [])]), None)
        if hand:
            info["website"] = hand.get("website") or ""
            info["org_kind"] = hand.get("kind") or info["org_kind"]
            if hand.get("what"):
                info["what"].append({"text": hand["what"], "src": hand.get("src") or "config/organizations.yaml", "basis": "hand note"})
            for o in hand.get("officers") or []:
                info["officers"].append({"name": o.get("name"), "role": o.get("role", ""), "src": o.get("src") or hand.get("src") or "config/organizations.yaml"})
        from .entities import PAC_WORDS

        if e["kind"] == "committee" or PAC_WORDS.search(name):
            self._enrich_fec(info)                  # only committees are registered with the FEC
        if not re.search(r"\b(llc|l\.l\.c|llp|lllp|pllc|p\.c|pc|lp)\b\.?", name, re.I):
            self._enrich_propublica(info)           # for-profit legal forms are never IRS-exempt organizations
        self._enrich_opencorporates(info)
        if info["website"]:
            self._enrich_site(info)
        kind = info["org_kind"] or ("committee" if info["fec_id"] else "nonprofit" if info["ein"] else "company" if any(
            s.get("registry") == "opencorporates" for s in info["sources"]) else "unknown")
        info["org_kind"] = kind
        summary = "; ".join(w["text"] for w in info["what"][:4])
        rec = Record("org", "organization", info["sources"][0]["u"] if info["sources"] else f"entity:{eid}",
                     title=name, text=summary or name, channel="api" if info["sources"] else "derived", evidence="primary",
                     key=f"org|{eid}", meta={**info, "opened": bool(info["sources"]), "opened_url": info["sources"][0]["u"] if info["sources"] else ""})
        self.store.add(rec)
        self.org_state[eid] = {"checked": dt.date.today().isoformat(), "kind": kind, "sources": len(info["sources"])}
        e["attrs"]["org_kind"] = kind
        return info

    def _enrich_fec(self, info: dict):
        name = info["name"]
        d = self._fec("/committees/", {"q": name, "per_page": 5})
        from .entities import norm_org

        hit = next((r for r in (d or {}).get("results", []) if norm_org(r.get("name") or "") == norm_org(name)), None)
        if not hit:
            return
        cid = hit["committee_id"]
        det = (self._fec(f"/committee/{cid}/") or {}).get("results", [{}])
        det = det[0] if det else {}
        src = f"{FEC}/committee/{cid}/"
        info["fec_id"] = cid
        info["sources"].append({"t": f"FEC committee {cid}", "u": src, "registry": "fec"})
        what = ", ".join(x for x in (det.get("committee_type_full"), det.get("designation_full"), det.get("organization_type_full"),
                                     det.get("party_full")) if x)
        if what:
            info["what"].append({"text": f"Registered with the FEC as a {what}", "src": src, "basis": "FEC registration"})
        since = (det.get("first_file_date") or "")[:4]
        place = ", ".join(x for x in ((det.get("city") or "").title(), det.get("state") or "") if x)
        if since or place:
            info["what"].append({"text": "Filing with the FEC" + (f" since {since}" if since else "") + (f"; based in {place}" if place else ""),
                                 "src": src, "basis": "FEC registration"})
        if det.get("affiliated_committee_name"):
            info["what"].append({"text": f"Affiliated committee: {det['affiliated_committee_name']}", "src": src, "basis": "FEC registration"})
        info["connected_org"] = det.get("connected_organization_name") or ""
        info["treasurer"] = det.get("treasurer_name") or ""
        if info["treasurer"]:
            info["officers"].append({"name": info["treasurer"], "role": "treasurer", "src": src})
        if det.get("custodian_of_records_name"):
            info["officers"].append({"name": det["custodian_of_records_name"], "role": "custodian of records", "src": src})
        info["city"], info["state"] = det.get("city") or "", det.get("state") or ""
        info["website"] = info["website"] or (det.get("website") or "")
        info["recipients"] += self._fec_recipients(cid)

    def _fec_recipients(self, cid: str) -> list[dict]:
        """Who receives a committee's money in Georgia (FEC schedule B: its newest 100 payments to GA recipients)."""
        rr = self._fec("/schedules/schedule_b/", {"committee_id": cid, "per_page": 100, "sort": "-disbursement_date",
                                                 "recipient_state": "GA"})
        return [{"name": s.get("recipient_name"), "amount": s.get("disbursement_amount"),
                 "date": (s.get("disbursement_date") or "")[:10], "purpose": s.get("disbursement_description"),
                 "src": s.get("pdf_url") or f"{FEC}/schedules/schedule_b/?committee_id={cid}"}
                for s in (rr or {}).get("results", [])[:100]]

    def _enrich_propublica(self, info: dict, name: str | None = None, label: str = ""):
        """IRS exempt-organization data for the org itself, or (label set) for its connected organization."""
        if not self.f:
            return
        from .entities import norm_org

        name = name or info["name"]
        try:
            r = self.f.get(f"{PROPUBLICA}/search.json", params={"q": name, "state[id]": "GA"}, use_cache=False)
        except (Blocked, Disallowed):
            return
        if r.status != 200:
            return
        try:
            orgs = r.json().get("organizations", [])
        except Exception:  # noqa: BLE001
            return
        hit = next((o for o in orgs if norm_org(o.get("name") or "") == norm_org(name)), None)
        if not hit and orgs and norm_org(orgs[0].get("name") or "").startswith(norm_org(name)):
            hit = orgs[0]
        if not hit:
            if not label and info.get("connected_org") and norm_org(info["connected_org"]) != norm_org(name):
                self._enrich_propublica(info, info["connected_org"], label=f"Connected organization {info['connected_org']}: ")
            return
        ein = hit.get("ein")
        src = f"{PROPUBLICA}/organizations/{ein}.json"
        if not label:
            info["ein"] = str(ein)
        info["sources"].append({"t": f"ProPublica Nonprofit Explorer (IRS data) EIN {ein}", "u": src, "registry": "propublica"})
        sub = str(hit.get("subseccd") or hit.get("subsection_code") or "")
        ntee = (hit.get("ntee_code") or "")[:1].upper()
        parts = []
        if sub in SUBSECTION:
            parts.append(SUBSECTION[sub])
        if ntee in NTEE:
            parts.append(f"activity area: {NTEE[ntee]} (NTEE {hit.get('ntee_code')})")
        if parts:
            info["what"].append({"text": label + "IRS-registered nonprofit: " + "; ".join(parts), "src": src,
                                 "basis": "IRS Business Master File via ProPublica"})
        if not label:
            info["city"] = info["city"] or (hit.get("city") or "")
            info["state"] = info["state"] or (hit.get("state") or "")
        try:
            rr = self.f.get(src, use_cache=False)
            if rr.status == 200:
                filings = rr.json().get("filings_with_data", [])
                if filings:
                    f0 = filings[0]
                    info["what"].append({"text": label + f"Most recent IRS filing (tax year {f0.get('tax_prd_yr')}): revenue "
                                                 f"${(f0.get('totrevenue') or 0):,.0f}, expenses ${(f0.get('totfuncexpns') or 0):,.0f}",
                                         "src": f0.get("pdf_url") or src, "basis": "Form 990"})
        except (Blocked, Disallowed):
            pass
        except Exception:  # noqa: BLE001
            pass

    def _enrich_opencorporates(self, info: dict):
        key = os.environ.get("OPENCORPORATES_API_KEY")
        if not key or not self.f:
            return
        from .entities import norm_org

        try:
            r = self.f.get(f"{OPENCORPORATES}/companies/search", params={"q": info["name"], "jurisdiction_code": "us_ga",
                                                                         "api_token": key, "per_page": 5}, use_cache=False)
        except (Blocked, Disallowed):
            return
        if r.status != 200:
            return
        try:
            comps = [c["company"] for c in r.json()["results"]["companies"]]
        except Exception:  # noqa: BLE001
            return
        hit = next((c for c in comps if norm_org(c.get("name") or "") == norm_org(info["name"])), None)
        if not hit:
            return
        src = hit.get("opencorporates_url") or f"{OPENCORPORATES}/companies/us_ga/{hit.get('company_number')}"
        info["sources"].append({"t": f"Georgia corporate registry via OpenCorporates ({hit.get('company_number')})", "u": src,
                                "registry": "opencorporates"})
        info["what"].append({"text": f"Georgia-registered {hit.get('company_type') or 'company'} ({hit.get('current_status') or 'status unknown'}),"
                                     f" formed {hit.get('incorporation_date') or '?'}; registered address {hit.get('registered_address_in_full') or '?'}",
                             "src": src, "basis": "corporate registry"})
        try:
            rr = self.f.get(f"{OPENCORPORATES}/companies/us_ga/{hit.get('company_number')}", params={"api_token": key}, use_cache=False)
            if rr.status == 200:
                for o in rr.json()["results"]["company"].get("officers", []):
                    oo = o.get("officer", o)
                    info["officers"].append({"name": oo.get("name"), "role": oo.get("position", ""), "src": src})
        except (Blocked, Disallowed):
            pass
        except Exception:  # noqa: BLE001
            pass

    def _enrich_site(self, info: dict):
        """The organization's own 'about' page: what it says it does and any listed officers/board."""
        if not self.f:
            return
        from . import extract

        try:
            r = self.f.get(info["website"])
        except (Blocked, Disallowed):
            info["notes"].append("site not crawlable")
            return
        except Exception:  # noqa: BLE001
            return
        if r.status != 200 or "html" not in (r.content_type or ""):
            return
        soup = extract.soup_of(r.text)
        about_url = None
        for a in soup.find_all("a", href=True):
            t = (a.get_text(" ", strip=True) or "").lower()
            if re.search(r"\babout\b|mission|who we are|leadership|board|our team|staff|officers", t):
                from urllib.parse import urljoin

                about_url = urljoin(r.url, a["href"])
                break
        page = r
        if about_url and about_url != r.url:
            try:
                page = self.f.get(about_url)
            except Exception:  # noqa: BLE001
                page = r
        text = extract.main_text(page.text) if page.status == 200 else ""
        if text:
            info["sources"].append({"t": f"{info['name']} — about page", "u": page.url, "registry": "site"})
            info["what"].append({"text": re.sub(r"\s+", " ", text)[:700], "src": page.url, "basis": "self-description"})
            for m in re.finditer(r"([A-Z][a-z]+(?:\s+[A-Z][a-z.]+){1,3})\s*[,–\-]\s*((?:Executive\s+)?(?:President|Chair(?:man|woman|person)?|"
                                 r"Vice\s+Chair|Treasurer|Secretary|Director|CEO|Founder|Board\s+Member|Trustee)[^,.\n]{0,40})", text):
                info["officers"].append({"name": m.group(1), "role": m.group(2).strip(), "src": page.url})
            for m in re.finditer(r"((?:Executive\s+)?(?:President|Chair(?:man|woman|person)?|Treasurer|Secretary|CEO|Founder))\s*[:–\-]\s*"
                                 r"([A-Z][a-z]+(?:\s+[A-Z][a-z.]+){1,3})", text):
                info["officers"].append({"name": m.group(2), "role": m.group(1).strip(), "src": page.url})
        seen = set()
        info["officers"] = [o for o in info["officers"] if o.get("name") and not (o["name"].lower() in seen or seen.add(o["name"].lower()))]

    # ------------------------------------------------------------------ 6. the commission's portal (confirmed URLs only)
    def probe_commission(self) -> dict:
        """Open the Commission's home page and list the search/report links it offers, for a person to confirm."""
        gc = self.opts.get("georgia_commission") or {}
        home = gc.get("home")
        out = {"home": home, "links": [], "checked": dt.date.today().isoformat()}
        if not home or not self.f:
            return out
        from . import extract
        from urllib.parse import urljoin

        try:
            r = self.f.get(home)
        except (Blocked, Disallowed) as exc:
            out["error"] = str(exc)
            return out
        if r.status == 200 and "html" in (r.content_type or ""):
            soup = extract.soup_of(r.text)
            for a in soup.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                h = urljoin(r.url, a["href"])
                if re.search(r"search|efile|e-file|report|view|campaign|disclosure|lobby|data", f"{t} {h}", re.I):
                    out["links"].append({"text": t[:80], "url": h})
        (self.root / "state").mkdir(exist_ok=True)
        write_json_atomic((self.root / "state" / "finance_portal.json"), out, indent=1)
        return out

    def commission_search(self, c: dict) -> int:
        """Only with a person-confirmed search_url template in config/finance.yaml (placeholders {last} {first})."""
        gc = self.opts.get("georgia_commission") or {}
        tpl = gc.get("search_url")
        if not tpl or not gc.get("confirmed") or not self.f:
            return 0
        parts = c["name"].split()
        url = tpl.format(last=parts[-1], first=parts[0], name=c["name"])
        try:
            r = self.f.get(url)
        except (Blocked, Disallowed):
            return 0
        if r.status != 200:
            return 0
        text = r.text if "html" in (r.content_type or "") else ""
        if not text:
            return 0
        from . import extract

        rows = parse_ccdr_text(extract.main_text(text))
        n = 0
        for row in rows:
            rec = contribution_record(f"cand:{c['id']}", r.url, row, c["name"], "candidate",
                                      "Georgia Government Transparency and Campaign Finance Commission", needs_review=True,
                                      jurisdiction=c.get("jurisdiction", ""), channel="html")
            rec.meta["candidate_id"] = c["id"]
            if self.store.add_new(rec):
                n += 1
        return n

    # ------------------------------------------------------------------ run
    def run(self, candidates: list[dict], since: str = "2016-01-01", deadline: float | None = None,
            org_limit: int = 40, orgs: bool = True) -> dict:
        # the copies of FEC lines and Commission exports candidate research once kept as `connection` records, without
        # the FEC's memo codes (candidates._connections no longer makes them; money is counted from these records only)
        junk = self.store.delete_where("kind='contribution' AND json_extract(meta,'$.filing_source')="
                                       "'Campaign Contribution Disclosure Report posted by the jurisdiction' AND "
                                       "coalesce(json_extract(meta,'$.recipient'),'')=''")
        if junk:
            self.log(f"  finance: {junk} lines read from posted forms that name no filer removed (blank forms and guides)")
        gone = self.store.delete_where("kind='connection' AND json_extract(meta,'$.type')='contribution'")
        if gone:
            self.log(f"  finance: {gone} duplicate money copies from candidate research removed")
        out = {"imported": self.from_imports(candidates)}
        out["documents"] = self.from_documents(candidates)
        fec_n = 0
        if self.f:
            for c in candidates:
                if deadline and time.monotonic() > deadline:
                    break
                if c.get("fec_id") or re.search(r"u\.?s\.? (house|senate)|congress", (c.get("office") or "").lower()):
                    try:
                        fec_n += self.fec(c, since, deadline)
                    except OutOfTime:
                        break                  # the FEC host's next turn is past the budget: done for this run
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"  fec {c['id']}: {type(exc).__name__}: {exc}"[:200])
                if (self.opts.get("georgia_commission") or {}).get("confirmed"):
                    try:
                        out["commission"] = out.get("commission", 0) + self.commission_search(c)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"  commission {c['id']}: {type(exc).__name__}: {exc}"[:200])
        out["fec"] = fec_n
        if self.f:
            try:
                out["fec_totals"] = self.fec_totals_all(deadline)
            except OutOfTime:
                pass
            except Exception as exc:  # noqa: BLE001
                self.log(f"  fec totals: {type(exc).__name__}: {exc}"[:200])
        if getattr(self, "fec_refused", 0):
            out["fec_refused_calls"] = self.fec_refused
        out.update(self.resolve_all(candidates))
        if not orgs:
            # the FEC re-read: what the organizations are is the model job's (gacrawl finance orgs); here the lookups
            # held every re-read run to its last minute, and the corrected money reached the site five hours late
            self.flush()
            return out
        try:
            org = self.enrich_orgs(deadline, limit=org_limit or None)
            out["organizations"], out["organization_lookups"] = org.get("done", 0), org
        except OutOfTime:
            out["organizations"] = "stopped: out of time"
        self.flush()
        return out
