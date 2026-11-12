"""GaDOE's state funding reports (QBE: Quality Basic Education), from its public report app (qbe.gadoe.org/qbepublic,
an Angular app on GaDOE's finance framework API). GaDOE's redesigned site no longer posts allotment sheets or QBE
reports as files; the app builds them on request. This reader asks the same API the app does:

  GET  <api>/api/Framework/GetUserToken?sessionID=public&applicationID=198     the public session's token
  GET  <api>/api/Reports/GetReportGroupsAndReports/?itApplicationRoleID=896&reportGroupID=0   the reports
  GET  <api>/api/Reports/GetReportData/?reportID=N          the report's definition (sent back to generate it)
  GET  <api>/api/Reports/GetReportParameter/?reportID=N     its parameters (fiscal year, school system ...)
  GET  <api>/api/Reports/GetReportParameterControls/?reportID=N   the form's controls, with their value sources
  GET  <api>/api/Reports/GetParameterDataSource/?executable=X     a control's choices (the years, the systems)
  POST <api>/api/Reports/GenerateReport  {reportParametersValues, reportData}  -> the PDF or spreadsheet

Each report it keeps is a `budget_doc` record (source schools:budgets, kind as the old budgets step made them):
the statewide ones with jurisdiction "ga", a school system's allotment sheet with the district's id, so the Schools
tab lists it with that district. `discover` prints what the API answers (shapes, choices), to build or fix this.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import time

APP = "https://qbe.gadoe.org/qbepublic/"
APP_ID, ROLE_ID = "198", "896"
REFERRER = "Grantswebng"     # the app's own RefererApp header


def _json(r):
    try:
        return json.loads(r.text)
    except ValueError:
        return None


class QBE:
    def __init__(self, fetcher, log=print):
        self.f, self.log = fetcher, log
        self.api, self.headers = "", {}

    def open(self) -> bool:
        s = _json(self.f.get(APP + "assets/appsettings.json", use_cache=False)) or {}
        self.api = (s.get("frameworkapiURL") or "https://finframework.gadoe.org").rstrip("/")
        t = _json(self.f.get(f"{self.api}/api/Framework/GetUserToken", params={"sessionID": "public", "applicationID": s.get("applicationID") or APP_ID},
                             use_cache=False)) or {}
        if not t.get("token"):
            self.log("qbe: the public session gave no token")
            return False
        self.headers = {"Authorization": f"Bearer {t['token']}", "RefererApp": REFERRER, "Accept": "application/json"}
        self.role = s.get("itApplicationRoleID") or ROLE_ID
        return True

    def get(self, path: str, **params):
        r = self.f.get(f"{self.api}/api/Reports/{path}", params=params, headers=self.headers, use_cache=False)
        if r.status != 200:
            raise RuntimeError(f"{path}: HTTP {r.status}")
        return _json(r)

    def reports(self) -> list[dict]:
        return (self.get("GetReportGroupsAndReports/", itApplicationRoleID=self.role, reportGroupID=0) or {}).get("reportNames") or []

    def generate(self, data, values) -> tuple[bytes, str]:
        r = self.f.post(f"{self.api}/api/Reports/GenerateReport", json_body={"reportParametersValues": values, "reportData": data},
                        headers={**self.headers, "Accept": "application/octet-stream, application/json"})
        if r.status != 200:
            raise RuntimeError(f"GenerateReport: HTTP {r.status} {r.text[:200]}")
        return r.content, r.content_type


def _pick(code: str, choices: list) -> str:
    """The default the reader takes for a control: the newest fiscal year, else the first real choice, else the only
    one there is ("-1": All Schools)."""
    allk = [str(c.get("key")) for c in choices if str(c.get("key") or "").strip() not in ("", "None")]
    keys = [k for k in allk if k not in ("-1", "0")]
    if not keys:
        return allk[0] if allk else ""
    if "year" in code.lower():
        nums = [k for k in keys if k.isdigit()]
        return max(nums, key=int) if nums else keys[0]
    return keys[0]


class Form:
    """A report's form, filled the way the app fills it: each control's choices come from its stored procedure, called
    with the values of the controls it depends on and the session's object data."""

    def __init__(self, q: QBE, rid: int):
        self.q, self.rid = q, rid
        self.data = q.get("GetReportData/", reportID=rid)
        self.controls = sorted(q.get("GetReportParameterControls/", reportID=rid) or [], key=lambda c: c.get("sequence") or 0)
        self.object = {"itApplicationRoleID": int(q.role), "reportID": rid}
        self.values: dict[str, str] = {c["controlID"]: "" for c in self.controls}
        self.parents: dict[int, list] = {}

    def choices(self, c: dict) -> list:
        if c["paramID"] not in self.parents:
            self.parents[c["paramID"]] = self.q.get("GetDependentParentParamIDs/", paramID=c["paramID"]) or []
        params = ",".join(f"{p['controlID']}={self.values.get(p['controlID']) or 'null'}" for p in self.parents[c["paramID"]])
        ex = f"{c['storedProcedureName']} {params}{', ' if params else ''}@ObjectData = {json.dumps([self.object], separators=(',', ':'))}"
        return self.q.get("GetParameterDataSource/", executable=ex) or []

    def fill(self, chosen: dict | None = None) -> dict:
        """Every control in order: the given value, else the default. Returns {controlID: choices}."""
        seen = {}
        for c in self.controls:
            ch = self.choices(c) if c.get("storedProcedureName") else []
            seen[c["controlID"]] = ch
            self.values[c["controlID"]] = str((chosen or {}).get(c["controlID"]) or _pick(c.get("code") or "", ch))
        return seen

    def generate(self, export_type: int | None = None) -> tuple[bytes, str]:
        """The report as the app downloads it; export_type 1 PDF, 2 CSV, 3 Excel (the app's own choice list)."""
        data = {**self.data, **({"reportExportTypeID": export_type} if export_type else {})}
        return self.q.generate(data, [{"paramName": k, "paramValue": v} for k, v in self.values.items()])


def _shape(x, depth=0):
    if isinstance(x, list):
        return [_shape(x[0], depth + 1)] + ([f"... {len(x)} items"] if len(x) > 1 else []) if x else []
    if isinstance(x, dict) and depth < 4:
        return {k: _shape(v, depth + 1) for k, v in list(x.items())[:40]}
    return x if not isinstance(x, str) else x[:120]


def discover(fetcher, report_ids=(23, 25, 27), log=print) -> dict:
    """What the API answers for these reports: each control's choices (all of a short list; the first and last of a
    long one), the values the reader would take, and one generated report each (its type, size and first bytes)."""
    q = QBE(fetcher, log)
    out = {"opened": q.open(), "api": q.api}
    if not out["opened"]:
        return out
    out["reports"] = [{k: r.get(k) for k in ("reportID", "value", "reportGroupID", "reportExportType")} for r in q.reports()]
    for rid in report_ids:
        d = out[str(rid)] = {}
        try:
            fm = Form(q, rid)
            seen = fm.fill()
            d["controls"] = [{"controlID": c["controlID"], "code": c.get("code"), "parents": [p.get("controlID") for p in fm.parents.get(c["paramID"], [])],
                              "n": len(seen.get(c["controlID"]) or []),
                              "choices": (seen.get(c["controlID"]) or [])[:12] + (["..."] + (seen.get(c["controlID"]) or [])[-4:]
                                                                                  if len(seen.get(c["controlID"]) or []) > 16 else [])}
                             for c in fm.controls]
            d["values"] = fm.values
            for et in (None, 2):
                try:
                    body, ct = fm.generate(et)
                    d[f"generate_{et or 'default'}"] = {"type": ct, "bytes": len(body), "start": body[:600].decode("utf-8", "replace")}
                except Exception as exc:  # noqa: BLE001
                    d[f"generate_{et or 'default'}"] = f"{type(exc).__name__}: {exc}"[:300]
        except Exception as exc:  # noqa: BLE001
            d["error"] = f"{type(exc).__name__}: {exc}"[:400]
        time.sleep(1)
    # and the nightly reader itself, on a scratch store, for two school systems: what it would keep
    import tempfile
    from pathlib import Path

    from . import config
    from .store import Store

    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp))
        out["run"] = run(fetcher, st, config.ROOT, log=log, per_system_limit=2)
        out["kept"] = [{"title": r.get("title"), "jurisdiction": r.get("jurisdiction"), "format": (r.get("meta") or {}).get("format"),
                        "rows": (r.get("meta") or {}).get("rows"), "text": (r.get("text") or "")[:700]}
                       for r in st.read("schools:budgets", "budget_doc")]
        st.close()
    return out


# the reports kept: statewide (every school system in one table) and one per school system
STATEWIDE = {23: "QBE earnings by school system", 27: "QBE FTE counts by school system"}
PER_SYSTEM = {25: "QBE allotment sheet"}


def _rows_text(rows: list, max_rows: int = 4000) -> tuple[str, int]:
    rows = [[str(c if c is not None else "").strip() for c in r] for r in rows]
    rows = [[c for c in r if c] for r in rows]          # a report laid out for print leaves many empty cells
    rows = [r for r in rows if r]
    return "\n".join("\t".join(r) for r in rows[:max_rows]), len(rows)


def report_text(body: bytes) -> tuple[str, int, str]:
    """(text, rows or pages, kind) of a generated report. The server sends each report in its own format whatever
    the request asks (Nov 12: the allotment sheet as PDF, the earnings and FTE tables as Excel 97 workbooks)."""
    if body[:5] == b"%PDF-":
        import pdfplumber

        with pdfplumber.open(io.BytesIO(body)) as pdf:
            pages = [pg.extract_text() or "" for pg in pdf.pages[:60]]
        return "\n\n".join(pages).strip(), len(pages), "pdf"
    if body[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":            # Excel 97 (an OLE file)
        import xlrd

        wb = xlrd.open_workbook(file_contents=body)
        rows = [sh.row_values(i) for sh in wb.sheets()[:3] for i in range(sh.nrows)]
        rows = [[(int(c) if isinstance(c, float) and c.is_integer() else c) for c in r] for r in rows]
        t, n = _rows_text(rows)
        return t, n, "xls"
    if body[:2] == b"PK":                                               # a newer workbook
        from .schools import parse_xlsx_rows

        t, n = _rows_text(parse_xlsx_rows(body))
        return t, n, "xlsx"
    t, n = _rows_text(list(csv.reader(io.StringIO(body.decode("utf-8-sig", "replace")))))
    return t, n, "csv"


def _systems(root) -> dict[str, str]:
    """GaDOE system code ("601") -> district id ("sd-appling"), from the schools registry's state LEA ids (GA-601)."""
    from .schools import load_registry

    return {(d.get("st_leaid") or "").split("-")[-1].lstrip("0"): did for did, d in (load_registry(root).get("districts") or {}).items()
            if d.get("st_leaid")}


def run(fetcher, store, root, deadline: float | None = None, log=print, per_system_limit: int | None = None) -> dict:
    """The newest fiscal year's QBE reports, each folder (allotment run: Initial, Midterm, Accrual and their amendments)
    read once: the statewide earnings and FTE tables, then each school system's allotment sheet, as many as time
    allows (the rest the next night). Kept as budget_doc records with the report's text (PDF or Excel, as the app exports it)."""
    from .store import Record

    out = {"statewide": 0, "allotment_sheets": 0, "skipped": 0, "errors": []}

    def late():
        return deadline is not None and time.monotonic() > deadline

    q = QBE(fetcher, log)
    if not q.open():
        out["errors"].append("no public session")
        return out
    codes = _systems(root)
    fy_seen = None
    for rid in list(STATEWIDE) + list(PER_SYSTEM):
        if late():
            break
        try:
            fm = Form(q, rid)
            seen = fm.fill({"@CalculationLevelID": "1"})          # system level: one row or sheet per school system
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"report {rid}: {type(exc).__name__}: {exc}"[:200])
            continue
        fy, folder = fm.values.get("@FiscalYear", ""), fm.values.get("@FolderID", "")
        fy_seen = fy_seen or fy
        fname = next((c.get("value") for c in seen.get("@FolderID") or [] if str(c.get("key")) == folder), folder)
        term = next((c.get("value") for c in seen.get("@AllotmentTermID") or [] if str(c.get("key")) == fm.values.get("@AllotmentTermID")), "")
        orgs = [("", "")]
        if rid in PER_SYSTEM:
            orgs = [(str(c.get("key")), str(c.get("value") or "")) for c in seen.get("@OrganizationID") or []
                    if str(c.get("key")) not in ("", "-1")]
            if per_system_limit:
                orgs = orgs[:per_system_limit]
        for org, oname in orgs:
            if late():
                break
            mk = f"schools:qbe:{rid}:{folder}:{org or 'state'}"
            if store.kv_get(mk):
                out["skipped"] += 1
                continue
            if org:
                fm.values["@OrganizationID"] = org
            try:
                body, ct = fm.generate()
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"report {rid} {oname or 'state'}: {type(exc).__name__}: {exc}"[:200])
                if len(out["errors"]) > 8:
                    break
                continue
            try:
                text, nrows, fmt = report_text(body)
            except Exception as exc:  # noqa: BLE001 — kept for the next night (not marked read)
                out["errors"].append(f"report {rid} {oname or 'state'}: unreadable ({type(exc).__name__}: {exc})"[:200])
                continue
            code = oname.split(" - ", 1)[0].strip().lstrip("0") if oname else ""
            did = codes.get(code, "")
            label = (PER_SYSTEM.get(rid) or STATEWIDE.get(rid))
            place = oname.split(" - ", 1)[-1] if oname else "every school system"
            fyl = f"FY{str(fy)[-2:]}"
            title = f"{label}, {fname if str(fname).startswith(fyl) else f'{fyl} {fname}'}: {place}"
            meta = {"label": label, "found_on": APP, "opened": True, "topics": ["Budget and taxes"], "rows": nrows, "format": fmt,
                    "qbe": {"report": rid, "fiscal_year": fy, "term": term, "folder": fname, "system": oname,
                            "how": f"GaDOE QBE public reports: {label}; fiscal year {fy}, {term}, {fname}"
                                   + (f", {oname}" if oname else ", system level")}}
            rec = Record("schools:budgets", "budget_doc", f"{APP}#report={rid}&folder={folder}&system={org or 'all'}", title=title,
                         text=text, channel="api", jurisdiction=did or "ga", evidence="primary", meta=meta)
            store.add(rec)
            store.kv_set(mk, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            out["allotment_sheets" if org else "statewide"] += 1
            time.sleep(0.5)
    store.flush()
    out["fiscal_year"] = fy_seen
    log(f"qbe: {out['statewide']} statewide reports, {out['allotment_sheets']} allotment sheets, {out['skipped']} read before")
    return out
