"""Reading a reply to a Code of Ordinances request (openrecords.Requests.check_replies), from synthetic messages.

Nov 19, 2026 (run 44556142200): Wadley's clerk had acknowledged; then a city employee sent a zoning review letter for a
developer into the ORR-wadley thread, its only attachment a 512x512 logo. The tracker marked the request fulfilled,
saved the logo as Wadley's code (imports/ordinances/, which the withdraw step and the ordinances build read) and opened
"Wadley mentions a cost" for the letter's $30. Nothing here sends mail or opens an issue: the IMAP box is a fake and
every notice goes to a list."""
import email.utils
import io
from email.message import EmailMessage

import pytest

from gacrawl import openrecords as orr

ENV = {"ORR_SMTP_HOST": "smtp.example", "ORR_USER": "u", "ORR_PASSWORD": "p", "ORR_FROM": "requests@project.example",
       "ORR_IMAP_HOST": "imap.example"}
CFG = {"openrecords": {"enabled": True, "requester": "Georgia Civic Records Project", "daily_limit": 10}}


def orig(jid):
    return f"<orig-{jid}@project.example>"


class Box:
    """A fake IMAP mailbox holding the given messages."""

    def __init__(self, msgs):
        self.msgs = msgs

    def login(self, u, p):
        pass

    def select(self, *a, **k):
        pass

    def search(self, *a):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.msgs)))]

    def fetch(self, i, what):
        return "OK", [(b"", self.msgs[int(i) - 1].as_bytes())]

    def logout(self):
        pass


class NoMail:
    """An SMTP stand-in that fails the test if anything is sent."""

    def login(self, *a):
        pass

    def send_message(self, msg):
        raise AssertionError(f"a message was sent: {msg['Subject']}")

    def quit(self):
        pass


def requests_for(tmp_path, jid="wadley", name="Wadley", status="acknowledged"):
    notices = []
    r = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), log=lambda *_: None,
                     notify=lambda title, body: notices.append((title, body)) or f"issue#{len(notices)}")
    r.tracker.log(jid, "sent", "to the clerk", to=f"cityclerk@{jid}ga.gov", subject=orr.SUBJECT.format(jid=jid),
                  message_id=orig(jid), sent_at="2026-11-10T15:24:47+00:00", due="2026-11-13", name=name)
    if status != "sent":
        r.tracker.log(jid, status, "an earlier reply")
    r.tracker.save()
    return r, notices


def check(r, *msgs):
    return r.check_replies(imap_factory=lambda: Box(list(msgs)))


def quoted(jid="wadley", name="Wadley"):
    """Our letter as a reply quotes it (Zoho style, as Wadley's mail does)."""
    _, letter = orr.draft({"id": jid, "name": name, "kind": "city"}, "Georgia Civic Records Project",
                          "requests@project.example")
    return (f"\n\nFrom: Pat Requester <requests@project.example>\nTo: <cityclerk@{jid}ga.gov>\n"
            f"Date: Tue, 10 Nov 2026 10:24:46 -0500\nSubject: {orr.SUBJECT.format(jid=jid)}\n\n{letter}")


def reply(jid, text, frm="Clerk <cityclerk@wadleyga.gov>", subject=None, html=None):
    m = EmailMessage()
    m["From"] = frm
    m["Subject"] = subject or "Re: " + orr.SUBJECT.format(jid=jid)
    m["In-Reply-To"] = m["References"] = orig(jid)
    m["Message-ID"] = email.utils.make_msgid(domain="city.example")
    if text is not None:
        m.set_content(text)
        if html:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html, subtype="html")
    return m


def add(m, data, filename, maintype="image", subtype="png", disposition="attachment", cid=None, named_only=False):
    """Attach a part: a normal attachment, an inline one (disposition inline, Content-ID), or one with only a
    Content-ID and a name in its Content-Type (Outlook's signature images)."""
    if m.get_content_type() != "multipart/mixed":
        m.make_mixed()
    p = EmailMessage()
    if named_only:
        p.set_content(data, maintype=maintype, subtype=subtype, cid=cid, params={"name": filename})
    else:
        p.set_content(data, maintype=maintype, subtype=subtype, filename=filename, disposition=disposition, cid=cid)
    m.attach(p)
    return m


def png(size=(512, 512)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", size, (30, 60, 120, 255)).save(buf, "PNG")
    return buf.getvalue()


def jpg():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (850, 1100), "white").save(buf, "JPEG")
    return buf.getvalue()


def pdf():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "PDF")
    return buf.getvalue()


def codes(tmp_path):
    d = tmp_path / "imports" / "ordinances"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def held(tmp_path):
    d = tmp_path / orr.HELD_DIR
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def saved_notice(notices, files, rule=None):
    """The one notice a reply that saved files as the Code sends (fifth Nov 19 review): it names each file saved, the
    rule that took it, and how to undo it."""
    assert len(notices) == 1, notices
    body = notices[0][1]
    for f in files:
        assert f"- Saved as the Code: `imports/ordinances/{f}`, by " in body, (f, body)
    assert "- To undo: move" in body and "data/openrecords/attachments/" in body and "history" in body
    if rule:
        assert rule in body, body
    return notices[0]


WADLEY_LETTER = """Here's the complete letter, top to bottom, with all of the engineer's changes worked in:


Subject: Majestic Crown Cottage Court, Parcel W004-062 (N. MLK Jr. Blvd.): Zoning Review, Construction Standards and Site Plan Requirements

[Name],

Thank you for your interest in developing housing in the City of Wadley. We have reviewed the Majestic Crown Cottage Court Final Board Submittal Package and the Plat of Survey prepared by Cornerstone Land Surveying, dated January 28, 2026. This letter explains what the City Code allows on this property, the construction standards that apply, and what we need before the request can move forward.

1. Summary of the Request

Your package requests a zoning change, planned residential development approval, or similar approval to allow:

16 detached cottage homes on a single parcel of about 2.1 acres, served by a shared private drive, with common open space and a mail kiosk.

2. Zoning

The parcel is zoned R-1 Single-Family Residential. Under the zoning ordinance, more than one principal dwelling on a lot needs a planned residential development approval by the City Council after a public hearing, and the Planning Commission makes a recommendation first.

3. Construction Standards

Every home must meet the adopted building codes. The engineer's comments add a minimum finished floor elevation one foot above the crown of the drive, paved parking for two cars per home, and a sidewalk along the frontage on N. MLK Jr. Blvd.

4. Fees

The rezoning application fee is $30, payable to the City of Wadley when the application is filed, plus the cost of the legal notice in the county paper.

5. What We Need Before the Hearing

- A revised site plan showing the drive width, turning radius for fire apparatus, and setbacks.
- A drainage study sealed by a Georgia professional engineer.
- Confirmation from the water and sewer department that capacity is available.

Please contact City Hall with any questions about these items.

Sincerely,

Dwayne Flowers
City of Wadley"""


# ----------------------------------------------------------------------------- the Wadley case
@pytest.mark.parametrize("variant", ["attached logo", "inline logo", "letter as a PDF"])
def test_wadley_zoning_letter_in_the_request_thread_changes_nothing(tmp_path, variant):
    r, notices = requests_for(tmp_path)
    m = reply("wadley", WADLEY_LETTER + quoted(), frm="Dwayne Flowers <dflowers@wadleyga.gov>")
    if variant == "attached logo":
        add(m, png(), "Gem_reduce_25.png")
    elif variant == "inline logo":
        add(m, png(), "Gem_reduce_25.png", disposition="inline", cid="<gem25@wadleyga.gov>")
    else:
        add(m, pdf(), "Majestic_Crown_Zoning_Review.pdf", "application", "pdf")
    out = check(r, m)
    t = r.tracker.data["wadley"]
    assert t["status"] == "acknowledged"                      # the clerk's acknowledgement still stands
    assert codes(tmp_path) == [] and "files" not in t       # nothing counts as Wadley's code
    assert held(tmp_path) == {"attached logo": ["wadley__Gem_reduce_25.png"], "inline logo": [],
                              "letter as a PDF": ["wadley__Majestic_Crown_Zoning_Review.pdf"]}[variant]
    last = t["replies"][-1]
    assert last["note"] == orr.NOT_ABOUT and last["unrelated"] and last["files"] == []
    assert "cost_notices" not in out and out["unrelated"] == 1
    assert len(notices) == 1 and "unrelated" in notices[0][0] and "cost" not in notices[0][0]
    assert "dflowers@wadleyga.gov" in notices[0][1] and "To the Records Custodian" not in notices[0][1]
    assert t["history"][-1]["note"] == "unrelated-message notice: issue#1"
    assert not orr.Requests.answered({"replies": [last]})


def test_wadley_letter_without_its_subject_line_is_still_unrelated():
    own = orr.own_text(WADLEY_LETTER.replace(
        "Subject: Majestic Crown Cottage Court, Parcel W004-062 (N. MLK Jr. Blvd.): Zoning Review, Construction "
        "Standards and Site Plan Requirements\n", "") + quoted())
    assert "City Code" in own and "Records Custodian" not in own
    assert not orr.about_request(own, "", [], 1)
    assert orr.reply_costs(own) == ["$30"]          # what the old reading saw; only read for replies about the request


def test_the_quoted_original_is_not_the_reply():
    own = orr.own_text("We acknowledge your request.\r\n\r\n" + quoted().replace("\n", "\r\n"))
    assert own == "We acknowledge your request."
    gmail = ("Attached.\n\nOn Fri, Nov 6, 2026 at 8:33 PM Pat Requester <requests@project.example>\nwrote:\n\n"
             "> To the Records Custodian, the City of X:\n> I request an electronic copy of the Code of Ordinances")
    assert orr.own_text(gmail) == "Attached."
    outlook = ("Please see the link below.\n\n________________________________\nFrom: Pat Requester\nSent: Friday\n"
               "To: Clerk\nSubject: Open Records Request\n\nTo the Records Custodian")
    assert orr.own_text(outlook) == "Please see the link below."


# ----------------------------------------------------------------------------- documents: the Code
def test_inline_logo_with_a_pdf_code_keeps_only_the_code(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    html = ('<p>Good morning,</p><p>Attached is our Code of Ordinances.</p><p>Thanks,<br>'
            '<img src="cid:image001.png@01DD7039.BD299A50"><br>Shelia Cooper, City Clerk</p>')
    m = reply("big", "Good morning,\n\nAttached is our Code of Ordinances.\n\nThanks,\n"
              "[cid:image001.png@01DD7039.BD299A50]\nShelia Cooper, City Clerk" + quoted("big", "Bigtown"), html=html)
    add(m, png((2480, 2619)), "image001.png", disposition="inline", cid="<image001.png@01DD7039.BD299A50>")
    add(m, png((32, 32)), "facebook_32x32.png", cid="<facebook_32x32.png>", named_only=True)  # Outlook: a cid, no disposition
    add(m, pdf(), "Code of Ordinances.pdf", "application", "pdf")
    out = check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "fulfilled" and t["files"] == ["big__Code_of_Ordinances.pdf"]
    assert codes(tmp_path) == ["big__Code_of_Ordinances.pdf"] and held(tmp_path) == []
    assert out["attachments"] == 1
    saved_notice(notices, ["big__Code_of_Ordinances.pdf"], 'by its name ("Code of Ordinances.pdf")')


@pytest.mark.parametrize("text", ["Please see attached.", "", "Here you go."])
def test_a_pdf_named_as_the_code_alone_is_the_code(tmp_path, text):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = add(reply("big", text + quoted("big", "Bigtown")), pdf(), "Bigtown Code.pdf", "application", "pdf")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "fulfilled" and codes(tmp_path) == ["big__Bigtown_Code.pdf"]
    saved_notice(notices, ["big__Bigtown_Code.pdf"], "by its name")


@pytest.mark.parametrize("text", ["Please see attached.", "", "Here you go."])
def test_a_generic_pdf_with_a_cover_note_alone_is_held(tmp_path, text):
    """Fourth Nov 19 review: "See attached." with "Scan_001.pdf" reads the same in mail meant for someone else."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = add(reply("big", text + quoted("big", "Bigtown")), pdf(), "scan0001.pdf", "application", "pdf")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "acknowledged" and codes(tmp_path) == []
    assert held(tmp_path) == ["big__scan0001.pdf"] and [n[0] for n in notices] == [
        "Open records: Bigtown sent a document to look at"]
    assert "imports/ordinances/" in notices[0][1] and "data/openrecords/attachments/big__scan0001.pdf" in notices[0][1]


@pytest.mark.parametrize("how", ["exchange", "apple"])
def test_a_document_marked_inline_or_given_a_content_id_is_still_a_document(tmp_path, how):
    """Exchange gives ordinary attachments a Content-ID; Apple Mail marks them inline. A PDF is not a logo."""
    r, _ = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Attached is our code." + quoted("big", "Bigtown"))
    if how == "exchange":
        add(m, pdf(), "Code.pdf", "application", "pdf", cid="<5F1A@namprd04.prod.outlook.com>")
    else:
        add(m, pdf(), "Code.pdf", "application", "pdf", disposition="inline")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "fulfilled" and codes(tmp_path) == ["big__Code.pdf"]


def test_document_types_the_ordinances_build_reads_count_and_spreadsheets_do_not(tmp_path):
    r, _ = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Attached are our code and charter." + quoted("big", "Bigtown"))
    add(m, b"<html><body>Sec. 1-1</body></html>", "code.htm", "text", "html")
    add(m, b"PK\x03\x04", "chapters.zip", "application", "zip")
    add(m, b"sheet", "fees.xlsx", "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    add(m, pdf(), "Charter", "application", "pdf")          # no extension: named from its type
    check(r, m)
    assert codes(tmp_path) == ["big__Charter.pdf", "big__chapters.zip", "big__code.htm"]
    from gacrawl import ordinance_files

    assert ordinance_files.DOC_EXT <= set(orr.DOC_TYPES)


# ----------------------------------------------------------------------------- images: not the Code
def test_images_alone_are_held_and_change_nothing(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Please see attached." + quoted("big", "Bigtown"))
    add(m, jpg(), "IMG_0001.jpg", subtype="jpeg")
    add(m, jpg(), "IMG_0002.jpg", subtype="jpeg")
    out = check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "sent" and codes(tmp_path) == [] and "files" not in t
    assert held(tmp_path) == ["big__IMG_0001.jpg", "big__IMG_0002.jpg"] == t["held"]
    assert out["held"] == 2 and len(notices) == 1 and "images" in notices[0][0]
    assert "data/openrecords/attachments/big__IMG_0001.jpg" in notices[0][1]
    # the clerk did answer: no "I have not yet received a response", and not late
    t["sent_at"] = "2026-10-01T12:00:00+00:00"
    assert r.follow_ups(smtp_factory=NoMail)["sent"] == 0
    assert r.deadlines()["late"] == 0


def test_images_said_to_be_the_codes_pages_make_it_partial(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    m = reply("big", "Attached are the pages of our code." + quoted("big", "Bigtown"))
    add(m, jpg(), "page1.jpg", subtype="jpeg")
    add(m, jpg(), "page2.jpg", subtype="jpeg")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "partial"
    assert codes(tmp_path) == [] and held(tmp_path) == ["big__page1.jpg", "big__page2.jpg"]
    assert len(notices) == 1 and "images" in notices[0][0]


def test_a_code_scanned_as_many_page_images_counts_as_the_code(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    m = reply("big", "Attached are the scanned pages of our Code of Ordinances (it is not online)." +
              quoted("big", "Bigtown"))
    for i in range(1, 5):
        add(m, jpg(), f"Code_page_{i}.jpg", subtype="jpeg")
    check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "fulfilled" and len(t["files"]) == 4 and held(tmp_path) == []
    assert codes(tmp_path) == [f"big__Code_page_{i}.jpg" for i in range(1, 5)]   # where the build reads them (OCR)
    assert len(notices) == 1 and "page images" in notices[0][0]


def test_two_images_said_to_be_the_code_are_not_enough_to_count(tmp_path):
    r, _ = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    m = reply("big", "Attached are the scanned pages of our Code of Ordinances." + quoted("big", "Bigtown"))
    for i in (1, 2):
        add(m, jpg(), f"p{i}.jpg", subtype="jpeg")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "partial" and codes(tmp_path) == []


# ----------------------------------------------------------------------------- text-only replies
@pytest.mark.parametrize("text", [
    "Our code is online at municode: https://library.municode.com/ga/bigtown/codes/code_of_ordinances",
    "Our code is online at Municode.",
    "Ordinances are available on the county website at bigtown.org.",
])
def test_the_code_linked_or_online_is_partial(tmp_path, text):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    check(r, reply("big", text + quoted("big", "Bigtown")))
    assert r.tracker.data["big"]["status"] == "partial" and notices == [] and codes(tmp_path) == []


def test_the_code_linked_in_an_html_only_reply(tmp_path):
    r, _ = requests_for(tmp_path, "big", "Bigtown", "sent")
    html = ("<div>Our code is online at <a href='https://library.municode.com/ga/bigtown'>Municode</a>.</div>"
            "<div class='gmail_quote'>On Tue, Nov 10, 2026 Pat Requester wrote:<blockquote>To the Records "
            "Custodian</blockquote></div>")
    check(r, reply("big", None, html=html))
    assert r.tracker.data["big"]["status"] == "partial"


def test_an_acknowledgement(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    check(r, reply("big", "Good Morning:\n\nWe acknowledge your request.  You can expect the information by the end "
                          "of business, Monday, November 16th.  Thank you." + quoted("big", "Bigtown")))
    assert r.tracker.data["big"]["status"] == "acknowledged" and notices == []


def test_a_cost_reply_notifies_from_its_own_text(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    out = check(r, reply("big", "The cost for copies is $25." + quoted("big", "Bigtown")))
    assert r.tracker.data["big"]["status"] == "cost_estimate" and out["cost_notices"] == 1
    assert len(notices) == 1 and "mentions a cost" in notices[0][0] and "$25" in notices[0][1]
    assert "To the Records Custodian" not in notices[0][1]          # our own letter is not quoted back to us


def test_an_amount_in_the_quoted_thread_is_not_a_cost(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    text = ("We acknowledge your request and will send the code on Friday.\n\n-----Original Message-----\n"
            "From: Planning <planning@bigtown.gov>\nSent: Monday, November 16, 2026 9:00 AM\nSubject: fees\n\n"
            "The rezoning application fee is $30, payable when filed.")
    out = check(r, reply("big", text))
    assert r.tracker.data["big"]["status"] == "acknowledged" and "cost_notices" not in out and notices == []


def test_a_bounce_saves_nothing(tmp_path):
    r, notices = requests_for(tmp_path, "co-brantley", "Brantley County", "sent")
    m = reply("co-brantley", "** Address not found **\n\nYour message wasn't delivered to clerk@brantleycounty-ga.gov "
                             "because the address couldn't be found.\n\n550 5.1.1 User unknown",
              frm="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
              subject="Delivery Status Notification (Failure)")
    add(m, png((64, 64)), "icon.png")
    check(r, m)
    t = r.tracker.data["co-brantley"]
    assert t["status"] == "bounced" and codes(tmp_path) == [] and held(tmp_path) == [] and notices == []
    assert t["replies"][-1]["files"] == []


def test_a_thank_you_after_a_withdrawal_with_signature_icons_changes_nothing(tmp_path):
    """Doraville, Nov 11: 'My pleasure' after our withdrawal, seven logos and social icons in the signature."""
    r, notices = requests_for(tmp_path, "doraville", "Doraville", "withdrawn")
    m = reply("doraville", "My pleasure. Let me know if you need anything in the future. Have a great day ahead.\n\n"
                           "Shanta Bingham\nCity Clerk\n[cid:facebook_32x32_a126.png]<https://www.facebook.com/x>")
    for n in ("facebook_32x32_a126.png", "doraville_3026.png"):
        add(m, png((32, 32)), n, cid=f"<{n}>", named_only=True)
    check(r, m)
    t = r.tracker.data["doraville"]
    assert t["status"] == "withdrawn" and codes(tmp_path) == [] and held(tmp_path) == [] and notices == []


def test_an_out_of_office_reply_is_no_answer_and_no_notice(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "I am out of the office until Monday with limited access to email.",
              subject="Automatic reply: " + orr.SUBJECT.format(jid="big"))
    m["Auto-Submitted"] = "auto-replied"
    check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "sent" and t["replies"][-1]["note"] == "automatic reply" and notices == []
    assert not orr.Requests.answered(t)


def test_an_unrelated_reply_is_not_an_answer_for_the_deadline(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GACRAWL_ISSUES_REPO", raising=False)
    r, notices = requests_for(tmp_path, "wadley", "Wadley", "sent")
    check(r, reply("wadley", WADLEY_LETTER, frm="Dwayne Flowers <dflowers@wadleyga.gov>"))
    assert r.tracker.data["wadley"]["status"] == "sent" and len(notices) == 1
    assert r.deadlines()["late"] == 1


# ----------------------------------------------------------------------------- the classifier on real replies
@pytest.mark.parametrize("text", [
    "Attached is our code",
    "Our code is online at municode",
    "We acknowledge your request",
    "The cost for copies is $25",
    "Approximately $100.00 to fulfill this request.",                     # Waverly Hall, Nov 11
    "The total amount you will have to pay minus the first 15 min will be $186.65 and to get this digitized will "
    "take a week.",                                                       # Sale City, Nov 12
    "Our office is closed for the Veterans Day holiday on our return to work Thursday I will be able to give this "
    "the attention it needs so I can gather what you are asking for.",
    "These resolutions are also currently in effect, with a proposed 90-day extension being voted on Monday.",
    "Please be advised all this information is available for download on our website, www.cityofconcordga.gov "
    "under the Government tab.",
    "Good afternoon,\n\nI have received your message which was forwarded to me from the police department.\n\nI am "
    "working with the audit team this week.\n\nI will work on your request once I return.",
])
def test_real_replies_are_about_the_request(text):
    assert orr.about_request(orr.own_text(text + quoted()), "", [], 0)


def test_who_is_unrelated():
    assert not orr.about_request(orr.own_text(WADLEY_LETTER + quoted()), "", [], 1)
    # a note greeting someone else is theirs; a person's reply that names nothing else is an answer (third review)
    assert not orr.about_request("Tom,\n\nAre we still on for lunch Thursday?", "", [], 0)
    assert orr.about_request("Are we still on for lunch Thursday?", "", [], 0)
    assert orr.about_request("", "", [], 1)                                  # a file alone, in the request's thread
    assert orr.about_request("See attached.", "", ["Resolution_2026-12.pdf"], 1)
    assert orr.reply_costs("There is no fee for this request.") == []


def test_an_exchange_footer_part_is_not_a_document(tmp_path):
    """Exchange appends a server's footer as ATT00001.htm / .txt; now that web pages and text count as documents,
    an acknowledgement carrying one must not read as the Code."""
    r, _ = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "We acknowledge your request and will reply by Friday." + quoted("big", "Bigtown"))
    add(m, b"<html><body>This message has been scanned for viruses.</body></html>", "ATT00001.htm", "text", "html")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "acknowledged" and codes(tmp_path) == [] and held(tmp_path) == []


def test_a_short_note_with_another_document_is_not_the_code(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    m = add(reply("big", "Here's the letter for the Smith parcel."), pdf(), "Smith_parcel.pdf", "application", "pdf")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "acknowledged" and codes(tmp_path) == []
    assert held(tmp_path) == ["big__Smith_parcel.pdf"] and len(notices) == 1 and "unrelated" in notices[0][0]
    # the same note naming the code is the code
    assert orr.about_request("Here's the letter and our code.", "", [], 1)


# ----------------------------------------------------------------------------- Nov 19 review of this change
SIG = ("\n\nThank you,\n\nMary Smith, CMC\nCity Clerk\nCity of Smallville\n123 Main Street\nSmallville, GA 30000\n"
       "(478) 555-1212\n")
FIRST = "Here's the complete letter, top to bottom, with all of the engineer's changes worked in:"


def unrelated_and_nothing_counted(tmp_path, r, notices, jid="wadley", status="acknowledged"):
    t = r.tracker.data[jid]
    assert t["status"] == status and codes(tmp_path) == [] and "files" not in t
    assert len(notices) == 1 and "unrelated" in notices[0][0] and "cost" not in notices[0][0]
    assert t["replies"][-1]["note"] == orr.NOT_ABOUT


@pytest.mark.parametrize("first", [
    "Here's the complete letter on what the City Code allows on the Majestic Crown parcel, with all of the engineer's "
    "changes worked in:",
    "Here's the complete zoning ordinance review letter, top to bottom, with all of the engineer's changes worked in:",
])
@pytest.mark.parametrize("attach", ["logo", "pdf"])
def test_wadley_letter_naming_a_code_in_its_first_line_is_still_unrelated(tmp_path, first, attach):
    """1: a bare "City Code" / "zoning ordinance" in the first line read as about the request, and the pasted
    Subject line for another matter was only looked at afterwards."""
    r, notices = requests_for(tmp_path)
    m = reply("wadley", WADLEY_LETTER.replace(FIRST, first) + quoted(), frm="Dwayne Flowers <dflowers@wadleyga.gov>")
    if attach == "logo":
        add(m, png(), "Gem_reduce_25.png")
    else:
        add(m, pdf(), "Majestic_Crown_Zoning_Review.pdf", "application", "pdf")
    out = check(r, m)
    unrelated_and_nothing_counted(tmp_path, r, notices)
    assert "cost_notices" not in out
    # without its pasted Subject line, the first line still says it is a letter for another matter
    letter = WADLEY_LETTER.replace(FIRST, first)
    no_subject = "\n".join(ln for ln in letter.splitlines() if not ln.startswith("Subject:"))
    assert not orr.about_request(orr.own_text(no_subject), "", ["Zoning_Ordinance_Section_4.pdf"], 1)
    # nor does a file plainly named as a code make the full letter (another matter's Subject line) about the request
    assert not orr.about_request(orr.own_text(letter), "", ["City Code.pdf"], 1)


@pytest.mark.parametrize("text,files", [
    ("Here is the code enforcement letter for 412 Elm St.", [("pdf", "Elm_St_Notice.pdf")]),
    ("Here is the code enforcement letter for 412 Elm St.", [("pdf", "412_Elm.pdf")]),
    ("Here are the code enforcement photos for 412 Elm St, the grass is still over 12 inches." + SIG,
     [("jpg", f"IMG_77{i}.jpg") for i in range(1, 5)]),
])
def test_code_enforcement_is_not_the_code(tmp_path, text, files):
    """1: "code enforcement" (and building, fire, zip codes) is not the Code of Ordinances."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    m = reply("big", text)
    for kind, n in files:
        add(m, pdf(), n, "application", "pdf") if kind == "pdf" else add(m, jpg(), n, subtype="jpeg")
    check(r, m)
    unrelated_and_nothing_counted(tmp_path, r, notices, "big")
    assert held(tmp_path) == sorted(f"big__{n}" for _, n in files)


@pytest.mark.parametrize("text,icons", [
    ("Good morning,\n\nHere is the link to our Code of Ordinances: https://library.municode.com/ga/bigtown",
     [("image001.png", 32), ("image002.png", 32), ("image003.png", 32), ("image004.png", 32)]),
    ("Here are the ordinances you asked for, they are on our website at https://www.bigtownga.gov/ordinances",
     [("logo.png", 64), ("facebook.png", 64), ("twitter.png", 64)]),
])
def test_signature_images_sent_as_attachments_with_a_link_are_not_the_code(tmp_path, text, icons):
    """2: Outlook (plain-text replies) and Zoho send signature images as ordinary attachments, no Content-ID; with
    "here is ... code" they were counted as scanned pages and the link ignored."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", text + SIG + quoted("big", "Bigtown"))
    for n, px in icons:
        add(m, png((px, px)), n)
    check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "partial" and codes(tmp_path) == [] and held(tmp_path) == [] and notices == []


def test_page_sized_images_with_a_link_to_the_code_are_not_the_code(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Attached are a few pages; the full code is at https://library.municode.com/ga/bigtown" + SIG)
    for i in range(4):
        add(m, jpg(), f"page{i}.jpg", subtype="jpeg")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "partial" and codes(tmp_path) == []
    assert len(held(tmp_path)) == 4 and "images" in notices[0][0]


def test_what_counts_as_a_page_image():
    assert orr._image_shape("IMG_0001.jpg", jpg()) == "page"                      # 850x1100
    assert orr._image_shape("scan.png", png((1700, 2200))) == "page"
    assert orr._image_shape("Gem_reduce_25.png", png()) == "other"               # Wadley's 512x512 logo
    assert orr._image_shape("image001.png", png((2480, 2619))) == "other"        # Flowery Branch's seal: square
    assert orr._image_shape("image001.png", png((32, 32))) == "icon"
    assert orr._image_shape("bluesky_0f64.png", png((500, 500))) == "icon"       # a social icon by its name
    assert not orr.code_attached("Here is the link to our Code of Ordinances: https://library.municode.com/x")
    assert not orr.code_attached("Here are the ordinances you asked for, they are on our website")
    assert orr.code_attached("Attached are the pages of our ordinances.")
    assert orr.code_attached("I scanned the code, attached are the pages.")


def test_iphone_photos_of_the_code_marked_inline_are_kept(tmp_path):
    """3: Apple Mail and iPhones mark attached photos Content-Disposition: inline with no Content-ID; they were
    taken for signature images and dropped, the status set from "do not have" (denied)."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Attached are the pages of our ordinances. We do not have them in electronic form.\n\n"
                     "Sent from my iPhone" + quoted("big", "Bigtown"))
    for i in range(1, 9):
        add(m, jpg(), f"IMG_04{i:02d}.jpeg", subtype="jpeg", disposition="inline")
    check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "fulfilled" and len(codes(tmp_path)) == 8 and held(tmp_path) == []
    assert len(notices) == 1 and "page images" in notices[0][0]


def test_iphone_html_photos_shown_by_cid_are_kept(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    html = "<div>Attached are the pages of our ordinances.</div>" + "".join(
        f"<img src='cid:IMG{i}@apple'>" for i in range(1, 6))
    m = reply("big", "Attached are the pages of our ordinances." + quoted("big", "Bigtown"), html=html)
    for i in range(1, 6):
        add(m, jpg(), f"IMG_05{i:02d}.jpeg", subtype="jpeg", disposition="inline", cid=f"<IMG{i}@apple>")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "fulfilled" and len(codes(tmp_path)) == 5
    assert "page images" in notices[0][0]


def test_a_page_image_with_an_unreferenced_content_id_is_held_not_dropped(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Please see attached." + quoted("big", "Bigtown"))
    add(m, png((1700, 2200)), "Code_page1.png", disposition="inline", cid="<p1@x>")
    check(r, m)
    assert codes(tmp_path) == [] and held(tmp_path) == ["big__Code_page1.png"]
    assert len(notices) == 1 and "images" in notices[0][0]


def test_the_clerks_response_letter_is_held_not_counted_as_the_code(tmp_path):
    """4: in a reply about the request, any document counted as the Code: the clerk's letter was saved to
    imports/ordinances/ and the request closed."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Good morning,\n\nPlease see the attached letter regarding your open records request." + SIG +
              quoted("big", "Bigtown"))
    add(m, pdf(), "ORR Response Letter 11-18-2026.pdf", "application", "pdf")
    out = check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "acknowledged" and codes(tmp_path) == [] and "files" not in t
    assert held(tmp_path) == ["big__ORR_Response_Letter_11-18-2026.pdf"] and out["document_notices"] == 1
    assert len(notices) == 1 and "a document to look at" in notices[0][0]
    assert "data/openrecords/attachments/big__ORR_Response_Letter_11-18-2026.pdf" in notices[0][1]
    assert "big" not in orr.downloaded_places(tmp_path, _Places())        # the place is not counted as having its code


def test_an_invoice_is_held_and_the_request_waits_on_the_cost(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Attached is the invoice for your records request. Once payment is received we will send the "
                     "documents." + SIG + quoted("big", "Bigtown"))
    add(m, pdf(), "Invoice_2026-0114.pdf", "application", "pdf")
    out = check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "cost_estimate" and codes(tmp_path) == [] and held(tmp_path) == ["big__Invoice_2026-0114.pdf"]
    assert out["cost_notices"] == 1 and len(notices) == 1 and "mentions a cost" in notices[0][0]
    assert "big__Invoice_2026-0114.pdf" in notices[0][1] and "waits for you" in notices[0][1]


@pytest.mark.parametrize("text,files,code,kept", [
    ("The cost to copy the Code is $85. Please see the attached estimate.", ["Scan_001.pdf"], [], "cost_estimate"),
    ("Here is the Code. The fee for copies is $10, payable at City Hall.", ["Scan.pdf"], ["Scan.pdf"], "fulfilled"),
    ("Please see the attached letter.", ["Letter.pdf", "Code of Ordinances.pdf"], ["Code_of_Ordinances.pdf"],
     "fulfilled"),
])
def test_documents_with_a_cost_or_a_cover_letter(tmp_path, text, files, code, kept):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", text + SIG)
    for n in files:
        add(m, pdf(), n, "application", "pdf")
    check(r, m)
    assert r.tracker.data["big"]["status"] == kept and codes(tmp_path) == [f"big__{c}" for c in code]
    assert len(held(tmp_path)) == len(files) - len(code) and len(notices) == 1   # a held letter is always told


def _Places():
    from gacrawl import ordinance_files

    return ordinance_files.Places([{"id": "big", "name": "Bigtown", "kind": "city"}])


@pytest.mark.parametrize("text,name", [
    ("Good afternoon,\n\nRE: Request dated 11/10/2026\n\nPlease find the attached documents.", "Smallville_Ordinances.pdf"),
    ("Good afternoon,\n\nRE: Pat Requester\n\nPlease find the attached documents.", "Smallville_Ordinances.pdf"),
    ("Good morning,\n\nI apologize for the delay in getting back to you. Our longtime clerk retired at the end of "
     "September and I have been filling in while the council hires her replacement.\n\n" +
     "It has been a busy season with the budget hearings, the election and the holiday parade all landing in the "
     "same month, and our small staff is stretched thin. " * 7 +
     "\n\nAttached is our city code along with the ordinances passed since it was last updated in 2019.", "Book1.pdf"),
])
def test_a_code_is_not_lost_to_a_re_line_or_a_long_reply(tmp_path, text, name):
    """5: a "RE:" line in the body decided before the file's name was looked at, and a reply over 200 words that
    names the code only in a later line read as unrelated."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = add(reply("big", text + SIG + quoted("big", "Bigtown")), pdf(), name, "application", "pdf")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "fulfilled" and codes(tmp_path) == [f"big__{name}"]
    assert held(tmp_path) == []
    saved_notice(notices, [f"big__{name}"])


@pytest.mark.parametrize("top", ["MEMORANDUM\n\n", "City of Bigtown\nOffice of the City Clerk\n\n", ""])
def test_a_memo_header_at_the_top_is_the_reply_not_a_quote(tmp_path, top):
    """6: own_text cut at the first From:/To:/Date: block even at the very top, where it is the clerk's own memo
    header: the fee went unseen, the reply read as unrelated, and a follow-up would say we had no response."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    memo = (top + "From: Mary Smith, City Clerk\nTo: Georgia Civic Records Project\nDate: November 18, 2026\n"
            "Re: Open Records Request\n\nThe estimated cost to copy the Code is $85.00. Please confirm before we "
            "proceed." + SIG)
    out = check(r, reply("big", memo + quoted("big", "Bigtown")))
    t = r.tracker.data["big"]
    assert t["status"] == "cost_estimate" and out["cost_notices"] == 1 and "$85.00" in notices[0][1]
    assert orr.Requests.answered(t) and "To the Records Custodian" not in notices[0][1]
    t["sent_at"] = "2026-10-01T12:00:00+00:00"
    assert r.follow_ups(smtp_factory=NoMail)["sent"] == 0


def test_our_own_quote_header_with_nothing_above_is_still_a_quote():
    ours = ("requests@project.example", "Georgia Civic Records Project")
    outlook = ("\n\n________________________________\nFrom: Georgia Civic Records Project <requests@project.example>\n"
               "Sent: Tuesday\nTo: Clerk\nSubject: Open Records Request\n\nTo the Records Custodian ... any cost")
    assert orr.own_text(outlook, ours) == ""
    zoho = quoted("big", "Bigtown").lstrip()                            # From: ... [ORR-big] with nothing above it
    assert orr.own_text(zoho, ours) == "" and orr.own_text(zoho) == ""
    gmail_less = "From: Pat Requester\nDate: Tue\nTo: clerk@x.gov\n\nTo the Records Custodian ... any cost"
    assert orr.own_text(gmail_less, ("Pat Requester",)) == ""


# ----------------------------------------------------------------------------- lesser findings of the same review
def test_an_out_of_office_naming_requests_is_still_no_answer(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Thank you for your email. I am out of the office until November 30. I will respond to your "
                     "request when I return.", subject="Out of Office: " + orr.SUBJECT.format(jid="big"))
    m["Auto-Submitted"] = "auto-replied"
    check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "sent" and t["replies"][-1]["note"] == "automatic reply" and notices == []
    assert not orr.Requests.answered(t)


def test_a_body_in_an_unknown_charset_is_still_read(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    raw = (b"Message-ID: <b@x>\r\nSubject: Re: [ORR-big]\r\nFrom: clerk@bigtown.gov\r\nContent-Type: text/plain; "
           b"charset=unknown-8bit\r\n\r\nThe cost is $5.\r\n")
    check(r, email.message_from_bytes(raw))
    assert r.tracker.data["big"]["status"] == "cost_estimate" and "mentions a cost" in notices[0][0]


def test_a_postfix_bounce_carrying_the_original_is_matched(tmp_path):
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = MIMEMultipart("report", report_type="delivery-status")
    m["From"] = "Mail Delivery System <MAILER-DAEMON@mx.bigtown.gov>"
    m["Subject"] = "Undelivered Mail Returned to Sender"
    m["Message-ID"] = email.utils.make_msgid(domain="mx.example")
    m.attach(MIMEText("I'm sorry to have to inform you that your message could not be delivered.\n"
                      "<cityclerk@bigtownga.gov>: user unknown"))
    original = EmailMessage()
    original["Message-ID"], original["Subject"] = orig("big"), orr.SUBJECT.format(jid="big")
    original.set_content("To the Records Custodian ...")
    part = MIMEBase("message", "rfc822")
    part.set_payload([original])
    m.attach(part)
    check(r, m)
    assert r.tracker.data["big"]["status"] == "bounced" and codes(tmp_path) == [] and notices == []


def test_a_long_file_name_keeps_its_extension(tmp_path):
    r, _ = requests_for(tmp_path, "big", "Bigtown", "sent")
    check(r, add(reply("big", "Attached is our code."), pdf(), "C" * 300 + ".pdf", "application", "pdf"))
    (name,) = codes(tmp_path)
    assert name.endswith(".pdf") and len(name) == 150


def test_flowery_branch_ordinances_titled_without_the_word_are_held_for_the_owner(tmp_path):
    """Nov 10 (real): the Municode link, "There is no fee for this request.", the city seal shown inline, and two
    ordinances adopted since the supplement whose titles lack the word "ordinance" ("Senate Bill", "Review
    Process"). Neither the names nor the text say they are ordinances (fourth Nov 19 review: a file counts only on
    positive evidence), so they are held with one notice; the link makes the request partial; the seal is dropped."""
    r, notices = requests_for(tmp_path, "flowery-branch", "Flowery Branch", "sent")
    text = ("Good morning,\n\nPlease use the link below for the online library to Municode:\n"
            "https://library.municode.com/ga/flowery_branch/codes/code_of_ordinances\n\nThere is no fee for this "
            "request.\n\nKind regards,\n\n\n[cid:image001.png@01DD7039.BD299A50]\nShelia Cooper, CMC\nCity Clerk")
    m = reply("flowery-branch", text + quoted("flowery-branch", "Flowery Branch"))
    add(m, png((2480, 2619)), "image001.png", disposition="inline", cid="<image001.png@01DD7039.BD299A50>")
    names = ["788_Establish_Compliance_with_Senate_Bill_447.pdf", "789_New_Digital_Review_Process_in_Planning.pdf"]
    for n in names:
        add(m, pdf(), n, "application", "pdf")
    check(r, m)
    assert r.tracker.data["flowery-branch"]["status"] == "partial" and codes(tmp_path) == []
    assert held(tmp_path) == [f"flowery-branch__{n}" for n in names]
    assert [n[0] for n in notices] == ["Open records: Flowery Branch sent documents to look at"]
    # the same reply saying what they are counts them
    r2, notices2 = requests_for(tmp_path / "b", "flowery-branch", "Flowery Branch", "sent")
    m = reply("flowery-branch", text.replace("There is no fee", "Attached are the two ordinances adopted since the "
                                             "last supplement. There is no fee") + quoted("flowery-branch",
                                                                                         "Flowery Branch"))
    for n in names:
        add(m, pdf(), n, "application", "pdf")
    check(r2, m)
    assert r2.tracker.data["flowery-branch"]["status"] == "fulfilled"
    saved_notice(notices2, [f"flowery-branch__{n}" for n in names],
                 'by the reply\'s words: "Attached are the two ordinances adopted since the last supplement."')
    assert codes(tmp_path / "b") == [f"flowery-branch__{n}" for n in names]


# ----------------------------------------------------------------------------- second Nov 19 review
@pytest.mark.parametrize("text,name", [
    ("Bill,\n\nAttached is the zoning review for 412 Elm Street. Let me know if the developer has questions.\n\nJane",
     "412 Elm St Zoning Review.pdf"),
    ("Hi Tom,\n\nAttached is the business license application for 205 Oak Ave. Please review before Monday.\n\nJane",
     "Business License Application - 205 Oak Ave.pdf"),
    ("Mayor,\n\nAttached is the draft FY2027 budget for your review before the work session.",
     "FY2027 Draft Budget.pdf"),
    ("Attached is the signed copy for 412 Elm St.", "Scan_0042.pdf"),
    ("Here is the information on the Smith property at 18 Church Street you asked about.", "Smith property.pdf"),
    ("Here is the information for Monday's work session.", "Water rate study.pdf"),
    ("Kim,\n\nAttached is a copy of the signed lease for the depot building.\n\nThanks", "Depot Lease signed.pdf"),
    ("Scanned the documents you dropped off this morning.", "20261119103322.pdf"),
    ("Mayor,\n\nAttached for your review.", "FY2027.pdf"),                 # a bare note, to someone else
    ("Hi Tom, see attached.", "Scan_0042.pdf"),
])
def test_misdirected_staff_mail_with_a_generic_document_is_not_the_code(tmp_path, text, name):
    """1: words any message with a file uses (attached, a copy, the documents, information, scanned) made such a note
    "about" the request, and a document with a generic name was then the Code whenever no cost was quoted."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    check(r, add(reply("big", text + quoted("big", "Bigtown")), pdf(), name, "application", "pdf"))
    t = r.tracker.data["big"]
    assert t["status"] == "acknowledged" and codes(tmp_path) == [] and "files" not in t
    assert len(held(tmp_path)) == 1 and len(notices) == 1 and "cost" not in notices[0][0]
    assert "big" not in orr.downloaded_places(tmp_path, _Places())


@pytest.mark.parametrize("text,name", [
    ("Good morning,\n\nPlease see attached." + SIG, "Scan_20261118_093344.pdf"),
    ("See attached\n\nMary Smith\nCity Clerk\nCity of Smallville\n(478) 555-1212", "Scan_001.pdf"),   # no sign-off
    ("Please see attached.\n\n*Thanks,*\n\n*Pam Nelms*", "doc01234.pdf"),
    ("Dear Requestor,\n\nPlease find attached. Let me know if you have any questions.", "MillenGA.pdf"),
    ("Here you go." + SIG, "Book1.pdf"),
    (SIG.lstrip(), "Book1.pdf"),                          # "Thank you," and a signature, nothing else
    ("Attached are the documents you requested." + SIG, "Book1.pdf"),
    ("Good morning,\n\nIn response to your Open Records Request, please see the following." + SIG, "O-2026-21_001.pdf"),
])
def test_a_generic_document_with_a_bare_note_or_naming_the_request_is_held(tmp_path, text, name):
    """Fourth Nov 19 review: a generic name with a cover note, or a note that names only our request, reads exactly
    the same in mail meant for someone else in our thread, so it is held with one notice (not the Code until the owner
    moves it). An ordinance number in the name ("O-2026-21_001.pdf", Perry) names the code."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    check(r, add(reply("big", text + quoted("big", "Bigtown")), pdf(), name, "application", "pdf"))
    t = r.tracker.data["big"]
    if name.startswith("O-2026"):
        assert t["status"] == "fulfilled" and codes(tmp_path) == [f"big__{name}"]
        saved_notice(notices, [f"big__{name}"], f'by its name ("{name}")')
        return
    assert t["status"] == "acknowledged" and codes(tmp_path) == [] and "files" not in t
    assert held(tmp_path) == [f"big__{name.replace(' ', '_')}"]
    assert [n[0] for n in notices] == ["Open records: Bigtown sent a document to look at"]


def test_scanned_pages_for_another_matter_are_not_the_code(tmp_path):
    """2: "pages" alone counted as the thing attached, so page photos for an address were saved as the Code."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    m = reply("big", "Attached are the scanned pages for 412 Elm St.\n\nJane")
    for i in range(4):
        add(m, jpg(), f"IMG_{i}.JPG", subtype="jpeg")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "acknowledged" and codes(tmp_path) == [] and len(held(tmp_path)) == 4
    assert len(notices) == 1 and "unrelated" in notices[0][0]
    assert not orr.code_attached("Attached are the scanned pages for 412 Elm St.")
    assert not orr.code_attached("Attached are the scanned pages.")                   # pages of what is not said
    assert not orr.code_attached("Attached are the code pages for parcel 12.")
    assert orr.code_attached("Our code is not online, so attached are the scanned pages.")


def test_page_scans_without_a_word_on_what_they_are_are_held(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Attached are the scanned pages." + SIG + quoted("big", "Bigtown"))
    for i in range(4):
        add(m, jpg(), f"IMG_{i}.JPG", subtype="jpeg")
    check(r, m)
    t = r.tracker.data["big"]
    assert codes(tmp_path) == [] and len(held(tmp_path)) == 4 and "images" in notices[0][0]
    assert orr.Requests.answered(t)                  # the clerk answered: no "I have not yet received a response"


@pytest.mark.parametrize("text,name,status,notice", [
    ("Good afternoon,\n\nPlease see the attached letter.\n\nThe cost for copies of the code is $35.00, payable by "
     "check.", "Letter.pdf", "cost_estimate", "mentions a cost"),
    ("Good afternoon,\n\nPlease see the attached letter.\n\nThank you", "ORR Letter.pdf", "acknowledged",
     "a document to look at"),
    ("Please see the attached letter. The total is $35.00, payable by check.", "Letter.pdf", "cost_estimate",
     "mentions a cost"),
])
def test_the_clerks_own_letter_is_an_answer_not_an_unrelated_message(tmp_path, text, name, status, notice):
    """3: "the attached letter" in the first lines read as another matter's letter: the cost went unseen, and with
    the reply flagged unrelated a follow-up would tell the clerk we had no response."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    check(r, add(reply("big", text + SIG + quoted("big", "Bigtown")), pdf(), name, "application", "pdf"))
    t = r.tracker.data["big"]
    assert t["status"] == status and codes(tmp_path) == [] and held(tmp_path) == [f"big__{name.replace(' ', '_')}"]
    assert len(notices) == 1 and notice in notices[0][0] and "note" not in t["replies"][-1]
    assert orr.Requests.answered(t)
    t["sent_at"] = "2026-10-01T12:00:00+00:00"
    assert r.follow_ups(smtp_factory=NoMail)["sent"] == 0


def test_a_letter_for_another_matter_with_a_fee_is_still_unrelated(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "acknowledged")
    m = reply("big", "Here's the letter for the Smith rezoning. The application fee is $30." + quoted("big", "Bigtown"))
    out = check(r, add(m, pdf(), "Smith.pdf", "application", "pdf"))
    unrelated_and_nothing_counted(tmp_path, r, notices, "big")
    assert "cost_notices" not in out


@pytest.mark.parametrize("text,files,code", [
    ("Good afternoon,\n\nIn response to your open records request, attached is the City of Bigtown Code of "
     "Ordinances.", ["Open Records Response.pdf"], ["Open_Records_Response.pdf"]),
    ("Attached is the Code of Ordinances.", ["Response Letter.pdf"], ["Response_Letter.pdf"]),
    ("Attached is the Code of Ordinances.", ["Invoice.pdf"], []),                   # a bill never is the code
    ("Attached is our code.", ["Receipt.pdf"], []),
    ("Attached are the Code of Ordinances and our response letter.", ["Scan.pdf", "Response.pdf"], ["Scan.pdf"]),
    ("Please see the attached response.", ["Response.pdf"], []),                    # the code not said to be in it
])
def test_a_response_named_document_is_the_code_when_the_reply_says_the_code_is_attached(tmp_path, text, files, code):
    """4: "Open Records Response.pdf" was held although the reply said the Code was attached and it was the only
    document: clerks send the letter and the code as one PDF."""
    r, _ = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", text + SIG + quoted("big", "Bigtown"))
    for n in files:
        add(m, pdf(), n, "application", "pdf")
    check(r, m)
    assert codes(tmp_path) == [f"big__{c}" for c in code]
    assert r.tracker.data["big"]["status"] == ("fulfilled" if code else "acknowledged")


@pytest.mark.parametrize("text", [
    "Attached is the Code of Ordinances. The normal $25.00 copy fee has been waived." + SIG,
    "Please see the attached code." + SIG + "Online bill payment is now available at www.smallvillega.gov\n",
    "Please see the attached code.\n\nMary Smith\nCity Clerk\nPay your water bill online at www.smallvillega.gov/pay",
])
def test_a_waived_fee_or_a_signatures_bill_pay_line_is_no_cost(tmp_path, text):
    """5: an amount next to "waived" still counted, and a signature's "Online bill payment" read as a charge."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    out = check(r, add(reply("big", text + quoted("big", "Bigtown")), pdf(), "Code.pdf", "application", "pdf"))
    assert r.tracker.data["big"]["status"] == "fulfilled" and "cost_notices" not in out
    assert "check what was saved" in saved_notice(notices, ["big__Code.pdf"])[0]


def test_what_is_a_cost_after_the_second_review():
    assert orr.mentions_cost("The normal $25.00 copy fee has been waived.") == []
    assert orr.mentions_cost("There is no charge for the code, but the $5 mailing fee applies.") == ["$5"]
    assert orr.mentions_cost("The $10 fee is waived for electronic copies, but paper copies cost $0.25 per page.") == \
        ["$0.25"]
    assert orr.mentions_cost("Pay your water bill online. The fee for the code is $15.") == ["$15"]
    assert orr.reply_costs("Once we receive payment we will send the code; you can pay online.") == \
        ["amount not stated"]
    assert orr.reply_costs("Please see attached.\nThe fee of $25 can be paid at www.x.gov/pay") == ["$25"]
    assert orr._unsigned("Please see attached.\nThe fee of $25 can be paid at www.x.gov/pay").endswith("/pay")


# ----------------------------------------------------------------------------- third Nov 19 review
class SentMail:
    """An SMTP stand-in that records what would be sent."""

    def __init__(self, sink):
        self.sink = sink

    def login(self, *a):
        pass

    def send_message(self, msg):
        self.sink.append(msg["Subject"])

    def quit(self):
        pass


def follow_ups_sent(r, jid="big"):
    """The follow-ups due now that the request was sent long ago (records-send.yml runs `records check` and then
    `records follow-up` in the same job); NoMail fails the test if one is sent."""
    r.tracker.data[jid]["sent_at"] = "2026-10-01T12:00:00+00:00"
    return r.follow_ups(smtp_factory=NoMail)["sent"]


@pytest.mark.parametrize("text", [
    "Will do.",
    "I will look into this and get back with you." + SIG,
    "Hello,\n\nCould you tell me what this is for? Are you with a law firm?\n\nJane",
    "Who is the Georgia Civic Records Project and what will this be used for?" + SIG,
    "Please give me a call at 478-555-0100 so we can discuss." + SIG,
    "Yes sir, I'll get it out to you tomorrow.\n\nSent from my iPhone",
    "Do you want a hard copy or electronic?" + SIG,
    "Please submit this through JustFOIA at https://bigtownga.justfoia.com/publicportal so it can be tracked." + SIG,
    "The City Clerk position is currently vacant. Please direct this to the City Manager, John Smith, at "
    "jsmith@bigtownga.gov." + SIG,
    "I'll check with the Mayor and let you know." + SIG,
    "Good morning. Is this something you need certified?" + SIG,
    "I have forwarded this to our city attorney for review." + SIG,
    "Mr. Requester,\n\nI'm working on this now. Our office is short staffed this week.\n\nJane",
    "Good afternoon,\n\nI can help with that. Please give me about a week to pull everything together." + SIG,
    "On it!\n\nSent from my iPhone",
    "Hi, which email address should I send this to? This one?" + SIG,
    "Adding our City Manager, Tom Brown, who handles these." + SIG,
    "Buenos dias. Le respondo pronto." + SIG,
])
def test_a_persons_reply_that_names_nothing_else_is_an_answer(tmp_path, text):
    """1 (H1-H18): with no request words a clerk's reply read as "not about this request": the request stayed
    'sent', the owner was told it was unrelated, and the follow-up told the clerk no response had come."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    check(r, reply("big", text + quoted("big", "Bigtown")))
    t = r.tracker.data["big"]
    assert t["status"] == "acknowledged" and "note" not in t["replies"][-1] and notices == []
    assert orr.Requests.answered(t) and follow_ups_sent(r) == 0


@pytest.mark.parametrize("text", ["This has been forwarded to the appropriate department.", "Please allow 3 business "
                                  "days.", "Ok", "Noted.", "Please see my response below in red.",
                                  "Good morning, I will look for this today.",
                                  # a staff word beside the code is not a staff paper
                                  "The council will adopt the updated code at its work session on Monday; I will "
                                  "send it to you after."])
def test_short_replies_are_about_the_request(text):
    assert orr.about_request(orr.own_text(text + quoted("big", "Bigtown")), "", [], 0)


@pytest.mark.parametrize("text,name", [
    ("Attached is a copy. You can also pick up a printed copy at City Hall, 101 N Main Street.", "Document.pdf"),
    ("The full code is in the attached packet. Let me know if you need anything else.", "Bigtown packet.pdf"),
    ("Attached is what we have. For parcel data please see qPublic.", "Document.pdf"),
    ("See below from our attorney.\n\nJane\n\nFrom: Bill Jones <bjones@lawfirm.com>\nSent: Wednesday, November 11, "
     "2026 3:12 PM\nTo: Jane Doe <jdoe@bigtownga.gov>\nSubject: RE: " + orr.SUBJECT.format(jid="big") +
     "\n\nJane, the code is public. Send him the Municode link.", None),
])
def test_a_real_reply_with_an_aside_is_an_answer_and_its_file_in_doubt_is_held(tmp_path, text, name):
    """1 (R1-R4): the office's own address, "parcel data" or "the attached packet" in the first lines read as
    another matter, and so did a text with no request words: the reply was unrelated and a follow-up went out."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", text + SIG + quoted("big", "Bigtown"))
    if name:
        add(m, pdf(), name, "application", "pdf")
    check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "acknowledged" and "note" not in t["replies"][-1] and codes(tmp_path) == []
    assert held(tmp_path) == ([f"big__{name.replace(' ', '_')}"] if name else [])
    assert [n[0] for n in notices] == (["Open records: Bigtown sent a document to look at"] if name else [])
    assert follow_ups_sent(r) == 0


def test_a_message_read_as_unrelated_gets_no_follow_up_but_stays_late(tmp_path, monkeypatch):
    """1, the backstop: a message read as meant for something else may be a real answer misread, so no follow-up
    tells its sender that no response came; the request is still listed late, and the owner was told of it."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GACRAWL_ISSUES_REPO", raising=False)
    r, notices = requests_for(tmp_path, "wadley", "Wadley", "sent")
    check(r, reply("wadley", WADLEY_LETTER, frm="Dwayne Flowers <dflowers@wadleyga.gov>"))
    t = r.tracker.data["wadley"]
    assert t["status"] == "sent" and t["replies"][-1]["note"] == orr.NOT_ABOUT and "unrelated" in notices[0][0]
    assert not orr.Requests.answered(t) and orr.Requests.heard_from(t)
    assert follow_ups_sent(r, "wadley") == 0 and r.deadlines()["late"] == 1


def test_an_automatic_reply_alone_still_gets_the_follow_up(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "I am out of the office until Monday, November 30.",
              subject="Out of Office: Re: " + orr.SUBJECT.format(jid="big"))
    check(r, m)
    t = r.tracker.data["big"]
    assert t["status"] == "sent" and t["replies"][-1]["note"] == "automatic reply" and notices == []
    t["sent_at"] = "2026-10-01T12:00:00+00:00"
    sent = []
    assert r.follow_ups(smtp_factory=lambda: SentMail(sent))["sent"] == 1 and len(sent) == 1
    # an automatic reply that names nothing is no answer either (a person's would be)
    assert not orr.about_request("This mailbox is checked weekdays.", "", [], 0, auto=True)
    assert orr.about_request("This mailbox is checked weekdays.", "", [], 0)


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,name", [
    ("Hi Mrs. Johnson,\n\nAttached are the records you requested. Let me know if you need anything else.",
     "Scan_001.pdf"),
    ("Mr. Patel,\n\nHere is the information you requested on your water account. The balance shown is current as of "
     "today.", "Doc11182026.pdf"),
    ("Tom,\n\nAs requested, here is the updated budget spreadsheet in PDF.", "FY27 v3.pdf"),
    ("Mr. Smith,\n\nI've attached the copies you asked for. Thanks for coming by today.", "Copies.pdf"),
    ("As requested, here is the updated budget spreadsheet in PDF.", "FY27 v3.pdf"),
    ("Here is the information you requested on your water account.", "Doc11182026.pdf"),
    ("Attached is the FY2025 audit you requested.", "FY2025.pdf"),
    ("Per your request, attached is the adopted FY2027 budget.", "Adopted FY2027.pdf"),
    ("Mrs. Lee,\n\nHere is the chicken ordinance you asked about.", "Sec 10-4 Fowl.pdf"),
])
def test_requested_or_a_greeting_to_someone_else_does_not_make_a_file_the_code(tmp_path, status, text, name):
    """2: "requested", "you asked for" or "per your request" made a file with a name that says nothing the Code,
    even when the reply named it as something else (a budget, an audit, a water account) or greeted another
    resident: saved to imports/ordinances, fulfilled, no notice, and the place counted as having its code."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", status)
    check(r, add(reply("big", text + SIG + quoted("big", "Bigtown")), pdf(), name, "application", "pdf"))
    t = r.tracker.data["big"]
    assert t["status"] != "fulfilled" and codes(tmp_path) == [] and "files" not in t
    assert held(tmp_path) == [f"big__{name.replace(' ', '_')}"] and len(notices) == 1
    assert "big" not in orr.downloaded_places(tmp_path, _Places())


def test_a_note_greeting_someone_else_holds_even_a_code_and_page_scans(tmp_path):
    r, notices = requests_for(tmp_path, "big", "Bigtown", "sent")
    check(r, add(reply("big", "Hi Tom,\n\nHere is the Code of Ordinances you asked about." + SIG), pdf(),
                 "Code of Ordinances.pdf", "application", "pdf"))
    assert codes(tmp_path) == [] and held(tmp_path) == ["big__Code_of_Ordinances.pdf"]
    assert "a document to look at" in notices[0][0] and "greeting to Tom" in notices[0][1]
    r2, _ = requests_for(tmp_path / "b", "big", "Bigtown", "sent")
    m = reply("big", "Mayor,\n\nAttached are the scanned pages of our Code of Ordinances." + SIG)
    for i in range(4):
        add(m, jpg(), f"IMG_{i}.JPG", subtype="jpeg")
    check(r2, m)
    assert codes(tmp_path / "b") == [] and len(held(tmp_path / "b")) == 4
    assert r2.tracker.data["big"]["status"] != "fulfilled"


def test_who_a_greeting_names():
    ours = ("requests@project.example", "Pat Requester", "Electo")
    for text in ["Good morning, hope you're well!", "As promised,", "Mr. Electo,", "Dear Open Records Requester,",
                 "Good Morning:", "Yes sir,", "Good morning and Happy Friday,", "Hello again,", "To Pat Requester:",
                 "Dear Sir or Madam,", "To Whom It May Concern:", "Good afternoon Ms. Requester,", ""]:
        assert orr._addressee(text + "\n\nSee attached.", ours) == "", text
    for text, who in [("Hi Tom,", "Tom"), ("hi tom,", "tom"), ("Mayor and Council,", "Mayor and Council"),
                      ("Mrs. Johnson,", "Mrs. Johnson"), ("Good Morning Mr. Mayor,", "Mr. Mayor")]:
        assert orr._addressee(text + "\n\nSee attached.", ours) == who
    assert orr._addressee("Hi Mrs. Johnson, attached are the records.", ours) == "Mrs. Johnson"
    # a greeting to a group is staff mail
    for text in ["Hi all,", "Good morning everyone,", "Team,", "Hello all -", "All,"]:
        assert orr._group(text + "\n\nSee attached.") != "", text
    for text in ["Thank you all for your patience,", "Good morning,", "Hi Mr. Requester,"]:
        assert orr._group(text + "\n\nSee attached.") == "", text


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,name", [
    ("Per our conversation, attached is the draft ordinance amending Chapter 14 (alcohol) for Council's review on "
     "Monday. Please send me any changes by Friday.", "Draft Alcohol Ordinance Amendment.docx"),
    ("Attached is the proposed ordinance for the Commission's consideration.", "Ordinance 2026-14.pdf"),
    ("Please review and return. The amendment to Chapter 6 goes to the work session.", "Ordinance Amendment Ch 6.pdf"),
])
def test_a_draft_ordinance_for_council_review_is_not_the_code(tmp_path, status, text, name):
    """3: a file named "Ordinance" was the Code at once, though it was a draft sent for Council's review: fulfilled,
    the place counted as having its code, and the build would read an unadopted draft."""
    r, notices = requests_for(tmp_path, "big", "Bigtown", status)
    check(r, add(reply("big", text + SIG + quoted("big", "Bigtown")), pdf(), name, "application", "pdf"))
    t = r.tracker.data["big"]
    assert t["status"] == status and codes(tmp_path) == [] and held(tmp_path) == [f"big__{name.replace(' ', '_')}"]
    assert len(notices) == 1 and "unrelated" in notices[0][0]


def test_a_draft_beside_the_code_is_held_and_the_code_counts(tmp_path):
    r, _ = requests_for(tmp_path, "big", "Bigtown", "sent")
    m = reply("big", "Attached is our Code of Ordinances, and the proposed amendment to Chapter 14 for your "
                     "information." + SIG + quoted("big", "Bigtown"))
    add(m, pdf(), "Code of Ordinances.pdf", "application", "pdf")
    add(m, pdf(), "Proposed Amendment Ch 14.pdf", "application", "pdf")
    add(m, pdf(), "Draft Ordinance 2026-15.pdf", "application", "pdf")
    check(r, m)
    assert r.tracker.data["big"]["status"] == "fulfilled" and codes(tmp_path) == ["big__Code_of_Ordinances.pdf"]
    assert held(tmp_path) == ["big__Draft_Ordinance_2026-15.pdf", "big__Proposed_Amendment_Ch_14.pdf"]
    # naming our request is no evidence that a generic file is the Code (fourth Nov 19 review): held
    own = "Here is the information for your request, case no. 2026-114." + SIG
    assert not orr.doc_is_code("ORR 2026-114.pdf", own) and not orr.doc_is_code("Scan.pdf", "Per your request, "
                                                                                "attached is the case file." + SIG)


# ----------------------------------------------------------------------------- fourth Nov 19 review
# A file is saved to imports/ordinances as the Code only on positive evidence that it is: its name names the code
# (code, ordinances, charter, an ordinance number), or the reply says the code, the charter or the ordinances are what
# it sends; and nothing in the reply says it is for another matter. Everything else is held with one notice.
MSIG = ("\n\nThank you,\n\nJane Doe, CMC\nCity Clerk\nCity of Millen\n101 N Main Street | Millen, GA 30442\n"
        "P: (478) 555-0100\nwww.millenga.gov\n")


def _file(name):
    if name.lower().endswith((".jpg", ".jpeg")):
        return jpg(), name, "image", "jpeg"
    if name.lower().endswith(".docx"):
        return b"PK\x03\x04" + b"0" * 2000, name, "application", \
            "vnd.openxmlformats-officedocument.wordprocessingml.document"
    return pdf(), name, "application", "pdf"


def _millen(tmp_path, status, text, names, subject=None, sig=True):
    r, notices = requests_for(tmp_path, "millen", "Millen", status)
    m = reply("millen", text + (MSIG if sig else "") + quoted("millen", "Millen"), frm="Jane Doe <jdoe@millenga.gov>",
              subject=subject)
    for n in names:
        add(m, *_file(n))
    check(r, m)
    return r, notices


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,names", [
    # a generic name with a cover note, or naming only our request: reads the same in mail for someone else
    ("See attached.", ["Scan_001.pdf"]),
    ("Here you go", ["Document.pdf"]),
    ("", ["IMG_2034.pdf"]),
    ("Please see attached.", ["Millen.pdf"]),
    ("Here you go", ["Johnson.pdf"]),
    ("Your open records request has been fulfilled. Please see attached.", ["Scan_001.pdf"]),
    ("Attached are the records you requested.", ["Scan_001.pdf"]),
    ("Requested records attached.", ["Millen.pdf"]),
    ("Per your open records request, please find attached the requested documents.", ["Scan_001.pdf"]),
    # a name with a word that is not the code (a title, a section, a chapter, an article, legislation, a resolution)
    ("Please see attached Title VI Plan for your files.", ["Title VI Plan 2026.pdf"]),
    ("Here is the Section 8 inspection schedule for next week.", ["Section 8 Inspections Nov 2026.pdf"]),
    ("Attached is Chapter 3 of the comprehensive plan update.", ["Comp Plan Chapter 3.pdf"]),
    ("Attached is the article from the Herald about the new park.", ["Herald Article Nov 2026.pdf"]),
    ("FYI - attached is our Title VI Program for the GDOT review.", ["Title VI Program.pdf"]),
    ("Attached is the section of the employee handbook on holiday pay.", ["Handbook Section 7.pdf"]),
    ("Here is the SPLOST referendum legislation from the county.", ["SPLOST Legislation 2026.pdf"]),
    ("Attached is the signed resolution for the LMIG grant application.", ["Resolution 2026-14 LMIG.pdf"]),
    ("Attached is the budget amendment resolution the council adopted last night.",
     ["Resolution 2026-22 Budget Amendment.pdf"]),
    ("Hi all,\n\nAttached is the resolution for Monday.", ["Resolution.pdf"]),
    ("As requested, attached is Resolution 2026-09 adopting the hazard mitigation plan.", ["Res 2026-09.pdf"]),
    # codes that are not the Code
    ("Attached is the code sheet for the new alarm system at the water plant.", ["Alarm Codes.pdf"]),
    ("Good morning,\n\nHere are the radio codes for the new system.", ["Radio Codes.pdf"]),
    ("Attached is the CodeRED sign-up information for residents.", ["CodeRED Signup.pdf"]),
    ("Attached is the municipal court fine schedule by code section.", ["Fine Schedule by Code Section.pdf"]),
    # named as the code, but the reply says it is something else
    ("Attached is the Millen Main Street charter for the board.", ["Main Street Charter.pdf"]),
    ("Attached is the GMA model ordinance on short-term rentals you might find useful.",
     ["GMA Model STR Ordinance.pdf"]),
    ("Here is Sylvania's alcohol ordinance that we talked about.", ["Sylvania Alcohol Ordinance.pdf"]),
    ("Attached is the sample ordinance from our attorney.", ["Sample Golf Cart Ordinance.docx"]),
    ("Attached are the ordinances for first reading at Tuesday's council meeting.",
     ["Ordinances for 1st Reading 12-1-26.pdf"]),
    ("Here are the ordinances the Council will consider Monday night.", ["Ordinances 11-23.pdf"]),
    ("Per our conversation, see attached.", ["Ordinance 2026-14.pdf"]),
    # our request's own paperwork, said in the passive ("X is attached")
    ("Our open records request form is attached.", ["Scan_001.pdf"]),
    ("Our open records request form is attached. Please fill it out and return it to City Hall.", ["ORR.pdf"]),
    ("Our open records policy is attached.", ["Millen.pdf"]),
    ("A receipt for your open records request is attached.", ["Document.pdf"]),
    ("Good morning,\n\nA notice extending the time to respond to your open records request is enclosed.",
     ["Document.pdf"]),
    ("Your open records request acknowledgment is attached.", ["Scan_001.pdf"]),
    ("Your open records request has been reviewed; our response is attached.", ["Scan_001.pdf"]),
    ("Please see attached open records request.", ["ORR 2026-114.pdf"]),
    # pages that are photos of something else
    ("Attached are photos of the code book pages that got wet so we can order replacements.",
     ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg"]),
])
def test_a_file_without_positive_evidence_that_it_is_the_code_is_held(tmp_path, status, text, names):
    """1-3: each was saved to imports/ordinances as the Code (status fulfilled, the place counted as having its code,
    no notice), on its name alone, on a cover note's words, or on a passive "X is attached" nobody read."""
    r, notices = _millen(tmp_path, status, text, names)
    t = r.tracker.data["millen"]
    assert t["status"] != "fulfilled" and codes(tmp_path) == [] and "files" not in t
    assert held(tmp_path) == sorted(f"millen__{n.replace(' ', '_')}" for n in names)
    assert len(notices) == 1 and all(f"millen__{n.replace(' ', '_')}" in notices[0][1] for n in names)
    assert "imports/ordinances/" in notices[0][1]                       # how to accept them


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,names,code,subject", [
    ("Please see the attached Code of Ordinances.", ["Millen Code of Ordinances.pdf"], None, None),
    ("Attached is our code.", ["Scan_20261118.pdf"], None, None),
    ("The Code of Ordinances is attached.", ["Document.pdf"], None, None),
    ("Here you go.", ["Millen GA Code.pdf"], None, None),
    ("", ["Code of Ordinances.pdf"], None, None),
    ("Attached are the ordinances adopted since our last supplement.", ["Ord 2026-01.pdf", "Ord 2026-02.pdf"], None,
     None),
    ("Please see attached.", ["Charter.pdf", "Code.pdf"], None, None),
    ("Dear Requestor,\n\nAttached is the code of ordinances you requested.", ["COO.pdf"], None, None),
    ("Attached please find our Code of Ordinances (3 parts due to size).", ["Part 1.pdf", "Part 2.pdf", "Part 3.pdf"],
     None, None),
    ("We are not codified; attached are all ordinances on file.", ["Ordinance Book.pdf"], None, None),
    ("Here is the Millen Municipal Code.", ["MillenMunicipalCode.pdf"], None, None),
    ("Please see the attached response letter; the Code of Ordinances is included.", ["ORR Response Letter.pdf"], None,
     None),
    ("Attached is the code you requested. There is no charge.", ["Code.docx"], None, None),
    ("Attached are the Code of Ordinances and the Zoning Ordinance.", ["Code of Ordinances.pdf",
                                                                       "Zoning Ordinance.pdf"], None, None),
    ("Dear Requestor,\n\nPlease find the requested Code of Ordinances attached.", ["Scan_0042.pdf"], None, None),
    ("Here are the uncodified ordinances adopted since the last supplement (Supplement 14).", ["2026-03.pdf",
                                                                                               "2026-05.pdf"], None,
     None),
    ("The code is too large to email in one piece, so here is part 1 of 4.", ["Part 1.pdf"], None, None),
    ("Attached is the Code of Ordinances you requested as well as the cover letter.", ["Code of Ordinances.pdf",
                                                                                      "Cover Letter.pdf"],
     ["Code of Ordinances.pdf"], None),
    ("Good morning,\n\nIn response to your Open Records Request, please see the following:\n\n1. The Code is online at "
     "Municode.\n\n2. Any ordinances adopted since the most recent codification supplement that are not yet included "
     "in it.   Please see attached.", ["O-2026-21_001.pdf", "O-2026-22_001.pdf"], None, None),
    ("Attached is a PDF of our full code from Municode.", ["PTIICOOR.pdf"], None, None),
    ("Hi,\n\nHere is our code book.", ["Millen.pdf"], None, None),
    ("Good afternoon,\n\nThe Code of Ordinances, as requested.", ["Document1.pdf"], None, None),
    ("I have attached the city's ordinances. We do not have them codified.", ["Millen Ordinances.pdf"], None, None),
    ("See attached.", ["Scan_001.pdf"], None, "RE: Code of Ordinances - Millen"),       # the clerk's own subject
    ("I have also attached two ordinances that have not been codified.",
     ["Private_Sewer_Lateral_Ordinance_Amendment_7-21-2026.pdf"], None, None),           # Statesboro
    ("Below is the link for Code of Ordinances, and the Charter is attached.", ["Charter_2026.pdf"], None, None),
])
def test_a_file_named_or_said_to_be_the_code_is_still_the_code(tmp_path, status, text, names, code, subject):
    r, notices = _millen(tmp_path, status, text, names, subject)
    code = code or names
    assert r.tracker.data["millen"]["status"] == "fulfilled"
    assert codes(tmp_path) == sorted(f"millen__{n.replace(' ', '_')}" for n in code)
    # one notice either way: the files saved as the Code, and a held cover letter (fifth Nov 19 review)
    title, body = saved_notice(notices, [f"millen__{n.replace(' ', '_')}" for n in code])
    assert ("look at" in title) == (len(code) < len(names))
    assert ("files saved as the Code" in title) == (len(code) < len(names))
    assert f"status back to **{status}**" in body


def test_the_enabling_legislation_is_a_countys_charter(tmp_path):
    """Haralson County (Nov 9, real): "Please see the enabling legislation attached" with the Municode link."""
    r, notices = requests_for(tmp_path, "co-haralson", "Haralson County", "sent")
    m = reply("co-haralson", "Good Morning,\n\nPlease see the enabling legislation attached, and the link below is for "
              "the Haralson County ordinances you requested.\n\nhttps://library.municode.com/ga/haralson_county/codes/"
              "code_of_ordinances?nodeId=14595\n\nThank you,\nJulia" + quoted("co-haralson", "Haralson County"))
    add(m, pdf(), "Haralson_County_Enabling_Legislation_936145.1_.pdf", "application", "pdf")
    check(r, m)
    assert r.tracker.data["co-haralson"]["status"] == "fulfilled"
    assert codes(tmp_path) == ["co-haralson__Haralson_County_Enabling_Legislation_936145.1_.pdf"]
    saved_notice(notices, codes(tmp_path), "by its name")


def test_resolutions_are_held_for_the_owner(tmp_path):
    """Tattnall County (Nov 9, real): resolutions are not the Code, though one repeals an ordinance: held, one notice
    per reply, and the request stays partial on the link to the code."""
    r, notices = requests_for(tmp_path, "co-tattnall", "Tattnall County", "sent")
    m = reply("co-tattnall", "As the Open Records Officer I acknowledge receipt of your request today, November 9, "
              "2026.\n\nCopies of the code are accessible via our official website at tattnallcountyga.com by "
              "navigating to the Online/Codes and Ordinances section.\n\nPlease find attached Resolution 2026-11 to "
              "repeal and rescind Ordinance 2026-1.\n\nPlease confirm receipt.\nRegards," +
              quoted("co-tattnall", "Tattnall County"))
    add(m, pdf(), "Resolution_2026-11_Resolution_to_Repeal_Rescind.pdf", "application", "pdf")
    m2 = reply("co-tattnall", "These resolutions are also currently in effect, with a proposed 90-day extension being "
               "voted on in a Special Called Meeting on Monday.\n\nRegards," + quoted("co-tattnall", "Tattnall County"))
    add(m2, pdf(), "Resolution_2026-12_Solar_Farm_Moratorium.pdf", "application", "pdf")
    check(r, m, m2)
    assert r.tracker.data["co-tattnall"]["status"] == "partial" and codes(tmp_path) == [] and len(held(tmp_path)) == 2
    assert [n[0] for n in notices] == ["Open records: Tattnall County sent a document to look at"] * 2


def test_what_a_reply_says_it_sends():
    def said(t):
        return orr._said(t, "Millen", ("requests@project.example", "Georgia Civic Records Project"))

    for t in ["Attached is our city code along with the ordinances passed since it was last updated in 2019.",
              "The ordinances that have not been codified are attached.", "Our code, which was last updated in "
              "2019, is attached.", "The Code of Ordinances and the Charter are attached.", "You'll find our code "
              "attached.", "Attached to this email is the City of Millen Code of Ordinances.", "Here's our code book "
              "scanned", "Code of Ordinances - Millen", "Attached is our code, sorry for the delay."]:
        assert said(t).code and not said(t).other, t
    for t in ["Please see attached Title VI Plan for your files.", "Here is Sylvania's alcohol ordinance.",
              "Our open records request form is attached.", "A receipt for your open records request is attached.",
              "Attached is the ordinance on golf carts.", "Here are the ordinances the Council will consider Monday.",
              "Attached is the code sheet for the new alarm system."]:
        assert said(t).other and not said(t).code, t
    for t in ["Here is the link to our Code of Ordinances.", "I have not attached the code because it is too large.",
              "The code will be attached to my next email.", "I could not find the code.", "See attached.",
              "Attached are the records you requested.", "I trust you will find the records provided responsive to "
              "your request."]:
        assert not said(t).code and not said(t).other, t
    assert said("Your open records request has been reviewed; our response is attached.").paper
    assert orr._names_code("O-2026-21_001.pdf") and orr._names_code("Ord 2024-03.pdf")
    assert orr._names_code("Haralson_County_Enabling_Legislation.pdf") and orr._names_code("Charter_2026.pdf")
    for n in ["Title VI Plan 2026.pdf", "Resolution 2026-14.pdf", "Comp Plan Chapter 3.pdf", "Alarm Codes.pdf",
              "CodeRED Signup.pdf", "Fine Schedule by Code Section.pdf", "GMA Model STR Ordinance.pdf",
              "Ordinances for 1st Reading.pdf", "788_Establish_Compliance_with_Senate_Bill_447.pdf"]:
        assert not orr._names_code(n), n


# ----------------------------------------------------------------------------- fifth Nov 19 review
# Each of these was saved to imports/ordinances as the Code (status fulfilled, no notice): the sign that it is not
# was in another sentence, in a relative clause, in a word allowed beside any code ("county", "Georgia"), in a name
# rule's false friend (Charter Communications, Municode, P.O.), or the reply said only "the code". Now they are held
# with one notice, and every file that is saved as the Code is told to the owner (the rules can be wrong).
def _at(tmp_path, jid, name, status, text, names):
    r, notices = requests_for(tmp_path, jid, name, status)
    sig = f"\n\nThank you,\n\nJane Doe\nClerk\n{name}\n(478) 555-0100\n"
    m = reply(jid, text + sig + quoted(jid, name), frm=f"Jane Doe <jdoe@{jid}ga.gov>")
    for n in names:
        add(m, *_file(n))
    check(r, m)
    return r, notices


def _held_with_one_notice(tmp_path, r, notices, jid, names):
    t = r.tracker.data[jid]
    assert t["status"] != "fulfilled" and codes(tmp_path) == [] and "files" not in t, (t["status"], codes(tmp_path))
    assert held(tmp_path) == sorted(f"{jid}__{n.replace(' ', '_')}" for n in names)
    assert len(notices) == 1 and "Saved as the Code" not in notices[0][1]


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,names", [
    ("Here is the new ordinance. Can you put it on the December agenda?", ["Sign Ordinance.docx"]),
    ("Attached is the ordinance. Please review and let me know if you have any changes before Monday's meeting.",
     ["Golf Cart Ordinance v3.docx"]),
    ("Attached is the revised ordinance. It goes to council for first reading on December 1.",
     ["Short Term Rental Ordinance.docx"]),
    ("Here is the ordinance as amended by the attorney. Let me know what you think.", ["Ordinance 2026-15 rev.docx"]),
    ("Here is the ordinance. Let me know what you think.", ["Backyard Chickens Ordinance.docx"]),
    ("Attached is the ordinance. This is still a draft.", ["Ordinance.docx"]),
    ("Attached is the ordinance. The mayor still needs to sign it before it takes effect.", ["Ordinance 2026-20.pdf"]),
    ("Here is the ordinance. Please look it over before Monday.", ["Ordinance.docx"]),
    ("Attached is the ordinance. Council will vote on it Tuesday.", ["Ordinance 2026-30.pdf"]),
    ("Attached is the code update. This is a working copy.", ["Scan_001.pdf"]),
    ("Here is the ordinance with changes from legal. Thoughts?", ["Ordinance 2026-15.docx"]),
    ("Attached is our code. Take a look and send me your edits.", ["Scan_001.pdf"]),
])
def test_a_staff_or_draft_sign_anywhere_in_the_reply_holds_its_files(tmp_path, status, text, names):
    """1: the sign sat in another sentence than the one that sends the file (_PENDING is read over the whole reply)."""
    r, notices = _millen(tmp_path, status, text, names)
    _held_with_one_notice(tmp_path, r, notices, "millen", names)


def test_a_staff_sign_leaves_only_a_plainly_named_code():
    own = "Please review the attached and let me know if you have any changes." + MSIG
    assert orr.doc_is_code("Code of Ordinances.pdf", own, place="Millen") == "name"
    assert orr.doc_is_code("Millen GA Code.pdf", own, place="Millen") == "name"
    for name in ["Ordinance 2026-14.pdf", "Golf Cart Ordinance.docx", "Scan_001.pdf", "Zoning Ordinances.pdf"]:
        assert orr.doc_is_code(name, own, place="Millen") == "", name
    assert orr.doc_is_code("Scan_001.pdf", "Attached is our code." + MSIG, place="Millen") == "words"
    assert orr.doc_is_code("Scan_001.pdf", "Attached is our code. It is still a draft." + MSIG, place="Millen") == ""
    # a word in the signature is no sign ("Agendas are posted at ...")
    assert orr._pending("Attached is our code.\n\nThank you,\n\nJane Doe\nCity Clerk\nAgenda: www.millenga.gov") == ""


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,names", [
    ("Here are the ordinances that failed.", ["Failed Ordinances 2026.pdf"]),
    ("Attached is the ordinance the council voted down last night.", ["Ordinance 2026-14.pdf"]),
    ("Attached is the ordinance that was tabled at the last meeting.", ["Ordinance 2026-12.pdf"]),
    ("Attached is the 1998 code, which was replaced in 2015.", ["1998 Code.pdf"]),
    ("Attached is the ordinance that did not pass.", ["Ordinance 2026-14.pdf"]),
    ("Attached is the ordinance the council rejected in October.", ["Ordinance 2026-15.pdf"]),
    ("Here is the old code.", ["Scan_001.pdf"]),
    ("See attached.", ["Repealed Ordinances 2019.pdf"]),
    ("Please see attached.", ["Superseded Code 1998.pdf"]),
    ("", ["Former Code of Ordinances.pdf"]),
    # the sign in the next sentence (fifth review's verifier): every file is held, a plainly named one too
    ("Attached is the ordinance. It was voted down last month.", ["Ordinance 2026-14.pdf"]),
    ("Attached is the ordinance. It failed on second reading.", ["Ordinance 2026-14.pdf"]),
    ("Here is the code. It was replaced in 2015.", ["Code of Ordinances.pdf"]),
    ("Attached are the ordinances. Both were tabled.", ["Ordinances 2026.pdf"]),
    ("Attached is the annexation ordinance. The council denied it.", ["Ordinance 2026-09.pdf"]),
    ("Attached is the ordinance. The council voted it down last night.", ["Golf Cart Ordinance.docx"]),
    ("See attached.", ["Withdrawn Ordinance 2026-14.pdf"]),
    ("", ["Withdrawn Code of Ordinances.pdf"]),
])
def test_an_ordinance_never_adopted_or_a_code_replaced_is_not_the_code(tmp_path, status, text, names):
    """2: failed, voted down, tabled, replaced, old, previous or former, beside the code noun, in a relative clause
    after it, or in the file's name."""
    r, notices = _millen(tmp_path, status, text, names)
    _held_with_one_notice(tmp_path, r, notices, "millen", names)


@pytest.mark.parametrize("jid,name,text,names", [
    ("millen", "Millen", "Attached is the Georgia code.", ["Georgia Code 36-35-3.pdf"]),
    ("millen", "Millen", "Attached is the Georgia code.", ["Scan_001.pdf"]),
    ("millen", "Millen", "Attached is the GA code.", ["Scan_001.pdf"]),
    ("millen", "Millen", "Attached is the county code.", ["Jenkins County Code.pdf"]),
    ("millen", "Millen", "Here is the County's code.", ["Scan_001.pdf"]),
    ("millen", "Millen", "See attached.", ["State Code 2024.pdf"]),
    ("millen", "Millen", "See attached.", ["OCGA_36-35-3.pdf"]),
    ("millen", "Millen", "Here you go.", ["Jenkins Co Code.pdf"]),
    ("millen", "Millen", "See attached.", ["Sylvania Ordinances.pdf"]),
    ("millen", "Millen", "See attached.", ["Code of Ordinances - Sylvania.pdf"]),
    ("millen", "Millen", "", ["City of Statesboro Code.pdf"]),
    ("co-jenkins", "Jenkins County", "Attached is the city code.", ["Millen City Code.pdf"]),
    ("co-jenkins", "Jenkins County", "Here is the city code.", ["Scan_001.pdf"]),
    ("co-jenkins", "Jenkins County", "", ["Screven County Code.pdf"]),
])
def test_another_places_code_or_the_states_is_not_the_code(tmp_path, jid, name, text, names):
    """3: "city", "county", "Georgia" beside a code noun count only for the place's own kind; a name naming another
    place blocks it."""
    r, notices = _at(tmp_path, jid, name, "sent", text, names)
    _held_with_one_notice(tmp_path, r, notices, jid, names)


@pytest.mark.parametrize("jid,name,text,names", [
    ("millen", "Millen", "Attached is the city code.", ["Scan_001.pdf"]),
    ("millen", "Millen", "Attached is the City of Millen, Georgia Code of Ordinances.", ["Document.pdf"]),
    ("millen", "Millen", "Here you go.", ["Millen GA Code.pdf"]),
    ("co-jenkins", "Jenkins County", "Attached is the county code.", ["Scan_001.pdf"]),
    ("co-jenkins", "Jenkins County", "Here you go.", ["Jenkins County Code.pdf"]),
    ("co-jenkins", "Jenkins County", "", ["Jenkins Co Code.pdf"]),
    ("athens-clarke", "Athens", "Attached is the county code.", ["Scan_001.pdf"]),       # a consolidated government
    ("athens-clarke", "Athens", "", ["Athens-Clarke County Code of Ordinances.pdf"]),
])
def test_the_places_own_kind_of_code_is_still_the_code(tmp_path, jid, name, text, names):
    r, notices = _at(tmp_path, jid, name, "sent", text, names)
    assert r.tracker.data[jid]["status"] == "fulfilled"
    saved_notice(notices, [f"{jid}__{n.replace(' ', '_')}" for n in names])


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,names", [
    ("Please sign where highlighted and I will get it to the mayor.",
     ["Charter Communications Franchise Agreement.pdf"]),
    ("Thanks for coming to the luncheon. Attached is the charter.", ["Rotary Club Charter.pdf"]),
    ("FYI, Municode sent this over today.", ["Municode Recodification Proposal.pdf"]),
    ("See attached.", ["P.O. 2026-114.pdf"]),
    ("See attached.", ["W.O. 2026-5.pdf"]),
    ("Attached.", ["Boy Scout Troop 12 Charter.pdf"]),
    ("See attached.", ["Municode Invoice 2026.pdf"]),
    ("Please sign and return.", ["Charter Spectrum Renewal.pdf"]),
])
def test_a_charter_municode_or_purchase_order_name_is_not_the_code(tmp_path, status, text, names):
    """4: a business's or a club's charter, a Municode paper with no code word, a P.O. number."""
    r, notices = _millen(tmp_path, status, text, names)
    _held_with_one_notice(tmp_path, r, notices, "millen", names)


def test_what_names_the_code_after_the_fifth_review():
    assert orr._names_code_plainly("Code of Ordinances.pdf", "Millen") and orr._names_code_plainly("City Charter.pdf")
    assert orr._names_code_plainly("Charter_2026.pdf", "Fort Valley")
    assert orr._names_code("O-2026-21_001.pdf", "Perry") and not orr._names_code("P.O. 2026-114.pdf", "Millen")
    assert orr._names_code("Haralson_County_Enabling_Legislation_936145.1_.pdf", "Haralson County")
    assert orr._names_code("Private_Sewer_Lateral_Ordinance_Amendment_7-21-2026.pdf", "Statesboro")
    for n in ["Municode Recodification Proposal.pdf", "Sign Ordinances.pdf", "Ordinance 2026-14.pdf"]:
        assert not orr._names_code_plainly(n, "Millen"), n
    assert orr._names_other_place("Union City Code.pdf", "Millen") and not orr._names_other_place("Union City Code.pdf",
                                                                                                   "Union City")


@pytest.mark.parametrize("status", ["sent", "acknowledged"])
@pytest.mark.parametrize("text,names", [
    ("The new keypad at the water plant is installed. Attached is the code.", ["Scan_001.pdf"]),
    ("Here are the codes. Please don't share them.", ["Scan_001.pdf"]),
    ("The gate is fixed. Attached is the complete code.", ["Scan_001.pdf"]),
    ("Here is the code book I mentioned on the phone.", ["Scan_001.pdf"]),
    ("Attached is the ordinance I mentioned when you called yesterday.", ["Ordinance 2026-09.pdf"]),
    ("As I said on the phone, here is our code.", ["Scan_001.pdf"]),
    ("Here is the ordinance you asked about.", ["Fence Ordinance.pdf"]),
    ("Attached is the ordinance.\n\nThanks Tom!", ["Ordinance.pdf"]),
    ("Attached are the ordinances.\n\nThank you Mrs. Johnson.", ["Ordinances.pdf"]),
])
def test_a_bare_code_a_conversation_or_a_closing_to_someone_else_is_not_the_code(tmp_path, status, text, names):
    """5: a bare "the code" names nothing (a keypad's, a gate's); "I mentioned on the phone", "when you called" are a
    conversation we never had; "Thanks Tom!" is a note for Tom."""
    r, notices = _millen(tmp_path, status, text, names, sig="Thanks" not in text and "Thank you" not in text)
    _held_with_one_notice(tmp_path, r, notices, "millen", names)


def test_a_bare_code_against_a_named_one():
    def said(t):
        return orr._said(t, "Millen", ("requests@project.example", "Georgia Civic Records Project"))

    for t in ["Attached is the code.", "Here are the codes.", "Attached is the complete code.", "Attached is the "
              "Georgia code.", "Attached is the county code."]:
        assert not said(t).code, t
    for t in ["Attached is our code.", "Here is the Code.", "Attached is the city code.", "Attached is Millen's code.",
              "Here's the code book scanned", "Attached is the municipal code.", "Attached is the charter.",
              "Attached are the ordinances.", "The code is too large to email in one piece, so here is part 1 of 4."]:
        assert said(t).code, t


def test_every_save_as_the_code_is_told_once_with_the_rule_and_how_to_undo(tmp_path):
    """The safety net: the rules read names and words and can be wrong, so every file saved as the Code is named to
    the owner, with the rule that took it (by name, or by the reply's words, quoted) and how to undo it."""
    r, notices = _millen(tmp_path, "sent", "Good morning,\n\nAttached is our code.", ["Scan_0001.pdf",
                                                                                     "Millen Charter.pdf"])
    title, body = saved_notice(notices, ["millen__Scan_0001.pdf", "millen__Millen_Charter.pdf"])
    assert title == "Open records: Millen sent its Code: check what was saved"
    assert '`imports/ordinances/millen__Scan_0001.pdf`, by the reply\'s words: "Attached is our code."' in body
    assert '`imports/ordinances/millen__Millen_Charter.pdf`, by its name ("Millen Charter.pdf")' in body
    assert "set the request's status back to **sent**" in body and r.tracker.data["millen"]["status"] == "fulfilled"
    assert r.tracker.data["millen"]["history"][-1]["note"] == "saved notice: issue#1"
    # with a cost, still one notice, its title saying so
    r, notices = _millen(tmp_path / "b", "acknowledged", "Here is the Code. The fee for copies is $10, payable at City "
                                                         "Hall.", ["Scan.pdf"])
    title, body = saved_notice(notices, ["millen__Scan.pdf"], 'by the reply\'s words: "Here is the Code."')
    assert title == "Open records: Millen mentions a cost; files saved as the Code"
    assert "status back to **acknowledged**" in body
    # page images
    r, notices = requests_for(tmp_path / "c", "big", "Bigtown", "sent")
    m = reply("big", "Attached are the scanned pages of our Code of Ordinances." + quoted("big", "Bigtown"))
    for i in range(1, 4):
        add(m, jpg(), f"p{i}.jpg", subtype="jpeg")
    check(r, m)
    title, body = saved_notice(notices, [f"big__p{i}.jpg" for i in range(1, 4)], "page images")
    assert "page images" in title
    # a second reply to a request already fulfilled: nothing to set back
    r, notices = _millen(tmp_path / "d", "fulfilled", "", ["Code of Ordinances.pdf"])
    assert "set the request's status back" not in saved_notice(notices, ["millen__Code_of_Ordinances.pdf"])[1]
