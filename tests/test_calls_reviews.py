"""Missed-call text-back on the business's own Twilio number, and review requests (one tap or automatic)."""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import make_business
from valeria_one import channels as CH
from valeria_one import db
from valeria_one.channels.twilio_sms import TwilioSMS, signature
from valeria_one.services import Blocked, Invalid
from valeria_one.services import businesses as B
from valeria_one.services import deals as D
from valeria_one.services import followups as FU
from valeria_one.services import reviews as RV

CFG = {"account_sid": "AC1", "auth_token": "TT", "from_number": "+15550001111"}


@pytest.fixture
def twilio(monkeypatch):
    """Every Twilio call answered by a fake; what we sent is in .sent."""
    class Fake:
        sent, admin = [], []

        def handler(self, req):
            if req.url.path.endswith("/IncomingPhoneNumbers.json"):
                self.admin.append(("list", dict(req.url.params)))
                if req.url.params.get("PhoneNumber") == "+15550001111":
                    return httpx.Response(200, json={"incoming_phone_numbers": [{"sid": "PN1"}]})
                return httpx.Response(200, json={"incoming_phone_numbers": []})
            if "/IncomingPhoneNumbers/" in req.url.path:
                self.admin.append(("update", parse_qs(req.content.decode())))
                return httpx.Response(200, json={"sid": "PN1"})
            self.sent.append(parse_qs(req.content.decode()))
            return httpx.Response(201, json={"sid": f"SM{len(self.sent)}", "status": "queued"})
    f = Fake()
    f.sent, f.admin = [], []
    real = CH.adapter
    monkeypatch.setattr(CH, "adapter", lambda ch, http=None: TwilioSMS(http=httpx.Client(transport=httpx.MockTransport(f.handler)))
                        if ch == "sms" else real(ch, http))
    return f


def connect(client, h, extra=None, register=True, monkeypatch=None):
    if register:
        monkeypatch.setenv("VO_REGISTER_WEBHOOKS", "1")
    r = client.post("/v1/channels", headers=h, json={"channel": "sms", "name": "Shop line", "config": {**CFG, **(extra or {})}})
    assert r.status_code == 201, r.text
    return r.json()["webhook_url"].replace("https://one.test", "")


def call(client, path, form):
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature("TT", "https://one.test" + path, form)})


def test_connect_points_the_number_at_us(client, us, twilio, monkeypatch):
    _, h = us
    path = connect(client, h, monkeypatch=monkeypatch)
    kind, upd = twilio.admin[-1]
    assert kind == "update" and upd["SmsUrl"] == ["https://one.test" + path] and upd["VoiceUrl"] == ["https://one.test" + path + "/voice"]
    bad = client.post("/v1/channels", headers=h, json={"channel": "sms", "name": "x", "config": {**CFG, "from_number": "+15559999999"}})
    assert bad.status_code == 422 and "not a number in this Twilio account" in bad.json()["error"]["message"]


def test_missed_call_without_a_phone_texts_at_once(client, us, twilio, monkeypatch):
    bz, h = us
    path = connect(client, h, monkeypatch=monkeypatch)
    form = {"From": "+19514343352", "To": "+15550001111", "CallSid": "CA1"}
    assert client.post(path + "/voice", data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    r = call(client, path + "/voice", form)
    assert r.status_code == 200 and "<Say" in r.text and "<Hangup/>" in r.text and "just sent you a text" in r.text
    assert twilio.sent[-1]["To"] == ["+19514343352"] and "Sorry we missed your call" in twilio.sent[-1]["Body"][0]
    assert bz["name"] in twilio.sent[-1]["Body"][0]
    n = len(twilio.sent)
    call(client, path + "/voice", {**form, "CallSid": "CA2"})                     # calls again at once: no second text
    assert len(twilio.sent) == n
    c = client.get("/v1/contacts", headers=h).json()[0]
    assert c["phone"] == "+19514343352" and c["source"] == "missed call"
    conv = client.get("/v1/conversations", headers=h).json()[0]
    assert [m["direction"] for m in client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"]] == ["out"]


def test_ring_first_then_text_only_if_missed(client, us, twilio, monkeypatch):
    _, h = us
    path = connect(client, h, extra={"forward_to": "(951) 555-0100", "missed_text": "{business}: we'll call you back soon!"},
                   monkeypatch=monkeypatch)
    form = {"From": "+19514343352", "To": "+15550001111", "CallSid": "CA1"}
    r = call(client, path + "/voice", form)
    assert '<Dial timeout="20"' in r.text and "<Number>+19515550100</Number>" in r.text and 'callerId="+19514343352"' in r.text
    assert f'action="https://one.test{path}/voice/done"' in r.text and twilio.sent == []
    call(client, path + "/voice/done", {**form, "DialCallStatus": "completed"})
    assert twilio.sent == []                                                       # they picked up: no text
    call(client, path + "/voice/done", {**form, "DialCallStatus": "no-answer"})
    assert twilio.sent[-1]["Body"][0].endswith("we'll call you back soon!")
    with db.tx() as conn:
        types = [r[0] for r in conn.execute(db.events.select().with_only_columns(db.events.c.type))]
    assert "call.answered" in types and "call.missed" in types


def test_text_back_can_be_off_and_hidden_numbers_are_skipped(client, us, twilio, monkeypatch):
    _, h = us
    path = connect(client, h, extra={"text_back": "off"}, monkeypatch=monkeypatch)
    r = call(client, path + "/voice", {"From": "+19514343352", "To": "+15550001111"})
    assert "Please call again later" in r.text and twilio.sent == []
    call(client, path + "/voice", {"From": "anonymous", "To": "+15550001111"})
    assert twilio.sent == []


# ---------------------------------------------------------------- review requests
def _customer(client, h, consent=True, phone="+19514343352"):
    r = client.post("/v1/contacts", headers=h, json={"name": "Ana Ruiz", "phone": phone, "marketing_consent": consent,
                                                     "marketing_consent_source": "booking form"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _midday(biz):
    """Today at noon where the business is: a marketing message is allowed then, whatever the real clock says."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(biz["timezone"])).replace(hour=12, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def test_review_link_rules(us):
    bz, _ = us
    with db.tx() as conn:
        for bad in ("g.page/r/abc", "ftp://x", "https://x y"):
            with pytest.raises(Invalid):
                RV.set_url(conn, bz["id"], "t", bad)
        RV.set_url(conn, bz["id"], "t", "https://g.page/r/abc/review")
        assert B.get(conn, bz["id"])["review_url"] == "https://g.page/r/abc/review"


def test_ask_once_count_the_click(client, us, twilio, monkeypatch):
    bz, h = us
    connect(client, h, register=False, monkeypatch=monkeypatch)
    cid = _customer(client, h)
    with db.tx() as conn:
        nolink = B.get(conn, bz["id"])
    with pytest.raises(Invalid):
        RV.ask(nolink, "t", cid)                                                     # no link yet
    with db.tx() as conn:
        RV.set_url(conn, bz["id"], "t", "https://g.page/r/abc/review")
        biz = B.get(conn, bz["id"])
    noon = _midday(biz)                                                            # outside the US quiet hours (21–8)
    RV.ask(biz, "t", cid, now=noon)
    body = twilio.sent[-1]["Body"][0]
    assert body.startswith("Hi Ana, thank you for choosing") and "/r/" in body
    with pytest.raises(Invalid):
        RV.ask(biz, "t", cid, now=noon)                                              # once every 90 days
    token = re.search(r"/r/([\w-]+)", body).group(1)
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://g.page/r/abc/review"
    assert client.get("/r/nope", follow_redirects=False).status_code == 404
    with db.tx() as conn:
        assert RV.stats(conn, bz["id"]) == {"asked": 1, "clicked": 1, "days": 90}


def test_no_consent_no_ask(client, us, twilio, monkeypatch):
    bz, h = us
    connect(client, h, register=False, monkeypatch=monkeypatch)
    cid = _customer(client, h, consent=False)
    with db.tx() as conn:
        RV.set_url(conn, bz["id"], "t", "https://g.page/r/abc/review")
        biz = B.get(conn, bz["id"])
    with pytest.raises(Blocked):
        RV.ask(biz, "t", cid)
    with db.tx() as conn:
        assert not RV.recently_asked(conn, bz["id"], cid)                           # a blocked ask doesn't count


def test_automatic_after_each_appointment(client, us, twilio, monkeypatch):
    bz, h = us
    connect(client, h, register=False, monkeypatch=monkeypatch)
    cid = _customer(client, h)
    with db.tx() as conn:
        RV.set_url(conn, bz["id"], "t", "https://g.page/r/abc/review")
        biz = B.get(conn, bz["id"])
        FU.create_automation(conn, biz, "t", trigger="appointment.done", minutes=120,
                             message="Hi {name}! A review for {business}? {review_link}", kind="marketing")
    st = datetime.now(timezone.utc) - timedelta(hours=5)
    for i in range(2):
        with db.tx() as conn:
            a = D.book(conn, biz, "t", {"title": f"Visit {i}", "contact_id": cid, "starts_at": st + timedelta(minutes=i * 70),
                                        "ends_at": st + timedelta(minutes=i * 70 + 60)})
            D.set_appointment_status(conn, biz["id"], "t", a["id"], "done")
    from zoneinfo import ZoneInfo
    ny = (datetime.now(timezone.utc) + timedelta(hours=3)).astimezone(ZoneInfo(biz["timezone"]))
    if not 10 <= ny.hour < 18:                                                    # past the US quiet hours (21–8)
        ny = (ny + timedelta(days=1)).replace(hour=12, minute=0)
    res = FU.send_due(now=ny.astimezone(timezone.utc))
    assert res["sent"] == 1 and res["skipped"] == 1                                 # the second ask waits 90 days
    assert "/r/" in twilio.sent[-1]["Body"][0] and "{review_link}" not in twilio.sent[-1]["Body"][0]
    with db.tx() as conn:
        assert RV.stats(conn, bz["id"])["asked"] == 1
        skipped = conn.execute(db.scheduled_messages.select().where(db.scheduled_messages.c.status == "skipped")).mappings().one()
    assert skipped["error"] == "asked for a review in the last 90 days"


def test_review_screens(client):
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Review Shop", "country": "US", "industry": "generic",
                                 "email": "rv@example.com", "password": "una-clave-larga"})
    page = client.get("/app/followups", headers={"Accept-Language": "en"}).text
    assert "Add your review link" in page
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app/settings").text).group(1)
    client.post("/app/settings", data={"csrf": tok, "name": "Review Shop", "language": "en", "timezone": "America/New_York",
                                       "industry": "generic", "review_url": "https://g.page/r/xyz/review"})
    page = client.get("/app/followups", headers={"Accept-Language": "en"}).text
    assert "Ask every customer for a review 2 hours after their appointment" in page
    client.post("/app/followups/review-preset", data={"csrf": tok})
    page = client.get("/app/followups", headers={"Accept-Language": "en"}).text
    assert "Asking for reviews after every appointment" in page
    with db.tx() as conn:
        a = conn.execute(db.automations.select()).mappings().one()
    assert a["kind"] == "marketing" and "{review_link}" in a["message"] and a["minutes"] == 120
