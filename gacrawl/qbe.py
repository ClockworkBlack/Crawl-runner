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


def _shape(x, depth=0):
    if isinstance(x, list):
        return [_shape(x[0], depth + 1)] + ([f"... {len(x)} items"] if len(x) > 1 else []) if x else []
    if isinstance(x, dict) and depth < 4:
        return {k: _shape(v, depth + 1) for k, v in list(x.items())[:40]}
    return x if not isinstance(x, str) else x[:120]


def discover(fetcher, report_ids=(23, 25, 27), log=print) -> dict:
    """What the API answers for these reports: definition, parameters, controls, each control's choices (the first
    and last few), and one generate attempt with the defaults the API gives (its answer's type and size)."""
    q = QBE(fetcher, log)
    out = {"opened": q.open(), "api": q.api}
    if not out["opened"]:
        return out
    out["reports"] = [{k: r.get(k) for k in ("reportID", "value", "reportGroupID", "reportExportType", "path")} for r in q.reports()]
    for rid in report_ids:
        d = out[str(rid)] = {}
        for name in ("GetReportData/", "GetReportParameter/", "GetReportParameterControls/"):
            try:
                d[name] = q.get(name, reportID=rid)
            except Exception as exc:  # noqa: BLE001
                d[name] = f"{type(exc).__name__}: {exc}"
        ctr = d.get("GetReportParameterControls/")
        for c in (ctr if isinstance(ctr, list) else []):
            ex = c.get("executable") or c.get("dataSource") or c.get("storedProcedureName")
            if ex:
                try:
                    ch = q.get("GetParameterDataSource/", executable=ex)
                    c["_choices"] = (ch[:5] + ch[-3:]) if isinstance(ch, list) and len(ch) > 8 else ch
                    c["_choices_n"] = len(ch) if isinstance(ch, list) else None
                except Exception as exc:  # noqa: BLE001
                    c["_choices"] = f"{type(exc).__name__}: {exc}"
        vals = d.get("GetReportParameter/")
        try:
            body, ct = q.generate(d.get("GetReportData/"), vals if isinstance(vals, list) else [])
            d["generate"] = {"type": ct, "bytes": len(body), "start": body[:200].decode("latin-1")}
        except Exception as exc:  # noqa: BLE001
            d["generate"] = f"{type(exc).__name__}: {exc}"[:400]
        time.sleep(1)
    return out
