"""Open records requests: contact discovery, planning, drafting, sending (fake SMTP), replies (fake IMAP),
follow-ups, and PDF intake into the Screenshots box. Nothing is ever sent for real."""
import datetime as dt
import email.utils
import io
from email.message import EmailMessage
from pathlib import Path

import pytest

from gacrawl import openrecords as orr
from gacrawl.seed import _official_email, find_records_contact
from gacrawl.store import Record, Store

J = [{"id": "big", "name": "Bigtown", "kind": "city", "population": 50000,
      "records": {"emails": ["records@bigtown.gov"], "page": "https://bigtown.gov/open-records"}},
     {"id": "co-small", "name": "Small County", "kind": "county", "population": 3000,
      "records": {"emails": ["clerk@smallcounty.ga.us"], "page": "https://smallcounty.ga.us/clerk"}},
     {"id": "portal", "name": "Portalville", "kind": "town", "population": 9000,
      "records": {"portal": "https://portalville.govqa.us/WEBAPP/_rs/", "page": "https://portalville.gov/records"}},
     {"id": "coded", "name": "Codeburg", "kind": "city", "population": 80000, "portals": {"ecode360": "https://ecode360.com/CO1234"},
      "records": {"emails": ["clerk@codeburg.gov"]}},
     {"id": "nocontact", "name": "Quiet", "kind": "town", "population": 70000}]
ENV = {"ORR_SMTP_HOST": "smtp.example", "ORR_USER": "u", "ORR_PASSWORD": "p", "ORR_FROM": "requests@project.example",
       "ORR_IMAP_HOST": "imap.example"}
CFG = {"openrecords": {"enabled": True, "requester": "Georgia Civic Records Project", "daily_limit": 10}}


class FakeSMTP:
    sent = []

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def test_business_days():
    assert orr.business_days_after(dt.date(2026, 11, 6), 3) == dt.date(2026, 11, 11)   # Fri -> Wed
    assert orr.business_days_after(dt.date(2026, 11, 2), 3) == dt.date(2026, 11, 5)    # Mon -> Thu


def test_plan_skips_crawlable_and_contactless_and_orders_by_size(tmp_path):
    items = orr.plan(J, orr.Tracker(tmp_path))
    assert [i["jurisdiction"]["id"] for i in items] == ["big", "portal", "co-small"]
    assert [i["how"] for i in items] == ["email", "portal", "email"]


def test_draft_wording():
    subj, body = orr.draft(J[0], "Georgia Civic Records Project", "requests@project.example")
    assert "[ORR-big]" in subj and "City of Bigtown" in body and "50-18-70" in body and "any cost" in body
    assert body.rstrip().endswith("Reply to: requests@project.example")
    assert "Small County" in orr.draft(J[1], "X")[1]


def test_disabled_means_dry_run(tmp_path):
    FakeSMTP.sent = []
    r = orr.Requests(tmp_path, {"openrecords": {"enabled": False, "requester": "X"}}, orr.Mailbox(ENV), log=lambda *_: None)
    out = r.send_batch(J, smtp_factory=FakeSMTP)
    assert out["dry_run"] and out["sent"] == 0 and FakeSMTP.sent == [] and "enabled is false" in out["why_not_sending"]
    r2 = orr.Requests(tmp_path, CFG, orr.Mailbox({}), log=lambda *_: None)                    # no mailbox secrets
    assert not r2.enabled and "mailbox" in r2.why_not()


def test_send_limit_cooldown_and_portal(tmp_path):
    FakeSMTP.sent = []
    r = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), log=lambda *_: None)
    out = r.send_batch(J, limit=1, smtp_factory=FakeSMTP)
    assert out["sent"] == 1 and out["portal_only"] == 1 and len(FakeSMTP.sent) == 1
    m = FakeSMTP.sent[0]
    assert m["To"] == "records@bigtown.gov" and "Georgia Civic Records Project" in m["From"]
    t = r.tracker.data["big"]
    assert t["status"] == "sent" and t["due"] and t["message_id"] == m["Message-ID"]
    assert r.tracker.data["portal"]["status"] == "portal_only"
    out2 = r.send_batch(J, limit=1, smtp_factory=FakeSMTP)                                    # daily limit reached
    assert out2["sent"] == 0
    out3 = r.send_batch(J, limit=5, smtp_factory=FakeSMTP)
    assert [x["To"] for x in FakeSMTP.sent] == ["records@bigtown.gov", "clerk@smallcounty.ga.us"]   # never twice
    assert out3["sent"] == 1


def test_smtp_error_stops_the_batch(tmp_path):
    class Boom(FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection refused")
    r = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), log=lambda *_: None)
    out = r.send_batch(J, smtp_factory=Boom)
    assert out["errors"] == 1 and out["sent"] == 0 and r.tracker.data["big"]["status"] == "error"


def _reply(to_msg, body, attach=None, frm="Clerk <records@bigtown.gov>", subject=None):
    m = EmailMessage()
    m["From"], m["Subject"] = frm, subject or "RE: " + to_msg["Subject"]
    m["In-Reply-To"] = to_msg["Message-ID"]
    m["Message-ID"] = email.utils.make_msgid()
    m.set_content(body)
    if attach:
        m.add_attachment(attach, maintype="application", subtype="pdf", filename="Code of Ordinances.pdf")
    return m


class FakeIMAP:
    msgs = []

    def login(self, u, p):
        pass

    def select(self, *a, **k):
        pass

    def search(self, *a):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(FakeIMAP.msgs)))]

    def fetch(self, i, what):
        return "OK", [(b"", FakeIMAP.msgs[int(i) - 1].as_bytes())]

    def logout(self):
        pass


def _pdf():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "PDF")
    return buf.getvalue()


def test_replies_attachments_bounces_and_follow_ups(tmp_path):
    FakeSMTP.sent = []
    r = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), log=lambda *_: None)
    r.send_batch(J, smtp_factory=FakeSMTP)
    to_big, to_small = FakeSMTP.sent
    FakeIMAP.msgs = [_reply(to_big, "Attached is our current code.", attach=_pdf()),
                     _reply(to_small, "Delivery failed: mailbox unavailable", frm="Mail Delivery Subsystem <MAILER-DAEMON@x>",
                            subject="Undeliverable: " + to_small["Subject"]),
                     _reply(to_big, "unrelated", frm="someone@else", subject="Lunch?")]
    FakeIMAP.msgs[2].replace_header("In-Reply-To", "<nothing@x>")
    out = r.check_replies(imap_factory=FakeIMAP, attachments_dir=tmp_path / "inbox")
    assert out == {"checked": 3, "matched": 2, "attachments": 1}
    assert r.tracker.data["big"]["status"] == "fulfilled" and r.tracker.data["co-small"]["status"] == "bounced"
    saved = list((tmp_path / "inbox").iterdir())
    assert [p.name for p in saved] == ["big__Code_of_Ordinances.pdf"]
    assert r.check_replies(imap_factory=FakeIMAP, attachments_dir=tmp_path / "inbox")["matched"] == 0   # not twice
    # cost estimate classification
    FakeSMTP.sent = []
    r2 = orr.Requests(tmp_path / "b", CFG, orr.Mailbox(ENV), log=lambda *_: None)
    r2.send_batch(J[:1], smtp_factory=FakeSMTP)
    FakeIMAP.msgs = [_reply(FakeSMTP.sent[0], "The estimated cost for copies is $45.00. Please confirm.")]
    r2.check_replies(imap_factory=FakeIMAP)
    assert r2.tracker.data["big"]["status"] == "cost_estimate"
    # follow-up after the deadline, once, in the same thread
    r3 = orr.Requests(tmp_path / "c", CFG, orr.Mailbox(ENV), log=lambda *_: None)
    FakeSMTP.sent = []
    r3.send_batch(J[:1], smtp_factory=FakeSMTP)
    r3.tracker.data["big"]["sent_at"] = "2026-10-01T12:00:00+00:00"
    assert r3.follow_ups(smtp_factory=FakeSMTP)["sent"] == 1
    fu = FakeSMTP.sent[-1]
    assert fu["Subject"].startswith("Re: ") and fu["In-Reply-To"] == r3.tracker.data["big"]["message_id"]
    assert r3.follow_ups(smtp_factory=FakeSMTP)["sent"] == 0


def test_official_email_and_contact_discovery():
    assert _official_email("records@bigtown.gov", "https://www.bigtown.gov/")
    assert _official_email("clerk@cityofx.com", "https://www.cityofx.com/")
    assert not _official_email("someone@freemail.example", "https://www.cityofx.com/")

    class F:
        def get(self, url, **k):
            class R:
                status, content_type, url_ = 200, "text/html", url
            r = R()
            r.url = url
            r.text = ("<html><body>Contact the clerk: <a href='mailto:records@cityofx.com'>records</a> "
                      "or webmaster@freemail.example. Also cityclerk@cityofx.com</body></html>")
            return r
    home = "<html><body><a href='/open-records'>Open Records Requests</a> <a href='/parks'>Parks</a></body></html>"
    rc = find_records_contact(F(), home, "https://www.cityofx.com/")
    assert rc["emails"] == ["records@cityofx.com", "cityclerk@cityofx.com"] and rc["page"].endswith("/open-records")
    home2 = "<html><body><a href='https://cityofy.govqa.us/WEBAPP/_rs/'>Public Records Request</a></body></html>"
    assert find_records_contact(F(), home2, "https://cityofy.gov/")["portal"].startswith("https://cityofy.govqa.us")


def test_pdf_goes_into_ordinances_box(tmp_path):
    pytest.importorskip("pdfplumber")
    from gacrawl import screens
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    d = tmp_path / "imports" / "screens"
    d.mkdir(parents=True)
    c = canvas.Canvas(str(d / "canton__code.pdf"))
    c.drawString(72, 720, "CODE OF ORDINANCES")
    c.drawString(72, 700, "Sec. 18-3. Short-term rentals. No dwelling may be rented without a permit.")
    c.save()
    store = Store(tmp_path / "data")
    jur = [{"id": "canton", "name": "Canton", "kind": "city"}]
    stats = screens.Inbox(tmp_path, store, [], log=lambda *_: None, jurisdictions=jur).ingest()
    box = screens.Inbox.box_export(store)
    assert stats["new"] == 1 and len(box["ordinances"]) == 1
    item = box["ordinances"][0]
    assert item["jurisdictions"] == ["canton"] and "Short-term rentals" in item["text"]
    store.close()


# ----------------------------------------------------------------------------- address vetting
def _j(jid, emails, page, kind="county", portal=None, pop=1000):
    return {"id": jid, "name": jid, "kind": kind, "population": pop,
            "records": {"emails": emails, "page": page, "portal": portal}}


def test_vet_prefers_records_address_and_drops_departments():
    j = _j("co-x", ["fcboh_openrecords@dph.ga.gov", "orr@xcounty.gov", "customerservice@xcounty.gov"], "https://xcounty.gov/orr")
    assert orr.vet(j)["to"] == "orr@xcounty.gov"
    j = _j("r", ["firerecords@r.gov", "policerecords@r.gov", "cityclerkoffice@r.gov"], "https://r.gov/records")
    assert orr.vet(j)["to"] == "cityclerkoffice@r.gov"
    j = _j("d", ["dkpdopenrecords@d.gov", "e911openrecords@d.gov"], "https://d.gov/records")
    assert orr.vet(j)["how"] == "none"


def test_vet_people_and_info_need_review_portal_wins_over_them():
    assert orr.vet(_j("c", ["newsroom@c.gov", "lisa.cupid@c.gov"], "https://c.gov/clerk"))["how"] == "review"
    v = orr.vet(_j("g", ["meagean.diaz@g.com"], "https://g.com/media-relations/open-records", portal="https://g.govqa.us/x"))
    assert v["how"] == "portal"
    assert orr.vet(_j("i", ["info@i.com"], "https://i.com/orr"))["how"] == "review"


def test_vet_junk_and_bad_portals():
    assert orr.vet(_j("t", ["test@test.gov", "citymanager@test.gov"], "https://t.org/orr"))["how"] == "none"
    assert orr.vet(_j("p", [], "https://p.gov/", portal="https://p.gov/uploads/Banner-OpenRecords.jpg"))["how"] == "none"
    assert orr.vet(_j("s", [], "https://s.us/", portal="https://scountysheriffga.nextrequest.com/"))["how"] == "none"
    assert orr.vet(_j("ct", ["dococlerks@ct.us"], "https://ct.us/Courts/Clerk-of-Court"))["how"] == "review"
    assert orr.vet(_j("cc", ["openrecords@carroll.gov"], "http://www.carroll.com/orr"))["to"] == "openrecords@carroll.gov"


def test_plan_skips_school_districts_and_skip_list_uses_approved(tmp_path):
    js = [_j("sd-a", ["openrecords@a.org"], "https://a.org/orr", kind="school_district", pop=9),
          _j("co-b", ["jane.doe@b.gov"], "https://b.gov/orr", pop=5), _j("co-c", ["clerk@c.gov"], "https://c.gov/", pop=3)]
    reviews = []
    items = orr.plan(js, orr.Tracker(tmp_path), reviews=reviews)
    assert [i["jurisdiction"]["id"] for i in items] == ["co-c"] and [r["jurisdiction"]["id"] for r in reviews] == ["co-b"]
    items = orr.plan(js, orr.Tracker(tmp_path), contacts={"approved": {"co-b": "jane.doe@b.gov"}, "skip": ["co-c"]})
    assert [(i["jurisdiction"]["id"], i["to"]) for i in items] == [("co-b", "jane.doe@b.gov")]
    p = orr.write_review(tmp_path, reviews)
    assert "co-b" in p.read_text() and "jane.doe@b.gov" in p.read_text()


class InboxIMAP:
    def __init__(self, subjects):
        self.subjects = subjects

    def login(self, u, p):
        pass

    def select(self, *a, **k):
        return "OK", [b"1"]

    def search(self, charset, *crit):
        token = crit[-1].strip('"')
        hits = [str(i + 1).encode() for i, s in enumerate(self.subjects) if token in s]
        return "OK", [b" ".join(hits)]

    def logout(self):
        pass


def test_self_test_sends_marked_sample_and_finds_it(tmp_path):
    FakeSMTP.sent = []
    reqs = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV))
    inbox = []
    r = reqs.self_test(J, smtp_factory=FakeSMTP,
                       imap_factory=lambda: InboxIMAP(inbox + [m["Subject"] for m in FakeSMTP.sent]), wait_seconds=0)
    assert r["smtp"].startswith("ok") and r["imap"].startswith("ok") and r["seen_in_inbox"] is True
    m = FakeSMTP.sent[0]
    assert m["To"] == "requests@project.example" and m["Subject"].startswith("[TEST selftest-") and "[ORR-" not in m["Subject"]
    assert "TEST ONLY" in m.get_content() and not (tmp_path / "data" / "openrecords" / "requests.json").exists()


def test_self_test_without_secrets_sends_nothing(tmp_path):
    r = orr.Requests(tmp_path, CFG, orr.Mailbox({})).self_test(J)
    assert r["smtp"].startswith("not tried")


def test_portal_override(tmp_path):
    js = [_j("co-p", ["jane.doe@p.gov"], "https://p.gov/orr")]
    items = orr.plan(js, orr.Tracker(tmp_path), contacts={"approved": {}, "portals": {"co-p": "https://p.govqa.us/"}, "skip": []})
    assert [(i["how"], i["to"]) for i in items] == [("portal", "https://p.govqa.us/")]


def test_export_open_records_has_no_addresses(tmp_path):
    import json as _j
    from gacrawl.export import export_open_records
    t = orr.Tracker(tmp_path)
    t.log("big", "sent", "to records@bigtown.gov", to="records@bigtown.gov", sent_at="2026-11-07T04:10:00+00:00",
          due="2026-11-11", name="Bigtown", found_on="https://bigtown.gov/orr", message_id="<x@y>")
    t.log("portal", "portal_only", "submit by hand", portal="https://p.govqa.us/", name="Portalville")
    t.save()
    counts = export_open_records(tmp_path, tmp_path / "o.json")
    txt = (tmp_path / "o.json").read_text()
    assert counts == {"sent": 1, "portal_only": 1} and "records@bigtown.gov" not in txt and "<x@y>" not in txt
    rows = {r["jurisdiction"]: r for r in _j.loads(txt)["requests"]}
    assert rows["big"]["how"] == "email" and rows["portal"]["portal"] == "https://p.govqa.us/"


def test_cost_detection():
    assert orr.mentions_cost("The estimated cost is $45.50 for 300 pages.") == ["$45.50"]
    assert orr.mentions_cost("There is a fee; we will send an invoice.") == ["amount not stated"]
    assert orr.mentions_cost("Attached is the code you asked for. Thanks!") == []


def test_cost_reply_notifies(tmp_path):
    sent_to = []
    reqs = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), notify=lambda t, b: sent_to.append((t, b)) or "issue#1")
    FakeSMTP.sent = []
    reqs.send_batch(J, smtp_factory=FakeSMTP)
    mid = reqs.tracker.data["big"]["message_id"]
    reply = EmailMessage()
    reply["From"] = "clerk@bigtown.gov"; reply["Subject"] = "Re: " + reqs.tracker.data["big"]["subject"]
    reply["In-Reply-To"] = mid; reply["Message-ID"] = "<r1@bigtown.gov>"
    reply.set_content("The copying fee will be $62.00. Please confirm before we proceed.")

    class Box(InboxIMAP):
        def search(self, *a):
            return "OK", [b"1"]

        def fetch(self, i, what):
            return "OK", [(b"1", reply.as_bytes())]

    out = reqs.check_replies(imap_factory=lambda: Box([]))
    assert out["cost_notices"] == 1 and reqs.tracker.data["big"]["status"] == "cost_estimate"
    assert "Bigtown" in sent_to[0][0] and "$62.00" in sent_to[0][1]



def test_deadlines_open_one_issue_and_comment_new(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    calls = []
    issues = []

    def gh(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return issues
        if method == "POST" and path.endswith("/issues"):
            issues.append({"number": 7, "title": body["title"], "html_url": "u"}); return issues[-1]
        return {}

    reqs = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV))
    reqs.tracker.log("big", "sent", "x", name="Bigtown", sent_at="2026-11-02T04:00:00+00:00", due="2026-11-05")
    reqs.tracker.log("co-small", "sent", "x", name="Small County", sent_at="2099-01-01T04:00:00+00:00", due="2099-01-06")
    out = reqs.deadlines(gh=gh)
    assert out["late"] == 1 and calls[1][0] == "POST" and "Bigtown" in calls[1][2]["body"]
    assert reqs.deadlines(gh=gh)["newly_late"] == 0          # no second notice for the same place
    reqs.tracker.log("big", "acknowledged", "reply")
    reqs.deadlines(gh=gh)
    assert calls[-1][2]["state"] == "closed"
