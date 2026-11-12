"""A letter from outbox/ goes out once, from the project mailbox, with the FEC key line filled in. Fictional data."""
from gacrawl.openrecords import Mailbox, send_letter


class FakeSMTP:
    sent = []

    def login(self, u, p):
        pass

    def send_message(self, m):
        FakeSMTP.sent.append(m)

    def quit(self):
        pass


def test_letter_sent_once(tmp_path):
    env = {"ORR_SMTP_HOST": "smtp.example.test", "ORR_USER": "u", "ORR_PASSWORD": "p", "ORR_FROM": "project@example.test",
           "FEC_API_KEY": "KEY123"}
    p = tmp_path / "letter.txt"
    p.write_text("To: api@example.test\nSubject: A request\n\nHello,\n\n{FEC_KEY_LINE}\n\nThanks\n")
    r = send_letter(p, Mailbox(env), env, smtp_factory=FakeSMTP)
    assert r["sent"] and r["with_key"] and (tmp_path / "letter.txt.sent").exists()
    m = FakeSMTP.sent[-1]
    assert m["To"] == "api@example.test" and m["From"] == "project@example.test" and "upgraded: KEY123" in m.get_content()
    again = send_letter(p, Mailbox(env), env, smtp_factory=FakeSMTP)
    assert not again["sent"] and again["why"].startswith("already sent") and len(FakeSMTP.sent) == 1


def test_letter_without_key_offers_it(tmp_path):
    env = {"ORR_SMTP_HOST": "smtp.example.test", "ORR_USER": "u", "ORR_PASSWORD": "p", "ORR_FROM": "project@example.test"}
    p = tmp_path / "l.txt"
    p.write_text("To: api@example.test\nSubject: S\n\n{FEC_KEY_LINE}\n")
    send_letter(p, Mailbox(env), env, smtp_factory=FakeSMTP)
    assert "reply to this message and we will send it" in FakeSMTP.sent[-1].get_content()
