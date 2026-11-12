"""GaDOE's QBE report app, read through its own API (gacrawl/qbe.py): the form is filled the way the app fills it,
each report is read once per allotment folder, and each school system's allotment sheet is kept with its district.
A fake API throughout; the layouts are the ones the real API answered on Nov 12, 2026."""
import json
from types import SimpleNamespace

from gacrawl import qbe
from gacrawl.store import Store


def _pdf(line: str) -> bytes:
    import io

    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.drawString(72, 720, line)
    c.save()
    return b.getvalue()

CONTROLS = [
    {"paramID": 66, "controlID": "@FiscalYear", "code": "FiscalYear", "storedProcedureName": "uspRSSelectFiscalYear", "sequence": 1},
    {"paramID": 117, "controlID": "@CalculationLevelID", "code": "CalculationLevel", "storedProcedureName": "uspRSSelectCalculationLevel", "sequence": 2},
    {"paramID": 67, "controlID": "@AllotmentTermID", "code": "AllotmentTerm", "storedProcedureName": "uspRSSelectAllotmentTerm", "sequence": 3},
    {"paramID": 68, "controlID": "@FolderID", "code": "Folder", "storedProcedureName": "uspRSSelectFolder", "sequence": 4},
]
ORG = [{"paramID": 70, "controlID": "@OrganizationID", "code": "OrganizationID", "storedProcedureName": "uspRSSelectOrganization", "sequence": 5},
       {"paramID": 71, "controlID": "@OrganizationSchoolID", "code": "OrganizationSchoolID", "storedProcedureName": "uspRSSelectSchool", "sequence": 6}]
CHOICES = {"uspRSSelectFiscalYear": [{"key": "2027", "value": "2027"}, {"key": "2026", "value": "2026"}],
           "uspRSSelectCalculationLevel": [{"key": "1", "value": "System Level"}, {"key": "2", "value": "Site Level"}],
           "uspRSSelectAllotmentTerm": [{"key": "5", "value": "Initial"}],
           "uspRSSelectFolder": [{"key": "35", "value": "FY27 Initial Amendment 5 SB10"}],
           "uspRSSelectOrganization": [{"key": "-1", "value": "State"}, {"key": "37496", "value": "601 - Appling County"},
                                       {"key": "37497", "value": "602 - Atkinson County"}],
           "uspRSSelectSchool": [{"key": "-1", "value": "All School"}]}
PARENTS = {67: ["@CalculationLevelID"], 68: ["@FiscalYear", "@CalculationLevelID", "@AllotmentTermID"], 70: ["@CalculationLevelID"],
           71: ["@CalculationLevelID", "@FolderID", "@OrganizationID"]}


class FakeFetcher:
    def __init__(self):
        self.generated, self.executables = [], []

    def _r(self, body, ct="application/json", status=200):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        return SimpleNamespace(status=status, text=b.decode("utf-8", "replace"), content=b, content_type=ct)

    def get(self, url, params=None, headers=None, use_cache=True, kind=None):
        params = params or {}
        if url.endswith("appsettings.json"):
            return self._r({"frameworkapiURL": "https://api.example.test", "applicationID": "198", "itApplicationRoleID": "896"})
        if url.endswith("GetUserToken"):
            return self._r({"token": "t0k"})
        assert headers and headers["Authorization"] == "Bearer t0k"
        rid = int(params.get("reportID") or 0)
        if url.endswith("GetReportData/"):
            return self._r({"reportID": rid, "reportExportTypeID": 1 if rid == 25 else 3})
        if url.endswith("GetReportParameterControls/"):
            return self._r(CONTROLS + (ORG if rid == 25 else []))
        if url.endswith("GetDependentParentParamIDs/"):
            return self._r([{"controlID": c} for c in PARENTS.get(int(params["paramID"]), [])])
        if url.endswith("GetParameterDataSource/"):
            ex = params["executable"]
            self.executables.append(ex)
            return self._r(CHOICES[ex.split(" ", 1)[0]])
        raise AssertionError(url)

    def post(self, url, data=None, headers=None, json_body=None, kind=None):
        assert url.endswith("/api/Reports/GenerateReport")
        v = {p["paramName"]: p["paramValue"] for p in json_body["reportParametersValues"]}
        self.generated.append((json_body["reportData"]["reportID"], v))
        if json_body["reportData"]["reportID"] == 25:          # the allotment sheet comes as a PDF, whatever is asked
            return self._r(_pdf(f"Allotment sheet {v['@OrganizationID']}: Direct instruction 1,000"), "application/octet-stream")
        return self._r(b"System,Earnings\n601,12000000\n602,5000000\n", "text/csv")


def test_qbe_reports_once_each_and_allotment_sheets_by_district(tmp_path, monkeypatch):
    monkeypatch.setattr(qbe.time, "sleep", lambda s: None)
    monkeypatch.setattr(qbe, "_systems", lambda root: {"601": "sd-appling", "602": "sd-atkinson"})
    f = FakeFetcher()
    store = Store(tmp_path / "data")
    out = qbe.run(f, store, tmp_path, log=lambda *a: None)
    assert out["statewide"] == 2 and out["allotment_sheets"] == 2 and not out["errors"], out
    # the app's own request for a control's choices: its procedure, its parents' values, the session's object data
    assert any(e.startswith("uspRSSelectFolder @FiscalYear=2027,@CalculationLevelID=1,@AllotmentTermID=5, @ObjectData = [")
               for e in f.executables)
    v = dict(f.generated)[25]
    assert v["@FiscalYear"] == "2027" and v["@FolderID"] == "35" and v["@OrganizationSchoolID"] == "-1"
    docs = store.read("schools:budgets", "budget_doc")
    sheets = {d["jurisdiction"]: d for d in docs if d["meta"]["qbe"]["report"] == 25}
    assert set(sheets) == {"sd-appling", "sd-atkinson"} and "Direct instruction 1,000" in sheets["sd-appling"]["text"]
    assert sheets["sd-appling"]["meta"]["format"] == "pdf"
    assert sheets["sd-atkinson"]["title"] == "QBE allotment sheet, FY27 Initial Amendment 5 SB10: Atkinson County"
    assert {d["jurisdiction"] for d in docs if d["meta"]["qbe"]["report"] in (23, 27)} == {"ga"}
    # the next night: nothing read twice
    f2 = FakeFetcher()
    out2 = qbe.run(f2, store, tmp_path, log=lambda *a: None)
    assert out2["skipped"] == 4 and not f2.generated
    store.close()
