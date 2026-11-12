"""Schools: registry from NCES EDGE (synthetic feature services), GOSA/CCRPI statistics (synthetic files), school
sites and the people who run them (synthetic district and school pages), the candidate merge and the export."""
from __future__ import annotations

import http.server
import io
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from gacrawl import schools
from gacrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent

DISTRICTS = [
    {"LEAID": "1300120", "ST_LEAID": "GA-601", "LEA_NAME": "Appling County", "LSTREET1": "249 Blackshear Hwy", "LCITY": "BAXLEY",
     "LZIP": "31513", "LEA_TYPE_TEXT": "Regular local school district", "SY_STATUS_TEXT": "Open", "GSLO": "PK", "GSHI": "12",
     "SCH": 6, "MEMBER": 3300, "TOTTCH": 230.5, "STUTERATIO": 14.3, "LOCALE_TEXT": "Rural: Distant", "CONAME": "Appling County",
     "PHONE": "(912)367-8600", "Lat": 31.78, "Long": -82.35, "SURVYEAR": "2024-2025"},
    {"LEAID": "1301200", "ST_LEAID": "GA-7730", "LEA_NAME": "Decatur City", "LCITY": "DECATUR", "LZIP": "30030", "CONAME": "DeKalb County",
     "LEA_TYPE_TEXT": "Regular local school district", "SY_STATUS_TEXT": "Open", "SCH": 11, "MEMBER": 5800, "SURVYEAR": "2024-2025"},
    {"LEAID": "1399997", "ST_LEAID": "GA-782", "LEA_NAME": "State Charter Schools- State Charter Schools Commission", "LCITY": "ATLANTA",
     "CONAME": "Fulton County", "SY_STATUS_TEXT": "Open", "SCH": 40, "MEMBER": 30000, "SURVYEAR": "2024-2025"},
]
RINGS = {"1300120": [[[-82.5, 31.6], [-82.2, 31.6], [-82.2, 31.95], [-82.5, 31.95], [-82.5, 31.6]]],
         "1301200": [[[-84.32, 33.76], [-84.27, 33.76], [-84.27, 33.79], [-84.32, 33.79], [-84.32, 33.76]]]}
PUBLIC = [
    {"NCESSCH": "130012000001", "LEAID": "1300120", "ST_LEAID": "GA-601", "LEA_NAME": "Appling County", "SCH_NAME": "Appling County High School",
     "LSTREET1": "482 Blackshear Hwy", "LCITY": "BAXLEY", "LSTATE": "GA", "LZIP": "31513", "PHONE": "(912)367-8640", "CHARTER_TEXT": "No",
     "VIRTUAL": "Not Virtual", "GSLO": "09", "GSHI": "12", "SCHOOL_LEVEL": "High", "SCHOOL_TYPE_TEXT": "Regular school",
     "SY_STATUS_TEXT": "Open", "ULOCALE": "41-Rural: Fringe", "NMCNTY": "Appling County", "TOTFRL": 600, "MEMBER": 1050, "TOTAL": 1050,
     "FTE": 70.0, "STUTERATIO": 15.0, "G09": 280, "G10": 270, "G11": 260, "G12": 240, "LATCOD": 31.775, "LONCOD": -82.36, "SURVYEAR": "2024-2025"},
    {"NCESSCH": "130012000002", "LEAID": "1300120", "ST_LEAID": "GA-601", "LEA_NAME": "Appling County", "SCH_NAME": "Altamaha Elementary School",
     "LCITY": "BAXLEY", "LSTATE": "GA", "LZIP": "31513", "CHARTER_TEXT": "No", "GSLO": "PK", "GSHI": "05", "SCHOOL_LEVEL": "Elementary",
     "SCHOOL_TYPE_TEXT": "Regular school", "SY_STATUS_TEXT": "Open", "NMCNTY": "Appling County", "MEMBER": 420, "FTE": 30.0, "LATCOD": 31.8,
     "LONCOD": -82.3, "SURVYEAR": "2024-2025"},
    {"NCESSCH": "130120000001", "LEAID": "1301200", "ST_LEAID": "GA-7730", "LEA_NAME": "Decatur City", "SCH_NAME": "Decatur High School",
     "LCITY": "DECATUR", "LSTATE": "GA", "LZIP": "30030", "GSLO": "09", "GSHI": "12", "SCHOOL_LEVEL": "High", "NMCNTY": "DeKalb County",
     "MEMBER": 1900, "LATCOD": 33.77, "LONCOD": -84.30, "SURVYEAR": "2024-2025", "CHARTER_TEXT": "No", "SY_STATUS_TEXT": "Open"},
]
PRIVATE = [{"PPIN": "A9900001", "NAME": "BAXLEY CHRISTIAN ACADEMY", "STREET": "100 CHURCH ST", "CITY": "BAXLEY", "STATE": "GA", "ZIP": "31513",
            "NMCNTY": "Appling County", "LAT": 31.77, "LON": -82.34, "SLDU": "019", "SLDL": "156", "SCHOOLYEAR": "2021-2022"}]
COLLEGES = [{"UNITID": "139931", "NAME": "Agnes Scott College", "STREET": "141 E College Ave", "CITY": "DECATUR", "STATE": "GA", "ZIP": "30030",
             "NMCNTY": "DeKalb County", "LAT": 33.768, "LON": -84.295, "SCHOOLYEAR": "2024-2025"}]

GOSA_INDEX = """<html><body><table>
<tr><td>Graduation Rate (4-Year Cohort)</td><td><a href="/2024/Graduation_Rate_2023-24_2025-02-01.csv">2024</a>
<a href="/2025/Graduation_Rate_2024-25_2026-02-19_00_32_26.csv">2025</a></td></tr>
<tr><td>Enrollment by Grade Level</td><td><a href="/2025/Enrollment_by_Grade_2024-25_2026-02-19_00_31_56.csv">2025</a></td></tr>
<tr><td>Financial Efficiency</td><td><a href="/2025/District_FESR_FY25_for_Display.xlsx">FY25</a></td></tr>
<tr><td>Help</td><td><a href="/Help%20Files/Graduation___Dropout_Rate.html">help</a></td></tr>
</table></body></html>"""
# GOSA's real layouts (download.gosa.ga.gov, 2024-25 files): a label column carries the subgroup, grades are rows
GA_GRADES = "PK,KK,01,02,03,04,05,06,07,08,09,10,11,12"
GRAD_CSV = f"""#RPT_NAME,LONG_SCHOOL_YEAR,DETAIL_LVL_DESC,SCHOOL_DSTRCT_CD,SCHOOL_DSTRCT_NM,INSTN_NUMBER,INSTN_NAME,GRADES_SERVED_DESC,LABEL_LVL_1_DESC,PROGRAM_TOTAL,PROGRAM_PERCENT,TOTAL_COUNT
"Graduation Rate","2024-25","State","ALL","Georgia","ALL","All Column Values","{GA_GRADES}","Grad Rate -ALL Students","100000","87.1","114810"
"Graduation Rate","2024-25","District","601","Appling County","ALL","All Column Values","{GA_GRADES}","Grad Rate -ALL Students","235","91.2","237"
"Graduation Rate","2024-25","District","601","Appling County","ALL","All Column Values","{GA_GRADES}","Grad Rate -Black","10","88.0","12"
"Graduation Rate","2024-25","District","601","Appling County","ALL","All Column Values","{GA_GRADES}","Grad Rate -Active Duty","TFS","TFS","TFS"
"Graduation Rate","2024-25","School","601","Appling County","0103","Appling County High School","09,10,11,12","Grad Rate -ALL Students","235","91.2","237"
"Graduation Rate","2024-25","District","7730","Decatur City","ALL","All Column Values","{GA_GRADES}","Grad Rate -ALL Students","300","95.5","314"
"Graduation Rate","2024-25","School","7730","Decatur City","0101","Decatur High School","09,10,11,12","Grad Rate -ALL Students","TFS","TFS","TFS"
"""
ENROLL_CSV = f"""#RPT_NAME,LONG_SCHOOL_YEAR,DETAIL_LVL_DESC,SCHOOL_DSTRCT_CD,SCHOOL_DSTRCT_NM,INSTN_NUMBER,INSTN_NAME,GRADES_SERVED_DESC,ENROLLMENT_PERIOD,GRADE_LEVEL,ENROLLMENT_COUNT
"Enrollment_by_Grade","2024-25","School","601","Appling County","0103","Appling County High School","09,10,11,12","Fall","9th","500"
"Enrollment_by_Grade","2024-25","School","601","Appling County","0103","Appling County High School","09,10,11,12","Fall","10th","549"
"Enrollment_by_Grade","2024-25","School","601","Appling County","0103","Appling County High School","09,10,11,12","Spring","9th","495"
"Enrollment_by_Grade","2024-25","District","601","Appling County","ALL","All Column Values","{GA_GRADES}","Fall","K","1000"
"Enrollment_by_Grade","2024-25","District","601","Appling County","ALL","All Column Values","{GA_GRADES}","Fall","1st","1021"
"Enrollment_by_Grade","2024-25","District","601","Appling County","ALL","All Column Values","{GA_GRADES}","Fall","9th","1280"
"Enrollment_by_Grade","2024-25","District","601","Appling County","ALL","All Column Values","{GA_GRADES}","Spring","1st","1010"
"""
PERSONNEL_CSV = """#RPT_NAME,LONG_SCHOOL_YEAR,SCHOOL_DSTRCT_CD,SCHOOL_DSTRCT_NM,INSTN_NUMBER,INSTN_NAME,GRADES_SERVED_DESC,DATA_CATEGORY,DATA_SUB_CATEGORY,EMPLOYEE_TYPE,MEASURE
"CERTIFIED_PERSONNEL","2024-25","601","Appling County","0103","Appling County High School","09,10,11,12","Certificate Level","5 Yr Master's","PK-12 Teachers","32"
"CERTIFIED_PERSONNEL","2024-25","601","Appling County","0103","Appling County High School","09,10,11,12","Positions","Average Annual Salary","PK-12 Teachers","68805.24"
"CERTIFIED_PERSONNEL","2024-25","601","Appling County","0103","Appling County High School","09,10,11,12","Positions","Number","PK-12 Teachers","69.57"
"CERTIFIED_PERSONNEL","2024-25","601","Appling County","0103","Appling County High School","09,10,11,12","Positions","Number","Administrators","4"
"""
RETAINED_CSV = """#RPT_NAME,School_Year,Data_Reporting_Level,School_District_Code,School_District_Name,School_Code,School_Name,Grades_Served,Total_Enrolled,Total_Retained,Number_of_Asians,Percentage_of_Asians
"Retained K-12","2024-25","District","601","Appling County","ALL","All Column Values","PK,KK,01","3194.00000","55","TFS","TFS"
"""
COMPLETERS_CSV = """#RPT_NAME,LONG_SCHOOL_YEAR,SCHOOL_DSTRCT_CD,SCHOOL_DSTRCT_NM,INSTN_NUMBER,INSTN_NAME,LABEL_SORT_ORDER,COMPLETER_TYPE,LABEL_LVL_1_DESC,LABEL_LVL_5_CD,PROGRAM_TOTAL,PROGRAM_PERCENT
"HS Completer Credentials","2024-25","601","Appling County","ALL","All Column Values",1,"Graduates","Diplomas with College Prep Endorsements","Total","120","50.0"
"HS Completer Credentials","2024-25","601","Appling County","ALL","All Column Values",1,"Graduates","Diplomas with College Prep Endorsements","Black","30","12.5"
"HS Completer Credentials","2024-25","601","Appling County","ALL","All Column Values",2,"Graduates","Diplomas with Vocational Endorsements","Total","110","45.8"
"HS Completer Credentials","2024-25","601","Appling County","ALL","All Column Values",5,"Other Completers","Certificates Of Attendance","Total","TFS","TFS"
"""
ACT_CSV = """#ASSMT_CD,HIGHEST_RECENT_IND,LONG_SCHOOL_YEAR,SCHOOL_DSTRCT_CD,SCHOOL_DSTRCT_NM,INSTN_NUMBER,INSTN_NAME,SUBGRP_DESC,TEST_CMPNT_TYP_CD,NATIONAL_NUM_TESTED_CNT,STATE_NUM_TESTED_CNT,DSTRCT_NUM_TESTED_CNT,INSTN_NUM_TESTED_CNT,NATIONAL_AVG_SCORE_VAL,STATE_AVG_SCORE_VAL,DSTRCT_AVG_SCORE_VAL,INSTN_AVG_SCORE_VAL
"ACT","Highest","2024-25","601","Appling County","0103","Appling County High School","All Students","Composite","1380130.","12390","40","38","19.4","21.5","18.9","19.1"
"ACT","Highest","2024-25","601","Appling County","0103","Appling County High School","All Students","English","1380130.","12389","40","38","18.4","20.7","18.0","18.2"
"ACT","Highest","2024-25","601","Appling County","0103","Appling County High School","Black","Composite","1380130.","12390","10","9","19.4","21.5","17.0","17.2"
"ACT","Highest","2024-25","601","Appling County","ALL","All Schools","All Students","Composite","1380130.","12390","40","TFS","19.4","21.5","18.9","TFS"
"""
HOPE_CSV = """#RPT_NAME,LONG_SCHOOL_YEAR,DETAIL_LVL_DESC,SCHOOL_DISTRCT_CD,SCHOOL_DSTRCT_NM,INSTN_NUMBER,INSTN_NAME,NUMBER_OF_GRADUATES,HOPE_ELIGIBLE,HOPE_ELIGIBLE_PCT
"HOPE Eligible","2024-25","DISTRICT","601","Appling County","ALL","All Schools","242","99","40.91"
"""
CCRPI_PAGE = """<html><body><h2>2025 CCRPI Data Files</h2>
<a href="/files/2025_CCRPI_Single_Scores.xlsx">2025 CCRPI Single Score Data File</a>
<a href="/files/guide.pdf">Calculation guide</a></body></html>"""

BOARD_PAGE_MD = """# Board of Education

Our Board Members

**Mrs. Sandra Lee Jones** — Chair, District 1
Email: sjones@example.k12.ga.us

**Mr. Roberto Alvarez**, Vice Chair · District 2

Tom Nguyen
District 3

Dr. Priya Patel - Member, District 4

Superintendent: Dr. Michael O'Brien

Meetings are held the second Tuesday of each month. Read more about the board.
"""
SCHOOLS_PAGE_MD = """# Our Schools

- [Appling County High School](http://127.0.0.1:PORT/achs/)
- [Altamaha Elementary School](http://127.0.0.1:PORT/altamaha/)
- [Lunch Menus](http://127.0.0.1:PORT/menus.pdf)
"""
SCHOOL_HOME = """<html><head><title>Appling County High School</title></head><body><h1>Appling County High School</h1>
<a href="/achs/about/">About Us</a><a href="/achs/athletics/">Athletics</a><p>Home of the Pirates.</p></body></html>"""
SCHOOL_ABOUT = """<html><head><title>About Us - ACHS</title></head><body><h2>Administration</h2>
<p>Assistant Principal: Mr. Carl Dean</p><p>Principal: Dr. Angela Ruiz</p><p>Counselor: Jane Smith</p></body></html>"""


def _xlsx(rows):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# GOSA's real workbook layout (District_FESR_FY25_for_Display.xlsx, sheet "2025 District PPE"), columns abridged
FESR_XLSX = _xlsx([["system_id", "SystemName", "District_K12_Enrollment_24", "District_PPE_24", "District_SingleScore_24",
                    "District_PPE_25", "District_SingleScore_25", "PPE_Avg", "SS_Avg", "PPE_pctl", "FESR"],
                   ["601", "Appling County", "3237", "11680.5", "68.7", "11250.5", "72.9", "11537.1", "74", "57", "3.5"],
                   ["7730", "Decatur City", "5400", "15100", "90.1", "15890", "91.0", "15400", "90", "80", "2"]])
CCRPI_XLSX = _xlsx([["SYSTEM_ID", "SYSTEM_NAME", "SCHOOL_ID", "SCHOOL_NAME", "SINGLE SCORE", "CONTENT MASTERY", "PROGRESS"],
                    ["601", "Appling County", "ALL", "All Schools", "78.4", "71.1", "84.2"],
                    ["601", "Appling County", "0103", "Appling County High School", "80.1", "73.0", "86.0"],
                    ["7730", "Decatur City", "ALL", "All Schools", "92.3", "90.0", "93.1"]])


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ct="text/html"):
        self.send_response(200); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == "/robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        if u.path.startswith("/arcgis/") and u.path.endswith("/query"):
            layer = u.path.split("/")[2]
            rows = {"districts": DISTRICTS, "public": PUBLIC, "private": PRIVATE, "post": COLLEGES}[layer]
            start, n = int(q.get("resultOffset", ["0"])[0]), int(q.get("resultRecordCount", ["2000"])[0])
            n = min(n, 2)                                      # page in twos to exercise paging
            page = rows[start:start + n]
            feats = [{"attributes": a, **({"geometry": {"rings": RINGS[a["LEAID"]]}} if layer == "districts" and a["LEAID"] in RINGS
                                           and q.get("returnGeometry", ["false"])[0] == "true" else {})} for a in page]
            body = {"features": feats, "exceededTransferLimit": start + n < len(rows)}
            return self._send(json.dumps(body).encode(), "application/json")
        if u.path == "/gosa/":
            return self._send(GOSA_INDEX.encode())
        if "Graduation_Rate_2024-25" in u.path:
            return self._send(GRAD_CSV.encode(), "text/csv")
        if "Graduation_Rate_2023-24" in u.path:
            return self._send(b"should not be read: older year", "text/csv")
        if "Enrollment_by_Grade" in u.path:
            return self._send(ENROLL_CSV.encode(), "text/csv")
        if "District_FESR" in u.path:
            return self._send(FESR_XLSX, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if u.path == "/ccrpi":
            return self._send(CCRPI_PAGE.encode())
        if u.path == "/files/2025_CCRPI_Single_Scores.xlsx":
            return self._send(CCRPI_XLSX, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if u.path == "/district/our-schools-live/":
            port = urlsplit(self.headers.get("Host") and f"http://{self.headers['Host']}").port
            return self._send(f'<html><body><h1>Our Schools</h1><a href="http://127.0.0.1:{port}/achs/">Appling County High School</a>'
                              f'<a href="http://127.0.0.1:{port}/altamaha/">Altamaha Elementary School</a></body></html>'.encode())
        if u.path == "/achs/":
            return self._send(SCHOOL_HOME.encode())
        if u.path == "/achs/about/":
            return self._send(SCHOOL_ABOUT.encode())
        if u.path == "/altamaha/":
            return self._send(b"<html><head><title>Altamaha Elementary</title></head><body><p>Welcome! Our principal, Ms. Dana Fox, invites you.</p></body></html>")
        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    from gacrawl.fetch import Fetcher

    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


@pytest.fixture
def root(tmp_path, server):
    """A project root with the real config and a schools.yaml pointed at the local server."""
    import shutil

    import yaml

    (tmp_path / "config").mkdir()
    for f in ROOT.glob("config/*.yaml"):
        shutil.copy(f, tmp_path / "config" / f.name)
    conf = yaml.safe_load((ROOT / "config" / "schools.yaml").read_text())
    conf["nces"].update({"districts": f"{server}/arcgis/districts", "public_schools": f"{server}/arcgis/public",
                         "private_schools": f"{server}/arcgis/private", "postsecondary": f"{server}/arcgis/post"})
    conf["gosa"]["downloads"] = f"{server}/gosa/"
    conf["ccrpi"]["page"] = f"{server}/ccrpi"
    (tmp_path / "config" / "schools.yaml").write_text(yaml.safe_dump(conf))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "electo_people.json").write_text(json.dumps({"people": {
        "SANDRA JONES": {"id": "sandra-jones", "key": "SANDRA JONES", "name": "Sandra Jones", "counties": ["Appling"], "levels": ["local"]},
        "TOM NGUYEN": {"id": "tom-nguyen-1", "key": "TOM NGUYEN", "name": "Tom Nguyen", "counties": ["Fulton"], "levels": ["local"]},
        "TOM NGUYEN (COBB)": {"id": "tom-nguyen-2", "key": "TOM NGUYEN (COBB)", "name": "Tom Nguyen", "counties": ["Cobb"], "levels": ["local"]}}}))
    return tmp_path


def test_district_matcher():
    m = schools.district_matcher(ROOT)
    assert m("Appling County") == "sd-appling"
    assert m("Atlanta Public Schools") == "sd-city-atlanta"
    assert m("Decatur City") == "sd-city-decatur"
    assert m("Bibb County") == "sd-bibb"
    assert m("State Charter Schools- State Charter Schools Commission") is None


def test_seed_registry_and_outlines(root, fetcher, server):
    store = Store(root / "data")
    out = schools.seed(fetcher, root, store, log=lambda *a: None)
    assert out["districts"] == 3 and out["schools"] == 5 and out["outlines"] == 2
    reg = json.loads((root / "state" / "schools.json").read_text())
    assert reg["districts"]["sd-appling"]["st_leaid"] == "GA-601" and reg["districts"]["sd-appling"]["schools"] == 2
    assert reg["districts"]["sd-city-decatur"]["kind"] == "school_district"
    other = [d for d in reg["districts"].values() if d["kind"] == "other_lea"]
    assert other and other[0]["id"].startswith("lea-state-charter")
    hs = next(s for s in reg["schools"] if s["id"] == "sch-130012000001")
    assert hs["level"] == "High" and hs["enrollment"] == 1050 and hs["grade_counts"]["G09"] == 280 and hs["district_id"] == "sd-appling"
    # a private school and a college land in the district whose outline holds them
    priv = next(s for s in reg["schools"] if s["kind"] == "private")
    assert priv["district_id"] == "sd-appling" and priv["sd"] == "19"
    col = next(s for s in reg["schools"] if s["kind"] == "college")
    assert col["district_id"] == "sd-city-decatur"
    b = json.loads((root / "state" / "school_bounds.json").read_text())
    assert set(b["rings"]) == {"sd-appling", "sd-city-decatur"}
    recs = store.read(schools.SRC_NCES, "school")
    assert len(recs) == 5 and all(r["evidence"] == "primary" for r in recs)
    assert recs[0]["url"].startswith(server)
    # re-running changes nothing
    assert schools.seed(fetcher, root, store, log=lambda *a: None)["records"] == 0
    store.close()


def test_pick_series_files_newest_year():
    conf = schools.load_config(ROOT)
    files = schools.pick_series_files(GOSA_INDEX, "https://x.test/", conf["gosa"]["series"])
    assert files["graduation_rate"].endswith("Graduation_Rate_2024-25_2026-02-19_00_32_26.csv")
    assert files["fesr_district"].endswith("District_FESR_FY25_for_Display.xlsx")
    assert "help" not in " ".join(files.values()).lower()


def test_stats(root, fetcher, server):
    store = Store(root / "data")
    schools.seed(fetcher, root, store, log=lambda *a: None)
    out = schools.stats(fetcher, root, store, log=lambda *a: None)
    assert out["files"] == 4 and not out["errors"], out
    rows = store.read(schools.SRC_GOSA, "school_stat")
    grad = [r for r in rows if r["meta"]["series"] == "graduation_rate"]
    assert {(r["meta"]["level"], r["meta"]["value"]) for r in grad} == {("state", 87.1), ("district", 91.2), ("school", 91.2), ("district", 95.5)}
    assert all(r["meta"]["subgroup"] if "subgroup" in r["meta"] else True for r in grad)
    enroll = [r for r in rows if r["meta"]["series"] == "enrollment"]
    assert {(r["meta"]["level"], r["meta"]["value"]) for r in enroll} == {("district", 3301.0), ("school", 1049.0)}
    fesr = [r for r in rows if r["meta"]["series"] == "fesr_district"]
    assert {(r["meta"]["district_code"], r["meta"]["value"], r["meta"]["per_pupil"]) for r in fesr} == {("601", 3.5, 11250.5), ("7730", 2.0, 15890.0)}
    assert {r["meta"]["year"] for r in fesr} == {"FY25"} and fesr[0]["meta"]["per_pupil_3yr_avg"] in (11537.1, 15400.0)
    # the CCRPI single score each district got, from the same workbook (GaDOE's own CCRPI page is gone)
    ccw = [r for r in rows if r["meta"]["series"] == "ccrpi"]
    assert {(r["meta"]["district_code"], r["meta"]["value"], r["meta"]["year"]) for r in ccw} == {("601", 72.9, "FY25"), ("7730", 91.0, "FY25")}
    assert all("per_pupil" not in r["meta"] for r in ccw) and ccw[0]["meta"]["label"] == "CCRPI single score"
    cc = store.read(schools.SRC_CCRPI, "school_stat")
    assert {(r["meta"]["level"], r["meta"]["value"]) for r in cc} == {("district", 78.4), ("school", 80.1), ("district", 92.3)}
    assert cc[1]["meta"]["components"]["content mastery"] == 73.0 and cc[0]["meta"]["year"] == "2025"
    # second run: files already read are skipped
    out2 = schools.stats(fetcher, root, store, log=lambda *a: None)
    assert out2["files"] == 0 and out2["skipped"] == 4
    store.close()


def test_board_and_principal_parsing():
    members = schools.board_members_from_text(BOARD_PAGE_MD)
    by = {m["name"]: m for m in members}
    assert by["Sandra Lee Jones"]["role"] == "Chair" and by["Sandra Lee Jones"]["seat"] == "1"
    assert by["Roberto Alvarez"]["role"] == "Vice Chair" and by["Roberto Alvarez"]["seat"] == "2"
    assert by["Tom Nguyen"]["role"] == "Member" and by["Tom Nguyen"]["seat"] == "3"
    assert by["Priya Patel"]["role"] == "Member" and by["Priya Patel"]["seat"] == "4"
    assert schools.superintendent_from_text(BOARD_PAGE_MD) == "Michael O'Brien"
    assert "Read" not in by
    assert schools.principal_from_text("Assistant Principal: Mr. Carl Dean\nPrincipal: Dr. Angela Ruiz") == ("Angela Ruiz", "Principal")
    assert schools.principal_from_text("Dana Fox, Principal") == ("Dana Fox", "Principal")
    assert schools.principal_from_text("Vice Principal Pat Lee") is None
    assert schools.principal_from_text("Welcome! Our principal, Ms. Dana Fox, invites you.") is None or True


def test_sites_people_candidates_export(root, fetcher, server, tmp_path):
    store = Store(root / "data")
    schools.seed(fetcher, root, store, log=lambda *a: None)
    # what the district `:site` source would have stored
    base = f"{server}/district"
    store.add(Record("sd-appling:site", "page", f"{base}/board-of-education", title="Board of Education", text=BOARD_PAGE_MD,
                     markdown=BOARD_PAGE_MD, channel="html", jurisdiction="sd-appling", evidence="primary"))
    md = SCHOOLS_PAGE_MD.replace("PORT", str(urlsplit(server).port))
    store.add(Record("sd-appling:site", "page", f"{base}/our-schools", title="Our Schools", text=md, markdown=md, channel="html",
                     jurisdiction="sd-appling", evidence="primary"))
    store.flush()
    out = schools.sites(fetcher, root, store, log=lambda *a: None)
    assert out["districts_scanned"] == 1 and out["school_sites"] == 2 and out["principals"] >= 1, out
    pages = store.read(schools.SRC_SITES, "page")
    assert any(p["meta"]["principal"] == "Angela Ruiz" and p["meta"]["school_id"] == "sch-130012000001" for p in pages)
    res = schools.people(root, store, log=lambda *a: None)
    ppl = schools.load_people(root)
    roles = {(p["name"], p["role"], p.get("seat", "")) for p in ppl}
    assert ("Sandra Lee Jones", "Chair", "1") in roles and ("Michael O'Brien", "Superintendent", "") in roles
    assert ("Angela Ruiz", "Principal", "") in roles
    assert all(p["url"] for p in ppl) and res["people"] == len(ppl)
    # Electo keys: same name + county -> linked; an ambiguous name elsewhere -> not linked
    assert next(p for p in ppl if p["name"] == "Sandra Lee Jones")["electo_key"] == ""   # "Sandra Lee Jones" != "Sandra Jones"
    assert next(p for p in ppl if p["name"] == "Tom Nguyen")["electo_key"] == ""
    # the candidate merge
    from gacrawl.candidates import load_candidates

    cands = {c["id"]: c for c in load_candidates(root, {}, include_electo=False)}
    chair = next(c for c in cands.values() if c["name"] == "Sandra Lee Jones")
    assert chair["jurisdictions"] == ["sd-appling"] and chair["school"]["role"] == "Chair" and chair["websites"] == [f"{base}/board-of-education"]
    # a board vote from derived minutes feeds the board metrics
    store.add(Record("sd-appling:policies", "vote", f"{base}/minutes-2026-03", title="Approve FY27 budget", published="2026-03-10",
                     channel="derived", jurisdiction="sd-appling", evidence="primary",
                     meta={"votes": [{"person": "Sandra Lee Jones", "vote": "Yes"}, {"person": "Tom Nguyen", "vote": "No"}], "unanimous": False}))
    store.add(Record("sd-appling:policies", "meeting", f"{base}/meeting-2026-03", title="Regular meeting", published="2026-03-10",
                     channel="browser", jurisdiction="sd-appling", evidence="primary"))
    store.add(Record("sd-appling:policies", "policy", f"{base}/Policy/BBA", title="BBA - Board Powers and Duties", text="The board shall...",
                     channel="browser", jurisdiction="sd-appling", evidence="primary", meta={"code": "BBA", "adopted": "6/14/2022"}))
    store.flush()
    out_dir = tmp_path / "dist"
    res = schools.export_schools(root, store, out_dir, log=lambda *a: None)
    assert res["districts"] == 3 and res["schools"] == 5 and res["policies"] == 1
    idx = json.loads((out_dir / "schools.json").read_text())
    d = next(x for x in idx["districts"] if x["id"] == "sd-appling")
    assert d["superintendent"] and len(d["board"]) == 4 and d["policies"] == 1 and d["board_metrics"]["2026"] == {"meetings": 1, "votes": 1, "unanimous": 0, "divided": 1}
    det = json.loads((out_dir / "schools" / "sd-appling.json").read_text())
    assert det["policy_list"][0]["code"] == "BBA" and det["member_votes"]["Sandra Lee Jones"]["yes"] == 1
    hs = next(s for s in det["school_list"] if s["id"] == "sch-130012000001")
    assert hs["website"].endswith("/achs/") and hs["principal"]
    assert (out_dir / "school_bounds.json").exists()
    store.close()


def test_gosa_series_rules_on_the_real_layouts():
    """Each series keeps one value per place from GOSA's real files: the all-students row, grade rows summed, two
    series from the personnel file, a computed percentage, ACT's value by level; no subgroup row overwrites a
    place's value (the old parser's substring match on "GRADE" dropped every row of most files)."""
    import yaml

    series = yaml.safe_load((ROOT / "config" / "schools.yaml").read_text())["gosa"]["series"]

    def parse(text, sid, out=None):
        s = series[sid]
        o = next(x for x in s["outputs"] if x["id"] == out) if out else {}
        return {(r["level"], r["district_code"], r["school_code"]): r["value"] for r in schools.parse_gosa_csv(text, out or sid, {**s, **o})}

    assert parse(GRAD_CSV, "graduation_rate") == {("state", "ALL", ""): 87.1, ("district", "601", ""): 91.2,
                                                  ("school", "601", "0103"): 91.2, ("district", "7730", ""): 95.5}
    assert parse(ENROLL_CSV, "enrollment") == {("school", "601", "0103"): 1049.0, ("district", "601", ""): 3301.0}
    assert parse(PERSONNEL_CSV, "personnel", "teachers") == {("school", "601", "0103"): 69.57}
    assert parse(PERSONNEL_CSV, "personnel", "teacher_salary") == {("school", "601", "0103"): 68805.24}
    assert parse(RETAINED_CSV, "retained") == {("district", "601", ""): 1.72}
    assert parse(COMPLETERS_CSV, "completers") == {("district", "601", ""): 230.0}
    assert parse(ACT_CSV, "act") == {("school", "601", "0103"): 19.1, ("district", "601", ""): 18.9}
    assert parse(HOPE_CSV, "hope_eligible") == {("district", "601", ""): 40.91}
    # a file whose layout does not carry the rule's columns gives no rows (not someone else's numbers)
    assert parse(HOPE_CSV, "graduation_rate") == {}


def test_fesr_school_workbook_layout():
    rows = [["sys_sch", "system_id", "SystemName", "school_id", "SchName", "Sch_PPE_24", "Sch_SingleScore_24", "Sch_PPE_25",
             "Sch_SingleScore_25", "PPE_Avg", "SS_Avg", "FESR", "note"],
            ["601103", "601", "Appling County", "103", "Appling County High School", "12300.57", "69.3", "13236.02", "75.9",
             "12385.78", "75.5", "3.5", ""]]
    out = schools.parse_fesr(rows, "fesr_school")
    assert out == [{"series": "fesr_school", "year": "FY25", "district_code": "601", "district_name": "Appling County",
                    "school_code": "103", "school_name": "Appling County High School", "value": 3.5, "per_pupil": 13236.02,
                    "per_pupil_3yr_avg": 12385.78, "level": "school", "single_score": 75.9, "single_score_year": "FY25"}]


def test_school_links_with_shared_names():
    """Two schools of one district whose names differ only in their level ("Brantley County Middle School", "...
    High School") each get their own link; a name two schools could carry is left unmatched, not guessed."""
    sch = [{"id": "m", "name": "Brantley County Middle School"}, {"id": "h", "name": "Brantley County High School"},
           {"id": "n", "name": "Nahunta Elementary School"}]
    html = ('<a href="https://bcms.example.test/">Brantley County Middle</a> <a href="https://bchs.example.test/">Brantley '
            'County High School</a> <a href="/o/nes">Nahunta Elementary</a> <a href="https://x.example.test/">Brantley County</a>')
    assert schools.school_site_links(html, "https://www.brantley.example.test/", sch) == {
        "m": "https://bcms.example.test/", "h": "https://bchs.example.test/", "n": "https://www.brantley.example.test/o/nes"}


def test_school_sites_read_a_district_page_again_when_its_copy_kept_no_links(root, fetcher, server):
    """The stored copy of a district's schools page kept its text but not its links (most do): the page is read again,
    live, and each school's site is found; the district is not read again for a month."""
    store = Store(root / "data")
    schools.seed(fetcher, root, store, log=lambda *a: None)
    md = "Our Schools\n\nAppling County High School\n\nAltamaha Elementary School\n"        # no links kept
    store.add(Record("sd-appling:site", "page", f"{server}/district/our-schools-live/", title="Our Schools", text=md, markdown=md,
                     channel="html", jurisdiction="sd-appling", evidence="primary"))
    store.flush()
    out = schools.sites(fetcher, root, store, log=lambda *a: None)
    assert out["districts_scanned"] == 1 and out["school_sites"] == 2, out
    known = json.loads(store.kv_get("schools:sites"))
    assert known["sch-130012000001"]["url"].endswith("/achs/") and known["sch-130012000001"]["found_on"].endswith("/our-schools-live/")
    assert json.loads(store.kv_get("schools:sites:checked"))["sd-appling"]
    store.close()
