"""One name per committee and one sitting per committee and day (gacrawl/agencies.py: _committee, committee_name,
gwcca_label, gwcca_about, READER_REV and what run withdraws; config/agencies.yaml: a body's `committees`).

Nov. 20, 2026: the first reading of the whole page of the Georgia World Congress Center Authority (the Internet
Archive's copy of May 1, 2026, fetched by the runner's probe) filed 376 meetings under 33 names. One committee stood
under four ("Executive Finance Committee", "Executive_Finance Committee", "Joint Finance Executive Committee", "Joint
GWCCA Executive & Finance Committee"), and 19 sittings were split in two: the agenda under one spelling, the minutes
under another ("Conference Call", "Special Meeting", "Prelimanary", "Board Executive Committee", "COP"). Files were
passed over for how their label or address is written: the board's minutes of Jan. 27, 2026 (an address with one
quotation mark too many), an agenda ("12-10-24-GWCCA ..."), seven retreats' minutes ("09-16 & 17-21 ...").

The test page (fixtures/agencies/gwcca_labels.html) is that copy's schedule and the file links of the 76 days
concerned, in the page's order, with eight links whose labels carry no meeting's day."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gacrawl import agencies as A
from gacrawl.store import Record, Store

FX = Path(__file__).parent / "fixtures" / "agencies"
ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.gwcca.org/about"
TS = "20260501095954"
COPY = f"https://web.archive.org/web/{TS}/{URL}"
BODIES = yaml.safe_load((ROOT / "config" / "agencies.yaml").read_text())["agencies"]
GW = next(a for a in BODIES if a["id"] == "gwcca")
PAGE = (FX / "gwcca_labels.html").read_text()
# the file links of the test page that belong to no meeting's day: a notice and agendas with no day, lists, a roster
NO_DAY = {"Call for Special Meeting of Executive Committee Notice", "2025 BOG Retreat Preliminary Meeting Agenda",
          "2024 BOG Planning Retreat Preliminary Agenda", "2020 Board Meeting Schedule", "2017 GWCCA Board Committee Assignments",
          "2016 Authority - Name only - as of 07-27-16", "March 2013 Authority Meeting Power Point"}
THE_LIST = "07-10-15 Committee Assignments by Committee"


def read(page=PAGE, ag=GW):
    return A.gwcca_about(page, URL, dict(ag), {"u": URL})


def on(ms, date):
    return sorted((m for m in ms if m["date"] == date), key=lambda m: (m["body"], m["mk"]))


def files_of(ms):
    return {m[s] for m in ms for s in A.SLOTS if m[s]} | {x["u"] for m in ms for x in m["docs"]}


def file_links(html):
    """Every link of the page to a file, by its address alone: (label, address as written)."""
    return [(label, href) for ln in A.page_lines(html, URL) for label, href in ln["links"]
            if re.search(r"\.(?:pdf|docx?|pptx?)", href, re.I) or "acrobat.adobe.com" in href]


def page_of(*labels):
    """A small page in the Authority's layout with these file labels."""
    return ("<html><body><main><h3>Meeting Minutes and Materials</h3><ul>"
            + "".join(f'<li><a href="https://www.gwcca.org/wp-content/uploads/f{i}.pdf">{lab}</a></li>' for i, lab in enumerate(labels))
            + "</ul></main></body></html>")


# ------------------------------------------------------------------------------------------- the words of a label
def test_the_words_that_say_what_a_file_is_are_no_part_of_a_committees_name():
    for title, want in (("Development Committee Meeting Prelimanary Agenda", "Development Committee"),
                        ("Development Committee Meeting - Preliminary Agenda", "Development Committee"),
                        ("Personnel Committee Meeting Agenda PRELIMINARY", "Personnel Committee"),
                        ("2023 Project Development Committee Mtg. Minutes", "2023 Project Development Committee"),
                        ("2023 Project Development Committee Mtg Preliminary Agenda", "2023 Project Development Committee"),
                        ("Administrative Committee Agenda", "Administrative Committee"), ("Audit Committee Meeting Minutes", "Audit Committee"),
                        ("Operations & Safety (PDF, 2 MB)", "Operations & Safety Committee"),
                        # how they met is the reader's to say, body by body: the general rule keeps it
                        ("Nominating Committee Conference Call Meeting Prelimianry Agenda", "Nominating Committee Conference Call"),
                        ("Board of Directors Meeting Minutes", ""), ("Preliminary Agenda", "")):
        assert A._committee(title) == want, (title, A._committee(title))


def test_a_committee_is_named_by_its_bodys_list():
    for label, want in (("Executive Finance Committee Meeting Preliminary Agenda", "Executive Finance Committee"),
                        ("Executive_Finance Committee Meeting Minutes", "Executive Finance Committee"),
                        ("Joint Finance Executive Committee Meeting Preliminary Agenda", "Executive Finance Committee"),
                        ("Joint GWCCA Executive & Finance Committee Meeting Minutes", "Executive Finance Committee"),
                        ("Executive and Finance Committee Minutes", "Executive Finance Committee"),
                        ("F&B Review Committee Meeting Minutes", "Food & Beverage Review Committee"),
                        ("FB Review Committee Meeting Preliminary Agenda", "Food & Beverage Review Committee"),
                        ("Food & Beverage Review Committee Meeting Minutes", "Food & Beverage Review Committee"),
                        ("Park Committee Meeting - Agenda", "Centennial Olympic Park Committee"),
                        ("Centennial Olympic Park Committee meeting minutes", "Centennial Olympic Park Committee"),
                        ("COP Meeting Agenda", "Centennial Olympic Park Committee"),
                        ("Centennial Olympic Park Meeting Agenda", "Centennial Olympic Park Committee"),
                        ("Campus Master Plan Preliminary Agenda", "Campus Master Plan Committee"),
                        ("Campus Master Plan Committee Minutes", "Campus Master Plan Committee"),
                        ("Campus Master Plan Meeting Minutes", "Campus Master Plan Committee"),
                        ("Finance Committee Meeting Minutes", "Finance Committee"),
                        ("Finance Committee - OPEB Board of Trustees Meeting Agenda", "Finance Committee"),
                        ("OPEB Board of Trustees Meeting Minutes", "Finance Committee"),
                        ("Campus Master Plan Meeting - Preliminary Agenda", "Campus Master Plan Committee"), ("Campus Master Plan - Minutes", "Campus Master Plan Committee"),
                        ("COP - Agenda", "Centennial Olympic Park Committee"), ("Centennial Olympic Park - Preliminary Agenda", "Centennial Olympic Park Committee"),
                        # committees that only share a word with those above, and the board's own files
                        ("Executive Committee Meeting Minutes", ""), ("Executive Search Committee Preliminary Agenda", ""),
                        ("Stadium Development Committee Minutes", ""), ("Board of Governors Meeting Minutes", ""),
                        # a deck about the park or the plan shown to the board names no committee
                        ("Campus Master Plan Update Presentation", ""), ("COP Update Presentation", ""), ("Board Presentation - Campus Master Plan", ""),
                        ("Centennial Olympic Park Update", ""), ("Scope of work", ""), ("", "")):
        assert A.committee_name(GW, label) == want, (label, A.committee_name(GW, label))
    assert A.committee_name({}, "Executive_Finance Committee Minutes") == "" and A.committee_name({"committees": []}, "Park Committee") == ""
    assert A.committee_name({"committees": [{"name": " Audit  Committee ", "rx": "audit"}]}, "AUDIT_committee minutes") == "Audit Committee"


def test_every_bodys_list_of_committees_in_the_config_can_be_used():
    seen = 0
    for ag in BODIES:
        if "committees" not in ag:
            continue
        seen += 1
        assert isinstance(ag["committees"], list) and ag["committees"], ag["id"]
        names = [c["name"] for c in ag["committees"]]
        assert len(set(names)) == len(names), (ag["id"], names)
        for c in ag["committees"]:
            assert sorted(c) == ["name", "rx"] and isinstance(c["name"], str) and c["name"].strip() and isinstance(c["rx"], str), (ag["id"], c)
            re.compile(c["rx"])
            assert A.committee_name(ag, c["name"]) == c["name"], (ag["id"], c["name"], "its own name is filed under another")
            assert not re.search(c["rx"], "", re.I) and not re.search(c["rx"], "Board of Governors Meeting Minutes", re.I), (ag["id"], c)
    assert seen >= 1 and GW["committees"]


def test_a_labels_day_however_it_is_written():
    for label, want in (("04-14-26 GWCCA Executive_Finance Committee Meeting Minutes", ("2026-04-14", "Executive Finance Committee Meeting Minutes")),
                        ("7-22-25 Board Presentation", ("2025-07-22", "Board Presentation")),
                        ("12-10-24-GWCCA Board of Governors Meeting Preliminary Agenda", ("2024-12-10", "Board of Governors Meeting Preliminary Agenda")),
                        ("09-16 & 17-21 GWCCA Board of Governors Planning Retreat Minutes", ("2021-09-16", "Board of Governors Planning Retreat Minutes")),
                        ("09-23 & 24-2015 Authority Planning Retreat Minutes", ("2015-09-23", "Authority Planning Retreat Minutes")),
                        ("09-14-22 & 9-15-22 GWCCA Board of Governors Planning Retreat Minutes", ("2022-09-14", "Board of Governors Planning Retreat Minutes")),
                        ("CALL FOR SPECIAL BOG MEETING - 04-30-26", ("2026-04-30", "CALL FOR SPECIAL BOG MEETING")),
                        ("Call for Special Meeting of Executive Committee - 11-05-26", ("2026-11-05", "Call for Special Meeting of Executive Committee")),
                        ("Notice of Special Called Meeting - 05-05-27", ("2027-05-05", "Notice of Special Called Meeting")),
                        ("04-14-26 - Board Presentation", ("2026-04-14", "Board Presentation")),
                        ("09-16-17-27 Board Retreat Presentation", ("2027-09-16", "Board Retreat Presentation")),      # two days, as the files are named
                        # a day at the end of what is no call or notice of a meeting
                        ("2027 Board Meeting Schedule - 11-05-26", None), ("Hotel Presentation - 05-05-19", None),
                        # no meeting's day: a year, a month, a roster's "as of", a day that is none
                        ("2024 BOG Planning Retreat Preliminary Agenda", None), ("March 2013 Authority Meeting Power Point", None),
                        ("2016 Authority - Name only - as of 07-27-16", None), ("Call for Special Meeting of Executive Committee Notice", None),
                        ("13-45-26 Board Presentation", None), ("04-14-26", None), ("", None)):
        assert A.gwcca_label(label) == want, (label, A.gwcca_label(label))


# ------------------------------------------------------------------------------------------- the Authority's page
def test_the_authoritys_committees_stand_under_one_name_each():
    ms = read()
    bodies = {m["body"] for m in ms}
    assert bodies == {"", "Executive Committee", "Finance Committee", "Personnel Committee", "Development Committee", "Nominating Committee",
                      "Centennial Olympic Park Committee", "Executive Finance Committee", "Food & Beverage Review Committee",
                      "Campus Master Plan Committee", "2023 Project Development Committee", "Executive Search Committee"}, sorted(bodies)
    assert not [b for b in bodies if re.search(r"_|\bjoint\b|\bconf|\bspecial\b|prelim|\bmtg\b|^board\b|\bfb\b|f&b|\bcop\b|opeb", b, re.I)]
    # a committee sits once a day; the board has its meeting, its retreat and a special called meeting apart
    assert {m["mk"] for m in ms if m["body"]} == {""} and {m["mk"] for m in ms if not m["body"]} == {"", "retreat", "special"}
    assert len({(m["date"], m["body"], m["mk"]) for m in ms}) == len(ms) == 106
    names = {m["name"].replace(m["body"], "", 1) for m in ms if m["body"]}
    assert names == {"", " Special Meeting", " - OPEB Board of Trustees"}


def test_one_sittings_files_are_together_whatever_their_labels_say():
    ms = read()
    # the agenda under "Joint Finance Executive Committee", the minutes under "Executive_Finance Committee"
    (ef,) = on(ms, "2026-04-14")
    assert ef["body"] == ef["name"] == "Executive Finance Committee"
    assert "Joint-Finance-Executive-Committee" in ef["agenda"] and "Executive_Finance-Committee" in ef["minutes"] and not ef["docs"]
    # three labels for one sitting, beside the board's own meeting of that day: nothing is dropped
    board, ef = on(ms, "2025-07-22")
    assert board["body"] == "" and "Board-of-Governors" in board["agenda"] and "Board-of-Governors" in board["minutes"]
    assert ef["body"] == "Executive Finance Committee" and ef["agenda"] and ef["minutes"] and len(ef["docs"]) == 1
    # an agenda marked "Special Meeting", minutes not: one sitting, named a special meeting
    (ex,) = on(ms, "2026-04-03")
    assert ex["body"] == "Executive Committee" and ex["name"] == "Executive Committee Special Meeting" and ex["agenda"] and ex["minutes"] and ex["mk"] == ""
    # an agenda marked "Conference Call" (or "Conf Call"), minutes not, or minutes of the "Board Executive Committee"
    for day in ("2014-03-18", "2016-08-24", "2018-01-31", "2019-10-18"):
        (ex,) = on(ms, day)
        assert (ex["body"], ex["name"], ex["where"]) == ("Executive Committee", "Executive Committee", "Conference call") and ex["agenda"] and ex["minutes"], day
    (nom,) = on(ms, "2014-11-11")                 # "... Conference Call Meeting Prelimianry Agenda"
    assert nom["body"] == "Nominating Committee" and nom["where"] == "Conference call" and nom["agenda"] and nom["minutes"]
    (fb,) = on(ms, "2017-07-13")                  # "Food & Beverage ... Minutes", "F&B ... Conference Call ... Agenda"
    assert fb["body"] == "Food & Beverage Review Committee" and fb["agenda"] and fb["minutes"]
    assert [m["body"] for m in on(ms, "2018-02-21")] == ["Food & Beverage Review Committee"]          # "FB Review Committee"
    # the park's committee under three labels on one day
    (park,) = on(ms, "2013-04-22")
    assert park["body"] == "Centennial Olympic Park Committee" and "COP" in park["agenda"] and park["minutes"]
    (park,) = on(ms, "2015-04-20")
    assert park["body"] == "Centennial Olympic Park Committee" and park["agenda"] and park["minutes"]
    assert [m["body"] for m in on(ms, "2023-06-02")] == ["2023 Project Development Committee"]        # "... Committee Mtg. Minutes"
    assert [m["body"] for m in on(ms, "2019-12-02")] == ["Development Committee"]                     # "... Prelimanary Agenda"
    # the Finance Committee sitting as the OPEB Board of Trustees: the agenda under one label, the minutes under the other
    for day in ("2016-03-15", "2016-04-07", "2016-06-06"):
        (fin,) = on(ms, day)
        assert (fin["body"], fin["name"]) == ("Finance Committee", "Finance Committee - OPEB Board of Trustees") and fin["agenda"] and fin["minutes"], day
    # a meeting of the board held by telephone: its place says so
    (call,) = on(ms, "2015-12-28")
    assert call["body"] == "" and call["where"] == "Conference call" and call["name"] == "Board of Governors Meeting"
    # the board's special called meeting and its retreat keep their own sittings, as before
    (sp,) = on(ms, "2021-03-25")
    assert sp["mk"] == "special" and sp["name"] == "Board of Governors Special Called Meeting" and sp["agenda"] and sp["minutes"]
    assert [(m["mk"], bool(m["minutes"])) for m in on(ms, "2020-09-17")] == [("", False), ("retreat", True)]


def test_a_committees_agenda_is_not_filed_as_the_boards():
    ms = read()
    # "Campus Master Plan Preliminary Agenda" names no "Committee": it took the board's agenda place (Mar. 25, 2025),
    # or stood as a board meeting that was never held (Oct. 24, 2024)
    board, plan = on(ms, "2025-03-25")
    assert board["body"] == "" and "Board-of-Governors-Meeting" in board["agenda"] and [x["t"] for x in board["docs"]] == ["Board Presentation"]
    assert plan["body"] == "Campus Master Plan Committee" and "Campus-Master-Plan-Preliminary-Agenda" in plan["agenda"] and plan["minutes"]
    assert [m["body"] for m in on(ms, "2024-10-24")] == ["Campus Master Plan Committee"]
    assert [m["body"] for m in on(ms, "2013-03-05")] == ["Centennial Olympic Park Committee"]         # "COP Meeting Agenda" alone
    # a list kept among the files is no sitting's file
    (park,) = on(ms, "2015-07-10")
    assert park["body"] == "Centennial Olympic Park Committee" and park["agenda"] and not park["docs"]
    assert not any("Assignments" in x["t"] for m in ms for x in m["docs"])


def test_a_file_is_read_however_its_label_or_address_is_written():
    ms = read()
    # the board's minutes of Jan. 27, 2026: the page writes their address with one quotation mark too many
    board, _ = on(ms, "2026-01-27")
    assert board["minutes"] == "https://www.gwcca.org/wp-content/uploads/2026/03/01-27-26-GWCCA-Board-of-Governors-Meeting-Minutes.pdf" and board["agenda"]
    assert any(h.endswith('.pdf"') for _, h in file_links(PAGE)) and not any('"' in u or "%22" in u for u in files_of(ms))
    # "12-10-24-GWCCA Board of Governors Meeting Preliminary Agenda": a hyphen after the day
    board, ex = on(ms, "2024-12-10")
    assert "Preliminary-Agenda-DRAFT" in board["agenda"] and board["minutes"] and ex["body"] == "Executive Committee"
    # a deck kept at Adobe, not as a file of the site
    (board,) = on(ms, "2026-03-31")
    assert board["minutes"] and [x["t"] for x in board["docs"]] == ["Board Presentation"] and board["docs"][0]["u"].startswith("https://acrobat.adobe.com/id/urn:")
    # the notice that called a meeting, its day at the label's end
    (called,) = on(ms, "2026-04-30")
    assert called["agenda"] and [x["t"] for x in called["docs"]] == ["CALL FOR SPECIAL BOG MEETING"]
    # a retreat of two days ("09-16 & 17-21 ..."), filed on its first
    for day in ("2013-09-19", "2014-09-25", "2015-09-23", "2016-09-21", "2017-09-20", "2018-09-19", "2021-09-16"):
        r = [m for m in on(ms, day) if m["mk"] == "retreat"]
        assert len(r) == 1 and r[0]["minutes"] and r[0]["name"] == "Board of Governors Planning Retreat" and r[0]["body"] == "", day
    (r,) = on(ms, "2022-09-14")                   # "09-14-22 & 9-15-22 ...": the second day is no part of what the file is
    assert r["mk"] == "retreat" and r["minutes"] and r["agenda"] and [x["t"] for x in r["docs"]] == ["Board Retreat", "Board Retreat Preliminary Agenda"]


def test_no_file_of_a_meeting_is_lost():
    """Measured by the page, not by the reader's own patterns: every link to a file is filed, but those that belong to
    no meeting's day (named one by one) and the one list passed over on purpose."""
    ag = dict(GW)
    ms = A.gwcca_about(PAGE, URL, ag, {"u": URL})
    kept, every = files_of(ms), file_links(PAGE)
    left = {label for label, href in every if href.rstrip('"') not in kept}
    assert len(every) == 188 and left == NO_DAY | {THE_LIST}, sorted(left ^ (NO_DAY | {THE_LIST}))
    assert kept <= {h.rstrip('"') for _, h in every} and len(kept) == 177
    assert ag["_passed"] == [next(h for label, h in every if label == THE_LIST)]                    # said to the run: passed over, not lost
    # the schedule's coming meetings are there as before
    assert [m["name"] for m in on(ms, "2026-12-08")] == ["Board of Governors Meeting"] and on(ms, "2026-09-16")[0]["mk"] == "retreat"


def test_the_order_of_the_pages_files_changes_nothing():
    def shape(ms):
        return sorted((m["date"], m["body"], m["mk"], m["name"], m["where"], tuple(sorted(files_of([m])))) for m in ms)

    head, items, tail = PAGE.partition("<ul>\n")[0], re.findall(r"<li>.*?</li>\n", PAGE.partition("<ul>\n")[2]), "</ul>\n</main></body></html>\n"
    assert len(items) == 188
    back = head + "<ul>\n" + "".join(reversed(items)) + tail
    odd = head + "<ul>\n" + "".join(items[1::2] + items[0::2]) + tail
    assert shape(read(back)) == shape(read()) == shape(read(odd))


def test_labels_close_to_the_pages_own():
    ms = read(page_of("05-05-27 Executive Committee Special Conference Call Preliminary Agenda", "05-05-27 GWCCA Executive Committee Meeting Minutes",
                      "06-06-27 Special Meeting of the Executive Committee Preliminary Agenda", "06-06-27 Executive Committee Meeting Minutes",
                      "07-07-27 Board of Governors Executive Committee Meeting Minutes",
                      "08-08-27 Campus Master Plan Update Presentation", "08-08-27 COP Update Presentation", "08-08-27 GWCCA Board of Governors Meeting Minutes",
                      "09-09-27 Centennial Olympic Park Meeting Agenda", "10-10-27 Special Called Conf. Call Meeting of the Personnel Committee Agenda",
                      "11-11-27 Special Projects Committee Meeting Minutes",
                      "Call for Special Meeting of Executive Committee - 12-12-27", "12-12-27 Executive Committee Meeting Minutes",
                      "01-11-28 OPEB Board of Trustees Meeting Minutes", "01-11-28 Finance Committee Meeting Preliminary Agenda",
                      "02-02-28 Campus Master Plan - Minutes", "02-02-28 COP - Agenda", "2028 Board Meeting Schedule - 02-02-28"))
    (a,) = on(ms, "2027-05-05")
    assert (a["body"], a["name"], a["where"]) == ("Executive Committee", "Executive Committee Special Meeting", "Conference call") and a["agenda"] and a["minutes"]
    (b,) = on(ms, "2027-06-06")
    assert (b["body"], b["name"]) == ("Executive Committee", "Executive Committee Special Meeting") and b["agenda"] and b["minutes"]
    assert [m["body"] for m in on(ms, "2027-07-07")] == ["Executive Committee"]
    (board,) = on(ms, "2027-08-08")               # decks shown to the board: its own files, no committee's sitting
    assert board["body"] == "" and board["minutes"] and sorted(x["t"] for x in board["docs"]) == ["COP Update Presentation", "Campus Master Plan Update Presentation"]
    assert [m["body"] for m in on(ms, "2027-09-09")] == ["Centennial Olympic Park Committee"]
    (p,) = on(ms, "2027-10-10")
    assert (p["body"], p["name"], p["where"]) == ("Personnel Committee", "Personnel Committee Special Meeting", "Conference call")
    (sp,) = on(ms, "2027-11-11")                  # "Special" in a committee's own name is its name
    assert (sp["body"], sp["name"]) == ("Special Projects Committee", "Special Projects Committee")
    (c,) = on(ms, "2027-12-12")                   # the notice that called a committee's special meeting, its day last
    assert (c["body"], c["name"]) == ("Executive Committee", "Executive Committee Special Meeting") and c["minutes"]
    assert [x["t"] for x in c["docs"]] == ["Call for Special Meeting of Executive Committee"]
    (f,) = on(ms, "2028-01-11")
    assert (f["body"], f["name"]) == ("Finance Committee", "Finance Committee - OPEB Board of Trustees") and f["agenda"] and f["minutes"]
    assert [m["body"] for m in on(ms, "2028-02-02")] == ["Campus Master Plan Committee", "Centennial Olympic Park Committee"]     # and no board meeting of a schedule's day


def test_an_address_with_a_quotation_mark_too_many():
    for tail in ('"', "%22", "'", '%22"'):
        page = ('<html><body><main><ul><li><a href="https://www.gwcca.org/wp-content/uploads/a.pdf' + tail.replace('"', "&quot;")
                + '">05-05-27 GWCCA Board of Governors Meeting Minutes</a></li></ul></main></body></html>')
        (m,) = read(page)
        assert m["minutes"] == "https://www.gwcca.org/wp-content/uploads/a.pdf", tail


def test_without_the_bodys_list_a_label_keeps_its_own_name():
    """What the list is for: the reader alone takes off only how they met."""
    ms = read(ag={})
    assert [m["body"] for m in on(ms, "2026-04-14")] == ["Executive Finance Committee", "Joint Finance Executive Committee"]
    assert [m["body"] for m in on(ms, "2026-04-03")] == ["Executive Committee"] and [m["body"] for m in on(ms, "2018-01-31")] == ["Executive Committee"]
    assert {"Campus Master Plan Committee", "FB Review Committee", "Finance Committee - OPEB Board of Trustees"} <= {m["body"] for m in ms}


# ------------------------------------------------------------------------------------------- a corrected reader
def earlier_reader(html, url, ag, page):
    """The Authority's reader as it was before the correction (Nov. 19 to 20, 2026), word for word but for the names
    it takes from this module."""
    def committee(title):
        t = A.squash(re.sub(r"\((?:PDF|BOD)[^)]*\)", " ", title, flags=re.I))
        t = A.squash(re.sub(r"\b(?:draft|preliminary|signed|approved|view|meeting|agenda|minutes|presentation|materials|summary)\b", " ", t, flags=re.I)).strip(" -–:,")
        if not t or A._is_board(title):
            return ""
        return t if re.search(r"committee", t, re.I) else f"{t} Committee"

    book, year, mon, in_sched = A.Book(), None, 0, False
    for ln in A.page_lines(html, url):
        t = ln["t"]
        sm = re.search(r"(20\d\d) meeting schedule", t, re.I)
        if sm:
            year, in_sched = int(sm.group(1)), True
            continue
        if in_sched and not ln["links"]:
            if re.fullmatch(rf"{A._MON}\.?", t, re.I):
                mon = A._mon(t)
                continue
            dm = re.fullmatch(r"(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?", t)
            if dm and mon:
                d = A._ok(year, mon, int(dm.group(1)))
                if d:
                    m = book.meet(d, "", "retreat" if dm.group(2) else "")
                    m["name"] = f"Authority planning retreat ({t.strip()})" if dm.group(2) else "Board of Governors Meeting"
                mon = 0
                continue
            if ln["h"] and not re.search(r"schedule", t, re.I) and len(t) > 12:
                in_sched = False
        for label, href in ln["links"]:
            lm = re.match(r"\s*(\d{1,2}-\d{1,2}-(?:20)?\d\d)\s+(.+)$", label)
            if not lm or not A.DOC_RX.search(href):
                continue
            ds = A.dates_in(lm.group(1))
            if not ds:
                continue
            what = A.squash(re.sub(r"^GWCCA\s+", "", lm.group(2)))
            body = "" if not re.search(r"committee", what, re.I) else committee(what)
            mk = "special" if re.search(r"special called", what, re.I) else ("retreat" if re.search(r"retreat", what, re.I) else "")
            body = A.squash(re.sub(r"\bspecial called\b|\bplanning retreat\b", "", body, flags=re.I))
            m = book.meet(ds[0], body, mk)
            m["name"] = m["name"] or (body or {"special": "Board of Governors Special Called Meeting", "retreat": "Board of Governors Planning Retreat"}.get(mk, "Board of Governors Meeting"))
            book.file(m, A._kind(what, href), what, href)
    return book.out()


STALE = re.compile(r"_|\bjoint\b|\bfb\b|f&b|^park committee|^board executive|conf(?:erence)? call|prelim|\bmtg\b|\bspecial$|opeb|assignments", re.I)


class Fetcher:
    def __init__(self, pages=None):
        self.asked, self.pages = [], pages or {}

    def get(self, url, **k):
        self.asked.append(url)
        t = self.pages.get(url)
        return SimpleNamespace(status=200 if t else 404, text=t or "", url=url, content_type="text/html", content=(t or "").encode())


class Nights:
    """Runs of the state reader over one store, the Archive answering from what the test hands it."""

    def __init__(self, tmp_path, monkeypatch, root=ROOT, only=("gwcca",)):
        import gacrawl.wayback as W

        self.mp, self.tmp, self.root, self.only = monkeypatch, tmp_path, root, list(only)
        self.asked, self.copies, self.n = [], {URL: (PAGE, TS)}, 0
        monkeypatch.setattr(W, "Wayback", lambda *a, **k: SimpleNamespace(off=""))
        monkeypatch.setattr(A, "read_archived", self._archived)
        self.store = Store(tmp_path / "data")

    def _archived(self, wb, u, copies=4):
        self.asked.append(u)
        return self.copies.get(u) or (None, "")

    def earlier(self):
        self.mp.setattr(A, "READER_REV", {})
        self.mp.setitem(A.READERS, "gwcca_about", earlier_reader)

    def corrected(self, rev=2):
        self.mp.setattr(A, "READER_REV", {"gwcca_about": rev})
        self.mp.setitem(A.READERS, "gwcca_about", A.gwcca_about)

    def run(self, fetcher=None):
        return A.run(fetcher or Fetcher(), self.store, self.root, only=self.only, log=lambda *a: None, minutes_cap=0)

    def body(self, bid="gwcca", day="2026-11-20"):
        self.n += 1
        out = self.tmp / f"export{self.n}.json"
        self.counts = A.export_state_meetings(self.store, self.root, out, today=day, log=lambda *a: None)
        return next(b for b in json.loads(out.read_text())["bodies"] if b["id"] == bid)

    def recs(self, src="ga-agency:gwcca"):
        return {r["key"]: (r.get("meta") or {}) for r in self.store.read(src, "meeting")}

    def versions(self, src="ga-agency:gwcca"):
        """How many times each record's content has been written (1: as first stored)."""
        return {r["key"]: r["version"] for r in self.store.read(src, "meeting")}

    def stamp(self, aid="gwcca", u=URL):
        return json.loads(self.store.kv_get(f"agency_archive:{aid}:{u}") or "{}")

    def age(self, aid="gwcca", u=URL):
        """The copy's stamp set back past its days, so the next run looks for the copy again."""
        self.store.kv_set(f"agency_archive:{aid}:{u}", json.dumps(self.stamp(aid, u) | {"at": "2026-01-01"}))

    def tomorrow(self, aid="gwcca", u=URL, since=None):
        """A copy asked for today and not taken is asked for again tomorrow: its mark set back a day (and, with
        `since`, the day it was first not taken)."""
        st = self.stamp(aid, u)
        assert st.get("tried"), st
        self.store.kv_set(f"agency_archive:{aid}:{u}", json.dumps(st | {"tried": "2026-01-01"} | ({"since": since} if since else {})))


@pytest.fixture()
def nights(tmp_path, monkeypatch):
    n = Nights(tmp_path, monkeypatch)
    yield n
    n.store.close()


def row_files(b):
    return {r[k] for r in b["meetings"] for k in ("a", "m", "s", "v") if r.get(k)} | {x["u"] for r in b["meetings"] for x in r.get("x") or []}


def test_a_copy_is_read_again_by_a_corrected_reader_and_what_the_earlier_one_filed_is_withdrawn(nights):
    # the first night: the reader as it was
    nights.earlier()
    r1 = nights.run()
    b1 = nights.body()
    v1 = nights.versions()
    assert set(v1.values()) == {1}
    names1 = {r.get("b", "") for r in b1["meetings"]}
    assert {"Executive_Finance Committee", "Joint Finance Executive Committee", "Board Executive Committee", "FB Review Committee",
            "Development Committee Prelimanary", "Executive Committee Conf Call", "Committee Assignments by Committee"} <= names1
    assert sorted(r["b"] for r in b1["meetings"] if r["d"] == "2026-04-14") == ["Executive_Finance Committee", "Joint Finance Executive Committee"]
    assert nights.asked == [URL] and nights.stamp().keys() == {"at", "as_of"} and not any("reader_rev" in mt for mt in nights.recs().values())
    assert not [n for n in r1["notes"] if "withdrawn" in n]
    nights.run()                                  # the same night again, the same reader: not asked for again inside its days
    assert nights.asked == [URL]

    # the corrected reader: the copy is read again though its days have not passed
    nights.corrected()
    # ... a meeting on file that the body's own site gave (its file one the page lists on that day), and one of this
    # page with a file the page no longer lists
    (ex,) = on(read(), "2026-04-03")
    live = {"fmt": A.FMT, "agency": "gwcca", "EventDate": "2026-04-03", "committee": "Live Committee", "found_on": URL, "what": "Live Committee",
            "EventMinutesFile": ex["minutes"]}
    lost = {"fmt": A.FMT, "agency": "gwcca", "EventDate": "2011-01-11", "committee": "Old Committee", "captured": "2020-01-01", "via": "wayback",
            "found_on": f"https://web.archive.org/web/20200101000000/{URL}", "what": "Old Committee", "EventMinutesFile": "https://www.gwcca.org/gone.pdf"}
    bare = {"fmt": A.FMT, "agency": "gwcca", "EventDate": "2011-03-03", "committee": "Bare Committee", "captured": "2026-05-01", "via": "wayback",
            "found_on": COPY, "what": "Bare Committee"}
    for mt in (live, lost, bare):
        m = A.from_meta(mt)
        nights.store.add(Record("ga-agency:gwcca", "meeting", f"{mt['found_on']}#meeting-{m['date']}", title="x", text="x", published=m["date"],
                                channel="html", jurisdiction="ga", evidence="primary", key=A.key_of("gwcca", m), meta=mt))
    r2 = nights.run()
    assert nights.asked == [URL, URL] and nights.stamp() == {"at": nights.stamp()["at"], "as_of": "2026-05-01", "rev": 2}
    b2 = nights.body()
    (ef,) = [r for r in b2["meetings"] if r["d"] == "2026-04-14"]
    assert ef["b"] == "Executive Finance Committee" and ef["a"] and ef["m"] and ef["p"] == COPY
    names2 = {r.get("b", "") for r in b2["meetings"]}
    assert not [n for n in names2 if STALE.search(n)], sorted(n for n in names2 if STALE.search(n))
    # not judged: read from the body's own site; a file the page no longer lists; no file at all
    assert {"Live Committee", "Old Committee", "Bare Committee"} <= names2
    gone = [n for n in r2["notes"] if "filed by an earlier reader" in n]
    assert all(n.startswith("gwcca: ") and n.endswith(f": filed by an earlier reader of {COPY}, not by today's: withdrawn") for n in gone)
    who = [n.split(": ")[1].split(" ", 1)[1] for n in gone]
    # 42 of the earlier reader's 122 meetings: 39 under a spelling no longer used, and 3 board meetings that were a
    # committee's agenda ("COP Meeting Agenda", "Campus Master Plan Preliminary Agenda")
    assert len(gone) == 42 and who.count("the board") == 3 and all(w == "the board" or STALE.search(w) for w in who), sorted(set(who))
    assert any("2026-04-14 Joint Finance Executive Committee" in n for n in gone) and any("2015-07-10 Committee Assignments by Committee" in n for n in gone)
    assert not any(x in n for n in gone for x in ("Old Committee", "Live Committee", "Bare Committee"))
    assert len(b1["meetings"]) == 122 and len(b2["meetings"]) == 106 + 3
    # nothing the page holds stops being shown: every file of the first night's rows is in the second night's
    assert row_files(b1) - row_files(b2) == {next(h for label, h in file_links(PAGE) if label == THE_LIST)}
    # ... and the files the earlier reader passed over are there
    assert sorted(label for label, h in file_links(PAGE) if h.rstrip('"') in row_files(b2) - row_files(b1)) == [
        "01-27-26 GWCCA Board of Governors Meeting Minutes", "03-31-26 Board Presentation",
        "09-16 & 17-21 GWCCA Board of Governors Planning Retreat Minutes", "09-19 & 20-18 Board Planning Retreat - Minutes",
        "09-19 & 20-2013 GWCCA Board of Governors Planning Retreat Minutes", "09-20 & 21-17 Board Planning Retreat Minutes",
        "09-20 & 21-17 GWCCA Board Planning Retreat - Preliminary Agenda", "09-21 & 22-16 Board Planning Retreat - Agenda",
        "09-21 & 22-16 GWCCA Board Planning Retreat Minutes", "09-23 & 24-15 Board Planning Retreat - Preliminary Agenda",
        "09-23 & 24-15 GWCCA Board Planning Retreat Presentation", "09-23 & 24-2015 Authority Planning Retreat Minutes",
        "09-25 & 26-2014 Authority Planning Retreat Minutes", "12-10-24-GWCCA Board of Governors Meeting Preliminary Agenda",
        "CALL FOR SPECIAL BOG MEETING - 04-30-26"]
    # what was withdrawn is still on file, marked; what the corrected reader filed carries its number
    recs = nights.recs()
    assert recs["ga-agency:gwcca|meeting|2026-04-14|joint-finance-executive-committee"].get("withdrawn")
    assert recs["ga-agency:gwcca|meeting|2026-04-14|executive-finance-committee"].get("reader_rev") == 2
    assert sum(1 for mt in recs.values() if mt.get("withdrawn")) == len(gone)
    # a meeting the corrected reader files just as before has not changed, though it now carries the reader's number:
    # it is not written as a new version, and its "changed" stamp stays
    v2 = nights.versions()
    for key in ("ga-agency:gwcca|meeting|2026-12-08", "ga-agency:gwcca|meeting|2026-02-24", "ga-agency:gwcca|meeting|2026-02-24|executive-committee",
                "ga-agency:gwcca|meeting|2021-03-25|special"):
        assert recs[key].get("reader_rev") == 2 and v2[key] == v1[key] == 1, key
    # ... and one it files with more than before (the board's minutes of Jan. 27, 2026) has
    assert v2["ga-agency:gwcca|meeting|2026-01-27"] == 2 and recs["ga-agency:gwcca|meeting|2026-01-27"]["EventMinutesFile"]
    # ... for every meeting both readers file: a new version when, and only when, what the reader files differs
    was, now = ({A.key_of("gwcca", m): m for m in ms} for ms in (earlier_reader(PAGE, URL, {}, {}), read()))
    both = was.keys() & now.keys()
    assert len(both) == 80 and all((v2[k] == 1) == (was[k] == now[k]) for k in both), [k for k in both if (v2[k] == 1) != (was[k] == now[k])]
    assert sum(1 for k in both if v2[k] == 1) == 48

    # the night after: nothing is asked for again, nothing more is withdrawn, the file is the same
    r3 = nights.run()
    assert nights.asked == [URL, URL] and not [n for n in r3["notes"] if "withdrawn" in n]
    b3 = nights.body()
    assert [(r["d"], r.get("b", "")) for r in b3["meetings"]] == [(r["d"], r.get("b", "")) for r in b2["meetings"]]
    # its days pass and the copy is read again: what was withdrawn is not judged a second time
    nights.age()
    r4 = nights.run()
    assert nights.asked == [URL, URL, URL] and r4["notes"] == [] and nights.body() == b3
    assert sum(1 for mt in nights.recs().values() if mt.get("withdrawn")) == 42


def test_an_earlier_copy_is_not_read_over_a_later_one(nights):
    """The later copy did not answer and the Archive gave an earlier one, without the files posted since: read over
    what is on file, it took the minutes and agendas of 2026 off the meetings that had them. It is left unread, and the
    later copy asked for again the next day. Then the corrected reader puts right what the earlier one filed."""
    nights.earlier()
    nights.run()
    b1 = nights.body()
    nights.corrected()
    earlier_copy = re.sub(r"<li><a href=\"[^\"]*uploads/2026/[^\n]*</li>\n", "", PAGE)
    assert earlier_copy.count("<li>") < PAGE.count("<li>") - 10
    nights.copies[URL] = (earlier_copy, "20260410094648")
    r2 = nights.run()
    b2 = nights.body()
    said = f"gwcca: the Archive gave its copy of 2026-04-10 of {URL}, earlier than the one on file (2026-05-01): not read over it"
    assert b2 == b1 and r2["notes"] == [said] and not r2["errors"]
    assert "rev" not in nights.stamp() and nights.stamp()["as_of"] == "2026-05-01" and nights.stamp()["tried"] == nights.stamp()["since"]
    nights.run()
    assert len(nights.asked) == 2                 # once a day, not on every run
    nights.tomorrow()
    r3 = nights.run()
    assert len(nights.asked) == 3 and r3["notes"] == [said] and nights.body() == b1
    # gone on for as long as the copy's days: an error, not a note
    nights.tomorrow(since="2026-01-01")
    r4 = nights.run()
    assert len(nights.asked) == 4 and r4["notes"] == [] and r4["errors"] == [said.replace("gwcca: ", f"gwcca {URL}: ") + " (since 2026-01-01)"]
    # a later copy than any before answers
    nights.tomorrow()
    nights.copies[URL] = (PAGE, "20260601000000")
    r5 = nights.run()
    b5 = nights.body()
    assert [r["b"] for r in b5["meetings"] if r["d"] == "2026-04-14"] == ["Executive Finance Committee"]
    assert not [n for n in {r.get("b", "") for r in b5["meetings"]} if STALE.search(n)] and len(b5["meetings"]) == 106
    assert b5["archive_as_of"] == "2026-06-01" and any("2026-04-14 Joint Finance Executive Committee" in n for n in r5["notes"]) and not r5["errors"]
    assert row_files(b1) - row_files(b5) == {next(h for label, h in file_links(PAGE) if label == THE_LIST)}
    assert nights.stamp() == {"at": nights.stamp()["at"], "as_of": "2026-06-01", "rev": 2}


def test_an_earlier_copy_of_the_same_day_is_not_read_over_a_later_one(nights):
    """Two copies of Apr. 10, 2026 are in the Archive's index, 36 minutes apart: by the copy the meetings on file were
    read from, to the second, not by a day."""
    nights.corrected()
    nights.copies[URL] = (PAGE, "20260410102248")
    nights.run()
    b1 = nights.body()
    nights.age()
    nights.copies[URL] = (re.sub(r"<li><a href=\"[^\"]*uploads/2026/[^\n]*</li>\n", "", PAGE), "20260410094648")
    r = nights.run()
    assert nights.body() == b1 and len(nights.asked) == 2 and "earlier than the one on file (2026-04-10)" in r["notes"][0]


def test_a_later_copy_that_lists_no_meeting_says_nothing(nights):
    """A later copy that is no longer the body's page, however real it looks: the coming meeting is not called gone,
    and the copy the meetings came from is read again when it answers."""
    nights.corrected()
    nights.run()
    b1 = nights.body()
    assert [r["d"] for r in b1["meetings"] if r["st"] == "scheduled"] == ["2026-12-08"]
    nights.age()
    nights.copies[URL] = ("<html><body><main><h1>About GWCCA</h1><p>" + "The Authority's campus in downtown Atlanta. " * 30 + "</p></main></body></html>", "20261201000000")
    r2 = nights.run()
    assert nights.body() == b1 and not [n for n in r2["notes"] if "withdrawn" in n]
    assert r2["notes"] == [f"gwcca: the Archive's copy of 2026-12-01 of {URL} lists no meeting: what is on file from its copy of 2026-05-01 stays"]
    assert nights.stamp()["as_of"] == "2026-05-01" and nights.stamp()["rev"] == 2
    nights.run()
    assert len(nights.asked) == 2                 # once a day
    nights.tomorrow()
    nights.copies[URL] = (PAGE, TS)               # the copy of May 1 answers again: read, not refused
    r3 = nights.run()
    assert len(nights.asked) == 3 and r3["notes"] == [] and not r3["errors"] and nights.body() == b1 and "tried" not in nights.stamp()


def test_a_reader_that_stops_or_a_dry_run_leaves_the_copy_to_be_read_again(nights):
    nights.earlier()
    nights.run()
    b1 = nights.body()
    # the corrected reader stops on its first night
    nights.corrected()
    nights.mp.setitem(A.READERS, "gwcca_about", lambda *a: (_ for _ in ()).throw(ValueError("the page changed")))
    r2 = nights.run()
    assert any("reader gwcca_about: ValueError" in e for e in r2["errors"]) and nights.body() == b1
    assert "rev" not in nights.stamp() and nights.stamp()["as_of"] == "2026-05-01" and nights.stamp()["tried"]
    nights.corrected()
    nights.run()
    assert len(nights.asked) == 2                 # once a day
    # a dry run the next day reads and stores nothing: the stamp does not take the reader's number
    nights.tomorrow()
    A.run(Fetcher(), nights.store, ROOT, only=["gwcca"], dry_run=True, log=lambda *a: None, minutes_cap=0)
    assert len(nights.asked) == 3 and "rev" not in nights.stamp() and nights.body() == b1
    r4 = nights.run()                             # the run after it reads the copy again and puts the filing right
    assert len(nights.asked) == 4 and nights.stamp()["rev"] == 2 and len([n for n in r4["notes"] if "filed by an earlier reader" in n]) == 42
    assert len(nights.body()["meetings"]) == 106


def test_a_file_two_days_link_is_filed_again_on_its_own_day(nights):
    """One file linked from two days' labels, and a reading that came without one of them: the meeting of that day is
    not withdrawn for its file being filed under the other day."""
    def page(*labels):
        return ("<html><body><main><ul>" + "".join(f'<li><a href="https://www.gwcca.org/wp-content/uploads/{f}.pdf">{lab}</a></li>' for lab, f in labels)
                + "</ul></main></body></html>")

    both = page(("11-26-19 Nominating Committee Mtg. Minutes", "nom"), ("11-29-18 Nominating Committee Meeting Minutes", "nom"),
                ("11-29-18 GWCCA Board of Governors Meeting Minutes", "board"))
    nights.earlier()
    nights.copies[URL] = (both, TS)
    nights.run()
    assert sorted((r["d"], r.get("b", "")) for r in nights.body()["meetings"]) == [("2018-11-29", ""), ("2018-11-29", "Nominating Committee"), ("2019-11-26", "Nominating Committee Mtg.")]
    nights.corrected()
    nights.copies[URL] = (page(("11-29-18 Nominating Committee Meeting Minutes", "nom"), ("11-29-18 GWCCA Board of Governors Meeting Minutes", "board")), TS)
    r = nights.run()                              # the answer came without the line of 2019
    assert not [n for n in r["notes"] if "withdrawn" in n]
    assert ("2019-11-26", "Nominating Committee Mtg.") in {(x["d"], x.get("b", "")) for x in nights.body()["meetings"]}
    nights.age()
    nights.copies[URL] = (both, TS)               # whole again: filed under its day by the corrected reader, the earlier filing withdrawn
    r = nights.run()
    assert [n.split(": ")[1] for n in r["notes"] if "withdrawn" in n] == ["2019-11-26 Nominating Committee Mtg."]
    assert sorted((x["d"], x.get("b", "")) for x in nights.body()["meetings"]) == [("2018-11-29", ""), ("2018-11-29", "Nominating Committee"), ("2019-11-26", "Nominating Committee")]


def test_a_copy_that_came_cut_short_takes_nothing_it_lacks(nights):
    """The same copy, half of it: a meeting the earlier reader filed is withdrawn only when every file of it is filed
    again by this reading."""
    nights.earlier()
    nights.run()
    nights.corrected()
    half = re.sub(r"<li><a href=\"[^\"]*uploads/2026/[^\n]*</li>\n", "", PAGE)
    nights.copies[URL] = (half, TS)
    r2 = nights.run()
    b2 = nights.body()
    gone = [n.split(": ")[1] for n in r2["notes"] if "filed by an earlier reader" in n]
    assert 30 < len(gone) < 42 and "2026-04-14 Joint Finance Executive Committee" not in gone and "2018-01-31 Board Executive Committee" in gone
    stale = [r for r in b2["meetings"] if STALE.search(r.get("b", ""))]
    assert stale and all(any("uploads/2026/" in f for f in row_files({"meetings": [r]})) for r in stale), [(r["d"], r["b"]) for r in stale]
    assert sorted(r["b"] for r in b2["meetings"] if r["d"] == "2026-04-14") == ["Executive_Finance Committee", "Joint Finance Executive Committee"]


def test_a_later_copy_right_away(nights):
    nights.earlier()
    nights.run()
    b1 = nights.body()
    nights.corrected()
    nights.copies[URL] = (PAGE, "20260601000000")                         # the Archive has a newer copy by the next run
    nights.run()
    b2 = nights.body()
    assert not [n for n in {r.get("b", "") for r in b2["meetings"]} if STALE.search(n)] and len(b2["meetings"]) == 106
    assert {r["p"] for r in b2["meetings"]} == {f"https://web.archive.org/web/20260601000000/{URL}"}
    assert row_files(b1) - row_files(b2) == {next(h for label, h in file_links(PAGE) if label == THE_LIST)}


def test_a_copy_that_does_not_answer_changes_nothing_and_is_asked_for_again(nights):
    nights.earlier()
    nights.run()
    b1 = nights.body()
    nights.corrected()
    nights.copies[URL] = (None, "")
    r2 = nights.run()
    assert not [n for n in r2["notes"] if "withdrawn" in n] and "rev" not in nights.stamp() and len(nights.asked) == 2
    assert [(r["d"], r.get("b", "")) for r in nights.body()["meetings"]] == [(r["d"], r.get("b", "")) for r in b1["meetings"]]
    nights.copies[URL] = (PAGE, TS)
    nights.run()                                  # asked for again on the next run, not after its days
    assert len(nights.asked) == 3 and nights.stamp()["rev"] == 2 and len(nights.body()["meetings"]) == 106


def test_a_coming_meeting_a_later_copy_no_longer_lists_is_withdrawn(nights):
    """The schedule's Dec. 8 moved to Dec. 15: only a copy as late as the one that listed it says it is gone."""
    nights.corrected()
    nights.run()
    assert [r["d"] for r in nights.body()["meetings"] if r["d"] > "2026-11-20"] == ["2026-12-08"]
    moved = PAGE.replace("<p>DEC</p>\n<h4>8</h4>", "<p>DEC</p>\n<h4>15</h4>")
    assert moved != PAGE
    nights.age()
    nights.copies[URL] = (moved, "20260410094648")                        # an earlier copy than the one on file: not read
    r = nights.run()
    assert [x["d"] for x in nights.body()["meetings"] if x["d"] > "2026-11-20"] == ["2026-12-08"] and not [n for n in r["notes"] if "withdrawn" in n]
    nights.tomorrow()
    nights.copies[URL] = (moved, "20260601000000")                        # a later one
    r = nights.run()
    assert [x["d"] for x in nights.body()["meetings"] if x["d"] > "2026-11-20"] == ["2026-12-15"]
    assert [n for n in r["notes"] if "withdrawn" in n] == [f"gwcca: 2026-12-08 is no longer on {COPY}: withdrawn"]


def test_the_boards_meeting_and_its_retreat_of_one_day_are_both_written_out(nights):
    """Sept. 17, 2020 on the Authority's page: the retreat's minutes stand before the board meeting's file. The export
    wrote the retreat first and left the board's own meeting out as "a second row for the same body, committee, day
    and hour"."""
    nights.corrected()
    res = nights.run()
    gw = nights.body()
    assert nights.counts["left_out"] == 0 and "warning" not in nights.counts and len(gw["meetings"]) == res["meetings"] == 106
    day = sorted((r for r in gw["meetings"] if r["d"] == "2020-09-17"), key=lambda r: r.get("b", ""))
    assert [(r.get("b", ""), r["n"]) for r in day] == [("", "Board of Governors Meeting"), ("Board of Governors Planning Retreat", "Board of Governors Planning Retreat")]
    assert day[1]["m"] and day[1]["a"] and day[0]["x"][0]["l"] == "Board Meeting"
    for d in ("2019-09-19", "2023-09-13", "2023-09-14"):
        assert sorted(r.get("b", "") for r in gw["meetings"] if r["d"] == d) == ["", "Board of Governors Planning Retreat"], d
    # every row: the format's keys in order, a place only where the page gave one
    assert all(A.validate_meeting(r) == [] for r in gw["meetings"])
    assert {r["w"] for r in gw["meetings"] if "w" in r} == {"Conference call"}


def test_the_corrected_reading_passes_electos_importer(nights):
    """Electo's own check of the format (ga/crawl_import.py state_bodies, strict), when its checkout is beside this one."""
    import sys

    ga = ROOT.parent / "Electo" / "ga"
    if not (ga / "crawl_import.py").exists():
        pytest.skip("Electo is not checked out beside Crawl")
    sys.path.insert(0, str(ga))
    try:
        import crawl_import as CI
    except Exception as exc:  # noqa: BLE001 — its imports need Electo's own environment
        pytest.skip(f"Electo's importer does not import here: {type(exc).__name__}")
    finally:
        sys.path.remove(str(ga))
    if not hasattr(CI, "state_bodies"):
        pytest.skip("this Electo checkout has no state_bodies")
    nights.corrected()
    nights.run()
    gw = nights.body()
    bodies, dropped = CI.state_bodies([gw], {}, "", True)
    assert dropped == {} and len(bodies) == 1 and len(bodies[0]["meetings"]) == len(gw["meetings"]) == 106
    assert bodies[0]["source"] == "archive" and bodies[0]["archive_as_of"] == "2026-05-01"


def test_a_reader_of_number_one_changes_nothing(nights):
    """Every other body: no number on its records or its copy's stamp, and nothing withdrawn but a coming meeting its
    page no longer lists (as before)."""
    nights.mp.setattr(A, "READER_REV", {})
    nights.run()
    assert nights.stamp().keys() == {"at", "as_of"} and not any("reader_rev" in mt for mt in nights.recs().values())
    # a past meeting on file from this very copy that the reader does not file, its file filed that day under another: it stays
    (ex,) = on(A.gwcca_about((FX / "gwcca_labels.html").read_text(), URL, dict(GW), {"u": URL}), "2026-04-03")
    mt = {"fmt": A.FMT, "agency": "gwcca", "EventDate": "2026-04-03", "committee": "Gone Committee", "captured": "2026-05-01", "via": "wayback", "found_on": COPY,
          "EventMinutesFile": ex["minutes"]}
    m = A.from_meta(mt)
    nights.store.add(Record("ga-agency:gwcca", "meeting", f"{COPY}#meeting-x", title="x", text="x", published=m["date"], channel="archive",
                            jurisdiction="ga", evidence="primary", key=A.key_of("gwcca", m), meta=mt))
    nights.age()
    res = nights.run()
    assert len(nights.asked) == 2 and not [n for n in res["notes"] if "withdrawn" in n] and nights.stamp().keys() == {"at", "as_of"}
    assert "Gone Committee" in {r.get("b", "") for r in nights.body()["meetings"]}


# ------------------------------------------------------------------------------------------- a body of several pages
TWO_PAGES = """agencies:
  - id: two
    kind: authority
    name: Two Page Authority
    agency: Two Page Authority
    reader: gwcca_about
    committees:
      - {name: "Executive Finance Committee", rx: 'executive[ _&]+finance|finance[ _&]+executive'}
    pages:
      - {u: "https://two.example.gov/files", archive: true, once: true}
      - https://two.example.gov/now
"""
NOW = "https://two.example.gov/now"


def schedule(day):
    return f"<html><body><main><h3>2026 Meeting Schedule</h3><p>DEC</p><h4>{day}</h4></main></body></html>"
FILES = "https://two.example.gov/files"


@pytest.fixture()
def two(tmp_path, monkeypatch):
    (tmp_path / "root" / "config").mkdir(parents=True)
    (tmp_path / "root" / "config" / "agencies.yaml").write_text(TWO_PAGES)
    n = Nights(tmp_path, monkeypatch, root=tmp_path / "root", only=("two",))
    n.copies = {FILES: (page_of("04-14-26 Joint Finance Executive Committee Meeting Preliminary Agenda", "04-14-26 GWCCA Executive_Finance Committee Meeting Minutes"), TS)}
    yield n
    n.store.close()


def test_a_page_read_once_is_read_once_more_by_a_corrected_reader(two):
    live = Fetcher({NOW: schedule(9)})
    two.earlier()
    two.run(live)
    assert two.asked == [FILES] and "rev" not in two.stamp("two", FILES)
    assert sorted(mt["committee"] for mt in two.recs("ga-agency:two").values()) == ["", "Executive_Finance Committee", "Joint Finance Executive Committee"]
    two.age("two", FILES)
    two.run(live)
    assert two.asked == [FILES]                                           # once: never again, whatever its days
    two.corrected()
    r = two.run(live)
    assert two.asked == [FILES, FILES] and two.stamp("two", FILES)["rev"] == 2
    assert [n.split(": ")[1] for n in r["notes"] if "filed by an earlier reader" in n] == ["2026-04-14 Joint Finance Executive Committee"]
    two.age("two", FILES)
    two.run(live)
    two.run(live)
    assert two.asked == [FILES, FILES]                                    # and then never again


def test_a_night_that_was_not_whole(two):
    """Another page of the body did not answer: what the earlier reader filed from the copy read is still put right
    (its files are all filed again), but no coming meeting is called gone."""
    two.earlier()
    two.run(Fetcher({NOW: schedule(9)}))
    assert "ga-agency:two|meeting|2026-12-09" in two.recs("ga-agency:two")
    two.corrected()
    r = two.run(Fetcher())                                                # the body's own page answers 404 this night
    assert any("HTTP 404" in e for e in r["errors"])
    recs = two.recs("ga-agency:two")
    assert not recs["ga-agency:two|meeting|2026-12-09"].get("withdrawn")
    assert recs["ga-agency:two|meeting|2026-04-14|joint-finance-executive-committee"].get("withdrawn")
    assert two.stamp("two", FILES)["rev"] == 2 and len([n for n in r["notes"] if "filed by an earlier reader" in n]) == 1
    # the nights after, the body's page lists Dec. 16 and no longer Dec. 9. The page read once is not read again, so no
    # night is whole and the meeting stays on file (as before this change: a body with a page read once)
    r = two.run(Fetcher({NOW: schedule(16)}))
    assert two.asked == [FILES, FILES] and [n for n in r["notes"] if "withdrawn" in n] == []
    assert not two.recs("ga-agency:two")["ga-agency:two|meeting|2026-12-09"].get("withdrawn")


# ------------------------------------------------------------------------------------------- copies, page by page
def body_of(tmp_path, monkeypatch, config, only):
    (tmp_path / "root" / "config").mkdir(parents=True)
    (tmp_path / "root" / "config" / "agencies.yaml").write_text(config)
    return Nights(tmp_path, monkeypatch, root=tmp_path / "root", only=(only,))


def test_each_page_is_measured_against_its_own_copies(tmp_path, monkeypatch):
    """Two pages of one body, the second captured 21 seconds after the first: each is read again from its own copy,
    not refused for being earlier than the other page's."""
    a, b = "https://pair.example.gov/files", "https://pair.example.gov/more"
    n = body_of(tmp_path, monkeypatch, f"""agencies:
  - id: pair
    kind: authority
    name: Pair Authority
    agency: Pair Authority
    reader: gwcca_about
    pages:
      - {{u: "{a}", archive: true}}
      - {{u: "{b}", archive: true}}
""", "pair")
    try:
        n.copies = {a: (page_of("04-14-26 GWCCA Board of Governors Meeting Minutes"), "20260501095954"),
                    b: (page_of("03-10-26 GWCCA Board of Governors Meeting Minutes"), "20260501100015")}
        n.corrected()
        n.run()
        assert sorted(n.recs("ga-agency:pair")) == ["ga-agency:pair|meeting|2026-03-10", "ga-agency:pair|meeting|2026-04-14"]
        n.age("pair", a)
        n.age("pair", b)
        r = n.run()
        assert n.asked == [a, b, a, b] and r["notes"] == [] and not r["errors"]
        assert n.stamp("pair", a)["at"] == n.stamp("pair", b)["at"] != "2026-01-01" and "tried" not in n.stamp("pair", a)
    finally:
        n.store.close()


def test_a_page_whose_meetings_are_filed_under_their_own_posts(tmp_path, monkeypatch):
    """A page of notices, each meeting filed under its post's address: no meeting on file names the page's copy, so an
    earlier copy is told by the day of the last copy the reader found meetings on (the stamp), and a later copy with
    no meeting does not move that day."""
    u = "https://mbcta.net/category/board-meetings/"
    n = body_of(tmp_path, monkeypatch, f"""agencies:
  - id: posty
    kind: transit
    name: Posty Transit Authority
    agency: Posty Transit Authority
    reader: notice_posts
    pages:
      - {{u: "{u}", archive: true}}
""", "posty")
    try:
        posts = (FX / "mbcta_posts.html").read_text()
        n.copies = {u: (posts, "20260212101010")}
        n.mp.setattr(A, "READER_REV", {})
        n.run()
        recs = n.recs("ga-agency:posty")
        assert len(recs) == 2 and all(mt["captured"] == "2026-02-12" and A.copy_of(mt["found_on"]) == ("", "") for mt in recs.values())
        assert n.stamp("posty", u) == {"at": n.stamp("posty", u)["at"], "as_of": "2026-02-12"}
        # a later copy that lists no meeting: not taken, the stamp's day stays
        n.age("posty", u)
        n.copies[u] = ("<html><body><main><p>" + "Riding the bus in Macon. " * 40 + "</p></main></body></html>", "20260301000000")
        r = n.run()
        assert r["notes"] == [f"posty: the Archive's copy of 2026-03-01 of {u} lists no meeting: what is on file from its copy of 2026-02-12 stays"]
        assert n.stamp("posty", u)["as_of"] == "2026-02-12" and n.stamp("posty", u)["tried"]
        # an earlier copy than the last one with meetings: not read over it
        n.tomorrow("posty", u)
        n.copies[u] = (posts, "20251024201618")
        r = n.run()
        assert r["notes"] == [f"posty: the Archive gave its copy of 2025-10-24 of {u}, earlier than the one on file (2026-02-12): not read over it"]
        # the same copy again: read, and the marks are gone (a reader of number 1: the stamp is written by the read alone)
        n.tomorrow("posty", u)
        n.copies[u] = (posts, "20260212101010")
        r = n.run()
        assert r["notes"] == [] and n.stamp("posty", u) == {"at": n.stamp("posty", u)["at"], "as_of": "2026-02-12"} and len(n.asked) == 4
    finally:
        n.store.close()


def test_a_first_copy_with_no_meeting_names_no_day(nights):
    """Nothing on file and nothing found: the stamp carries no copy's day, so the copy that does list meetings, an
    earlier one, is read when it answers."""
    nights.corrected()
    nights.copies[URL] = ("<html><body><main><h1>About GWCCA</h1><p>" + "The Authority's campus in downtown Atlanta. " * 30 + "</p></main></body></html>", "20261201000000")
    r = nights.run()
    assert r["notes"] == [] and nights.recs() == {} and nights.stamp() == {"at": nights.stamp()["at"]}
    b = nights.body()
    assert b["meetings"] == [] and b["source"] == "archive" and "archive_as_of" not in b
    nights.age()
    nights.copies[URL] = (PAGE, TS)
    r = nights.run()
    assert r["notes"] == [] and len(nights.body()["meetings"]) == 106 and nights.stamp()["as_of"] == "2026-05-01"


def test_a_dry_run_does_not_mark_a_copy_as_asked_for(nights):
    nights.earlier()
    nights.run()
    nights.corrected()
    nights.copies[URL] = (re.sub(r"<li><a href=\"[^\"]*uploads/2026/[^\n]*</li>\n", "", PAGE), "20260410094648")
    before = nights.stamp()
    out = A.run(Fetcher(), nights.store, ROOT, only=["gwcca"], dry_run=True, log=lambda *a: None, minutes_cap=0)
    assert any("earlier than the one on file" in x for x in out["notes"]) and nights.stamp() == before
    nights.copies[URL] = ("<html><body><main><p>" + "Nothing here. " * 60 + "</p></main></body></html>", "20261201000000")
    out = A.run(Fetcher(), nights.store, ROOT, only=["gwcca"], dry_run=True, log=lambda *a: None, minutes_cap=0)
    assert any("lists no meeting" in x for x in out["notes"]) and nights.stamp() == before
    nights.copies[URL] = (PAGE, TS)               # the run that stores, the same day: it asks, reads and puts the filing right
    r = nights.run()
    assert len(nights.asked) == 4 and nights.stamp()["rev"] == 2 and len([x for x in r["notes"] if "filed by an earlier reader" in x]) == 42


def test_notices_and_long_labels():
    ms = read(page_of("Notice of Special Called Meeting of the Finance Committee - 11-05-27", "11-05-27 Finance Committee Meeting Minutes",
                      "Call for Special Meeting of Executive Committee Notice - 11-06-27", "11-06-27 GWCCA Executive Committee Meeting Minutes",
                      "11-07-27 Campus Master Plan Meeting Presentation", "11-07-27 " + "Board of Governors Meeting Minutes and a great deal more " * 8))
    # (a notice of a meeting stands where its agenda does, as for every body: _kind)
    (f,) = on(ms, "2027-11-05")
    assert (f["body"], f["name"]) == ("Finance Committee", "Finance Committee Special Meeting") and f["minutes"].endswith("f1.pdf") and f["agenda"].endswith("f0.pdf")
    (e,) = on(ms, "2027-11-06")
    assert (e["body"], e["name"]) == ("Executive Committee", "Executive Committee Special Meeting") and e["minutes"].endswith("f3.pdf") and e["agenda"].endswith("f2.pdf")
    assert not f["docs"] and not e["docs"]
    assert [m["body"] for m in on(ms, "2027-11-07")] == ["Campus Master Plan Committee"]              # a paragraph that is a link is no label
    assert A.gwcca_label("11-07-27 " + "x" * 300) is None and A.gwcca_label("11-07-27 " + "x" * 200) == ("2027-11-07", "x" * 200)
