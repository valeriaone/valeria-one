"""watb01 (v0.54.0): the missed-call text on WhatsApp — the business's own approved template (WhatsApp's rule to write first),
SMS when it can't go; the phone line's missed-call settings on the Channels page; one tap writes the «llamada perdida» template."""
from urllib.parse import parse_qs

import httpx
import pytest

from _va import csrf, owner_session
from test_calls_reviews import CFG, call, connect
from valeria_one import channels as CH
from valeria_one import crypto, db
from valeria_one.channels.twilio_sms import TwilioSMS
from valeria_one.channels.twilio_whatsapp import TwilioWhatsApp
from valeria_one.services import inbox as X


@pytest.fixture
def wire(monkeypatch):
    class Fake:
        sms, wa, fail_wa = [], [], False

        def handler(self, req):
            if req.url.path.endswith("/IncomingPhoneNumbers.json"):
                return httpx.Response(200, json={"incoming_phone_numbers": [{"sid": "PN1"}]})
            if "/IncomingPhoneNumbers/" in req.url.path:
                return httpx.Response(200, json={"sid": "PN1"})
            body = parse_qs(req.content.decode())
            if (body.get("From") or [""])[0].startswith("whatsapp:"):
                if self.fail_wa:
                    return httpx.Response(400, json={"code": 63016, "message": "template not approved"})
                self.wa.append(body)
            else:
                self.sms.append(body)
            return httpx.Response(201, json={"sid": f"SM{len(self.sms) + len(self.wa)}", "status": "queued"})
    f = Fake()
    f.sms, f.wa = [], []
    real = CH.adapter

    def adapter(ch, http=None):
        cl = httpx.Client(transport=httpx.MockTransport(f.handler))
        if ch == "sms":
            return TwilioSMS(http=cl)
        if ch == "whatsapp_twilio":
            return TwilioWhatsApp(http=cl)
        return real(ch, http)
    monkeypatch.setattr(CH, "adapter", adapter)
    return f


def _wa_line(us, approved=True):
    bz, _ = us
    with db.tx() as conn:
        acc = X.connect(conn, bz, "t", channel="whatsapp_twilio", name="WA", cfg={**CFG, "from_number": "+15550002222"})
        conn.execute(db.wa_templates.insert().values(business_id=bz["id"], channel_account_id=acc["id"], name="missed_call", language="en_US",
                                                     category="UTILITY", body="Hi {{1}}, we saw your call to {{2}}. Reply here.",
                                                     status="APPROVED" if approved else "PENDING", meta_id="HX" + "1" * 32))
    return acc


def _set(path_acc_id, **cfg):
    with db.tx() as conn:
        acc = conn.execute(db.channel_accounts.select().where(db.channel_accounts.c.id == path_acc_id)).mappings().one()
    X.save_config(acc["id"], {**crypto.open_(acc["config"]), **cfg})


def _sms_acc(bz):
    with db.tx() as conn:
        return conn.execute(db.channel_accounts.select().where(db.channel_accounts.c.business_id == bz["id"],
                                                               db.channel_accounts.c.channel == "sms")).mappings().one()["id"]


def test_missed_call_goes_on_whatsapp_with_their_approved_template(client, us, wire, monkeypatch):
    bz, h = us
    path = connect(client, h, monkeypatch=monkeypatch)
    _wa_line(us)
    _set(_sms_acc(bz), wa_template="missed_call")
    call(client, path + "/voice", {"From": "+19514343352", "To": "+15550001111", "CallSid": "CA1"})
    assert wire.wa and not wire.sms
    assert wire.wa[-1]["To"] == ["whatsapp:+19514343352"] and wire.wa[-1]["ContentSid"] == ["HX" + "1" * 32]


def test_no_approved_template_or_whatsapp_fails_then_sms(client, us, wire, monkeypatch):
    bz, h = us
    path = connect(client, h, monkeypatch=monkeypatch)
    _wa_line(us, approved=False)
    _set(_sms_acc(bz), wa_template="missed_call")
    call(client, path + "/voice", {"From": "+19514343352", "To": "+15550001111", "CallSid": "CA1"})
    assert wire.sms and not wire.wa                                                    # pending template: SMS as before
    with db.tx() as conn:
        conn.execute(db.wa_templates.update().values(status="APPROVED"))
        conn.execute(db.events.delete().where(db.events.c.type == "call.missed"))      # past the 30-minute quiet time
    wire.fail_wa, n = True, len(wire.sms)
    call(client, path + "/voice", {"From": "+19514343352", "To": "+15550001111", "CallSid": "CA2"})
    assert len(wire.sms) == n + 1                                                      # WhatsApp refused: the SMS still went


def test_the_phone_line_settings_and_the_one_tap_template(client, us, wire, monkeypatch):
    bz, h = us
    connect(client, h, monkeypatch=monkeypatch)
    _wa_line(us)
    owner_session(client, bz)
    page = client.get("/app/channels").text
    assert "Missed calls on this line" in page and 'name="wa_template"' in page and "missed_call" in page
    tok = csrf(client, "/app/channels")
    acc = _sms_acc(bz)
    client.post(f"/app/channels/{acc}/calls", data={"csrf": tok, "forward_to": "(951) 555-0100", "missed_text": "We'll call you back",
                                                     "text_back": "1", "wa_template": "missed_call"})
    with db.tx() as conn:
        cfg = crypto.open_(conn.execute(db.channel_accounts.select().where(db.channel_accounts.c.id == acc)).mappings().one()["config"])
    assert (cfg["forward_to"], cfg["missed_text"], cfg["text_back"], cfg["voicemail"], cfg["wa_template"]) == (
        "+19515550100", "We'll call you back", "on", "off", "missed_call")
    assert cfg["auth_token"] == "TT"                                                   # its keys are kept
    client.post(f"/app/channels/{acc}/calls", data={"csrf": tok, "wa_template": "not_mine"})
    with db.tx() as conn:
        cfg = crypto.open_(conn.execute(db.channel_accounts.select().where(db.channel_accounts.c.id == acc)).mappings().one()["config"])
    assert cfg["wa_template"] == ""                                                    # only their approved templates
    with db.tx() as conn:
        conn.execute(db.wa_templates.delete())
    r = client.post("/app/channels/missed-call-template", data={"csrf": tok}, follow_redirects=False)
    assert r.status_code == 303 and "/templates" in r.headers["location"]
    with db.tx() as conn:
        t = conn.execute(db.wa_templates.select()).mappings().one()
    assert t["name"] == "missed_call" and t["status"] == "DRAFT" and "{{1}}" in t["body"] and "{{2}}" in t["body"]
